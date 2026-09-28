"""Interactive configuration lifecycle for scnet-ocrdrop."""

from __future__ import annotations

import getpass
import json
import os
import sys
from typing import Any, Dict, List, Mapping, Optional

from .config import (
    DEFAULT_REMOTE_ROOTS,
    config_path,
    load_user_config,
    normalize_remote_root,
    reset_user_config,
    safe_config_summary,
    save_user_config,
)
from .credentials import (
    CredentialError,
    delete_openapi_credentials,
    load_openapi_credentials,
    secure_store_name,
    store_openapi_credentials,
)
from .openapi import OpenAPIError, SCNetOpenAPI


def _ask(prompt: str, default: str = "") -> str:
    suffix = " [%s]" % default if default else ""
    answer = input("%s%s: " % (prompt, suffix)).strip()
    return answer or default


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    answer = input(prompt + suffix + ": ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _choose(
    prompt: str,
    items: List[Mapping[str, Any]],
    *,
    label_key: str,
    requested: Optional[str] = None,
) -> Mapping[str, Any]:
    if requested:
        for item in items:
            if str(item.get("region_id") or item.get("id")) == requested or str(
                item.get(label_key)
            ) == requested:
                return item
        raise OpenAPIError("%s %r is not available" % (prompt, requested))
    if len(items) == 1:
        return items[0]
    if not sys.stdin.isatty():
        raise OpenAPIError(
            "%s requires an explicit option in a non-interactive terminal"
            % prompt
        )
    print(prompt + ":")
    for index, item in enumerate(items, 1):
        identifier = item.get("region_id") or item.get("id")
        print(
            "  %d. %s (%s)"
            % (index, item.get(label_key) or identifier, identifier)
        )
    while True:
        value = _ask("选择", "1")
        try:
            index = int(value)
        except ValueError:
            continue
        if 1 <= index <= len(items):
            return items[index - 1]


def _credential_input() -> Dict[str, str]:
    if not sys.stdin.isatty():
        raise OpenAPIError(
            "credentials are unavailable; use environment variables or "
            "run setup in an interactive terminal"
        )
    user = _ask("SCNet 平台用户名")
    access_key = getpass.getpass("AccessKey（输入不回显）: ").strip()
    secret_key = getpass.getpass("SecretKey（输入不回显）: ").strip()
    if not user or not access_key or not secret_key:
        raise OpenAPIError("用户名、AccessKey 和 SecretKey 都不能为空")
    return {
        "user": user,
        "access_key": access_key,
        "secret_key": secret_key,
    }


def _display_config_path() -> str:
    if os.environ.get("OCRDROP_CONFIG_HOME"):
        return "$OCRDROP_CONFIG_HOME/config.json"
    if os.environ.get("XDG_CONFIG_HOME"):
        return "$XDG_CONFIG_HOME/scnet-ocrdrop/config.json"
    return "~/.config/scnet-ocrdrop/config.json"


def setup_status() -> int:
    config = load_user_config()
    credentials, provider = load_openapi_credentials()
    summary = safe_config_summary(config)
    summary["openapi_credentials"] = (
        "configured" if credentials else "not configured"
    )
    if provider:
        summary["credential_provider"] = provider
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def setup_reset(remove_credentials: bool) -> int:
    removed_config = reset_user_config()
    result = {"config_removed": removed_config}
    if remove_credentials:
        try:
            removed, provider = delete_openapi_credentials()
        except CredentialError as exc:
            raise SystemExit(str(exc))
        result["credentials_removed"] = removed
        result["credential_provider"] = provider
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def configure(args: Any) -> int:
    current = load_user_config()
    transport = getattr(args, "transport", None)
    if not transport:
        default_transport = str(current.get("transport") or "ssh")
        if (
            getattr(args, "setup_action", None) == "new"
            and not config_path().exists()
        ):
            default_transport = "openapi"
        if sys.stdin.isatty():
            transport = _ask(
                "Transport（openapi/ssh）",
                default_transport,
            )
        else:
            transport = default_transport
    if transport not in ("ssh", "openapi"):
        raise SystemExit("transport must be openapi or ssh")
    backend = getattr(args, "ocr_backend", None)
    if not backend:
        default_backend = str(
            current.get("default_ocr_backend") or "mineru3"
        )
        backend = (
            _ask(
                "OCR backend（mineru3/mineru4/paddleocr）",
                default_backend,
            )
            if sys.stdin.isatty()
            else default_backend
        )
    if backend not in DEFAULT_REMOTE_ROOTS:
        raise SystemExit(
            "unsupported OCR backend: %s" % backend
        )
    result = dict(current)
    result["transport"] = transport
    result["default_ocr_backend"] = backend
    result.setdefault("remote_roots", dict(DEFAULT_REMOTE_ROOTS))
    if getattr(args, "remote_root", None):
        result["remote_roots"][backend] = normalize_remote_root(
            str(args.remote_root)
        )

    if transport == "ssh":
        ssh = result.get("ssh")
        ssh = dict(ssh) if isinstance(ssh, dict) else {}
        alias = getattr(args, "ssh", None) or ssh.get("alias")
        if not alias and sys.stdin.isatty():
            alias = _ask("SSH Host 别名")
        if not alias:
            raise SystemExit("SSH transport requires a Host alias")
        ssh["alias"] = str(alias)
        result["ssh"] = ssh
        save_user_config(result)
        print("已保存非敏感配置：%s" % _display_config_path())
        print("下一步：scnet-ocrdrop doctor")
        return 0

    credentials, provider = load_openapi_credentials()
    entered = False
    if credentials is None:
        credentials = _credential_input()
        entered = True
    api = SCNetOpenAPI(
        timeout=int(getattr(args, "api_timeout", 60) or 60),
        credentials=credentials,
    )
    contexts = api.discover_contexts()
    if not contexts:
        raise OpenAPIError(
            "账号没有暴露 HPC 作业和文件服务的授权区域"
        )
    region = _choose(
        "选择默认 HPC 区域",
        contexts,
        label_key="region_name",
        requested=getattr(args, "region", None),
    )
    schedulers = list(region.get("schedulers") or [])
    scheduler = _choose(
        "选择默认 Scheduler",
        schedulers,
        label_key="name",
        requested=getattr(args, "scheduler_id", None),
    )
    stored_provider = provider
    available_store = secure_store_name()
    if entered and available_store:
        save_secret = True
        if sys.stdin.isatty():
            save_secret = _ask_yes_no(
                "是否将 AK/SK 保存到 %s？" % available_store,
                default=True,
            )
        if save_secret:
            try:
                stored_provider = store_openapi_credentials(
                    credentials["user"],
                    credentials["access_key"],
                    credentials["secret_key"],
                )
            except CredentialError as exc:
                raise SystemExit(str(exc))
    elif entered and not available_store:
        print(
            "系统没有可用安全凭据库；本次凭据未落盘。"
            "后续请通过 SCNET_OPENAPI_* 环境变量注入。",
            file=sys.stderr,
        )
    result["openapi"] = {
        "default_region_id": region["region_id"],
        "region_name": region.get("region_name"),
        "scheduler_id": scheduler["id"],
        "credential_provider": stored_provider,
    }
    save_user_config(result)
    print("OpenAPI 凭据验证成功。")
    print(
        "默认区域：%s；Scheduler：%s"
        % (
            region.get("region_name") or region["region_id"],
            scheduler.get("name") or scheduler["id"],
        )
    )
    print("已保存非敏感配置：%s" % _display_config_path())
    print("下一步：scnet-ocrdrop doctor")
    return 0


def run_setup(args: Any) -> int:
    action = getattr(args, "setup_action", None) or "new"
    if action == "status":
        return setup_status()
    if action == "reset":
        return setup_reset(bool(getattr(args, "credentials", False)))
    if action not in ("new", "modify"):
        raise SystemExit("unsupported setup action: %s" % action)
    try:
        return configure(args)
    except OpenAPIError as exc:
        raise SystemExit(str(exc))
