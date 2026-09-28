"""Secure SCNet OpenAPI credential discovery and storage.

The credential-store layout is compatible with the scnet-hpc skill so the two
tools can share one SCNet platform credential set.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from typing import Any, Dict, Optional, Tuple


SERVICE = "scnet-hpc-openapi"


class CredentialError(RuntimeError):
    """A bounded credential-store failure."""


def _valid(data: Any) -> bool:
    return isinstance(data, dict) and all(
        isinstance(data.get(key), str) and data[key]
        for key in ("user", "access_key", "secret_key")
    )


def environment_credentials() -> Optional[Dict[str, str]]:
    data = {
        "user": os.environ.get("SCNET_OPENAPI_USER", ""),
        "access_key": os.environ.get("SCNET_OPENAPI_ACCESS_KEY", ""),
        "secret_key": os.environ.get("SCNET_OPENAPI_SECRET_KEY", ""),
    }
    return data if _valid(data) else None


def secure_store_name() -> Optional[str]:
    if platform.system() == "Darwin" and shutil.which("security"):
        return "macOS Keychain"
    if shutil.which("secret-tool"):
        return "Secret Service"
    return None


def load_secure_credentials() -> Optional[Dict[str, str]]:
    provider = secure_store_name()
    if provider == "macOS Keychain":
        command = ["security", "find-generic-password", "-s", SERVICE, "-w"]
    elif provider == "Secret Service":
        command = ["secret-tool", "lookup", "service", SERVICE]
    else:
        return None
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return data if _valid(data) else None


def load_openapi_credentials(
    include_environment: bool = True,
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    if include_environment:
        data = environment_credentials()
        if data:
            return data, "environment"
    data = load_secure_credentials()
    if data:
        return data, secure_store_name()
    return None, None


def store_openapi_credentials(
    user: str, access_key: str, secret_key: str
) -> str:
    data = {
        "user": user,
        "access_key": access_key,
        "secret_key": secret_key,
    }
    if not _valid(data):
        raise CredentialError("OpenAPI credentials are incomplete")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    provider = secure_store_name()
    if provider == "macOS Keychain":
        command = [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            SERVICE,
            "-a",
            user,
            "-w",
            payload,
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    elif provider == "Secret Service":
        command = [
            "secret-tool",
            "store",
            "--label=SCNet OpenAPI",
            "service",
            SERVICE,
            "user",
            user,
        ]
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    else:
        raise CredentialError(
            "no supported secure store; use SCNET_OPENAPI_USER, "
            "SCNET_OPENAPI_ACCESS_KEY, and SCNET_OPENAPI_SECRET_KEY"
        )
    if completed.returncode != 0:
        raise CredentialError(
            completed.stderr.strip() or "failed to write %s" % provider
        )
    return str(provider)


def delete_openapi_credentials() -> Tuple[bool, Optional[str]]:
    provider = secure_store_name()
    if provider == "macOS Keychain":
        command = ["security", "delete-generic-password", "-s", SERVICE]
    elif provider == "Secret Service":
        command = ["secret-tool", "clear", "service", SERVICE]
    else:
        return False, None
    completed = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return True, provider
    if load_secure_credentials() is None:
        return False, provider
    raise CredentialError(
        completed.stderr.strip() or "failed to clear %s" % provider
    )
