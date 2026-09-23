"""Salesforce CLI executor — full delegation, no token ever enters this process.

Operator-ratified 2026-07-14 (replacing the sf-CLI session-borrow client
factory — dead on current CLI releases: verified live against CLI 2.142.7
that `sf org display --json` now returns the literal string `"[REDACTED] Use
'sf org auth show-access-token' to view"` for `accessToken`, unconditionally,
even without `--verbose`). Every verb shells out to the `sf` CLI itself —
`data query/get/delete`, `sobject describe`, `org display` via the standard
`--json` envelope, and `api request rest` (beta — see the create/update/
list_sobjects note in `record_actions.py`) for the calls that need an
arbitrary JSON body. The CLI's own keychain-backed refresh token is the
durable credential and stays inside the CLI process; this plugin never reads,
holds, or forwards an access token. There is therefore no session to expire
from OUR point of view — a CLI invocation either succeeds (the CLI refreshed
whatever it needed to, transparently) or fails with a classified fault, never
a mid-flight "expired session" this process must detect and retry.

Foreign-target invariant: the org binding is verified exactly once per
process lifetime, lazily, on first use — `org display --json`'s resolved
`instanceUrl` host must equal the registered `instance_host` pin, or the
verification fails loudly. The CLI's local alias cache can hold many org
sessions; the pin guarantees this plugin only ever talks to the one the
operator registered.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from .app_config import AppConfigLoader, SalesforceOrgConfig
from .constants import (
    DEFAULT_API_VERSION,
    DEFAULT_SF_CLI_PATH,
    ERROR_AUTH_FAILED,
    ERROR_NOT_CONFIGURED,
    ERROR_PROVISION_CONSENT_REQUIRED,
    SF_CLI_TIMEOUT_SECONDS,
)
from .errors import SalesforceCliCallError, SalesforceServiceError

_HOMEBREW_GUARD_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
    "HOMEBREW_NO_INSTALL_UPGRADE": "1",
}


class SalesforceCliExecutor:
    """Runs `sf` CLI verbs against the address-book-bound org; parses JSON results.

    Lazily verifies the org binding (host pin) on first use and caches it for
    the process lifetime — there is nothing to rebuild afterward, since the
    CLI manages its own credential refresh internally.
    """

    def __init__(
        self,
        app_config_loader: AppConfigLoader,
        *,
        api_version: str = DEFAULT_API_VERSION,
        sf_cli_path: str = DEFAULT_SF_CLI_PATH,
    ) -> None:
        self._loader = app_config_loader
        self._api_version = api_version
        self._sf_cli_path = sf_cli_path
        self._target_org: str | None = None
        self._username: str = ""

    @property
    def api_version(self) -> str:
        """The pinned API version this executor targets."""
        return self._api_version

    @property
    def sf_cli_path(self) -> str:
        """The sf CLI binary path this executor invokes."""
        return self._sf_cli_path

    @property
    def username(self) -> str:
        """Username of the verified org binding ('' before first call)."""
        return self._username

    def probe_cli(self) -> dict[str, object]:
        """Return a local, credential-free ``sf --version`` capability verdict."""
        configured_path = self._sf_cli_path
        executable_path = configured_path if os.path.isabs(configured_path) and os.path.isfile(configured_path) and os.access(configured_path, os.X_OK) else shutil.which(configured_path)
        if executable_path is None:
            return {
                "executable_path": "",
                "version": "",
                "executable": False,
                "configured": False,
            }
        try:
            completed = subprocess.run(
                [executable_path, "--version"],
                capture_output=True,
                text=True,
                timeout=SF_CLI_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {
                "executable_path": executable_path,
                "version": "",
                "executable": False,
                "configured": False,
            }
        version = completed.stdout.strip().splitlines()[0] if completed.returncode == 0 and completed.stdout.strip() else ""
        return {
            "executable_path": executable_path,
            "version": version,
            "executable": bool(version),
            "configured": (bool(version) and os.path.isabs(configured_path) and os.path.realpath(configured_path) == os.path.realpath(executable_path)),
        }

    def provision_cli(self, *, acknowledge_system_change: bool) -> dict[str, object]:
        """Install the reviewed ``sf`` formula only when the local probe is unsatisfied.

        This method owns no configuration persistence: its caller binds the verified
        absolute executable through the plugin's ordinary config-update path.
        """

        observed = self.probe_cli()
        if _cli_observation_has_executable(observed):
            return {
                "provisioned": False,
                "reason": "already_present",
                "executable_path": observed["executable_path"],
                "version": observed["version"],
            }
        if not acknowledge_system_change:
            raise SalesforceServiceError(
                ERROR_PROVISION_CONSENT_REQUIRED,
                "Explicit acknowledgement is required before Homebrew may install sf and its declared Node closure.",
            )
        brew = _homebrew_executable()
        _require_sf_install_plan(brew)
        _install_sf_formula(brew)
        self._sf_cli_path = "sf"
        verified = self.probe_cli()
        if not _cli_observation_has_executable(verified):
            raise SalesforceServiceError(
                ERROR_NOT_CONFIGURED,
                "The installed sf executable did not pass its required version probe.",
            )
        return {
            "provisioned": True,
            "reason": "installed",
            "executable_path": verified["executable_path"],
            "version": verified["version"],
        }

    def run_json(
        self,
        argv_tail: list[str],
        *,
        env_overrides: Mapping[str, str] | None = None,
    ) -> Any:
        """Run `sf <argv_tail...> --target-org <org> --json`; return the `result` payload."""
        target_org = self._ensure_verified()
        argv = [self._sf_cli_path, *argv_tail, "--target-org", target_org, "--json"]
        completed = self._invoke(argv, env_overrides, target_org)
        return _parse_envelope(completed, target_org)

    def run_rest(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        raw_response: bool = False,
    ) -> Any:
        """Run `sf api request rest <path> --method <method> [--body @file]`.

        Used only where the stable `data create/update record --values`
        mini-language is unsafe for arbitrary field content (see
        `record_actions.py`). No `--json` support on this CLI command; the
        raw REST response/error body is parsed directly.

        ``raw_response=True`` skips JSON-decoding a successful response and
        returns the CLI's raw stdout text instead — Bulk v2's
        successfulResults/failedResults/unprocessedrecords endpoints return
        CSV bodies, not JSON (``bulk_actions.bulk_job_results``). Error
        responses are still parsed as the usual REST error array either way.
        """
        target_org = self._ensure_verified()
        with _json_body_file(body) as body_path:
            argv = [
                self._sf_cli_path,
                "api",
                "request",
                "rest",
                path,
                "--method",
                method,
                "--target-org",
                target_org,
            ]
            if body_path is not None:
                argv += ["--body", f"@{body_path}"]
            completed = self._invoke(argv, None, target_org)
        if raw_response:
            return _parse_rest_response_raw(completed, target_org)
        return _parse_rest_response(completed, target_org)

    def _ensure_verified(self) -> str:
        if self._target_org is None:
            config = self._loader.load()
            self._verify_org_binding(config)
            self._target_org = config.target_org
        return self._target_org

    def _verify_org_binding(self, config: SalesforceOrgConfig) -> None:
        argv = [self._sf_cli_path, "org", "display", "--target-org", config.target_org, "--json"]
        completed = self._invoke(argv, None, config.target_org)
        login_hint = f"run: sf org login web --alias {config.target_org}"
        try:
            result = _parse_envelope(completed, config.target_org)
        except (SalesforceServiceError, SalesforceCliCallError) as exc:
            raise SalesforceServiceError(
                ERROR_AUTH_FAILED,
                f"no live sf CLI session for org {config.target_org!r} — {login_hint}",
            ) from exc
        instance_url, username = _extract_org_display_fields(result, config.target_org, login_hint)
        _require_pinned_host(instance_url, config.instance_host)
        self._username = username

    def _invoke(
        self,
        argv: list[str],
        env_overrides: Mapping[str, str] | None,
        target_org: str,
    ) -> subprocess.CompletedProcess[str]:
        """Fixed argv, no shell — the only interpolated values are operator config
        (binary path, org alias, API path) and query/body content passed via
        argv elements or tempfiles, never shell-joined text."""
        env = {**os.environ, **env_overrides} if env_overrides else None
        try:
            return subprocess.run(  # noqa: S603 — fixed argv, operator-config binary
                argv,
                capture_output=True,
                text=True,
                timeout=SF_CLI_TIMEOUT_SECONDS,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise SalesforceServiceError(
                ERROR_NOT_CONFIGURED,
                (f"sf CLI not found at {self._sf_cli_path!r}. Install the standalone bundle and pin its absolute path via the plugin config key 'sf_cli_path'."),
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SalesforceServiceError(
                ERROR_AUTH_FAILED,
                (f"sf CLI did not answer within {SF_CLI_TIMEOUT_SECONDS:.0f}s for org {target_org!r}."),
            ) from exc


def _cli_observation_has_executable(observed: dict[str, object]) -> bool:
    """Accept a probe result only when its resolved executable and version are sound."""

    executable_path = observed.get("executable_path")
    version = observed.get("version")
    return observed.get("executable") is True and isinstance(executable_path, str) and os.path.isabs(executable_path) and isinstance(version, str) and bool(version)


def _homebrew_executable() -> str:
    """Resolve the only supported package manager before any sf mutation."""

    brew = shutil.which("brew")
    if brew is None or not os.path.isabs(brew):
        raise SalesforceServiceError(
            ERROR_NOT_CONFIGURED,
            "Homebrew must resolve to an absolute executable before sf can be provisioned.",
        )
    return brew


def _require_sf_install_plan(brew: str) -> None:
    """Require Homebrew's dry-run to match the reviewed sf and Node closure."""

    dry_run = _run_sf_homebrew(
        brew,
        ("install", "--dry-run", "sf"),
        "Homebrew could not produce the reviewed sf installation plan.",
    )
    if dry_run.returncode != 0 or not _sf_install_plan_is_exact(f"{dry_run.stdout}\n{dry_run.stderr}"):
        raise SalesforceServiceError(
            ERROR_NOT_CONFIGURED,
            "Homebrew proposed a mutation outside the approved sf and Node package closure.",
        )


def _install_sf_formula(brew: str) -> None:
    """Install exactly the package set admitted by the preceding dry-run."""

    installed = _run_sf_homebrew(
        brew,
        ("install", "sf"),
        "Homebrew could not complete the approved sf installation.",
    )
    if installed.returncode != 0:
        raise SalesforceServiceError(
            ERROR_NOT_CONFIGURED,
            "Homebrew did not complete the approved sf installation.",
        )


def _run_sf_homebrew(brew: str, args: tuple[str, ...], failure_message: str) -> subprocess.CompletedProcess[str]:
    """Run one guarded sf Homebrew vector and normalize launch failures."""

    environment = {**os.environ, **_HOMEBREW_GUARD_ENV}
    try:
        return subprocess.run(
            [brew, *args],
            capture_output=True,
            text=True,
            timeout=SF_CLI_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SalesforceServiceError(ERROR_NOT_CONFIGURED, failure_message) from exc


def _sf_install_plan_is_exact(output: str) -> bool:
    """Accept exactly the reviewed ``sf`` formula and its declared Node closure."""

    lines = [line.strip() for line in output.splitlines()]
    if _sf_plan_has_prohibited_mutation(lines):
        return False
    return _sf_install_items(lines) == {"sf", "node"}


def _sf_plan_has_prohibited_mutation(lines: list[str]) -> bool:
    """Reject a dry-run that contains any unreviewed package mutation kind."""

    prohibited = ("Would upgrade", "Would reinstall", "Would remove", "Would unlink")
    return any(line.startswith(prohibited) for line in lines)


def _sf_install_items(lines: list[str]) -> set[str] | None:
    """Extract the one declared Salesforce formula closure from a dry-run."""

    headers = [index for index, line in enumerate(lines) if line.startswith("Would install")]
    if len(headers) != 1 or lines[headers[0]] != "Would install 2 formulae:":
        return None
    items: list[str] = []
    for line in lines[headers[0] + 1 :]:
        if line.startswith("Would "):
            break
        if line and not line.startswith("==>"):
            items.append(line)
    item_set = set(items)
    return item_set if len(items) == len(item_set) == 2 else None


def _parse_envelope(completed: subprocess.CompletedProcess[str], target_org: str) -> Any:
    """Parse the standard `--json` envelope (`data`/`sobject`/`org` commands)."""
    if completed.returncode == 0:
        payload = _decode_json(completed.stdout)
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        raise SalesforceCliCallError("", "sf CLI returned an unexpected --json envelope shape")
    payload = _decode_json(completed.stdout)
    if not isinstance(payload, dict):
        raise SalesforceServiceError(
            ERROR_AUTH_FAILED,
            f"sf CLI command failed for org {target_org!r} with no parseable output.",
        )
    raise SalesforceCliCallError(*_error_fields_from_envelope(payload))


def _parse_rest_response(completed: subprocess.CompletedProcess[str], target_org: str) -> Any:
    """Parse `api request rest` output — no `--json` envelope; raw body or raw error array."""
    if completed.returncode == 0:
        text = completed.stdout.strip()
        return _decode_json(text) if text else None
    raise _rest_error(completed, target_org)


def _parse_rest_response_raw(completed: subprocess.CompletedProcess[str], target_org: str) -> str:
    """Parse `api request rest` output without JSON-decoding a successful body (CSV bodies)."""
    if completed.returncode == 0:
        return completed.stdout
    raise _rest_error(completed, target_org)


def _rest_error(completed: subprocess.CompletedProcess[str], target_org: str) -> Exception:
    payload = _decode_json(completed.stdout)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        first = payload[0]
        code = first.get("errorCode")
        message = first.get("message")
        return SalesforceCliCallError(
            code if isinstance(code, str) else "",
            message if isinstance(message, str) else "",
        )
    return SalesforceServiceError(
        ERROR_AUTH_FAILED,
        f"sf CLI command failed for org {target_org!r} with no parseable output.",
    )


def _error_fields_from_envelope(payload: dict[str, Any]) -> tuple[str, str]:
    data = payload.get("data")
    if isinstance(data, dict):
        code = data.get("errorCode")
        message = data.get("message")
        if isinstance(code, str) and code:
            return code, message if isinstance(message, str) else ""
    name = payload.get("name")
    message = payload.get("message")
    return (name if isinstance(name, str) else ""), (message if isinstance(message, str) else "")


def _decode_json(text: str) -> Any:
    try:
        return json.loads(text or "")
    except json.JSONDecodeError:
        return None


def _extract_org_display_fields(result: Any, target_org: str, login_hint: str) -> tuple[str, str]:
    instance_url = result.get("instanceUrl") if isinstance(result, dict) else None
    username = result.get("username") if isinstance(result, dict) else None
    if not isinstance(instance_url, str) or not instance_url or not isinstance(username, str) or not username:
        raise SalesforceServiceError(
            ERROR_AUTH_FAILED,
            f"sf CLI session for org {target_org!r} is incomplete — {login_hint}",
        )
    return instance_url, username


def _require_pinned_host(instance_url: str, pinned_host: str) -> None:
    # The invariant guard: never talk to an org other than the registered one,
    # no matter what the local CLI alias cache resolves to.
    host = (urlparse(instance_url).hostname or "").lower()
    expected = pinned_host.strip().lower()
    if host != expected:
        raise SalesforceServiceError(
            ERROR_NOT_CONFIGURED,
            (f"sf CLI org resolves to instance host {host!r}, which does not match the registered instance_host {expected!r} — refusing to connect. Fix the CLI alias or the salesforce_org entry."),
        )


@contextmanager
def _json_body_file(body: dict[str, Any] | None) -> Generator[str | None]:
    """Write `body` to a tempfile as JSON for the call, then clean it up.

    File-based (never a shell-joined argv string) so arbitrary field values —
    quotes, braces, unicode — round-trip exactly via `json.dump`/`json.load`,
    with no custom quoting grammar to get wrong.
    """
    if body is None:
        yield None
        return
    fd, path = tempfile.mkstemp(suffix=".json", prefix="salesforce_plugin_body_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle)
        yield path
    finally:
        os.unlink(path)
