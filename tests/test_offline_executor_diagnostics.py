from __future__ import annotations

import io
import json
import unittest
from unittest.mock import Mock, patch

from adapters.obsidian.component_bridge import PUBLIC_EXECUTOR_PROTOCOL
from adapters.obsidian.offline_executor_host import (
    OfflineExecutorHostError,
    _run_docker,
)
from adapters.obsidian.public_discovery_executor import _safe_diagnostic_code
from tests.test_public_boundary import _components, _task


class OfflineExecutorDiagnosticsTests(unittest.TestCase):
    def diagnostic(self, payload: bytes, return_code: int = 2) -> str:
        output = io.BytesIO()
        child = Mock()
        child.stdin = io.BytesIO()
        child.poll.return_value = return_code

        def finish(timeout: int) -> int:
            self.assertEqual(timeout, 120)
            output.write(payload)
            return return_code

        child.wait.side_effect = finish
        task = _task({"artifactDigest": "ab" * 32, "profileDigest": "cd" * 32})
        with (
            patch(
                "adapters.obsidian.offline_executor_host.subprocess.Popen",
                return_value=child,
            ),
            patch("adapters.obsidian.offline_executor_host.subprocess.run") as cleanup,
            self.assertRaises(OfflineExecutorHostError) as raised,
        ):
            _run_docker(
                ("docker", "run", "--name", "synthetic"), _components(), output, task
            )
        cleanup.assert_called_once()
        code = str(raised.exception)
        self.assertEqual(_safe_diagnostic_code(code), code)
        return code

    def test_trusted_structured_rejection_survives_workflow_sanitization(self) -> None:
        payload = json.dumps(
            {
                "protocol": PUBLIC_EXECUTOR_PROTOCOL,
                "error": {"code": "plugin_manifest_id_invalid"},
            }
        ).encode()
        self.assertEqual(
            self.diagnostic(payload),
            "offline_executor_adapter_rejected_plugin_manifest_id_invalid",
        )

    def test_unstructured_failures_expose_only_exit_or_signal(self) -> None:
        for status, suffix in ((125, "exit_125"), (137, "exit_137"), (-9, "signal_9")):
            with self.subTest(status=status):
                self.assertEqual(
                    self.diagnostic(b"private source or Docker stderr", status),
                    "offline_executor_adapter_failed_" + suffix,
                )

    def test_malformed_or_untrusted_output_never_enters_diagnostics(self) -> None:
        examples = [
            b"not json",
            b"x" * 1025,
            b'{"error":{},"error":{},"protocol":"x"}',
        ]
        for code in ("private\nsource", "x" * 95, {"secret": "private"}):
            examples.append(
                json.dumps(
                    {
                        "protocol": PUBLIC_EXECUTOR_PROTOCOL,
                        "error": {"code": code},
                    }
                ).encode()
            )
        examples.append(
            json.dumps(
                {
                    "protocol": "wrong",
                    "error": {"code": "plugin_manifest_id_invalid"},
                }
            ).encode()
        )
        for payload in examples:
            with self.subTest(payload_size=len(payload)):
                self.assertEqual(
                    self.diagnostic(payload), "offline_executor_adapter_failed_exit_2"
                )
