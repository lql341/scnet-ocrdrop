#!/usr/bin/env python3
"""Local upload/status client for the remote Slurm OCR drop queue."""

from __future__ import print_function

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ocrdrop.config import DEFAULT_REMOTE_ROOTS, load_user_config
from ocrdrop.openapi import OpenAPIError
from ocrdrop.setup_cli import run_setup
from ocrdrop.transports import OpenAPITransport, SSHTransport, run_command


SECRET_MARKERS = (
    "access_key",
    "secret_key",
    "password",
    "private_key",
    "token",
)
PATH_KEYS = {
    "batch_dir",
    "config_path",
    "home_path",
    "key_path",
    "output",
    "output_dir",
    "path",
    "realpath",
    "source_path",
    "traceback_path",
    "work_dir",
}
PERSONAL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._-])(?:/Users/|/home/|/public/home/)[^\s\"']+"
)


def run(cmd, capture=False):
    """Compatibility wrapper retained for callers and tests."""

    return run_command(cmd, capture=capture)


def validate_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value or ""):
        raise SystemExit("invalid batch id: %r" % value)


def sanitize_payload(value: Any, show_paths: bool = False, key: str = "") -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in SECRET_MARKERS):
        return "<redacted>"
    if not show_paths and lowered in PATH_KEYS:
        return "<redacted-path>"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_payload(
                item,
                show_paths=show_paths,
                key=str(item_key),
            )
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [
            sanitize_payload(item, show_paths=show_paths, key=key)
            for item in value
        ]
    if isinstance(value, str) and not show_paths:
        return PERSONAL_PATH_RE.sub("<redacted-path>", value)
    return value


def print_json(value: Any, args: Any) -> None:
    print(
        json.dumps(
            sanitize_payload(
                value,
                show_paths=bool(getattr(args, "show_paths", False)),
            ),
            ensure_ascii=True,
            indent=2,
        )
    )


def print_remote_output(output: str, args: Any) -> None:
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        text = str(output)
        if not getattr(args, "show_paths", False):
            text = PERSONAL_PATH_RE.sub("<redacted-path>", text)
        print(text)
        return
    print_json(value, args)


def get_transport(args):
    current = getattr(args, "_transport_instance", None)
    if current is not None:
        return current
    current = (
        OpenAPITransport(args)
        if args.transport == "openapi"
        else SSHTransport(args)
    )
    args._transport_instance = current
    return current


def remote_cli(args, extra, capture=False):
    return get_transport(args).remote_cli(extra, capture=capture)


def remote_status(args, batch_id):
    validate_id(batch_id)
    return get_transport(args).status(batch_id)


def wait_for_batch(args, batch_id):
    started = time.monotonic()
    last_counts = None
    while True:
        output, manifest = remote_status(args, batch_id)
        counts = manifest.get("counts", {})
        marker = (
            counts.get("pending", 0),
            counts.get("running", 0),
            counts.get("done", 0),
            counts.get("failed", 0),
            manifest.get("status"),
        )
        if marker != last_counts:
            print(
                "batch=%s status=%s pending=%s running=%s done=%s failed=%s"
                % (
                    batch_id,
                    manifest.get("status", "unknown"),
                    counts.get("pending", 0),
                    counts.get("running", 0),
                    counts.get("done", 0),
                    counts.get("failed", 0),
                ),
                file=sys.stderr,
            )
            last_counts = marker
        if manifest.get("status") in ("complete", "partial"):
            return output, manifest
        if args.timeout and time.monotonic() - started >= args.timeout:
            raise SystemExit("wait timed out for batch %s" % batch_id)
        time.sleep(max(1, args.poll_interval))


def _input_sources(args):
    sources = [Path(path).expanduser().resolve() for path in args.files]
    missing = [str(path) for path in sources if not path.exists()]
    if missing:
        raise SystemExit("missing files: " + ", ".join(missing))
    unsupported = [
        str(path)
        for path in sources
        if path.is_file() and path.suffix.lower() != ".pdf"
    ]
    if unsupported:
        raise SystemExit("only PDF files are accepted: " + ", ".join(unsupported))
    pdf_count = sum(
        1
        for source in sources
        for path in (
            [source]
            if source.is_file()
            else [
                item
                for item in source.rglob("*")
                if item.suffix.lower() == ".pdf"
            ]
        )
        if path.is_file()
    )
    if not pdf_count:
        raise SystemExit("no PDFs found in the supplied paths")
    return sources


def push(args):
    if args.plan_only and (args.wait or args.fetch):
        raise SystemExit("--plan-only cannot be combined with --wait or --fetch")
    sources = _input_sources(args)
    upload_id = (
        dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid.uuid4().hex[:6]
    )
    remote_dir = get_transport(args).upload_sources(sources, upload_id)
    extra = ["submit", remote_dir]
    if args.workers is not None:
        extra += ["--workers", str(args.workers)]
    if args.chunk_pages is not None:
        extra += ["--chunk-pages", str(args.chunk_pages)]
    if args.whole_document_pages is not None:
        extra += ["--whole-document-pages", str(args.whole_document_pages)]
    if args.plan_only:
        extra.append("--plan-only")
    output = remote_cli(args, extra, capture=True)
    payload = json.loads(output)
    print_json(payload, args)
    batch_id = payload["batch_id"]
    if args.wait or args.fetch:
        _, manifest = wait_for_batch(args, batch_id)
        print_json(manifest, args)
        if manifest.get("status") != "complete":
            raise SystemExit(
                "batch ended with status %s" % manifest.get("status")
            )
    if args.fetch:
        args.batch = batch_id
        fetch(args)


def status(args):
    if args.batch:
        validate_id(args.batch)
    _, payload = get_transport(args).status(args.batch)
    print_json(payload, args)


def wait(args):
    _, manifest = wait_for_batch(args, args.batch)
    print_json(manifest, args)
    return 0 if manifest.get("status") == "complete" else 2


def retry(args):
    validate_id(args.batch)
    extra = ["retry", "--batch", args.batch]
    if args.workers is not None:
        extra += ["--workers", str(args.workers)]
    if args.include_running:
        extra.append("--include-running")
    print_remote_output(remote_cli(args, extra, capture=True), args)


def fetch(args):
    validate_id(args.batch)
    destination = Path(args.output).expanduser().resolve()
    get_transport(args).fetch(args.batch, destination)
    print(str(Path(args.output) / "output"))


def doctor(args):
    print_remote_output(remote_cli(args, ["doctor"], capture=True), args)


def inventory(args):
    print_remote_output(remote_cli(args, ["inventory"], capture=True), args)


def deploy(args):
    source_dir = Path(__file__).resolve().parent
    project_root = source_dir.parents[1]
    default_launcher = (
        project_root / "examples" / "runtime" / "paddleocr-dcu-python.example"
        if args.ocr_backend == "paddleocr"
        else project_root / "examples" / "runtime" / "dcu-python.example"
    )
    launcher = (
        Path(args.launcher).expanduser().resolve()
        if args.launcher
        else default_launcher
    )
    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        raise SystemExit("missing config: %s" % config)
    if not launcher.is_file():
        raise SystemExit("missing runtime launcher: %s" % launcher)
    get_transport(args).deploy(source_dir, config, launcher)
    print(
        "deployed transport=%s backend=%s root=%s"
        % (args.transport, args.ocr_backend, args.remote_root)
    )


def parser():
    command_parser = argparse.ArgumentParser(prog="scnet-ocrdrop")
    command_parser.add_argument(
        "--transport",
        choices=("ssh", "openapi"),
        default=None,
    )
    command_parser.add_argument(
        "--ocr-backend",
        choices=("mineru3", "mineru4", "paddleocr"),
        default=None,
    )
    command_parser.add_argument("--ssh", default=None)
    command_parser.add_argument("--remote-root", default=None)
    command_parser.add_argument("--region", default=None)
    command_parser.add_argument("--scheduler-id", default=None)
    command_parser.add_argument("--api-timeout", type=int, default=60)
    command_parser.add_argument("--control-timeout", type=int, default=600)
    command_parser.add_argument(
        "--show-paths",
        action="store_true",
        help="show private absolute paths in command output",
    )
    sub = command_parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser(
        "setup",
        help="configure SSH or OpenAPI without writing credentials to JSON",
    )
    setup.add_argument(
        "setup_action",
        nargs="?",
        choices=("new", "modify", "status", "reset"),
        default="new",
    )
    setup.add_argument(
        "--credentials",
        action="store_true",
        help="with reset, also delete credentials from the system store",
    )
    setup.add_argument(
        "--enable-region",
        dest="setup_enabled_regions",
        action="append",
        default=[],
        help="enable a region by ID or name; repeat or use comma-separated values",
    )
    setup.add_argument(
        "--default-region",
        dest="setup_default_region",
        help="default region ID or name selected from enabled regions",
    )
    setup.add_argument(
        "--region-scheduler",
        dest="setup_region_schedulers",
        action="append",
        default=[],
        metavar="REGION=SCHEDULER",
        help="scheduler selection for an enabled region; repeat as needed",
    )

    sub.add_parser("doctor")
    sub.add_parser("inventory")

    deploy_parser = sub.add_parser("deploy")
    deploy_parser.add_argument("--config", required=True)
    deploy_parser.add_argument(
        "--launcher",
        help="Local worker launcher copied to remote app/dcu-python",
    )

    push_parser = sub.add_parser("push")
    push_parser.add_argument("files", nargs="+")
    push_parser.add_argument("--workers", type=int)
    push_parser.add_argument("--chunk-pages", type=int)
    push_parser.add_argument("--whole-document-pages", type=int)
    push_parser.add_argument("--plan-only", action="store_true")
    push_parser.add_argument("--wait", action="store_true")
    push_parser.add_argument("--fetch", action="store_true")
    push_parser.add_argument("--output", default="./ocrdrop-results")
    push_parser.add_argument("--poll-interval", type=int, default=20)
    push_parser.add_argument("--timeout", type=int, default=0)

    status_parser = sub.add_parser("status")
    status_parser.add_argument("--batch")

    wait_parser = sub.add_parser("wait")
    wait_parser.add_argument("--batch", required=True)
    wait_parser.add_argument("--poll-interval", type=int, default=20)
    wait_parser.add_argument("--timeout", type=int, default=0)

    retry_parser = sub.add_parser("retry")
    retry_parser.add_argument("--batch", required=True)
    retry_parser.add_argument("--workers", type=int)
    retry_parser.add_argument("--include-running", action="store_true")

    fetch_parser = sub.add_parser("fetch")
    fetch_parser.add_argument("--batch", required=True)
    fetch_parser.add_argument("--output", default="./ocrdrop-results")
    return command_parser


def _configured_value(
    cli_value: Optional[str],
    environment_name: str,
    saved_value: Optional[str],
) -> Optional[str]:
    return cli_value or os.environ.get(environment_name) or saved_value


def apply_user_config(args):
    config = load_user_config()
    args.transport = _configured_value(
        args.transport,
        "OCRDROP_TRANSPORT",
        str(config.get("transport") or "ssh"),
    )
    args.ocr_backend = _configured_value(
        args.ocr_backend,
        "OCRDROP_BACKEND",
        str(config.get("default_ocr_backend") or "mineru3"),
    )
    roots = config.get("remote_roots")
    roots = roots if isinstance(roots, dict) else DEFAULT_REMOTE_ROOTS
    args.remote_root = _configured_value(
        args.remote_root,
        "OCRDROP_REMOTE_ROOT",
        os.environ.get("MINERU_DROP_REMOTE_ROOT")
        or str(
            roots.get(args.ocr_backend)
            or DEFAULT_REMOTE_ROOTS.get(args.ocr_backend, "scnet-ocrdrop")
        ),
    )
    ssh = config.get("ssh")
    ssh = ssh if isinstance(ssh, dict) else {}
    args.ssh = _configured_value(
        args.ssh,
        "OCRDROP_SSH",
        os.environ.get("MINERU_DROP_SSH") or ssh.get("alias"),
    )
    openapi = config.get("openapi")
    openapi = openapi if isinstance(openapi, dict) else {}
    explicit_region = args.region or os.environ.get(
        "SCNET_OPENAPI_REGION_ID"
    )
    selected_region = explicit_region or openapi.get("default_region_id")
    regions = openapi.get("regions")
    regions = regions if isinstance(regions, dict) else {}
    enabled = openapi.get("enabled_region_ids")
    enabled_ids = (
        {str(region_id) for region_id in enabled}
        if isinstance(enabled, list)
        else set()
    )
    if selected_region:
        requested = str(selected_region)
        resolved_region = None
        if requested in regions or requested in enabled_ids:
            resolved_region = requested
        else:
            for region_id, metadata in regions.items():
                if (
                    isinstance(metadata, dict)
                    and str(metadata.get("name")) == requested
                ):
                    resolved_region = str(region_id)
                    break
        if enabled_ids:
            if resolved_region is None or resolved_region not in enabled_ids:
                raise SystemExit(
                    "OpenAPI region %r is not enabled; run "
                    "`scnet-ocrdrop setup modify`" % requested
                )
        args.region = resolved_region or requested
    else:
        args.region = None
    region_config = (
        regions.get(str(args.region), {})
        if args.region is not None
        else {}
    )
    region_config = (
        region_config if isinstance(region_config, dict) else {}
    )
    saved_scheduler = region_config.get("scheduler_id")
    if (
        not saved_scheduler
        and str(args.region or "")
        == str(openapi.get("default_region_id") or "")
    ):
        saved_scheduler = openapi.get("scheduler_id")
    args.scheduler_id = _configured_value(
        args.scheduler_id,
        "SCNET_OPENAPI_SCHEDULER_ID",
        saved_scheduler,
    )
    return args


def main():
    command_parser = parser()
    args = command_parser.parse_args()
    if args.command == "setup":
        return run_setup(args)
    apply_user_config(args)
    try:
        if args.command == "deploy":
            deploy(args)
        elif args.command == "doctor":
            doctor(args)
        elif args.command == "inventory":
            inventory(args)
        elif args.command == "push":
            push(args)
        elif args.command == "status":
            status(args)
        elif args.command == "wait":
            return wait(args)
        elif args.command == "retry":
            retry(args)
        elif args.command == "fetch":
            fetch(args)
    except OpenAPIError as exc:
        raise SystemExit(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
