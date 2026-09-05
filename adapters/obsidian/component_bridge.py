"""Bounded stdin/stdout bridge for trusted Obsidian release components.

The bridge accepts exactly ``manifest.json`` and ``main.js`` as inert bytes and
returns only the Adapter's canonical source catalog.  It never accepts an
archive, path, URL, command, credential, or output location from the caller.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from .adapter_worker import AdapterContractError, build_snapshot

PUBLIC_EXECUTOR_PROTOCOL: Final = "trans-hub.obsidian-public-executor.v2"
PUBLIC_RESULT_PROTOCOL: Final = "trans-hub.public-discovery-result"
PUBLIC_RESULT_REVISION: Final = 1
MAX_MANIFEST_BYTES: Final = 1024 * 1024
MAX_MAIN_BYTES: Final = 63 * 1024 * 1024
MAX_COMPONENT_BYTES: Final = MAX_MANIFEST_BYTES + MAX_MAIN_BYTES
MAX_REQUEST_BYTES: Final = ((MAX_COMPONENT_BYTES + 2) // 3) * 4 + 4_096
MAX_RESPONSE_BYTES: Final = 64 * 1024 * 1024
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,128}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_CONTENT_SCOPE = re.compile(r"^[a-z][a-z0-9]*(?:[-.:][a-z0-9]+)*$")
_MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991
_MAX_CORE_CATALOG_BYTES: Final = 16 * 1024 * 1024
_MAX_CORE_CATALOG_UNITS: Final = 50_000
_MAX_CORE_SOURCE_TEXT_BYTES: Final = 1024 * 1024
_SOURCE_DEFINITIONS: Final = {
    "runtime": ("main.js", "javascript"),
    "metadata": ("manifest.json", "json"),
    "documentation": ("README.md", "markdown"),
}


class ObsidianComponentBridgeError(ValueError):
    """A deterministic protocol failure safe to expose to the trusted host."""


@dataclass(frozen=True, slots=True)
class PublicComponent:
    role: str
    name: str
    content: bytes


def handle_public_request(raw: bytes) -> bytes:
    """Return the complete canonical public-discovery result envelope."""

    request = _parse_unique_json(raw)
    if not isinstance(request, dict) or set(request) != {
        "authority_resource_version",
        "components",
        "materialization_target_digest",
        "policy_revision",
        "protocol",
    }:
        raise ObsidianComponentBridgeError("obsidian_public_executor_request_invalid")
    if request["protocol"] != PUBLIC_EXECUTOR_PROTOCOL:
        raise ObsidianComponentBridgeError("obsidian_public_executor_protocol_invalid")
    materialization_target_digest = request["materialization_target_digest"]
    authority_resource_version = request["authority_resource_version"]
    if (
        not isinstance(authority_resource_version, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", authority_resource_version)
        is None
    ):
        raise ObsidianComponentBridgeError("obsidian_public_executor_request_invalid")
    if not isinstance(materialization_target_digest, str) or not _DIGEST.fullmatch(
        materialization_target_digest
    ):
        raise ObsidianComponentBridgeError("obsidian_public_executor_target_invalid")
    policy_revision = request["policy_revision"]
    if (
        isinstance(policy_revision, bool)
        or not isinstance(policy_revision, int)
        or policy_revision < 1
        or policy_revision > _MAX_SAFE_INTEGER
    ):
        raise ObsidianComponentBridgeError("obsidian_public_executor_request_invalid")
    components = _decode_components(request["components"])
    _validate_public_manifest_fields(components)
    snapshot = _adapter_snapshot(components, authority_resource_version)
    validate_public_source_catalog(snapshot["source_catalog"])
    response = _canonical_json(
        {
            "result": {
                "materialization_target_digest": materialization_target_digest,
                "protocol": PUBLIC_RESULT_PROTOCOL,
                "revision": PUBLIC_RESULT_REVISION,
            },
            "source_catalog": snapshot["source_catalog"],
        }
    )
    if len(response) > MAX_RESPONSE_BYTES:
        raise ObsidianComponentBridgeError("obsidian_component_bridge_response_too_large")
    return response


def _decode_components(value: object) -> tuple[PublicComponent, ...]:
    if not isinstance(value, list) or len(value) != 2:
        raise ObsidianComponentBridgeError("obsidian_component_closure_invalid")
    selected: dict[str, PublicComponent] = {}
    definitions = {
        "manifest": ("manifest.json", MAX_MANIFEST_BYTES),
        "main": ("main.js", MAX_MAIN_BYTES),
    }
    total = 0
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"content_base64", "name", "role"}:
            raise ObsidianComponentBridgeError("obsidian_component_closure_invalid")
        role = raw["role"]
        name = raw["name"]
        encoded = raw["content_base64"]
        if (
            not isinstance(role, str)
            or role not in definitions
            or role in selected
            or name != definitions[role][0]
            or not isinstance(encoded, str)
        ):
            raise ObsidianComponentBridgeError("obsidian_component_closure_invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ObsidianComponentBridgeError(
                "obsidian_component_base64_invalid"
            ) from exc
        if not content or len(content) > definitions[role][1]:
            raise ObsidianComponentBridgeError("obsidian_component_size_invalid")
        total += len(content)
        if total > MAX_COMPONENT_BYTES:
            raise ObsidianComponentBridgeError("obsidian_component_size_invalid")
        selected[role] = PublicComponent(role, name, content)
    if set(selected) != set(definitions):
        raise ObsidianComponentBridgeError("obsidian_component_closure_incomplete")
    return tuple(selected[role] for role in ("manifest", "main"))


def _validate_public_manifest_fields(
    components: tuple[PublicComponent, ...],
) -> None:
    manifest_component = next(
        (component for component in components if component.role == "manifest"), None
    )
    if manifest_component is None:
        raise ObsidianComponentBridgeError("obsidian_public_executor_manifest_invalid")
    manifest = _parse_unique_json(manifest_component.content)
    if not isinstance(manifest, dict):
        raise ObsidianComponentBridgeError("obsidian_public_executor_manifest_invalid")
    name = manifest.get("name")
    version = manifest.get("version")
    description = manifest.get("description")
    if (
        _bounded_text(name, 240) is None
        or _bounded_text(version, 240) is None
        or not isinstance(description, str)
        or not description.strip()
        or len(description.strip().encode("utf-8")) > _MAX_CORE_SOURCE_TEXT_BYTES
    ):
        raise ObsidianComponentBridgeError("obsidian_public_executor_manifest_invalid")


def validate_public_source_catalog(value: object) -> None:
    """Apply the Core revision-two limits before a result can be uploaded."""

    if not isinstance(value, dict) or set(value) != {
        "protocol",
        "resource",
        "revision",
        "sources",
        "stream",
        "units",
    }:
        raise ObsidianComponentBridgeError("obsidian_public_executor_catalog_invalid")
    if (
        value["protocol"] != "trans-hub.canonical-source-catalog"
        or value["revision"] != 2
        or len(_canonical_json({"source_catalog": value})) > _MAX_CORE_CATALOG_BYTES
        or not _jsonb_text_valid(value)
    ):
        raise ObsidianComponentBridgeError("obsidian_public_executor_catalog_invalid")
    resource = value["resource"]
    stream = value["stream"]
    sources = value["sources"]
    units = value["units"]
    if (
        not isinstance(resource, dict)
        or set(resource)
        != {
            "content_digest",
            "name",
            "object_kind_key",
            "resource_key",
            "version",
            "version_scheme",
        }
        or not isinstance(stream, dict)
        or set(stream) != {"locale", "stream_key"}
        or not isinstance(sources, list)
        or not sources
        or len(sources) > len(_SOURCE_DEFINITIONS)
        or not isinstance(units, list)
        or not units
        or len(units) > _MAX_CORE_CATALOG_UNITS
    ):
        raise ObsidianComponentBridgeError("obsidian_public_executor_catalog_invalid")
    resource_key = _bounded_text(resource.get("resource_key"), 200)
    if (
        resource_key is None
        or _bounded_text(resource.get("name"), 240) is None
        or _bounded_text(resource.get("version"), 240) is None
        or resource.get("version_scheme") != "semver"
        or resource.get("object_kind_key") != "plugin"
        or not isinstance(resource.get("content_digest"), str)
        or _DIGEST.fullmatch(cast(str, resource["content_digest"])) is None
        or stream.get("stream_key") != f"community-resource:{resource_key}"
        or stream.get("locale") != "en"
    ):
        raise ObsidianComponentBridgeError("obsidian_public_executor_catalog_invalid")

    source_keys: set[str] = set()
    logical_paths: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "format_family",
            "key",
            "logical_path",
        }:
            raise ObsidianComponentBridgeError("obsidian_public_executor_catalog_invalid")
        key = _bounded_text(source.get("key"), 240)
        logical_path = _bounded_text(source.get("logical_path"), 512)
        format_family = _bounded_text(source.get("format_family"), 80)
        if (
            key is None
            or logical_path is None
            or format_family is None
            or _SOURCE_DEFINITIONS.get(key) != (logical_path, format_family)
            or key in source_keys
            or logical_path in logical_paths
        ):
            raise ObsidianComponentBridgeError("obsidian_public_executor_catalog_invalid")
        source_keys.add(key)
        logical_paths.add(logical_path)

    unit_keys: set[str] = set()
    assigned_sources: set[str] = set()
    for unit in units:
        if not isinstance(unit, dict) or set(unit) != {
            "context",
            "format_signature",
            "key",
            "placeholder_signature",
            "source_key",
            "text",
        }:
            raise ObsidianComponentBridgeError("obsidian_public_executor_catalog_invalid")
        key = _bounded_text(unit.get("key"), 240)
        text = unit.get("text")
        source_key = unit.get("source_key")
        placeholder = unit.get("placeholder_signature")
        if (
            key is None
            or key in unit_keys
            or not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-8")) > _MAX_CORE_SOURCE_TEXT_BYTES
            or not isinstance(source_key, str)
            or source_key not in source_keys
            or not isinstance(placeholder, str)
            or len(placeholder) > 4096
            or unit.get("format_signature") != "plain-text-v1"
            or not _valid_core_context(unit.get("context"))
        ):
            raise ObsidianComponentBridgeError("obsidian_public_executor_catalog_invalid")
        unit_keys.add(key)
        assigned_sources.add(source_key)
    if assigned_sources != source_keys:
        raise ObsidianComponentBridgeError("obsidian_public_executor_catalog_invalid")


def _bounded_text(value: object, limit: int) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value) <= limit else None


def _valid_core_context(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    scopes = value.get("content_scopes")
    if (
        not isinstance(scopes, list)
        or not scopes
        or len(scopes) > 64
        or any(
            not isinstance(scope, str)
            or len(scope) > 64
            or _CONTENT_SCOPE.fullmatch(scope) is None
            for scope in scopes
        )
        or scopes != sorted(set(scopes))
    ):
        return False
    coverage = value.get("upstream_locale_coverage")
    return isinstance(coverage, list) and len(coverage) <= 256 and not any(
        not isinstance(locale, str)
        or len(locale) > 64
        or _LANGUAGE_TAG.fullmatch(locale) is None
        for locale in coverage
    ) and coverage == sorted(set(coverage))


def _jsonb_text_valid(value: object) -> bool:
    if isinstance(value, str):
        return "\x00" not in value and not any(
            0xD800 <= ord(character) <= 0xDFFF for character in value
        )
    if isinstance(value, list):
        return all(_jsonb_text_valid(item) for item in value)
    if isinstance(value, dict):
        return all(
            _jsonb_text_valid(key) and _jsonb_text_valid(item)
            for key, item in value.items()
        )
    return True


def _adapter_snapshot(
    closure: tuple[PublicComponent, ...],
    authority_resource_version: str,
) -> dict[str, object]:
    components = {item.role: item.content for item in closure}
    locale_components: dict[str, tuple[str, bytes]] = {}
    for item in closure:
        role = item.role
        if role.startswith("locale:"):
            locale = role.removeprefix("locale:")
            locale_components[locale] = (
                item.name,
                item.content,
            )
    raw = build_snapshot(
        components["manifest"],
        components["main"],
        registry_metadata_content=components.get("registry-metadata"),
        readme_content=components.get("readme"),
        native_locale_components=locale_components,
        authority_resource_version=authority_resource_version,
    )
    snapshot = _parse_unique_json(raw)
    if not isinstance(snapshot, dict) or not isinstance(
        snapshot.get("source_catalog"), dict
    ):
        raise ObsidianComponentBridgeError("obsidian_component_bridge_adapter_output_invalid")
    return cast(dict[str, object], snapshot)


def _parse_unique_json(raw: bytes) -> object:
    def unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ObsidianComponentBridgeError(
                    "obsidian_component_bridge_json_duplicate_key"
                )
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
        )
    except ObsidianComponentBridgeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObsidianComponentBridgeError(
            "obsidian_component_bridge_json_invalid"
        ) from exc


def _canonical_json(value: object) -> bytes:
    """Serialize the integer-only Adapter result with RFC 8785 ordering."""

    return _canonical_text(value).encode("utf-8")


def _canonical_text(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ObsidianComponentBridgeError("obsidian_public_executor_json_invalid")
        return str(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ObsidianComponentBridgeError(
                "obsidian_public_executor_json_invalid"
            ) from exc
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_text(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ObsidianComponentBridgeError("obsidian_public_executor_json_invalid")
        try:
            keys = sorted(
                value, key=lambda key: key.encode("utf-16-be", errors="strict")
            )
        except UnicodeEncodeError as exc:
            raise ObsidianComponentBridgeError(
                "obsidian_public_executor_json_invalid"
            ) from exc
        return "{" + ",".join(
            f"{_canonical_text(key)}:{_canonical_text(value[key])}" for key in keys
        ) + "}"
    raise ObsidianComponentBridgeError("obsidian_public_executor_json_invalid")


def _error_response(code: str, protocol: str = PUBLIC_EXECUTOR_PROTOCOL) -> bytes:
    safe_code = (
        code if _ERROR_CODE.fullmatch(code) else "obsidian_component_bridge_failed"
    )
    return _canonical_json(
        {
            "error": {"code": safe_code},
            "protocol": protocol,
        }
    )


def public_main() -> None:
    _disable_core_dumps()
    _main(handle_public_request, PUBLIC_EXECUTOR_PROTOCOL)


def _disable_core_dumps() -> None:
    try:
        import resource

        _, hard_limit = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, hard_limit))
    except (ImportError, OSError, ValueError) as exc:
        sys.stdout.buffer.write(
            _error_response(
                "obsidian_public_executor_core_dump_disable_failed",
                PUBLIC_EXECUTOR_PROTOCOL,
            )
        )
        raise SystemExit(2) from exc


def _main(handler: Callable[[bytes], bytes], error_protocol: str) -> None:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        sys.stdout.buffer.write(
            _error_response("obsidian_component_bridge_request_too_large", error_protocol)
        )
        raise SystemExit(2)
    try:
        response = handler(raw)
    except (
        AdapterContractError,
        ObsidianComponentBridgeError,
    ) as exc:
        sys.stdout.buffer.write(_error_response(str(exc), error_protocol))
        raise SystemExit(2) from None
    except Exception:
        sys.stdout.buffer.write(
            _error_response("obsidian_component_bridge_failed", error_protocol)
        )
        raise SystemExit(2) from None
    sys.stdout.buffer.write(response)


if __name__ == "__main__":
    public_main()
