from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from backend.ai.interfaces import ModelDescriptor, ModelRegistry, ModelValidationError

# Trusted, code-controlled model catalogue for SiloVault.

# Descriptors here are written in code and reviewed like source.  They are never
# derived from HTTP/client input, and no download, network, or installation
# behaviour belongs in this module.

# The catalogue ships empty on purpose: no model URL, checksum, size, or licence
# has been independently verified yet, and inventing metadata would be unsafe.
# Entries are added only when their metadata is genuinely established.
#
# Currently registered one entry for Microsoft's Florence-2-base vision model.
MODEL_CATALOGUE: Tuple[ModelDescriptor, ...] = (
    ModelDescriptor(
        name="Florence-2 Base",
        version="1.0",
        modalities=("vision",),
        model_id="microsoft.florence-2-base",
        compatible_runtimes=("florence2-cpu",),
    ),
)

# Algorithms the installation service can verify.  Kept in sync with
# ``backend.ai.installation``; only these may appear on installable entries.
_SUPPORTED_CHECKSUMS = {"sha256": 64, "sha512": 128}


def register_catalogue(
    registry: ModelRegistry,
    descriptors: Optional[Iterable[ModelDescriptor]] = None,
) -> List[str]:
    """Validate and register catalogue *descriptors* into *registry*.

    Defaults to :data:`MODEL_CATALOGUE`.  Already-registered identical entries
    are skipped, so construction is idempotent.  A conflicting registration or
    invalid metadata raises :class:`ModelValidationError` from the catalogue or
    the registry and leaves nothing partially registered by this call for that
    entry.

    Returns the identifiers present after registration.
    """
    entries = MODEL_CATALOGUE if descriptors is None else tuple(descriptors)
    identifiers: List[str] = []
    for descriptor in entries:
        if not isinstance(descriptor, ModelDescriptor):
            raise ModelValidationError(
                "catalogue entries must be ModelDescriptor instances"
            )

        existing = registry.find(descriptor.identifier)
        if existing is not None:
            if existing == descriptor:
                identifiers.append(existing.identifier)
                continue
            raise ModelValidationError(
                f"Model '{descriptor.identifier}' is already registered "
                "with different metadata"
            )

        ModelRegistry.validate_descriptor(descriptor)
        _validate_installable_metadata(descriptor)
        identifiers.append(registry.register(descriptor))
    return identifiers


def _validate_installable_metadata(descriptor: ModelDescriptor) -> None:
    """Enforce catalogue policy for entries that declare a download source.

    Since the default ``MODEL_CATALOGUE`` entry does not declare a
    ``download_url``, this guard is a no-op for the shipped descriptor.
    """
    if descriptor.download_url is None:
        return

    import urllib.parse as urlparse

    parsed = urlparse.urlparse(descriptor.download_url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ModelValidationError(
            "installable catalogue models require an absolute https download_url"
        )
    if not descriptor.checksum:
        raise ModelValidationError(
            "installable catalogue models require a checksum"
        )

    value = descriptor.checksum.strip()
    if ":" in value:
        algorithm, _, digest = value.partition(":")
        algorithm = algorithm.strip().lower()
    else:
        algorithm, digest = "sha256", value
    digest = digest.strip().lower()
    expected_length = _SUPPORTED_CHECKSUMS.get(algorithm)
    if expected_length is None:
        raise ModelValidationError(
            f"unsupported checksum algorithm for installable model: {algorithm!r}"
        )
    if len(digest) != expected_length or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ModelValidationError(
            f"invalid {algorithm} checksum for installable model"
        )