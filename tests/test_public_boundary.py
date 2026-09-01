from __future__ import annotations

import ast
import base64
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

from adapters.obsidian.build_public_executor import build
from adapters.obsidian.component_bridge import (
    ObsidianComponentBridgeError,
    PUBLIC_EXECUTOR_PROTOCOL,
    _canonical_json,
    handle_public_request,
)
from adapters.obsidian.offline_executor_host import (
    OfflineExecutorComponent,
    OfflineExecutorHost,
    OfflineExecutorHostError,
    OfflineExecutorTask,
)

ROOT = Path(__file__).parents[1]
TARGET_DIGEST = "91" * 32
MANIFEST = json.dumps(
    {
        "id": "example-plugin",
        "name": "Example Plugin",
        "version": "1.0.0",
        "description": "Explore your notes.",
    }
).encode()
MAIN = b'new Notice("Adapter ready");'


def _components() -> tuple[OfflineExecutorComponent, ...]:
    return (
        OfflineExecutorComponent("manifest", "manifest.json", MANIFEST),
        OfflineExecutorComponent("main", "main.js", MAIN),
    )


def _request(components: tuple[OfflineExecutorComponent, ...]) -> bytes:
    return json.dumps(
        {
            "components": [
                {
                    "content_base64": base64.b64encode(component.content).decode("ascii"),
                    "name": component.name,
                    "role": component.role,
                }
                for component in components
            ],
            "materialization_target_digest": TARGET_DIGEST,
            "policy_revision": 7,
            "protocol": PUBLIC_EXECUTOR_PROTOCOL,
        }
    ).encode()


def _task(receipt: dict[str, object]) -> OfflineExecutorTask:
    return OfflineExecutorTask(
        adapter_artifact_digest=str(receipt["artifactDigest"]),
        adapter_profile_digest=str(receipt["profileDigest"]),
        expected_manifest_digest=sha256(MANIFEST).hexdigest(),
        expected_manifest_size=len(MANIFEST),
        expected_main_digest=sha256(MAIN).hexdigest(),
        expected_main_size=len(MAIN),
        materialization_target_digest=TARGET_DIGEST,
        policy_revision=7,
        result_max_bytes=8 * 1024 * 1024,
    )


class PublicBoundaryTests(unittest.TestCase):
    def test_release_source_path_contains_no_zip_compatibility_plane(self) -> None:
        adapter_root = ROOT / "adapters" / "obsidian"
        self.assertFalse((adapter_root / "zip_bridge.py").exists())
        self.assertFalse((adapter_root / "public_zip_closure.py").exists())
        profile = json.loads(
            (adapter_root / "public-executor-profile.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            profile["entryModule"], "adapters.obsidian.component_bridge"
        )
        self.assertNotIn(
            "zip_base64",
            (adapter_root / "public_discovery_executor.py").read_text(encoding="utf-8"),
        )

    def test_pyz_build_is_repeatable_and_executes_without_component_output(self) -> None:
        temporary = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        first = temporary / "first.pyz"
        second = temporary / "second.pyz"
        first_receipt = build(first)
        second_receipt = build(second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first_receipt, second_receipt)

        components = _components()
        completed = subprocess.run(
            [sys.executable, "-I", str(first)],
            input=_request(components),
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
        self.assertNotIn(MAIN, first.read_bytes())
        self.assertNotIn(base64.b64encode(MAIN), completed.stdout)

    def test_component_closure_rejects_missing_duplicate_and_legacy_zip_request(self) -> None:
        manifest_only = _request((_components()[0],))
        with self.assertRaisesRegex(
            ObsidianComponentBridgeError, "component_closure_invalid"
        ):
            handle_public_request(manifest_only)

        duplicate = _request((_components()[0], _components()[0]))
        with self.assertRaisesRegex(
            ObsidianComponentBridgeError, "component_closure_invalid"
        ):
            handle_public_request(duplicate)

        legacy = json.dumps(
            {
                "materialization_target_digest": TARGET_DIGEST,
                "policy_revision": 7,
                "protocol": "trans-hub.obsidian-public-executor.v1",
                "zip_base64": base64.b64encode(b"legacy zip").decode("ascii"),
            }
        ).encode()
        with self.assertRaisesRegex(
            ObsidianComponentBridgeError, "request_invalid"
        ):
            handle_public_request(legacy)

    def test_host_never_persists_components_and_concurrency_is_isolated(self) -> None:
        temporary = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        artifact = temporary / "executor.pyz"
        receipt = build(artifact)
        components = _components()
        observed_roots: list[Path] = []
        observed_names: list[str] = []

        def runner(
            command: tuple[str, ...],
            received: tuple[OfflineExecutorComponent, ...],
            output: object,
            _task_value: OfflineExecutorTask,
        ) -> None:
            mount = command[command.index("--mount") + 1]
            source = Path(mount.split("source=", 1)[1].split(",", 1)[0])
            observed_roots.append(source.parent)
            observed_names.append(command[command.index("--name") + 1])
            self.assertEqual(received, components)
            self.assertFalse((source.parent / "source.raw").exists())
            output.write(handle_public_request(_request(received)))  # type: ignore[attr-defined]

        host = OfflineExecutorHost(executor_artifact=artifact, runner=runner)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _index: host.prepare_result(components, _task(receipt)),
                    range(2),
                )
            )
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(set(observed_roots)), 2)
        self.assertEqual(len(set(observed_names)), 2)
        self.assertTrue(all(not root.exists() for root in observed_roots))

        failed_roots: list[Path] = []

        def fail_runner(
            command: tuple[str, ...],
            _received: tuple[OfflineExecutorComponent, ...],
            _output: object,
            _task_value: OfflineExecutorTask,
        ) -> None:
            mount = command[command.index("--mount") + 1]
            source = Path(mount.split("source=", 1)[1].split(",", 1)[0])
            failed_roots.append(source.parent)
            raise OfflineExecutorHostError("offline_executor_timeout")

        with self.assertRaisesRegex(OfflineExecutorHostError, "timeout"):
            OfflineExecutorHost(
                executor_artifact=artifact, runner=fail_runner
            ).prepare_result(components, _task(receipt))
        self.assertTrue(failed_roots)
        self.assertTrue(all(not root.exists() for root in failed_roots))

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
