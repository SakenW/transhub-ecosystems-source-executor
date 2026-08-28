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
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, Protocol, TypeVar, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from .offline_executor_host import (
    OfflineExecutorHost,
    OfflineExecutorHostError,
    OfflineExecutorTask,
)

_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REFERENCE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+=:/-]{0,511}$")
_SAFE_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@() -]{0,254}$")
_RESULT_MEDIA_TYPE: Final = "application/vnd.trans-hub.public-discovery-result+json"
_GITHUB_API_ORIGIN: Final = "https://api.github.com"
_GITHUB_API_VERSION: Final = "2022-11-28"
_MAX_CONTROL_BYTES: Final = 1024 * 1024
_MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991


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
    sha256: str


@dataclass(frozen=True, slots=True)
class SourcePlan:
    owner_id: int
    owner_login: str
    repository_id: int
    repository_name: str
    release_id: int
    primary_asset: Asset
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


class SourceReader(Protocol):
    def chunks(self, plan: SourcePlan) -> Iterable[bytes]: ...


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


class HttpControlPlane:
    def __init__(self, api_base: str) -> None:
        _validate_https_base(api_base)
        self._base = api_base.rstrip("/")

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
        primary = [asset for asset in assets if asset.asset_id == primary_id]
        if len(primary) != 1 or len({asset.asset_id for asset in assets}) != len(assets):
            raise ExecutorError("executor_source_plan_invalid")
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
            primary[0],
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
            "projectionGeneration": plan.projection_generation,
            "authorityBindingDigest": plan.authority_binding_digest,
            "sourcePlanDigest": plan.source_plan_digest,
            "adapterBuildDigest": plan.adapter_build_digest,
            "adapterProfileDigest": plan.adapter_profile_digest,
            "resultSchema": plan.result_schema,
            "resultMediaType": plan.result_media_type,
            "resultMaxBytes": plan.result_max_bytes,
            "materializationTargetDigest": plan.materialization_target_digest,
            "expectedTransportDigest": sha256(result).hexdigest(),
            "expectedSizeBytes": len(result),
        }
        payload["requestDigest"] = sha256(_canonical_json(payload)).hexdigest()
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
    """Download only the frozen primary asset; no server-provided locator is used."""

    def chunks(self, plan: SourcePlan) -> Iterable[bytes]:
        path = (
            "/repos/"
            + quote(plan.owner_login, safe="")
            + "/"
            + quote(plan.repository_name, safe="")
            + "/releases/assets/"
            + str(plan.primary_asset.asset_id)
        )
        request = Request(
            _GITHUB_API_ORIGIN + path,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "trans-hub-public-discovery-executor",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=60) as response:
                if response.status != 200:
                    raise ExecutorError("executor_source_download_failed", retryable=True)
                length = response.headers.get("Content-Length")
                if length is not None and (
                    not length.isdigit() or int(length) != plan.primary_asset.size
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
            expected_raw_digest=plan.primary_asset.sha256,
            expected_raw_size=plan.primary_asset.size,
            materialization_target_digest=plan.materialization_target_digest,
            policy_revision=plan.projection_generation,
            result_max_bytes=plan.result_max_bytes,
        )

        def prepare() -> bytes:
            raw_chunks = source.chunks(plan)
            try:
                result = host_factory(config.artifact).prepare_result(raw_chunks, task)
                if not isinstance(result, bytes):
                    raise ExecutorError("executor_result_type_invalid")
                return result
            finally:
                close_chunks = getattr(raw_chunks, "close", None)
                if callable(close_chunks):
                    close_chunks()

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


def _retry(operation: Callable[[], "_T"], attempts: int = 3) -> "_T":
    for ordinal in range(attempts):
        try:
            return operation()
        except ExecutorError as exc:
            if not exc.retryable or ordinal + 1 == attempts:
                raise
    raise AssertionError("unreachable")


_T = TypeVar("_T")


def _failure_code(code: str, *, retryable: bool) -> str:
    if retryable:
        return "executor_workflow_failed"
    if "source" in code or "raw" in code:
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
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise ExecutorError(code) from None
    return _object(value, keys, code)


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


def main() -> int:
    try:
        config = ExecutorConfig.from_environment(os.environ)
        tokens = ActionsOidcProvider.from_environment(os.environ, config.oidc_audience)
        outcome = execute_one(
            config=config,
            tokens=tokens,
            control=HttpControlPlane(config.api_base),
            source=GitHubReleaseAssetReader(),
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
