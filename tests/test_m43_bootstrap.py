"""Focused tests for M4.3.3 AI model services composition."""

import ast
import pathlib

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.ai import (
    AiModelServices,
    AiModelServicesUnavailableError,
    ModelDescriptor,
    ModelRegistry,
    build_ai_model_services,
    configure_ai_model_services,
    get_ai_model_services,
)
from backend.ai import bootstrap
from backend.config import Settings


def _settings(tmp_path, models_dir=None):
    return Settings(_env_file=None, models_dir=models_dir)


def test_configured_models_dir_is_propagated(tmp_path):
    root = tmp_path / "models"
    services = build_ai_model_services(_settings(tmp_path, str(root)))

    assert services.storage.root == root.resolve()
    # Construction must not create the storage directory.
    assert not root.exists()


def test_unset_models_dir_uses_platform_default(monkeypatch):
    captured = {}

    class StubStorage:
        def __init__(self, root=None):
            captured["root"] = root
            self.root = pathlib.Path("/stub/models")

    monkeypatch.setattr(bootstrap, "ModelStorage", StubStorage)

    services = build_ai_model_services(_settings(None, None))

    assert captured["root"] is None
    assert isinstance(services.storage, StubStorage)


def test_container_and_manager_share_the_registry(tmp_path):
    services = build_ai_model_services(
        _settings(tmp_path, str(tmp_path / "models"))
    )

    assert isinstance(services.registry, ModelRegistry)
    assert services.manager.model_registry is services.registry

    descriptor = ModelDescriptor(
        name="Test Model",
        version="1.0",
        modalities=("text",),
        model_id="vendor.model",
    )
    services.manager.register_descriptor(descriptor)
    assert services.registry.find("vendor.model") is descriptor


def test_configure_constructs_services_once(monkeypatch, tmp_path):
    calls = []

    def counting_build(settings):
        calls.append(settings)
        return build_ai_model_services(settings)

    monkeypatch.setattr(bootstrap, "build_ai_model_services", counting_build)
    app = FastAPI()
    settings = _settings(tmp_path, str(tmp_path / "models"))

    services = configure_ai_model_services(app, settings)

    assert len(calls) == 1
    assert calls[0] is settings
    assert app.state.ai_models is services
    assert app.state.ai_models_error is None
    assert services.manager.model_registry is services.registry


def test_lifespan_configures_ai_services_exactly_once():
    source = pathlib.Path("backend/main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "configure_ai_model_services"
    ]

    assert len(calls) == 1
    # Startup still gates on the migration state.
    assert "check_database_migrated" in source


def test_dependency_returns_shared_services(tmp_path):
    services = build_ai_model_services(
        _settings(tmp_path, str(tmp_path / "models"))
    )
    app = FastAPI()
    app.state.ai_models = services

    @app.get("/services")
    def read_services(
        resolved: AiModelServices = Depends(get_ai_model_services),
    ):
        return {"root": str(resolved.storage.root)}

    response = TestClient(app).get("/services")

    assert response.status_code == 200
    assert response.json()["root"] == str(services.storage.root)


def test_dependency_returns_503_when_unavailable():
    app = FastAPI()
    app.state.ai_models = None
    app.state.ai_models_error = (
        "Android requires an explicit app-private model storage root"
    )

    @app.get("/services")
    def read_services(
        resolved: AiModelServices = Depends(get_ai_model_services),
    ):
        return {"root": str(resolved.storage.root)}

    response = TestClient(app).get("/services")

    assert response.status_code == 503
    assert "Android requires" in response.json()["detail"]


def test_unavailable_storage_is_reported_clearly(monkeypatch, tmp_path):
    class FailingStorage:
        def __init__(self, root=None):
            raise ValueError(
                "Android requires an explicit app-private model storage root"
            )

    monkeypatch.setattr(bootstrap, "ModelStorage", FailingStorage)
    settings = _settings(tmp_path, None)

    with pytest.raises(AiModelServicesUnavailableError) as excinfo:
        build_ai_model_services(settings)
    assert "Model storage is unavailable" in str(excinfo.value)
    assert "Android requires" in str(excinfo.value)

    # Startup must not be blocked: the failure is recorded on app state.
    app = FastAPI()
    assert configure_ai_model_services(app, settings) is None
    assert app.state.ai_models is None
    assert "Android requires" in app.state.ai_models_error
    assert not (tmp_path / "models").exists()
