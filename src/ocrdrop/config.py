"""User-local, non-secret configuration for scnet-ocrdrop."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict


CONFIG_VERSION = 1
DEFAULT_REMOTE_ROOTS = {
    "mineru3": "softwares/projects/scnet-ocrdrop/deployments/mineru3",
    "mineru4": "softwares/projects/scnet-ocrdrop/deployments/mineru4",
    "paddleocr": "softwares/projects/scnet-ocrdrop/deployments/paddleocr",
}
SECRET_KEY_MARKERS = (
    "access_key",
    "secret_key",
    "password",
    "private_key",
    "token",
)
PRIVATE_METADATA_KEYS = {
    "home_path",
    "key_path",
    "platform_user",
    "username",
}


def config_root() -> Path:
    override = os.environ.get("OCRDROP_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base).expanduser() / "scnet-ocrdrop"
    return Path.home() / ".config" / "scnet-ocrdrop"


def config_path() -> Path:
    return config_root() / "config.json"


def validate_profile_name(value: str, label: str = "profile") -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value or ""):
        raise ValueError("%s contains unsafe characters" % label)
    return value


def normalize_remote_root(value: str) -> str:
    """Return a safe home-relative deployment root.

    Absolute paths are intentionally excluded from saved configuration. They may
    still be passed explicitly for one command or stored in the private remote
    deployment config.
    """

    raw = str(value or "").strip()
    if raw.startswith("~/"):
        raw = raw[2:]
    raw = raw.strip("/")
    if not raw:
        raise ValueError("remote root must not be empty")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("remote root must be a normalized relative path")
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts):
        raise ValueError("remote root contains unsafe characters")
    return "/".join(parts)


def default_config() -> Dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "transport": "ssh",
        "default_ocr_backend": "mineru3",
        "remote_roots": dict(DEFAULT_REMOTE_ROOTS),
    }


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_user_config() -> Dict[str, Any]:
    result = default_config()
    saved = _read_json(config_path())
    result.update(saved)
    roots = dict(DEFAULT_REMOTE_ROOTS)
    saved_roots = saved.get("remote_roots")
    if isinstance(saved_roots, dict):
        for name, value in saved_roots.items():
            if isinstance(name, str) and isinstance(value, str):
                try:
                    roots[validate_profile_name(name, "OCR backend")] = (
                        normalize_remote_root(value)
                    )
                except ValueError:
                    continue
    result["remote_roots"] = roots
    return result


def _reject_secrets(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            qualified = (path + "." + str(key)).strip(".")
            if any(marker in lowered for marker in SECRET_KEY_MARKERS):
                if item not in (None, "", False, [], {}):
                    raise ValueError(
                        "refusing to write secret-like field to config: %s"
                        % qualified
                    )
            if lowered in PRIVATE_METADATA_KEYS and item not in (
                None,
                "",
                False,
                [],
                {},
            ):
                raise ValueError(
                    "refusing to write private identity/path field to config: %s"
                    % qualified
                )
            _reject_secrets(item, qualified)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, "%s[%d]" % (path, index))
    elif isinstance(value, str) and value.startswith(("/", "~/")):
        raise ValueError(
            "refusing to write an absolute user path to config: %s" % path
        )


def _clean_config(data: Dict[str, Any]) -> Dict[str, Any]:
    clean = json.loads(json.dumps(data, ensure_ascii=False))
    clean["version"] = CONFIG_VERSION
    transport = str(clean.get("transport") or "ssh")
    if transport not in ("ssh", "openapi"):
        raise ValueError("transport must be ssh or openapi")
    clean["transport"] = transport
    clean["default_ocr_backend"] = validate_profile_name(
        str(clean.get("default_ocr_backend") or "mineru3"),
        "OCR backend",
    )
    roots = clean.get("remote_roots")
    if not isinstance(roots, dict):
        roots = {}
    clean["remote_roots"] = {
        validate_profile_name(str(name), "OCR backend"): normalize_remote_root(
            str(value)
        )
        for name, value in roots.items()
    }
    _reject_secrets(clean)
    return clean


def save_user_config(data: Dict[str, Any]) -> Path:
    clean = _clean_config(data)
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(
        prefix=".config.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(clean, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path


def reset_user_config() -> bool:
    path = config_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return True


def safe_config_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    openapi = data.get("openapi")
    openapi = openapi if isinstance(openapi, dict) else {}
    regions = openapi.get("regions")
    regions = regions if isinstance(regions, dict) else {}
    enabled = openapi.get("enabled_region_ids")
    enabled = enabled if isinstance(enabled, list) else []
    if not enabled and openapi.get("default_region_id"):
        enabled = [str(openapi["default_region_id"])]
    ssh = data.get("ssh")
    ssh = ssh if isinstance(ssh, dict) else {}
    return {
        "transport": data.get("transport") or "ssh",
        "default_ocr_backend": data.get("default_ocr_backend") or "mineru3",
        "ssh_alias_configured": bool(ssh.get("alias")),
        "openapi_region_configured": bool(openapi.get("default_region_id")),
        "openapi_enabled_regions": [
            {
                "region_id": str(region_id),
                "name": (
                    regions.get(str(region_id), {}).get("name")
                    if isinstance(regions.get(str(region_id)), dict)
                    else None
                ),
            }
            for region_id in enabled
        ],
        "openapi_region_name": openapi.get("region_name"),
        "openapi_default_region_id": openapi.get("default_region_id"),
        "credential_provider": openapi.get("credential_provider"),
    }
