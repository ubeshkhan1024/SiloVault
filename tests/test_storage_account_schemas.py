"""Focused tests for HVSA H1.5.3 storage account API schemas.

Covers request validation (provider id, display name, partial updates), the
guarantee that clients can never supply ``credentials_ref``, that responses
never expose credentials, and the defensive-copy behaviour of configuration.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.models import StorageAccount
from backend.schemas import (
    StorageAccountCreate,
    StorageAccountListResponse,
    StorageAccountResponse,
    StorageAccountUpdate,
)


def make_orm_account(configuration=None) -> StorageAccount:
    """Build an unpersisted ORM account with every column populated."""
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
    return StorageAccount(
        id="11111111-1111-1111-1111-111111111111",
        provider_id="GOOGLE_DRIVE",
        display_name="Work Drive",
        credentials_ref="secret-ref-do-not-expose",
        status="connected",
        configuration={"nested": {"a": 1}} if configuration is None else configuration,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# StorageAccountCreate
# ---------------------------------------------------------------------------


def test_create_accepts_valid_payload_and_trims_display_name():
    schema = StorageAccountCreate(
        provider_id="GOOGLE_DRIVE",
        display_name="  Work Drive  ",
        configuration={"storage_root": "/data"},
    )

    assert schema.provider_id == "GOOGLE_DRIVE"
    assert schema.display_name == "Work Drive"
    assert schema.configuration == {"storage_root": "/data"}


def test_create_configuration_defaults_are_independent():
    first = StorageAccountCreate(provider_id="LOCAL", display_name="A")
    second = StorageAccountCreate(provider_id="LOCAL", display_name="B")

    assert first.configuration == {}
    first.configuration["root"] = "/data"
    assert second.configuration == {}


def test_create_rejects_client_supplied_credentials_ref():
    with pytest.raises(ValidationError):
        StorageAccountCreate(
            provider_id="GOOGLE_DRIVE",
            display_name="Work",
            credentials_ref="storage-account:other",
        )


@pytest.mark.parametrize(
    "provider_id",
    [
        "local",
        "Local",
        "1LOCAL",
        "_LOCAL",
        "LOCAL ",
        " LOCAL",
        "LOC-AL",
        "",
        "L" * 65,
    ],
)
def test_create_rejects_invalid_provider_ids(provider_id):
    with pytest.raises(ValidationError):
        StorageAccountCreate(provider_id=provider_id, display_name="Work")


@pytest.mark.parametrize("provider_id", ["LOCAL", "GOOGLE_DRIVE", "BOX2", "A"])
def test_create_accepts_canonical_provider_ids(provider_id):
    assert StorageAccountCreate(
        provider_id=provider_id, display_name="Work"
    ).provider_id == provider_id


@pytest.mark.parametrize("display_name", ["", "   ", "\t\n ", "x" * 256])
def test_create_rejects_invalid_display_names(display_name):
    with pytest.raises(ValidationError):
        StorageAccountCreate(provider_id="LOCAL", display_name=display_name)


def test_create_rejects_non_object_configuration():
    with pytest.raises(ValidationError):
        StorageAccountCreate(
            provider_id="LOCAL", display_name="Work", configuration=["/data"]
        )


# ---------------------------------------------------------------------------
# StorageAccountUpdate (partial PATCH)
# ---------------------------------------------------------------------------


def test_update_allows_empty_partial_payload():
    update = StorageAccountUpdate()

    assert update.model_dump(exclude_unset=True) == {}


def test_update_only_reports_supplied_fields():
    by_name = StorageAccountUpdate(display_name="  New Name  ")
    assert by_name.model_dump(exclude_unset=True) == {"display_name": "New Name"}

    by_config = StorageAccountUpdate(configuration={"storage_root": "/data"})
    assert by_config.model_dump(exclude_unset=True) == {
        "configuration": {"storage_root": "/data"}
    }


def test_update_rejects_provider_id_and_credentials_ref():
    with pytest.raises(ValidationError):
        StorageAccountUpdate(provider_id="LOCAL")

    with pytest.raises(ValidationError):
        StorageAccountUpdate(credentials_ref="storage-account:other")


@pytest.mark.parametrize("display_name", ["", "   ", "\t\n ", "x" * 256])
def test_update_rejects_invalid_display_names(display_name):
    with pytest.raises(ValidationError):
        StorageAccountUpdate(display_name=display_name)


# ---------------------------------------------------------------------------
# StorageAccountResponse
# ---------------------------------------------------------------------------


def test_response_never_exposes_credentials_or_extra_orm_fields():
    response = StorageAccountResponse.model_validate(make_orm_account())

    dumped = response.model_dump()
    assert set(dumped) == {
        "id",
        "provider_id",
        "display_name",
        "status",
        "configuration",
        "created_at",
        "updated_at",
    }
    assert "credentials_ref" not in dumped
    assert "secret-ref-do-not-expose" not in str(dumped)


def test_response_configuration_is_a_deep_defensive_copy():
    source_configuration = {"nested": {"a": 1}}
    account = make_orm_account(configuration=source_configuration)

    response = StorageAccountResponse.model_validate(account)
    assert response.configuration == source_configuration
    assert response.configuration is not source_configuration

    response.configuration["nested"]["a"] = 999
    response.configuration["new"] = "added"

    assert source_configuration == {"nested": {"a": 1}}
    assert account.configuration == {"nested": {"a": 1}}


# ---------------------------------------------------------------------------
# StorageAccountListResponse
# ---------------------------------------------------------------------------


def test_list_response_holds_responses_and_total():
    response = StorageAccountResponse.model_validate(make_orm_account())

    listing = StorageAccountListResponse(items=[response], total=1)

    assert listing.total == 1
    assert listing.items[0].id == "11111111-1111-1111-1111-111111111111"
    assert "credentials_ref" not in listing.model_dump()["items"][0]
