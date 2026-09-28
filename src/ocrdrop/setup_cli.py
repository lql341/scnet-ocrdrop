"""Interactive configuration lifecycle for scnet-ocrdrop."""

from __future__ import annotations

import getpass
import json
import os
import select
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
    default_id: Optional[str] = None,
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
    default_index = 1
    for index, item in enumerate(items, 1):
        identifier = item.get("region_id") or item.get("id")
        if default_id and str(identifier) == str(default_id):
            default_index = index
        marker = "*" if index == default_index else " "
        print(
            "  %s %d. %s (%s)"
            % (marker, index, item.get(label_key) or identifier, identifier)
        )
    while True:
        value = _ask("选择", str(default_index))
        try:
            index = int(value)
        except ValueError:
            continue
        if 1 <= index <= len(items):
            return items[index - 1]


def _parse_multi_numbers(value: str, count: int) -> set:
    selected = set()
    for part in value.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                start, end = end, start
            selected.update(range(start - 1, end))
        else:
            selected.add(int(part) - 1)
    if not selected or any(index < 0 or index >= count for index in selected):
        raise ValueError("selection out of range")
    return selected


def _multi_choose_interactive(
    prompt: str,
    choices: List[str],
    selected: set,
) -> set:
    import termios
    import tty

    cursor = min(selected) if selected else 0
    digits = ""
    line_count = len(choices) + 1
    print(prompt)
    print(
        "使用 ↑/↓ 移动，Space 勾选，a 全选，n 清空，"
        "Enter 保存，q 取消。"
    )

    def render(first: bool = False) -> None:
        if not first:
            sys.stdout.write("\033[%dA" % line_count)
        for index, choice in enumerate(choices):
            pointer = "▶" if index == cursor else " "
            checked = "x" if index in selected else " "
            sys.stdout.write(
                "\r\033[2K  %s [%s] %d. %s\n"
                % (pointer, checked, index + 1, choice)
            )
        hint = (
            "编号定位：%s" % digits
            if digits
            else "已选择 %d/%d" % (len(selected), len(choices))
        )
        sys.stdout.write("\r\033[2K  %s\n" % hint)
        sys.stdout.flush()

    input_fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(input_fd)
    render(first=True)
    try:
        tty.setcbreak(input_fd)
        while True:
            char = os.read(input_fd, 1).decode("utf-8", "ignore")
            if char in ("\r", "\n"):
                if digits:
                    value = int(digits)
                    if 1 <= value <= len(choices):
                        cursor = value - 1
                    digits = ""
                    render()
                    continue
                if selected:
                    break
                render()
                continue
            if char == "\x1b":
                ready, _, _ = select.select([input_fd], [], [], 0.1)
                sequence = (
                    os.read(input_fd, 2).decode("utf-8", "ignore")
                    if ready
                    else ""
                )
                if sequence == "[A":
                    cursor = (cursor - 1) % len(choices)
                    digits = ""
                    render()
                elif sequence == "[B":
                    cursor = (cursor + 1) % len(choices)
                    digits = ""
                    render()
                continue
            if char == " ":
                if cursor in selected:
                    selected.remove(cursor)
                else:
                    selected.add(cursor)
                digits = ""
                render()
                continue
            if char in ("a", "A"):
                selected = set(range(len(choices)))
                digits = ""
                render()
                continue
            if char in ("n", "N"):
                selected.clear()
                digits = ""
                render()
                continue
            if char in ("q", "Q", "\x03"):
                raise OpenAPIError("用户取消配置")
            if char in ("\x7f", "\b"):
                digits = digits[:-1]
                render()
                continue
            if char.isdigit():
                digits += char
                value = int(digits)
                if 1 <= value <= len(choices):
                    cursor = value - 1
                render()
    finally:
        termios.tcsetattr(input_fd, termios.TCSADRAIN, old_settings)
    print("已启用 %d 个区域。" % len(selected))
    return selected


def _multi_choose(
    prompt: str,
    choices: List[str],
    defaults: set,
) -> set:
    selected = set(defaults)
    if (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.name == "posix"
        and os.environ.get("TERM", "") != "dumb"
    ):
        return _multi_choose_interactive(prompt, choices, selected)
    print(prompt)
    for index, choice in enumerate(choices, 1):
        marker = "x" if index - 1 in selected else " "
        print("  [%s] %d. %s" % (marker, index, choice))
    default_text = ",".join(str(index + 1) for index in sorted(selected))
    print("输入编号列表，如 1,3,5-8；直接按 Enter 保留当前勾选。")
    try:
        return _parse_multi_numbers(
            _ask("选择", default_text),
            len(choices),
        )
    except (TypeError, ValueError) as exc:
        raise OpenAPIError(
            "多选格式无效，请输入如 1,3,5-8"
        ) from exc


def _match_context(
    contexts: List[Mapping[str, Any]],
    requested: str,
) -> Mapping[str, Any]:
    for item in contexts:
        if str(item.get("region_id")) == requested or str(
            item.get("region_name")
        ) == requested:
            return item
    raise OpenAPIError("OpenAPI region %r is not available" % requested)


def _requested_region_values(args: Any) -> List[str]:
    values = []
    for raw in getattr(args, "setup_enabled_regions", None) or []:
        values.extend(
            item.strip() for item in str(raw).split(",") if item.strip()
        )
    return values


def _requested_scheduler_map(args: Any) -> Dict[str, str]:
    result = {}
    for raw in getattr(args, "setup_region_schedulers", None) or []:
        if "=" not in str(raw):
            raise OpenAPIError(
                "--region-scheduler must use REGION=SCHEDULER"
            )
        region, scheduler = str(raw).split("=", 1)
        region = region.strip()
        scheduler = scheduler.strip()
        if not region or not scheduler:
            raise OpenAPIError(
                "--region-scheduler must use REGION=SCHEDULER"
            )
        result[region] = scheduler
    return result


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
    existing_openapi = current.get("openapi")
    existing_openapi = (
        existing_openapi if isinstance(existing_openapi, dict) else {}
    )
    existing_enabled = existing_openapi.get("enabled_region_ids")
    if isinstance(existing_enabled, list):
        existing_enabled_ids = {
            str(region_id) for region_id in existing_enabled
        }
    elif existing_openapi.get("default_region_id"):
        existing_enabled_ids = {
            str(existing_openapi["default_region_id"])
        }
    else:
        existing_enabled_ids = set()

    requested_enabled = _requested_region_values(args)
    requested_default = (
        getattr(args, "setup_default_region", None)
        or getattr(args, "region", None)
    )
    if not requested_enabled and getattr(args, "region", None):
        requested_enabled = [str(args.region)]
    if requested_enabled:
        enabled_contexts = []
        enabled_ids = set()
        for requested in requested_enabled:
            context = _match_context(contexts, requested)
            region_id = str(context["region_id"])
            if region_id not in enabled_ids:
                enabled_contexts.append(context)
                enabled_ids.add(region_id)
    elif sys.stdin.isatty():
        default_ids = (
            existing_enabled_ids
            if existing_enabled_ids
            else {str(item["region_id"]) for item in contexts}
        )
        region_labels = [
            "%s (%s)"
            % (
                item.get("region_name") or item["region_id"],
                item["region_id"],
            )
            for item in contexts
        ]
        default_indices = {
            index
            for index, item in enumerate(contexts)
            if str(item["region_id"]) in default_ids
        }
        selected_indices = _multi_choose(
            "选择在本机启用的 HPC 区域（可多选）：",
            region_labels,
            default_indices,
        )
        enabled_contexts = [
            item
            for index, item in enumerate(contexts)
            if index in selected_indices
        ]
    else:
        enabled_contexts = [
            item
            for item in contexts
            if str(item["region_id"]) in existing_enabled_ids
        ]
        if not enabled_contexts:
            if len(contexts) == 1:
                enabled_contexts = [contexts[0]]
            else:
                raise OpenAPIError(
                    "multiple regions are available; use --enable-region "
                    "for non-interactive setup"
                )

    enabled_region_ids = {
        str(item["region_id"]) for item in enabled_contexts
    }
    if requested_default:
        default_region = _match_context(
            enabled_contexts,
            str(requested_default),
        )
    elif (
        existing_openapi.get("default_region_id")
        and str(existing_openapi["default_region_id"])
        in enabled_region_ids
    ):
        existing_default_id = str(existing_openapi["default_region_id"])
        if sys.stdin.isatty() and len(enabled_contexts) > 1:
            default_region = _choose(
                "从已启用区域中选择默认 HPC 作业区域",
                enabled_contexts,
                label_key="region_name",
                default_id=existing_default_id,
            )
        else:
            default_region = _match_context(
                enabled_contexts,
                existing_default_id,
            )
    elif len(enabled_contexts) == 1:
        default_region = enabled_contexts[0]
    elif sys.stdin.isatty():
        default_region = _choose(
            "从已启用区域中选择默认 HPC 作业区域",
            enabled_contexts,
            label_key="region_name",
        )
    else:
        raise OpenAPIError(
            "multiple regions are enabled; use --default-region"
        )

    existing_regions = existing_openapi.get("regions")
    existing_regions = (
        existing_regions if isinstance(existing_regions, dict) else {}
    )
    requested_scheduler_map = _requested_scheduler_map(args)
    requested_scheduler_by_region = {}
    for requested_region, scheduler_id in requested_scheduler_map.items():
        context = _match_context(enabled_contexts, requested_region)
        requested_scheduler_by_region[str(context["region_id"])] = scheduler_id

    region_records = {}
    for context in enabled_contexts:
        region_id = str(context["region_id"])
        schedulers = list(context.get("schedulers") or [])
        if not schedulers:
            raise OpenAPIError(
                "region %s has no available scheduler" % region_id
            )
        saved_region = existing_regions.get(region_id)
        saved_region = (
            saved_region if isinstance(saved_region, dict) else {}
        )
        requested_scheduler = requested_scheduler_by_region.get(region_id)
        if (
            not requested_scheduler
            and region_id == str(default_region["region_id"])
        ):
            requested_scheduler = getattr(args, "scheduler_id", None)
        saved_scheduler = saved_region.get("scheduler_id")
        if (
            not saved_scheduler
            and region_id == str(existing_openapi.get("default_region_id"))
        ):
            saved_scheduler = existing_openapi.get("scheduler_id")
        if requested_scheduler:
            scheduler = _choose(
                "选择 %s 的 Scheduler"
                % (context.get("region_name") or region_id),
                schedulers,
                label_key="name",
                requested=str(requested_scheduler),
            )
        elif len(schedulers) == 1:
            scheduler = schedulers[0]
        elif sys.stdin.isatty():
            scheduler = _choose(
                "选择 %s 的 Scheduler"
                % (context.get("region_name") or region_id),
                schedulers,
                label_key="name",
                default_id=(
                    str(saved_scheduler) if saved_scheduler else None
                ),
            )
        elif saved_scheduler:
            scheduler = _choose(
                "选择 Scheduler",
                schedulers,
                label_key="name",
                requested=str(saved_scheduler),
            )
        else:
            raise OpenAPIError(
                "region %s has multiple schedulers; use "
                "--region-scheduler REGION=SCHEDULER" % region_id
            )
        region_records[region_id] = {
            "name": context.get("region_name"),
            "scheduler_id": str(scheduler["id"]),
            "schedulers": [
                {
                    "id": str(item.get("id", "")),
                    "name": item.get("name"),
                    "type": item.get("type"),
                }
                for item in schedulers
            ],
        }

    default_region_id = str(default_region["region_id"])
    default_scheduler_id = str(
        region_records[default_region_id]["scheduler_id"]
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
        "enabled_region_ids": [
            str(item["region_id"]) for item in enabled_contexts
        ],
        "default_region_id": default_region_id,
        "region_name": default_region.get("region_name"),
        "scheduler_id": default_scheduler_id,
        "regions": region_records,
        "credential_provider": stored_provider,
    }
    save_user_config(result)
    print("OpenAPI 凭据验证成功。")
    print(
        "已启用区域：%d；默认区域：%s；Scheduler：%s"
        % (
            len(enabled_contexts),
            default_region.get("region_name") or default_region_id,
            default_scheduler_id,
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
