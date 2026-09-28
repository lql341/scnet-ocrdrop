import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC_ROOT = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))

MODULE_PATH = Path(__file__).parents[1] / "src" / "ocrdrop" / "remote.py"
SPEC = importlib.util.spec_from_file_location("mineru_drop", MODULE_PATH)
mineru_drop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mineru_drop)

CLIENT_MODULE_PATH = Path(__file__).parents[1] / "src" / "ocrdrop" / "client.py"
CLIENT_SPEC = importlib.util.spec_from_file_location("mineru_drop_client", CLIENT_MODULE_PATH)
mineru_drop_client = importlib.util.module_from_spec(CLIENT_SPEC)
CLIENT_SPEC.loader.exec_module(mineru_drop_client)

from ocrdrop.backends import supported_backends
from ocrdrop.backends.paddleocr import record_to_lines
from ocrdrop.config import config_path, load_user_config, save_user_config
from ocrdrop.credentials import environment_credentials
from ocrdrop.openapi import (
    SCNetOpenAPI,
    OpenAPIError,
    canonical_signature,
    redact_error_detail,
    resolve_home_relative,
)
from ocrdrop import setup_cli
from ocrdrop.transports import OpenAPITransport, _json_from_mixed_output


class OCRDropTest(unittest.TestCase):
    def test_plan_ranges_keeps_short_document_whole(self):
        self.assertEqual(mineru_drop.plan_ranges(80, 64, 128), [(0, 79)])

    def test_plan_ranges_splits_long_document(self):
        self.assertEqual(
            mineru_drop.plan_ranges(205, 96, 128),
            [(0, 95), (96, 191), (192, 204)],
        )

    def test_shift_page_indices(self):
        value = {
            "page_idx": 0,
            "nested": [{"page_num": 2}, {"text": "unchanged"}],
        }
        shifted = mineru_drop.shift_page_indices(value, 96)
        self.assertEqual(shifted["page_idx"], 96)
        self.assertEqual(shifted["nested"][0]["page_num"], 98)

    def test_copy_images_rewrites_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto = root / "auto"
            images = auto / "images"
            destination = root / "merged" / "images"
            images.mkdir(parents=True)
            (images / "x.jpg").write_bytes(b"image")
            markdown = "![](images/x.jpg)"
            result = mineru_drop.copy_images_and_rewrite(
                markdown,
                auto,
                destination,
                "p000001-000010",
            )
            self.assertIn("images/p000001-000010-x.jpg", result)
            self.assertTrue((destination / "p000001-000010-x.jpg").is_file())

    def test_completed_merge_status_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = root / "batches" / "batch-1"
            for state in mineru_drop.TASK_STATES:
                (batch / "queue" / state).mkdir(parents=True, exist_ok=True)
            (batch / "queue" / "done" / "task.json").write_text("{}")
            mineru_drop.atomic_write_json(
                batch / "manifest.json",
                {
                    "schema": mineru_drop.SCHEMA,
                    "batch_id": "batch-1",
                    "task_count": 1,
                    "status": "complete",
                    "merged_at": "2026-09-27T00:00:00+00:00",
                },
            )
            cfg = {"root": str(root)}
            result = mineru_drop.update_batch_status(cfg, "batch-1")
            self.assertEqual(result["status"], "complete")

    def test_default_backend_is_mineru3(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(root / "runtime"),
                        "partition_dcu": "dcu",
                        "partition_cpu": "cpu",
                        "venv": str(root / "venv"),
                        "python_launcher": str(root / "launcher"),
                        "model_config": str(root / "mineru.json"),
                        "module_loads": ["gcc", "dtk"],
                    }
                )
            )
            cfg = mineru_drop.load_config(config_path)
            self.assertEqual(cfg["backend"], "mineru3")
            self.assertEqual(cfg["adapter"], "mineru3")

    def test_mineru4_defaults_can_be_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(root / "runtime"),
                        "partition_dcu": "dcu",
                        "partition_cpu": "cpu",
                        "venv": str(root / "venv"),
                        "python_launcher": str(root / "launcher"),
                        "model_config": str(root / "mineru.yaml"),
                        "module_loads": ["gcc", "dtk"],
                        "adapter": "mineru4",
                        "tier": "basic",
                        "small_backend": "torch",
                    }
                )
            )
            cfg = mineru_drop.load_config(config_path)
            self.assertEqual(cfg["backend"], "mineru4")
            self.assertEqual(cfg["adapter"], "mineru4")
            self.assertEqual(cfg["tier"], "basic")

    def test_mineru4_source_page_ids_are_not_shifted(self):
        pages = [{"page_idx": 8}, {"page_idx": 9}]
        # MinerU 4 page-range parsing already reports source page IDs.
        merged = list(pages)
        self.assertEqual([page["page_idx"] for page in merged], [8, 9])

    def test_paddleocr_backend_is_registered(self):
        self.assertIn("paddleocr", supported_backends())

    def test_paddleocr_record_is_normalized(self):
        lines = record_to_lines({
            "rec_texts": ["  first   line ", "", "second"],
            "rec_scores": [0.91, 0.1, 0.82],
            "rec_polys": [
                [[0, 0], [10, 0], [10, 5], [0, 5]],
                [[0, 0], [0, 0], [0, 0], [0, 0]],
                [[2, 2], [8, 2], [8, 6], [2, 6]],
            ],
        })
        self.assertEqual([line["text"] for line in lines], ["first line", "second"])
        self.assertEqual(lines[0]["score"], 0.91)
        self.assertEqual(lines[1]["polygon"][0], [2, 2])

    def test_paddleocr_config_does_not_require_mineru_model_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(root / "runtime"),
                        "partition_dcu": "dcu",
                        "partition_cpu": "cpu",
                        "venv": str(root / "paddleocr"),
                        "python_launcher": str(root / "launcher"),
                        "module_loads": ["gcc", "dtk"],
                        "backend": "paddleocr",
                        "activate_venv": False,
                        "paddle_cache_home": str(root / "cache"),
                    }
                )
            )
            cfg = mineru_drop.load_config(config_path)
            self.assertEqual(cfg["backend"], "paddleocr")
            self.assertFalse(cfg["activate_venv"])
            self.assertFalse(cfg["trust_remote_code"])
            script = mineru_drop.render_worker_slurm(
                cfg,
                {"batch_id": "batch-1", "workers": 1},
            )
            self.assertIn("PADDLE_PDX_CACHE_HOME", script)
            self.assertNotIn("/bin/activate", script)

    def test_inventory_identifies_redeployable_and_runtime_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            venv = root / "venv"
            app.mkdir()
            venv.mkdir()
            config = root / "config.json"
            config.write_text("{}")
            cfg = {
                "root": str(root),
                "_config_path": str(config),
                "backend": "mineru3",
                "python_launcher": str(app / "dcu-python"),
                "venv": str(venv),
                "model_config": str(root / "mineru.json"),
            }
            output = io.StringIO()
            with redirect_stdout(output):
                result = mineru_drop.inventory_main(cfg)
            payload = json.loads(output.getvalue())
            policies = {item["policy"] for item in payload["items"]}
            self.assertEqual(result, 1)
            self.assertIn("redeploy-from-git", policies)
            self.assertIn("preserve-runtime", policies)
            self.assertIn("retain-with-batch", policies)

    def test_atomic_json_handles_surrogateescaped_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            filename = "document-\udce8\udcaf\udc89.pdf"
            mineru_drop.atomic_write_json(path, {"source_name": filename})
            loaded = mineru_drop.read_json(path)
            self.assertEqual(loaded["source_name"], filename)

    def test_client_command_failure_is_concise(self):
        with self.assertRaises(SystemExit) as caught:
            mineru_drop_client.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('detail line', file=sys.stderr); sys.exit(7)",
                ],
                capture=True,
            )
        self.assertEqual(
            str(caught.exception),
            "command failed (exit 7): detail line",
        )

    def test_user_config_is_private_and_rejects_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                "os.environ",
                {"OCRDROP_CONFIG_HOME": tmp},
                clear=False,
            ):
                path = save_user_config(
                    {
                        "transport": "openapi",
                        "default_ocr_backend": "mineru3",
                        "remote_roots": {
                            "mineru3": "softwares/projects/scnet-ocrdrop"
                        },
                        "openapi": {
                            "default_region_id": "11250",
                            "credential_provider": "macOS Keychain",
                        },
                    }
                )
                self.assertEqual(path, config_path())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(
                    load_user_config()["openapi"]["default_region_id"],
                    "11250",
                )
                with self.assertRaises(ValueError):
                    save_user_config(
                        {
                            "transport": "openapi",
                            "default_ocr_backend": "mineru3",
                            "remote_roots": {
                                "mineru3": "softwares/projects/scnet-ocrdrop"
                            },
                            "secret_key": "must-not-be-written",
                        }
                    )
                with self.assertRaises(ValueError):
                    save_user_config(
                        {
                            "transport": "openapi",
                            "default_ocr_backend": "mineru3",
                            "remote_roots": {
                                "mineru3": "softwares/projects/scnet-ocrdrop"
                            },
                            "openapi": {
                                "home_path": "/public/home/EXAMPLE_USER",
                            },
                        }
                    )

    def test_environment_credentials_require_complete_set(self):
        with patch.dict(
            "os.environ",
            {
                "SCNET_OPENAPI_USER": "alice",
                "SCNET_OPENAPI_ACCESS_KEY": "ak",
                "SCNET_OPENAPI_SECRET_KEY": "sk",
            },
            clear=True,
        ):
            self.assertEqual(
                environment_credentials(),
                {
                    "user": "alice",
                    "access_key": "ak",
                    "secret_key": "sk",
                },
            )
        with patch.dict(
            "os.environ",
            {
                "SCNET_OPENAPI_USER": "alice",
                "SCNET_OPENAPI_ACCESS_KEY": "ak",
            },
            clear=True,
        ):
            self.assertIsNone(environment_credentials())

    def test_openapi_signature_is_deterministic(self):
        self.assertEqual(
            canonical_signature("ak", "1", "user", "sk"),
            "ba7ca90c1ccf373e0632370dedc06b2fc18c4990c0635bc1b6e8cf8b16ef806c",
        )

    def test_openapi_error_detail_redacts_credentials(self):
        detail = (
            '{"accessKey":"real-ak","secretKey":"real-sk",'
            '"token":"real-token","signature":"real-signature"} '
            "https://user:"
            "password"
            "@example.invalid/path"
        )
        redacted = redact_error_detail(detail)
        for secret in (
            "real-ak",
            "real-sk",
            "real-token",
            "real-signature",
            "password",
        ):
            self.assertNotIn(secret, redacted)

    def test_openapi_remote_root_stays_inside_home(self):
        self.assertEqual(
            resolve_home_relative(
                "/public/home/EXAMPLE_USER",
                "softwares/projects/scnet-ocrdrop",
            ),
            "/public/home/EXAMPLE_USER/softwares/projects/scnet-ocrdrop",
        )
        with self.assertRaises(OpenAPIError):
            resolve_home_relative(
                "/public/home/EXAMPLE_USER",
                "/public/home/OTHER_USER/scnet-ocrdrop",
            )

    def test_client_output_redacts_paths_and_credentials(self):
        result = mineru_drop_client.sanitize_payload(
            {
                "source_path": "/public/home/EXAMPLE_USER/private.pdf",
                "access_key": "not-public",
                "credential_provider": "macOS Keychain",
            }
        )
        self.assertEqual(result["source_path"], "<redacted-path>")
        self.assertEqual(result["access_key"], "<redacted>")
        self.assertEqual(
            result["credential_provider"],
            "macOS Keychain",
        )

    def test_exported_manifests_drop_private_paths(self):
        document = {
            "source_path": "/public/home/EXAMPLE_USER/inbox/a.pdf",
            "source_name": "a.pdf",
            "sha256": "abc",
        }
        task = {
            "source_path": "/public/home/EXAMPLE_USER/inbox/a.pdf",
            "output_dir": "/public/home/EXAMPLE_USER/chunks/a",
            "traceback_path": "/public/home/EXAMPLE_USER/errors/a.txt",
            "task_id": "a",
            "worker": {"hostname": "node1", "slurm_job_id": "1"},
        }
        self.assertNotIn(
            "source_path",
            mineru_drop.public_document_record(document),
        )
        public_task = mineru_drop.public_task_record(task)
        self.assertNotIn("source_path", public_task)
        self.assertNotIn("output_dir", public_task)
        self.assertNotIn("hostname", public_task["worker"])

    def test_openapi_control_output_ignores_shell_banner(self):
        self.assertEqual(
            _json_from_mixed_output(
                "module initialized\n"
                '{\n'
                '  "batch_id": "batch-1",\n'
                '  "checks": {"sbatch": true, "pdfinfo": true}\n'
                '}\n'
                "scheduler epilogue\n"
            ),
            {
                "batch_id": "batch-1",
                "checks": {"sbatch": True, "pdfinfo": True},
            },
        )

    def test_openapi_status_reads_manifest_without_control_job(self):
        class FakeAPI:
            def read_json(self, context, path):
                return {
                    "batch_id": "batch-1",
                    "task_count": 1,
                    "merged_at": "2026-09-28T00:00:00+00:00",
                    "documents": [],
                    "slurm": {},
                }

            def list_files(self, context, path):
                if path.endswith("/done"):
                    return [{"name": "task.json", "isDirectory": False}]
                return []

        transport = OpenAPITransport.__new__(OpenAPITransport)
        transport.api = FakeAPI()
        transport.context = {}
        transport.remote_root = "/public/home/EXAMPLE_USER/ocrdrop"
        _, manifest = transport.status("batch-1")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["counts"]["done"], 1)

    def test_multi_region_number_parser(self):
        self.assertEqual(
            setup_cli._parse_multi_numbers("1,3,5-4", 5),
            {0, 2, 3, 4},
        )
        with self.assertRaises(ValueError):
            setup_cli._parse_multi_numbers("", 3)
        with self.assertRaises(ValueError):
            setup_cli._parse_multi_numbers("4", 3)

    def test_setup_modify_preserves_multi_region_selection(self):
        contexts = [
            {
                "region_id": "r1",
                "region_name": "Region One",
                "schedulers": [
                    {"id": "s1", "name": "Scheduler One", "type": "slurm"}
                ],
            },
            {
                "region_id": "r2",
                "region_name": "Region Two",
                "schedulers": [
                    {"id": "s2a", "name": "Scheduler A", "type": "slurm"},
                    {"id": "s2b", "name": "Scheduler B", "type": "slurm"},
                ],
            },
        ]
        current = {
            "transport": "openapi",
            "default_ocr_backend": "mineru3",
            "remote_roots": {
                "mineru3": "softwares/projects/scnet-ocrdrop/deployments/mineru3"
            },
            "openapi": {
                "enabled_region_ids": ["r1", "r2"],
                "default_region_id": "r2",
                "region_name": "Region Two",
                "scheduler_id": "s2b",
                "credential_provider": "test-store",
                "regions": {
                    "r1": {
                        "name": "Region One",
                        "scheduler_id": "s1",
                    },
                    "r2": {
                        "name": "Region Two",
                        "scheduler_id": "s2b",
                    },
                },
            },
        }
        saved = []

        class FakeAPI:
            def __init__(self, **kwargs):
                pass

            def discover_contexts(self):
                return contexts

        args = SimpleNamespace(
            transport="openapi",
            ocr_backend="mineru3",
            remote_root=None,
            ssh=None,
            api_timeout=30,
            setup_action="modify",
            region=None,
            scheduler_id=None,
            setup_enabled_regions=[],
            setup_default_region=None,
            setup_region_schedulers=[],
        )
        with patch.object(setup_cli, "load_user_config", return_value=current), patch.object(
            setup_cli,
            "load_openapi_credentials",
            return_value=(
                {"user": "u", "access_key": "a", "secret_key": "s"},
                "test-store",
            ),
        ), patch.object(setup_cli, "SCNetOpenAPI", FakeAPI), patch.object(
            setup_cli, "save_user_config", side_effect=saved.append
        ), patch.object(
            setup_cli.sys.stdin, "isatty", return_value=False
        ):
            self.assertEqual(setup_cli.configure(args), 0)

        openapi = saved[0]["openapi"]
        self.assertEqual(openapi["enabled_region_ids"], ["r1", "r2"])
        self.assertEqual(openapi["default_region_id"], "r2")
        self.assertEqual(openapi["regions"]["r1"]["scheduler_id"], "s1")
        self.assertEqual(openapi["regions"]["r2"]["scheduler_id"], "s2b")
        self.assertNotIn("home_path", json.dumps(openapi))
        self.assertNotIn("username", json.dumps(openapi))

    def test_setup_noninteractive_can_change_enabled_regions(self):
        contexts = [
            {
                "region_id": "r1",
                "region_name": "Region One",
                "schedulers": [
                    {"id": "s1", "name": "Scheduler One", "type": "slurm"}
                ],
            },
            {
                "region_id": "r2",
                "region_name": "Region Two",
                "schedulers": [
                    {"id": "s2a", "name": "Scheduler A", "type": "slurm"},
                    {"id": "s2b", "name": "Scheduler B", "type": "slurm"},
                ],
            },
        ]
        saved = []

        class FakeAPI:
            def __init__(self, **kwargs):
                pass

            def discover_contexts(self):
                return contexts

        args = SimpleNamespace(
            transport="openapi",
            ocr_backend="mineru3",
            remote_root=None,
            ssh=None,
            api_timeout=30,
            setup_action="modify",
            region=None,
            scheduler_id=None,
            setup_enabled_regions=["r1,r2"],
            setup_default_region="r2",
            setup_region_schedulers=["r2=s2b"],
        )
        with patch.object(
            setup_cli,
            "load_user_config",
            return_value={
                "transport": "openapi",
                "default_ocr_backend": "mineru3",
                "remote_roots": {},
            },
        ), patch.object(
            setup_cli,
            "load_openapi_credentials",
            return_value=(
                {"user": "u", "access_key": "a", "secret_key": "s"},
                "test-store",
            ),
        ), patch.object(setup_cli, "SCNetOpenAPI", FakeAPI), patch.object(
            setup_cli, "save_user_config", side_effect=saved.append
        ), patch.object(
            setup_cli.sys.stdin, "isatty", return_value=False
        ):
            self.assertEqual(setup_cli.configure(args), 0)

        openapi = saved[0]["openapi"]
        self.assertEqual(openapi["default_region_id"], "r2")
        self.assertEqual(openapi["scheduler_id"], "s2b")
        self.assertEqual(openapi["regions"]["r2"]["scheduler_id"], "s2b")

    def test_client_uses_scheduler_for_selected_region(self):
        args = SimpleNamespace(
            transport=None,
            ocr_backend=None,
            remote_root=None,
            ssh=None,
            region="Region Two",
            scheduler_id=None,
        )
        config = {
            "transport": "openapi",
            "default_ocr_backend": "mineru3",
            "remote_roots": {
                "mineru3": "softwares/projects/scnet-ocrdrop/deployments/mineru3"
            },
            "openapi": {
                "enabled_region_ids": ["r1", "r2"],
                "default_region_id": "r1",
                "scheduler_id": "s1",
                "regions": {
                    "r1": {
                        "name": "Region One",
                        "scheduler_id": "s1",
                    },
                    "r2": {
                        "name": "Region Two",
                        "scheduler_id": "s2",
                    },
                },
            },
        }
        with patch.object(
            mineru_drop_client, "load_user_config", return_value=config
        ), patch.dict("os.environ", {}, clear=True):
            mineru_drop_client.apply_user_config(args)
        self.assertEqual(args.region, "r2")
        self.assertEqual(args.scheduler_id, "s2")

    def test_client_rejects_region_not_enabled_by_setup(self):
        args = SimpleNamespace(
            transport=None,
            ocr_backend=None,
            remote_root=None,
            ssh=None,
            region="r3",
            scheduler_id=None,
        )
        config = {
            "transport": "openapi",
            "default_ocr_backend": "mineru3",
            "remote_roots": {
                "mineru3": "softwares/projects/scnet-ocrdrop/deployments/mineru3"
            },
            "openapi": {
                "enabled_region_ids": ["r1", "r2"],
                "default_region_id": "r1",
                "regions": {
                    "r1": {"name": "Region One", "scheduler_id": "s1"},
                    "r2": {"name": "Region Two", "scheduler_id": "s2"},
                },
            },
        }
        with patch.object(
            mineru_drop_client, "load_user_config", return_value=config
        ), patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit) as caught:
                mineru_drop_client.apply_user_config(args)
        self.assertIn("not enabled", str(caught.exception))

    def test_resolve_context_only_discovers_selected_region(self):
        api = SCNetOpenAPI(
            credentials={"user": "u", "access_key": "a", "secret_key": "s"}
        )
        context = {
            "region_id": "r2",
            "region_name": "Region Two",
            "username": "region-user",
            "home_path": "/public/home/EXAMPLE_USER",
            "hpc_url": "https://hpc.example.invalid",
            "efile_url": "https://efile.example.invalid",
            "token": "temporary",
            "schedulers": [
                {"id": "s2", "name": "Scheduler Two", "type": "slurm"}
            ],
        }
        with patch.object(
            api,
            "select_region",
            return_value={"clusterId": "r2", "token": "temporary"},
        ), patch.object(
            api, "discover_context", return_value=context
        ) as discover, patch.object(
            api,
            "discover_contexts",
            side_effect=AssertionError("must not scan all regions"),
        ):
            result = api.resolve_context("r2", "s2")
        discover.assert_called_once_with("r2")
        self.assertEqual(result["region_id"], "r2")
        self.assertEqual(result["scheduler_id"], "s2")


if __name__ == "__main__":
    unittest.main()
