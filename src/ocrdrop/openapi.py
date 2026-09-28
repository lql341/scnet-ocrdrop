"""SCNet OpenAPI transport primitives.

Authentication and endpoint behavior follow the MIT-licensed scnet-hpc
implementation. This module is dependency-free and intentionally keeps tokens
in memory only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import __version__
from .credentials import load_openapi_credentials


STATUS_MAP = {
    "statR": "RUNNING",
    "statQ": "PENDING",
    "statH": "HELD",
    "statS": "SUSPENDED",
    "statE": "EXITING",
    "statC": "COMPLETED",
    "statW": "WAITING",
    "statX": "OTHER",
}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "OTHER"}
WALLTIME_RE = re.compile(r"^(?:[0-9]+-)?[0-9]{1,3}:[0-9]{2}:[0-9]{2}$")


class OpenAPIError(RuntimeError):
    """A bounded user-facing OpenAPI failure."""


def redact_error_detail(value: str) -> str:
    result = str(value)
    result = re.sub(
        r'(?i)("?(?:accessKey|secretKey|signature|token)"?\s*[:=]\s*")'
        r'[^"]+',
        r'\1<redacted>',
        result,
    )
    result = re.sub(
        r"(?i)((?:accessKey|secretKey|signature|token)\s*[:=]\s*)"
        r"[^\s,;}]+",
        r"\1<redacted>",
        result,
    )
    result = re.sub(
        r"(https?://[^/\s:@]+:)[^/\s@]+@",
        r"\1<redacted>@",
        result,
    )
    return result


def canonical_signature(
    access_key: str, timestamp: str, user: str, secret_key: str
) -> str:
    message = json.dumps(
        {"accessKey": access_key, "timestamp": timestamp, "user": user},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def service_endpoint(base_url: str, service: str, suffix: str) -> str:
    split = urlsplit(base_url.rstrip("/"))
    path = split.path.rstrip("/")
    service_part = "/" + service
    if not path.endswith(service_part):
        path += service_part
    path += "/" + suffix.lstrip("/")
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


def _safe_absolute(path: str, label: str) -> str:
    if (
        not path.startswith("/")
        or "\n" in path
        or "\r" in path
        or "\x00" in path
    ):
        raise OpenAPIError("%s must be a safe absolute path" % label)
    return path


def join_remote_path(root: str, *parts: str) -> str:
    root = _safe_absolute(root, "remote root")
    result = PurePosixPath(root)
    for value in parts:
        text = str(value)
        if not text or text.startswith("/") or any(
            item in ("", ".", "..") for item in PurePosixPath(text).parts
        ):
            raise OpenAPIError("unsafe remote path component: %r" % text)
        result = result / text
    return str(result)


def is_directory_entry(item: Mapping[str, Any]) -> bool:
    value = item.get("isDirectory")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "directory", "dir")


def resolve_home_relative(home_path: str, value: str) -> str:
    home = _safe_absolute(str(home_path), "OpenAPI home path").rstrip("/")
    raw = str(value or "").strip()
    if raw.startswith("/"):
        resolved = str(PurePosixPath(raw))
        if resolved != home and not resolved.startswith(home + "/"):
            raise OpenAPIError(
                "remote root must remain inside the selected OpenAPI home"
            )
        return resolved
    if raw.startswith("~/"):
        raw = raw[2:]
    raw = raw.strip("/")
    if not raw:
        return home
    parts = PurePosixPath(raw).parts
    if any(part in ("", ".", "..") for part in parts):
        raise OpenAPIError("remote root must be normalized")
    return str(PurePosixPath(home).joinpath(*parts))


class SCNetOpenAPI:
    """Small SCNet HPC/OpenAPI client used by the OCR transport."""

    def __init__(
        self,
        *,
        timeout: int = 60,
        credentials: Optional[Mapping[str, str]] = None,
    ):
        self.timeout = int(timeout)
        self.explicit_credentials = dict(credentials or {})
        self._regions_cache: Optional[List[Dict[str, Any]]] = None
        self._center_cache: Dict[str, Dict[str, Any]] = {}
        self._context_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    @staticmethod
    def env(name: str, default: Optional[str] = None) -> Optional[str]:
        value = os.environ.get(name)
        return value if value not in (None, "") else default

    def request(
        self,
        method: str,
        url: str,
        *,
        token: Optional[str] = None,
        json_body: Any = None,
        form: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "scnet-ocrdrop/%s" % __version__,
        }
        if headers:
            request_headers.update(headers)
        if token:
            request_headers["token"] = token
        data = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, method=method, headers=request_headers)
        attempts = 3 if method.upper() == "GET" else 1
        raw = None
        for attempt in range(attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                break
            except HTTPError as exc:
                detail = redact_error_detail(
                    exc.read().decode("utf-8", "replace")
                )
                raise OpenAPIError(
                    "HTTP %s from SCNet OpenAPI: %s"
                    % (exc.code, detail[:500])
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise OpenAPIError(
                    "SCNet OpenAPI request failed: %s" % exc
                ) from exc
        if raw is None:
            raise OpenAPIError("SCNet OpenAPI returned no data")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenAPIError("SCNet OpenAPI returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise OpenAPIError("SCNet OpenAPI response must be a JSON object")
        code = str(payload.get("code", ""))
        if code != "0":
            raise OpenAPIError(
                "SCNet OpenAPI error %s: %s"
                % (code, payload.get("msg") or "unknown error")
            )
        return payload.get("data")

    def _credentials(self) -> Dict[str, str]:
        stored, _ = load_openapi_credentials()
        values = {
            "user": (
                self.env("SCNET_OPENAPI_USER")
                or self.explicit_credentials.get("user")
                or (stored or {}).get("user")
            ),
            "access_key": (
                self.env("SCNET_OPENAPI_ACCESS_KEY")
                or self.explicit_credentials.get("access_key")
                or (stored or {}).get("access_key")
            ),
            "secret_key": (
                self.env("SCNET_OPENAPI_SECRET_KEY")
                or self.explicit_credentials.get("secret_key")
                or (stored or {}).get("secret_key")
            ),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise OpenAPIError(
                "OpenAPI credentials are not configured; run "
                "`scnet-ocrdrop setup new` or set SCNET_OPENAPI_USER, "
                "SCNET_OPENAPI_ACCESS_KEY, and SCNET_OPENAPI_SECRET_KEY"
            )
        return {key: str(value) for key, value in values.items()}

    def regions(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._regions_cache is not None and not refresh:
            return self._regions_cache
        direct_token = self.env("SCNET_OPENAPI_TOKEN")
        if direct_token:
            self._regions_cache = [
                {
                    "clusterId": self.env("SCNET_OPENAPI_REGION_ID", "") or "",
                    "clusterName": self.env(
                        "SCNET_OPENAPI_REGION_NAME", "configured"
                    ),
                    "token": direct_token,
                }
            ]
            return self._regions_cache
        credentials = self._credentials()
        timestamp = str(int(time.time()))
        signature = canonical_signature(
            credentials["access_key"],
            timestamp,
            credentials["user"],
            credentials["secret_key"],
        )
        auth_base = self.env(
            "SCNET_OPENAPI_AUTH_BASE", "https://api.scnet.cn"
        )
        data = self.request(
            "POST",
            str(auth_base).rstrip("/") + "/api/user/v3/tokens",
            headers={
                "user": credentials["user"],
                "accessKey": credentials["access_key"],
                "signature": signature,
                "timestamp": timestamp,
            },
        )
        if not isinstance(data, list):
            raise OpenAPIError("token endpoint returned an unexpected shape")
        self._regions_cache = [
            item for item in data if isinstance(item, dict)
        ]
        return self._regions_cache

    def select_region(self, requested: Optional[str]) -> Dict[str, Any]:
        usable = [
            item
            for item in self.regions()
            if str(item.get("clusterId", "")) != "0" and item.get("token")
        ]
        if requested:
            for item in usable:
                if str(item.get("clusterId")) == str(requested) or str(
                    item.get("clusterName")
                ) == str(requested):
                    return item
            raise OpenAPIError("OpenAPI region %r is not available" % requested)
        if len(usable) == 1:
            return usable[0]
        names = ", ".join(
            "%s(%s)" % (item.get("clusterName"), item.get("clusterId"))
            for item in usable
        )
        raise OpenAPIError(
            "multiple OpenAPI regions are available; configure one: " + names
        )

    @staticmethod
    def enabled_url(center: Mapping[str, Any], field: str) -> str:
        values = center.get(field)
        if not isinstance(values, list):
            raise OpenAPIError("center response has no %s" % field)
        for item in values:
            if not isinstance(item, dict):
                continue
            enabled = str(item.get("enable", "true")).lower() == "true"
            if enabled and item.get("url"):
                return str(item["url"])
        raise OpenAPIError("center response has no enabled URL in %s" % field)

    def center(
        self, requested: Optional[str]
    ) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
        region = self.select_region(requested)
        region_id = str(region.get("clusterId", ""))
        token = str(region["token"])
        if region_id not in self._center_cache:
            center_url = self.env(
                "SCNET_OPENAPI_CENTER_URL",
                "https://www.scnet.cn/ac/openapi/v2/center",
            )
            data = self.request("GET", str(center_url), token=token)
            if not isinstance(data, dict):
                raise OpenAPIError(
                    "center endpoint returned an unexpected shape"
                )
            self._center_cache[region_id] = data
        return self._center_cache[region_id], token, region

    def discover_context(self, region: str) -> Dict[str, Any]:
        center, token, selected = self.center(region)
        hpc_url = self.enabled_url(center, "hpcUrls")
        efile_url = self.enabled_url(center, "efileUrls")
        schedulers = self.request(
            "GET",
            service_endpoint(hpc_url, "hpc", "/openapi/v2/cluster"),
            token=token,
        )
        if not isinstance(schedulers, list):
            schedulers = []
        user_info = center.get("clusterUserInfo") or {}
        return {
            "region_id": str(selected.get("clusterId") or region),
            "region_name": selected.get("clusterName") or center.get("name"),
            "username": user_info.get("userName"),
            "home_path": user_info.get("homePath"),
            "hpc_url": hpc_url,
            "efile_url": efile_url,
            "token": token,
            "schedulers": [
                {
                    "id": str(item.get("id", "")),
                    "name": item.get("text"),
                    "type": item.get("JobManagerType"),
                }
                for item in schedulers
                if isinstance(item, dict)
            ],
        }

    def discover_contexts(self) -> List[Dict[str, Any]]:
        contexts = []
        for region in self.regions():
            region_id = str(region.get("clusterId", ""))
            if region_id == "0" or not region.get("token"):
                continue
            try:
                contexts.append(self.discover_context(region_id))
            except OpenAPIError:
                continue
        return contexts

    def resolve_context(
        self, region: Optional[str], scheduler_id: Optional[str] = None
    ) -> Dict[str, Any]:
        cache_key = (str(region or ""), str(scheduler_id or ""))
        if cache_key in self._context_cache:
            return self._context_cache[cache_key]
        selected = self.select_region(region)
        selected_id = str(selected.get("clusterId", ""))
        try:
            context = self.discover_context(selected_id)
        except OpenAPIError as exc:
            raise OpenAPIError(
                "selected region does not expose HPC and file services"
            ) from exc
        schedulers = context.get("schedulers") or []
        requested = scheduler_id or self.env("SCNET_OPENAPI_SCHEDULER_ID")
        scheduler = None
        if requested:
            scheduler = next(
                (
                    item
                    for item in schedulers
                    if str(item.get("id")) == str(requested)
                ),
                None,
            )
            if scheduler is None:
                raise OpenAPIError(
                    "scheduler %r is not available" % requested
                )
        elif len(schedulers) == 1:
            scheduler = schedulers[0]
        else:
            available = ", ".join(
                "%s(%s)" % (item.get("name"), item.get("id"))
                for item in schedulers
            )
            raise OpenAPIError(
                "multiple schedulers are available; configure one: "
                + available
            )
        if not context.get("home_path"):
            raise OpenAPIError("OpenAPI did not report a home path")
        if not context.get("username"):
            raise OpenAPIError("OpenAPI did not report a region username")
        result = dict(context)
        result["scheduler_id"] = str(scheduler["id"])
        result.pop("schedulers", None)
        self._context_cache[cache_key] = result
        return result

    def mkdir(self, context: Mapping[str, Any], path: str) -> None:
        path = _safe_absolute(path, "directory path")
        url = (
            service_endpoint(
                str(context["efile_url"]),
                "efile",
                "/openapi/v2/file/mkdir",
            )
            + "?"
            + urlencode({"path": path, "createParents": "true"})
        )
        self.request(
            "POST",
            url,
            token=str(context["token"]),
            json_body={},
        )

    def list_files(
        self, context: Mapping[str, Any], path: str
    ) -> List[Dict[str, Any]]:
        path = _safe_absolute(path, "directory path")
        result = []
        start = 0
        limit = 200
        while True:
            params = {
                "path": path,
                "limit": limit,
                "start": start,
                "order": "asc",
                "orderBy": "name",
            }
            data = self.request(
                "GET",
                service_endpoint(
                    str(context["efile_url"]),
                    "efile",
                    "/openapi/v2/file/list",
                )
                + "?"
                + urlencode(params),
                token=str(context["token"]),
            )
            if not isinstance(data, dict):
                raise OpenAPIError("file list returned an unexpected shape")
            page = [
                item
                for item in (data.get("fileList") or [])
                if isinstance(item, dict)
            ]
            result.extend(page)
            total = int(data.get("total") or len(result))
            if not page or len(result) >= total:
                return result
            start += len(page)

    def _multipart(
        self,
        url: str,
        token: str,
        fields: Mapping[str, Any],
        file_name: str,
        content: bytes,
        content_type: str,
    ) -> Any:
        boundary = "----scnet-ocrdrop-" + uuid.uuid4().hex
        parts = []
        for name, value in fields.items():
            parts.append(
                (
                    "--%s\r\n"
                    'Content-Disposition: form-data; name="%s"\r\n\r\n'
                    "%s\r\n" % (boundary, name, value)
                ).encode("utf-8")
            )
        safe_name = (
            file_name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        )
        parts.append(
            (
                "--%s\r\n"
                'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
                "Content-Type: %s\r\n\r\n"
                % (boundary, safe_name, content_type)
            ).encode("utf-8")
            + content
            + b"\r\n"
        )
        parts.append(("--%s--\r\n" % boundary).encode("ascii"))
        request = Request(
            url,
            data=b"".join(parts),
            method="POST",
            headers={
                "token": token,
                "Content-Type": "multipart/form-data; boundary=%s" % boundary,
                "Accept": "application/json",
                "User-Agent": "scnet-ocrdrop/%s" % __version__,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            raise OpenAPIError(
                "OpenAPI multipart upload failed: %s" % exc
            ) from exc
        if not isinstance(payload, dict) or str(payload.get("code", "")) != "0":
            raise OpenAPIError(
                "SCNet OpenAPI error %s: %s"
                % (payload.get("code"), payload.get("msg"))
            )
        return payload.get("data")

    def upload_file(
        self,
        context: Mapping[str, Any],
        local_path: Path,
        remote_dir: str,
        *,
        cover: bool = False,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> None:
        local_path = Path(local_path)
        if not local_path.is_file():
            raise OpenAPIError("local file does not exist")
        remote_dir = _safe_absolute(remote_dir, "remote upload directory")
        content_type = (
            mimetypes.guess_type(local_path.name)[0]
            or "application/octet-stream"
        )
        size = local_path.stat().st_size
        if chunk_size > 0 and size > chunk_size:
            total_chunks = max(1, (size + chunk_size - 1) // chunk_size)
            identifier = "%s-%s-%s" % (size, local_path.name, uuid.uuid4().hex)
            endpoint = service_endpoint(
                str(context["efile_url"]),
                "efile",
                "/openapi/v2/file/burst",
            )
            with local_path.open("rb") as stream:
                for chunk_number in range(1, total_chunks + 1):
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    self._multipart(
                        endpoint,
                        str(context["token"]),
                        {
                            "chunkNumber": chunk_number,
                            "cover": "cover" if cover else "uncover",
                            "filename": local_path.name,
                            "identifier": identifier,
                            "path": remote_dir,
                            "relativePath": local_path.name,
                            "totalChunks": total_chunks,
                            "totalSize": size,
                            "chunkSize": chunk_size,
                            "currentChunkSize": len(chunk),
                        },
                        local_path.name,
                        chunk,
                        content_type,
                    )
            self.request(
                "POST",
                service_endpoint(
                    str(context["efile_url"]),
                    "efile",
                    "/openapi/v2/file/merge",
                ),
                token=str(context["token"]),
                form={
                    "cover": "cover" if cover else "uncover",
                    "filename": local_path.name,
                    "id": "",
                    "identifier": identifier,
                    "path": remote_dir,
                    "relativePath": local_path.name,
                },
            )
            return
        self._multipart(
            service_endpoint(
                str(context["efile_url"]),
                "efile",
                "/openapi/v2/file/upload",
            ),
            str(context["token"]),
            {
                "cover": "cover" if cover else "uncover",
                "path": remote_dir,
            },
            local_path.name,
            local_path.read_bytes(),
            content_type,
        )

    def download_bytes(
        self, context: Mapping[str, Any], remote_path: str
    ) -> bytes:
        remote_path = _safe_absolute(remote_path, "remote download path")
        url = (
            service_endpoint(
                str(context["efile_url"]),
                "efile",
                "/openapi/v2/file/download",
            )
            + "?"
            + urlencode({"path": remote_path})
        )
        request = Request(
            url,
            method="GET",
            headers={
                "token": str(context["token"]),
                "User-Agent": "scnet-ocrdrop/%s" % __version__,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                content = response.read()
        except (HTTPError, URLError, OSError) as exc:
            raise OpenAPIError("OpenAPI download failed: %s" % exc) from exc
        if "application/json" in content_type:
            try:
                payload = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and str(payload.get("code", "0")) != "0":
                raise OpenAPIError(
                    "SCNet OpenAPI error %s: %s"
                    % (payload.get("code"), payload.get("msg"))
                )
        return content

    def read_json(
        self, context: Mapping[str, Any], remote_path: str
    ) -> Dict[str, Any]:
        try:
            value = json.loads(
                self.download_bytes(context, remote_path).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenAPIError("remote file is not valid JSON") from exc
        if not isinstance(value, dict):
            raise OpenAPIError("remote JSON file must contain an object")
        return value

    def download_file(
        self,
        context: Mapping[str, Any],
        remote_path: str,
        local_path: Path,
        *,
        cover: bool = False,
    ) -> None:
        local_path = Path(local_path)
        if local_path.exists() and not cover:
            raise OpenAPIError("local output already exists")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self.download_bytes(context, remote_path))

    def download_tree(
        self,
        context: Mapping[str, Any],
        remote_root: str,
        local_root: Path,
    ) -> None:
        local_root = Path(local_root)
        local_root.mkdir(parents=True, exist_ok=True)
        for item in self.list_files(context, remote_root):
            name = str(item.get("name") or "")
            if not name or name in (".", ".."):
                continue
            remote_path = str(
                item.get("path") or join_remote_path(remote_root, name)
            )
            destination = local_root / name
            if is_directory_entry(item):
                self.download_tree(context, remote_path, destination)
            else:
                self.download_file(
                    context, remote_path, destination, cover=True
                )

    def submit_job(
        self,
        context: Mapping[str, Any],
        *,
        name: str,
        command: str,
        work_dir: str,
        queue: str,
        cpus: int = 1,
        memory: str = "1gb",
        walltime: str = "00:10:00",
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> str:
        work_dir = _safe_absolute(work_dir, "OpenAPI work directory")
        if any(char in name + queue for char in "\r\n"):
            raise OpenAPIError("job name and queue must not contain newlines")
        if not WALLTIME_RE.fullmatch(walltime):
            raise OpenAPIError("walltime must use HH:MM:SS or D-HH:MM:SS")
        body = {
            "strJobManagerID": str(context["scheduler_id"]),
            "mapAppJobInfo": {
                "GAP_CMD_FILE": command,
                "GAP_NNODE": "1",
                "GAP_NODE_STRING": "",
                "GAP_SUBMIT_TYPE": "cmd",
                "GAP_JOB_NAME": name,
                "GAP_WORK_DIR": work_dir,
                "GAP_QUEUE": queue,
                "GAP_NPROC": str(max(1, int(cpus))),
                "GAP_PPN": "",
                "GAP_NGPU": "",
                "GAP_NDCU": "",
                "GAP_JOB_MEM": memory,
                "GAP_WALL_TIME": walltime,
                "GAP_EXCLUSIVE": "",
                "GAP_APPNAME": "BASE",
                "GAP_MULTI_SUB": "",
                "GAP_STD_OUT_FILE": stdout
                or work_dir.rstrip("/") + "/std.out.%j",
                "GAP_STD_ERR_FILE": stderr
                or work_dir.rstrip("/") + "/std.err.%j",
                "GAP_SCHEDULER_OPT_WEB": "",
                "GAP_CLUSTER_ID": str(context["region_id"]),
            },
        }
        data = self.request(
            "POST",
            service_endpoint(
                str(context["hpc_url"]),
                "hpc",
                "/openapi/v2/apptemplates/BASIC/BASE/job",
            ),
            token=str(context["token"]),
            json_body=body,
        )
        return str(data)

    def job(
        self, context: Mapping[str, Any], job_id: str
    ) -> Dict[str, Any]:
        data = self.request(
            "GET",
            service_endpoint(
                str(context["hpc_url"]),
                "hpc",
                "/openapi/v2/jobs/%s" % quote(str(job_id)),
            ),
            token=str(context["token"]),
        )
        if not isinstance(data, dict):
            raise OpenAPIError("job endpoint returned an unexpected shape")
        raw_state = data.get("jobStatus") or data.get("state") or ""
        return {
            "job_id": str(data.get("jobId") or job_id),
            "state": STATUS_MAP.get(str(raw_state), str(raw_state)),
            "raw_state": str(raw_state),
            "exit_code": data.get("exitCode"),
            "reason": data.get("reason"),
            "stdout": data.get("outputPath"),
            "stderr": data.get("errorPath"),
        }

    def wait_job(
        self,
        context: Mapping[str, Any],
        job_id: str,
        *,
        timeout: int = 600,
        poll_interval: int = 3,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        while True:
            result = self.job(context, job_id)
            if result["state"] in TERMINAL_STATES:
                return result
            if timeout and time.monotonic() - started >= timeout:
                raise OpenAPIError("timed out waiting for control job")
            time.sleep(max(1, poll_interval))
