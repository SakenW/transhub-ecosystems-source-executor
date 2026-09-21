"""Fail-closed executor for one immutable Web canonical-catalog release asset.

The platform only supplies an already-authorized GitHub Release identity and
its asset digest.  This module never receives a web URL, DOM, selector, or
browser profile.  Site parsing remains upstream of the frozen catalog.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Protocol, cast
from uuid import uuid4

from adapters.obsidian.public_discovery_executor import (
    Asset,
    Claim,
    ExecutorError,
    GitHubMetadataReader,
    LicenseEvidence,
    ResultUploader,
    SourceReader,
    TokenProvider,
    UploadGrant,
    HttpControlPlane,
    _strict_json_value,
    _attach_license_evidence,
    _canonical_json,
    _failure_code,
    _read_pinned_license_evidence,
    _retry,
)

_CATALOG_ASSET_NAME: Final = re.compile(
    r"^[a-z0-9][a-z0-9-]{0,63}[.]canonical-source-catalog[.]json$"
)
_SITE_KEY: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA1: Final = re.compile(r"^[0-9a-f]{40}$")
_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_MAX_CATALOG_BYTES: Final = 16 * 1024 * 1024
_RESULT_MEDIA_TYPE: Final = "application/vnd.trans-hub.public-discovery-result+json"
_RESULT_SCHEMA: Final = "canonical-json-v1"


@dataclass(frozen=True, slots=True)
class WebCatalogPlan:
    """One Release asset that is a complete canonical Web source catalog."""

    owner_id: int
    owner_login: str
    repository_id: int
    repository_name: str
    release_id: int
    release_tag: str
    release_commit_sha: str
    catalog_asset: Asset
    projection_generation: int
    authority_binding_digest: str
    source_plan_digest: str
    adapter_build_digest: str
    adapter_profile_digest: str
    result_schema: str
    result_media_type: str
    result_max_bytes: int
    materialization_target_digest: str


class WebCatalogControlPlane(Protocol):
    def claim(self, token: str) -> Claim | None: ...

    def source_plan(self, token: str, claim: Claim) -> WebCatalogPlan: ...

    def grant(
        self,
        token: str,
        claim: Claim,
        plan: WebCatalogPlan,
        result: bytes,
        command_id: str,
    ) -> UploadGrant: ...

    def confirm(self, token: str, claim: Claim, grant: UploadGrant) -> None: ...

    def status(self, token: str, claim: Claim) -> str: ...

    def fail(self, token: str, claim: Claim, failure_code: str, evidence_digest: str) -> None: ...


class HttpWebCatalogControlPlane:
    """Use the shared HTTP controls while giving Web assets their own parser."""

    def __init__(self, control: HttpControlPlane) -> None:
        self._control = control

    def claim(self, token: str) -> Claim | None:
        return self._control.claim(token)

    def source_plan(self, token: str, claim: Claim) -> WebCatalogPlan:
        _, body = self._control._request(
            "GET", f"/v1/public-discovery-executor/tasks/{claim.task_id}/source-plan", token
        )
        value = _strict_json_value(body, "web_catalog_source_plan_invalid")
        if not isinstance(value, Mapping):
            raise ExecutorError("web_catalog_source_plan_invalid")
        return parse_web_catalog_source_plan(
            value, claimed_adapter_build_digest=claim.adapter_build_digest
        )

    def grant(
        self,
        token: str,
        claim: Claim,
        plan: WebCatalogPlan,
        result: bytes,
        command_id: str,
    ) -> UploadGrant:
        return self._control.grant(token, claim, plan, result, command_id)  # type: ignore[arg-type]

    def confirm(self, token: str, claim: Claim, grant: UploadGrant) -> None:
        self._control.confirm(token, claim, grant)

    def status(self, token: str, claim: Claim) -> str:
        return self._control.status(token, claim)

    def fail(self, token: str, claim: Claim, failure_code: str, evidence_digest: str) -> None:
        self._control.fail(token, claim, failure_code, evidence_digest)


def parse_web_catalog_source_plan(
    value: Mapping[str, object], *, claimed_adapter_build_digest: str
) -> WebCatalogPlan:
    """Parse the control-plane source plan without accepting a locator."""

    expected = {
        "provider",
        "ownerId",
        "ownerLogin",
        "repositoryId",
        "repositoryName",
        "releaseId",
        "commitSha",
        "tag",
        "assets",
        "primaryAssetId",
        "projectionGeneration",
        "authorityBindingDigest",
        "sourcePlanDigest",
        "adapterBuildDigest",
        "adapterProfileDigest",
        "resultSchema",
        "resultMediaType",
        "resultMaxBytes",
        "materializationTargetDigest",
    }
    if set(value) != expected or value.get("provider") != "github_release":
        raise ExecutorError("web_catalog_source_plan_invalid")
    assets_value = value["assets"]
    if not isinstance(assets_value, list) or len(assets_value) != 1:
        raise ExecutorError("web_catalog_source_plan_invalid")
    raw_asset = assets_value[0]
    if not isinstance(raw_asset, Mapping) or set(raw_asset) != {
        "assetId",
        "name",
        "size",
        "sha256",
    }:
        raise ExecutorError("web_catalog_source_plan_invalid")
    name = raw_asset["name"]
    asset_id = raw_asset["assetId"]
    size = raw_asset["size"]
    digest = raw_asset["sha256"]
    if (
        not isinstance(name, str)
        or _CATALOG_ASSET_NAME.fullmatch(name) is None
        or not isinstance(asset_id, int)
        or isinstance(asset_id, bool)
        or asset_id < 1
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= _MAX_CATALOG_BYTES
        or not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or value["primaryAssetId"] != asset_id
    ):
        raise ExecutorError("web_catalog_source_plan_invalid")
    scalar_ids = ("ownerId", "repositoryId", "releaseId", "projectionGeneration")
    if any(
        not isinstance(value[key], int)
        or isinstance(value[key], bool)
        or cast(int, value[key]) < 1
        for key in scalar_ids
    ):
        raise ExecutorError("web_catalog_source_plan_invalid")
    owner_login = value["ownerLogin"]
    repository_name = value["repositoryName"]
    tag = value["tag"]
    commit_sha = value["commitSha"]
    if (
        not isinstance(owner_login, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner_login)
        or not isinstance(repository_name, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", repository_name)
        or not isinstance(tag, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,254}", tag)
        or "//" in tag
        or any(part in {".", ".."} for part in tag.split("/"))
        or not isinstance(commit_sha, str)
        or _SHA1.fullmatch(commit_sha) is None
    ):
        raise ExecutorError("web_catalog_source_plan_invalid")
    digests = (
        "authorityBindingDigest",
        "sourcePlanDigest",
        "adapterBuildDigest",
        "adapterProfileDigest",
        "materializationTargetDigest",
    )
    if any(
        not isinstance(value[key], str) or _DIGEST.fullmatch(cast(str, value[key])) is None
        for key in digests
    ):
        raise ExecutorError("web_catalog_source_plan_invalid")
    if value["adapterBuildDigest"] != claimed_adapter_build_digest:
        raise ExecutorError("web_catalog_source_plan_binding_changed")
    if (
        value["resultSchema"] != _RESULT_SCHEMA
        or value["resultMediaType"] != _RESULT_MEDIA_TYPE
        or not isinstance(value["resultMaxBytes"], int)
        or isinstance(value["resultMaxBytes"], bool)
        or not 1 <= cast(int, value["resultMaxBytes"]) <= 64 * 1024 * 1024
    ):
        raise ExecutorError("web_catalog_source_plan_invalid")
    return WebCatalogPlan(
        owner_id=cast(int, value["ownerId"]),
        owner_login=owner_login,
        repository_id=cast(int, value["repositoryId"]),
        repository_name=repository_name,
        release_id=cast(int, value["releaseId"]),
        release_tag=tag,
        release_commit_sha=commit_sha,
        catalog_asset=Asset(asset_id, name, size, digest),
        projection_generation=cast(int, value["projectionGeneration"]),
        authority_binding_digest=cast(str, value["authorityBindingDigest"]),
        source_plan_digest=cast(str, value["sourcePlanDigest"]),
        adapter_build_digest=cast(str, value["adapterBuildDigest"]),
        adapter_profile_digest=cast(str, value["adapterProfileDigest"]),
        result_schema=_RESULT_SCHEMA,
        result_media_type=_RESULT_MEDIA_TYPE,
        result_max_bytes=cast(int, value["resultMaxBytes"]),
        materialization_target_digest=cast(str, value["materializationTargetDigest"]),
    )


def execute_web_catalog_one(
    *,
    tokens: TokenProvider,
    control: WebCatalogControlPlane,
    metadata: GitHubMetadataReader,
    source: SourceReader,
    uploader: ResultUploader,
) -> str:
    """Hand one frozen Web catalog to the existing verified-result path."""

    claim = control.claim(tokens.token())
    if claim is None:
        return "web_catalog_no_task"
    return execute_web_catalog_claim(
        tokens=tokens,
        control=control,
        metadata=metadata,
        source=source,
        uploader=uploader,
        claim=claim,
    )


def execute_web_catalog_claim(
    *,
    tokens: TokenProvider,
    control: WebCatalogControlPlane,
    metadata: GitHubMetadataReader,
    source: SourceReader,
    uploader: ResultUploader,
    claim: Claim,
) -> str:
    """Complete an already leased Web catalog task without re-claiming work."""

    try:
        site_key = _site_key_from_reference(claim.source_reference)
        plan = control.source_plan(tokens.token(), claim)
        catalog = _load_catalog(source, plan, site_key)
        evidence = _read_pinned_license_evidence(metadata, plan)
        result = _build_result(catalog, plan, evidence)
        if len(result) > plan.result_max_bytes:
            raise ExecutorError("web_catalog_result_size_invalid")
        grant = _retry(
            lambda: control.grant(tokens.token(), claim, plan, result, str(uuid4()))
        )
        _retry(lambda: uploader.upload(grant, result))
        try:
            _retry(lambda: control.confirm(tokens.token(), claim, grant))
        except ExecutorError as exc:
            if (
                not exc.retryable
                or _retry(lambda: control.status(tokens.token(), claim))
                != "materialization_pending"
            ):
                raise
        return "web_catalog_result_handed_off"
    except ExecutorError as exc:
        failure_code = _failure_code(exc.code, retryable=exc.retryable)
        try:
            _retry(
                lambda: control.fail(
                    tokens.token(),
                    claim,
                    failure_code,
                    sha256(exc.code.encode("ascii", "ignore")).hexdigest(),
                )
            )
        except ExecutorError as failure:
            raise ExecutorError(
                "web_catalog_failure_close_unconfirmed", retryable=True
            ) from failure
        raise


def _site_key_from_reference(value: str) -> str:
    match = re.fullmatch(r"web-site-catalog/([a-z0-9][a-z0-9-]{0,63})", value)
    if match is None:
        raise ExecutorError("web_catalog_source_reference_invalid")
    return match.group(1)


def _load_catalog(source: SourceReader, plan: WebCatalogPlan, site_key: str) -> dict[str, object]:
    content = bytearray()
    digest = sha256()
    for chunk in source.chunks(plan, plan.catalog_asset):
        if not isinstance(chunk, bytes) or not chunk:
            raise ExecutorError("web_catalog_source_read_invalid")
        content.extend(chunk)
        digest.update(chunk)
        if len(content) > plan.catalog_asset.size:
            raise ExecutorError("web_catalog_source_size_mismatch")
    if len(content) != plan.catalog_asset.size or digest.hexdigest() != plan.catalog_asset.sha256:
        raise ExecutorError("web_catalog_source_digest_mismatch")
    try:
        root = json.loads(bytes(content), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExecutorError("web_catalog_source_json_invalid") from exc
    if not isinstance(root, dict) or set(root) != {"source_catalog"}:
        raise ExecutorError("web_catalog_source_shape_invalid")
    catalog = root["source_catalog"]
    if not isinstance(catalog, dict):
        raise ExecutorError("web_catalog_source_shape_invalid")
    resource = catalog.get("resource")
    stream = catalog.get("stream")
    if (
        catalog.get("protocol") != "trans-hub.canonical-source-catalog"
        or catalog.get("revision") != 2
        or not isinstance(resource, dict)
        or resource.get("resource_key") != "web-site:" + site_key
        or resource.get("object_kind_key") != "website"
        or not isinstance(stream, dict)
        or stream.get("stream_key") != "web-site:" + site_key
        or stream.get("locale") != "en"
    ):
        raise ExecutorError("web_catalog_source_identity_invalid")
    return cast(dict[str, object], catalog)


def _build_result(
    catalog: dict[str, object], plan: WebCatalogPlan, evidence: LicenseEvidence
) -> bytes:
    return _canonical_json(
        {
            "license_evidence": {
                "immutable_source_revision": evidence.immutable_revision,
                "license_digest": evidence.digest,
                "license_evidence_uri": evidence.uri,
                "license_identifier": evidence.identifier,
            },
            "result": {
                "materialization_target_digest": plan.materialization_target_digest,
                "protocol": "trans-hub.public-discovery-result",
                "revision": 2,
            },
            "source_catalog": catalog,
        }
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


__all__ = [
    "WebCatalogPlan",
    "WebCatalogControlPlane",
    "HttpWebCatalogControlPlane",
    "execute_web_catalog_claim",
    "execute_web_catalog_one",
    "parse_web_catalog_source_plan",
]
