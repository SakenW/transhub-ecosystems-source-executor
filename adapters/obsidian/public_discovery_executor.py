#!/usr/bin/env python3
"""Fail-closed GitHub Actions client for one public-discovery task.

The module intentionally keeps every transport locator and credential inside
the process.  Its command-line surface accepts no URL, token, object key,
source digest, or source bytes, and its diagnostics are bounded error codes.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from hashlib import sha1, sha256
from math import isfinite
from pathlib import Path
from typing import Final, Literal, Protocol, TypeVar, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from .component_bridge import MAX_MAIN_BYTES, MAX_MANIFEST_BYTES
from .offline_executor_host import (
    OfflineExecutorComponent,
    OfflineExecutorHost,
    OfflineExecutorHostError,
    OfflineExecutorTask,
)

_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_SHA1: Final = re.compile(r"^[0-9a-f]{40}$")
_SAFE_REFERENCE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+=:/-]{0,511}$")
_SAFE_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@() -]{0,254}$")
_PLUGIN_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RESULT_MEDIA_TYPE: Final = "application/vnd.trans-hub.public-discovery-result+json"
_GITHUB_API_ORIGIN: Final = "https://api.github.com"
_GITHUB_RELEASE_API_VERSION: Final = "2022-11-28"
_GITHUB_REGISTRY_API_VERSION: Final = "2026-03-10"
_MAX_CONTROL_BYTES: Final = 1024 * 1024
_MAX_GITHUB_METADATA_BYTES: Final = 8 * 1024 * 1024
_MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991
_OFFICIAL_DIRECTORY_PROFILE_PATH: Final = Path(__file__).with_name(
    "official-directory-profile.json"
)
_OFFICIAL_DIRECTORY_REPOSITORY_ID: Final = 262_342_594
_OFFICIAL_DIRECTORY_OWNER_ID: Final = 65_011_256
_OFFICIAL_DIRECTORY_OWNER: Final = "obsidianmd"
_OFFICIAL_DIRECTORY_REPOSITORY: Final = "obsidian-releases"
_OFFICIAL_DIRECTORY_BRANCH: Final = "master"
_OFFICIAL_DIRECTORY_PATH: Final = "community-plugins.json"


class ExecutorError(RuntimeError):
    """A bounded diagnostic that is safe to emit without exception context."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    api_base: str
    oidc_audience: str
    artifact: Path
    ref_protected: bool

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "ExecutorConfig":
        protected = environment.get("GITHUB_REF_PROTECTED")
        api_base = environment.get("TRANS_HUB_PUBLIC_DISCOVERY_API_BASE", "")
        audience = environment.get("TRANS_HUB_PUBLIC_DISCOVERY_OIDC_AUDIENCE", "")
        artifact = environment.get("TRANS_HUB_PUBLIC_DISCOVERY_EXECUTOR_ARTIFACT", "")
        if protected != "true":
            raise ExecutorError("executor_ref_not_protected")
        _validate_https_base(api_base)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", audience):
            raise ExecutorError("executor_oidc_audience_invalid")
        artifact_path = Path(artifact)
        if not artifact_path.is_absolute() or not artifact_path.is_file():
            raise ExecutorError("executor_artifact_unavailable")
        return cls(
            api_base=api_base.rstrip("/"),
            oidc_audience=audience,
            artifact=artifact_path,
            ref_protected=True,
        )


@dataclass(frozen=True, slots=True)
class Claim:
    task_id: str
    source_reference: str
    adapter_build_digest: str
    lease_fence: int


@dataclass(frozen=True, slots=True)
class Asset:
    asset_id: int
    name: str
    size: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class SourcePlan:
    owner_id: int
    owner_login: str
    repository_id: int
    repository_name: str
    release_id: int
    manifest_asset: Asset
    main_asset: Asset
    projection_generation: int
    authority_binding_digest: str
    source_plan_digest: str
    adapter_build_digest: str
    adapter_profile_digest: str
    result_schema: str
    result_media_type: str
    result_max_bytes: int
    materialization_target_digest: str


@dataclass(frozen=True, slots=True)
class OfficialDirectoryProfile:
    registry_key: str
    api_version: str
    repository_id: int
    owner_id: int
    owner_login: str
    repository_name: str
    default_branch: str
    directory_path: str
    max_directory_bytes: int
    required_assets: tuple[tuple[str, int], ...]
    authority_digest: str
    profile_digest: str


@dataclass(frozen=True, slots=True)
class RegistryResolutionClaim:
    job_id: str
    registry_key: str
    external_object_id: str
    registry_authority_digest: str
    validator_profile_digest: str
    expected_registry_head_generation: int
    lease_fence: int


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    revision: str
    commit_sha: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    repository_id: int
    owner_id: int
    owner_login: str
    repository_name: str


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    release_id: int
    tag: str
    commit_sha: str


@dataclass(frozen=True, slots=True)
class RegistryResolutionResult:
    status: Literal["present", "absent"]
    registry: RegistrySnapshot
    entry_digest: str | None
    repository: RepositoryIdentity | None
    release: ReleaseIdentity | None
    assets: tuple[Asset, ...]

    def payload(
        self, claim: RegistryResolutionClaim, command_id: str
    ) -> dict[str, object]:
        _uuid_text(command_id, "registry_resolution_command_invalid")
        value: dict[str, object] = {
            "commandId": command_id,
            "leaseFence": claim.lease_fence,
            "registryKey": claim.registry_key,
            "externalObjectId": claim.external_object_id,
            "registryAuthorityDigest": claim.registry_authority_digest,
            "validatorProfileDigest": claim.validator_profile_digest,
            "expectedRegistryHeadGeneration": claim.expected_registry_head_generation,
            "resolutionStatus": self.status,
            "registryRevision": self.registry.revision,
            "registryCommitSha": self.registry.commit_sha,
            "registryContentDigest": self.registry.content_digest,
        }
        if self.status == "present":
            if (
                self.entry_digest is None
                or self.repository is None
                or self.release is None
                or any(asset.sha256 is None for asset in self.assets)
            ):
                raise ExecutorError("registry_resolution_result_invalid")
            value.update(
                {
                    "entryDigest": self.entry_digest,
                    "repositoryId": self.repository.repository_id,
                    "repositoryOwnerId": self.repository.owner_id,
                    "repositoryOwnerLogin": self.repository.owner_login,
                    "repositoryName": self.repository.repository_name,
                    "releaseId": self.release.release_id,
                    "releaseTag": self.release.tag,
                    "releaseCommitSha": self.release.commit_sha,
                    "assets": [
                        {
                            "assetId": asset.asset_id,
                            "name": asset.name,
                            "size": asset.size,
                            "sha256": asset.sha256,
                        }
                        for asset in self.assets
                    ],
                }
            )
        elif (
            self.entry_digest is not None
            or self.repository is not None
            or self.release is not None
            or self.assets
        ):
            raise ExecutorError("registry_resolution_result_invalid")
        value["evidenceDigest"] = sha256(_canonical_json(value)).hexdigest()
        return value


@dataclass(frozen=True, slots=True)
class UploadGrant:
    grant_receipt_id: str
    result_object_id: str
    token: str
    object_key: str
    upload_origin: str
    content_type: str
    expected_size_bytes: int


class TokenProvider(Protocol):
    def token(self) -> str: ...


class ControlPlane(Protocol):
    def claim(self, token: str) -> Claim | None: ...

    def source_plan(self, token: str, claim: Claim) -> SourcePlan: ...

    def grant(
        self,
        token: str,
        claim: Claim,
        plan: SourcePlan,
        result: bytes,
        command_id: str,
    ) -> UploadGrant: ...

    def confirm(self, token: str, claim: Claim, grant: UploadGrant) -> None: ...

    def status(self, token: str, claim: Claim) -> str: ...

    def fail(self, token: str, claim: Claim, failure_code: str, evidence_digest: str) -> None: ...


class RegistryResolutionControlPlane(Protocol):
    def registry_resolution_claim(
        self, token: str
    ) -> RegistryResolutionClaim | None: ...

    def registry_resolution_result(
        self,
        token: str,
        claim: RegistryResolutionClaim,
        result: RegistryResolutionResult,
        command_id: str,
    ) -> None: ...

    def registry_resolution_fail(
        self,
        token: str,
        claim: RegistryResolutionClaim,
        failure_code: str,
        evidence_digest: str,
        command_id: str,
    ) -> None: ...


class GitHubMetadataReader(Protocol):
    def json_object(self, path: str) -> dict[str, object]: ...

    def raw_bytes(self, path: str, limit: int) -> bytes: ...

    def release_asset_digest(
        self, owner_login: str, repository_name: str, asset: Asset
    ) -> str: ...


class SourceReader(Protocol):
    def chunks(self, plan: SourcePlan, asset: Asset) -> Iterable[bytes]: ...


class ResultUploader(Protocol):
    def upload(self, grant: UploadGrant, result: bytes) -> None: ...


class ActionsOidcProvider:
    def __init__(self, request_url: str, request_token: str, audience: str) -> None:
        if not request_token or not audience:
            raise ExecutorError("executor_oidc_configuration_missing")
        _validate_https_url(request_url, allow_query=True)
        self._request_url = request_url
        self._request_token = request_token
        self._audience = audience

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str], audience: str
    ) -> "ActionsOidcProvider":
        return cls(
            environment.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""),
            environment.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ""),
            audience,
        )

    def token(self) -> str:
        separator = "&" if "?" in self._request_url else "?"
        request = Request(
            self._request_url + separator + urlencode({"audience": self._audience}),
            headers={"Authorization": "Bearer " + self._request_token},
            method="GET",
        )
        body = _open_bytes(request, _MAX_CONTROL_BYTES, "executor_oidc_request_failed")
        value = _json_object(body, {"value"}, "executor_oidc_response_invalid")
        token = value["value"]
        if not isinstance(token, str) or not token or len(token) > 16_384:
            raise ExecutorError("executor_oidc_response_invalid")
        return token


def load_official_directory_profile(
    path: Path = _OFFICIAL_DIRECTORY_PROFILE_PATH,
) -> OfficialDirectoryProfile:
    try:
        body = path.read_bytes()
    except OSError:
        raise ExecutorError("registry_validator_profile_unavailable") from None
    if not body or len(body) > 64 * 1024:
        raise ExecutorError("registry_validator_profile_invalid")
    value = _strict_json_object(
        body,
        {
            "schema",
            "registryKey",
            "provider",
            "apiVersion",
            "directoryRepository",
            "directoryPath",
            "maxDirectoryBytes",
            "requiredReleaseAssets",
        },
        "registry_validator_profile_invalid",
    )
    repository = _object(
        value["directoryRepository"],
        {
            "repositoryId",
            "ownerId",
            "ownerLogin",
            "repositoryName",
            "defaultBranch",
        },
        "registry_validator_profile_invalid",
    )
    raw_assets = value["requiredReleaseAssets"]
    if not isinstance(raw_assets, list) or len(raw_assets) != 2:
        raise ExecutorError("registry_validator_profile_invalid")
    required_assets: list[tuple[str, int]] = []
    for raw_asset in raw_assets:
        asset = _object(
            raw_asset, {"name", "maxBytes"}, "registry_validator_profile_invalid"
        )
        name = asset["name"]
        if name not in {"main.js", "manifest.json"}:
            raise ExecutorError("registry_validator_profile_invalid")
        required_assets.append(
            (
                cast(str, name),
                _bounded_int(
                    asset["maxBytes"],
                    1,
                    64 * 1024 * 1024,
                    "registry_validator_profile_invalid",
                ),
            )
        )
    expected_assets = {
        "main.js": MAX_MAIN_BYTES,
        "manifest.json": MAX_MANIFEST_BYTES,
    }
    if (
        value["schema"] != "trans-hub.official-directory-validator-profile.v1"
        or value["registryKey"] != "official-directory"
        or value["provider"] != "github-rest"
        or value["apiVersion"] != _GITHUB_REGISTRY_API_VERSION
        or repository["repositoryId"] != _OFFICIAL_DIRECTORY_REPOSITORY_ID
        or repository["ownerId"] != _OFFICIAL_DIRECTORY_OWNER_ID
        or repository["ownerLogin"] != _OFFICIAL_DIRECTORY_OWNER
        or repository["repositoryName"] != _OFFICIAL_DIRECTORY_REPOSITORY
        or repository["defaultBranch"] != _OFFICIAL_DIRECTORY_BRANCH
        or value["directoryPath"] != _OFFICIAL_DIRECTORY_PATH
        or dict(required_assets) != expected_assets
        or len(dict(required_assets)) != 2
    ):
        raise ExecutorError("registry_validator_profile_invalid")
    max_directory_bytes = _bounded_int(
        value["maxDirectoryBytes"],
        1,
        16 * 1024 * 1024,
        "registry_validator_profile_invalid",
    )
    authority = {
        "provider": value["provider"],
        "repositoryId": repository["repositoryId"],
        "ownerId": repository["ownerId"],
        "ownerLogin": repository["ownerLogin"],
        "repositoryName": repository["repositoryName"],
        "defaultBranch": repository["defaultBranch"],
        "directoryPath": value["directoryPath"],
    }
    return OfficialDirectoryProfile(
        registry_key=cast(str, value["registryKey"]),
        api_version=cast(str, value["apiVersion"]),
        repository_id=cast(int, repository["repositoryId"]),
        owner_id=cast(int, repository["ownerId"]),
        owner_login=cast(str, repository["ownerLogin"]),
        repository_name=cast(str, repository["repositoryName"]),
        default_branch=cast(str, repository["defaultBranch"]),
        directory_path=cast(str, value["directoryPath"]),
        max_directory_bytes=max_directory_bytes,
        required_assets=tuple(sorted(required_assets)),
        authority_digest=sha256(_canonical_json(authority)).hexdigest(),
        profile_digest=sha256(_canonical_json(value)).hexdigest(),
    )


class HttpGitHubMetadataReader:
    """Read bounded GitHub metadata and one pinned directory body in memory."""

    def __init__(self, token: str | None = None) -> None:
        self._token = _github_api_token(token)

    def json_object(self, path: str) -> dict[str, object]:
        return _strict_json_object(
            self._request(path, "application/vnd.github+json", _MAX_GITHUB_METADATA_BYTES),
            None,
            "registry_github_metadata_invalid",
        )

    def raw_bytes(self, path: str, limit: int) -> bytes:
        return self._request(path, "application/vnd.github.raw+json", limit)

    def release_asset_digest(
        self, owner_login: str, repository_name: str, asset: Asset
    ) -> str:
        return GitHubReleaseAssetReader(self._token).release_asset_digest(
            owner_login, repository_name, asset
        )

    def _request(self, path: str, accept: str, limit: int) -> bytes:
        if not path.startswith("/") or "//" in path or "\\" in path:
            raise ExecutorError("registry_github_path_invalid")
        request = Request(
            _GITHUB_API_ORIGIN + path,
            headers=_github_api_headers(
                accept,
                "trans-hub-official-directory-validator",
                _GITHUB_REGISTRY_API_VERSION,
                self._token,
            ),
            method="GET",
        )
        try:
            with urlopen(request, timeout=30) as response:
                final = urlsplit(response.geturl())
                if (
                    response.status != 200
                    or final.scheme != "https"
                    or final.hostname != "api.github.com"
                    or final.username is not None
                    or final.password is not None
                ):
                    raise ExecutorError("registry_github_response_invalid", retryable=True)
                body = cast(bytes, response.read(limit + 1))
        except HTTPError:
            raise ExecutorError("registry_github_request_failed", retryable=True) from None
        except (OSError, URLError):
            raise ExecutorError("registry_github_request_failed", retryable=True) from None
        if len(body) > limit:
            raise ExecutorError("registry_github_response_too_large")
        return body


class HttpControlPlane:
    def __init__(self, api_base: str) -> None:
        _validate_https_base(api_base)
        self._base = api_base.rstrip("/")

    def registry_resolution_claim(
        self, token: str
    ) -> RegistryResolutionClaim | None:
        status, body = self._request(
            "POST",
            "/v1/public-discovery-executor/registry-resolution-claims",
            token,
        )
        if status == 204:
            return None
        value = _json_object(
            body,
            {
                "jobId",
                "registryKey",
                "externalObjectId",
                "registryAuthorityDigest",
                "validatorProfileDigest",
                "expectedRegistryHeadGeneration",
                "leaseFence",
            },
            "registry_resolution_claim_invalid",
        )
        registry_key = _identifier(
            value["registryKey"], 80, "registry_resolution_claim_invalid", extra="_-"
        )
        external_object_id = value["externalObjectId"]
        if (
            not isinstance(external_object_id, str)
            or _PLUGIN_ID.fullmatch(external_object_id) is None
        ):
            raise ExecutorError("registry_resolution_claim_invalid")
        return RegistryResolutionClaim(
            job_id=_uuid_text(value["jobId"], "registry_resolution_claim_invalid"),
            registry_key=registry_key,
            external_object_id=external_object_id,
            registry_authority_digest=_digest(
                value["registryAuthorityDigest"], "registry_resolution_claim_invalid"
            ),
            validator_profile_digest=_digest(
                value["validatorProfileDigest"], "registry_resolution_claim_invalid"
            ),
            expected_registry_head_generation=_bounded_int(
                value["expectedRegistryHeadGeneration"],
                0,
                _MAX_SAFE_INTEGER,
                "registry_resolution_claim_invalid",
            ),
            lease_fence=_positive_int(
                value["leaseFence"], "registry_resolution_claim_invalid"
            ),
        )

    def registry_resolution_result(
        self,
        token: str,
        claim: RegistryResolutionClaim,
        result: RegistryResolutionResult,
        command_id: str,
    ) -> None:
        self._request(
            "POST",
            f"/v1/public-discovery-executor/registry-resolution-jobs/{claim.job_id}/results",
            token,
            result.payload(claim, command_id),
        )

    def registry_resolution_fail(
        self,
        token: str,
        claim: RegistryResolutionClaim,
        failure_code: str,
        evidence_digest: str,
        command_id: str,
    ) -> None:
        if failure_code not in {
            "registry_resolution_retryable",
            "registry_profile_changed",
            "registry_validation_rejected",
        }:
            raise ExecutorError("registry_resolution_failure_invalid")
        _uuid_text(command_id, "registry_resolution_command_invalid")
        self._request(
            "POST",
            f"/v1/public-discovery-executor/registry-resolution-jobs/{claim.job_id}/fail",
            token,
            {
                "commandId": command_id,
                "leaseFence": claim.lease_fence,
                "registryKey": claim.registry_key,
                "externalObjectId": claim.external_object_id,
                "registryAuthorityDigest": claim.registry_authority_digest,
                "validatorProfileDigest": claim.validator_profile_digest,
                "failureCode": failure_code,
                "evidenceDigest": _digest(
                    evidence_digest, "registry_resolution_failure_invalid"
                ),
            },
        )

    def claim(self, token: str) -> Claim | None:
        status, body = self._request("POST", "/v1/public-discovery-executor/claims", token)
        if status == 204:
            return None
        value = _json_object(
            body,
            {
                "taskId",
                "registryKey",
                "externalObjectId",
                "sourceReference",
                "adapterBuildDigest",
                "leaseFence",
                "leaseExpiresAt",
            },
            "executor_claim_invalid",
        )
        task_id = _uuid_text(value["taskId"], "executor_claim_invalid")
        source_reference = _safe_reference(value["sourceReference"], "executor_claim_invalid")
        digest = _digest(value["adapterBuildDigest"], "executor_claim_invalid")
        fence = _positive_int(value["leaseFence"], "executor_claim_invalid")
        return Claim(task_id, source_reference, digest, fence)

    def source_plan(self, token: str, claim: Claim) -> SourcePlan:
        _, body = self._request(
            "GET", f"/v1/public-discovery-executor/tasks/{claim.task_id}/source-plan", token
        )
        value = _json_object(
            body,
            {
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
            },
            "executor_source_plan_invalid",
        )
        if value["provider"] != "github_release":
            raise ExecutorError("executor_source_plan_invalid")
        assets_value = value["assets"]
        if not isinstance(assets_value, list) or not 1 <= len(assets_value) <= 64:
            raise ExecutorError("executor_source_plan_invalid")
        assets: list[Asset] = []
        for raw_asset in assets_value:
            asset = _object(
                raw_asset, {"assetId", "name", "size", "sha256"}, "executor_source_plan_invalid"
            )
            name = asset["name"]
            if not isinstance(name, str) or _SAFE_NAME.fullmatch(name) is None:
                raise ExecutorError("executor_source_plan_invalid")
            assets.append(
                Asset(
                    _positive_int(asset["assetId"], "executor_source_plan_invalid"),
                    name,
                    _nonnegative_int(asset["size"], "executor_source_plan_invalid"),
                    _digest(asset["sha256"], "executor_source_plan_invalid"),
                )
            )
        primary_id = _positive_int(value["primaryAssetId"], "executor_source_plan_invalid")
        if (
            sum(asset.asset_id == primary_id for asset in assets) != 1
            or len({asset.asset_id for asset in assets}) != len(assets)
            or len({asset.name for asset in assets}) != len(assets)
        ):
            raise ExecutorError("executor_source_plan_invalid")
        manifest = [asset for asset in assets if asset.name == "manifest.json"]
        main = [asset for asset in assets if asset.name == "main.js"]
        if (
            len(manifest) != 1
            or len(main) != 1
            or not 1 <= manifest[0].size <= MAX_MANIFEST_BYTES
            or not 1 <= main[0].size <= MAX_MAIN_BYTES
            or primary_id != main[0].asset_id
        ):
            raise ExecutorError("executor_source_component_closure_incomplete")
        owner_login = _identifier(value["ownerLogin"], 39, "executor_source_plan_invalid")
        repository_name = _identifier(
            value["repositoryName"], 100, "executor_source_plan_invalid", extra="._-"
        )
        result_schema = _identifier(
            value["resultSchema"], 128, "executor_source_plan_invalid", extra="._/-"
        )
        media_type = value["resultMediaType"]
        if media_type != _RESULT_MEDIA_TYPE:
            raise ExecutorError("executor_source_plan_invalid")
        plan = SourcePlan(
            _positive_int(value["ownerId"], "executor_source_plan_invalid"),
            owner_login,
            _positive_int(value["repositoryId"], "executor_source_plan_invalid"),
            repository_name,
            _positive_int(value["releaseId"], "executor_source_plan_invalid"),
            manifest[0],
            main[0],
            _positive_int(value["projectionGeneration"], "executor_source_plan_invalid"),
            _digest(value["authorityBindingDigest"], "executor_source_plan_invalid"),
            _digest(value["sourcePlanDigest"], "executor_source_plan_invalid"),
            _digest(value["adapterBuildDigest"], "executor_source_plan_invalid"),
            _digest(value["adapterProfileDigest"], "executor_source_plan_invalid"),
            result_schema,
            media_type,
            _bounded_int(
                value["resultMaxBytes"], 1, 64 * 1024 * 1024, "executor_source_plan_invalid"
            ),
            _digest(value["materializationTargetDigest"], "executor_source_plan_invalid"),
        )
        if plan.adapter_build_digest != claim.adapter_build_digest:
            raise ExecutorError("executor_source_plan_binding_changed")
        return plan

    def grant(
        self,
        token: str,
        claim: Claim,
        plan: SourcePlan,
        result: bytes,
        command_id: str,
    ) -> UploadGrant:
        _uuid_text(command_id, "executor_grant_command_invalid")
        payload = {
            "leaseFence": claim.lease_fence,
            "grantCommandId": command_id,
            "expectedTransportDigest": sha256(result).hexdigest(),
            "expectedSizeBytes": len(result),
        }
        _, body = self._request(
            "POST",
            f"/v1/public-discovery-executor/tasks/{claim.task_id}/result-upload-grants",
            token,
            payload,
        )
        return _parse_upload_grant(body, len(result), plan.result_media_type)

    def confirm(self, token: str, claim: Claim, grant: UploadGrant) -> None:
        _, body = self._request(
            "POST",
            f"/v1/public-discovery-executor/tasks/{claim.task_id}/result-upload-confirmations",
            token,
            {
                "leaseFence": claim.lease_fence,
                "grantReceiptId": grant.grant_receipt_id,
                "resultObjectId": grant.result_object_id,
            },
        )
        value = _json_object(
            body, {"confirmationId", "taskState", "replayed"}, "executor_confirmation_invalid"
        )
        _uuid_text(value["confirmationId"], "executor_confirmation_invalid")
        if value["taskState"] != "materialization_pending" or not isinstance(
            value["replayed"], bool
        ):
            raise ExecutorError("executor_confirmation_invalid")

    def status(self, token: str, claim: Claim) -> str:
        _, body = self._request(
            "GET",
            f"/v1/public-discovery-executor/tasks/{claim.task_id}/result-upload-status?"
            + urlencode({"lease_fence": claim.lease_fence}),
            token,
        )
        try:
            value = cast(dict[str, object], json.loads(body))
        except (UnicodeError, json.JSONDecodeError):
            raise ExecutorError("executor_result_status_invalid") from None
        if not isinstance(value, dict):
            raise ExecutorError("executor_result_status_invalid")
        state = value.get("resultState")
        if state not in {"upload_grant", "materialization_pending"}:
            raise ExecutorError("executor_result_status_invalid")
        forbidden = {"token", "objectKey", "uploadOrigins", "url", "rawBytes"}
        if forbidden.intersection(value):
            raise ExecutorError("executor_result_status_invalid")
        return cast(str, state)

    def fail(self, token: str, claim: Claim, failure_code: str, evidence_digest: str) -> None:
        self._request(
            "POST",
            f"/v1/public-discovery-executor/tasks/{claim.task_id}/fail",
            token,
            {
                "leaseFence": claim.lease_fence,
                "sourceReference": claim.source_reference,
                "adapterBuildDigest": claim.adapter_build_digest,
                "failureCode": failure_code,
                "evidenceDigest": evidence_digest,
            },
        )

    def _request(
        self, method: str, path: str, token: str, payload: Mapping[str, object] | None = None
    ) -> tuple[int, bytes]:
        data = None if payload is None else _canonical_json(payload)
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self._base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                status = response.status
                body = response.read(_MAX_CONTROL_BYTES + 1)
        except HTTPError as exc:
            retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
            raise ExecutorError("executor_control_request_failed", retryable=retryable) from None
        except (OSError, URLError):
            raise ExecutorError("executor_control_request_failed", retryable=True) from None
        if len(body) > _MAX_CONTROL_BYTES or status not in {200, 201, 204}:
            raise ExecutorError("executor_control_response_invalid")
        return status, body


class GitHubReleaseAssetReader:
    """Download one frozen component asset; no server-provided locator is used."""

    def __init__(self, token: str | None = None) -> None:
        self._token = _github_api_token(token)

    def chunks(self, plan: SourcePlan, asset: Asset) -> Iterable[bytes]:
        return self._chunks(plan.owner_login, plan.repository_name, asset)

    def release_asset_digest(
        self, owner_login: str, repository_name: str, asset: Asset
    ) -> str:
        digest = sha256()
        size = 0
        for chunk in self._chunks(owner_login, repository_name, asset):
            size += len(chunk)
            digest.update(chunk)
        if size != asset.size:
            raise ExecutorError("registry_release_asset_size_mismatch")
        actual = digest.hexdigest()
        if asset.sha256 is not None and actual != asset.sha256:
            raise ExecutorError("registry_release_asset_digest_mismatch")
        return actual

    def _chunks(
        self, owner_login: str, repository_name: str, asset: Asset
    ) -> Iterable[bytes]:
        path = (
            "/repos/"
            + quote(owner_login, safe="")
            + "/"
            + quote(repository_name, safe="")
            + "/releases/assets/"
            + str(asset.asset_id)
        )
        request = Request(
            _GITHUB_API_ORIGIN + path,
            headers=_github_api_headers(
                "application/octet-stream",
                "trans-hub-public-discovery-executor",
                _GITHUB_RELEASE_API_VERSION,
                self._token,
            ),
            method="GET",
        )
        try:
            with urlopen(request, timeout=60) as response:
                if response.status != 200:
                    raise ExecutorError("executor_source_download_failed", retryable=True)
                length = response.headers.get("Content-Length")
                if length is not None and (
                    not length.isdigit() or int(length) != asset.size
                ):
                    raise ExecutorError("executor_source_size_mismatch")
                while chunk := response.read(64 * 1024):
                    yield chunk
        except HTTPError as exc:
            raise ExecutorError(
                "executor_source_download_failed",
                retryable=exc.code in {408, 425, 429, 500, 502, 503, 504},
            ) from None
        except (OSError, URLError):
            raise ExecutorError("executor_source_download_failed", retryable=True) from None


class QiniuResultUploader:
    def upload(self, grant: UploadGrant, result: bytes) -> None:
        boundary = "transhub" + uuid4().hex
        body = b"".join(
            (
                _form_field(boundary, "token", grant.token.encode("utf-8")),
                _form_field(boundary, "key", grant.object_key.encode("utf-8")),
                _form_file(boundary, grant.content_type, result),
                f"--{boundary}--\r\n".encode("ascii"),
            )
        )
        request = Request(
            grant.upload_origin,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            response_body = _open_bytes(
                request, _MAX_CONTROL_BYTES, "executor_result_upload_failed"
            )
        except ExecutorError as exc:
            if getattr(exc, "http_status", None) == 614:
                return
            raise
        try:
            value = cast(dict[str, object], json.loads(response_body))
        except (UnicodeError, json.JSONDecodeError):
            raise ExecutorError("executor_result_upload_response_invalid") from None
        if not isinstance(value, dict):
            raise ExecutorError("executor_result_upload_response_invalid")
        if value.get("key") not in {None, grant.object_key}:
            raise ExecutorError("executor_result_upload_response_invalid")


def execute_registry_resolution_one(
    *,
    tokens: TokenProvider,
    control: RegistryResolutionControlPlane,
    github: GitHubMetadataReader,
    profile: OfficialDirectoryProfile,
) -> str:
    """Resolve at most one official-directory claim without retaining source bytes."""

    claim_token = tokens.token()
    claim = control.registry_resolution_claim(claim_token)
    if claim is None:
        return "registry_resolution_no_job"
    try:
        _validate_registry_resolution_binding(claim, profile)
        result = resolve_official_directory_claim(claim, profile, github)
        result_command_id = str(uuid4())
        _retry(
            lambda: control.registry_resolution_result(
                tokens.token(), claim, result, result_command_id
            )
        )
        return "registry_resolution_" + result.status
    except ExecutorError as exc:
        failure_code = _registry_resolution_failure_code(
            exc.code, retryable=exc.retryable
        )
        failure_command_id = str(uuid4())
        try:
            _retry(
                lambda: control.registry_resolution_fail(
                    tokens.token(),
                    claim,
                    failure_code,
                    _registry_resolution_failure_evidence(
                        claim, failure_code, exc.code
                    ),
                    failure_command_id,
                )
            )
        except ExecutorError:
            raise ExecutorError(
                "registry_resolution_failure_close_unconfirmed", retryable=True
            ) from None
        raise ExecutorError(exc.code, retryable=exc.retryable) from None


def resolve_official_directory_claim(
    claim: RegistryResolutionClaim,
    profile: OfficialDirectoryProfile,
    github: GitHubMetadataReader,
) -> RegistryResolutionResult:
    _validate_registry_resolution_binding(claim, profile)
    repository_path = _github_repository_path(
        profile.owner_login, profile.repository_name
    )
    directory_repository = _retry(lambda: github.json_object(repository_path))
    _validate_directory_repository(directory_repository, profile)
    directory_commit = _retry(
        lambda: github.json_object(
            repository_path
            + "/commits/"
            + quote(profile.default_branch, safe="")
        )
    )
    commit_sha, tree_sha = _commit_identity(directory_commit)
    directory_tree = _retry(
        lambda: github.json_object(
            repository_path + "/git/trees/" + quote(tree_sha, safe="")
        )
    )
    revision, directory_size = _directory_blob_identity(
        directory_tree, tree_sha, profile
    )
    directory_body = _retry(
        lambda: github.raw_bytes(
            repository_path
            + "/contents/"
            + quote(profile.directory_path, safe="/")
            + "?"
            + urlencode({"ref": commit_sha}),
            profile.max_directory_bytes,
        )
    )
    if len(directory_body) != directory_size:
        raise ExecutorError("registry_directory_size_mismatch")
    git_blob_sha = sha1(
        f"blob {len(directory_body)}\0".encode("ascii") + directory_body,
        usedforsecurity=False,
    ).hexdigest()
    if git_blob_sha != revision:
        raise ExecutorError("registry_directory_revision_mismatch")
    registry = RegistrySnapshot(
        revision=revision,
        commit_sha=commit_sha,
        content_digest=sha256(directory_body).hexdigest(),
    )
    entry = _official_directory_entry(directory_body, claim.external_object_id)
    del directory_body
    if entry is None:
        return RegistryResolutionResult("absent", registry, None, None, None, ())

    entry_digest = sha256(_canonical_json(entry)).hexdigest()
    owner_login, repository_name = _directory_repository_reference(entry)
    plugin_repository_path = _github_repository_path(owner_login, repository_name)
    plugin_repository = _retry(
        lambda: github.json_object(plugin_repository_path)
    )
    repository = _plugin_repository_identity(
        plugin_repository, owner_login, repository_name
    )
    release_value = _retry(
        lambda: github.json_object(plugin_repository_path + "/releases/latest")
    )
    release_id, release_tag, assets = _latest_release_identity(
        release_value, profile
    )
    assets = tuple(
        replace(
            asset,
            sha256=_retry(
                lambda asset=asset: github.release_asset_digest(
                    owner_login, repository_name, asset
                )
            ),
        )
        for asset in assets
    )
    release_commit_value = _retry(
        lambda: github.json_object(
            plugin_repository_path
            + "/commits/"
            + quote(release_tag, safe="")
        )
    )
    release_commit_sha, _ = _commit_identity(release_commit_value)
    release = ReleaseIdentity(release_id, release_tag, release_commit_sha)
    return RegistryResolutionResult(
        "present", registry, entry_digest, repository, release, assets
    )


def _validate_registry_resolution_binding(
    claim: RegistryResolutionClaim, profile: OfficialDirectoryProfile
) -> None:
    if (
        claim.registry_key != profile.registry_key
        or claim.registry_authority_digest != profile.authority_digest
        or claim.validator_profile_digest != profile.profile_digest
    ):
        raise ExecutorError("registry_validator_profile_binding_changed")


def _github_repository_path(owner: str, repository: str) -> str:
    return "/repos/" + quote(owner, safe="") + "/" + quote(repository, safe="")


def _validate_directory_repository(
    value: Mapping[str, object], profile: OfficialDirectoryProfile
) -> None:
    identity = _plugin_repository_identity(
        value, profile.owner_login, profile.repository_name
    )
    if (
        identity.repository_id != profile.repository_id
        or identity.owner_id != profile.owner_id
        or value.get("default_branch") != profile.default_branch
        or value.get("archived") is not False
        or value.get("disabled") is not False
    ):
        raise ExecutorError("registry_directory_identity_mismatch")


def _plugin_repository_identity(
    value: Mapping[str, object], expected_owner: str, expected_repository: str
) -> RepositoryIdentity:
    owner = value.get("owner")
    if not isinstance(owner, dict):
        raise ExecutorError("registry_repository_identity_invalid")
    owner_login = owner.get("login")
    repository_name = value.get("name")
    full_name = value.get("full_name")
    expected_full_name = expected_owner + "/" + expected_repository
    if (
        not isinstance(owner_login, str)
        or not isinstance(repository_name, str)
        or not isinstance(full_name, str)
        or owner_login.casefold() != expected_owner.casefold()
        or repository_name.casefold() != expected_repository.casefold()
        or full_name.casefold() != expected_full_name.casefold()
        or value.get("private") is not False
    ):
        raise ExecutorError("registry_repository_identity_mismatch")
    return RepositoryIdentity(
        _positive_int(value.get("id"), "registry_repository_identity_invalid"),
        _positive_int(owner.get("id"), "registry_repository_identity_invalid"),
        _identifier(
            owner_login, 39, "registry_repository_identity_invalid", extra="-"
        ),
        _identifier(
            repository_name,
            100,
            "registry_repository_identity_invalid",
            extra="._-",
        ),
    )


def _commit_identity(value: Mapping[str, object]) -> tuple[str, str]:
    commit_sha = _sha1_digest(value.get("sha"), "registry_commit_identity_invalid")
    commit = value.get("commit")
    if not isinstance(commit, dict) or not isinstance(commit.get("tree"), dict):
        raise ExecutorError("registry_commit_identity_invalid")
    tree_sha = _sha1_digest(
        cast(dict[str, object], commit["tree"]).get("sha"),
        "registry_commit_identity_invalid",
    )
    return commit_sha, tree_sha


def _directory_blob_identity(
    value: Mapping[str, object],
    expected_tree_sha: str,
    profile: OfficialDirectoryProfile,
) -> tuple[str, int]:
    if (
        value.get("sha") != expected_tree_sha
        or value.get("truncated") is not False
        or not isinstance(value.get("tree"), list)
    ):
        raise ExecutorError("registry_directory_tree_invalid")
    matches = [
        entry
        for entry in cast(list[object], value["tree"])
        if isinstance(entry, dict) and entry.get("path") == profile.directory_path
    ]
    if len(matches) != 1:
        raise ExecutorError("registry_directory_path_invalid")
    entry = cast(dict[str, object], matches[0])
    if entry.get("type") != "blob" or entry.get("mode") != "100644":
        raise ExecutorError("registry_directory_path_invalid")
    revision = _sha1_digest(
        entry.get("sha"), "registry_directory_revision_invalid"
    )
    size = _bounded_int(
        entry.get("size"),
        1,
        profile.max_directory_bytes,
        "registry_directory_size_invalid",
    )
    return revision, size


def _official_directory_entry(
    body: bytes, external_object_id: str
) -> dict[str, object] | None:
    value = _strict_json_value(body, "registry_directory_content_invalid")
    if not isinstance(value, list) or len(value) > 100_000:
        raise ExecutorError("registry_directory_content_invalid")
    entries: dict[str, dict[str, object]] = {}
    for raw_entry in value:
        if not isinstance(raw_entry, dict) or not all(
            isinstance(key, str) for key in raw_entry
        ):
            raise ExecutorError("registry_directory_content_invalid")
        plugin_id = raw_entry.get("id")
        if (
            not isinstance(plugin_id, str)
            or _PLUGIN_ID.fullmatch(plugin_id) is None
            or plugin_id in entries
        ):
            raise ExecutorError("registry_directory_content_invalid")
        _directory_repository_reference(raw_entry)
        entries[plugin_id] = cast(dict[str, object], raw_entry)
    return entries.get(external_object_id)


def _directory_repository_reference(entry: Mapping[str, object]) -> tuple[str, str]:
    repository = entry.get("repo")
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise ExecutorError("registry_directory_entry_invalid")
    owner, name = repository.split("/", 1)
    return (
        _identifier(owner, 39, "registry_directory_entry_invalid", extra="-"),
        _identifier(
            name, 100, "registry_directory_entry_invalid", extra="._-"
        ),
    )


def _latest_release_identity(
    value: Mapping[str, object], profile: OfficialDirectoryProfile
) -> tuple[int, str, tuple[Asset, ...]]:
    if value.get("draft") is not False or value.get("prerelease") is not False:
        raise ExecutorError("registry_release_metadata_invalid")
    release_id = _positive_int(
        value.get("id"), "registry_release_metadata_invalid"
    )
    tag = _safe_reference(
        value.get("tag_name"), "registry_release_metadata_invalid"
    )
    raw_assets = value.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) > 1_000:
        raise ExecutorError("registry_release_assets_invalid")
    limits = dict(profile.required_assets)
    selected: list[Asset] = []
    for name in sorted(limits):
        matches = [
            raw_asset
            for raw_asset in raw_assets
            if isinstance(raw_asset, dict) and raw_asset.get("name") == name
        ]
        if len(matches) != 1:
            raise ExecutorError("registry_release_assets_invalid")
        raw_asset = cast(dict[str, object], matches[0])
        digest = raw_asset.get("digest")
        if raw_asset.get("state") != "uploaded":
            raise ExecutorError("registry_release_assets_invalid")
        if digest is not None and (
            not isinstance(digest, str) or not digest.startswith("sha256:")
        ):
            raise ExecutorError("registry_release_assets_invalid")
        selected.append(
            Asset(
                _positive_int(
                    raw_asset.get("id"), "registry_release_assets_invalid"
                ),
                name,
                _bounded_int(
                    raw_asset.get("size"),
                    1,
                    limits[name],
                    "registry_release_assets_invalid",
                ),
                None
                if digest is None
                else _digest(
                    digest.removeprefix("sha256:"),
                    "registry_release_assets_invalid",
                ),
            )
        )
    if len({asset.asset_id for asset in selected}) != len(selected):
        raise ExecutorError("registry_release_assets_invalid")
    return release_id, tag, tuple(selected)


def _registry_resolution_failure_code(code: str, *, retryable: bool) -> str:
    if retryable:
        return "registry_resolution_retryable"
    if "profile" in code or "binding" in code:
        return "registry_profile_changed"
    return "registry_validation_rejected"


def _registry_resolution_failure_evidence(
    claim: RegistryResolutionClaim, failure_code: str, diagnostic_code: str
) -> str:
    return sha256(
        _canonical_json(
            {
                "jobId": claim.job_id,
                "registryKey": claim.registry_key,
                "externalObjectId": claim.external_object_id,
                "registryAuthorityDigest": claim.registry_authority_digest,
                "validatorProfileDigest": claim.validator_profile_digest,
                "expectedRegistryHeadGeneration": claim.expected_registry_head_generation,
                "leaseFence": claim.lease_fence,
                "failureCode": failure_code,
                "diagnosticCode": diagnostic_code,
            }
        )
    ).hexdigest()


def execute_one(
    *,
    config: ExecutorConfig,
    tokens: TokenProvider,
    control: ControlPlane,
    source: SourceReader,
    uploader: ResultUploader,
    host_factory: Callable[[Path], OfflineExecutorHost] = lambda artifact: OfflineExecutorHost(
        executor_artifact=artifact
    ),
) -> str:
    """Execute at most one claim and return a non-sensitive outcome code."""

    if not config.ref_protected:
        raise ExecutorError("executor_ref_not_protected")
    claim_token = tokens.token()
    claim = control.claim(claim_token)
    if claim is None:
        return "executor_no_task"
    try:
        plan = control.source_plan(claim_token, claim)
        task = OfflineExecutorTask(
            adapter_artifact_digest=plan.adapter_build_digest,
            adapter_profile_digest=plan.adapter_profile_digest,
            expected_manifest_digest=plan.manifest_asset.sha256,
            expected_manifest_size=plan.manifest_asset.size,
            expected_main_digest=plan.main_asset.sha256,
            expected_main_size=plan.main_asset.size,
            materialization_target_digest=plan.materialization_target_digest,
            policy_revision=plan.projection_generation,
            result_max_bytes=plan.result_max_bytes,
        )

        def prepare() -> bytes:
            components = (
                _read_component(source, plan, "manifest", plan.manifest_asset),
                _read_component(source, plan, "main", plan.main_asset),
            )
            result = host_factory(config.artifact).prepare_result(components, task)
            if not isinstance(result, bytes):
                raise ExecutorError("executor_result_type_invalid")
            return result

        result = _retry(prepare)
        grant_command_id = str(uuid4())
        grant = _retry(
            lambda: control.grant(
                tokens.token(), claim, plan, result, grant_command_id
            )
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
        return "executor_result_handed_off"
    except (ExecutorError, OfflineExecutorHostError) as exc:
        code = exc.code if isinstance(exc, ExecutorError) else str(exc)
        failure_code = _failure_code(code, retryable=getattr(exc, "retryable", False))
        try:
            _retry(
                lambda: control.fail(
                    tokens.token(),
                    claim,
                    failure_code,
                    sha256(code.encode("ascii", "ignore")).hexdigest(),
                )
            )
        except ExecutorError:
            raise ExecutorError("executor_failure_close_unconfirmed", retryable=True) from None
        raise ExecutorError(code, retryable=getattr(exc, "retryable", False)) from None


def execute_fair_cycle(
    *,
    config: ExecutorConfig,
    tokens: TokenProvider,
    control: HttpControlPlane,
    github: GitHubMetadataReader,
    profile: OfficialDirectoryProfile,
    source: SourceReader,
    uploader: ResultUploader,
    host_factory: Callable[[Path], OfflineExecutorHost] = lambda artifact: OfflineExecutorHost(
        executor_artifact=artifact
    ),
) -> str:
    """Try one Stage A claim before one Stage B claim on every healthy run."""

    registry_error: ExecutorError | None = None
    try:
        registry_outcome = execute_registry_resolution_one(
            tokens=tokens,
            control=control,
            github=github,
            profile=profile,
        )
    except ExecutorError as exc:
        registry_error = exc
        registry_outcome = "registry_resolution_failed"
    source_outcome = execute_one(
        config=config,
        tokens=tokens,
        control=control,
        source=source,
        uploader=uploader,
        host_factory=host_factory,
    )
    if registry_error is not None:
        raise registry_error
    if (
        registry_outcome == "registry_resolution_no_job"
        and source_outcome == "executor_no_task"
    ):
        return "executor_no_job"
    return registry_outcome + ";" + source_outcome


def _retry(operation: Callable[[], "_T"], attempts: int = 3) -> "_T":
    for ordinal in range(attempts):
        try:
            return operation()
        except ExecutorError as exc:
            if not exc.retryable or ordinal + 1 == attempts:
                raise
    raise AssertionError("unreachable")


def _read_component(
    source: SourceReader,
    plan: SourcePlan,
    role: Literal["manifest", "main"],
    asset: Asset,
) -> OfflineExecutorComponent:
    expected_name = "manifest.json" if role == "manifest" else "main.js"
    if asset.name != expected_name or asset.size < 1:
        raise ExecutorError("executor_source_component_invalid")
    chunks = source.chunks(plan, asset)
    content = bytearray()
    digest = sha256()
    try:
        for chunk in chunks:
            if not isinstance(chunk, bytes) or not chunk:
                raise ExecutorError("executor_source_chunk_invalid")
            if len(content) + len(chunk) > asset.size:
                raise ExecutorError("executor_source_size_mismatch")
            content.extend(chunk)
            digest.update(chunk)
    finally:
        close_chunks = getattr(chunks, "close", None)
        if callable(close_chunks):
            close_chunks()
    if (
        asset.sha256 is None
        or len(content) != asset.size
        or digest.hexdigest() != asset.sha256
    ):
        raise ExecutorError("executor_source_evidence_mismatch")
    return OfflineExecutorComponent(role, expected_name, bytes(content))


_T = TypeVar("_T")


def _failure_code(code: str, *, retryable: bool) -> str:
    if retryable:
        return "executor_workflow_failed"
    if "source" in code or "component" in code:
        return "source_validation_rejected"
    if "profile" in code or "binding" in code:
        return "registry_projection_changed"
    if "adapter" in code or "result_" in code:
        return "adapter_validation_rejected"
    return "executor_workflow_failed"


def _parse_upload_grant(body: bytes, expected_size: int, media_type: str) -> UploadGrant:
    value = _json_object(
        body,
        {
            "grantReceiptId",
            "resultObjectId",
            "grantExpiresAt",
            "provider",
            "bucket",
            "objectKey",
            "token",
            "uploadOrigins",
            "contentType",
            "expectedSizeBytes",
            "grantState",
            "replayed",
        },
        "executor_upload_grant_invalid",
    )
    origins = value["uploadOrigins"]
    if value["provider"] != "qiniu_kodo" or not isinstance(origins, list) or len(origins) != 1:
        raise ExecutorError("executor_upload_grant_invalid")
    origin = origins[0]
    if not isinstance(origin, str):
        raise ExecutorError("executor_upload_grant_invalid")
    _validate_https_url(origin, allow_query=False)
    token = value["token"]
    key = value["objectKey"]
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 4096
        or not isinstance(key, str)
        or re.fullmatch(r"public-discovery/results/[0-9a-f]{64}[.]canonical-json", key) is None
        or value["contentType"] != media_type
        or value["expectedSizeBytes"] != expected_size
        or value["grantState"] != "upload_grant"
    ):
        raise ExecutorError("executor_upload_grant_invalid")
    return UploadGrant(
        _uuid_text(value["grantReceiptId"], "executor_upload_grant_invalid"),
        _uuid_text(value["resultObjectId"], "executor_upload_grant_invalid"),
        token,
        key,
        origin,
        media_type,
        expected_size,
    )


def _open_bytes(request: Request, limit: int, code: str) -> bytes:
    try:
        with urlopen(request, timeout=30) as response:
            body = cast(bytes, response.read(limit + 1))
            if response.status != 200 or len(body) > limit:
                raise ExecutorError(code)
            return body
    except HTTPError as exc:
        error = ExecutorError(code, retryable=exc.code in {408, 425, 429, 500, 502, 503, 504})
        error.http_status = exc.code  # type: ignore[attr-defined]
        raise error from None
    except (OSError, URLError):
        raise ExecutorError(code, retryable=True) from None


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _json_object(body: bytes, keys: set[str], code: str) -> dict[str, object]:
    value = _strict_json_value(body, code)
    return _object(value, keys, code)


def _strict_json_object(
    body: bytes, keys: set[str] | None, code: str
) -> dict[str, object]:
    value = _strict_json_value(body, code)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ExecutorError(code)
    if keys is not None and set(value) != keys:
        raise ExecutorError(code)
    return cast(dict[str, object], value)


def _strict_json_value(body: bytes, code: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_key")
            value[key] = item
        return value

    def reject_constant(_value: str) -> object:
        raise ValueError("non_finite_number")

    try:
        value = json.loads(
            body,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ExecutorError(code) from None
    _validate_json_value(value, code)
    return value


def _validate_json_value(value: object, code: str) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 64:
            raise ExecutorError(code)
        if item is None or isinstance(item, (bool, int)):
            continue
        if isinstance(item, float):
            if not isfinite(item):
                raise ExecutorError(code)
            continue
        if isinstance(item, str):
            if any("\ud800" <= character <= "\udfff" for character in item):
                raise ExecutorError(code)
            continue
        if isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
            continue
        if isinstance(item, dict):
            for key, child in item.items():
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
            continue
        raise ExecutorError(code)


def _object(value: object, keys: set[str], code: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or not all(isinstance(key, str) for key in value)
    ):
        raise ExecutorError(code)
    return cast(dict[str, object], value)


def _uuid_text(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise ExecutorError(code)
    try:
        return str(UUID(value))
    except ValueError:
        raise ExecutorError(code) from None


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ExecutorError(code)
    return value


def _sha1_digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA1.fullmatch(value) is None:
        raise ExecutorError(code)
    return value


def _positive_int(value: object, code: str) -> int:
    return _bounded_int(value, 1, _MAX_SAFE_INTEGER, code)


def _nonnegative_int(value: object, code: str) -> int:
    return _bounded_int(value, 0, 64 * 1024 * 1024, code)


def _bounded_int(value: object, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ExecutorError(code)
    return value


def _safe_reference(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or _SAFE_REFERENCE.fullmatch(value) is None
        or "//" in value
        or "\\" in value
    ):
        raise ExecutorError(code)
    return value


def _identifier(value: object, maximum: int, code: str, *, extra: str = "-") -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ExecutorError(code)
    if any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" + extra
        for character in value
    ):
        raise ExecutorError(code)
    if value in {".", ".."} or "//" in value:
        raise ExecutorError(code)
    return value


def _validate_https_base(value: str) -> None:
    _validate_https_url(value, allow_query=False)
    parsed = urlsplit(value)
    if parsed.path not in {"", "/", "/api"}:
        raise ExecutorError("executor_api_base_invalid")


def _validate_https_url(value: str, *, allow_query: bool) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ExecutorError("executor_url_invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise ExecutorError("executor_url_invalid")


def _form_field(boundary: str, name: str, value: bytes) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii")
        + value
        + b"\r\n"
    )


def _form_file(boundary: str, content_type: str, value: bytes) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="result.json"\r\nContent-Type: {content_type}\r\n\r\n'.encode(
            "ascii"
        )
        + value
        + b"\r\n"
    )


def _github_api_token(token: str | None) -> str | None:
    if token is None or token == "":
        return None
    if "\r" in token or "\n" in token:
        raise ExecutorError("executor_github_token_invalid")
    return token


def _github_api_headers(
    accept: str, user_agent: str, api_version: str, token: str | None
) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": user_agent,
        "X-GitHub-Api-Version": api_version,
    }
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    return headers


def main() -> int:
    try:
        config = ExecutorConfig.from_environment(os.environ)
        tokens = ActionsOidcProvider.from_environment(os.environ, config.oidc_audience)
        control = HttpControlPlane(config.api_base)
        outcome = execute_fair_cycle(
            config=config,
            tokens=tokens,
            control=control,
            github=HttpGitHubMetadataReader(os.environ.get("GITHUB_TOKEN")),
            profile=load_official_directory_profile(),
            source=GitHubReleaseAssetReader(os.environ.get("GITHUB_TOKEN")),
            uploader=QiniuResultUploader(),
        )
    except (ExecutorError, OfflineExecutorHostError) as exc:
        code = exc.code if isinstance(exc, ExecutorError) else str(exc)
        print("public_discovery_executor_failed:" + code, file=sys.stderr)
        return 1
    print(outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
