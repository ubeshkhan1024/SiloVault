from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Tuple

# Supported checksum algorithms and their hexadecimal digest lengths.
# Duplicated here to avoid circular imports if needed, or imported from installation.
_SUPPORTED_CHECKSUMS = {"sha256": 64, "sha512": 128}

# Shared validation utilities
def _validate_checksum_format(checksum: str) -> Tuple[str, str]:
    """Validate and normalize checksum format.

    Returns ``(algorithm, digest)`` where algorithm is lowercased and digest is
    lowercased hex without any prefix.
    """
    value = checksum.strip()
    if ":" in value:
        algorithm, digest = value.split(":", 1)
        algorithm = algorithm.strip().lower()
    else:
        algorithm, digest = "sha256", value
    digest = digest.strip().lower()
    if algorithm not in _SUPPORTED_CHECKSUMS:
        raise ValueError(f"Unsupported checksum algorithm: {algorithm!r}")
    expected_length = _SUPPORTED_CHECKSUMS[algorithm]
    if len(digest) != expected_length or re.fullmatch(r"[0-9a-f]+", digest) is None:
        raise ValueError(
            f"Invalid {algorithm} checksum: expected {expected_length} hex characters"
        )
    return algorithm, digest

# v1 manifest validation (existing single-file format)
def _validate_v1_manifest(manifest: Dict) -> None:
    """Validate v1 single-file manifest structure."""
    required = ["identifier", "version", "artifact", "size", "checksum"]
    for key in required:
        if key not in manifest:
            raise ValueError(f"Missing required v1 field: {key}")
    if not isinstance(manifest["identifier"], str):
        raise ValueError("identifier must be a string")
    if not isinstance(manifest["version"], str):
        raise ValueError("version must be a string")
    if not isinstance(manifest["artifact"], str):
        raise ValueError("artifact must be a string")
    if not isinstance(manifest["size"], int) or manifest["size"] < 0:
        raise ValueError("size must be a non-negative integer")
    if not isinstance(manifest["checksum"], str):
        raise ValueError("checksum must be a string")
    _validate_checksum_format(manifest["checksum"])

# v2 manifest validation (new multi-file format)
def _validate_v2_manifest(manifest: Dict) -> None:
    """Validate v2 multi-file manifest structure and content."""
    # Schema version validation
    schema_version = manifest.get("schema_version")

    if type(schema_version) is not int or schema_version not in (1, 2):
        raise ValueError("schema_version must be an integer, either 1 or 2")
    if manifest["schema_version"] == 1:
        _validate_v1_manifest(manifest)
        return

    # Required fields for v2
    required = ["schema_version", "identifier", "version", "packaging", "installed_at", "entry_point", "files"]
    for key in required:
        if key not in manifest:
            raise ValueError(f"Missing required v2 field: {key}")

    # Validate identifier
    if not isinstance(manifest["identifier"], str):
        raise ValueError("identifier must be a string")
    if not manifest["identifier"]:
        raise ValueError("identifier must not be empty")
    if "/" in manifest["identifier"] or "\\" in manifest["identifier"]:
        raise ValueError("identifier must not contain path separators")

    # Validate version
    if not isinstance(manifest["version"], str):
        raise ValueError("version must be a string")
    if not manifest["version"]:
        raise ValueError("version must not be empty")

    # Validate packaging info
    if not isinstance(manifest["packaging"], str):
        raise ValueError("packaging must be a string")
    if not manifest["packaging"]:
        raise ValueError("packaging must not be empty")

    # Validate installed_at timestamp
    installed_at = manifest.get("installed_at")
    if not isinstance(installed_at, str) or not installed_at:
        raise ValueError("installed_at must be a non-empty ISO 8601 timestamp")

    try:
        parsed_timestamp = datetime.fromisoformat(
            installed_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("installed_at must be a valid ISO 8601 timestamp") from exc

    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        raise ValueError("installed_at must include an explicit timezone")
    
    # Validate entry_point
    if not isinstance(manifest["entry_point"], str):
        raise ValueError("entry_point must be a string")
    if not manifest["entry_point"]:
        raise ValueError("entry_point must not be empty")

    # Validate files array
    if not isinstance(manifest["files"], list):
        raise ValueError("files must be an array")
    if not manifest["files"]:
        raise ValueError("files must not be empty")
    seen_paths = set()
    for idx, file_info in enumerate(manifest["files"]):
        if not isinstance(file_info, dict):
            raise ValueError(f"files[{idx}] must be an object")
        file_required = ["path", "size", "sha256"]
        for key in file_required:
            if key not in file_info:
                raise ValueError(f"files[{idx}] missing required field: {key}")
        # Path validation
            from pathlib import PurePosixPath, PureWindowsPath

        path = file_info["path"]
        if not isinstance(path, str):
            raise ValueError(f"files[{idx}].path must be a string")
        if not path:
            raise ValueError(f"files[{idx}].path must not be empty")

        # Reject absolute paths, drive paths and UNC paths.
        windows_path = PureWindowsPath(path)
        posix_path = PurePosixPath(path)

        if (
            posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
        ):
            raise ValueError(f"files[{idx}].path must be relative")

        # Use forward slashes as the canonical manifest separator.
        if "\\" in path:
            raise ValueError(f"files[{idx}].path must use forward slashes")

        parts = path.split("/")

        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"files[{idx}].path contains an invalid path component")

        # Reject Windows reserved names and alternate data stream syntax.
        reserved_names = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }

        for part in parts:
            if ":" in part:
                raise ValueError(f"files[{idx}].path contains an invalid colon")
            if part.rstrip(" .").split(".")[0].upper() in reserved_names:
                raise ValueError(f"files[{idx}].path contains a reserved Windows name")
            if part.endswith((" ", ".")):
                raise ValueError(f"files[{idx}].path contains an invalid Windows name")

        norm_path = path.casefold()

        if norm_path in seen_paths:
            raise ValueError(f"Duplicate file path: {path}")

        seen_paths.add(norm_path)
        
        # Validate size
        size = file_info["size"]
        if type(size) is not int or size < 0:
            raise ValueError(f"files[{idx}].size must be a non-negative integer")
        
        # Validate SHA-256
        sha256 = file_info["sha256"]
        if not isinstance(sha256, str):
            raise ValueError(f"files[{idx}].sha256 must be a string")
        if len(sha256) != 64 or not re.fullmatch(r"[0-9a-f]+", sha256):
            raise ValueError(f"files[{idx}].sha256 must be a 64-character hex string")
            
    # Verify entry_point is included in files
    entry_point_norm = manifest["entry_point"].replace("\\", "/").lower()
    if not any(f["path"].replace("\\", "/").lower() == entry_point_norm for f in manifest["files"]):
        raise ValueError("entry_point must be listed in files array")
