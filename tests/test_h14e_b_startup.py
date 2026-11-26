"""Startup ordering tests for HVSA H1.4E-B managed adapter reconciliation.

Every startup dependency is replaced with a recorder, so these tests never
touch a real database, keyring, provider, or network.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

import backend.main as main_mod


class _FakeRecovery:
    """Stand-in for RecoveryService: records nothing, deletes nothing."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def process_pending_deletions(self, batch_size: int = 5) -> int:
        return 0


def _patch_lifespan(monkeypatch, order: list) -> None:
    """Replace every startup dependency with a recorder."""
    monkeypatch.setattr(main_mod, "check_database_migrated", lambda: None)
    monkeypatch.setattr(main_mod, "configure_ai_model_services", lambda app, s: None)
    monkeypatch.setattr(main_mod, "LocalAdapter", lambda path: object())
    monkeypatch.setattr(main_mod, "register", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "remove", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "RecoveryService", _FakeRecovery)
    monkeypatch.setattr(
        main_mod, "start_ai_worker", lambda app, settings: order.append("ai_worker")
    )

    async def _stop_ai_worker(app):
        order.append("stop_ai_worker")

    monkeypatch.setattr(main_mod, "stop_ai_worker", _stop_ai_worker)

    def _load_legacy():
        order.append("legacy")
        return None

    monkeypatch.setattr(main_mod, "load_google_drive_credentials", _load_legacy)


def _run_lifespan(order: list) -> None:
    async def _run():
        app = FastAPI()
        async with main_mod.lifespan(app):
            order.append("serving")

    asyncio.run(_run())


def test_lifespan_restores_managed_adapters_between_legacy_and_dependents(
    monkeypatch,
):
    order: list = []
    _patch_lifespan(monkeypatch, order)

    def _restore(session_factory):
        order.append("managed_restore")
        assert session_factory is main_mod.SessionLocal
        return {"restored": 0}

    monkeypatch.setattr(main_mod, "restore_managed_adapters", _restore)

    _run_lifespan(order)

    assert order == [
        "legacy",
        "managed_restore",
        "ai_worker",
        "serving",
        "stop_ai_worker",
    ]


def test_lifespan_continues_when_managed_restoration_raises(monkeypatch):
    order: list = []
    _patch_lifespan(monkeypatch, order)

    def _restore(session_factory):
        order.append("managed_restore")
        raise RuntimeError("credentials unavailable")

    monkeypatch.setattr(main_mod, "restore_managed_adapters", _restore)

    _run_lifespan(order)

    assert order == [
        "legacy",
        "managed_restore",
        "ai_worker",
        "serving",
        "stop_ai_worker",
    ]


def test_restore_managed_adapters_is_importable() -> None:
    """The lifespan uses this exact symbol; a rename must not go unnoticed."""
    from backend.api.v1.endpoints.storage_accounts import restore_managed_adapters

    assert main_mod.restore_managed_adapters is restore_managed_adapters
