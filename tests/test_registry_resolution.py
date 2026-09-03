from __future__ import annotations

import json
import unittest
from dataclasses import replace
from hashlib import sha1, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from adapters.obsidian.public_discovery_executor import (
    ExecutorConfig,
    ExecutorError,
    HttpGitHubMetadataReader,
    HttpControlPlane,
    OfficialDirectoryProfile,
    RegistryResolutionClaim,
    RegistryResolutionResult,
    RegistrySnapshot,
    execute_fair_cycle,
    execute_registry_resolution_one,
    load_official_directory_profile,
    _safe_diagnostic_code,
)

ROOT = Path(__file__).parents[1]
DIRECTORY_COMMIT = "a1" * 20
DIRECTORY_TREE = "b2" * 20
RELEASE_COMMIT = "c3" * 20
RELEASE_TREE = "d4" * 20
MANIFEST = b'{"id":"example-plugin","version":"1.0.0"}'
MAIN = b"console.log('example')"


def _git_blob_sha(body: bytes) -> str:
    return sha1(
        f"blob {len(body)}\0".encode("ascii") + body,
        usedforsecurity=False,
    ).hexdigest()


def _directory(*entries: dict[str, object]) -> bytes:
    return json.dumps(entries, separators=(",", ":"), sort_keys=True).encode()


def _claim(profile: OfficialDirectoryProfile) -> RegistryResolutionClaim:
    return RegistryResolutionClaim(
        "11111111-1111-4111-8111-111111111111",
        profile.registry_key,
        "example-plugin",
        profile.authority_digest,
        profile.profile_digest,
        0,
        7,
    )


class _Tokens:
    def __init__(self) -> None:
        self.count = 0

    def token(self) -> str:
        self.count += 1
        return f"oidc-{self.count}"


class _RegistryControl:
    def __init__(self, claim: RegistryResolutionClaim | None) -> None:
        self.claim_value = claim
        self.results: list[dict[str, object]] = []
        self.failures: list[tuple[str, str]] = []
        self.result_commands: list[str] = []
        self.failure_commands: list[str] = []

    def registry_resolution_claim(
        self, _token: str
    ) -> RegistryResolutionClaim | None:
        return self.claim_value

    def registry_resolution_result(
        self,
        _token: str,
        claim: RegistryResolutionClaim,
        result: RegistryResolutionResult,
        command_id: str,
    ) -> None:
        self.result_commands.append(command_id)
        self.results.append(result.payload(claim, command_id))

    def registry_resolution_fail(
        self,
        _token: str,
        _claim: RegistryResolutionClaim,
        failure_code: str,
        evidence_digest: str,
        command_id: str,
    ) -> None:
        self.failure_commands.append(command_id)
        self.failures.append((failure_code, evidence_digest))


class _Github:
    def __init__(
        self,
        profile: OfficialDirectoryProfile,
        body: bytes,
        *,
        transient_path: str | None = None,
        always_transient: bool = False,
    ) -> None:
        owner = profile.owner_login
        repository = profile.repository_name
        directory_repository_path = f"/repos/{owner}/{repository}"
        plugin_repository_path = "/repos/plugin-owner/example-repo"
        self.calls: list[str] = []
        self.raw = body
        self.transient_path = transient_path
        self.always_transient = always_transient
        self.transient_count = 0
        self.values: dict[str, dict[str, object]] = {
            directory_repository_path: {
                "id": profile.repository_id,
                "name": repository,
                "full_name": f"{owner}/{repository}",
                "owner": {"id": profile.owner_id, "login": owner},
                "private": False,
                "default_branch": profile.default_branch,
                "archived": False,
                "disabled": False,
            },
            directory_repository_path + f"/commits/{profile.default_branch}": {
                "sha": DIRECTORY_COMMIT,
                "commit": {
                    "tree": {"sha": DIRECTORY_TREE},
                    "committer": {"date": "2026-09-03T00:00:00Z"},
                },
            },
            directory_repository_path + f"/git/trees/{DIRECTORY_TREE}": {
                "sha": DIRECTORY_TREE,
                "truncated": False,
                "tree": [
                    {
                        "path": profile.directory_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": _git_blob_sha(body),
                        "size": len(body),
                    }
                ],
            },
            plugin_repository_path: {
                "id": 987654,
                "name": "example-repo",
                "full_name": "plugin-owner/example-repo",
                "owner": {"id": 123456, "login": "plugin-owner"},
                "private": False,
            },
            plugin_repository_path + "/releases/latest": {
                "id": 24680,
                "tag_name": "1.0.0",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "id": 13579,
                        "name": "main.js",
                        "state": "uploaded",
                        "size": len(MAIN),
                        "digest": "sha256:" + sha256(MAIN).hexdigest(),
                    },
                    {
                        "id": 97531,
                        "name": "manifest.json",
                        "state": "uploaded",
                        "size": len(MANIFEST),
                        "digest": "sha256:" + sha256(MANIFEST).hexdigest(),
                    },
                ],
            },
            plugin_repository_path + "/commits/1.0.0": {
                "sha": RELEASE_COMMIT,
                "commit": {
                    "tree": {"sha": RELEASE_TREE},
                    "committer": {"date": "2026-09-03T00:00:00Z"},
                },
            },
        }

    def json_object(self, path: str) -> dict[str, object]:
        self.calls.append(path)
        self._maybe_transient(path)
        try:
            return self.values[path]
        except KeyError:
            raise AssertionError(f"unexpected GitHub path: {path}") from None

    def raw_bytes(self, path: str, _limit: int) -> bytes:
        self.calls.append(path)
        self._maybe_transient(path)
        self.raw_path = path
        return self.raw

    def release_asset_digest(
        self, owner_login: str, repository_name: str, asset: object
    ) -> str:
        self.calls.append(
            f"/repos/{owner_login}/{repository_name}/releases/assets"
        )
        name = getattr(asset, "name")
        value = {"main.js": MAIN, "manifest.json": MANIFEST}.get(name)
        if value is None:
            raise AssertionError(f"unexpected asset: {name}")
        actual = sha256(value).hexdigest()
        declared = getattr(asset, "sha256")
        if declared is not None and declared != actual:
            raise ExecutorError("registry_release_asset_digest_mismatch")
        return actual

    def _maybe_transient(self, path: str) -> None:
        if path != self.transient_path:
            return
        self.transient_count += 1
        if self.always_transient or self.transient_count == 1:
            raise ExecutorError("registry_github_request_failed", retryable=True)


class RegistryResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_official_directory_profile()
        self.present_body = _directory(
            {
                "id": "example-plugin",
                "name": "Example Plugin",
                "description": "raw directory text must not leave the validator",
                "repo": "plugin-owner/example-repo",
            },
            {
                "id": "another-plugin",
                "name": "Another",
                "repo": "another-owner/another-repo",
            },
        )

    def test_github_actions_token_is_used_only_as_an_api_authorization_header(self) -> None:
        captured: list[object] = []

        class _Response:
            status = 200

            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def geturl(self) -> str:
                return "https://api.github.com/repos/obsidianmd/obsidian-releases"

            def read(self, _limit: int) -> bytes:
                return b"{}"

        def _open(request: object, *, timeout: int) -> _Response:
            self.assertEqual(timeout, 30)
            captured.append(request)
            return _Response()

        with patch(
            "adapters.obsidian.public_discovery_executor.urlopen", _open
        ):
            self.assertEqual(
                HttpGitHubMetadataReader("gha-short-lived-token").json_object(
                    "/repos/obsidianmd/obsidian-releases"
                ),
                {},
            )

        request = captured[0]
        self.assertEqual(
            getattr(request, "get_header")("Authorization"),
            "Bearer gha-short-lived-token",
        )
        self.assertEqual(
            getattr(request, "full_url"),
            "https://api.github.com/repos/obsidianmd/obsidian-releases",
        )
        with self.assertRaisesRegex(ExecutorError, "executor_github_token_invalid"):
            HttpGitHubMetadataReader("token" + chr(10) + "header")

    def test_failure_diagnostics_are_bounded_codes_only(self) -> None:
        self.assertEqual(
            _safe_diagnostic_code("registry_release_asset_digest_mismatch"),
            "registry_release_asset_digest_mismatch",
        )
        self.assertEqual(_safe_diagnostic_code("token\\nvalue"), "unknown")

    def test_profile_is_fixed_and_drift_fails_closed(self) -> None:
        self.assertEqual(self.profile.registry_key, "official-directory")
        self.assertEqual(self.profile.repository_id, 262342594)
        self.assertEqual(self.profile.owner_id, 65011256)
        self.assertEqual(self.profile.api_version, "2026-03-10")
        self.assertEqual(
            self.profile.authority_digest,
            "ebd8067a957d2950abda527de370631f863b902c940c7abdc0615e73ef2b8784",
        )
        self.assertEqual(
            self.profile.profile_digest,
            "b723fbc68d9f32d87421ad6a279d15a04f3c3c2f6962b8ead544608de8341e4b",
        )

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            value = json.loads(
                (ROOT / "adapters/obsidian/official-directory-profile.json").read_text()
            )
            value["directoryRepository"]["repositoryId"] = 1
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(
                ExecutorError, "registry_validator_profile_invalid"
            ):
                load_official_directory_profile(path)

        drifted = replace(
            _claim(self.profile), validator_profile_digest="00" * 32
        )
        control = _RegistryControl(drifted)
        github = _Github(self.profile, self.present_body)
        with self.assertRaisesRegex(
            ExecutorError, "registry_validator_profile_binding_changed"
        ):
            execute_registry_resolution_one(
                tokens=_Tokens(),
                control=control,
                github=github,
                profile=self.profile,
            )
        self.assertEqual(github.calls, [])
        self.assertEqual(control.failures[0][0], "registry_profile_changed")

    def test_present_result_contains_only_bound_metadata_and_digests(self) -> None:
        control = _RegistryControl(_claim(self.profile))
        github = _Github(self.profile, self.present_body)
        outcome = execute_registry_resolution_one(
            tokens=_Tokens(),
            control=control,
            github=github,
            profile=self.profile,
        )
        self.assertEqual(outcome, "registry_resolution_present")
        self.assertEqual(control.failures, [])
        self.assertEqual(len(control.results), 1)
        result = control.results[0]
        self.assertEqual(
            set(result),
            {
                "commandId",
                "leaseFence",
                "registryKey",
                "externalObjectId",
                "registryAuthorityDigest",
                "validatorProfileDigest",
                "expectedRegistryHeadGeneration",
                "resolutionStatus",
                "registryRevision",
                "registryCommitSha",
                "registryContentDigest",
                "entryDigest",
                "repositoryId",
                "repositoryOwnerId",
                "repositoryOwnerLogin",
                "repositoryName",
                "releaseId",
                "releaseTag",
                "releaseCommitSha",
                "assets",
                "evidenceDigest",
            },
        )
        self.assertEqual(result["resolutionStatus"], "present")
        self.assertEqual(result["registryCommitSha"], DIRECTORY_COMMIT)
        self.assertEqual(result["repositoryId"], 987654)
        self.assertEqual(result["releaseId"], 24680)
        self.assertEqual(result["releaseCommitSha"], RELEASE_COMMIT)
        self.assertEqual(
            [asset["name"] for asset in cast(list[dict[str, object]], result["assets"])],
            ["main.js", "manifest.json"],
        )
        serialized = json.dumps(result, sort_keys=True)
        for forbidden in (
            "raw directory text",
            "plugin-owner/example-repo",
            "https://",
            "browser_download_url",
            "content_base64",
            "main.js content",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            result["evidenceDigest"],
            sha256(
                json.dumps(
                    {key: value for key, value in result.items() if key != "evidenceDigest"},
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        )

    def test_absent_is_only_emitted_after_complete_pinned_directory(self) -> None:
        body = _directory(
            {
                "id": "another-plugin",
                "name": "Another",
                "repo": "another-owner/another-repo",
            }
        )
        control = _RegistryControl(_claim(self.profile))
        github = _Github(self.profile, body)
        outcome = execute_registry_resolution_one(
            tokens=_Tokens(),
            control=control,
            github=github,
            profile=self.profile,
        )
        self.assertEqual(outcome, "registry_resolution_absent")
        result = control.results[0]
        self.assertEqual(result["resolutionStatus"], "absent")
        self.assertEqual(
            set(result),
            {
                "leaseFence",
                "commandId",
                "registryKey",
                "externalObjectId",
                "registryAuthorityDigest",
                "validatorProfileDigest",
                "expectedRegistryHeadGeneration",
                "resolutionStatus",
                "registryRevision",
                "registryCommitSha",
                "registryContentDigest",
                "evidenceDigest",
            },
        )
        self.assertFalse(any("releases/latest" in path for path in github.calls))

    def test_network_failure_retries_and_never_becomes_absent(self) -> None:
        path = "/repos/plugin-owner/example-repo/releases/latest"
        transient = _Github(
            self.profile, self.present_body, transient_path=path
        )
        control = _RegistryControl(_claim(self.profile))
        self.assertEqual(
            execute_registry_resolution_one(
                tokens=_Tokens(),
                control=control,
                github=transient,
                profile=self.profile,
            ),
            "registry_resolution_present",
        )
        self.assertEqual(transient.transient_count, 2)

        unavailable = _Github(
            self.profile,
            self.present_body,
            transient_path=path,
            always_transient=True,
        )
        control = _RegistryControl(_claim(self.profile))
        with self.assertRaisesRegex(
            ExecutorError, "registry_github_request_failed"
        ):
            execute_registry_resolution_one(
                tokens=_Tokens(),
                control=control,
                github=unavailable,
                profile=self.profile,
            )
        self.assertEqual(control.results, [])
        self.assertEqual(control.failures[0][0], "registry_resolution_retryable")
        self.assertEqual(unavailable.transient_count, 3)

    def test_result_and_failure_command_ids_are_stable_across_retries(self) -> None:
        class ResultRetryControl(_RegistryControl):
            def registry_resolution_result(
                self,
                token: str,
                claim: RegistryResolutionClaim,
                result: RegistryResolutionResult,
                command_id: str,
            ) -> None:
                super().registry_resolution_result(
                    token, claim, result, command_id
                )
                if len(self.result_commands) == 1:
                    raise ExecutorError(
                        "executor_control_request_failed", retryable=True
                    )

        result_control = ResultRetryControl(_claim(self.profile))
        self.assertEqual(
            execute_registry_resolution_one(
                tokens=_Tokens(),
                control=result_control,
                github=_Github(self.profile, self.present_body),
                profile=self.profile,
            ),
            "registry_resolution_present",
        )
        self.assertEqual(len(result_control.result_commands), 2)
        self.assertEqual(len(set(result_control.result_commands)), 1)
        self.assertEqual(
            result_control.results[0]["evidenceDigest"],
            result_control.results[1]["evidenceDigest"],
        )

        class FailureRetryControl(_RegistryControl):
            def registry_resolution_fail(
                self,
                token: str,
                claim: RegistryResolutionClaim,
                failure_code: str,
                evidence_digest: str,
                command_id: str,
            ) -> None:
                super().registry_resolution_fail(
                    token,
                    claim,
                    failure_code,
                    evidence_digest,
                    command_id,
                )
                if len(self.failure_commands) == 1:
                    raise ExecutorError(
                        "executor_control_request_failed", retryable=True
                    )

        failure_control = FailureRetryControl(
            replace(
                _claim(self.profile), validator_profile_digest="00" * 32
            )
        )
        with self.assertRaisesRegex(
            ExecutorError, "registry_validator_profile_binding_changed"
        ):
            execute_registry_resolution_one(
                tokens=_Tokens(),
                control=failure_control,
                github=_Github(self.profile, self.present_body),
                profile=self.profile,
            )
        self.assertEqual(len(failure_control.failure_commands), 2)
        self.assertEqual(len(set(failure_control.failure_commands)), 1)

    def test_release_assets_without_github_digest_are_hashed_in_memory(self) -> None:
        github = _Github(self.profile, self.present_body)
        release = github.values["/repos/plugin-owner/example-repo/releases/latest"]
        assets = cast(list[dict[str, object]], release["assets"])
        assets[0]["digest"] = None
        control = _RegistryControl(_claim(self.profile))
        self.assertEqual(
            execute_registry_resolution_one(
                tokens=_Tokens(),
                control=control,
                github=github,
                profile=self.profile,
            ),
            "registry_resolution_present",
        )
        result_assets = cast(list[dict[str, object]], control.results[0]["assets"])
        self.assertEqual(result_assets[0]["sha256"], sha256(MAIN).hexdigest())

        mismatched = _Github(self.profile, self.present_body)
        mismatched_release = mismatched.values[
            "/repos/plugin-owner/example-repo/releases/latest"
        ]
        mismatched_assets = cast(list[dict[str, object]], mismatched_release["assets"])
        mismatched_assets[0]["digest"] = "sha256:" + "00" * 32
        with self.assertRaisesRegex(ExecutorError, "registry_release_asset_digest_mismatch"):
            execute_registry_resolution_one(
                tokens=_Tokens(),
                control=_RegistryControl(_claim(self.profile)),
                github=mismatched,
                profile=self.profile,
            )

    def test_nonstandard_or_invalid_unicode_directory_never_becomes_absent(self) -> None:
        bodies = (
            b'[{"id":"another-plugin","repo":"another-owner/another-repo","extra":NaN}]',
            b'[{"id":"example-plugin","repo":"plugin-owner/example-repo","extra":"\\ud800"}]',
            b'[{"id":"another-plugin","repo":"another-owner/another-repo","extra":1e999}]',
            b'[{"id":"another-plugin","repo":"another-owner/another-repo","extra":'
            + b"[" * 900
            + b"0"
            + b"]" * 900
            + b"}]",
            b'[{"id":"another-plugin","repo":"another-owner/another-repo","extra":'
            + b"[" * 1100
            + b"0"
            + b"]" * 1100
            + b"}]",
        )
        for body in bodies:
            with self.subTest(body=body):
                control = _RegistryControl(_claim(self.profile))
                with self.assertRaisesRegex(
                    ExecutorError, "registry_directory_content_invalid"
                ):
                    execute_registry_resolution_one(
                        tokens=_Tokens(),
                        control=control,
                        github=_Github(self.profile, body),
                        profile=self.profile,
                    )
                self.assertEqual(control.results, [])
                self.assertEqual(
                    control.failures[0][0], "registry_validation_rejected"
                )
        self.assertEqual(control.failures[0][0], "registry_validation_rejected")

        github = _Github(self.profile, self.present_body + b"\n")
        github.raw = self.present_body
        control = _RegistryControl(_claim(self.profile))
        with self.assertRaisesRegex(ExecutorError, "registry_directory_size_mismatch"):
            execute_registry_resolution_one(
                tokens=_Tokens(),
                control=control,
                github=github,
                profile=self.profile,
            )
        self.assertEqual(control.results, [])

    def test_fair_cycle_attempts_stage_a_before_stage_b_and_reports_no_job(self) -> None:
        events: list[str] = []

        class Control(_RegistryControl):
            def registry_resolution_claim(
                self, _token: str
            ) -> RegistryResolutionClaim | None:
                events.append("stage-a")
                return None

            def claim(self, _token: str) -> None:
                events.append("stage-b")
                return None

        with TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "executor.pyz"
            artifact.write_bytes(b"executor")
            outcome = execute_fair_cycle(
                config=ExecutorConfig(
                    "https://api.example.test/api", "trans-hub", artifact, True
                ),
                tokens=_Tokens(),
                control=cast(object, Control(None)),  # type: ignore[arg-type]
                github=_Github(self.profile, self.present_body),
                profile=self.profile,
                source=cast(object, None),  # type: ignore[arg-type]
                uploader=cast(object, None),  # type: ignore[arg-type]
            )
        self.assertEqual(outcome, "executor_no_job")
        self.assertEqual(events, ["stage-a", "stage-b"])

    def test_stage_a_failure_is_closed_without_starving_stage_b(self) -> None:
        events: list[str] = []
        drifted = replace(
            _claim(self.profile), validator_profile_digest="00" * 32
        )

        class Control(_RegistryControl):
            def registry_resolution_claim(
                self, token: str
            ) -> RegistryResolutionClaim | None:
                events.append("stage-a")
                return super().registry_resolution_claim(token)

            def claim(self, _token: str) -> None:
                events.append("stage-b")
                return None

        control = Control(drifted)
        with TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "executor.pyz"
            artifact.write_bytes(b"executor")
            with self.assertRaisesRegex(
                ExecutorError, "registry_validator_profile_binding_changed"
            ):
                execute_fair_cycle(
                    config=ExecutorConfig(
                        "https://api.example.test/api",
                        "trans-hub",
                        artifact,
                        True,
                    ),
                    tokens=_Tokens(),
                    control=cast(object, control),  # type: ignore[arg-type]
                    github=_Github(self.profile, self.present_body),
                    profile=self.profile,
                    source=cast(object, None),  # type: ignore[arg-type]
                    uploader=cast(object, None),  # type: ignore[arg-type]
                )
        self.assertEqual(events, ["stage-a", "stage-b"])
        self.assertEqual(control.failures[0][0], "registry_profile_changed")

    def test_http_control_plane_uses_registry_resolution_wire_contract(self) -> None:
        client = HttpControlPlane("https://api.example.test")
        captured: list[tuple[str, str, object | None]] = []
        claim_body = {
            "jobId": "11111111-1111-4111-8111-111111111111",
            "registryKey": "official-directory",
            "externalObjectId": "example-plugin",
            "registryAuthorityDigest": self.profile.authority_digest,
            "validatorProfileDigest": self.profile.profile_digest,
            "expectedRegistryHeadGeneration": 0,
            "leaseFence": 7,
        }

        def request(
            method: str,
            path: str,
            _token: str,
            payload: object | None = None,
        ) -> tuple[int, bytes]:
            captured.append((method, path, payload))
            if path.endswith("registry-resolution-claims"):
                return 200, json.dumps(claim_body).encode()
            return 204, b""

        client._request = request  # type: ignore[method-assign]
        claim = client.registry_resolution_claim("claim-oidc")
        self.assertIsNotNone(claim)
        assert claim is not None
        result = RegistryResolutionResult(
            "absent",
            RegistrySnapshot(1, "cd" * 20, "ef" * 32),
            None,
            None,
            None,
            (),
        )
        client.registry_resolution_result(
            "result-oidc",
            claim,
            result,
            "22222222-2222-4222-8222-222222222222",
        )
        client.registry_resolution_fail(
            "fail-oidc",
            claim,
            "registry_resolution_retryable",
            "01" * 32,
            "33333333-3333-4333-8333-333333333333",
        )

        self.assertEqual(
            captured[0][:2],
            (
                "POST",
                "/v1/public-discovery-executor/registry-resolution-claims",
            ),
        )
        self.assertEqual(
            captured[1][1],
            "/v1/public-discovery-executor/registry-resolution-jobs/"
            "11111111-1111-4111-8111-111111111111/results",
        )
        self.assertEqual(
            cast(dict[str, object], captured[1][2])["resolutionStatus"],
            "absent",
        )
        self.assertEqual(
            cast(dict[str, object], captured[1][2])["evidenceDigest"],
            "75cb62e2b05524255cf1c03572365fa477b9edde2bcc9ffb8cc5d56aa47d8510",
        )
        self.assertEqual(
            captured[2][1],
            "/v1/public-discovery-executor/registry-resolution-jobs/"
            "11111111-1111-4111-8111-111111111111/fail",
        )
        self.assertEqual(
            captured[2][2],
            {
                "commandId": "33333333-3333-4333-8333-333333333333",
                "leaseFence": 7,
                "registryKey": "official-directory",
                "externalObjectId": "example-plugin",
                "registryAuthorityDigest": self.profile.authority_digest,
                "validatorProfileDigest": self.profile.profile_digest,
                "failureCode": "registry_resolution_retryable",
                "evidenceDigest": "01" * 32,
            },
        )


if __name__ == "__main__":
    unittest.main()
