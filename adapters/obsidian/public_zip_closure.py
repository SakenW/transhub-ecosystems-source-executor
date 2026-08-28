"""Stdlib-only ZIP closure for the public trusted executor.

This module is deliberately independent from Adapter Plane and Trans-Hub Core.
It treats every byte from a public release as data: it never imports, evaluates,
or executes a file contained in the archive.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_COMPONENT_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 128
MAX_ARCHIVE_EXPANDED_BYTES = 80 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100


class PublicZipClosureError(ValueError):
    """The untrusted archive cannot form the fixed parser input."""


@dataclass(frozen=True, slots=True)
class PublicZipComponent:
    """One selected plugin file, never an executable module."""

    role: str
    name: str
    content: bytes


def components_from_zip(zip_bytes: bytes) -> tuple[PublicZipComponent, ...]:
    """Select a bounded Obsidian plugin closure without exposing ZIP metadata."""

    if not zip_bytes or len(zip_bytes) > MAX_ZIP_BYTES:
        raise PublicZipClosureError("obsidian_connector_zip_size_invalid")
    try:
        with ZipFile(BytesIO(zip_bytes)) as archive:
            archive_entries = archive.infolist()
            if not archive_entries or len(archive_entries) > MAX_ARCHIVE_ENTRIES:
                raise PublicZipClosureError("obsidian_connector_zip_entries_invalid")
            seen_paths: set[str] = set()
            selected_entries: list[tuple[ZipInfo, str, PurePosixPath]] = []
            expanded_size = 0
            for entry in archive_entries:
                path = _validate_entry(entry)
                if entry.filename in seen_paths:
                    raise PublicZipClosureError(
                        "obsidian_connector_zip_path_duplicate"
                    )
                seen_paths.add(entry.filename)
                if entry.is_dir():
                    continue
                expanded_size += entry.file_size
                if expanded_size > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise PublicZipClosureError("obsidian_connector_zip_expanded_too_large")
                role = _component_role(path)
                if role is not None:
                    selected_entries.append((entry, role, path))

            plugin_root = _plugin_root(selected_entries)
            selected: dict[str, PublicZipComponent] = {}
            for entry, role, path in selected_entries:
                _validate_component_root(path, role, plugin_root)
                component = PublicZipComponent(role, path.name, archive.read(entry))
                if component.role in selected:
                    raise PublicZipClosureError("obsidian_connector_zip_component_duplicate")
                selected[component.role] = component
    except BadZipFile as exc:
        raise PublicZipClosureError("obsidian_connector_zip_invalid") from exc
    components = tuple(sorted(selected.values(), key=lambda item: (item.role, item.name)))
    _validate_components(components)
    return components


def source_revision(zip_bytes: bytes) -> str:
    """Return a content identity without retaining the raw archive."""

    return sha256(zip_bytes).hexdigest()


def _validate_components(components: tuple[PublicZipComponent, ...]) -> None:
    roles = {component.role for component in components}
    if {"manifest", "main"} - roles:
        raise PublicZipClosureError("obsidian_connector_closure_incomplete")


def _validate_entry(entry: ZipInfo) -> PurePosixPath:
    name = entry.filename
    path = PurePosixPath(name)
    parts = name.removesuffix("/").split("/")
    unix_mode = (entry.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    expected_type = stat.S_IFDIR if entry.is_dir() else stat.S_IFREG
    if (
        not name
        or path.is_absolute()
        or not name.isascii()
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in name
        or "\x00" in name
        or entry.flag_bits & 0x1
        or entry.file_size > MAX_COMPONENT_BYTES
        or (entry.compress_size == 0 and entry.file_size > 0)
        or (
            entry.compress_size > 0
            and entry.file_size > entry.compress_size * MAX_COMPRESSION_RATIO
        )
        or entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
        or file_type not in {0, expected_type}
    ):
        raise PublicZipClosureError("obsidian_connector_zip_entry_invalid")
    return path


def _component_role(path: PurePosixPath) -> str | None:
    parts = path.parts
    leaf = path.name
    if leaf == "manifest.json":
        return "manifest"
    if leaf == "main.js":
        return "main"
    if leaf == "README.md":
        return "readme"
    if leaf == "community-plugin.json":
        return "registry-metadata"
    if len(parts) >= 2 and parts[-2] == "locales" and leaf.endswith(".json"):
        locale = leaf.removesuffix(".json")
        if not locale:
            raise PublicZipClosureError("obsidian_connector_locale_invalid")
        return f"locale:{locale}"
    return None


def _plugin_root(
    entries: list[tuple[ZipInfo, str, PurePosixPath]],
) -> PurePosixPath:
    manifest_roots = [path.parent for _, role, path in entries if role == "manifest"]
    if not manifest_roots:
        raise PublicZipClosureError("obsidian_connector_closure_incomplete")
    if len(manifest_roots) > 1:
        raise PublicZipClosureError("obsidian_connector_zip_component_duplicate")
    return manifest_roots[0]


def _validate_component_root(
    path: PurePosixPath, role: str, plugin_root: PurePosixPath
) -> None:
    expected_parent = plugin_root / "locales" if role.startswith("locale:") else plugin_root
    if path.parent != expected_parent:
        raise PublicZipClosureError("obsidian_connector_zip_plugin_root_invalid")
