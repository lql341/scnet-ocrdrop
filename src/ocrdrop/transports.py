"""Local-to-cluster transports for scnet-ocrdrop."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from .openapi import (
    OpenAPIError,
    SCNetOpenAPI,
    is_directory_entry,
    join_remote_path,
    resolve_home_relative,
)


def run_command(command: Sequence[str], capture: bool = False) -> str:
    clean_env = os.environ.copy()
    clean_env["LANG"] = "C"
    clean_env["LC_ALL"] = "C"
    kwargs = {
        "check": False,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": clean_env,
        "stderr": subprocess.PIPE,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
    proc = subprocess.run(list(command), **kwargs)
    if proc.returncode:
        lines = [
            line.strip()
            for line in (proc.stderr or "").splitlines()
            if line.strip()
        ]
        detail = lines[-1] if lines else "no error details"
        raise SystemExit(
            "command failed (exit %d): %s" % (proc.returncode, detail)
        )
    return proc.stdout.strip() if capture else ""


def validate_remote_root(value: str, allow_absolute: bool = True) -> str:
    raw = str(value or "").strip()
    if not raw or any(char in raw for char in "\r\n\x00"):
        raise SystemExit("invalid remote root")
    if not allow_absolute and raw.startswith("/"):
        raise SystemExit("saved remote root must be relative to the remote home")
    candidate = raw[2:] if raw.startswith("~/") else raw.lstrip("/")
    if any(part in ("", ".", "..") for part in candidate.split("/")):
        raise SystemExit("remote root must be normalized")
    if any(
        not re.fullmatch(r"[A-Za-z0-9._-]+", part)
        for part in candidate.split("/")
    ):
        raise SystemExit("remote root contains unsafe characters")
    return raw.rstrip("/")


def _json_from_mixed_output(text: str) -> Optional[Any]:
    decoder = json.JSONDecoder()
    candidates = [
        index for index, char in enumerate(text) if char in ("{", "[")
    ]
    result = None
    result_size = -1
    for index in candidates:
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not text[index + end :].strip():
            return value
        if end > result_size:
            result = value
            result_size = end
    return result


def _pdf_uploads(sources: Iterable[Path]) -> Iterable[Tuple[Path, Path]]:
    """Yield local PDF and relative remote parent pairs."""

    for source in sources:
        if source.is_file():
            yield source, Path()
            continue
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix.lower() == ".pdf":
                yield path, Path(source.name) / path.relative_to(source).parent


class SSHTransport:
    name = "ssh"

    def __init__(self, args: Any):
        if not args.ssh:
            raise SystemExit("set --ssh, OCRDROP_SSH, or run setup")
        self.ssh = str(args.ssh)
        self.remote_root = validate_remote_root(str(args.remote_root))

    def _ssh(self, command: Sequence[str], capture: bool = False) -> str:
        remote_command = " ".join(shlex.quote(str(item)) for item in command)
        return run_command(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.ssh,
                remote_command,
            ],
            capture=capture,
        )

    def remote_cli(
        self, extra: Sequence[str], capture: bool = False
    ) -> str:
        command = [
            "env",
            "LC_ALL=en_US.utf8",
            "LANG=en_US.utf8",
            "python3",
            self.remote_root + "/app/mineru_drop.py",
            "--config",
            self.remote_root + "/config.json",
        ] + list(extra)
        return self._ssh(command, capture=capture)

    def status(
        self, batch_id: Optional[str]
    ) -> Tuple[str, Any]:
        extra = ["status"]
        if batch_id:
            extra += ["--batch", batch_id]
        output = self.remote_cli(extra, capture=True)
        return output, json.loads(output)

    def upload_sources(self, sources: Sequence[Path], upload_id: str) -> str:
        remote_dir = self.remote_root + "/inbox/" + upload_id
        self._ssh(["mkdir", "-p", remote_dir])
        run_command(
            ["scp", "-q", "-r"]
            + [str(path) for path in sources]
            + [self.ssh + ":" + remote_dir + "/"]
        )
        return remote_dir

    def fetch(self, batch_id: str, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        remote = (
            self.ssh
            + ":"
            + self.remote_root
            + "/batches/"
            + batch_id
            + "/output"
        )
        run_command(["scp", "-q", "-r", remote, str(destination)])
        return destination / "output"

    def deploy(
        self,
        source_dir: Path,
        config: Path,
        launcher: Path,
    ) -> None:
        self._ssh(
            [
                "mkdir",
                "-p",
                self.remote_root + "/app",
                self.remote_root + "/app/ocrdrop",
                self.remote_root + "/app/ocrdrop/backends",
                self.remote_root + "/inbox",
                self.remote_root + "/batches",
            ]
        )
        run_command(
            [
                "scp",
                "-q",
                str(source_dir / "remote.py"),
                self.ssh + ":" + self.remote_root + "/app/mineru_drop.py",
            ]
        )
        run_command(
            [
                "scp",
                "-q",
                str(source_dir / "__init__.py"),
                self.ssh + ":" + self.remote_root + "/app/ocrdrop/",
            ]
        )
        run_command(
            [
                "scp",
                "-q",
                str(source_dir / "backends" / "__init__.py"),
                str(source_dir / "backends" / "base.py"),
                str(source_dir / "backends" / "mineru.py"),
                str(source_dir / "backends" / "paddleocr.py"),
                self.ssh
                + ":"
                + self.remote_root
                + "/app/ocrdrop/backends/",
            ]
        )
        run_command(
            [
                "scp",
                "-q",
                str(launcher),
                self.ssh + ":" + self.remote_root + "/app/dcu-python",
            ]
        )
        run_command(
            [
                "scp",
                "-q",
                str(config),
                self.ssh + ":" + self.remote_root + "/config.json",
            ]
        )
        self._ssh(
            [
                "chmod",
                "600",
                self.remote_root + "/config.json",
            ]
        )
        self._ssh(
            [
                "chmod",
                "+x",
                self.remote_root + "/app/mineru_drop.py",
                self.remote_root + "/app/dcu-python",
            ]
        )


class OpenAPITransport:
    name = "openapi"

    def __init__(self, args: Any):
        self.api = SCNetOpenAPI(timeout=int(getattr(args, "api_timeout", 60)))
        self.context = self.api.resolve_context(
            getattr(args, "region", None),
            getattr(args, "scheduler_id", None),
        )
        self.saved_remote_root = validate_remote_root(
            str(args.remote_root), allow_absolute=True
        )
        self.remote_root = resolve_home_relative(
            str(self.context["home_path"]),
            self.saved_remote_root,
        )
        self.control_timeout = int(
            getattr(args, "control_timeout", 600) or 600
        )
        self._deployment_config_cache: Optional[Dict[str, Any]] = None

    def _remote(self, *parts: str) -> str:
        return join_remote_path(self.remote_root, *parts)

    def _deployment_config(self) -> Dict[str, Any]:
        if self._deployment_config_cache is None:
            self._deployment_config_cache = self.api.read_json(
                self.context, self._remote("config.json")
            )
        return self._deployment_config_cache

    def _read_with_retry(self, path: str) -> str:
        last_error = None
        for _ in range(5):
            try:
                return self.api.download_bytes(
                    self.context, path
                ).decode("utf-8", "replace")
            except OpenAPIError as exc:
                last_error = exc
                time.sleep(1)
        raise OpenAPIError("control log is unavailable: %s" % last_error)

    def _run_control(
        self,
        extra: Sequence[str],
        *,
        queue: Optional[str] = None,
        walltime: str = "00:10:00",
    ) -> str:
        cfg = self._deployment_config()
        selected_queue = queue or str(cfg.get("partition_cpu") or "")
        if not selected_queue:
            raise OpenAPIError(
                "remote config does not define partition_cpu"
            )
        control_logs = self._remote("control-logs")
        self.api.mkdir(self.context, control_logs)
        request_id = uuid.uuid4().hex[:12]
        stdout = join_remote_path(control_logs, request_id + ".out")
        stderr = join_remote_path(control_logs, request_id + ".err")
        python_command = " ".join(
            shlex.quote(str(item))
            for item in [
                "python3",
                self._remote("app", "mineru_drop.py"),
                "--config",
                self._remote("config.json"),
            ]
            + list(extra)
        )
        python_module = str(cfg.get("python_module") or "")
        command = (
            "module purge && module load %s && %s"
            % (shlex.quote(python_module), python_command)
            if python_module
            else python_command
        )
        job_id = self.api.submit_job(
            self.context,
            name="ocrdrop-control-" + request_id[:8],
            command=command,
            work_dir=self.remote_root,
            queue=selected_queue,
            cpus=1,
            memory="1gb",
            walltime=walltime,
            stdout=stdout,
            stderr=stderr,
        )
        job = self.api.wait_job(
            self.context,
            job_id,
            timeout=self.control_timeout,
        )
        output = self._read_with_retry(stdout).strip()
        exit_code = str(job.get("exit_code") or "")
        succeeded = (
            job.get("state") == "COMPLETED"
            and (not exit_code or exit_code.startswith("0"))
        )
        if not succeeded:
            try:
                error = self._read_with_retry(stderr).strip()
            except OpenAPIError:
                error = ""
            detail = [
                line.strip()
                for line in error.splitlines()
                if line.strip()
            ]
            raise OpenAPIError(
                "OpenAPI control job %s failed: %s"
                % (
                    job_id,
                    detail[-1]
                    if detail
                    else job.get("reason") or job.get("state"),
                )
            )
        parsed = _json_from_mixed_output(output)
        if parsed is not None:
            return json.dumps(parsed, ensure_ascii=True, indent=2)
        return output

    def remote_cli(
        self, extra: Sequence[str], capture: bool = False
    ) -> str:
        output = self._run_control(extra)
        if not capture and output:
            print(output)
            return ""
        return output

    def _counts(self, batch_id: str) -> Dict[str, int]:
        result = {}
        for state in ("pending", "running", "done", "failed"):
            path = self._remote("batches", batch_id, "queue", state)
            try:
                result[state] = sum(
                    1
                    for item in self.api.list_files(self.context, path)
                    if not is_directory_entry(item)
                    and str(item.get("name") or "").endswith(".json")
                )
            except OpenAPIError:
                result[state] = 0
        return result

    def _batch_manifest(self, batch_id: str) -> Dict[str, Any]:
        manifest = self.api.read_json(
            self.context,
            self._remote("batches", batch_id, "manifest.json"),
        )
        counts = self._counts(batch_id)
        if counts["pending"] or counts["running"]:
            status = "running"
        elif counts["failed"]:
            status = "partial"
        elif counts["done"] == int(manifest.get("task_count") or 0):
            status = "complete" if manifest.get("merged_at") else "parsed"
        else:
            status = manifest.get("status", "unknown")
        manifest["counts"] = counts
        manifest["status"] = status
        return manifest

    def status(
        self, batch_id: Optional[str]
    ) -> Tuple[str, Any]:
        if batch_id:
            payload: Any = self._batch_manifest(batch_id)
        else:
            batches_root = self._remote("batches")
            entries = self.api.list_files(self.context, batches_root)
            names = sorted(
                (
                    str(item.get("name"))
                    for item in entries
                    if is_directory_entry(item)
                    and item.get("name")
                ),
                reverse=True,
            )[:20]
            payload = []
            for name in names:
                try:
                    manifest = self._batch_manifest(name)
                except OpenAPIError:
                    continue
                payload.append(
                    {
                        "batch_id": manifest.get("batch_id") or name,
                        "created_at": manifest.get("created_at"),
                        "status": manifest.get("status"),
                        "counts": manifest.get("counts", {}),
                        "documents": len(manifest.get("documents", [])),
                        "slurm": manifest.get("slurm", {}),
                    }
                )
        output = json.dumps(payload, ensure_ascii=True, indent=2)
        return output, payload

    def upload_sources(self, sources: Sequence[Path], upload_id: str) -> str:
        remote_dir = self._remote("inbox", upload_id)
        self.api.mkdir(self.context, remote_dir)
        created = {remote_dir}
        for local_path, relative_parent in _pdf_uploads(sources):
            target = remote_dir
            for part in relative_parent.parts:
                target = join_remote_path(target, part)
                if target not in created:
                    self.api.mkdir(self.context, target)
                    created.add(target)
            self.api.upload_file(
                self.context,
                local_path,
                target,
                chunk_size=8 * 1024 * 1024,
            )
        return remote_dir

    def fetch(self, batch_id: str, destination: Path) -> Path:
        target = destination / "output"
        if target.exists() and any(target.iterdir()):
            raise OpenAPIError(
                "output directory already exists and is not empty"
            )
        self.api.download_tree(
            self.context,
            self._remote("batches", batch_id, "output"),
            target,
        )
        return target

    def deploy(
        self,
        source_dir: Path,
        config: Path,
        launcher: Path,
    ) -> None:
        private_config = json.loads(config.read_text(encoding="utf-8"))
        configured_root = resolve_home_relative(
            str(self.context["home_path"]),
            str(private_config.get("root") or self.saved_remote_root),
        )
        if configured_root != self.remote_root:
            raise OpenAPIError(
                "private config root does not match the selected deployment"
            )
        queue = str(private_config.get("partition_cpu") or "")
        if not queue:
            raise OpenAPIError("private config is missing partition_cpu")
        directories = [
            self.remote_root,
            self._remote("app"),
            self._remote("app", "ocrdrop"),
            self._remote("app", "ocrdrop", "backends"),
            self._remote("inbox"),
            self._remote("batches"),
            self._remote("control-logs"),
        ]
        for directory in directories:
            self.api.mkdir(self.context, directory)
        with tempfile.TemporaryDirectory(prefix="ocrdrop-deploy-") as tmp:
            stage = Path(tmp)
            controller = stage / "mineru_drop.py"
            runtime_launcher = stage / "dcu-python"
            remote_config = stage / "config.json"
            shutil.copy2(str(source_dir / "remote.py"), str(controller))
            shutil.copy2(str(launcher), str(runtime_launcher))
            shutil.copy2(str(config), str(remote_config))
            self.api.upload_file(
                self.context, controller, self._remote("app"), cover=True
            )
            self.api.upload_file(
                self.context,
                source_dir / "__init__.py",
                self._remote("app", "ocrdrop"),
                cover=True,
            )
            for name in ("__init__.py", "base.py", "mineru.py", "paddleocr.py"):
                self.api.upload_file(
                    self.context,
                    source_dir / "backends" / name,
                    self._remote("app", "ocrdrop", "backends"),
                    cover=True,
                )
            self.api.upload_file(
                self.context,
                runtime_launcher,
                self._remote("app"),
                cover=True,
            )
            self.api.upload_file(
                self.context,
                remote_config,
                self.remote_root,
                cover=True,
            )
        self._deployment_config_cache = private_config
        command = " ".join(
            [
                "chmod",
                "600",
                shlex.quote(self._remote("config.json")),
                "&&",
                "chmod",
                "+x",
                shlex.quote(self._remote("app", "mineru_drop.py")),
                shlex.quote(self._remote("app", "dcu-python")),
            ]
        )
        logs = self._remote("control-logs")
        request_id = uuid.uuid4().hex[:12]
        job_id = self.api.submit_job(
            self.context,
            name="ocrdrop-deploy-" + request_id[:8],
            command=command,
            work_dir=self.remote_root,
            queue=queue,
            cpus=1,
            memory="1gb",
            walltime="00:05:00",
            stdout=join_remote_path(logs, request_id + ".out"),
            stderr=join_remote_path(logs, request_id + ".err"),
        )
        job = self.api.wait_job(
            self.context, job_id, timeout=self.control_timeout
        )
        exit_code = str(job.get("exit_code") or "")
        if job.get("state") != "COMPLETED" or (
            exit_code and not exit_code.startswith("0")
        ):
            raise OpenAPIError("deployment permission job failed")
