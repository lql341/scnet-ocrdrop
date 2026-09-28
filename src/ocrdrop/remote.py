#!/usr/bin/env python3
"""Slurm-native drop queue for OCR document backends.

The controller runs on a login node with the standard library only.  Worker
commands run inside the validated MinerU/DCU runtime through the configured
launcher.  Queue claims use atomic renames on the shared filesystem so a Slurm
job array can dynamically balance page chunks without a database service.
"""

from __future__ import print_function

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

from ocrdrop.backends import get_backend_class


SCHEMA = "scnet-ocrdrop.v1"
PDF_SUFFIXES = {".pdf"}
TASK_STATES = ("pending", "running", "done", "failed")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    # Login-node Python 3.6 may decode non-ASCII filenames through
    # surrogateescape when its inherited locale is not UTF-8. ASCII escaping
    # preserves those filesystem bytes and avoids UnicodeEncodeError.
    tmp.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_config(path):
    config_path = Path(path).expanduser().resolve()
    cfg = read_json(config_path)
    cfg.setdefault("backend", cfg.get("adapter", "mineru3"))
    required = [
        "root",
        "partition_dcu",
        "partition_cpu",
        "venv",
        "python_launcher",
        "module_loads",
    ]
    if cfg["backend"].startswith("mineru"):
        required.append("model_config")
    missing = [key for key in required if not cfg.get(key)]
    if missing:
        raise SystemExit("missing config keys: " + ", ".join(missing))
    cfg["_config_path"] = str(config_path)
    cfg["root"] = str(Path(cfg["root"]).expanduser().resolve())
    cfg.setdefault("python_module", "python/3.8.10")
    cfg.setdefault("gres", "dcu:1")
    cfg.setdefault("cpus_per_worker", 8)
    cfg.setdefault("memory_per_worker", "27gb")
    cfg.setdefault("worker_walltime", "08:00:00")
    cfg.setdefault("merge_walltime", "00:30:00")
    cfg.setdefault("chunk_pages", 96)
    cfg.setdefault("whole_document_pages", 128)
    cfg.setdefault("max_workers", 4)
    cfg.setdefault("language", "ch")
    cfg.setdefault("parse_method", "auto")
    cfg.setdefault("formula_enable", True)
    cfg.setdefault("table_enable", True)
    cfg.setdefault("thread_count", 8)
    # Keep the old key readable so existing private configurations continue to
    # work while callers migrate to the backend-neutral name.
    cfg.setdefault("adapter", cfg["backend"])
    cfg.setdefault("tier", "basic")
    cfg.setdefault("ocr_mode", "txt")
    cfg.setdefault("small_backend", "")
    cfg.setdefault("device_mode", "")
    cfg.setdefault("intra_op_threads", cfg["thread_count"])
    cfg.setdefault("inter_op_threads", 1)
    cfg.setdefault("disable_formula_sdpa", False)
    cfg.setdefault("mineru_config", "")
    cfg.setdefault("runtime_env", {})
    cfg.setdefault("activate_venv", cfg["backend"] != "paddleocr")
    cfg.setdefault("paddleocr_engine", "transformers")
    cfg.setdefault("paddleocr_device", "gpu:0")
    cfg.setdefault("text_detection_model_name", "PP-OCRv5_mobile_det")
    cfg.setdefault("text_recognition_model_name", "PP-OCRv5_mobile_rec")
    cfg.setdefault("render_scale", 2.0)
    cfg.setdefault("trust_remote_code", False)
    return cfg


def app_dir(cfg):
    return Path(cfg["root"]) / "app"


def batch_dir(cfg, batch_id):
    validate_id(batch_id, "batch id")
    return Path(cfg["root"]) / "batches" / batch_id


def validate_id(value, label):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value or ""):
        raise SystemExit("invalid %s: %r" % (label, value))


def slugify(name):
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    return (value or "document")[:80]


def sha256_file(path, block_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            block = fh.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def pdf_page_count(path):
    proc = subprocess.run(
        ["pdfinfo", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("pdfinfo failed for %s: %s" % (path, proc.stderr.strip()))
    match = re.search(r"^Pages:\s+(\d+)\s*$", proc.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("pdfinfo did not report page count for %s" % path)
    return int(match.group(1))


def collect_inputs(raw_paths):
    found = []
    for raw in raw_paths:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in PDF_SUFFIXES))
        elif path.is_file() and path.suffix.lower() in PDF_SUFFIXES:
            found.append(path)
        else:
            print("skip unsupported or missing input: %s" % path, file=sys.stderr)
    unique = []
    seen = set()
    for path in found:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    if not unique:
        raise SystemExit("no PDF inputs found")
    return unique


def plan_ranges(page_count, chunk_pages, whole_document_pages):
    if page_count <= 0:
        return []
    if page_count <= whole_document_pages:
        return [(0, page_count - 1)]
    result = []
    start = 0
    while start < page_count:
        end = min(page_count - 1, start + chunk_pages - 1)
        result.append((start, end))
        start = end + 1
    return result


def create_batch(cfg, inputs, workers, chunk_pages, whole_document_pages, language):
    root = Path(cfg["root"])
    root.mkdir(parents=True, exist_ok=True)
    batch_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    bdir = batch_dir(cfg, batch_id)
    for directory in [
        bdir / "queue" / state for state in TASK_STATES
    ] + [
        bdir / "chunks",
        bdir / "output",
        bdir / "logs",
        bdir / "errors",
        bdir / "slurm",
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    documents = []
    tasks = []
    for doc_index, path in enumerate(inputs, 1):
        pages = pdf_page_count(path)
        digest = sha256_file(path)
        doc_id = "%03d-%s-%s" % (doc_index, slugify(path.stem), digest[:8])
        ranges = plan_ranges(pages, chunk_pages, whole_document_pages)
        document = {
            "doc_id": doc_id,
            "source_path": str(path),
            "source_name": path.name,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "page_count": pages,
            "chunk_count": len(ranges),
        }
        documents.append(document)
        for chunk_index, (start, end) in enumerate(ranges, 1):
            task_id = "%s-c%04d-p%06d-%06d" % (
                doc_id,
                chunk_index,
                start + 1,
                end + 1,
            )
            chunk_name = "%s__p%06d-%06d" % (slugify(path.stem), start + 1, end + 1)
            task = {
                "schema": SCHEMA,
                "batch_id": batch_id,
                "task_id": task_id,
                "doc_id": doc_id,
                "chunk_index": chunk_index,
                "source_path": str(path),
                "source_name": path.name,
                "source_sha256": digest,
                "start_page_id": start,
                "end_page_id": end,
                "page_count": end - start + 1,
                "chunk_name": chunk_name,
                "language": language,
                "status": "pending",
                "created_at": now_iso(),
                "attempt": 0,
            }
            tasks.append(task)
            atomic_write_json(bdir / "queue" / "pending" / (task_id + ".json"), task)

    effective_workers = max(1, min(int(workers), int(cfg["max_workers"]), len(tasks)))
    manifest = {
        "schema": SCHEMA,
        "batch_id": batch_id,
        "created_at": now_iso(),
        "status": "planned",
        "backend": cfg["backend"],
        "config_path": cfg["_config_path"],
        "workers": effective_workers,
        "chunk_pages": chunk_pages,
        "whole_document_pages": whole_document_pages,
        "language": language,
        "documents": documents,
        "task_count": len(tasks),
        "merge_note": (
            "Long documents are split without overlap. Chunk boundaries are explicit; "
            "cross-boundary paragraph/table continuity requires manual review."
        ),
        "slurm": {},
    }
    atomic_write_json(bdir / "manifest.json", manifest)
    return manifest


def shell_quote(value):
    import shlex

    return shlex.quote(str(value))


def render_worker_slurm(cfg, manifest):
    bdir = batch_dir(cfg, manifest["batch_id"])
    modules = " ".join(shell_quote(x) for x in cfg["module_loads"])
    max_index = manifest["workers"] - 1
    threads = int(cfg["thread_count"])
    activation = (
        "source %s/bin/activate" % shell_quote(cfg["venv"])
        if cfg.get("activate_venv")
        else ": # runtime launcher owns the Python environment"
    )
    backend_env = []
    if cfg["backend"].startswith("mineru"):
        backend_env.extend([
            "export MINERU_MODEL_SOURCE=local",
            "export MINERU_DEVICE_MODE=cuda",
            "export MINERU_TOOLS_CONFIG_JSON=%s"
            % shell_quote(cfg["model_config"]),
            "export MINERU_PDF_RENDER_THREADS=2",
        ])
    elif cfg["backend"] == "paddleocr":
        backend_env.extend([
            "export PADDLE_PDX_MODEL_SOURCE=%s"
            % shell_quote(cfg.get("paddle_model_source", "BOS")),
            "export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True",
        ])
        if cfg.get("paddle_cache_home"):
            backend_env.append(
                "export PADDLE_PDX_CACHE_HOME=%s"
                % shell_quote(cfg["paddle_cache_home"])
            )
    backend_env_text = "\n".join(backend_env)
    optional_env = []
    if cfg["backend"].startswith("mineru"):
        for name, value in (
            ("MINERU_CONFIG", cfg.get("mineru_config")),
            ("MINERU_MODEL_SMALL_BACKEND", cfg.get("small_backend")),
            ("MINERU_DEVICE_MODE", cfg.get("device_mode")),
            ("MINERU_INTRA_OP_NUM_THREADS", cfg.get("intra_op_threads")),
            ("MINERU_INTER_OP_NUM_THREADS", cfg.get("inter_op_threads")),
        ):
            if value not in (None, ""):
                optional_env.append("export %s=%s" % (name, shell_quote(value)))
    for name, value in sorted(cfg.get("runtime_env", {}).items()):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid runtime environment variable: %r" % name)
        optional_env.append("export %s=%s" % (name, shell_quote(value)))
    optional_env_text = "\n".join(optional_env)
    return """#!/bin/bash -l
#SBATCH -p {partition}
#SBATCH --gres={gres}
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={memory}
#SBATCH --time={walltime}
#SBATCH --array=0-{max_index}
#SBATCH -J {backend}-{batch}
#SBATCH -o {logs}/worker-%A_%a.out
#SBATCH -e {logs}/worker-%A_%a.err

set -u
module purge
module load {modules}
{activation}

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export OMP_NUM_THREADS={threads}
export ORT_NUM_THREADS={threads}
export OPENBLAS_NUM_THREADS={threads}
export MKL_NUM_THREADS={threads}
{backend_env}
{optional_env}

{launcher} {app}/mineru_drop.py --config {config} worker --batch {batch}
rc=$?
echo "OCRDROP_WORKER_RC=$rc"
exit "$rc"
""".format(
        partition=cfg["partition_dcu"],
        gres=cfg["gres"],
        cpus=int(cfg["cpus_per_worker"]),
        memory=cfg["memory_per_worker"],
        walltime=cfg["worker_walltime"],
        max_index=max_index,
        backend=cfg["backend"],
        batch=manifest["batch_id"],
        logs=str(bdir / "logs"),
        modules=modules,
        activation=activation,
        threads=threads,
        backend_env=backend_env_text,
        optional_env=optional_env_text,
        launcher=shell_quote(cfg["python_launcher"]),
        app=shell_quote(app_dir(cfg)),
        config=shell_quote(cfg["_config_path"]),
    )


def render_merge_slurm(cfg, manifest, worker_job_id):
    bdir = batch_dir(cfg, manifest["batch_id"])
    return """#!/bin/bash -l
#SBATCH -p {partition}
#SBATCH --cpus-per-task=2
#SBATCH --mem=4gb
#SBATCH --time={walltime}
#SBATCH -J ocrdrop-merge-{batch}
#SBATCH -o {logs}/merge-%j.out
#SBATCH -e {logs}/merge-%j.err
#SBATCH --dependency=afterany:{worker_job_id}

set -u
module purge
module load {python_module}
python3 {app}/mineru_drop.py --config {config} merge --batch {batch}
rc=$?
echo "OCRDROP_MERGE_RC=$rc"
exit "$rc"
""".format(
        partition=cfg["partition_cpu"],
        walltime=cfg["merge_walltime"],
        batch=manifest["batch_id"],
        logs=str(bdir / "logs"),
        worker_job_id=worker_job_id,
        python_module=shell_quote(cfg["python_module"]),
        app=shell_quote(app_dir(cfg)),
        config=shell_quote(cfg["_config_path"]),
    )


def sbatch(script_path):
    proc = subprocess.run(
        ["sbatch", "--parsable", str(script_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("sbatch failed: %s" % proc.stderr.strip())
    return proc.stdout.strip().split(";", 1)[0]


def dispatch_batch(cfg, batch_id, workers=None):
    bdir = batch_dir(cfg, batch_id)
    manifest_path = bdir / "manifest.json"
    manifest = read_json(manifest_path)
    pending = list((bdir / "queue" / "pending").glob("*.json"))
    if not pending:
        raise SystemExit("batch has no pending tasks: %s" % batch_id)
    if workers is not None:
        manifest["workers"] = max(
            1,
            min(int(workers), int(cfg["max_workers"]), len(pending)),
        )
    else:
        manifest["workers"] = max(1, min(int(manifest["workers"]), len(pending)))

    worker_script = bdir / "slurm" / "workers.slurm"
    worker_script.write_text(render_worker_slurm(cfg, manifest), encoding="utf-8")
    worker_job_id = sbatch(worker_script)

    merge_script = bdir / "slurm" / "merge.slurm"
    merge_script.write_text(
        render_merge_slurm(cfg, manifest, worker_job_id),
        encoding="utf-8",
    )
    merge_job_id = sbatch(merge_script)

    manifest["status"] = "submitted"
    manifest["submitted_at"] = now_iso()
    manifest["slurm"] = {
        "worker_job_id": worker_job_id,
        "merge_job_id": merge_job_id,
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def task_counts(bdir):
    return {
        state: len(list((bdir / "queue" / state).glob("*.json")))
        for state in TASK_STATES
    }


def update_batch_status(cfg, batch_id):
    bdir = batch_dir(cfg, batch_id)
    manifest_path = bdir / "manifest.json"
    manifest = read_json(manifest_path)
    counts = task_counts(bdir)
    if counts["pending"] or counts["running"]:
        status = "running"
    elif counts["failed"]:
        status = "partial"
    elif counts["done"] == manifest["task_count"]:
        status = "complete" if manifest.get("merged_at") else "parsed"
    else:
        status = manifest.get("status", "unknown")
    manifest["status"] = status
    manifest["counts"] = counts
    manifest["updated_at"] = now_iso()
    atomic_write_json(manifest_path, manifest)
    return manifest


def claim_task(bdir):
    pending_dir = bdir / "queue" / "pending"
    running_dir = bdir / "queue" / "running"
    worker_tag = "%s_%s_%s" % (
        os.environ.get("SLURM_JOB_ID", "local"),
        os.environ.get("SLURM_ARRAY_TASK_ID", "0"),
        os.getpid(),
    )
    for source in sorted(pending_dir.glob("*.json")):
        destination = running_dir / (source.stem + "." + worker_tag + ".json")
        try:
            os.rename(str(source), str(destination))
            return destination, read_json(destination)
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return None, None


def worker_main(cfg, batch_id):
    bdir = batch_dir(cfg, batch_id)
    launcher = str(Path(cfg["python_launcher"]).resolve())

    import multiprocessing

    multiprocessing.set_executable(launcher)
    try:
        multiprocessing.get_context("spawn").set_executable(launcher)
    except Exception:
        pass

    from importlib.metadata import version

    backend_class = get_backend_class(cfg)
    package_version = version(backend_class.package_name)
    if not backend_class.validate_version(package_version):
        raise RuntimeError(
            "backend %s requires %s %s.x, found %s"
            % (
                backend_class.name,
                backend_class.package_name,
                backend_class.expected_major,
                package_version,
            )
        )
    backend = backend_class(cfg)

    try:
        import torch

        def optional_version(name):
            try:
                return version(name)
            except Exception:
                return None

        runtime = {
            "recorded_at": now_iso(),
            "python": sys.version,
            "backend": backend.name,
            "backend_package": backend.package_name,
            "backend_version": package_version,
            "torch": optional_version("torch"),
            "torchvision": optional_version("torchvision"),
            "onnxruntime": optional_version("onnxruntime"),
            "device_count": torch.cuda.device_count(),
            "arch": (
                torch.cuda.get_device_properties(0).gcnArchName
                if torch.cuda.device_count()
                else None
            ),
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
            "hostname": os.uname().nodename,
        }
        runtime_name = "runtime-%s_%s.json" % (
            os.environ.get("SLURM_JOB_ID", "local"),
            os.environ.get("SLURM_ARRAY_TASK_ID", "0"),
        )
        atomic_write_json(bdir / "logs" / runtime_name, runtime)
    except Exception as exc:
        print("RUNTIME_FINGERPRINT_FAILED %s: %s" % (type(exc).__name__, exc), flush=True)

    failed_count = 0
    completed_count = 0
    while True:
        running_path, task = claim_task(bdir)
        if task is None:
            break
        started = time.perf_counter()
        task["status"] = "running"
        task["started_at"] = now_iso()
        task["attempt"] = int(task.get("attempt", 0)) + 1
        task["worker"] = {
            "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
            "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
            "hostname": os.uname().nodename,
            "pid": os.getpid(),
        }
        atomic_write_json(running_path, task)
        task_output = bdir / "chunks" / task["doc_id"] / task["task_id"]
        task_output.mkdir(parents=True, exist_ok=True)
        try:
            auto_dir = backend.parse(task, task_output)
            task["status"] = "done"
            task["completed_at"] = now_iso()
            task["elapsed_s"] = round(time.perf_counter() - started, 3)
            task["output_dir"] = str(auto_dir)
            task["output_files"] = {
                str(path.relative_to(auto_dir)): path.stat().st_size
                for path in sorted(auto_dir.rglob("*"))
                if path.is_file()
            }
            atomic_write_json(running_path, task)
            os.replace(
                str(running_path),
                str(bdir / "queue" / "done" / (task["task_id"] + ".json")),
            )
            completed_count += 1
            print(
                "TASK_DONE id=%s pages=%d-%d elapsed=%.3f"
                % (
                    task["task_id"],
                    task["start_page_id"] + 1,
                    task["end_page_id"] + 1,
                    task["elapsed_s"],
                ),
                flush=True,
            )
        except Exception as exc:
            failed_count += 1
            task["status"] = "failed"
            task["completed_at"] = now_iso()
            task["elapsed_s"] = round(time.perf_counter() - started, 3)
            task["error"] = "%s: %s" % (type(exc).__name__, exc)
            error_path = bdir / "errors" / (task["task_id"] + ".traceback.txt")
            error_path.write_text(traceback.format_exc(), encoding="utf-8")
            task["traceback_path"] = str(error_path)
            atomic_write_json(running_path, task)
            os.replace(
                str(running_path),
                str(bdir / "queue" / "failed" / (task["task_id"] + ".json")),
            )
            print("TASK_FAILED id=%s error=%s" % (task["task_id"], task["error"]), flush=True)

    backend.shutdown()
    update_batch_status(cfg, batch_id)
    print(
        "WORKER_DONE completed=%d failed=%d" % (completed_count, failed_count),
        flush=True,
    )
    return 1 if failed_count else 0


def doctor_main(cfg):
    launcher = Path(cfg["python_launcher"])
    backend_class = get_backend_class(cfg)
    model_config_ok = (
        Path(cfg["model_config"]).is_file()
        if cfg["backend"].startswith("mineru")
        else True
    )
    checks = {
        "root": Path(cfg["root"]).is_dir(),
        "python_launcher": launcher.is_file() and os.access(str(launcher), os.X_OK),
        "venv_python": (Path(cfg["venv"]) / "bin" / "python3.10").is_file(),
        "model_config": model_config_ok,
        "sbatch": shutil.which("sbatch") is not None,
        "pdfinfo": shutil.which("pdfinfo") is not None,
    }
    package_version = None
    version_error = None
    if checks["python_launcher"]:
        doctor_env = os.environ.copy()
        doctor_env.update({
            str(name): str(value)
            for name, value in cfg.get("runtime_env", {}).items()
        })
        if cfg["backend"] == "paddleocr":
            doctor_env.setdefault(
                "PADDLE_PDX_MODEL_SOURCE",
                str(cfg.get("paddle_model_source", "BOS")),
            )
            if cfg.get("paddle_cache_home"):
                doctor_env.setdefault(
                    "PADDLE_PDX_CACHE_HOME",
                    str(cfg["paddle_cache_home"]),
                )
        proc = subprocess.run(
            [
                str(launcher),
                "-c",
                (
                    "from importlib.metadata import version; "
                    "print(version(%r))"
                    % backend_class.package_name
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=doctor_env,
        )
        if proc.returncode == 0:
            package_version = proc.stdout.strip()
        else:
            version_error = proc.stderr.strip()
    backend_name = cfg["backend"]
    backend_compatible = backend_class.validate_version(package_version)
    payload = {
        "schema": SCHEMA,
        "backend": backend_name,
        "backend_version": package_version,
        "backend_compatible": backend_compatible,
        "checks": checks,
        "version_error": version_error,
        "recommendation": (
            "Keep each OCR backend in a separate validated runtime. "
            "Use persistent workers to amortize model loading."
        ),
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if all(checks.values()) and backend_compatible else 1


def inventory_main(cfg):
    root = Path(cfg["root"])
    items = []

    def add(name, path, role, policy):
        path = Path(path)
        items.append({
            "name": name,
            "path": str(path),
            "exists": path.exists(),
            "realpath": os.path.realpath(str(path)),
            "role": role,
            "policy": policy,
        })

    add(
        "remote_config",
        cfg["_config_path"],
        "Slurm resources, runtime paths, and backend settings",
        "redeploy-from-private-config",
    )
    add(
        "controller",
        app_dir(cfg) / "mineru_drop.py",
        "Login-node controller and Slurm worker entrypoint",
        "redeploy-from-git",
    )
    add(
        "backend_package_init",
        app_dir(cfg) / "ocrdrop" / "__init__.py",
        "Python package marker",
        "redeploy-from-git",
    )
    add(
        "backend_registry",
        app_dir(cfg) / "ocrdrop" / "backends" / "__init__.py",
        "Backend name-to-class registry",
        "redeploy-from-git",
    )
    add(
        "backend_base",
        app_dir(cfg) / "ocrdrop" / "backends" / "base.py",
        "DocumentBackend contract",
        "redeploy-from-git",
    )
    add(
        "mineru_backend",
        app_dir(cfg) / "ocrdrop" / "backends" / "mineru.py",
        "MinerU 3 and MinerU 4 worker implementations",
        "redeploy-from-git",
    )
    add(
        "paddleocr_backend",
        app_dir(cfg) / "ocrdrop" / "backends" / "paddleocr.py",
        "PaddleOCR worker implementation",
        "redeploy-from-git",
    )
    add(
        "worker_launcher",
        cfg["python_launcher"],
        "Validated Python/glibc launcher used on compute nodes",
        "preserve-runtime",
    )
    add(
        "backend_venv",
        cfg["venv"],
        "Python packages and accelerator-specific framework",
        "preserve-runtime",
    )
    add(
        "backend_python",
        Path(cfg["venv"]) / "bin" / "python3.10",
        "Backend Python interpreter",
        "preserve-runtime",
    )
    add(
        "inbox",
        root / "inbox",
        "Uploaded source PDFs required for retry",
        "retain-with-batch",
    )
    add(
        "batches",
        root / "batches",
        "Manifests, task state, logs, chunks, and merged output",
        "retain-until-exported",
    )
    if cfg["backend"].startswith("mineru"):
        add(
            "model_config",
            cfg["model_config"],
            "MinerU model paths and backend configuration",
            "preserve-runtime",
        )
    if cfg["backend"] == "paddleocr":
        cache = Path(cfg.get("paddle_cache_home", ""))
        add(
            "paddle_cache",
            cache,
            "Offline PaddleX model cache",
            "preserve-runtime",
        )
        add(
            "paddle_det_model",
            cache / "official_models" / "PP-OCRv5_mobile_det_safetensors",
            "PP-OCRv5 detection weights",
            "preserve-runtime",
        )
        add(
            "paddle_rec_model",
            cache / "official_models" / "PP-OCRv5_mobile_rec_safetensors",
            "PP-OCRv5 recognition weights",
            "preserve-runtime",
        )

    payload = {
        "schema": SCHEMA,
        "backend": cfg["backend"],
        "deployment_root": cfg["root"],
        "items": items,
        "all_present": all(item["exists"] for item in items),
        "note": (
            "Git can restore redeploy-from-git files. Runtime environments, "
            "model caches, private config, inbox, and batches require separate retention."
        ),
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload["all_present"] else 1


def shift_page_indices(value, offset):
    if isinstance(value, list):
        return [shift_page_indices(item, offset) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ("page_idx", "page_num") and isinstance(item, int):
                result[key] = item + offset
            else:
                result[key] = shift_page_indices(item, offset)
        return result
    return value


def copy_images_and_rewrite(markdown, auto_dir, destination, prefix):
    source_images = auto_dir / "images"
    destination.mkdir(parents=True, exist_ok=True)
    if not source_images.is_dir():
        return markdown
    mapping = {}
    for source in sorted(source_images.iterdir()):
        if not source.is_file():
            continue
        target_name = prefix + "-" + source.name
        shutil.copy2(str(source), str(destination / target_name))
        mapping[source.name] = target_name
    for old, new in mapping.items():
        markdown = markdown.replace("images/" + old, "images/" + new)
    return markdown


def find_named(auto_dir, suffix):
    matches = list(auto_dir.glob("*" + suffix))
    return matches[0] if matches else None


def public_document_record(document):
    """Remove private deployment paths from exported result metadata."""
    return {
        key: value
        for key, value in document.items()
        if key != "source_path"
    }


def public_task_record(task):
    """Return the reproducibility fields that are safe to fetch and share."""
    private_keys = {
        "output_dir",
        "source_path",
        "traceback_path",
    }
    result = {
        key: value
        for key, value in task.items()
        if key not in private_keys
    }
    worker = result.get("worker")
    if isinstance(worker, dict):
        result["worker"] = {
            key: value
            for key, value in worker.items()
            if key != "hostname"
        }
    return result


def merge_document(bdir, document, tasks):
    output_dir = bdir / "output" / document["doc_id"]
    images_dir = output_dir / "images"
    chunks_dir = output_dir / "chunks"
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    markdown_parts = []
    merged_content = []
    merged_middle = None
    merged_chunk_records = []

    for task in sorted(tasks, key=lambda item: int(item["start_page_id"])):
        auto_dir = Path(task["output_dir"])
        prefix = "p%06d-%06d" % (
            task["start_page_id"] + 1,
            task["end_page_id"] + 1,
        )
        md_path = find_named(auto_dir, ".md")
        if md_path:
            markdown = md_path.read_text(encoding="utf-8", errors="replace")
            markdown = copy_images_and_rewrite(markdown, auto_dir, images_dir, prefix)
            markdown_parts.append(
                "<!-- MinerU chunk: pages %d-%d -->\n\n%s"
                % (task["start_page_id"] + 1, task["end_page_id"] + 1, markdown.strip())
            )

        content_path = find_named(auto_dir, "_content_list.json")
        if content_path:
            content = read_json(content_path)
            shifted = shift_page_indices(content, int(task["start_page_id"]))
            if isinstance(shifted, list):
                merged_content.extend(shifted)
            else:
                merged_content.append(shifted)

        middle_path = find_named(auto_dir, "_middle.json")
        if middle_path:
            middle = read_json(middle_path)
            offset = int(task["start_page_id"])
            if "pdf_info" in middle:
                if merged_middle is None:
                    merged_middle = {
                        "pdf_info": [],
                        "_merge": {"schema": SCHEMA},
                    }
                pdf_info = shift_page_indices(middle.get("pdf_info", []), offset)
                merged_middle["pdf_info"].extend(pdf_info)
                for key in ("_backend", "_version_name"):
                    if key in middle and key not in merged_middle:
                        merged_middle[key] = middle[key]
            elif "pages" in middle:
                if merged_middle is None:
                    merged_middle = {
                        key: value
                        for key, value in middle.items()
                        if key != "pages"
                    }
                    merged_middle["pages"] = []
                    merged_middle["_merge"] = {"schema": SCHEMA}
                # MinerU 4 preserves source PDF page_idx for page-range parses.
                # Unlike MinerU 3 pdf_info, these indices must not be shifted
                # again during chunk merge.
                pages = middle.get("pages", [])
                merged_middle["pages"].extend(pages)

        chunk_manifest = {
            "task_id": task["task_id"],
            "pages": [task["start_page_id"] + 1, task["end_page_id"] + 1],
            "elapsed_s": task.get("elapsed_s"),
        }
        merged_chunk_records.append(chunk_manifest)
        atomic_write_json(
            chunks_dir / (task["task_id"] + ".json"),
            public_task_record(task),
        )

    (output_dir / "document.md").write_text(
        "\n\n---\n\n".join(markdown_parts).rstrip() + "\n",
        encoding="utf-8",
    )
    atomic_write_json(output_dir / "content_list.json", merged_content)
    atomic_write_json(
        output_dir / "middle.json",
        merged_middle if merged_middle is not None else {"_merge": {"schema": SCHEMA}},
    )
    document_manifest = {
        "schema": SCHEMA,
        "document": public_document_record(document),
        "merged_at": now_iso(),
        "complete": len(tasks) == int(document["chunk_count"]),
        "chunks": merged_chunk_records,
        "boundary_warning": (
            "Chunk boundaries are explicit. Cross-boundary paragraphs and tables "
            "may require manual review."
        ),
    }
    atomic_write_json(output_dir / "manifest.json", document_manifest)
    return document_manifest


def merge_main(cfg, batch_id):
    bdir = batch_dir(cfg, batch_id)
    manifest = update_batch_status(cfg, batch_id)
    done_tasks = [read_json(path) for path in sorted((bdir / "queue" / "done").glob("*.json"))]
    tasks_by_doc = {}
    for task in done_tasks:
        tasks_by_doc.setdefault(task["doc_id"], []).append(task)

    merged = []
    for document in manifest["documents"]:
        tasks = tasks_by_doc.get(document["doc_id"], [])
        if tasks:
            merged.append(merge_document(bdir, document, tasks))

    counts = task_counts(bdir)
    manifest["counts"] = counts
    manifest["merged_at"] = now_iso()
    manifest["merged_documents"] = len(merged)
    manifest["status"] = (
        "complete"
        if counts["done"] == manifest["task_count"] and not counts["failed"]
        else "partial"
    )
    atomic_write_json(bdir / "manifest.json", manifest)
    print(json.dumps({
        "batch_id": batch_id,
        "status": manifest["status"],
        "counts": counts,
        "output": str(bdir / "output"),
    }, ensure_ascii=True))
    return 0 if manifest["status"] == "complete" else 2


def requeue_tasks(cfg, batch_id, include_running):
    bdir = batch_dir(cfg, batch_id)
    moved = 0
    states = ["failed"] + (["running"] if include_running else [])
    for state in states:
        for source in sorted((bdir / "queue" / state).glob("*.json")):
            task = read_json(source)
            task["status"] = "pending"
            task["requeued_at"] = now_iso()
            task.pop("error", None)
            task.pop("traceback_path", None)
            destination = bdir / "queue" / "pending" / (task["task_id"] + ".json")
            atomic_write_json(destination, task)
            source.unlink()
            moved += 1
    update_batch_status(cfg, batch_id)
    return moved


def print_status(cfg, batch_id=None):
    root = Path(cfg["root"])
    batches_root = root / "batches"
    if batch_id:
        manifest = update_batch_status(cfg, batch_id)
        print(json.dumps(manifest, ensure_ascii=True, indent=2))
        return
    rows = []
    if batches_root.is_dir():
        for manifest_path in sorted(batches_root.glob("*/manifest.json"), reverse=True)[:20]:
            manifest = update_batch_status(cfg, manifest_path.parent.name)
            rows.append({
                "batch_id": manifest["batch_id"],
                "created_at": manifest["created_at"],
                "status": manifest["status"],
                "counts": manifest.get("counts", {}),
                "documents": len(manifest.get("documents", [])),
                "slurm": manifest.get("slurm", {}),
            })
    print(json.dumps(rows, ensure_ascii=True, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(prog="scnet-ocrdrop-remote")
    parser.add_argument("--config", required=True, help="Path to remote config.json")
    # Python 3.6 on the login node does not support required=True here.
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="Check Slurm tools, runtime paths, and MinerU major version")
    sub.add_parser("inventory", help="List protected deployment and runtime paths")

    submit = sub.add_parser("submit", help="Plan PDFs and submit Slurm jobs")
    submit.add_argument("inputs", nargs="+")
    submit.add_argument("--workers", type=int)
    submit.add_argument("--chunk-pages", type=int)
    submit.add_argument("--whole-document-pages", type=int)
    submit.add_argument("--language")
    submit.add_argument("--plan-only", action="store_true")

    dispatch = sub.add_parser("dispatch", help="Submit workers for an existing batch")
    dispatch.add_argument("--batch", required=True)
    dispatch.add_argument("--workers", type=int)

    worker = sub.add_parser("worker", help="Run a Slurm worker")
    worker.add_argument("--batch", required=True)

    merge = sub.add_parser("merge", help="Merge completed chunks")
    merge.add_argument("--batch", required=True)

    status = sub.add_parser("status", help="Show batch status")
    status.add_argument("--batch")

    retry = sub.add_parser("retry", help="Requeue failed tasks and submit workers")
    retry.add_argument("--batch", required=True)
    retry.add_argument("--workers", type=int)
    retry.add_argument("--include-running", action="store_true")

    return parser


def main():
    args = build_parser().parse_args()
    if not args.command:
        build_parser().print_help()
        return 2
    cfg = load_config(args.config)
    if args.command == "doctor":
        return doctor_main(cfg)
    if args.command == "inventory":
        return inventory_main(cfg)
    if args.command == "submit":
        inputs = collect_inputs(args.inputs)
        workers = (
            args.workers
            if args.workers is not None
            else (1 if len(inputs) == 1 else int(cfg["max_workers"]))
        )
        chunk_pages = args.chunk_pages or int(cfg["chunk_pages"])
        whole_limit = args.whole_document_pages or int(cfg["whole_document_pages"])
        language = args.language or cfg["language"]
        if chunk_pages < 1 or whole_limit < 1:
            raise SystemExit("page limits must be positive")
        manifest = create_batch(
            cfg,
            inputs,
            workers,
            chunk_pages,
            whole_limit,
            language,
        )
        if not args.plan_only:
            manifest = dispatch_batch(cfg, manifest["batch_id"])
        print(json.dumps({
            "batch_id": manifest["batch_id"],
            "status": manifest["status"],
            "task_count": manifest["task_count"],
            "workers": manifest["workers"],
            "slurm": manifest.get("slurm", {}),
            "batch_dir": str(batch_dir(cfg, manifest["batch_id"])),
        }, ensure_ascii=True))
        return 0
    if args.command == "dispatch":
        manifest = dispatch_batch(cfg, args.batch, args.workers)
        print(json.dumps(manifest["slurm"], ensure_ascii=True))
        return 0
    if args.command == "worker":
        return worker_main(cfg, args.batch)
    if args.command == "merge":
        return merge_main(cfg, args.batch)
    if args.command == "status":
        print_status(cfg, args.batch)
        return 0
    if args.command == "retry":
        moved = requeue_tasks(cfg, args.batch, args.include_running)
        if not moved:
            raise SystemExit("no tasks were requeued")
        manifest = dispatch_batch(cfg, args.batch, args.workers)
        print(json.dumps({
            "batch_id": args.batch,
            "requeued": moved,
            "slurm": manifest["slurm"],
        }, ensure_ascii=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
