"""Content-locked host for the fail-closed public Obsidian executor candidate.

The host accepts two already bounded release components in memory and owns the
offline Docker boundary.  It never persists component bytes and deliberately
does not implement task claiming, source download, OIDC, result grants, Kodo,
or any other network operation.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Final, Literal, TypeVar
from uuid import uuid4

from .build_public_executor import PROFILE_PATH, canonical_json
from .component_bridge import (
    MAX_MAIN_BYTES,
    MAX_MANIFEST_BYTES,
    PUBLIC_EXECUTOR_PROTOCOL,
    PUBLIC_RESULT_PROTOCOL,
    PUBLIC_RESULT_REVISION,
    _canonical_json,
    _parse_unique_json,
    validate_public_source_catalog,
)

FIXED_PYTHON_IMAGE: Final = (
    "python@sha256:399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0"
)
MAX_RESULT_BYTES: Final = 64 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991
_CONTAINER_EXECUTOR_PATH: Final = "/opt/trans-hub/public-executor.pyz"
_Phase = Literal["components_validated", "adapter_completed", "before_upload"]
_T = TypeVar("_T")


class OfflineExecutorHostError(RuntimeError):
    """The local candidate could not preserve its isolation contract."""


@dataclass(frozen=True, slots=True)
class OfflineExecutorComponent:
    """One exact inert GitHub Release asset selected by the source plan."""

    role: Literal["manifest", "main"]
    name: Literal["manifest.json", "main.js"]
    content: bytes


@dataclass(frozen=True, slots=True)
class OfflineExecutorTask:
    """Exact immutable inputs received from a future trusted control plane."""

    adapter_artifact_digest: str
    adapter_profile_digest: str
    expected_manifest_digest: str
    expected_manifest_size: int
    expected_main_digest: str
    expected_main_size: int
    materialization_target_digest: str
    policy_revision: int
    result_max_bytes: int

    def __post_init__(self) -> None:
        if (
            any(
                _DIGEST.fullmatch(value) is None
                for value in (
                    self.adapter_artifact_digest,
                    self.adapter_profile_digest,
                    self.expected_manifest_digest,
                    self.expected_main_digest,
                    self.materialization_target_digest,
                )
            )
            or self.expected_manifest_size < 1
            or self.expected_manifest_size > MAX_MANIFEST_BYTES
            or self.expected_main_size < 1
            or self.expected_main_size > MAX_MAIN_BYTES
            or self.result_max_bytes < 1
            or self.result_max_bytes > MAX_RESULT_BYTES
            or isinstance(self.policy_revision, bool)
            or self.policy_revision < 1
            or self.policy_revision > _MAX_SAFE_INTEGER
        ):
            raise OfflineExecutorHostError("offline_executor_task_invalid")


Runner = Callable[
    [
        tuple[str, ...],
        tuple[OfflineExecutorComponent, ...],
        BinaryIO,
        OfflineExecutorTask,
    ],
    None,
]
PhaseHook = Callable[[_Phase, Path | None], None]
Uploader = Callable[[bytes], _T]


class OfflineExecutorHost:
    """Parse one exact in-memory component closure and hand off structured JSON."""

    def __init__(
        self,
        *,
        executor_artifact: Path,
        runner: Runner | None = None,
        phase_hook: PhaseHook | None = None,
    ) -> None:
        self._executor_artifact = executor_artifact
        self._runner = runner or _run_docker
        self._phase_hook = phase_hook or (lambda _phase, _path: None)

    def prepare_result(
        self,
        components: tuple[OfflineExecutorComponent, ...],
        task: OfflineExecutorTask,
    ) -> bytes:
        """Return a verified structured result without persisting source bytes."""

        _disable_core_dumps()
        _validate_components(components, task)
        self._phase_hook("components_validated", None)
        with tempfile.TemporaryDirectory(
            prefix="trans-hub-public-discovery-"
        ) as temporary:
            temporary_root = Path(temporary)
            temporary_root.chmod(0o700)
            staged_executor = temporary_root / "public-executor.pyz"
            _stage_executor(
                self._executor_artifact,
                staged_executor,
                expected_artifact_digest=task.adapter_artifact_digest,
                expected_profile_digest=task.adapter_profile_digest,
            )
            with tempfile.TemporaryFile(mode="w+b") as output:
                container_name = "trans-hub-public-discovery-" + uuid4().hex
                command = docker_run_command(
                    staged_executor=staged_executor,
                    container_name=container_name,
                )
                self._runner(command, components, output, task)
                self._phase_hook("adapter_completed", None)
                output_size = output.tell()
                if output_size < 1 or output_size > task.result_max_bytes:
                    raise OfflineExecutorHostError(
                        "offline_executor_result_size_invalid"
                    )
                output.seek(0)
                result = output.read()
            _validate_result(result, task)
        return result

    def prepare_and_upload(
        self,
        components: tuple[OfflineExecutorComponent, ...],
        task: OfflineExecutorTask,
        uploader: Uploader[_T],
    ) -> _T:
        """Invoke the caller's structured-only upload after parser validation."""

        result = self.prepare_result(components, task)
        self._phase_hook("before_upload", None)
        return uploader(result)


def docker_run_command(
    *, staged_executor: Path, container_name: str
) -> tuple[str, ...]:
    """Build the immutable Docker argv; component bytes never appear in it."""

    if (
        not staged_executor.is_absolute()
        or not staged_executor.is_file()
        or staged_executor.is_symlink()
        or not re.fullmatch(r"trans-hub-public-discovery-[0-9a-f]{32}", container_name)
    ):
        raise OfflineExecutorHostError("offline_executor_container_input_invalid")
    return (
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--name",
        container_name,
        "--network",
        "none",
        "--pull=never",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--ulimit",
        "core=0",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--cpus",
        "1",
        "--user",
        "65534:65534",
        "--workdir",
        "/tmp",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
        "--mount",
        (
            f"type=bind,source={staged_executor},"
            f"target={_CONTAINER_EXECUTOR_PATH},readonly,bind-nonrecursive"
        ),
        "--entrypoint",
        "python3",
        FIXED_PYTHON_IMAGE,
        "-I",
        "-B",
        _CONTAINER_EXECUTOR_PATH,
    )


def _stage_executor(
    source: Path,
    destination: Path,
    *,
    expected_artifact_digest: str,
    expected_profile_digest: str,
) -> None:
    try:
        source_stat = source.lstat()
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineExecutorHostError("offline_executor_artifact_unavailable") from exc
    if (
        not stat.S_ISREG(source_stat.st_mode)
        or source.is_symlink()
        or sha256(canonical_json(profile)).hexdigest() != expected_profile_digest
        or profile.get("container")
        != {
            "capDrop": "ALL",
            "image": FIXED_PYTHON_IMAGE,
            "network": "none",
            "noNewPrivileges": True,
            "pull": "never",
            "readOnly": True,
        }
    ):
        raise OfflineExecutorHostError("offline_executor_profile_mismatch")
    artifact = source.read_bytes()
    if sha256(artifact).hexdigest() != expected_artifact_digest:
        raise OfflineExecutorHostError("offline_executor_artifact_digest_mismatch")
    destination.write_bytes(artifact)
    destination.chmod(0o444)


def _disable_core_dumps() -> None:
    try:
        import resource

        _, hard_limit = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, hard_limit))
    except (ImportError, OSError, ValueError) as exc:
        raise OfflineExecutorHostError("offline_executor_core_dump_disable_failed") from exc


def _validate_components(
    components: tuple[OfflineExecutorComponent, ...], task: OfflineExecutorTask
) -> None:
    if not isinstance(components, tuple) or len(components) != 2:
        raise OfflineExecutorHostError("offline_executor_component_closure_invalid")
    selected: dict[str, OfflineExecutorComponent] = {}
    for component in components:
        if not isinstance(component, OfflineExecutorComponent):
            raise OfflineExecutorHostError("offline_executor_component_closure_invalid")
        expected_name = {"manifest": "manifest.json", "main": "main.js"}.get(
            component.role
        )
        if (
            expected_name is None
            or component.name != expected_name
            or component.role in selected
            or not isinstance(component.content, bytes)
            or not component.content
        ):
            raise OfflineExecutorHostError("offline_executor_component_closure_invalid")
        selected[component.role] = component
    if set(selected) != {"manifest", "main"}:
        raise OfflineExecutorHostError("offline_executor_component_closure_incomplete")
    evidence = {
        "manifest": (task.expected_manifest_size, task.expected_manifest_digest),
        "main": (task.expected_main_size, task.expected_main_digest),
    }
    for role, component in selected.items():
        expected_size, expected_digest = evidence[role]
        if (
            len(component.content) != expected_size
            or sha256(component.content).hexdigest() != expected_digest
        ):
            raise OfflineExecutorHostError("offline_executor_component_evidence_mismatch")


def _run_docker(
    command: tuple[str, ...],
    components: tuple[OfflineExecutorComponent, ...],
    output: BinaryIO,
    task: OfflineExecutorTask,
) -> None:
    container_name = command[command.index("--name") + 1]
    child: subprocess.Popen[bytes] | None = None
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", os.defpath)},
        )
        if child.stdin is None:
            raise OfflineExecutorHostError("offline_executor_stdin_unavailable")
        _write_request(child.stdin, components, task)
        child.stdin.close()
        try:
            return_code = child.wait(timeout=120)
        except subprocess.TimeoutExpired as exc:
            child.kill()
            child.wait()
            raise OfflineExecutorHostError("offline_executor_timeout") from exc
        if return_code != 0:
            raise OfflineExecutorHostError("offline_executor_adapter_failed")
    except FileNotFoundError as exc:
        raise OfflineExecutorHostError("offline_executor_docker_unavailable") from exc
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        try:
            subprocess.run(
                ("docker", "rm", "--force", container_name),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.environ.get("PATH", os.defpath)},
                check=False,
            )
        except OSError:
            pass


def _write_request(
    stdin: BinaryIO,
    components: tuple[OfflineExecutorComponent, ...],
    task: OfflineExecutorTask,
) -> None:
    request = {
        "components": [
            {
                "content_base64": base64.b64encode(component.content).decode("ascii"),
                "name": component.name,
                "role": component.role,
            }
            for component in components
        ],
        "materialization_target_digest": task.materialization_target_digest,
        "policy_revision": task.policy_revision,
        "protocol": PUBLIC_EXECUTOR_PROTOCOL,
    }
    stdin.write(_canonical_json(request))
    stdin.flush()


def _validate_result(result: bytes, task: OfflineExecutorTask) -> None:
    try:
        value = _parse_unique_json(result)
        canonical = _canonical_json(value)
    except ValueError as exc:
        raise OfflineExecutorHostError("offline_executor_result_json_invalid") from exc
    if canonical != result or not isinstance(value, dict) or set(value) != {
        "result",
        "source_catalog",
    }:
        raise OfflineExecutorHostError("offline_executor_result_not_canonical")
    binding = value["result"]
    catalog = value["source_catalog"]
    if (
        not isinstance(binding, dict)
        or set(binding)
        != {"materialization_target_digest", "protocol", "revision"}
        or binding["protocol"] != PUBLIC_RESULT_PROTOCOL
        or binding["revision"] != PUBLIC_RESULT_REVISION
        or binding["materialization_target_digest"]
        != task.materialization_target_digest
        or not isinstance(catalog, dict)
        or catalog.get("protocol") != "trans-hub.canonical-source-catalog"
        or catalog.get("revision") != 2
    ):
        raise OfflineExecutorHostError("offline_executor_result_binding_invalid")
    try:
        validate_public_source_catalog(catalog)
    except ValueError as exc:
        raise OfflineExecutorHostError("offline_executor_result_catalog_invalid") from exc


__all__ = [
    "FIXED_PYTHON_IMAGE",
    "OfflineExecutorComponent",
    "OfflineExecutorHost",
    "OfflineExecutorHostError",
    "OfflineExecutorTask",
    "docker_run_command",
]
