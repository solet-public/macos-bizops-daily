"""Named constants for macos_self_deployment_plugin.

All color tokens, socket paths, env var names, process keys, result
type strings, and timing knobs are centralized here so the surrounding
code stays free of magic strings.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final

from ananta.core.runtime import (
    DRAINING_SENTINEL_SUFFIX as _CORE_DRAINING_SENTINEL_SUFFIX,
)

PLUGIN_NAME: Final[str] = "macos_self_deployment_plugin"

# Env var contract — mirrored from ananta.core.runtime.port_manager.
ENV_SOLET_NAME: Final[str] = "SOLET_NAME"
ENV_SOLET_COLOR: Final[str] = "SOLET_COLOR"
ENV_SOLET_INSTANCE_ID: Final[str] = "SOLET_INSTANCE_ID"
ENV_APP_HOME: Final[str] = "APP_HOME"
# Audit-only (design 2026-06-27 §4.8): set on every materialized-release
# green-spawn so a child's logs/env record which immutable release id it
# is running, keeping the color axis (router routing identity) and the
# release axis (code tree) separate, auditable axes. Nothing reads it back
# at runtime — it is purely an observability/log field.
ENV_SOLET_RELEASE_ID: Final[str] = "SOLET_RELEASE_ID"

# Color tokens accepted by the router. Mirrored here from
# ``blue_green_router/router_state.py`` so callers that only need the
# token constants don't pull in the full router subpackage. (The router
# subpackage lives inside this plugin since 2026-06-15; see
# workbench/2026-06-15_homunculus_root_layout_decisions.md for the fold.)
COLOR_BLUE: Final[str] = "blue"
COLOR_GREEN: Final[str] = "green"
_COLOR_TOKENS: Final[frozenset[str]] = frozenset({COLOR_BLUE, COLOR_GREEN})

# Process key for the durable handoff. Canonical service-interface form
# (Task #21 fix per `workbench/2026-06-07_plugin_namespace_callsite_sweep.md`
# NS.C). The plugin is a bound ServiceProvider on `local_self_deployment_service`
# per `profile/config/service_bindings.json`, so ``PluginProcessScanner.
# _should_skip_plugin()`` SKIPS its ``@platform_process`` decorators from
# the ``plugin::*`` registry at scan time. The bound surface is reachable
# only via ``service_interface::local_self_deployment_service::*``; the
# canonical ABC + ``@service_interface_process`` decorator + KB JSON for
# ``complete_swap`` were added in commit ``0e72ac15``.
COMPLETE_SWAP_PROCESS_KEY: Final[str] = (
    "service_interface::local_self_deployment_service::complete_swap"
)

# Result-type tokens for the @platform_process EDGE result-processor merge.
RESULT_TYPE_RESTART: Final[str] = "local_blue_green_restart_result"
RESULT_TYPE_COMPLETE_SWAP: Final[str] = "local_blue_green_complete_swap_result"
RESULT_TYPE_STATUS: Final[str] = "local_blue_green_swap_status_result"
RESULT_TYPE_ROLLBACK: Final[str] = "local_blue_green_swap_rollback_result"
# Durable code rollback (rollback_release) — DISTINCT from the in-window
# router re-point (RESULT_TYPE_ROLLBACK / swap_rollback). The string is
# mirrored verbatim in the local_self_deployment_service PublicAPI
# registration (services/local_self_deployment_service/interfaces/public.py).
RESULT_TYPE_ROLLBACK_RELEASE: Final[str] = "local_blue_green_rollback_release_result"
RESULT_TYPE_AUTOSTART_INSTALL: Final[str] = "macos_self_deployment_autostart_install_result"
RESULT_TYPE_AUTOSTART_UNINSTALL: Final[str] = "macos_self_deployment_autostart_uninstall_result"
RESULT_TYPE_AUTOSTART_STATUS: Final[str] = "macos_self_deployment_autostart_status_result"

# LaunchAgent autostart conventions. Label scheme is operator-neutral
# (no "com.<org>" prefix) so a solet's autostart never collides
# with an organization-owned plist.
AUTOSTART_LABEL_PREFIX: Final[str] = "local.solet"
AUTOSTART_PLIST_DIR_DEFAULT: Final[str] = "~/Library/LaunchAgents"
AUTOSTART_LOG_DIR_DEFAULT: Final[str] = "~/.ananta/logs"

# PATH written into the LaunchAgent's EnvironmentVariables (§39.2, reported and
# field-verified by a seed adopter). A launchd-spawned process inherits NO login
# shell, so with no PATH key it gets launchd's bare default
# ``/usr/bin:/bin:/usr/sbin:/sbin`` -- which excludes both Homebrew prefixes.
# The platform shells out to Homebrew-installed SYSTEM binaries (``tmux`` at
# minimum, the substrate of the swap-durable fleet host), so in-daemon
# ``shutil.which("tmux")`` returned None on a machine where tmux was correctly
# installed at ``/opt/homebrew/bin/tmux`` -- present but invisible.
#
# DETERMINISTIC BY CONSTRUCTION: a fixed literal, never the operator's live
# ``$PATH``. Capturing the interactive PATH would make the rendered plist vary
# by whoever ran the install (breaking the byte-comparison staleness check in
# ``_classify_install_prior``) and would leak the operator's local layout --
# personal toolchain dirs, checkout paths, employer-specific prefixes -- into a
# generated artifact. Both Homebrew prefixes are listed unconditionally
# (``/opt/homebrew`` Apple Silicon, ``/usr/local`` Intel) rather than
# arch-detected: a non-existent directory on PATH is inert, and one literal
# keeps the render arch-independent.
AUTOSTART_PATH_ENV: Final[str] = (
    "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:"
    "/usr/bin:/bin:/usr/sbin:/sbin"
)

# Option-B supervisor (2026-06-28). The LaunchAgent runs this module —
# NOT ``ananta.cli`` directly — so the launchd-managed process is a thin,
# colour-agnostic crash-supervisor that spawns + re-spawns the active solet
# from ``current`` and survives blue-green cutovers untouched. Because no
# solet colour is ever launchd-managed under this model, a drained/SIGTERM'd
# colour is never respawned by launchd: the ghost-respawn class is
# structurally impossible. Run as ``<current>/venv/bin/python3 -m
# macos_self_deployment_plugin.supervisor --app-home <profile>``.
AUTOSTART_SUPERVISOR_MODULE: Final[str] = "macos_self_deployment_plugin.supervisor"

# Router mgmt unix-socket filename suffix, appended to the solet name
# under the runtime dir: ``<runtime>/<name>.router.sock``. Single-sourced
# so the router bind path, the plugin's client path, and the supervisor's
# liveness-poll path cannot drift.
ROUTER_SOCKET_SUFFIX: Final[str] = ".router.sock"

# Router public-port discovery filename suffix: ``<runtime>/<name>.router.port``.
# NOTE (tracked debt, 2026-08-14 Lane X): this constant is NEW and currently has
# ONE consumer — the readiness-failure diagnosis in ``plugin.py``, which reads
# the file's PRESENCE as the fingerprint of an overlapping router restart. The
# same string is still spelled literally in router.py, uninstall_router.py,
# mcp_ingress.py and stale_runtime_cleanup.py; those were left alone because
# sweeping them crosses into files other lanes hold. Fold them in when one lane
# owns them all.
ROUTER_PORT_SUFFIX: Final[str] = ".router.port"

# Synthetic flow-id prefix for durably-enqueued complete_swap rows.
# The enqueuing actor (blue) typically holds no caller flow_id —
# apply_manifest's interface contract doesn't carry one — so the
# enqueue mints a fresh flow tag for the finisher's own continuation.
FLOW_ID_PREFIX: Final[str] = "flow-localbg-"

# Timing knobs.
# 600s ceiling derived from observed worst case: a green spawn whose
# kb_lifecycle finds an install-record gap and re-embeds a large KB
# (e.g. compositions ~4359 chunks via LM Studio under GPU contention
# with blue still serving traffic) routinely exceeds the prior 120s
# value (observed to be exceeded in practice, forcing the
# launch.py fallback). 600s gives ~5x margin over the smallest
# observed failure case while still bounding genuinely hung greens to
# 10min before SIGKILL. The underlying install-record-vanishing bug
# (task #18, 2026-06-01) is the real root cause; when fixed this
# ceiling can come back down.
DEFAULT_GREEN_READY_TIMEOUT_SECONDS: Final[int] = 600
DEFAULT_GREEN_READY_POLL_INTERVAL_SECONDS: Final[float] = 1.0
DEFAULT_ROUTER_REQUEST_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 10.0
DEFAULT_PRIOR_TERM_GRACE_SECONDS: Final[float] = 10.0
DEFAULT_PRIOR_TERM_POLL_INTERVAL_SECONDS: Final[float] = 0.2

# Post-cutover finisher escalation policy, carried on the durable
# ``PendingFinisher`` record (iss_8d1ec833, adjudication D1 property
# ``no_sigkill_reachable_from_this_channel``). An ordinary service-driven swap
# keeps today's ladder: SIGTERM, grace, then SIGKILL. A swap that entered
# through the reconciliation/``cutover_release`` channel must never reach
# signal 9, so on grace overrun its finisher stops, keeps the record and
# surfaces ``prior_sigterm_timeout_needs_intervention`` instead.
FINISHER_ESCALATION_SIGKILL_AFTER_GRACE: Final[str] = "sigkill_after_grace"
FINISHER_ESCALATION_NEEDS_INTERVENTION: Final[str] = "needs_intervention"
# The heartbeat backstop's per-tick bounded wait for the prior to exit after
# its SIGTERM. Deliberately much shorter than DEFAULT_PRIOR_TERM_GRACE_SECONDS:
# the backstop runs ON the heartbeat thread every DEFAULT_HEARTBEAT_INTERVAL
# tick and retries a still-alive prior on the next tick (SIGTERM again, never
# SIGKILL), so the per-tick wait only needs to cover a prior that honours
# SIGTERM (milliseconds) while keeping the heartbeat cadence inside the
# router's 30s liveness window even with a prior that ignores it.
DEFAULT_BACKSTOP_TERM_VERIFY_SECONDS: Final[float] = 2.0
# prepare_for_readiness bounded wait for the router socket. The router is a
# SEPARATE KeepAlive LaunchAgent that comes up independently of the solet; at a fresh
# BIRTH both agents load ~simultaneously (RunAtLoad), so the main boot can reach
# the router-socket check a beat before the router has created its socket. Poll
# this bounded window rather than failing one-shot on that benign race (the FATAL
# first-boot + LaunchAgent-restart cycle observed on fresh seed births). Still
# fails LOUDLY past the window — a genuinely absent router is a real error.
DEFAULT_ROUTER_SOCKET_WAIT_SECONDS: Final[float] = 20.0
DEFAULT_ROUTER_SOCKET_POLL_INTERVAL_SECONDS: Final[float] = 0.5

# MgmtServer.start() bounded wait for an INCUMBENT router to release the mgmt
# socket path before reclaiming it. An overlapping restart (`launchctl
# bootout` immediately followed by `bootstrap`, and `launchctl kickstart -k`,
# which is overlapping BY DESIGN) briefly runs two routers: the outgoing one
# still answers `status` while the incoming one is binding. Waiting that window
# out lets the normal restart heal; a path still answering past it is a genuine
# two-router condition and start() refuses loudly (RouterSocketBusyError)
# rather than deleting a live router's socket.
#
# COUPLING — read before tuning either number. This wait MUST stay well under
# DEFAULT_ROUTER_SOCKET_WAIT_SECONDS (20.0) above, the platform child's own
# deadline for the socket to appear. The child starts its 20s clock
# independently of the router's reclaim wait, so a reclaim wait at or near 20s
# trades a socket-stolen failure for a worse one: the router politely waiting
# for an incumbent while the child times out and the platform crash-loops. The
# two are a budget, not two independent knobs.
DEFAULT_MGMT_SOCKET_RECLAIM_WAIT_SECONDS: Final[float] = 5.0
DEFAULT_MGMT_SOCKET_RECLAIM_POLL_INTERVAL_SECONDS: Final[float] = 0.2
DEFAULT_MGMT_SOCKET_PROBE_TIMEOUT_SECONDS: Final[float] = 1.0

# GTE-06 L2 fresh-source preflight probe (design §3.3 / Q1 ruling). The
# probe imports + bare-instantiates the manifest's plugin set in a fresh
# subprocess — no solet boot, no DB, no embedder — so a healthy pass is
# seconds. Timeout ⇒ process-group SIGKILL ⇒ classified ProbeTimeout RED
# (fail-closed; blocks the swap). Operator-tunable per Q1 via the plugin
# config key below; the constant is the default.
DEFAULT_PREFLIGHT_PROBE_TIMEOUT_SECONDS: Final[float] = 120.0
CONFIG_KEY_PREFLIGHT_PROBE_TIMEOUT_SECONDS: Final[str] = (
    "preflight_probe_timeout_seconds"
)

# Option-B supervisor cadence (2026-06-28). The supervisor polls the
# router every interval and spawns a replacement only when the router has
# NO active colour (``active_instance_id is None``) — deferring liveness
# entirely to the router's authoritative ``_heartbeat_gc`` (which clears a
# dead active binding within ~heartbeat-timeout + gc-interval). So crash
# recovery is "always recovers within ~router-GC + the solet-boot", distinct
# from the zero-downtime planned cutover; the latency floor is the
# router's heartbeat timeout, a router-side tunable, NOT shortened here.
# The backoff bounds a crash-loop when the ``current`` release is broken:
# each consecutive boot that fails to reach active doubles the wait, up to
# the cap, so a bad release degrades to slow-retry instead of a spin.
DEFAULT_SUPERVISOR_POLL_INTERVAL_SECONDS: Final[float] = 5.0
DEFAULT_SUPERVISOR_SPAWN_BACKOFF_BASE_SECONDS: Final[float] = 5.0
DEFAULT_SUPERVISOR_SPAWN_BACKOFF_CAP_SECONDS: Final[float] = 120.0

# Brief grace between ``router.activate(next_color)`` and quiescing the
# prior color's background work. The router's drain window begins
# immediately on activate; this pause gives in-flight requests on the
# prior color a tick to land their response before the prior color's
# background work pauses. Short enough not to materially affect cutover
# wall-clock.
DEFAULT_POST_ACTIVATE_GRACE_SECONDS: Final[float] = 0.5

# Slice 2.5 of 2026-06-05_bridge_port_routing_and_session_lifecycle_design.md:
# strict-I2 unified transient-state budget. The TLA spec's
# ChildSelfSigtermOnFailedRegistration action (line 411) is enabled from
# BOTH "spawning" AND "bindingPort" states against the SAME RegDeadline;
# the implementation mirrors that with one deadline covering bind-wait
# and register together. 600s matches the blue-green precedent
# (DEFAULT_GREEN_READY_TIMEOUT_SECONDS, set in commit b8c20ac4 per Dusk's
# 2026-06-04 recommendation) because the bind-wait phase races against
# the full cold-start startup sequence: agent_messaging_plugin's
# start_interface may not complete for many seconds when KB lifecycle
# is purging large numbers of orphaned chunks (observed empirically
# 2026-06-06: 103K orphan chunks took >30s to purge with the original
# 30s budget, causing strict-I2 self-SIGTERM during cold start).
# Strict-I2 invariant still holds — a child that NEVER gets a port
# within 600s, or never registers within that budget, still SIGTERMs
# with a structured token. The budget is a real-world margin, not
# a relaxation of the invariant.
DEFAULT_TRANSIENT_STATE_BUDGET_SECONDS: Final[float] = 600.0
DEFAULT_BRIDGE_PORT_POLL_INTERVAL_SECONDS: Final[float] = 1.0
DEFAULT_REGISTRATION_POLL_INTERVAL_SECONDS: Final[float] = 0.5

# Structured error tokens logged immediately before the heartbeat thread
# SIGTERMs its own process when the unified transient-state budget
# expires. Operators + smoke tests grep these tokens to confirm the
# spawn-path guarantee fired rather than the child silently sitting
# idle. Two tokens distinguish which phase missed the deadline so the
# operator can diagnose without re-reading the lifecycle source.
#
# The ``LOCAL`` qualifier on the bridge-port-appeared token disambiguates
# the in-process attribute ``agent_messaging_plugin.bridge_port`` (where
# THIS solet's own bridge HTTP server bound) from the on-disk file
# ``~/.ananta/runtime/<name>.bridge.port`` (router-owned, used by MCP
# bridge subprocesses to discover the router). The Phase 1 bind-wait
# polls the in-process LOCAL port via cross-plugin lookup
# (``plugin.py::_lookup_bridge_port``); it does NOT read the file. The
# an earlier RCA conflated the two when reading this token name; the
# rename surfaces the distinction so the next agent reading
# ``FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED`` cannot
# misinterpret the failure mode as a file-side issue.
FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED: Final[str] = (
    "FAILED_REGISTRATION_BOUNDED_WINDOW_EXPIRED"
)
FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED: Final[str] = (
    "FAILED_REGISTRATION_LOCAL_BRIDGE_PORT_NEVER_APPEARED"
)

# Cross-plugin lookup target for the bridge HTTP port. The plugin owns
# its own bound port; the deployment plugin reads it via
# orchestrator_ref.plugin_manager.plugins[<name>].bridge_port — same
# pattern as `_collect_set_active_targets` uses for set_active.
AGENT_MESSAGING_PLUGIN_NAME: Final[str] = "agent_messaging_plugin"
BRIDGE_PORT_ATTRIBUTE: Final[str] = "bridge_port"
# BLG-04: same cross-plugin lookup pattern, for the streamable HTTP
# listener's own (now ephemeral, no longer hardcoded) bound port.
STREAMABLE_PORT_ATTRIBUTE: Final[str] = "streamable_bound_port"

# Single cross-color drain marker `~/.ananta/runtime/<name>.draining`. this solet's
# CORE SIGTERM handler reads it (ananta.core.runtime.is_draining) and exits 0
# on an intentional drain so launchd KeepAlive does NOT respawn the drained
# color. (The Slice-4 LaunchAgent PathState predicate this once gated was
# dropped by F2 Choice Y in favour of the in-app handler — launchd's OR-combined
# KeepAlive keys defeated PathState anyway.) The suffix now lives in CORE
# (single source: the read side must not import this plugin); re-exported here
# for backward-compatible imports.
DRAINING_SENTINEL_SUFFIX: Final[str] = _CORE_DRAINING_SENTINEL_SUFFIX

# Slice 4.5 stop_self timing. Pre-SIGTERM delay gives the verb's
# response the time it needs to flush back to the MCP caller before
# the detached watchdog signals our pid; SIGKILL escalation matches
# the dispatch's "bounded ~10s window" framing for a solet child that
# ignores SIGTERM.
DEFAULT_STOP_SELF_PRE_SIGTERM_DELAY_SECONDS: Final[float] = 0.5
DEFAULT_STOP_SELF_SIGKILL_ESCALATION_SECONDS: Final[float] = 10.0

# Result-type token for the stop_self EDGE result-processor merge.
RESULT_TYPE_STOP_SELF: Final[str] = "macos_self_deployment_stop_self_result"

# Synthetic audit token prefix returned by restart_with_manifest when
# the action factory isn't available (smoke / degenerate-config).
AUDIT_TOKEN_PREFIX: Final[str] = "local_bg_restart_"

# Status string tokens.
STATUS_QUEUED: Final[str] = "queued"
STATUS_COMPLETED: Final[str] = "completed"
STATUS_FAILED: Final[str] = "failed"
STATUS_PROBE_FAILED: Final[str] = "probe_failed"
STATUS_ROLLBACK_NOT_APPLICABLE: Final[str] = "rollback_not_applicable"
STATUS_ROLLED_BACK: Final[str] = "rolled_back"


class RestartReasonCode(StrEnum):
    """Machine-readable ``RestartResult.reason_code`` vocabulary.

    Stable tokens that classify a restart/rollback outcome (paired with the
    core ``RestartStatus``) so callers branch on a specific cause without
    parsing ``message`` prose. The vocabulary is plugin-owned; the core
    ``RestartResult.reason_code`` is a plain ``str`` to stay backend-agnostic.

    Partitioned by the ``RestartStatus`` it accompanies (design
    ``2026-06-27_true_local_blue_green_materialized_artifacts_design.md``
    §4.5 durable-rollback verb spec):

    - ``NONE`` — success paths (``queued``/``completed``/``dry_run``) carry
      no diagnostic code.
    - ``FAILED`` codes — the system is UNCHANGED + coherent + RETRYABLE.
    - ``NEEDS_INTERVENTION`` codes — automated recovery is EXHAUSTED; a
      human must act before the next automated attempt is safe.
    """

    NONE = ""

    # --- FAILED (system unchanged + coherent + retryable) -----------------
    ROUTER_UNREACHABLE = "router_unreachable"
    NOT_ACTIVE_INSTANCE = "not_active_instance"
    ROOT_MANIFEST_DRIFT = "root_manifest_drift"
    BUILD_FAILED = "build_failed"
    SCHEMA_PREFLIGHT_REFUSED = "schema_preflight_refused"
    # GTE-06: the L2 fresh-source preflight probe rejected the candidate
    # (or the probe harness itself failed — fail-closed, A5a). Pairs with
    # ``RestartStatus.PROBE_FAILED``, not ``FAILED``: core reacts by
    # rolling the committed manifest bytes back. System unchanged;
    # retryable after the source (or probe) is fixed.
    PROBE_REJECTED = "probe_rejected"
    SPAWN_FAILED = "spawn_failed"
    # The candidate process started but EXITED before registering with the
    # router. Distinct from REGISTER_TIMEOUT, which means it was still alive
    # and simply never finished coming up: a dead candidate is detected within
    # a poll interval instead of costing the swap its full ready-timeout (and
    # holding the action queue for all of it).
    SPAWN_DIED = "spawn_died"
    REGISTER_TIMEOUT = "register_timeout"
    ACTIVATE_REFUSED = "activate_refused"
    # The post-activate symlink swap failed but the F2-gated compensation
    # CONFIRMED the router rollback to the prior color (pre-swap pair
    # restored) — retryable. ``CUTOVER_*`` for a forward cutover,
    # ``ROLLBACK_*`` for the durable-rollback verb's symlink op.
    CUTOVER_COMPENSATED = "cutover_compensated"
    ROLLBACK_COMPENSATED = "rollback_compensated"
    # Durable-rollback verb only. NO_ROLLBACK_TARGET: no ``previous`` release on
    # disk to roll back to (``current``/``previous`` move atomically, so "no
    # previous" IS "no rollback target"). STALE_CURRENT_RELEASE: the caller's
    # asserted ``expected_current_release`` no longer matches the live
    # ``current`` (someone else deployed/rolled back since they observed it) —
    # the concurrency CAS surface (Architect ruling (c), 2026-06-28).
    NO_ROLLBACK_TARGET = "no_rollback_target"
    STALE_CURRENT_RELEASE = "stale_current_release"

    # --- NEEDS_INTERVENTION (automated recovery exhausted) ----------------
    # The post-activate symlink swap failed and the F2-gated router rollback
    # did NOT take (RPC error / refusal / drain expired): the candidate is
    # LEFT ALIVE because the router may still route to it, so killing it
    # would route live traffic to a dead color. A forward cutover reaches
    # this via the F2-iv retrofit.
    CUTOVER_ROUTER_ROLLBACK_FAILED = "cutover_router_rollback_failed"
    # Durable-rollback verb: the rollback TARGET (``previous``) failed to
    # register / pass its health probe within the timeout — the safety net
    # itself is void (nothing healthy to fall back to).
    ROLLBACK_TARGET_UNBOOTABLE = "rollback_target_unbootable"
    # Durable-rollback verb: the symlink swap failed AND its compensation
    # could not complete its clear/restore, so the durable ``current``/
    # ``previous`` pair MAY be incoherent.
    COMPENSATION_INCOMPLETE = "compensation_incomplete"
    # The rejected candidate (readiness failure, activate error/refusal) was
    # SIGTERM'd and unregistered but is NOT confirmed gone: it ignored SIGTERM
    # for the whole grace window (or the signal was denied). This channel never
    # escalates to SIGKILL (``no_sigkill_reachable_from_this_channel``,
    # iss_8d1ec833), so the message names pid + instance_id + start_token for
    # the operator and no second signal is ever sent.
    CANDIDATE_TERMINATE_TIMEOUT = "candidate_terminate_timeout"


def is_valid_color(token: str) -> bool:
    """Return True iff ``token`` is a recognized color token."""
    return token in _COLOR_TOKENS


def opposite_color(token: str) -> str:
    """Map ``blue`` → ``green`` and vice-versa.

    Raises ``ValueError`` for any other input so a bad color cannot
    silently flip into a wrong band.
    """
    if token == COLOR_BLUE:
        return COLOR_GREEN
    if token == COLOR_GREEN:
        return COLOR_BLUE
    msg = f"opposite_color called with non-color token {token!r}"
    raise ValueError(msg)


def resolve_project_root(app_home: Path) -> Path:
    """Walk one level up from ``<APP_HOME>`` to the repo root.

    Convention: ``<repo_root>/profile`` is ``APP_HOME``. Fall back to
    ``app_home`` itself if the parent doesn't contain a recognisable
    project marker — fast-fail surfaces the misconfiguration at smoke
    time rather than papering over it.

    Lives here rather than in :mod:`swap_orchestrator` because the probe
    runner needs the SAME derivation to tell the candidate's interpreter
    which root to classify, and the orchestrator already imports the
    runner — one definition, two callers, no drift surface.
    """
    candidate = app_home.parent
    if (candidate / "pyproject.toml").is_file() or (candidate / "ananta").is_dir():
        return candidate
    return app_home
