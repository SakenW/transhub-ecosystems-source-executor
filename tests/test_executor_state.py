from __future__ import annotations

import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from adapters.obsidian.offline_executor_host import OfflineExecutorHostError
from adapters.obsidian import public_discovery_executor
from adapters.obsidian.public_discovery_executor import (
    Asset,
    Claim,
    ExecutorConfig,
    ExecutorError,
    HttpControlPlane,
    SourcePlan,
    UploadGrant,
    QiniuResultUploader,
    execute_one,
)


def _claim() -> Claim:
    return Claim("11111111-1111-4111-8111-111111111111", "registry/item", "aa" * 32, 7)


def _plan() -> SourcePlan:
    manifest = b"manifest"
    main = b"main"
    return SourcePlan(
        1,
        "official-owner",
        2,
        "official-plugin",
        3,
        Asset(4, "manifest.json", len(manifest), sha256(manifest).hexdigest()),
        Asset(5, "main.js", len(main), sha256(main).hexdigest()),
        6,
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
        ("https://upload.example.test",),
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
    def __init__(self) -> None:
        self.asset_names: list[str] = []

    def chunks(
        self, _plan_value: SourcePlan, asset: Asset
    ) -> tuple[bytes, ...]:
        self.asset_names.append(asset.name)
        return (b"manifest",) if asset.name == "manifest.json" else (b"main",)


class _Host:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def prepare_result(self, components: object, _task: object) -> bytes:
        self.assert_components = components
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
            source = _Source()
            outcome = execute_one(
                config=self._config(Path(temporary)),
                tokens=_Tokens(),
                control=control,
                source=source,
                uploader=uploader,
                host_factory=lambda _artifact: _Host(),  # type: ignore[arg-type,return-value]
            )
        self.assertEqual(outcome, "executor_result_handed_off")
        self.assertEqual(source.asset_names, ["manifest.json", "main.js"])
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

    def test_grant_sends_only_result_byte_facts(self) -> None:
        client = HttpControlPlane("https://api.example.test")
        captured: dict[str, object] = {}

        def request(
            method: str, path: str, token: str, payload: dict[str, object] | None = None
        ) -> tuple[int, bytes]:
            captured.update(
                {"method": method, "path": path, "token": token, "payload": payload}
            )
            return (
                200,
                json.dumps(
                    {
                        "grantReceiptId": "22222222-2222-4222-8222-222222222222",
                        "resultObjectId": "33333333-3333-4333-8333-333333333333",
                        "grantExpiresAt": "2026-01-01T00:00:00Z",
                        "provider": "qiniu_kodo",
                        "bucket": "result-bucket",
                        "objectKey": "public-discovery/results/" + "12" * 32 + ".canonical-json",
                        "token": "short-lived",
                        "uploadOrigins": [
                            "https://upload.example.test",
                            "https://fallback-upload.example.test",
                        ],
                        "contentType": "application/vnd.trans-hub.public-discovery-result+json",
                        "expectedSizeBytes": 2,
                        "grantState": "upload_grant",
                        "replayed": False,
                    }
                ).encode(),
            )

        client._request = request  # type: ignore[method-assign]
        grant = client.grant(
            "oidc", _claim(), _plan(), b"{}", "44444444-4444-4444-8444-444444444444"
        )

        self.assertEqual(
            captured["payload"],
            {
                "leaseFence": 7,
                "grantCommandId": "44444444-4444-4444-8444-444444444444",
                "expectedTransportDigest": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
                "expectedSizeBytes": 2,
            },
        )
        self.assertEqual(
            grant.upload_origins,
            ("https://upload.example.test", "https://fallback-upload.example.test"),
        )

    def test_result_uploader_tries_each_server_granted_origin(self) -> None:
        grant = UploadGrant(
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
            "short-lived",
            "public-discovery/results/" + "12" * 32 + ".canonical-json",
            ("https://first-upload.example.test", "https://second-upload.example.test"),
            "application/vnd.trans-hub.public-discovery-result+json",
            2,
        )
        attempted: list[str] = []
        original = public_discovery_executor._open_bytes

        def upload(request: object, _limit: int, _code: str) -> bytes:
            url = getattr(request, "full_url")
            attempted.append(url)
            if url == grant.upload_origins[0]:
                error = ExecutorError("executor_result_upload_failed", retryable=True)
                error.http_status = 503  # type: ignore[attr-defined]
                raise error
            return b'{"key":"' + grant.object_key.encode() + b'"}'

        public_discovery_executor._open_bytes = upload
        try:
            QiniuResultUploader().upload(grant, b"{}")
        finally:
            public_discovery_executor._open_bytes = original
        self.assertEqual(attempted, list(grant.upload_origins))

    def test_source_plan_requires_manifest_and_main_release_assets(self) -> None:
        client = HttpControlPlane("https://api.example.test")
        payload = {
            "provider": "github_release",
            "ownerId": 1,
            "ownerLogin": "official-owner",
            "repositoryId": 2,
            "repositoryName": "official-plugin",
            "releaseId": 3,
            "commitSha": "ab" * 20,
            "tag": "1.0.0",
            "assets": [
                {
                    "assetId": 4,
                    "name": "manifest.json",
                    "size": 8,
                    "sha256": sha256(b"manifest").hexdigest(),
                },
                {
                    "assetId": 5,
                    "name": "main.js",
                    "size": 4,
                    "sha256": sha256(b"main").hexdigest(),
                },
            ],
            "primaryAssetId": 5,
            "projectionGeneration": 6,
            "authorityBindingDigest": "cc" * 32,
            "sourcePlanDigest": "dd" * 32,
            "adapterBuildDigest": "aa" * 32,
            "adapterProfileDigest": "ee" * 32,
            "resultSchema": "canonical-json-v1",
            "resultMediaType": "application/vnd.trans-hub.public-discovery-result+json",
            "resultMaxBytes": 1024,
            "materializationTargetDigest": "ff" * 32,
        }
        client._request = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            200,
            json.dumps(payload).encode(),
        )
        plan = client.source_plan("oidc", _claim())
        self.assertEqual(plan.manifest_asset.name, "manifest.json")
        self.assertEqual(plan.main_asset.name, "main.js")

        component_assets = list(payload["assets"])  # type: ignore[arg-type]
        payload["assets"] = component_assets + [
            {
                "assetId": 7,
                "name": "legacy.zip",
                "size": 3,
                "sha256": "bb" * 32,
            }
        ]
        payload["primaryAssetId"] = 7
        with self.assertRaisesRegex(
            ExecutorError, "source_component_closure_incomplete"
        ):
            client.source_plan("oidc", _claim())

        payload["assets"] = [component_assets[0]]
        payload["primaryAssetId"] = 4
        with self.assertRaisesRegex(
            ExecutorError, "source_component_closure_incomplete"
        ):
            client.source_plan("oidc", _claim())


if __name__ == "__main__":
    unittest.main()
