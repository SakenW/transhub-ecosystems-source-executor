#!/usr/bin/env python3
"""Deterministic static UI scanner for official Obsidian plugin releases.

This file is also the immutable artifact executed by Adapter Plane.  It stays
stdlib-only so the sandbox does not need the API process environment or any
ambient dependency, credential, or network access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from array import array
from bisect import bisect_right
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, Literal, NamedTuple, TypedDict, cast

CONTRACT_REVISION: Final = 16
PARSER_ID: Final = "obsidian-plugin-ui-structured-v16"
PLUGIN_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
LOCALE_ROLE_PATTERN: Final = re.compile(
    r"^locale:([A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*)(?::([a-f0-9]{12}))?$"
)
MAX_LOCALE_COMPONENT_BYTES: Final = 4 * 1024 * 1024
MAX_LOCALE_ENTRIES: Final = 10_000
MAX_LOCALE_DEPTH: Final = 16
MAX_README_COMPONENT_BYTES: Final = 1024 * 1024
QUOTED: Final = r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)'
QUOTED_NO_CAPTURE: Final = r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)'
UI_CALL: Final = re.compile(
    rf"(?:Notice|setText|setButtonText|setName|setDesc|setPlaceholder|"
    rf"setTooltip|setTitle|addHeading|appendText)\s*\(\s*{QUOTED}"
)
OPTION_CALL: Final = re.compile(rf"addOption\s*\(\s*{QUOTED}\s*,\s*{QUOTED}")
UI_PROPERTY: Final = re.compile(
    rf"(?:name|description|text|placeholder|label|tooltip|title|header|desc|"
    rf"message|buttonText|ariaLabel|caption|subtitle|summary|warning|error|"
    rf"success|hint)\s*:\s*{QUOTED}"
)
TEXT_CONTENT_ASSIGNMENT: Final = re.compile(rf"\.textContent\s*=\s*{QUOTED}")
INNER_TEXT_ASSIGNMENT: Final = re.compile(rf"\.innerText\s*=\s*{QUOTED}")
INNER_HTML_ASSIGNMENT: Final = re.compile(rf"\.innerHTML\s*=\s*{QUOTED}")
OBSIDIAN_CREATE_TEXT: Final = re.compile(
    rf"\b(?:createEl|createSpan|createDiv|createButton)\s*\(\s*"
    rf"(?:{QUOTED_NO_CAPTURE}\s*,\s*)?\{{[^{{}}]{{0,512}}?\btext\s*:\s*{QUOTED}"
)
REACT_DEFAULT_CREATE_ELEMENT_CHILD: Final = re.compile(
    rf"\b[A-Za-z_$][A-Za-z0-9_$]*\.default\.createElement\(\s*"
    rf"(?:{QUOTED_NO_CAPTURE}|[A-Za-z_$][A-Za-z0-9_$]*)\s*,\s*"
    rf"(?:null|\{{[^{{}}]{{0,4096}}\}})\s*,\s*{QUOTED}"
)
REACT_DEFAULT_CREATE_ELEMENT_PROPERTY: Final = re.compile(
    rf"\b[A-Za-z_$][A-Za-z0-9_$]*\.default\.createElement\(\s*"
    rf"(?:{QUOTED_NO_CAPTURE}|[A-Za-z_$][A-Za-z0-9_$]*)\s*,\s*"
    rf"\{{[^{{}}]{{0,4096}}?\b(?:placeholder|title|aria-label)\s*:\s*{QUOTED}"
)
PLACEHOLDER: Final = re.compile(
    r"\$\{[^}]+\}|\{\{[^}]+\}\}|\{\d+\}|%[sdif]|"
    r"</?[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][\w:.-]*"
    r"(?:=(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*\s*/?>"
)
README_HEADING: Final = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
README_LIST_ITEM: Final = re.compile(r"^\s{0,3}(?:[-+*]|\d+[.)])\s+")
README_BLOCKQUOTE: Final = re.compile(r"^\s{0,3}>")
README_FENCE_START: Final = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
README_TABLE_ROW: Final = re.compile(r"^\s*\|?.*\|.*\|?\s*$")
README_HORIZONTAL_RULE: Final = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
README_INLINE_CODE: Final = re.compile(r"(`+)([^`]*?)\1")
README_INLINE_LINK: Final = re.compile(r"(!?)\[([^\]]*)\]\((?:\\.|[^)])*\)")
README_REFERENCE_LINK: Final = re.compile(r"(!?)\[([^\]]*)\]\[[^\]]*\]")
README_AUTOLINK: Final = re.compile(r"<https?://[^>]+>")
README_HTML_TAG: Final = re.compile(r"</?[A-Za-z][^>]*>")
README_DYNAMIC_TOKEN: Final = re.compile(r"\{\{th:expr:\d+\}\}")
README_LINKED_IMAGE: Final = re.compile(
    r"\[\s*(?:!\[[^\]]*\]\((?:\\.|[^)])*\)|<img\b[^>]*>)" r"\s*\]\((?:\\.|[^)])*\)",
    re.IGNORECASE,
)
DYNAMIC_PLACEHOLDER_PREFIX: Final = "th:expr:"
UI_CALL_NAMES: Final = frozenset(
    {
        "Notice",
        "setText",
        "setButtonText",
        "setName",
        "setDesc",
        "setPlaceholder",
        "setTooltip",
        "setTitle",
        "addHeading",
        "appendText",
    }
)
UI_PROPERTY_NAMES: Final = frozenset(
    {
        "name",
        "description",
        "text",
        "placeholder",
        "label",
        "tooltip",
        "title",
        "header",
        "desc",
        "message",
        "buttonText",
        "ariaLabel",
        "caption",
        "subtitle",
        "summary",
        "warning",
        "error",
        "success",
        "hint",
    }
)
# DOM text sinks assigned through member expressions (Svelte compiled output
# and common plugin code): `p1.textContent = "..."`, `this.summary.innerText =
# "..."`, `button0.innerHTML = "<div>Add Item</div>"`.
DOM_TEXT_SINK_PROPERTIES: Final = frozenset({"textContent", "innerText", "innerHTML"})
# Obsidian DOM creation helpers accepting a display-text option:
# `container.createEl("h4", { text: "..." })` and the createSpan/createDiv/
# createButton wrappers.
OBSIDIAN_CREATE_CALL_NAMES: Final = frozenset(
    {"createEl", "createSpan", "createDiv", "createButton"}
)
SETTINGS_SCHEMA_MIN_ENTRIES: Final = 3
SETTINGS_SCHEMA_MAX_PARENT_TOKENS: Final = 20_000
SETTINGS_SCHEMA_MAX_ENTRY_TOKENS: Final = 500
UI_TEXT_DICTIONARY_MIN_GROUPS: Final = 3
UI_TEXT_DICTIONARY_MIN_VALUES: Final = 30
UI_TEXT_DICTIONARY_MIN_TITLE_RATIO: Final = 0.85
UI_TEXT_DICTIONARY_MAX_DEPTH: Final = 3
ENGLISH_SCHEMA_STOP_WORDS: Final = re.compile(
    r"\b(?:the|of|to|and|for|with|from|is|are|show|select|choose|display|"
    r"when|how|if|not|all|new|default|file|folder|note|list|view|setting|"
    r"option|enable|disable|sort|group|title|name|value|item|this|that|"
    r"you|your|will|can|also|only|size|color|icon|date|time|field|property|"
    r"text|page|row|column|pane|panel|window|open|close|add|remove|edit|"
    r"save|apply|back|next|previous|first|last|other|same|each|between|"
    r"during|after|before|above|below|left|right|top|bottom|into|out|more|"
    r"less|most|least|few|many|much|such|both|every|own|another|use|hide)\b",
    re.IGNORECASE,
)
ENGLISH_SCHEMA_MIN_DESC_SAMPLES: Final = 5
ENGLISH_SCHEMA_MIN_HIT_RATIO: Final = 0.8
UI_CONTEXT_SIGNAL_PROPERTIES: Final = frozenset(
    {
        "callback",
        "checkCallback",
        "editorCallback",
        "editorCheckCallback",
        "onClick",
        "onclick",
    }
)
SAFE_NATIVE_DOM_TAG_NAMES: Final = frozenset(
    {
        "a",
        "abbr",
        "address",
        "article",
        "aside",
        "b",
        "bdi",
        "bdo",
        "blockquote",
        "button",
        "caption",
        "cite",
        "dd",
        "del",
        "details",
        "dfn",
        "dialog",
        "div",
        "dl",
        "dt",
        "em",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "i",
        "input",
        "ins",
        "label",
        "legend",
        "li",
        "main",
        "mark",
        "menu",
        "meter",
        "nav",
        "ol",
        "optgroup",
        "option",
        "output",
        "p",
        "progress",
        "q",
        "rp",
        "rt",
        "ruby",
        "s",
        "section",
        "select",
        "small",
        "span",
        "strong",
        "sub",
        "summary",
        "sup",
        "table",
        "tbody",
        "td",
        "textarea",
        "tfoot",
        "th",
        "thead",
        "time",
        "tr",
        "u",
        "ul",
    }
)
SAFE_NATIVE_DOM_VISIBLE_PROPERTIES: Final = frozenset(
    {"aria-label", "ariaLabel", "placeholder", "title"}
)
MAX_NESTED_CREATE_ELEMENT_DEPTH: Final = 8

StringOrigin = Literal[
    "manifest.name",
    "manifest.description",
    "registry.name",
    "registry.description",
    "readme",
    "ui-call",
    "ui-property",
]
ExtractionStrategy = Literal[
    "manifest", "registry", "markdown", "structured", "regex-fallback"
]
SemanticRole = Literal["official-name", "description", "readme", "runtime-ui"]


class StringEvidence(TypedDict):
    origin: StringOrigin
    strategy: ExtractionStrategy
    symbol: str
    offset: int | None
    line: int | None
    column: int | None


class SnapshotString(TypedDict):
    evidence: list[StringEvidence]
    key: str
    origins: list[StringOrigin]
    semantic_role: SemanticRole
    placeholder_signature: str
    source: str


class NativeLocaleCoverageEntry(TypedDict):
    placeholder_signature: str
    resource_key: str
    string_key: str


class NativeLocaleCoverage(TypedDict):
    covered_entries: list[NativeLocaleCoverageEntry]
    locale: str
    resource_digest: str
    resource_name: str
    source_resource_digest: str
    source_resource_name: str


class _NativeLocaleCoverageState(NamedTuple):
    carrier: Literal["embedded", "explicit"]
    entries_by_string: dict[str, NativeLocaleCoverageEntry]
    targets_by_string: dict[str, str]
    rejected_strings: set[str]
    resource_digest: str
    resource_name: str
    source_resource_digest: str
    source_resource_name: str


class _Token(NamedTuple):
    kind: Literal["identifier", "literal", "punctuation", "other"]
    raw: str
    start: int
    end: int
    line: int
    column: int


class _RenderedExpression(NamedTuple):
    text: str
    static_text: str


LocalePath = tuple[str, ...]
EmbeddedLocaleCatalog = tuple[str, dict[LocalePath, str]]
LocaleComponent = tuple[str, str, bytes]
LocaleComponents = dict[str, list[LocaleComponent]]


class AdapterContractError(ValueError):
    """The acquired component closure does not satisfy the Obsidian contract."""


class ComponentRow(TypedDict):
    role: str
    name: str
    media_type: str
    path: str
    size: int
    transport_digest: str


class AdapterRequest(TypedDict):
    job_id: str
    ipc_namespace: str
    components: list[ComponentRow]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _read_request(path: Path) -> AdapterRequest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterContractError("adapter_request_invalid") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("components"), list):
        raise AdapterContractError("adapter_request_invalid")
    return cast(AdapterRequest, raw)


def _read_component(row: ComponentRow) -> bytes:
    try:
        path = Path(row["path"])
        expected_size = row["size"]
        expected_digest = row["transport_digest"]
    except (KeyError, TypeError) as exc:
        raise AdapterContractError("adapter_component_metadata_invalid") from exc
    if (
        not path.is_absolute()
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 0
        or not isinstance(expected_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
    ):
        raise AdapterContractError("adapter_component_metadata_invalid")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise AdapterContractError("adapter_component_unreadable") from exc
    if (
        len(content) != expected_size
        or hashlib.sha256(content).hexdigest() != expected_digest
    ):
        raise AdapterContractError("adapter_component_identity_mismatch")
    return content


def _canonical_locale(value: str) -> str:
    parts = value.split("-")
    canonical = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            canonical.append(part.title())
        elif len(part) == 2 and part.isalpha():
            canonical.append(part.upper())
        else:
            canonical.append(part.lower())
    result = "-".join(canonical)
    # Obsidian's long-standing locale convention uses bare ``zh`` for
    # Simplified Chinese, while ``zh-TW`` is the distinct Traditional catalog.
    return "zh-CN" if result == "zh" else result


def _required_components(
    request: AdapterRequest,
) -> tuple[bytes, bytes, bytes | None, bytes | None, LocaleComponents]:
    selected: dict[str, bytes] = {}
    registry_metadata: bytes | None = None
    readme_content: bytes | None = None
    locale_components: LocaleComponents = {}
    for raw_row in request["components"]:
        if not isinstance(raw_row, dict):
            raise AdapterContractError("adapter_component_metadata_invalid")
        row = cast(ComponentRow, raw_row)
        role = row.get("role")
        name = row.get("name")
        if not isinstance(role, str) or not isinstance(name, str):
            raise AdapterContractError("adapter_component_metadata_invalid")
        locale_match = LOCALE_ROLE_PATTERN.fullmatch(role)
        if locale_match is not None:
            locale = _canonical_locale(locale_match.group(1))
            resource_id = locale_match.group(2) or "default"
            resources = locale_components.setdefault(locale, [])
            if (
                any(item[0] == resource_id for item in resources)
                or not name
                or name != Path(name).name
            ):
                raise AdapterContractError("adapter_locale_component_invalid")
            content = _read_component(row)
            if len(content) > MAX_LOCALE_COMPONENT_BYTES:
                raise AdapterContractError("adapter_locale_component_too_large")
            resources.append((resource_id, name, content))
            continue
        if role == "registry-metadata":
            if name != "community-plugin.json" or registry_metadata is not None:
                raise AdapterContractError("adapter_registry_metadata_invalid")
            registry_metadata = _read_component(row)
            continue
        if role == "readme":
            if name != "README.md" or readme_content is not None:
                raise AdapterContractError("adapter_readme_component_invalid")
            readme_content = _read_component(row)
            if (
                len(readme_content) > MAX_README_COMPONENT_BYTES
                or b"\x00" in readme_content
            ):
                raise AdapterContractError("adapter_readme_component_invalid")
            continue
        expected_name = {"manifest": "manifest.json", "main": "main.js"}.get(role)
        if expected_name is None:
            continue
        if name != expected_name or role in selected:
            raise AdapterContractError("adapter_component_closure_invalid")
        selected[role] = _read_component(row)
    if set(selected) != {"manifest", "main"}:
        raise AdapterContractError("adapter_component_closure_incomplete")
    return (
        selected["manifest"],
        selected["main"],
        registry_metadata,
        readme_content,
        locale_components,
    )


def _manifest_value(manifest: dict[str, object], field: str) -> str:
    value = manifest.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AdapterContractError(f"plugin_manifest_{field}_invalid")
    return unicodedata.normalize("NFC", value.strip())


def _decode_manifest(content: bytes) -> dict[str, object]:
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterContractError("plugin_manifest_invalid") from exc
    if not isinstance(raw, dict):
        raise AdapterContractError("plugin_manifest_invalid")
    return cast(dict[str, object], raw)


def _decode_registry_metadata(content: bytes, plugin_id: str) -> tuple[str, str]:
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterContractError("plugin_registry_metadata_invalid") from exc
    if not isinstance(raw, dict) or raw.get("id") != plugin_id:
        raise AdapterContractError("plugin_registry_metadata_identity_mismatch")
    metadata = cast(dict[str, object], raw)
    return (
        _manifest_value(metadata, "name"),
        _manifest_value(metadata, "description"),
    )


def _decode_js_literal(literal: str) -> str | None:
    if len(literal) < 2 or literal[0] not in {'"', "'", "`"}:
        return None
    quote = literal[0]
    if literal[-1] != quote:
        return None
    body = literal[1:-1]
    if quote == "`" and "${" in body:
        return None
    output: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\":
            output.append(character)
            index += 1
            continue
        index += 1
        if index >= len(body):
            return None
        escaped = body[index]
        index += 1
        simple = {
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "b": "\b",
            "f": "\f",
            "v": "\v",
        }
        if escaped in simple:
            output.append(simple[escaped])
            continue
        if escaped in {"x", "u"}:
            width = 2 if escaped == "x" else 4
            code = body[index : index + width]
            if len(code) != width or not re.fullmatch(r"[0-9a-fA-F]+", code):
                return None
            value = int(code, 16)
            if 0xD800 <= value <= 0xDFFF:
                return None
            output.append(chr(value))
            index += width
            continue
        output.append(escaped)
    return "".join(output)


def _normalize_community_bundle(bundle: str) -> str:
    """Canonical installed-artifact form shared with the Obsidian client.

    Obsidian's community installer deletes an inline ``//# sourceMappingURL=``
    line and appends a ``/* nosourcemap */`` suppression comment, so the
    installed artifact never hashes like the raw release asset.  The adapter
    mirrors the client's normalization (drop the suppression suffix, drop the
    source map line, trim trailing whitespace) so authoritative artifact
    digests match what users actually run.
    """

    suffix = "\n/* nosourcemap */"
    if bundle.endswith(suffix):
        bundle = bundle[: -len(suffix)]
    index = bundle.rfind("\n//# sourceMappingURL=")
    if index >= 0:
        bundle = bundle[:index]
    elif bundle.startswith("//# sourceMappingURL="):
        bundle = ""
    return bundle.rstrip()


def _is_translatable_ui_text(value: str) -> bool:
    if not 2 <= len(value) <= 300:
        return False
    if not any(unicodedata.category(character).startswith("L") for character in value):
        return False
    exclusions = (
        r"^(?:https?:|data:|app:|obsidian:)",
        r"[/\\].+\.(?:js|ts|json|css|svg|png|md)$",
        r"^[a-z0-9_.-]+(?:/[a-z0-9_.{}:-]+)+$",
        r"^[.#][A-Za-z0-9_-]+$",
        r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){2,}$",
        r"^[a-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+$",
        r"^[A-Z_][A-Z0-9_]+$",
        r"^%[A-Za-z_][A-Za-z0-9_]*$",
    )
    if any(
        re.search(pattern, value, re.IGNORECASE if index < 2 else 0)
        for index, pattern in enumerate(exclusions)
    ):
        return False
    return not _is_language_neutral_structured_literal(value)


def _is_language_neutral_structured_literal(value: str) -> bool:
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, (dict, list)):
        return False
    return _has_only_structured_identifiers(parsed)


def _has_only_structured_identifiers(value: object) -> bool:
    if isinstance(value, str):
        return re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$.-]*", value) is not None
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, list):
        return all(_has_only_structured_identifiers(item) for item in value)
    if isinstance(value, dict):
        if not all(_is_structured_machine_key(key) for key in value):
            return False
        return all(_has_only_structured_identifiers(item) for item in value.values())
    return False


def _is_structured_machine_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    if (
        re.fullmatch(
            r"(?:kind|type|id|action|actions|mode|scope|scopes|status|variant|version|enabled|disabled)",
            key,
            re.IGNORECASE,
        )
        is not None
    ):
        return True
    # Serialized configuration examples are not UI copy merely because they
    # are assigned to a visible placeholder. Keep the suffix set narrow so
    # prose keys such as title and summary remain localizable.
    return (
        re.fullmatch(
            r"[a-z][A-Za-z0-9]*(?:mode|order|sort|variant|scope|status|id|type|version)",
            key,
            re.IGNORECASE,
        )
        is not None
    )


def _is_plausible_source_locale_text(value: str, source_locale: str) -> bool:
    if source_locale != "en":
        return True
    letter_count = 0
    latin_letter_count = 0
    for character in value:
        if not unicodedata.category(character).startswith("L"):
            continue
        letter_count += 1
        if unicodedata.name(character, "").startswith("LATIN"):
            latin_letter_count += 1
    return letter_count == 0 or latin_letter_count * 2 >= letter_count


def _placeholder_signature(value: str) -> str:
    placeholders = PLACEHOLDER.findall(value)
    if len(placeholders) < 2:
        return placeholders[0] if placeholders else ""
    return json.dumps(placeholders, ensure_ascii=False, separators=(",", ":"))


def _add_candidate(
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
    raw: str,
    origin: StringOrigin,
    evidence: StringEvidence,
    *,
    source_locale: str = "en",
    static_probe: str | None = None,
    ui_context_verified: bool = False,
) -> None:
    value = unicodedata.normalize("NFC", raw).strip()
    probe = unicodedata.normalize(
        "NFC", raw if static_probe is None else static_probe
    ).strip()
    if (
        origin == "ui-property"
        and evidence["symbol"] in UI_PROPERTY_NAMES | {"ui-property"}
        and not ui_context_verified
    ):
        return
    if (
        not _is_translatable_ui_text(value)
        or not _is_translatable_ui_text(probe)
        or not _is_plausible_source_locale_text(value, source_locale)
    ):
        return
    origins, evidence_rows = collected.setdefault(value, (set(), {}))
    origins.add(origin)
    evidence_rows[_canonical_json(evidence).decode("utf-8")] = evidence


def _collect_regex_matches(
    bundle: str,
    pattern: re.Pattern[str],
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
    origin: StringOrigin,
    symbol: str,
    capture_index: int = 1,
    ui_context_verified: bool = False,
    accept_rendered: Callable[[_RenderedExpression], bool] | None = None,
    transform_rendered: (
        Callable[[_RenderedExpression], _RenderedExpression | None] | None
    ) = None,
) -> None:
    for match in pattern.finditer(bundle):
        rendered = _render_fallback_literal(match.group(capture_index))
        if rendered is None:
            continue
        if accept_rendered is not None and not accept_rendered(rendered):
            continue
        if transform_rendered is not None:
            rendered = transform_rendered(rendered)
            if rendered is None:
                continue
        line, column = _offset_location(bundle, match.start())
        _add_candidate(
            collected,
            rendered.text,
            origin,
            {
                "origin": origin,
                "strategy": "regex-fallback",
                "symbol": symbol,
                "offset": match.start(),
                "line": line,
                "column": column,
            },
            static_probe=rendered.static_text,
            ui_context_verified=ui_context_verified,
        )


def _render_fallback_literal(literal: str) -> _RenderedExpression | None:
    if not literal.startswith("`") or "${" not in literal:
        decoded = _decode_js_literal(literal)
        if decoded is None:
            return None
        return _RenderedExpression(decoded, decoded)
    return _render_template_literal(literal, [0])


def _collect_structured_matches(
    bundle: str,
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
) -> bool:
    tokens = _tokenize_javascript(bundle)
    if tokens is None:
        return False
    matching = _build_matching_token_indexes(tokens)
    if matching is None:
        return False
    ui_context_property_indices = _ui_registration_context_property_indices(tokens)
    create_element_ends: list[int] = []
    for index, token in enumerate(tokens):
        while create_element_ends and create_element_ends[-1] < index:
            create_element_ends.pop()
        if token.kind != "identifier":
            continue
        next_token = tokens[index + 1] if index + 1 < len(tokens) else None
        if (
            (token.raw in UI_CALL_NAMES or token.raw == "addOption")
            and next_token is not None
            and next_token.raw == "("
        ):
            call = _read_call_arguments(tokens, index + 1, matching)
            if call is None:
                return False
            arguments, _ = call
            argument_index = 1 if token.raw == "addOption" else 0
            if argument_index < len(arguments):
                _add_structured_expression(
                    collected, arguments[argument_index], "ui-call", token
                )
            continue
        if (
            token.raw in OBSIDIAN_CREATE_CALL_NAMES
            and next_token is not None
            and next_token.raw == "("
        ):
            call = _read_call_arguments(tokens, index + 1, matching)
            if call is None:
                return False
            arguments, _ = call
            for argument in arguments:
                _collect_obsidian_create_text_option(argument, token, collected)
            continue
        if token.raw == "addOptions" and next_token is not None and next_token.raw == "(":
            call = _read_call_arguments(tokens, index + 1, matching)
            if call is None:
                return False
            arguments, _ = call
            if arguments:
                _collect_add_options_labels(arguments[0], token, collected)
            continue
        if (
            token.raw in DOM_TEXT_SINK_PROPERTIES
            and next_token is not None
            and next_token.raw == "="
            and _is_member_expression_receiver(tokens, index)
        ):
            expression = _read_property_expression(tokens, index + 2)
            if not expression:
                continue
            if token.raw == "innerHTML":
                counter = [0]
                rendered = _render_expression(expression, counter)
                if rendered is None:
                    continue
                text = _inner_html_text_content(rendered.text)
                if text is None:
                    continue
                _add_candidate(
                    collected,
                    text,
                    "ui-property",
                    {
                        "origin": "ui-property",
                        "strategy": "structured",
                        "symbol": "innerHTML",
                        "offset": token.start,
                        "line": token.line,
                        "column": token.column,
                    },
                    static_probe=text,
                    ui_context_verified=True,
                )
                continue
            _add_structured_expression(
                collected,
                expression,
                "ui-property",
                token,
                ui_context_verified=True,
                accept_rendered=_single_line_text
                if token.raw == "innerText"
                else None,
            )
            continue
        if (
            _is_safe_react_create_element_call(tokens, index)
            and next_token is not None
            and next_token.raw == "("
        ):
            call = _read_call_arguments(tokens, index + 1, matching)
            if call is None:
                return False
            arguments, end_index = call
            if len(create_element_ends) < MAX_NESTED_CREATE_ELEMENT_DEPTH:
                _collect_react_create_element(arguments, token, collected)
            create_element_ends.append(end_index)
            continue
        if (
            not create_element_ends
            and token.raw in UI_PROPERTY_NAMES
            and next_token is not None
            and next_token.raw == ":"
        ):
            expression = _read_property_expression(tokens, index + 2)
            if expression:
                _add_structured_expression(
                    collected,
                    expression,
                    "ui-property",
                    token,
                    ui_context_verified=index in ui_context_property_indices,
                )
    _collect_settings_schema_entries(tokens, matching, collected)
    _collect_settings_group_descriptors(tokens, matching, collected)
    _collect_grouped_ui_text_dictionary(tokens, matching, collected)
    return True


def _collect_grouped_ui_text_dictionary(
    tokens: list[_Token],
    matching: array,
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
) -> None:
    """Collect grouped UI copy dictionaries such as make.md's
    ``var wfe={hintText:{fileName:"Enter File Name"},timeUnits:{hour:"Hour"},
    aggregates:{values:"Values",...},...}``.

    The outer object must contain several nested group objects and most leaf
    string values must read as title-case English UI copy, so flat lookup
    tables (keyboard key names, HTML entities, emoji descriptors, easing
    function names, locale codes) are rejected by the group or ratio gate.
    """

    for index, token in enumerate(tokens):
        if index + 2 >= len(tokens):
            break
        if (
            token.kind != "identifier"
            or tokens[index + 1].raw != "="
            or tokens[index + 2].raw != "{"
        ):
            continue
        before = tokens[index - 1].raw if index > 0 else None
        if before not in (None, "var", "let", "const", ";", ","):
            continue
        open_index = index + 2
        end = cast(int, matching[open_index]) if open_index < len(matching) else -1
        if end < 0 or end - open_index > SETTINGS_SCHEMA_MAX_PARENT_TOKENS:
            continue
        groups = _collect_ui_text_dictionary_groups(
            tokens[open_index + 1 : end], 0
        )
        if (
            groups["group_count"] < UI_TEXT_DICTIONARY_MIN_GROUPS
            or groups["value_count"] < UI_TEXT_DICTIONARY_MIN_VALUES
            or groups["title_case_count"] / groups["value_count"]
            < UI_TEXT_DICTIONARY_MIN_TITLE_RATIO
        ):
            continue
        for value, literal in groups["entries"]:
            _add_candidate(
                collected,
                value,
                "ui-property",
                {
                    "origin": "ui-property",
                    "strategy": "structured",
                    "symbol": "dictionary",
                    "offset": literal.start,
                    "line": literal.line,
                    "column": literal.column,
                },
            )


class _UiTextDictionaryGroups(TypedDict):
    group_count: int
    value_count: int
    title_case_count: int
    entries: list[tuple[str, _Token]]


def _collect_ui_text_dictionary_groups(
    body: list[_Token],
    depth: int,
) -> _UiTextDictionaryGroups:
    group_count = 0
    value_count = 0
    title_case_count = 0
    entries: list[tuple[str, _Token]] = []
    for entry in _split_top_level_tokens(body):
        if not entry:
            continue
        colon = _top_level_token_index(entry, ":")
        if colon <= 0:
            continue
        value = entry[colon + 1 :]
        if not value:
            continue
        if value[0].raw == "{" and depth < UI_TEXT_DICTIONARY_MAX_DEPTH:
            group_count += 1
            nested = _collect_ui_text_dictionary_groups(value[1:-1], depth + 1)
            group_count += nested["group_count"]
            value_count += nested["value_count"]
            title_case_count += nested["title_case_count"]
            entries.extend(nested["entries"])
            continue
        if len(value) != 1 or value[0].kind != "literal":
            continue
        decoded = _decode_js_literal(value[0].raw)
        if decoded is None:
            continue
        if (
            not _is_translatable_ui_text(decoded)
            or not _is_plausible_source_locale_text(decoded, "en")
        ):
            continue
        value_count += 1
        if _is_title_case_ui_text(decoded):
            title_case_count += 1
        entries.append((decoded, value[0]))
    return {
        "group_count": group_count,
        "value_count": value_count,
        "title_case_count": title_case_count,
        "entries": entries,
    }


def _is_title_case_ui_text(value: str) -> bool:
    if len(value) < 2 or len(value) > 200:
        return False
    if not re.match(r"^[A-Za-z]", value):
        return False
    if not re.search(r"[A-Z]", value):
        return False
    if re.search(r"[:/.[\]{}<>]", value):
        return False
    return True


def _collect_settings_schema_entries(
    tokens: list[_Token],
    matching: Sequence[int],
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
) -> None:
    """Extract declarative settings schemas such as make.md's
    ``{ navigatorEnabled: { name: "Navigator", desc: "..." }, ... }``.

    The outer keys are plugin-specific identifiers, so the name/desc values
    are only provable as presentation when the parent object has several
    sibling entries that all carry a static ``name`` plus a static
    ``desc``/``description``.
    """

    for index, token in enumerate(tokens):
        if token.raw != "{":
            continue
        if index + 3 >= len(tokens):
            continue
        if (
            tokens[index + 1].kind != "identifier"
            or tokens[index + 2].raw != ":"
            or tokens[index + 3].raw != "{"
        ):
            continue
        end = matching[index] if index < len(matching) else -1
        if end < 0 or end >= len(tokens):
            continue
        schema_entries: list[tuple[_Token, list[_Token]]] = []
        for entry in _split_top_level_tokens(tokens[index + 1 : end]):
            colon = _top_level_token_index(entry, ":")
            if colon <= 0:
                continue
            key = entry[0]
            value = _strip_wrapping_parentheses(entry[colon + 1 :])
            if (
                key.kind != "identifier"
                or not value
                or value[0].raw != "{"
                or _matching_token_index(value, 0) != len(value) - 1
            ):
                continue
            if (
                _static_object_string_property(value, "name") is None
                or (
                    _static_object_string_property(value, "desc") is None
                    and _static_object_string_property(value, "description") is None
                )
            ):
                continue
            schema_entries.append((key, value))
        if len(schema_entries) < SETTINGS_SCHEMA_MIN_ENTRIES:
            continue
        # Plugins such as notebook-navigator ship one full settings schema per
        # language. The English source catalog must only contain the English
        # schema; other Latin-script packs pass the character-level source
        # filter, so judge the whole parent object by how much of its
        # description copy reads as English. With too few description samples
        # the gate is skipped to avoid dropping small valid English schemas.
        if not _is_english_settings_schema(schema_entries):
            continue
        for key, value in schema_entries:
            for property_name in ("name", "desc", "description"):
                expression = _static_object_string_property(value, property_name)
                if expression is not None:
                    _add_settings_schema_value(collected, expression, key)


def _is_english_settings_schema(
    schema_entries: list[tuple[_Token, list[_Token]]],
) -> bool:
    samples: list[str] = []
    for _, value in schema_entries:
        for property_name in ("desc", "description"):
            expression = _static_object_string_property(value, property_name)
            if expression is None or len(expression) != 1:
                continue
            if expression[0].kind != "literal":
                continue
            decoded = _decode_js_literal(expression[0].raw)
            if decoded is not None and len(decoded) > 5:
                samples.append(decoded)
    if len(samples) < ENGLISH_SCHEMA_MIN_DESC_SAMPLES:
        return True
    hits = sum(1 for sample in samples if ENGLISH_SCHEMA_STOP_WORDS.search(sample))
    return hits / len(samples) >= ENGLISH_SCHEMA_MIN_HIT_RATIO


def _static_object_string_property(
    object_tokens: list[_Token], property_name: str
) -> list[_Token] | None:
    for prop in _split_top_level_tokens(object_tokens[1:-1]):
        colon = _top_level_token_index(prop, ":")
        if colon <= 0:
            continue
        if _static_catalog_key(prop[:colon]) != property_name:
            continue
        value = prop[colon + 1 :]
        if len(value) == 1 and value[0].kind == "literal":
            return value
    return None


def _add_settings_schema_value(
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
    expression: list[_Token],
    key: _Token,
) -> None:
    counter = [0]
    rendered = _render_expression(expression, counter)
    if rendered is None:
        return
    _add_candidate(
        collected,
        rendered.text,
        "ui-property",
        {
            "origin": "ui-property",
            "strategy": "structured",
            "symbol": "settingsSchema",
            "offset": key.start,
            "line": key.line,
            "column": key.column,
        },
        static_probe=rendered.static_text,
        ui_context_verified=True,
    )


def _is_member_expression_receiver(tokens: list[_Token], property_index: int) -> bool:
    """Accept `receiver.prop = ...` where receiver is an identifier chain."""

    if property_index < 1 or tokens[property_index - 1].raw != ".":
        return False
    index = property_index - 2
    if index < 0:
        return False
    if tokens[index].raw == "this":
        return True
    if tokens[index].kind != "identifier":
        return False
    index -= 1
    while (
        index >= 1
        and tokens[index].raw == "."
        and tokens[index - 1].kind == "identifier"
    ):
        index -= 2
    return True


def _collect_obsidian_create_text_option(
    argument: list[_Token],
    call_token: _Token,
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
) -> None:
    options = _strip_wrapping_parentheses(argument)
    if (
        not options
        or options[0].raw != "{"
        or _matching_token_index(options, 0) != len(options) - 1
    ):
        return
    for entry in _split_top_level_tokens(options[1:-1]):
        colon = _top_level_token_index(entry, ":")
        if colon <= 0:
            continue
        key = _static_catalog_key(entry[:colon])
        if key != "text":
            continue
        _add_structured_expression(
            collected,
            entry[colon + 1 :],
            "ui-property",
            call_token,
            ui_context_verified=True,
        )


def _collect_add_options_labels(
    argument: list[_Token],
    call_token: _Token,
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
) -> None:
    """Extract DropdownComponent labels from
    ``dropdown.addOptions({ never: "Never", ... })``."""

    options = _strip_wrapping_parentheses(argument)
    if (
        not options
        or options[0].raw != "{"
        or _matching_token_index(options, 0) != len(options) - 1
    ):
        return
    for entry in _split_top_level_tokens(options[1:-1]):
        colon = _top_level_token_index(entry, ":")
        if colon <= 0:
            continue
        value = entry[colon + 1 :]
        if len(value) != 1 or value[0].kind != "literal":
            continue
        _add_structured_expression(
            collected,
            value,
            "ui-property",
            call_token,
            ui_context_verified=True,
        )


def _collect_add_options_regex_matches(
    bundle: str,
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
) -> None:
    """Regex fallback for ``addOptions({ key: "Label", ... })`` objects."""

    call_pattern = re.compile(r"addOptions\s*\(\s*\{")
    entry_pattern = re.compile(
        r'(?:[A-Za-z_$][A-Za-z0-9_$]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
        r"\s*:\s*(\"(?:\\.|[^\"\\])*\"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)"
    )
    for call in call_pattern.finditer(bundle):
        start = call.end()
        body = bundle[start : start + 4096]
        close = body.find("}")
        if close == -1:
            continue
        line, column = _offset_location(bundle, call.start())
        for entry in entry_pattern.finditer(body[:close]):
            literal = entry.group(1)
            if literal is None:
                continue
            rendered = _render_fallback_literal(literal)
            if rendered is None:
                continue
            _add_candidate(
                collected,
                rendered.text,
                "ui-property",
                {
                    "origin": "ui-property",
                    "strategy": "regex-fallback",
                    "symbol": "addOptions",
                    "offset": call.start(),
                    "line": line,
                    "column": column,
                },
                static_probe=rendered.static_text,
                ui_context_verified=True,
            )


def _static_object_array_property(
    object_tokens: list[_Token], property_name: str
) -> list[list[_Token]] | None:
    for prop in _split_top_level_tokens(object_tokens[1:-1]):
        colon = _top_level_token_index(prop, ":")
        if colon <= 0:
            continue
        if _static_catalog_key(prop[:colon]) != property_name:
            continue
        value = _strip_wrapping_parentheses(prop[colon + 1 :])
        if (
            not value
            or value[0].raw != "["
            or _matching_token_index(value, 0) != len(value) - 1
        ):
            continue
        return _split_top_level_tokens(value[1:-1])
    return None


def _collect_settings_group_descriptors(
    tokens: list[_Token],
    matching: Sequence[int],
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
) -> None:
    """Extract declarative settings-group descriptors such as QuickAdd's
    ``{ type: "group", heading: "Choice picker", items: [...] }``."""

    for index, token in enumerate(tokens):
        if token.raw != "{":
            continue
        end = matching[index] if index < len(matching) else -1
        if end < 0 or end - index > SETTINGS_SCHEMA_MAX_PARENT_TOKENS:
            continue
        object_tokens = tokens[index : end + 1]
        type_value = _static_object_string_property(object_tokens, "type")
        heading_value = _static_object_string_property(object_tokens, "heading")
        items = _static_object_array_property(object_tokens, "items")
        if type_value is None or heading_value is None or items is None:
            continue
        if not any(
            _static_object_string_property(item, "name") is not None
            for item in items
        ):
            continue
        descriptor_key = (
            tokens[index + 1] if index + 1 < len(tokens) else token
        )
        _add_settings_schema_value(collected, heading_value, descriptor_key)
        for item in items:
            for property_name in ("name", "desc", "description"):
                expression = _static_object_string_property(item, property_name)
                if expression is not None:
                    _add_settings_schema_value(
                        collected, expression, descriptor_key
                    )


def _single_line_text(rendered: _RenderedExpression) -> bool:
    return "\r" not in rendered.text and "\n" not in rendered.text


def _render_inner_html_text(
    rendered: _RenderedExpression,
) -> _RenderedExpression | None:
    text = _inner_html_text_content(rendered.text)
    if text is None:
        return None
    return _RenderedExpression(text, text)


def _inner_html_text_content(raw: str) -> str | None:
    if "${" in raw or f"{{{{{DYNAMIC_PLACEHOLDER_PREFIX}" in raw:
        return None
    text = re.sub(r"<[^>]*>", "", raw).strip()
    if not text or not any(
        unicodedata.category(character).startswith("L") for character in text
    ):
        return None
    return text


def _is_safe_react_create_element_call(tokens: list[_Token], index: int) -> bool:
    """Accept canonical React calls and the default-interop bundle shape."""

    if index < 2 or tokens[index - 1].raw != ".":
        return False
    if tokens[index - 2].raw in {"React", "ReactDOM"}:
        return True
    return (
        index >= 4
        and tokens[index - 2].raw == "default"
        and tokens[index - 3].raw == "."
        and tokens[index - 4].kind == "identifier"
    )


def _add_structured_expression(
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
    expression: list[_Token],
    origin: StringOrigin,
    symbol: _Token,
    *,
    ui_context_verified: bool = False,
    accept_rendered: Callable[[_RenderedExpression], bool] | None = None,
) -> None:
    counter = [0]
    rendered = _render_expression(expression, counter)
    if rendered is None:
        return
    if accept_rendered is not None and not accept_rendered(rendered):
        return
    _add_candidate(
        collected,
        rendered.text,
        origin,
        {
            "origin": origin,
            "strategy": "structured",
            "symbol": symbol.raw,
            "offset": symbol.start,
            "line": symbol.line,
            "column": symbol.column,
        },
        static_probe=rendered.static_text,
        ui_context_verified=ui_context_verified,
    )


def _collect_react_create_element(
    arguments: list[list[_Token]],
    call_token: _Token,
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
) -> None:
    tag_expression = _strip_wrapping_parentheses(arguments[0] if arguments else [])
    tag_name = (
        _decode_js_literal(tag_expression[0].raw)
        if len(tag_expression) == 1 and tag_expression[0].kind == "literal"
        else None
    )
    native_tag = tag_name in SAFE_NATIVE_DOM_TAG_NAMES
    component_tag = len(tag_expression) == 1 and tag_expression[0].kind == "identifier"
    if not native_tag and not component_tag:
        return
    if len(arguments) > 1:
        _collect_native_dom_visible_properties(
            arguments[1], call_token, collected, accepts_children=native_tag or component_tag
        )
    for child in arguments[2:]:
        _add_safe_native_dom_expression(
            collected, child, "ui-call", call_token, "createElement"
        )


def _collect_native_dom_visible_properties(
    expression: list[_Token],
    call_token: _Token,
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
    *,
    accepts_children: bool,
) -> None:
    properties = _strip_wrapping_parentheses(expression)
    if (
        not properties
        or properties[0].raw != "{"
        or _matching_token_index(properties, 0) != len(properties) - 1
    ):
        return
    for entry in _split_top_level_tokens(properties[1:-1]):
        colon = _top_level_token_index(entry, ":")
        if colon <= 0:
            continue
        key = _static_catalog_key(entry[:colon])
        if key == "children" and accepts_children:
            _add_safe_native_dom_expression(
                collected, entry[colon + 1 :], "ui-call", call_token, "createElement"
            )
            continue
        if key not in SAFE_NATIVE_DOM_VISIBLE_PROPERTIES:
            continue
        _add_safe_native_dom_expression(
            collected,
            entry[colon + 1 :],
            "ui-property",
            entry[0] if entry else call_token,
            key,
            ui_context_verified=True,
        )


def _add_safe_native_dom_expression(
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]],
    expression: list[_Token],
    origin: StringOrigin,
    symbol: _Token,
    symbol_name: str,
    *,
    ui_context_verified: bool = False,
) -> None:
    rendered = _render_safe_native_dom_expression(expression, [0])
    if rendered is None:
        return
    _add_candidate(
        collected,
        rendered.text,
        origin,
        {
            "origin": origin,
            "strategy": "structured",
            "symbol": symbol_name,
            "offset": symbol.start,
            "line": symbol.line,
            "column": symbol.column,
        },
        static_probe=rendered.static_text,
        ui_context_verified=ui_context_verified,
    )


def _ui_registration_context_property_indices(tokens: list[_Token]) -> set[int]:
    """Find UI properties backed by a sibling registration callback in O(n)."""

    delimiter_stack: list[tuple[str, int]] = []
    brace_stack: list[int] = []
    property_objects: dict[int, int] = {}
    registration_objects: set[int] = set()
    matching_open = {")": "(", "]": "[", "}": "{"}

    for index, token in enumerate(tokens):
        raw = token.raw
        expected_open = matching_open.get(raw)
        if expected_open is not None:
            if delimiter_stack and delimiter_stack[-1][0] == expected_open:
                _, open_index = delimiter_stack.pop()
                if expected_open == "{" and brace_stack[-1:] == [open_index]:
                    brace_stack.pop()
            continue

        next_token = tokens[index + 1] if index + 1 < len(tokens) else None
        is_property = next_token is not None and next_token.raw == ":"
        if token.kind == "identifier" and is_property and brace_stack:
            open_index = brace_stack[-1]
            if _is_object_literal_open(tokens, open_index):
                if token.raw in UI_PROPERTY_NAMES:
                    property_objects[index] = open_index
                if (
                    token.raw in UI_CONTEXT_SIGNAL_PROPERTIES
                    and delimiter_stack
                    and delimiter_stack[-1] == ("{", open_index)
                ):
                    registration_objects.add(open_index)

        if raw in {"(", "[", "{"}:
            delimiter_stack.append((raw, index))
            if raw == "{":
                brace_stack.append(index)

    return {
        property_index
        for property_index, open_index in property_objects.items()
        if open_index in registration_objects
    }


def _is_object_literal_open(tokens: list[_Token], open_index: int) -> bool:
    if open_index == 0:
        return False
    return tokens[open_index - 1].raw in {"=", "(", "[", ",", ":", "return", ">"}


def _render_expression(
    tokens: list[_Token], counter: list[int]
) -> _RenderedExpression | None:
    expression = _strip_wrapping_parentheses(tokens)
    if len(expression) == 1 and expression[0].kind == "literal":
        literal = expression[0].raw
        if literal.startswith("`"):
            return _render_template_literal(literal, counter)
        decoded = _decode_js_literal(literal)
        if decoded is None:
            return None
        return _RenderedExpression(decoded, decoded)
    plus = _find_last_top_level_plus(expression)
    if plus == -1:
        transparent_argument = _transparent_wrapper_argument(expression)
        if transparent_argument is None:
            return None
        rendered = _render_expression(transparent_argument, counter)
        return (
            rendered
            if rendered is not None
            and _is_plausible_transparent_wrapper_text(rendered.static_text)
            else None
        )
    left = _render_expression(expression[:plus], counter)
    right_tokens = expression[plus + 1 :]
    if left is not None:
        right = _render_expression(right_tokens, counter)
        if right is None:
            return _RenderedExpression(
                left.text + _next_dynamic_placeholder(counter), left.static_text
            )
        return _RenderedExpression(
            left.text + right.text, left.static_text + right.static_text
        )
    right = _render_expression(right_tokens, counter)
    if right is None:
        return None
    return _RenderedExpression(
        _next_dynamic_placeholder(counter) + right.text, right.static_text
    )


def _transparent_wrapper_argument(tokens: list[_Token]) -> list[_Token] | None:
    if (
        len(tokens) < 3
        or tokens[0].kind != "identifier"
        # Lower-case t("key") calls are normally a plugin's own i18n lookup:
        # the argument is an internal key, not source-language UI copy.
        or re.fullmatch(r"[A-Z][A-Za-z0-9_$]*", tokens[0].raw) is None
        or tokens[1].raw != "("
        or _matching_token_index(tokens, 1) != len(tokens) - 1
    ):
        return None
    arguments = _split_top_level_tokens(tokens[2:-1])
    return arguments[0] if len(arguments) == 1 and arguments[0] else None


def _is_plausible_transparent_wrapper_text(value: str) -> bool:
    text = value.strip()
    if "_" in text:
        return False
    latin_letters = [character for character in text if "LATIN" in unicodedata.name(character, "")]
    if len(latin_letters) < 2:
        return False
    if any(
        unicodedata.category(character).startswith("L")
        and "LATIN" not in unicodedata.name(character, "")
        for character in text
    ):
        return False
    return bool(text and (text[0].isupper() or any(character.isspace() for character in text)))


def _render_safe_native_dom_expression(
    tokens: list[_Token], counter: list[int]
) -> _RenderedExpression | None:
    expression = _strip_wrapping_parentheses(tokens)
    if len(expression) == 1 and expression[0].kind == "literal":
        literal = expression[0].raw
        if literal.startswith("`"):
            return _render_safe_native_dom_template_literal(literal, counter)
        decoded = _decode_js_literal(literal)
        if decoded is None:
            return None
        return _RenderedExpression(decoded, decoded)
    plus = _find_last_top_level_plus(expression)
    if plus == -1:
        if not _is_safe_native_dom_dynamic_reference(expression):
            return None
        return _RenderedExpression(_next_dynamic_placeholder(counter), "")
    left = _render_safe_native_dom_expression(expression[:plus], counter)
    right = _render_safe_native_dom_expression(expression[plus + 1 :], counter)
    if left is None or right is None:
        return None
    return _RenderedExpression(
        left.text + right.text, left.static_text + right.static_text
    )


def _render_safe_native_dom_template_literal(
    raw: str, counter: list[int]
) -> _RenderedExpression | None:
    body = raw[1:-1]
    text: list[str] = []
    static_text: list[str] = []
    chunk: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character == "\\":
            if index + 1 >= len(body):
                return None
            chunk.append(body[index : index + 2])
            index += 2
            continue
        if character != "$" or index + 1 >= len(body) or body[index + 1] != "{":
            chunk.append(character)
            index += 1
            continue
        decoded = _decode_js_literal(f"`{''.join(chunk)}`")
        if decoded is None:
            return None
        text.append(decoded)
        static_text.append(decoded)
        chunk = []
        end = _find_template_expression_end(body, index + 2)
        if end == -1:
            return None
        dynamic_tokens = _tokenize_javascript(body[index + 2 : end])
        if dynamic_tokens is None or not _is_safe_native_dom_dynamic_reference(
            dynamic_tokens
        ):
            return None
        text.append(_next_dynamic_placeholder(counter))
        index = end + 1
    decoded = _decode_js_literal(f"`{''.join(chunk)}`")
    if decoded is None:
        return None
    text.append(decoded)
    static_text.append(decoded)
    return _RenderedExpression("".join(text), "".join(static_text))


def _is_safe_native_dom_dynamic_reference(tokens: list[_Token]) -> bool:
    expression = _strip_wrapping_parentheses(tokens)
    if (
        not expression
        or expression[0].kind != "identifier"
        or expression[0].raw in {"false", "null", "true", "undefined"}
    ):
        return False
    index = 1
    while index < len(expression):
        if (
            expression[index].raw == "."
            and index + 1 < len(expression)
            and expression[index + 1].kind == "identifier"
        ):
            index += 2
            continue
        if (
            expression[index].raw == "?"
            and index + 2 < len(expression)
            and expression[index + 1].raw == "."
            and expression[index + 2].kind == "identifier"
        ):
            index += 3
            continue
        return False
    return True


def _render_template_literal(
    raw: str, counter: list[int]
) -> _RenderedExpression | None:
    body = raw[1:-1]
    text: list[str] = []
    static_text: list[str] = []
    chunk: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character == "\\":
            if index + 1 >= len(body):
                return None
            chunk.append(body[index : index + 2])
            index += 2
            continue
        if character != "$" or index + 1 >= len(body) or body[index + 1] != "{":
            chunk.append(character)
            index += 1
            continue
        decoded = _decode_js_literal(f"`{''.join(chunk)}`")
        if decoded is None:
            return None
        text.append(decoded)
        static_text.append(decoded)
        chunk = []
        end = _find_template_expression_end(body, index + 2)
        if end == -1:
            return None
        text.append(_next_dynamic_placeholder(counter))
        index = end + 1
    decoded = _decode_js_literal(f"`{''.join(chunk)}`")
    if decoded is None:
        return None
    text.append(decoded)
    static_text.append(decoded)
    return _RenderedExpression("".join(text), "".join(static_text))


def _find_template_expression_end(body: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(body):
        character = body[index]
        if character == "\\":
            index += 2
            continue
        if character in {'"', "'", "`"}:
            end = _find_quoted_end(body, index, character)
            if end == -1:
                return -1
            index = end + 1
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _next_dynamic_placeholder(counter: list[int]) -> str:
    placeholder = f"{{{{{DYNAMIC_PLACEHOLDER_PREFIX}{counter[0]}}}}}"
    counter[0] += 1
    return placeholder


def _strip_wrapping_parentheses(tokens: list[_Token]) -> list[_Token]:
    current = tokens
    while (
        current
        and current[0].raw == "("
        and _matching_token_index(current, 0) == len(current) - 1
    ):
        current = current[1:-1]
    return current


def _find_last_top_level_plus(tokens: list[_Token]) -> int:
    depth = 0
    last = -1
    for index, token in enumerate(tokens):
        if token.raw in {"(", "[", "{"}:
            depth += 1
        elif token.raw in {
            ")",
            "]",
            "}",
        }:
            depth -= 1
        elif token.raw == "+" and depth == 0:
            last = index
    return last


def _read_call_arguments(
    tokens: list[_Token],
    open_index: int,
    matching: array,
) -> tuple[list[list[_Token]], int] | None:
    arguments: list[list[_Token]] = [[]]
    end_index = matching[open_index]
    if end_index < 0:
        return None
    index = open_index + 1
    while index < end_index:
        token = tokens[index]
        if token.raw in {"(", "[", "{"}:
            nested_end = matching[index]
            if nested_end < 0 or nested_end > end_index:
                return None
            arguments[-1].extend(tokens[index : nested_end + 1])
            index = nested_end + 1
            continue
        if token.raw == ",":
            arguments.append([])
        else:
            arguments[-1].append(token)
        index += 1
    return arguments, end_index


def _build_matching_token_indexes(tokens: list[_Token]) -> array | None:
    closing_to_opening = {
        ")": "(",
        "]": "[",
        "}": "{",
    }
    opening = {"(", "[", "{"}
    stack: list[tuple[str, int]] = []
    # Keep delimiter pairs in a compact native array. A dict of Python ints
    # grows disproportionately for minified bundles with many delimiters.
    matching = array("i", [-1]) * len(tokens)
    for index, token in enumerate(tokens):
        if token.raw in opening:
            stack.append((token.raw, index))
            continue
        expected = closing_to_opening.get(token.raw)
        if expected is None:
            continue
        if not stack or stack[-1][0] != expected:
            return None
        _, open_index = stack.pop()
        matching[open_index] = index
        matching[index] = open_index
    return matching if not stack else None


def _read_property_expression(tokens: list[_Token], start: int) -> list[_Token]:
    result: list[_Token] = []
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token.raw in {"(", "[", "{"}:
            depth += 1
        elif token.raw in {
            ")",
            "]",
            "}",
        }:
            if depth == 0:
                break
            depth -= 1
        if depth == 0 and token.raw in {",", ";"}:
            break
        result.append(token)
    return result


def _matching_token_index(tokens: list[_Token], open_index: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}"}
    opening = tokens[open_index].raw
    closing = pairs.get(opening)
    if closing is None:
        return -1
    depth = 0
    for index in range(open_index, len(tokens)):
        if tokens[index].raw == opening:
            depth += 1
        elif tokens[index].raw == closing:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _tokenize_javascript(source: str) -> list[_Token] | None:
    tokens: list[_Token] = []
    line_starts = [0]
    line_starts.extend(index + 1 for index, value in enumerate(source) if value == "\n")
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            if newline == -1:
                break
            index = newline
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                return None
            index = end + 2
            continue
        start = index
        if character == "/" and _is_regex_literal_start(tokens):
            end = _find_regex_end(source, index)
            if end == -1:
                # Not a regex after all: division or another operator. A
                # regex literal containing a raw newline is invalid
                # JavaScript, so falling back to an operator token keeps
                # large multi-line bundles tokenizable.
                index += 1
                tokens.append(
                    _make_token("other", character, start, index, line_starts)
                )
                continue
            index = end
            while index < len(source) and source[index].isalpha():
                index += 1
            tokens.append(
                _make_token("other", source[start:index], start, index, line_starts)
            )
            continue
        if character in {'"', "'", "`"}:
            end = _find_quoted_end(source, index, character)
            if end == -1:
                return None
            index = end + 1
            tokens.append(
                _make_token("literal", source[start:index], start, index, line_starts)
            )
            continue
        if character in {"$", "_"} or character.isalpha():
            index += 1
            while index < len(source):
                value = source[index]
                if value not in {"$", "_"} and not value.isalnum():
                    break
                index += 1
            tokens.append(
                _make_token(
                    "identifier", source[start:index], start, index, line_starts
                )
            )
            continue
        index += 1
        kind: Literal["punctuation", "other"] = (
            "punctuation" if character in "()[]{}:,.+;?" else "other"
        )
        tokens.append(_make_token(kind, character, start, index, line_starts))
    return tokens


def _is_regex_literal_start(tokens: list[_Token]) -> bool:
    if not tokens:
        return True
    previous = tokens[-1].raw
    return previous in {
        "(",
        "[",
        "{",
        "=",
        ":",
        ",",
        ";",
        "!",
        "?",
        "+",
        "-",
        "*",
        "%",
        "&",
        "|",
        "^",
        "~",
        ">",
        "<",
    } or previous in {
        "return",
        "case",
        "throw",
        "delete",
        "typeof",
        "void",
        "new",
        "in",
        "of",
    }


def _find_regex_end(source: str, start: int) -> int:
    in_character_class = False
    index = start + 1
    while index < len(source):
        character = source[index]
        if character == "\\":
            index += 2
            continue
        if character in {"\n", "\r"}:
            return -1
        if character == "[":
            in_character_class = True
        elif character == "]":
            in_character_class = False
        elif character == "/" and not in_character_class:
            return index + 1
        index += 1
    return -1


def _find_quoted_end(source: str, start: int, quote: str) -> int:
    index = start + 1
    template_depth = 0
    while index < len(source):
        if source[index] == "\\":
            index += 2
            continue
        if quote == "`":
            if source.startswith("${", index):
                template_depth += 1
                index += 2
                continue
            if source[index] == "{" and template_depth > 0:
                template_depth += 1
                index += 1
                continue
            if source[index] == "}" and template_depth > 0:
                template_depth -= 1
                index += 1
                continue
        if source[index] == quote and template_depth == 0:
            return index
        index += 1
    return -1


def _make_token(
    kind: Literal["identifier", "literal", "punctuation", "other"],
    raw: str,
    start: int,
    end: int,
    line_starts: list[int],
) -> _Token:
    line_index = bisect_right(line_starts, start) - 1
    return _Token(
        kind,
        raw,
        start,
        end,
        line_index + 1,
        start - line_starts[line_index],
    )


def _split_top_level_tokens(
    tokens: list[_Token], delimiter: str = ","
) -> list[list[_Token]]:
    rows: list[list[_Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.raw in {"(", "[", "{"}:
            depth += 1
        elif token.raw in {")", "]", "}"}:
            depth -= 1
        if token.raw == delimiter and depth == 0:
            rows.append([])
        else:
            rows[-1].append(token)
    return rows


def _top_level_token_index(tokens: list[_Token], expected: str) -> int:
    depth = 0
    for index, token in enumerate(tokens):
        if token.raw in {"(", "[", "{"}:
            depth += 1
        elif token.raw in {")", "]", "}"}:
            depth -= 1
        elif token.raw == expected and depth == 0:
            return index
    return -1


def _static_catalog_key(tokens: list[_Token]) -> str | None:
    if len(tokens) != 1:
        return None
    token = tokens[0]
    if token.kind == "identifier":
        return token.raw
    if token.kind == "literal":
        return _decode_js_literal(token.raw)
    return None


def _render_locale_value(tokens: list[_Token]) -> _RenderedExpression | None:
    expression = tokens
    depth = 0
    for index in range(len(tokens) - 1):
        token = tokens[index]
        if token.raw in {"(", "[", "{"}:
            depth += 1
        elif token.raw in {")", "]", "}"}:
            depth -= 1
        elif token.raw == "=" and tokens[index + 1].raw == ">" and depth == 0:
            expression = tokens[index + 2 :]
            break
    return _render_expression(expression, [0])


def _collect_static_locale_value(
    tokens: list[_Token],
    path: LocalePath,
    result: dict[LocalePath, str],
) -> None:
    if not tokens:
        return
    if tokens[0].raw == "{" and _matching_token_index(tokens, 0) == len(tokens) - 1:
        for entry in _split_top_level_tokens(tokens[1:-1]):
            colon = _top_level_token_index(entry, ":")
            if colon <= 0:
                continue
            key = _static_catalog_key(entry[:colon])
            if key is None:
                continue
            _collect_static_locale_value(entry[colon + 1 :], (*path, key), result)
        return
    if tokens[0].raw == "[" and _matching_token_index(tokens, 0) == len(tokens) - 1:
        for index, entry in enumerate(_split_top_level_tokens(tokens[1:-1])):
            _collect_static_locale_value(entry, (*path, str(index)), result)
        return
    rendered = _render_locale_value(tokens)
    if rendered is None:
        return
    value = unicodedata.normalize("NFC", rendered.text).strip()
    if value:
        result[path] = value


def _embedded_locale_catalogs(bundle: str) -> dict[str, EmbeddedLocaleCatalog]:
    tokens = _tokenize_javascript(bundle)
    if tokens is None:
        return {}
    assignments: dict[str, list[_Token]] = {}
    for index, token in enumerate(tokens[:-2]):
        if (
            token.kind != "identifier"
            or tokens[index + 1].raw != "="
            or tokens[index + 2].raw != "{"
        ):
            continue
        end = _matching_token_index(tokens, index + 2)
        if end != -1:
            assignments[token.raw] = tokens[index + 2 : end + 1]
    for registry in assignments.values():
        locale_targets: dict[str, str] = {}
        for entry in _split_top_level_tokens(registry[1:-1]):
            colon = _top_level_token_index(entry, ":")
            if colon <= 0:
                continue
            locale_key = _static_catalog_key(entry[:colon])
            value = entry[colon + 1 :]
            if (
                locale_key is None
                or re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", locale_key)
                is None
                or len(value) != 1
                or value[0].kind != "identifier"
            ):
                continue
            locale_targets[_canonical_locale(locale_key)] = value[0].raw
        if len(locale_targets) < 3 or "en" not in locale_targets:
            continue
        generic_result: dict[str, EmbeddedLocaleCatalog] = {}
        for locale, variable in locale_targets.items():
            assigned = assignments.get(variable)
            if assigned is None:
                continue
            generic_catalog: dict[LocalePath, str] = {}
            _collect_static_locale_value(assigned, (), generic_catalog)
            if generic_catalog and len(generic_catalog) <= MAX_LOCALE_ENTRIES:
                generic_result[locale] = (f"locale:{locale}", generic_catalog)
        if "en" in generic_result:
            return generic_result
    targets: dict[str, tuple[str, str]] = {}
    for index, token in enumerate(tokens):
        match = re.fullmatch(r"STRINGS_([A-Z]{2,3}(?:_[A-Z0-9]{2,8})*)", token.raw)
        if match is None or index + 6 >= len(tokens):
            continue
        sequence = [row.raw for row in tokens[index + 1 : index + 6]]
        export_target_token = tokens[index + 6]
        if (
            sequence != [":", "(", ")", "=", ">"]
            or export_target_token.kind != "identifier"
        ):
            continue
        locale = _canonical_locale(match.group(1).replace("_", "-"))
        targets[export_target_token.raw] = (locale, token.raw)
    if "en" not in {locale for locale, _ in targets.values()}:
        return {}
    result: dict[str, EmbeddedLocaleCatalog] = {}
    for index, token in enumerate(tokens[:-2]):
        target_info = targets.get(token.raw)
        if (
            target_info is None
            or tokens[index + 1].raw != "="
            or tokens[index + 2].raw != "{"
        ):
            continue
        end = _matching_token_index(tokens, index + 2)
        if end == -1:
            continue
        static_catalog: dict[LocalePath, str] = {}
        _collect_static_locale_value(tokens[index + 2 : end + 1], (), static_catalog)
        if not static_catalog or len(static_catalog) > MAX_LOCALE_ENTRIES:
            continue
        locale, export_name = target_info
        result.setdefault(locale, (export_name, static_catalog))
    return result if "en" in result else {}


def _offset_location(source: str, offset: int) -> tuple[int, int]:
    prefix = source[:offset]
    return prefix.count("\n") + 1, offset - prefix.rfind("\n") - 1


def _readme_is_comment_start(line: str) -> bool:
    return line.lstrip().startswith("<!--")


def _readme_is_boundary(line: str) -> bool:
    return bool(
        not line.strip()
        or README_HEADING.match(line)
        or README_LIST_ITEM.match(line)
        or README_BLOCKQUOTE.match(line)
        or README_FENCE_START.match(line)
        or _readme_is_comment_start(line)
    )


def _render_readme_source(value: str) -> str | None:
    sentinel = "\ue000"

    def protect(label: str) -> str:
        return sentinel if label.strip() else ""

    rendered = README_LINKED_IMAGE.sub("", value)
    rendered = README_INLINE_CODE.sub(lambda match: protect(match.group(2)), rendered)
    rendered = README_INLINE_LINK.sub(
        lambda match: "" if match.group(1) == "!" else protect(match.group(2)),
        rendered,
    )
    rendered = README_REFERENCE_LINK.sub(
        lambda match: "" if match.group(1) == "!" else protect(match.group(2)),
        rendered,
    )
    rendered = README_AUTOLINK.sub(
        lambda match: protect(match.group(0)[1:-1]), rendered
    )
    rendered = README_HTML_TAG.sub("", rendered)
    token_index = 0

    def number_token(_match: re.Match[str]) -> str:
        nonlocal token_index
        token = f"{{{{th:expr:{token_index}}}}}"
        token_index += 1
        return token

    rendered = re.sub(sentinel, number_token, rendered)
    rendered = README_LIST_ITEM.sub("", rendered)
    rendered = re.sub(r"[*_~]+", "", rendered)
    rendered = (
        rendered.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    rendered = re.sub(r"\s+", " ", rendered).strip()
    semantic_text = README_DYNAMIC_TOKEN.sub("", rendered)
    if not rendered or not any(character.isalpha() for character in semantic_text):
        return None
    return unicodedata.normalize("NFC", rendered)


def _extract_readme_strings(content: bytes) -> list[str]:
    try:
        markdown = content.decode("utf-8")
    except UnicodeError as exc:
        raise AdapterContractError("adapter_readme_component_invalid") from exc
    lines = (
        markdown.removeprefix("\ufeff")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .split("\n")
    )
    values: set[str] = set()
    index = 0
    if lines and lines[0].strip() == "---":
        for cursor in range(1, len(lines)):
            if lines[cursor].strip() in {"---", "..."}:
                index = cursor + 1
                break

    def add(value: str) -> None:
        visible = README_LINKED_IMAGE.sub("", value)
        for pattern in (README_INLINE_LINK, README_REFERENCE_LINK):
            for match in pattern.finditer(visible):
                if match.group(1) == "!":
                    continue
                label = _render_readme_source(match.group(2))
                if label is not None:
                    values.add(label)
        rendered = _render_readme_source(visible)
        if rendered is not None:
            values.add(rendered)

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if _readme_is_comment_start(line):
            while index < len(lines):
                current = lines[index]
                index += 1
                if "-->" in current:
                    break
            continue
        fence = README_FENCE_START.match(line)
        if fence is not None:
            opening = fence.group(1)
            marker = re.escape(opening[0])
            closing = re.compile(rf"^\s{{0,3}}{marker}{{{len(opening)},}}\s*$")
            index += 1
            while index < len(lines):
                current = lines[index]
                index += 1
                if closing.match(current):
                    break
            continue
        if README_HEADING.match(line):
            value = README_HEADING.sub("", line)
            value = re.sub(r"\s+#+\s*$", "", value).strip()
            if not README_HORIZONTAL_RULE.match(line):
                add(value)
            index += 1
            continue
        if README_LIST_ITEM.match(line):
            block: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if not candidate.strip():
                    break
                if block and (
                    README_HEADING.match(candidate)
                    or README_BLOCKQUOTE.match(candidate)
                    or README_FENCE_START.match(candidate)
                    or _readme_is_comment_start(candidate)
                ):
                    break
                block.append(candidate)
                index += 1
            if not any(README_TABLE_ROW.match(candidate) for candidate in block):
                for candidate in block:
                    add(candidate)
            continue
        if README_BLOCKQUOTE.match(line):
            block = []
            while index < len(lines) and README_BLOCKQUOTE.match(lines[index]):
                block.append(re.sub(r"^\s{0,3}>\s?", "", lines[index]).strip())
                index += 1
            if not any(README_TABLE_ROW.match(candidate) for candidate in block):
                add("\n".join(candidate for candidate in block if candidate))
            continue
        block = []
        while index < len(lines) and not _readme_is_boundary(lines[index]):
            block.append(lines[index])
            index += 1
        if not block:
            index += 1
            continue
        if not any(
            README_TABLE_ROW.match(candidate) for candidate in block
        ) and not README_HORIZONTAL_RULE.match("\n".join(block)):
            add("\n".join(block).strip())
    return sorted(values)


def _evidence_sort_key(row: StringEvidence) -> tuple[int, str, str, str]:
    return (
        row["offset"] if row["offset"] is not None else -1,
        row["origin"],
        row["strategy"],
        row["symbol"],
    )


def _semantic_role(origins: set[StringOrigin]) -> SemanticRole:
    if "manifest.name" in origins or "registry.name" in origins:
        return "official-name"
    if "manifest.description" in origins or "registry.description" in origins:
        return "description"
    if "readme" in origins:
        return "readme"
    return "runtime-ui"


def _content_scopes(origins: list[StringOrigin]) -> list[str]:
    scopes: set[str] = set()
    if any(origin in {"ui-call", "ui-property"} for origin in origins):
        scopes.add("runtime-ui")
    if any(
        origin
        in {
            "manifest.name",
            "manifest.description",
            "registry.name",
            "registry.description",
        }
        for origin in origins
    ):
        scopes.add("metadata")
    if "readme" in origins:
        scopes.add("readme")
    return sorted(scopes)


def _canonical_source_key(origins: list[StringOrigin]) -> str | None:
    canonical_origins = {
        origin
        for origin in origins
        if origin not in {"registry.name", "registry.description"}
    }
    if canonical_origins & {"ui-call", "ui-property"}:
        return "runtime"
    if canonical_origins & {"manifest.name", "manifest.description"}:
        return "metadata"
    if "readme" in canonical_origins:
        return "documentation"
    return None


def _json_pointer_segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _flatten_locale_document(content: bytes) -> dict[str, str]:
    try:
        decoded: object = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterContractError("adapter_locale_component_invalid") from exc
    if not isinstance(decoded, dict):
        raise AdapterContractError("adapter_locale_component_invalid")
    flattened: dict[str, str] = {}

    def visit(value: object, path: tuple[str, ...], depth: int) -> None:
        if depth > MAX_LOCALE_DEPTH:
            raise AdapterContractError("adapter_locale_component_too_deep")
        if isinstance(value, dict):
            for raw_key, child in value.items():
                if not isinstance(raw_key, str) or raw_key == "":
                    raise AdapterContractError("adapter_locale_component_invalid")
                visit(child, (*path, raw_key), depth + 1)
            return
        if isinstance(value, list):
            raise AdapterContractError("adapter_locale_component_invalid")
        if not isinstance(value, str):
            return
        normalized = unicodedata.normalize("NFC", value).strip()
        if normalized == "":
            return
        resource_key = "/" + "/".join(_json_pointer_segment(part) for part in path)
        flattened[resource_key] = normalized
        if len(flattened) > MAX_LOCALE_ENTRIES:
            raise AdapterContractError("adapter_locale_component_too_many_entries")

    visit(decoded, (), 0)
    return flattened


def _flatten_typescript_locale_document(content: bytes) -> dict[str, str]:
    try:
        source = content.decode("utf-8")
    except UnicodeError as exc:
        raise AdapterContractError("adapter_locale_component_invalid") from exc
    tokens = _tokenize_javascript(source)
    if tokens is None:
        raise AdapterContractError("adapter_locale_component_invalid")

    named_objects: dict[str, int] = {}
    depth = 0
    for index, token in enumerate(tokens):
        if (
            depth == 0
            and token.raw == "const"
            and index + 3 < len(tokens)
            and tokens[index + 1].kind == "identifier"
            and tokens[index + 2].raw == "="
            and tokens[index + 3].raw == "{"
        ):
            named_objects[tokens[index + 1].raw] = index + 3
        if token.raw in {"(", "[", "{"}:
            depth += 1
        elif token.raw in {")", "]", "}"}:
            depth = max(0, depth - 1)

    object_start = -1
    for index, token in enumerate(tokens):
        if token.raw != "export":
            continue
        if index + 2 < len(tokens) and tokens[index + 1].raw == "default":
            if tokens[index + 2].raw == "{":
                object_start = index + 2
                break
            if tokens[index + 2].kind == "identifier":
                object_start = named_objects.get(tokens[index + 2].raw, -1)
                if object_start != -1:
                    break
            continue
        if index + 3 >= len(tokens) or tokens[index + 1].raw != "const":
            continue
        depth = 0
        for cursor in range(index + 3, len(tokens) - 1):
            raw = tokens[cursor].raw
            if raw in {"(", "[", "{"}:
                depth += 1
            elif raw in {
                ")",
                "]",
                "}",
            }:
                depth -= 1
            elif raw == "=" and depth == 0 and tokens[cursor + 1].raw == "{":
                object_start = cursor + 1
                break
            elif raw == ";" and depth == 0:
                break
        if object_start != -1:
            break
    if object_start == -1:
        raise AdapterContractError("adapter_locale_component_invalid")
    object_end = _matching_token_index(tokens, object_start)
    if object_end == -1:
        raise AdapterContractError("adapter_locale_component_invalid")
    catalog: dict[LocalePath, str] = {}
    _collect_static_locale_value(tokens[object_start : object_end + 1], (), catalog)
    if not catalog or len(catalog) > MAX_LOCALE_ENTRIES:
        raise AdapterContractError("adapter_locale_component_invalid")
    return {_locale_resource_key(path): value for path, value in catalog.items()}


def _flatten_locale_component(name: str, content: bytes) -> dict[str, str]:
    if name.lower().endswith(".json"):
        return _flatten_locale_document(content)
    if name.lower().endswith(".ts"):
        return _flatten_typescript_locale_document(content)
    raise AdapterContractError("adapter_locale_component_invalid")


def _record_native_locale_candidate(
    *,
    entries_by_string: dict[str, NativeLocaleCoverageEntry],
    targets_by_string: dict[str, str],
    rejected_strings: set[str],
    scanned: SnapshotString,
    target: str,
    resource_key: str,
) -> None:
    string_key = scanned["key"]
    if string_key in rejected_strings:
        return
    if (
        scanned["source"] == target
        or _placeholder_signature(target) != scanned["placeholder_signature"]
    ):
        entries_by_string.pop(string_key, None)
        targets_by_string.pop(string_key, None)
        rejected_strings.add(string_key)
        return
    existing_target = targets_by_string.get(string_key)
    if existing_target is not None and existing_target != target:
        entries_by_string.pop(string_key, None)
        targets_by_string.pop(string_key, None)
        rejected_strings.add(string_key)
        return
    targets_by_string[string_key] = target
    entries_by_string.setdefault(
        string_key,
        {
            "placeholder_signature": scanned["placeholder_signature"],
            "resource_key": resource_key,
            "string_key": string_key,
        },
    )


def _build_native_locale_coverage(
    strings: list[SnapshotString],
    locale_components: LocaleComponents,
) -> dict[str, _NativeLocaleCoverageState]:
    english = locale_components.get("en", [])
    if not english:
        return {}
    english_by_resource: dict[str, tuple[str, bytes, dict[str, str]]] = {}
    for resource_id, name, content in english:
        try:
            document = _flatten_locale_component(name, content)
        except AdapterContractError:
            continue
        english_by_resource[resource_id] = (name, content, document)
    if not english_by_resource:
        return {}
    scanned_by_source = {row["source"]: row for row in strings}
    result: dict[str, _NativeLocaleCoverageState] = {}
    for locale in sorted(locale_components):
        if locale == "en":
            continue
        target_by_resource = {
            resource_id: (name, content)
            for resource_id, name, content in locale_components[locale]
        }
        matched_resource_ids = sorted(
            set(english_by_resource) & set(target_by_resource)
        )
        entries_by_string: dict[str, NativeLocaleCoverageEntry] = {}
        targets_by_string: dict[str, str] = {}
        rejected_strings: set[str] = set()
        source_resources: list[LocaleComponent] = []
        target_resources: list[LocaleComponent] = []
        for resource_id in matched_resource_ids:
            source_name, source_content, source_document = english_by_resource[
                resource_id
            ]
            resource_name, resource_content = target_by_resource[resource_id]
            try:
                target_document = _flatten_locale_component(
                    resource_name, resource_content
                )
            except AdapterContractError:
                # A number of official repositories keep placeholder locale modules
                # that merely re-export English. They prove no translated coverage.
                continue
            source_resources.append((resource_id, source_name, source_content))
            target_resources.append((resource_id, resource_name, resource_content))
            for resource_key in sorted(set(source_document) & set(target_document)):
                source = source_document[resource_key]
                target = target_document[resource_key]
                scanned = scanned_by_source.get(source)
                if scanned is None or scanned["semantic_role"] != "runtime-ui":
                    continue
                _record_native_locale_candidate(
                    entries_by_string=entries_by_string,
                    targets_by_string=targets_by_string,
                    rejected_strings=rejected_strings,
                    scanned=scanned,
                    target=target,
                    resource_key=(
                        resource_key
                        if resource_id == "default"
                        else f"{resource_id}:{resource_key}"
                    ),
                )
        if source_resources and target_resources:
            source_resource_name, source_resource_digest = (
                _locale_component_set_identity(source_resources)
            )
            resource_name, resource_digest = _locale_component_set_identity(
                target_resources
            )
            result[locale] = _NativeLocaleCoverageState(
                carrier="explicit",
                entries_by_string=entries_by_string,
                targets_by_string=targets_by_string,
                rejected_strings=rejected_strings,
                resource_digest=resource_digest,
                resource_name=resource_name,
                source_resource_digest=source_resource_digest,
                source_resource_name=source_resource_name,
            )
    return result


def _locale_component_set_identity(
    components: list[LocaleComponent],
) -> tuple[str, str]:
    if len(components) == 1:
        _, name, content = components[0]
        return name, hashlib.sha256(content).hexdigest()
    descriptor = [
        {
            "resource_id": resource_id,
            "name": name,
            "digest": hashlib.sha256(content).hexdigest(),
        }
        for resource_id, name, content in sorted(components)
    ]
    return (
        f"locale-component-set:{len(descriptor)}",
        hashlib.sha256(_canonical_json({"components": descriptor})).hexdigest(),
    )


def _normalize_locale_components(
    value: LocaleComponents | dict[str, tuple[str, bytes]] | None,
) -> LocaleComponents:
    normalized: LocaleComponents = {}
    for raw_locale, raw in (value or {}).items():
        locale = _canonical_locale(raw_locale)
        if isinstance(raw, tuple):
            resources = [("default", raw[0], raw[1])]
        else:
            resources = list(raw)
        target = normalized.setdefault(locale, [])
        for resource in resources:
            if any(existing[0] == resource[0] for existing in target):
                raise AdapterContractError("adapter_locale_component_invalid")
            target.append(resource)
    return normalized


def _locale_resource_key(path: LocalePath) -> str:
    return "/" + "/".join(_json_pointer_segment(part) for part in path)


def _build_embedded_locale_coverage(
    strings: list[SnapshotString],
    catalogs: dict[str, EmbeddedLocaleCatalog],
    bundle_content: bytes,
) -> dict[str, _NativeLocaleCoverageState]:
    english = catalogs.get("en")
    if english is None:
        return {}
    source_export, source_document = english
    scanned_by_source = {row["source"]: row for row in strings}
    bundle_digest = hashlib.sha256(bundle_content).hexdigest()
    result: dict[str, _NativeLocaleCoverageState] = {}
    for locale in sorted(catalogs):
        if locale == "en":
            continue
        export_name, target_document = catalogs[locale]
        entries_by_string: dict[str, NativeLocaleCoverageEntry] = {}
        targets_by_string: dict[str, str] = {}
        rejected_strings: set[str] = set()
        for path in sorted(set(source_document) & set(target_document)):
            source = source_document[path]
            target = target_document[path]
            scanned = scanned_by_source.get(source)
            if scanned is None:
                continue
            _record_native_locale_candidate(
                entries_by_string=entries_by_string,
                targets_by_string=targets_by_string,
                rejected_strings=rejected_strings,
                scanned=scanned,
                target=target,
                resource_key=_locale_resource_key(path),
            )
        result[locale] = _NativeLocaleCoverageState(
            carrier="embedded",
            entries_by_string=entries_by_string,
            targets_by_string=targets_by_string,
            rejected_strings=rejected_strings,
            resource_digest=bundle_digest,
            resource_name=f"main.js#{export_name}",
            source_resource_digest=bundle_digest,
            source_resource_name=f"main.js#{source_export}",
        )
    return result


def _merged_resource_identity(
    states: list[_NativeLocaleCoverageState],
    *,
    source: bool,
) -> tuple[str, str]:
    if len(states) == 1:
        state = states[0]
        return (
            (state.source_resource_name, state.source_resource_digest)
            if source
            else (state.resource_name, state.resource_digest)
        )
    descriptor = [
        {
            "carrier": state.carrier,
            "digest": (
                state.source_resource_digest if source else state.resource_digest
            ),
            "name": state.source_resource_name if source else state.resource_name,
        }
        for state in states
    ]
    kind = "source" if source else "target"
    carriers = "+".join(state.carrier for state in states)
    return (
        f"native-locale-{kind}-set:{carriers}",
        hashlib.sha256(_canonical_json({"resources": descriptor})).hexdigest(),
    )


def _merge_native_locale_coverage(
    *coverage_groups: dict[str, _NativeLocaleCoverageState],
) -> list[NativeLocaleCoverage]:
    result: list[NativeLocaleCoverage] = []
    locales = sorted({locale for group in coverage_groups for locale in group})
    for locale in locales:
        states = sorted(
            (group[locale] for group in coverage_groups if locale in group),
            key=lambda state: state.carrier,
        )
        rejected_strings = {
            string_key for state in states for string_key in state.rejected_strings
        }
        targets_by_string: dict[str, str] = {}
        entries_by_string: dict[str, NativeLocaleCoverageEntry] = {}
        for state in states:
            for string_key in sorted(state.targets_by_string):
                if string_key in rejected_strings:
                    continue
                target = state.targets_by_string[string_key]
                existing_target = targets_by_string.get(string_key)
                if existing_target is not None and existing_target != target:
                    targets_by_string.pop(string_key, None)
                    entries_by_string.pop(string_key, None)
                    rejected_strings.add(string_key)
                    continue
                targets_by_string[string_key] = target
                candidate = state.entries_by_string[string_key]
                existing_entry = entries_by_string.get(string_key)
                if (
                    existing_entry is None
                    or candidate["resource_key"] < existing_entry["resource_key"]
                ):
                    entries_by_string[string_key] = candidate
        for string_key in rejected_strings:
            targets_by_string.pop(string_key, None)
            entries_by_string.pop(string_key, None)
        if not entries_by_string:
            continue
        source_resource_name, source_resource_digest = _merged_resource_identity(
            states, source=True
        )
        resource_name, resource_digest = _merged_resource_identity(states, source=False)
        result.append(
            {
                "covered_entries": sorted(
                    entries_by_string.values(), key=lambda item: item["string_key"]
                ),
                "locale": locale,
                "resource_digest": resource_digest,
                "resource_name": resource_name,
                "source_resource_digest": source_resource_digest,
                "source_resource_name": source_resource_name,
            }
        )
    return result


def build_snapshot(
    manifest_content: bytes,
    bundle_content: bytes,
    *,
    registry_metadata_content: bytes | None = None,
    readme_content: bytes | None = None,
    native_locale_components: (
        LocaleComponents | dict[str, tuple[str, bytes]] | None
    ) = None,
) -> bytes:
    manifest = _decode_manifest(manifest_content)
    plugin_id = _manifest_value(manifest, "id")
    if not PLUGIN_ID_PATTERN.fullmatch(plugin_id):
        raise AdapterContractError("plugin_manifest_id_invalid")
    plugin_name = _manifest_value(manifest, "name")
    plugin_version = _manifest_value(manifest, "version")
    description = _manifest_value(manifest, "description")
    registry_metadata = (
        _decode_registry_metadata(registry_metadata_content, plugin_id)
        if registry_metadata_content is not None
        else None
    )
    try:
        bundle = _normalize_community_bundle(bundle_content.decode("utf-8"))
    except UnicodeError as exc:
        raise AdapterContractError("plugin_bundle_utf8_invalid") from exc
    # The community installer removes the inline source map and appends a
    # nosourcemap marker, so the installed artifact never hashes like the raw
    # release asset.  Normalize the authoritative bundle to the same logical
    # artifact before extracting strings and computing the catalog digest.
    bundle_content = bundle.encode("utf-8")

    embedded_locale_catalogs = _embedded_locale_catalogs(bundle)
    collected: dict[str, tuple[set[StringOrigin], dict[str, StringEvidence]]] = {}
    _add_candidate(
        collected,
        plugin_name,
        "manifest.name",
        {
            "origin": "manifest.name",
            "strategy": "manifest",
            "symbol": "manifest.name",
            "offset": None,
            "line": None,
            "column": None,
        },
    )
    _add_candidate(
        collected,
        description,
        "manifest.description",
        {
            "origin": "manifest.description",
            "strategy": "manifest",
            "symbol": "manifest.description",
            "offset": None,
            "line": None,
            "column": None,
        },
    )
    if registry_metadata is not None:
        registry_name, registry_description = registry_metadata
        _add_candidate(
            collected,
            registry_name,
            "registry.name",
            {
                "origin": "registry.name",
                "strategy": "registry",
                "symbol": "community-plugins.name",
                "offset": None,
                "line": None,
                "column": None,
            },
        )
        _add_candidate(
            collected,
            registry_description,
            "registry.description",
            {
                "origin": "registry.description",
                "strategy": "registry",
                "symbol": "community-plugins.description",
                "offset": None,
                "line": None,
                "column": None,
            },
        )
    if readme_content is not None:
        if (
            len(readme_content) > MAX_README_COMPONENT_BYTES
            or b"\x00" in readme_content
        ):
            raise AdapterContractError("adapter_readme_component_invalid")
        for source in _extract_readme_strings(readme_content):
            _add_candidate(
                collected,
                source,
                "readme",
                {
                    "origin": "readme",
                    "strategy": "markdown",
                    "symbol": "README.md",
                    "offset": None,
                    "line": None,
                    "column": None,
                },
            )
    normalized_locale_components = _normalize_locale_components(
        native_locale_components
    )
    # Locale components prove upstream-native coverage only. Candidates not
    # proven by the client-equivalent scan of the exact release bundle,
    # manifest and README must not expand the canonical source catalog.
    if "en" in embedded_locale_catalogs:
        export_name, source_document = embedded_locale_catalogs["en"]
        for path, source in sorted(source_document.items()):
            _add_candidate(
                collected,
                source,
                "ui-property",
                {
                    "origin": "ui-property",
                    "strategy": "structured",
                    "symbol": f"{export_name}:{_locale_resource_key(path)}",
                    "offset": None,
                    "line": None,
                    "column": None,
                },
            )
    # An embedded English catalog is merged as upstream-native evidence, but
    # it is often partial (Style Settings ships a tiny locale pack over a
    # large hardcoded UI). The UI scan always runs so hardcoded strings are
    # still collected; same-source entries deduplicate and keep their native
    # target metadata.
    if not _collect_structured_matches(bundle, collected):
        _collect_regex_matches(bundle, UI_CALL, collected, "ui-call", "ui-call")
        _collect_regex_matches(
            bundle, OPTION_CALL, collected, "ui-call", "addOption", 2
        )
        _collect_regex_matches(
            bundle, UI_PROPERTY, collected, "ui-property", "ui-property"
        )
        _collect_regex_matches(
            bundle,
            TEXT_CONTENT_ASSIGNMENT,
            collected,
            "ui-property",
            "textContent",
            ui_context_verified=True,
        )
        _collect_regex_matches(
            bundle,
            INNER_TEXT_ASSIGNMENT,
            collected,
            "ui-property",
            "innerText",
            ui_context_verified=True,
            accept_rendered=_single_line_text,
        )
        _collect_regex_matches(
            bundle,
            INNER_HTML_ASSIGNMENT,
            collected,
            "ui-property",
            "innerHTML",
            ui_context_verified=True,
            transform_rendered=_render_inner_html_text,
        )
        _collect_regex_matches(
            bundle,
            OBSIDIAN_CREATE_TEXT,
            collected,
            "ui-property",
            "createEl",
            ui_context_verified=True,
        )
        _collect_add_options_regex_matches(bundle, collected)
        _collect_regex_matches(
            bundle,
            REACT_DEFAULT_CREATE_ELEMENT_CHILD,
            collected,
            "ui-call",
            "createElement",
        )
        _collect_regex_matches(
            bundle,
            REACT_DEFAULT_CREATE_ELEMENT_PROPERTY,
            collected,
            "ui-property",
            "createElement",
            ui_context_verified=True,
        )

    strings: list[SnapshotString] = []
    for source in sorted(collected):
        normalized = unicodedata.normalize("NFC", source)
        key = hashlib.sha256(f"{plugin_id}\0{normalized}".encode()).hexdigest()[:32]
        origins, evidence = collected[source]
        sorted_evidence: list[StringEvidence] = sorted(
            list(evidence.values()), key=_evidence_sort_key
        )
        strings.append(
            {
                "evidence": sorted_evidence,
                "key": key,
                "origins": sorted(origins),
                "semantic_role": _semantic_role(origins),
                "placeholder_signature": _placeholder_signature(source),
                "source": normalized,
            }
        )

    artifact_digest = hashlib.sha256(bundle_content).hexdigest()
    explicit_coverage = _build_native_locale_coverage(
        strings, normalized_locale_components
    )
    embedded_coverage = _build_embedded_locale_coverage(
        strings, embedded_locale_catalogs, bundle_content
    )
    native_locale_coverage = _merge_native_locale_coverage(
        embedded_coverage, explicit_coverage
    )
    covered_locales_by_string: dict[str, list[str]] = {}
    for coverage in native_locale_coverage:
        for entry in coverage["covered_entries"]:
            covered_locales_by_string.setdefault(entry["string_key"], []).append(
                coverage["locale"]
            )
    canonical_strings = [
        (row, source_key)
        for row in strings
        if (source_key := _canonical_source_key(row["origins"])) is not None
    ]
    source_definitions = {
        "runtime": {
            "key": "runtime",
            "logical_path": "main.js",
            "format_family": "javascript",
        },
        "metadata": {
            "key": "metadata",
            "logical_path": "manifest.json",
            "format_family": "json",
        },
        "documentation": {
            "key": "documentation",
            "logical_path": "README.md",
            "format_family": "markdown",
        },
    }
    active_source_keys = {source_key for _, source_key in canonical_strings}
    source_catalog = {
        "protocol": "trans-hub.canonical-source-catalog",
        "revision": 2,
        "resource": {
            "resource_key": plugin_id,
            "object_kind_key": "plugin",
            "name": plugin_name,
            "version": plugin_version,
            "version_scheme": "semver",
            "content_digest": artifact_digest,
        },
        "stream": {
            "stream_key": f"community-resource:{plugin_id}",
            "locale": "en",
        },
        "sources": [
            source_definitions[source_key]
            for source_key in ("runtime", "metadata", "documentation")
            if source_key in active_source_keys
        ],
        "units": [
            {
                "key": row["key"],
                "source_key": source_key,
                "text": row["source"],
                "placeholder_signature": row["placeholder_signature"],
                "format_signature": "plain-text-v1",
                "context": {
                    "content_scopes": _content_scopes(row["origins"]),
                    "semantic_role": row["semantic_role"],
                    "origins": row["origins"],
                    "evidence": row["evidence"],
                    "upstream_locale_coverage": sorted(
                        set(covered_locales_by_string.get(row["key"], []))
                    ),
                },
            }
            for row, source_key in canonical_strings
        ],
    }
    return _canonical_json(
        {
            "adapter": "obsidian",
            "artifact_digest": artifact_digest,
            "contract_revision": CONTRACT_REVISION,
            "parser": PARSER_ID,
            "plugin": {
                "description": description,
                "id": plugin_id,
                "name": plugin_name,
                "version": plugin_version,
            },
            "source_locale": "en",
            "strings": strings,
            "source_catalog": source_catalog,
            "native_locale_coverage": native_locale_coverage,
        }
    )


def run_adapter(request_path: Path, output_dir: Path) -> Path:
    request = _read_request(request_path)
    (
        manifest_content,
        bundle_content,
        registry_metadata,
        readme_content,
        locale_components,
    ) = _required_components(request)
    if not output_dir.is_dir():
        raise AdapterContractError("adapter_output_directory_invalid")
    snapshot_path = output_dir / "snapshot.bin"
    snapshot_path.write_bytes(
        build_snapshot(
            manifest_content,
            bundle_content,
            registry_metadata_content=registry_metadata,
            readme_content=readme_content,
            native_locale_components=locale_components,
        )
    )
    return snapshot_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_adapter(args.request, args.output)


if __name__ == "__main__":
    main()
