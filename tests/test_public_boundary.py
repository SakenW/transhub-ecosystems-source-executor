from __future__ import annotations

import ast
import base64
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from adapters.obsidian.build_public_executor import build
from adapters.obsidian.offline_executor_host import (
    OfflineExecutorHost,
    OfflineExecutorHostError,
    OfflineExecutorTask,
)
from adapters.obsidian.public_zip_closure import (
    PublicZipClosureError,
    components_from_zip,
)
from adapters.obsidian.zip_bridge import (
    PUBLIC_EXECUTOR_PROTOCOL,
    _canonical_json,
    handle_public_request,
)

ROOT = Path(__file__).parents[1]
TARGET_DIGEST = "91" * 32


def _plugin_zip() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "plugin/manifest.json",
            json.dumps(
                {
                    "id": "example-plugin",
                    "name": "Example Plugin",
                    "version": "1.0.0",
                    "description": "Explore your notes.",
                }
            ),
        )
        archive.writestr("plugin/main.js", 'new Notice("Adapter ready");')
    return output.getvalue()


def _request(raw: bytes) -> bytes:
    return json.dumps(
        {
            "materialization_target_digest": TARGET_DIGEST,
            "policy_revision": 7,
            "protocol": PUBLIC_EXECUTOR_PROTOCOL,
            "zip_base64": base64.b64encode(raw).decode("ascii"),
        }
    ).encode()


def _task(raw: bytes, receipt: dict[str, object]) -> OfflineExecutorTask:
    return OfflineExecutorTask(
        adapter_artifact_digest=str(receipt["artifactDigest"]),
        adapter_profile_digest=str(receipt["profileDigest"]),
        expected_raw_digest=sha256(raw).hexdigest(),
        expected_raw_size=len(raw),
        materialization_target_digest=TARGET_DIGEST,
        policy_revision=7,
        result_max_bytes=8 * 1024 * 1024,
    )


class PublicBoundaryTests(unittest.TestCase):
    def test_pyz_build_is_repeatable_and_executes_without_raw_output(self) -> None:
        with self.subTest("build twice"):
            temporary = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
            first = temporary / "first.pyz"
            second = temporary / "second.pyz"
            first_receipt = build(first)
            second_receipt = build(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_receipt, second_receipt)

        raw = _plugin_zip()
        completed = subprocess.run(
            [sys.executable, "-I", str(first)],
            input=_request(raw),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        result = json.loads(completed.stdout)
        self.assertEqual(_canonical_json(result), completed.stdout)
        self.assertEqual(
            result["result"],
            {
                "materialization_target_digest": TARGET_DIGEST,
                "protocol": "trans-hub.public-discovery-result",
                "revision": 1,
            },
        )
        self.assertNotIn(raw, first.read_bytes())
        self.assertNotIn(base64.b64encode(raw), completed.stdout)

    def test_zip_closure_rejects_traversal_and_excessive_compression(self) -> None:
        traversal = BytesIO()
        with ZipFile(traversal, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("../manifest.json", b"{}")
            archive.writestr("../main.js", b"const value = 1;")
        with self.assertRaisesRegex(PublicZipClosureError, "zip_entry_invalid"):
            components_from_zip(traversal.getvalue())

        bomb = BytesIO()
        with ZipFile(bomb, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr("plugin/manifest.json", b"{}")
            archive.writestr("plugin/main.js", b"A" * (1024 * 1024))
        with self.assertRaisesRegex(PublicZipClosureError, "zip_entry_invalid"):
            components_from_zip(bomb.getvalue())

    def test_host_cleanup_and_concurrency_use_distinct_ephemeral_paths(self) -> None:
        temporary = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        artifact = temporary / "executor.pyz"
        receipt = build(artifact)
        raw = _plugin_zip()
        observed_paths: list[Path] = []
        observed_names: list[str] = []

        def runner(
            command: tuple[str, ...],
            raw_path: Path,
            output: object,
            _task_value: OfflineExecutorTask,
        ) -> None:
            observed_paths.append(raw_path)
            observed_names.append(command[command.index("--name") + 1])
            output.write(handle_public_request(_request(raw_path.read_bytes())))  # type: ignore[attr-defined]

        host = OfflineExecutorHost(executor_artifact=artifact, runner=runner)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _index: host.prepare_result([raw], _task(raw, receipt)),
                    range(2),
                )
            )
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(set(observed_paths)), 2)
        self.assertEqual(len(set(observed_names)), 2)
        self.assertTrue(all(not path.exists() for path in observed_paths))

        failed_paths: list[Path] = []

        def fail_runner(
            _command: tuple[str, ...],
            raw_path: Path,
            _output: object,
            _task_value: OfflineExecutorTask,
        ) -> None:
            failed_paths.append(raw_path)
            raise OfflineExecutorHostError("offline_executor_timeout")

        with self.assertRaisesRegex(OfflineExecutorHostError, "timeout"):
            OfflineExecutorHost(
                executor_artifact=artifact, runner=fail_runner
            ).prepare_result([raw], _task(raw, receipt))
        self.assertTrue(failed_paths)
        self.assertTrue(all(not path.exists() for path in failed_paths))

    def test_executor_modules_import_only_standard_library_and_local_package(self) -> None:
        for path in sorted((ROOT / "adapters" / "obsidian").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".", 1)[0]]
                else:
                    continue
                for root in roots:
                    self.assertIn(
                        root,
                        sys.stdlib_module_names | {"__future__"},
                        f"{path.name} imports non-stdlib module {root}",
                    )


if __name__ == "__main__":
    unittest.main()
