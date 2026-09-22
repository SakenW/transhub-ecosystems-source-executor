from __future__ import annotations

import base64
import json
import unittest
from hashlib import sha256
from unittest.mock import Mock

from adapters.obsidian.public_discovery_executor import (
    Asset,
    Claim,
    ExecutorConfig,
    ExecutorError,
    HttpControlPlane,
    UploadGrant,
    execute_one,
)
from adapters.web.frozen_catalog_executor import (
    HttpWebCatalogControlPlane,
    WebCatalogPlan,
    execute_web_catalog_one,
    load_web_catalog_registry_profile,
    parse_web_catalog_source_plan,
)


def _catalog(site: str = "figma") -> bytes:
    return json.dumps(
        {
            "source_catalog": {
                "protocol": "trans-hub.canonical-source-catalog",
                "revision": 2,
                "resource": {
                    "resource_key": "web-site:" + site,
                    "object_kind_key": "website",
                },
                "stream": {"stream_key": "web-site:" + site, "locale": "en"},
                "sources": [{"key": "source", "logical_path": "fixture", "format_family": "snapshot"}],
                "units": [{"key": "unit", "text": "Files", "source_key": "source", "context": {"content_scopes": ["shell"]}}],
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _plan(content: bytes | None = None) -> WebCatalogPlan:
    content = content or _catalog()
    return WebCatalogPlan(
        1,
        "official-owner",
        2,
        "web-catalogs",
        3,
        "2026.9.21.1",
        "ab" * 20,
        Asset(4, "figma.canonical-source-catalog.json", len(content), sha256(content).hexdigest()),
        5,
        "cc" * 32,
        "dd" * 32,
        "ee" * 32,
        "ff" * 32,
        "canonical-json-v1",
        "application/vnd.trans-hub.public-discovery-result+json",
        1024 * 1024,
        "11" * 32,
    )


def _source_plan_payload(content: bytes) -> dict[str, object]:
    return {
        "provider": "github_release",
        "ownerId": 1,
        "ownerLogin": "official-owner",
        "repositoryId": 2,
        "repositoryName": "web-catalogs",
        "releaseId": 3,
        "commitSha": "ab" * 20,
        "tag": "2026.9.21.1",
        "assets": [{"assetId": 4, "name": "figma.canonical-source-catalog.json", "size": len(content), "sha256": sha256(content).hexdigest()}],
        "primaryAssetId": 4,
        "projectionGeneration": 5,
        "authorityBindingDigest": "cc" * 32,
        "sourcePlanDigest": "dd" * 32,
        "adapterBuildDigest": "ee" * 32,
        "adapterProfileDigest": "ff" * 32,
        "resultSchema": "public-discovery/v1",
        "resultMediaType": "application/vnd.trans-hub.public-discovery-result+json",
        "resultMaxBytes": 1024 * 1024,
        "materializationTargetDigest": "11" * 32,
    }


class _Tokens:
    def token(self) -> str:
        return "oidc"


class _SequentialTokens:
    def __init__(self) -> None:
        self.calls = 0

    def token(self) -> str:
        self.calls += 1
        return f"oidc-{self.calls}"


class _Source:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def chunks(self, _plan_value: WebCatalogPlan, _asset: Asset) -> tuple[bytes, ...]:
        return (self.content,)


class _Uploader:
    def __init__(self) -> None:
        self.result: bytes | None = None

    def upload(self, _grant: UploadGrant, result: bytes) -> None:
        self.result = result


class _Control:
    def __init__(self, plan: WebCatalogPlan) -> None:
        self.plan = plan
        self.failures: list[tuple[str, str]] = []
        self.source_plan_tokens: list[str] = []

    def claim(self, _token: str) -> Claim:
        return Claim("11111111-1111-4111-8111-111111111111", "registry/opaque", "ee" * 32, 7, "web-site-catalog", "figma")

    def source_plan(self, token: str, _claim: Claim) -> WebCatalogPlan:
        self.source_plan_tokens.append(token)
        return self.plan

    def grant(self, _token: str, _claim: Claim, _plan: WebCatalogPlan, _result: bytes, _command: str) -> UploadGrant:
        return UploadGrant("22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333", "token", "public-discovery/results/" + "12" * 32 + ".canonical-json", ("https://upload.example.test",), "application/vnd.trans-hub.public-discovery-result+json", 1)

    def confirm(self, _token: str, _claim: Claim, _grant: UploadGrant) -> None:
        return None

    def status(self, _token: str, _claim: Claim) -> str:
        return "materialization_pending"

    def fail(self, _token: str, _claim: Claim, failure_code: str, evidence: str) -> None:
        self.failures.append((failure_code, evidence))


class WebFrozenCatalogExecutorTests(unittest.TestCase):
    def test_registry_profile_is_pinned_to_the_catalog_repository_identity(self) -> None:
        profile = load_web_catalog_registry_profile()
        self.assertEqual(profile.registry_key, "web-site-catalog")
        self.assertEqual(profile.repository_id, 1378958674)
        self.assertEqual(profile.owner_id, 16665726)
        self.assertEqual(profile.default_branch, "main")

    def test_http_control_reuses_shared_controls_but_parses_catalog_plan_locally(self) -> None:
        content = _catalog()
        raw = _source_plan_payload(content)
        base = Mock()
        base._request.return_value = (200, json.dumps(raw).encode())
        plan = HttpWebCatalogControlPlane(base).source_plan("oidc", _Control(_plan(content)).claim("oidc"))
        self.assertEqual(plan.catalog_asset.sha256, sha256(content).hexdigest())
        base._request.assert_called_once()

    def test_shared_executor_dispatches_web_reference_before_obsidian_host(self) -> None:
        content = _catalog()
        control = HttpControlPlane("https://api.example.test")
        claim = _Control(_plan(content)).claim("oidc")
        control.claim = lambda _token: claim  # type: ignore[method-assign]
        control._request = lambda *_args: (200, json.dumps(_source_plan_payload(content)).encode())  # type: ignore[method-assign]
        grant = _Control(_plan(content)).grant("oidc", claim, _plan(content), b"{}", "00000000-0000-4000-8000-000000000000")
        control.grant = lambda *_args: grant  # type: ignore[method-assign]
        control.confirm = lambda *_args: None  # type: ignore[method-assign]
        control.status = lambda *_args: "materialization_pending"  # type: ignore[method-assign]
        control.fail = lambda *_args: None  # type: ignore[method-assign]
        metadata = Mock()
        metadata.json_object.return_value = {
            "license": {"spdx_id": "MIT"},
            "content": base64.b64encode(b"MIT").decode(),
            "encoding": "base64",
            "path": "LICENSE",
        }
        self.assertEqual(
            execute_one(
                config=ExecutorConfig("https://api.example.test", "audience", __file__, True),
                tokens=_Tokens(),
                control=control,
                metadata=metadata,
                source=_Source(content),
                uploader=_Uploader(),
            ),
            "web_catalog_result_handed_off",
        )

    def test_executes_exact_catalog_asset_and_emits_result_revision_two(self) -> None:
        content = _catalog()
        control = _Control(_plan(content))
        uploader = _Uploader()
        metadata = Mock()
        metadata.json_object.return_value = {
            "license": {"spdx_id": "MIT"},
            "content": base64.b64encode(b"MIT").decode(),
            "encoding": "base64",
            "path": "LICENSE",
        }

        outcome = execute_web_catalog_one(
            tokens=_Tokens(), control=control, metadata=metadata, source=_Source(content), uploader=uploader
        )

        self.assertEqual(outcome, "web_catalog_result_handed_off")
        self.assertIsNotNone(uploader.result)
        result = json.loads(uploader.result or b"{}")
        self.assertEqual(result["result"], {"materialization_target_digest": "11" * 32, "protocol": "trans-hub.public-discovery-result", "revision": 2})
        self.assertEqual(result["source_catalog"]["resource"]["resource_key"], "figma")
        self.assertEqual(result["source_catalog"]["resource"]["object_kind_key"], "localizable_resource")
        self.assertEqual(result["source_catalog"]["stream"]["stream_key"], "web-site:figma")
        self.assertEqual(result["license_evidence"]["license_identifier"], "MIT")

    def test_reuses_the_claim_token_for_the_source_plan(self) -> None:
        content = _catalog()
        control = _Control(_plan(content))
        metadata = Mock()
        metadata.json_object.return_value = {
            "license": {"spdx_id": "MIT"},
            "content": base64.b64encode(b"MIT").decode(),
            "encoding": "base64",
            "path": "LICENSE",
        }

        self.assertEqual(
            execute_web_catalog_one(
                tokens=_SequentialTokens(),
                control=control,
                metadata=metadata,
                source=_Source(content),
                uploader=_Uploader(),
            ),
            "web_catalog_result_handed_off",
        )
        self.assertEqual(control.source_plan_tokens, ["oidc-1"])

    def test_rejects_asset_for_a_different_site_and_closes_task(self) -> None:
        content = _catalog("replit")
        control = _Control(_plan(content))
        with self.assertRaisesRegex(ExecutorError, "web_catalog_source_identity_invalid"):
            execute_web_catalog_one(
                tokens=_Tokens(), control=control, metadata=Mock(), source=_Source(content), uploader=_Uploader()
            )
        self.assertEqual(control.failures[0][0], "source_validation_rejected")

    def test_source_plan_requires_one_matching_catalog_asset_and_bound_adapter(self) -> None:
        content = _catalog()
        payload = {
            "provider": "github_release",
            "ownerId": 1,
            "ownerLogin": "official-owner",
            "repositoryId": 2,
            "repositoryName": "web-catalogs",
            "releaseId": 3,
            "commitSha": "ab" * 20,
            "tag": "2026.9.21.1",
            "assets": [{"assetId": 4, "name": "figma.canonical-source-catalog.json", "size": len(content), "sha256": sha256(content).hexdigest()}],
            "primaryAssetId": 4,
            "projectionGeneration": 5,
            "authorityBindingDigest": "cc" * 32,
            "sourcePlanDigest": "dd" * 32,
            "adapterBuildDigest": "ee" * 32,
            "adapterProfileDigest": "ff" * 32,
            "resultSchema": "public-discovery/v1",
            "resultMediaType": "application/vnd.trans-hub.public-discovery-result+json",
            "resultMaxBytes": 1024,
            "materializationTargetDigest": "11" * 32,
        }
        plan = parse_web_catalog_source_plan(payload, claimed_adapter_build_digest="ee" * 32)
        self.assertEqual(plan.catalog_asset.name, "figma.canonical-source-catalog.json")
        payload["assets"][0]["name"] = "main.js"
        with self.assertRaisesRegex(ExecutorError, "web_catalog_source_plan_invalid"):
            parse_web_catalog_source_plan(payload, claimed_adapter_build_digest="ee" * 32)


if __name__ == "__main__":
    unittest.main()
