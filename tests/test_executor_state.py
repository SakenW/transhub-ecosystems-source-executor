from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from adapters.obsidian.offline_executor_host import OfflineExecutorHostError
from adapters.obsidian.public_discovery_executor import (
    Asset,
    Claim,
    ExecutorConfig,
    ExecutorError,
    SourcePlan,
    UploadGrant,
    execute_one,
)


def _claim() -> Claim:
    return Claim("11111111-1111-4111-8111-111111111111", "registry/item", "aa" * 32, 7)


def _plan() -> SourcePlan:
    return SourcePlan(
        1,
        "official-owner",
        2,
        "official-plugin",
        3,
        Asset(4, "plugin.zip", 3, "bb" * 32),
        5,
        "cc" * 32,
        "dd" * 32,
        "aa" * 32,
        "ee" * 32,
        "canonical-json-v1",
        "application/vnd.trans-hub.public-discovery-result+json",
        1024,
        "ff" * 32,
    )


def _grant() -> UploadGrant:
    return UploadGrant(
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "short-lived",
        "public-discovery/results/" + "12" * 32 + ".canonical-json",
        "https://upload.example.test",
        "application/vnd.trans-hub.public-discovery-result+json",
        2,
    )


class _Tokens:
    def __init__(self) -> None:
        self.count = 0

    def token(self) -> str:
        self.count += 1
        return f"oidc-{self.count}"


class _Source:
    def chunks(self, _plan_value: SourcePlan) -> tuple[bytes, ...]:
        return (b"raw",)


class _Host:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def prepare_result(self, chunks: object, _task: object) -> bytes:
        self.assert_chunks = b"".join(chunks)  # type: ignore[arg-type]
        if self.error:
            raise self.error
        return b"{}"


class _Uploader:
    def __init__(self) -> None:
        self.calls = 0

    def upload(self, _grant_value: UploadGrant, _result: bytes) -> None:
        self.calls += 1
        if self.calls == 1:
            raise ExecutorError("executor_result_upload_failed", retryable=True)


class _Control:
    def __init__(self) -> None:
        self.grant_commands: list[str] = []
        self.confirm_calls = 0
        self.status_calls = 0
        self.failures: list[str] = []

    def claim(self, _token: str) -> Claim:
        return _claim()

    def source_plan(self, _token: str, _claim_value: Claim) -> SourcePlan:
        return _plan()

    def grant(
        self,
        _token: str,
        _claim_value: Claim,
        _plan_value: SourcePlan,
        _result: bytes,
        command_id: str,
    ) -> UploadGrant:
        self.grant_commands.append(command_id)
        if len(self.grant_commands) == 1:
            raise ExecutorError("executor_control_request_failed", retryable=True)
        return _grant()

    def confirm(
        self, _token: str, _claim_value: Claim, _grant_value: UploadGrant
    ) -> None:
        self.confirm_calls += 1
        raise ExecutorError("executor_control_request_failed", retryable=True)

    def status(self, _token: str, _claim_value: Claim) -> str:
        self.status_calls += 1
        return "materialization_pending"

    def fail(
        self,
        _token: str,
        _claim_value: Claim,
        failure_code: str,
        _evidence_digest: str,
    ) -> None:
        self.failures.append(failure_code)


class ExecutorStateTests(unittest.TestCase):
    def _config(self, root: Path) -> ExecutorConfig:
        artifact = root / "executor.pyz"
        artifact.write_bytes(b"executor")
        return ExecutorConfig(
            "https://api.example.test/api", "trans-hub", artifact, True
        )

    def test_retries_are_bounded_and_ambiguous_confirmation_is_read_back(self) -> None:
        with TemporaryDirectory() as temporary:
            control = _Control()
            uploader = _Uploader()
            outcome = execute_one(
                config=self._config(Path(temporary)),
                tokens=_Tokens(),
                control=control,
                source=_Source(),
                uploader=uploader,
                host_factory=lambda _artifact: _Host(),  # type: ignore[arg-type,return-value]
            )
        self.assertEqual(outcome, "executor_result_handed_off")
        self.assertEqual(len(control.grant_commands), 2)
        self.assertEqual(len(set(control.grant_commands)), 1)
        self.assertEqual(uploader.calls, 2)
        self.assertEqual(control.confirm_calls, 3)
        self.assertEqual(control.status_calls, 1)
        self.assertEqual(control.failures, [])

    def test_adapter_failure_is_closed_before_any_upload(self) -> None:
        with TemporaryDirectory() as temporary:
            control = _Control()
            uploader = _Uploader()
            with self.assertRaisesRegex(ExecutorError, "offline_executor_adapter_failed"):
                execute_one(
                    config=self._config(Path(temporary)),
                    tokens=_Tokens(),
                    control=control,
                    source=_Source(),
                    uploader=uploader,
                    host_factory=lambda _artifact: _Host(
                        OfflineExecutorHostError("offline_executor_adapter_failed")
                    ),  # type: ignore[arg-type,return-value]
                )
        self.assertEqual(uploader.calls, 0)
        self.assertEqual(control.grant_commands, [])
        self.assertEqual(control.failures, ["adapter_validation_rejected"])


if __name__ == "__main__":
    unittest.main()
