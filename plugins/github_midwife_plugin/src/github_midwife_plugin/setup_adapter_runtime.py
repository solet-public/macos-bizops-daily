"""Bounded process, HTTP, and atomic-file primitives for setup operations."""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from .setup_adapter_contract import JsonObject, JsonValue, neutralized

_OUTPUT_LIMIT = 4096
_STRUCTURED_OUTPUT_LIMIT = 64 * 1024
_HTTP_JSON_LIMIT = 16 * 1024 * 1024
HOMEBREW_GUARD_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
    "HOMEBREW_NO_INSTALL_UPGRADE": "1",
}


class _Headers(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


class _HttpBody(Protocol):
    headers: _Headers

    def read(self, amount: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Public-safe command metadata; streams remain private to the adapter."""

    returncode: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str
    executable_missing: bool = False
    launch_error: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: int = -1
    stderr_bytes: int = -1

    def __post_init__(self) -> None:
        """Record the original stream sizes even for hermetic outcomes."""
        stdout_bytes = len(self.stdout.encode("utf-8"))
        stderr_bytes = len(self.stderr.encode("utf-8"))
        if self.stdout_bytes < 0:
            object.__setattr__(self, "stdout_bytes", stdout_bytes)
        if self.stderr_bytes < 0:
            object.__setattr__(self, "stderr_bytes", stderr_bytes)
        if self.stdout_bytes < stdout_bytes or self.stderr_bytes < stderr_bytes:
            raise ValueError("command stream byte count is smaller than captured output")
        if self.stdout_truncated != (self.stdout_bytes > stdout_bytes):
            raise ValueError("stdout truncation metadata does not match its byte count")
        if self.stderr_truncated != (self.stderr_bytes > stderr_bytes):
            raise ValueError("stderr truncation metadata does not match its byte count")

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


#: Small enough that a repair built around it still ends inside ``REPAIR_LIMIT`` with its remedy intact.  The rules the Manager's ``public_string`` holds a
#: repair to are mirrored, and enforced for every envelope text, by ``setup_adapter_contract.public_text`` (iss_67d2597e, iss_67472e3f).
_STDERR_DESCRIPTION_LIMIT = 200


def describe_outcome(outcome: CommandOutcome) -> str:
    """How a command ended, for a repair text: its exit code (or that it timed out), then its stderr, neutralized for the Manager's validator and capped."""
    state = "timed out" if outcome.timed_out else "executable missing" if outcome.executable_missing else f"exit {outcome.returncode}"
    stderr = " ".join(outcome.stderr.split())[:_STDERR_DESCRIPTION_LIMIT]
    return neutralized(f"{state}; stderr: {stderr!r}" if stderr else state)


@dataclass(frozen=True, slots=True)
class HomebrewAcquisition:
    """One reviewed Homebrew root and the only dependency closure it may add."""

    kind: Literal["formula", "cask"]
    name: str
    approved_closure: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"formula", "cask"}:
            raise ValueError("Homebrew acquisition kind is invalid")
        if not self.name or any(not item for item in (self.name, *self.approved_closure)):
            raise ValueError("Homebrew acquisition names must be non-empty")
        approved = (self.name, *self.approved_closure)
        if len(approved) != len(set(approved)):
            raise ValueError("Homebrew acquisition closure must not contain duplicates")


class Runtime(Protocol):
    """Injectable host boundary used by hermetic setup fixtures."""

    home: Path

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        output_limit: int = _OUTPUT_LIMIT,
    ) -> CommandOutcome: ...

    def http_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        payload: JsonObject | None = None,
    ) -> tuple[int, JsonValue]: ...

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None: ...


_HOMEBREW_BIN_DIRECTORIES = ("/opt/homebrew/bin", "/usr/local/bin")


def _which(runtime: Runtime, target: str) -> str | None:
    outcome = runtime.run(("/usr/bin/which", target), timeout_seconds=5)
    candidate = outcome.stdout.strip()
    if not outcome.ok or "\n" in candidate or not candidate or not Path(candidate).is_absolute():
        return None
    return candidate


def executable_fallback_directories(runtime: Runtime) -> tuple[str, ...]:
    """The fixed directories tried after PATH, in order: the Homebrew bins, then ``$HOME/.local/bin``.

    The last is where Claude Code's native installer puts ``claude``; it is based on the runtime's own home, the
    invoking user's.  ``solet_manager.host_software.claude_search_directories`` searches the same directories."""

    return (*_HOMEBREW_BIN_DIRECTORIES, str(runtime.home / ".local" / "bin"))


def resolve_executable(runtime: Runtime, name: str) -> str | None:
    """Resolve one approved executable through the platform-owned lookup tool.

    Falls back to the standard Homebrew bin directories, then the user's
    ``~/.local/bin``, when a bare lookup fails, mirroring
    bootstrap_adapter.protocol.resolve_executable -- this runtime's PATH is
    restricted the same way the pre-venv bootstrap adapter's is, so an
    executable absent from PATH would otherwise be misreported as unresolved
    even when it genuinely installed."""

    direct = _which(runtime, name)
    if direct is not None:
        return direct
    for directory in executable_fallback_directories(runtime):
        candidate = _which(runtime, f"{directory}/{name}")
        if candidate is not None:
            return candidate
    return None


class SystemRuntime:
    """Production host boundary with fixed vectors and capped I/O."""

    def __init__(self, *, home: Path | None = None) -> None:
        self.home = Path.home() if home is None else home

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        output_limit: int = _OUTPUT_LIMIT,
    ) -> CommandOutcome:
        started = time.monotonic()
        environment = self._environment(extra_env)
        try:
            completed = subprocess.run(  # noqa: S603 - every vector is registry-owned
                argv,
                cwd=cwd,
                env=environment,
                input=input_text,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return CommandOutcome(
                returncode=None,
                timed_out=True,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout="",
                stderr="",
            )
        except FileNotFoundError as exc:
            return CommandOutcome(
                returncode=None,
                timed_out=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout="",
                stderr="",
                executable_missing=True,
                launch_error=str(exc),
            )
        except OSError as exc:
            return CommandOutcome(
                returncode=None,
                timed_out=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout="",
                stderr="",
                launch_error=str(exc),
            )
        return bounded_command_outcome(
            returncode=completed.returncode,
            timed_out=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout=completed.stdout,
            stderr=completed.stderr,
            output_limit=output_limit,
        )

    def _environment(self, extra_env: dict[str, str] | None) -> dict[str, str]:
        environment = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        environment.update({name: value for name, value in os.environ.items() if name.startswith("HOMEBREW_")})
        if extra_env:
            environment.update(extra_env)
        environment.update(HOMEBREW_GUARD_ENV)
        return environment

    def http_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        payload: JsonObject | None = None,
    ) -> tuple[int, JsonValue]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                body = _read_bounded_http_body(cast(_HttpBody, response))
                value: object = json.loads(body)
                return response.status, cast(JsonValue, value)
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(mode)
            temporary.replace(path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()


def _homebrew_install_items(lines: list[str], acquisition: HomebrewAcquisition) -> list[str] | None:
    install_headers = [index for index, line in enumerate(lines) if line.startswith("Would install")]
    if len(install_headers) != 1:
        return None
    header = lines[install_headers[0]]
    expected_count = 1 + len(acquisition.approved_closure)
    package_kind = acquisition.kind
    allowed_headers = {
        f"Would install {expected_count} {package_kind}:",
        f"Would install {expected_count} {package_kind}s:",
    }
    if package_kind == "formula":
        allowed_headers.add(f"Would install {expected_count} formulae:")
    if header not in allowed_headers:
        return None
    items: list[str] = []
    for line in lines[install_headers[0] + 1 :]:
        if line.startswith("Would "):
            break
        if line and not line.startswith("==>"):
            items.append(line)
    return items


def homebrew_install_plan_error(outcome: CommandOutcome, acquisition: HomebrewAcquisition | str) -> str | None:
    """Reject a dry-run unless it exactly matches the declared package closure."""

    if not outcome.ok or outcome.stdout_truncated or outcome.stderr_truncated:
        return "Homebrew dry-run did not produce a complete successful plan."
    expected = _homebrew_acquisition(acquisition)
    approved = {expected.name, *expected.approved_closure}
    lines = [line.strip() for line in f"{outcome.stdout}\n{outcome.stderr}".splitlines()]
    if _has_prohibited_homebrew_mutation(lines, approved):
        return "Homebrew dry-run includes an unapproved package mutation."
    items = _homebrew_install_items(lines, expected)
    if items is None or len(items) != len(approved) or set(items) != approved:
        return "Homebrew dry-run install set differs from the approved package closure."
    return None


def _homebrew_acquisition(acquisition: HomebrewAcquisition | str) -> HomebrewAcquisition:
    """Normalize legacy leaf-formula callers into the reviewed acquisition shape."""

    if isinstance(acquisition, str):
        return HomebrewAcquisition(kind="formula", name=acquisition)
    return acquisition


def _has_prohibited_homebrew_mutation(lines: list[str], parents: set[str]) -> bool:
    """Reject non-install mutations, except formula-required upgrades of the approved closure's dependencies."""

    prohibited = ("Would reinstall", "Would remove", "Would unlink")
    return any(line.startswith(prohibited) for line in lines) or not _dependency_upgrades_are_reviewed(lines, parents)


_UPGRADE_DEPENDENCY_HEADER = re.compile(
    r"^Would upgrade (?P<count>[1-9][0-9]*) dependenc(?:y|ies) for (?P<parent>.+):$"
)
_UPGRADE_ITEM = re.compile(r"^[a-z0-9][a-z0-9@._+/-]*(?:\s+[0-9][A-Za-z0-9@._+/-]*)?$")


def _upgrade_block_is_reviewed(header: re.Match[str], followers: list[str]) -> bool:
    """A block's items run to the next ``Would`` heading and must match its declared count."""

    items = [item for item in itertools.takewhile(lambda line: not line.startswith("Would "), followers) if item]
    return len(items) == int(header["count"]) and all(_UPGRADE_ITEM.fullmatch(item) for item in items)


def _dependency_upgrades_are_reviewed(lines: list[str], parents: set[str]) -> bool:
    """Allow only complete ``Would upgrade N dependencies for <parent>:`` blocks of the requested closure."""

    for index, line in enumerate(lines):
        if not line.startswith("Would upgrade"):
            continue
        header = _UPGRADE_DEPENDENCY_HEADER.fullmatch(line)
        if header is None or header["parent"] not in parents or not _upgrade_block_is_reviewed(header, lines[index + 1 :]):
            return False
    return True


def bounded_command_outcome(
    *,
    returncode: int | None,
    timed_out: bool,
    duration_ms: int,
    stdout: str,
    stderr: str,
    executable_missing: bool = False,
    launch_error: str | None = None,
    output_limit: int = _OUTPUT_LIMIT,
) -> CommandOutcome:
    """Build an outcome with byte-bounded streams and explicit overflow facts."""
    capped_stdout, stdout_truncated, stdout_bytes = _cap_stream(stdout, output_limit)
    capped_stderr, stderr_truncated, stderr_bytes = _cap_stream(stderr, output_limit)
    return CommandOutcome(
        returncode=returncode,
        timed_out=timed_out,
        duration_ms=duration_ms,
        stdout=capped_stdout,
        stderr=capped_stderr,
        executable_missing=executable_missing,
        launch_error=launch_error,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
    )


def _cap_stream(stream: str, output_limit: int = _OUTPUT_LIMIT) -> tuple[str, bool, int]:
    if output_limit <= 0:
        raise ValueError("output_limit must be positive")
    encoded = stream.encode("utf-8")
    byte_count = len(encoded)
    if byte_count <= output_limit:
        return stream, False, byte_count
    return encoded[:output_limit].decode("utf-8", errors="ignore"), True, byte_count


def _read_bounded_http_body(
    response: _HttpBody,
    *,
    limit: int = _HTTP_JSON_LIMIT,
) -> bytes:
    """Read one HTTP body with byte-counted overflow and truncation checks."""

    declared_text = response.headers.get("Content-Length")
    declared: int | None = None
    if declared_text is not None:
        try:
            declared = int(declared_text)
        except ValueError as exc:
            raise ValueError(f"HTTP JSON response has invalid Content-Length {declared_text!r}") from exc
        if declared < 0:
            raise ValueError(f"HTTP JSON response has invalid Content-Length {declared}")
        if declared > limit:
            raise ValueError(f"HTTP JSON response body is too large: observed {declared} bytes from Content-Length; limit is {limit} bytes")

    body = response.read(limit + 1)
    observed = len(body)
    if observed > limit:
        raise ValueError(f"HTTP JSON response body is too large: observed at least {observed} bytes; limit is {limit} bytes")
    if declared is not None and observed != declared:
        raise ValueError(f"HTTP JSON response body is truncated: observed {observed} bytes; Content-Length declares {declared} bytes")
    return body


def read_json_object(path: Path) -> JsonObject | None:
    """Read a public JSON object, returning ``None`` on absence or invalidity."""

    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        return None
    return cast(JsonObject, raw)


__all__ = ["CommandOutcome", "Runtime", "SystemRuntime", "describe_outcome", "read_json_object"]
