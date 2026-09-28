"""Host free-space headroom check + loud operator alert.

Kickoff: ``workbench/2026-09-26_apple_native_migration_project/``
``kickoff_host_disk_guard_20260927.md``. Problem: ``iss_a5adbc1c`` — this host
has run its disk down before (55 stopped Tart debug snapshots, ``~/.tart/vms``
at 1.9T apparent, VM lanes cloning guests with only a brief-level ``df``
check, nothing on the platform warning or refusing before the disk fills).

Two verbs live here:

* :func:`check_host_disk_headroom` — the check itself. Measures free bytes at
  a path (default the ``/Users`` volume, where ``~/.tart`` and this repo both
  live) and, on a severity TRANSITION (``ok``→``warning``→``critical``, or a
  recovery back down — never once per tick), dispatches a peer message to a
  durable role via ``plugin::agent_messaging_plugin::peer_send_by_name``.
  ``agent_messaging_plugin`` is deliberately NOT a bound service (see
  ``AgentMessagingServiceInterface``'s own module docstring — binding it
  would hide its ``plugin::*::*`` EDGE processes from the process registry),
  so it is resolved the same way ``agent_messaging_session_source_plugin``'s
  own ``_agent_messaging_service()`` does
  (``plugins/agent_messaging_session_source_plugin/src/``
  ``agent_messaging_session_source_plugin/plugin.py:337-356``):
  ``orchestrator.plugin_manager.get_plugin("agent_messaging_plugin")``, fail
  fast if unavailable, then call the resolved instance's own
  ``@platform_process``-decorated method directly with
  ``method(params=..., state=...)`` — the SAME calling convention
  ``ActionProcessor._execute_plugin_method`` uses for every
  ``plugin::*::*`` verb (``ananta/src/ananta/core/actions/``
  ``action_processor.py:1122``), the platform's own canonical
  plugin-to-plugin call shape. This is NOT :func:`platform_health_plugin.`
  ``sweep.dispatch_one`` — that helper is the registry sweep's own
  diagnostic primitive (sentinel-arg dry-run/live classification), off
  limits for a production alert path; see that module's docstring. This is
  deliberately not a new delivery channel either: a log line is not "loud"
  by the kickoff's own definition, and a second delivery mechanism for the
  same kind of fact would be worse than none (two sources that can disagree
  teach the reader to trust neither — see
  ``reference_names_route_content_binds`` on the sibling trap of an
  unverified sender). The ``state`` the calling verb received is passed
  through unchanged, never fabricated: ``peer_send_by_name``'s sender ladder
  (``_resolve_role_send_sender``) stamps the caller's role or attribution
  when a manual ``process_call`` carried one, and a cron fire, which carries
  none, falls to the system scheduler sentinel by the ladder's own rule.

  **``peer_send_by_name``'s own result is inspected, not trusted blind.** A
  role with no live holder does not raise — ``dispatch_role_send`` persists
  the message either way (``peer_dispatch.py``'s own documented contract) and
  reports ``delivery="queued_for_replay"``, which reads exactly like success
  unless the caller checks the field. This module treats only
  ``queued_wake`` / ``queued_notification`` / ``queued_watcher`` (a live
  binding was actually reached) as a delivered alert; anything else —
  ``queued_for_replay``, ``peer_role_vacant``, ``peer_role_malformed``, a
  transport error — comes back as ``alerted=False`` with a reason, and is
  logged at ERROR. The severity-transition marker (see below) is
  deliberately NOT advanced on an undelivered alert, so the next tick retries
  it rather than silently treating a message nobody live received as "told."

  **Second, operator-facing surface:** this platform declares exactly one
  outward-facing (reaches a human directly, independent of any particular
  Claude/Codex session being alive) messaging provider in
  ``platform_health_plugin.constants.OUTWARD_FACING_PLUGIN_NAMESPACES`` —
  ``signal_plugin`` (``plugin::signal_plugin::post_message``). It is not
  dispatched to here: its manifest marks it ``optional: true``, it needs a
  vault-held phone number and a running ``signal-cli-rest-api`` backend, and
  nothing in this checkout (no ``profile/config`` entry, no vault secret
  found) shows it configured in this deployment. Wiring a live send to an
  unverified external channel that reaches a real phone is a different risk
  class from a peer-messaging role alert (which stays inside the fleet, to a
  role a human is already driving) — so this pass names it rather than
  calling it blind. If Signal is configured for a given deployment, wiring
  it in is a follow-up, not a silent gap: the configured role alert (below)
  remains the working operator-facing path today.

  **No baked-in identity default.** ``alert_role`` names a durable
  peer-messaging role and carries NO default here — a role name that exists
  in one solet's role-binding table is a guess in another's, and shipping a
  guessed identity as this plugin's default would be wrong for every
  install that doesn't happen to use that exact name. The target is a
  deployment-local setting (this plugin's own ``config:`` block, e.g.
  ``profile/config/plugins/platform_health_plugin.json`` on a checkout that
  sets one — never a value this shipped module hardcodes). When it is
  unset, :func:`check_host_disk_headroom` still measures and classifies
  correctly; it simply cannot alert anyone, returns ``alerted=False`` with
  ``alert_reason="alert_target_unconfigured"``, and logs at ERROR — and
  :func:`ensure_host_disk_guard_schedule` refuses to install a cron that
  would only ever produce that same unconfigured result on every tick.

  **Rate-limited by severity transition, not by tick.** A durable marker
  (`StateManagementInterface.get_key_value`/`set_key_value` — the platform's
  existing generic small-state primitive; no new table) records the last
  status an alert was successfully delivered for, keyed by the checked path.
  A tick alerts only when the freshly measured status differs from that
  marker — covering every named case (entering warning, escalating to
  critical, and recovering back to ok/warning) with the SAME comparison, not
  three separate rules. The marker lives in Postgres via the same
  ``state_service`` every other plugin's durable state goes through, so it
  survives a cron-triggered call's own process boundary (each tick is a
  fresh, stateless dispatch — nothing here may live in a module global).
* :func:`ensure_host_disk_guard_schedule` — idempotently wires the check onto
  the platform's own cron via the ``scheduling_service`` SERVICE (never the
  provider plugin directly, never a Claude Code loop — this platform's own
  standing guidance: a scheduled platform check belongs on the platform's
  scheduler, not a coding-agent session's own loop tool). Takes a
  ``scheduling_service`` object (``ananta.services.scheduling_service.``
  ``SchedulingService``, resolved via ``orchestrator.get_service(``
  ``"scheduling_service")`` at CALL time by the plugin's ensure verb, which
  raises if it is absent — it does not exist yet during plugin readiness,
  see ``plugin.py``'s module docstring) and calls its ``create_cron_schedule``/``get_schedules_by_tag`` methods
  directly — the service binding, not
  ``plugin::default_scheduling_plugin::*`` reached through
  ``plugin_manager``. **The flow_id rule, proven, not assumed:**
  ``action_factory._enforce_flow_id`` (``ananta/src/ananta/core/actions/``
  ``action_factory.py:992-1021``) refuses ANY action_definition without a
  ``flow_id`` — even on the EDGE_SINK path this cron uses — and it is
  enforced at FIRE time, not at registration time, so a schedule created
  with no ``flow_id`` would install cleanly and then fail every single fire
  forever (a strictly worse failure than not installing it: the operator
  sees a schedule that looks healthy while doing nothing). The default
  scheduling plugin's own ``create_cron_schedule`` handler stores
  ``state["flow_id"]``/``state["session_id"]`` on the schedule row itself
  (``default_scheduling_plugin/plugin.py:926-927``); at fire time
  ``ActionExecutor._build_action_definition`` copies that stored
  ``schedule.flow_id`` onto EACH per-action definition
  (``default_scheduling_plugin/src/default_scheduling_plugin/execution/``
  ``action_executor.py:180-181``) before submission — so the fix is passing
  a non-empty, system-owned ``state={"flow_id": ..., "session_id": ...}``
  at ``create_cron_schedule`` call time (mirroring
  ``actr_memory_plugin.setup_schedules``'s own per-cron system-owned
  constants,
  ``plugins/actr_memory_plugin/src/actr_memory_plugin/plugin.py:68-73``),
  not embedding a ``flow_id`` inside ``_schedule_action_definitions()``
  itself (which stays a plain ``{process_key, arguments}`` list — the
  schedule-level flow_id is what gets stamped onto it at fire time). The
  test suite proves both directions with the real ``ActionFactory`` class,
  not a fake: an action_definition built the way this schedule's fire path
  builds it (with a stamped ``flow_id``) passes
  ``ActionFactory()._enforce_flow_id`` cleanly; the same definition with the
  ``flow_id`` stripped raises exactly the error this module exists to avoid
  ever reaching production silently. **The cron targets the dedicated
  EDGE_SINK sibling** ``check_host_disk_headroom_cron`` (``is_discoverable=
  False``, no result/error processor customizations), not the discoverable
  EDGE ``check_host_disk_headroom`` — the canonical shape 1 of
  ``21_scheduling_service/01_template_flow_record_lifecycle.md``. Omitting
  ``result_processor_kind`` is only half that shape: an EDGE target that
  declares error customizations gets the inference ``process_error``
  processor attached at submission, and that scaffold cannot run on a
  system-owned flow with no flow record. The target is written as a LITERAL
  ``actions=[{...}]`` so the whole-tree gate's cron-target check (C5.1)
  can resolve and exempt it. Setting ``result_processor_kind`` to
  ``inference`` is the defect that left the predecessor KB-retrieval-audit
  cron firing daily for two months while executing its audit zero times
  (see ``ensure_kb_retrieval_audit_schedule``'s own docstring in
  ``default_scheduling_plugin/plugin.py``).

Fails loudly, not silently, when it cannot measure: :func:`measure_free_bytes`
re-raises whatever ``disk_usage`` raises rather than catching it. A check that
cannot see the disk and reports "ok" anyway is worse than no check.

The alert text carries only a fresh measurement and the floor it was compared
against — never a hardcoded remediation clause. A stale "do X before it's too
late" clause outlives the condition that prompted it and misleads a reader
long after the fix landed (measured elsewhere in this fleet against
``gauge_stale_notice`` — see
``reference_an_alerts_remediation_advice_outlives_the_defect_it_was_written_for``).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from ananta.llm.agent_messaging.state_results import require_completed

logger = logging.getLogger(__name__)

GIB: Final[int] = 1024**3

DEFAULT_CHECK_PATH: Final[str] = "/Users"
DEFAULT_WARNING_FLOOR_BYTES: Final[int] = 100 * GIB
DEFAULT_CRITICAL_FLOOR_BYTES: Final[int] = 50 * GIB
# Deliberately NO default identity here -- see the module docstring's "No
# baked-in identity default" section. A caller that wants a default reads
# one from this plugin's OWN config (plugin.yaml's `alert_role` key), never
# from a constant in shipped source.

STATUS_OK: Final[str] = "ok"
STATUS_WARNING: Final[str] = "warning"
STATUS_CRITICAL: Final[str] = "critical"

# check_host_disk_headroom's alert_reason when alert_role is unset.
REASON_ALERT_TARGET_UNCONFIGURED: Final[str] = "alert_target_unconfigured"

CHECK_PROCESS_NAME: Final[str] = "check_host_disk_headroom"
ENSURE_SCHEDULE_PROCESS_NAME: Final[str] = "ensure_host_disk_guard_schedule"
CHECK_PROCESS_KEY: Final[str] = f"plugin::platform_health_plugin::{CHECK_PROCESS_NAME}"
# The cron's EDGE_SINK target. ensure_host_disk_guard_schedule spells this
# key as a literal (see there); the smoke pins the literal to this constant.
CHECK_CRON_PROCESS_KEY: Final[str] = "plugin::platform_health_plugin::check_host_disk_headroom_cron"

DEFAULT_SCHEDULE_CRON: Final[str] = "*/10 * * * *"  # every 10 minutes
DEFAULT_SCHEDULE_TAG: Final[str] = "host_disk_guard"
DEFAULT_SCHEDULE_LABEL: Final[str] = "Host disk headroom guard"

# System-owned flow_id/session_id for the cron's own schedule row -- NOT the
# caller's identity (there is no caller; this is a scheduler-fired system
# check). action_factory._enforce_flow_id refuses an absent flow_id even on
# an EDGE_SINK path (see the module docstring); the actr_memory_plugin
# precedent for a per-cron system-owned constant pair
# (plugin.py:68-73) is mirrored here rather than reusing another cron's id,
# so a fire-time audit trail can distinguish this schedule from any other.
_HOST_DISK_GUARD_FLOW_ID: Final[str] = "flow-host-disk-guard"
_HOST_DISK_GUARD_SESSION_ID: Final[str] = "sess-host-disk-guard"

# The durable severity-transition marker: one row per checked path, holding
# the last status an alert was SUCCESSFULLY delivered for (not merely
# observed). Namespaced under this plugin's own name -- the key_value_store
# is a shared table across every plugin, so an unqualified key would risk
# collision.
STATE_NAMESPACE: Final[str] = "platform_health_plugin"
_LAST_NOTIFIED_KEY_PREFIX: Final[str] = "host_disk_guard:last_notified_status"

# peer_send_by_name's own documented delivery outcomes (peer_dispatch.py):
# these three mean a LIVE binding was actually reached. queued_for_replay
# means the message was durably persisted but the holder was unreachable --
# success at the transport layer, NOT a live delivery, and reads exactly like
# one unless this field is checked.
_LIVE_DELIVERY_KINDS: Final[frozenset[str]] = frozenset({
    "queued_wake", "queued_notification", "queued_watcher",
})


class _DiskUsage(Protocol):
    @property
    def free(self) -> int: ...


DiskUsageReader = Callable[[str], _DiskUsage]


@dataclass(frozen=True)
class HostDiskGuardConfig:
    """Thresholds are config with the kickoff's stated defaults, not a
    heuristic: WARNING < 100 GiB free, CRITICAL < 50 GiB free.

    ``alert_role`` has NO default (``None`` means unconfigured) -- see the
    module docstring's "No baked-in identity default" section. A caller
    that wants a deployment default resolves one from this plugin's own
    config before constructing this object; nothing here guesses one.
    """

    path: str = DEFAULT_CHECK_PATH
    warning_floor_bytes: int = DEFAULT_WARNING_FLOOR_BYTES
    critical_floor_bytes: int = DEFAULT_CRITICAL_FLOOR_BYTES
    alert_role: str | None = None

    def __post_init__(self) -> None:
        if self.critical_floor_bytes > self.warning_floor_bytes:
            raise ValueError(
                f"critical_floor_bytes ({self.critical_floor_bytes}) must not "
                f"exceed warning_floor_bytes ({self.warning_floor_bytes})"
            )


def measure_free_bytes(path: str, *, disk_usage: DiskUsageReader = shutil.disk_usage) -> int:
    """The host's free-byte count at ``path``.

    Raises whatever ``disk_usage`` raises, unchanged — a check that cannot
    measure must fail loudly (the kickoff's own requirement), not swallow the
    error and report a false "ok".
    """
    return disk_usage(path).free


def classify_headroom(free_bytes: int, *, warning_floor_bytes: int, critical_floor_bytes: int) -> str:
    """Pure classifier: ``critical`` < ``critical_floor_bytes`` <=
    ``warning`` < ``warning_floor_bytes`` <= ``ok``."""
    if free_bytes < critical_floor_bytes:
        return STATUS_CRITICAL
    if free_bytes < warning_floor_bytes:
        return STATUS_WARNING
    return STATUS_OK


def _gib(n: int) -> float:
    return n / GIB


def build_alert_text(*, status: str, free_bytes: int, path: str, config: HostDiskGuardConfig) -> str:
    floor = config.critical_floor_bytes if status == STATUS_CRITICAL else config.warning_floor_bytes
    return (
        f"HOST DISK {status.upper()}: {_gib(free_bytes):.1f} GiB free under {path} "
        f"(floor {_gib(floor):.1f} GiB, measured just now). This is a live "
        "measurement, not a cached one — re-check before acting rather than "
        "trusting this number's age."
    )


def _state_key(path: str) -> str:
    return f"{_LAST_NOTIFIED_KEY_PREFIX}::{path}"


def _get_state_service(orchestrator: object) -> object:
    get_service = getattr(orchestrator, "get_service", None)
    if get_service is None:
        raise RuntimeError("orchestrator has no get_service; cannot reach state_service")
    service = get_service("state_service")
    if service is None:
        raise RuntimeError("state_service is not bound on this solet")
    return service


def _read_last_notified_status(orchestrator: object, path: str) -> str:
    """The status an alert was last SUCCESSFULLY delivered for, or ``ok`` if
    none is recorded — a fresh marker is treated as a nominal baseline, so
    the first-ever tick alerts only if it observes something other than ok."""
    state = _get_state_service(orchestrator)
    data = require_completed(
        state.get_key_value(STATE_NAMESPACE, _state_key(path)),  # type: ignore[attr-defined]
        "host_disk_guard get last-notified status",
    )
    if not data.get("found"):
        return STATUS_OK
    value = data.get("value")
    if value in (STATUS_OK, STATUS_WARNING, STATUS_CRITICAL):
        return str(value)
    return STATUS_OK


def _write_last_notified_status(orchestrator: object, path: str, status: str) -> None:
    state = _get_state_service(orchestrator)
    require_completed(
        state.set_key_value(STATE_NAMESPACE, _state_key(path), status),  # type: ignore[attr-defined]
        "host_disk_guard set last-notified status",
    )


def _resolve_agent_messaging_plugin(orchestrator: object) -> object:
    """The ``agent_messaging_plugin`` instance, via structural typing.

    ``agent_messaging_plugin`` is deliberately NOT a bound service (see
    ``AgentMessagingServiceInterface``'s own module docstring — binding it
    would hide its ``plugin::*::*`` EDGE processes from the process
    registry), so it is consumed through
    ``orchestrator.plugin_manager.get_plugin(...)``, the same pattern
    ``agent_messaging_session_source_plugin``'s own
    ``_agent_messaging_service()`` uses
    (``plugins/agent_messaging_session_source_plugin/src/``
    ``agent_messaging_session_source_plugin/plugin.py:337-356``). Fails
    fast if the orchestrator or the plugin is unavailable — never a silent
    ``None`` swallowed into "not alerted".
    """
    plugin_manager = getattr(orchestrator, "plugin_manager", None)
    if plugin_manager is None:
        raise RuntimeError(
            "host_disk_guard: orchestrator has no plugin_manager; cannot reach "
            "agent_messaging_plugin to dispatch an alert",
        )
    plugin = plugin_manager.get_plugin("agent_messaging_plugin")
    if plugin is None:
        raise RuntimeError(
            "host_disk_guard: agent_messaging_plugin not loaded; cannot dispatch an alert",
        )
    return plugin


def _send_peer_alert(orchestrator: object, *, name: str, content: str, state: dict[str, Any]) -> object:
    """Dispatch ``peer_send_by_name`` directly on the resolved plugin
    instance, using ``method(params=..., state=...)`` — the public-method
    consumption the provider documents for itself
    (``agent_messaging_plugin/plugin.py`` module docstring) and the calling
    convention ``ActionProcessor._execute_plugin_method`` uses for every
    ``plugin::*::*`` verb. Deliberately NOT
    :func:`platform_health_plugin.sweep.dispatch_one` — that helper is the
    registry sweep's own diagnostic primitive, off limits for a production
    path (see that module's docstring). ``state`` is the one the calling
    verb received, passed through unchanged: the sender ladder reads the
    caller identity the action pipeline lifted into it.
    """
    plugin = _resolve_agent_messaging_plugin(orchestrator)
    method = plugin.peer_send_by_name  # type: ignore[attr-defined]
    return method(params={"name": name, "content": content}, state=state)


def _delivery_outcome(result: object) -> tuple[bool, str | None]:
    """Whether a ``peer_send_by_name`` dispatch actually reached a LIVE
    binding, and if not, why. Never trusts ``action_status == completed``
    alone: a completed dispatch can still carry
    ``delivery == "queued_for_replay"``, which means persisted-but-nobody-
    live-received-it, not delivered."""
    if not isinstance(result, dict):
        return False, f"unexpected peer_send_by_name response shape: {type(result).__name__}"
    if result.get("action_status") != "completed":
        error = result.get("error")
        if isinstance(error, dict):
            return False, f"{error.get('code')}: {error.get('message')}"
        return False, str(error)
    data = result.get("data")
    delivery = data.get("delivery") if isinstance(data, dict) else None
    if delivery in _LIVE_DELIVERY_KINDS:
        return True, None
    return False, f"delivery={delivery!r} — not a live holder"


def check_host_disk_headroom(
    orchestrator: object,
    *,
    config: HostDiskGuardConfig | None = None,
    state: dict[str, Any],
    disk_usage: DiskUsageReader = shutil.disk_usage,
) -> dict[str, Any]:
    """Measure host free space; on a severity TRANSITION, alert
    ``config.alert_role`` — at most once per transition, never once per tick.

    ``orchestrator`` needs both ``get_service("state_service")`` (the
    durable transition marker) and ``plugin_manager.get_plugin`` (the alert
    dispatch, see :func:`_send_peer_alert`) — a real, live orchestrator on
    every call; there is no quiet path that skips reaching it, because even
    an ``ok`` reading must be compared against the durable marker to detect
    a recovery. ``state`` is the calling verb's own, required so no caller
    can drop it by default; it is handed to the alert unchanged.
    """
    cfg = config or HostDiskGuardConfig()
    free_bytes = measure_free_bytes(cfg.path, disk_usage=disk_usage)
    status = classify_headroom(
        free_bytes,
        warning_floor_bytes=cfg.warning_floor_bytes,
        critical_floor_bytes=cfg.critical_floor_bytes,
    )
    last_notified = _read_last_notified_status(orchestrator, cfg.path)
    transitioned = status != last_notified
    alerted = False
    alert_reason: str | None = None
    if transitioned and not cfg.alert_role:
        # No target configured: measure correctly, but there is nobody to
        # tell. Marker NOT advanced -- same "stays pending" treatment as an
        # undelivered send, so this is retried (and re-logged) every tick
        # until a target is configured, rather than going quiet after once.
        alert_reason = REASON_ALERT_TARGET_UNCONFIGURED
        logger.error(
            "host_disk_guard: %s -> %s transition has no alert_role configured "
            "(platform_health_plugin config key 'alert_role' is unset); nobody "
            "was told; marker left at %r so this is retried every tick",
            last_notified, status, last_notified,
        )
    elif transitioned:
        # The sibling `if` branch above already excluded `not cfg.alert_role`,
        # so alert_role is guaranteed truthy here -- narrow it explicitly for
        # the type checker rather than widening _send_peer_alert's signature.
        assert cfg.alert_role
        text = build_alert_text(status=status, free_bytes=free_bytes, path=cfg.path, config=cfg)
        result = _send_peer_alert(orchestrator, name=cfg.alert_role, content=text, state=state)
        delivered, reason = _delivery_outcome(result)
        if delivered:
            alerted = True
            # Only advance the marker on a CONFIRMED live delivery -- an
            # undelivered transition stays pending, so the NEXT tick retries
            # the alert instead of silently treating "queued, nobody live
            # received it" as "told".
            _write_last_notified_status(orchestrator, cfg.path, status)
        else:
            alert_reason = reason
            logger.error(
                "host_disk_guard: %s -> %s alert to role %r NOT delivered to a live "
                "holder (%s); marker left at %r so the next tick retries",
                last_notified, status, cfg.alert_role, reason, last_notified,
            )
    return {
        "status": status,
        "free_bytes": free_bytes,
        "path": cfg.path,
        "warning_floor_bytes": cfg.warning_floor_bytes,
        "critical_floor_bytes": cfg.critical_floor_bytes,
        "transitioned": transitioned,
        "alerted": alerted,
        "alert_role": cfg.alert_role if transitioned else None,
        "alert_reason": alert_reason,
    }


def ensure_host_disk_guard_schedule(
    scheduling_service: object,
    *,
    alert_role: str | None,
    cron_expression: str = DEFAULT_SCHEDULE_CRON,
    tag: str = DEFAULT_SCHEDULE_TAG,
    label: str = DEFAULT_SCHEDULE_LABEL,
) -> dict[str, Any]:
    """Idempotently ensure the host-disk-guard cron exists on the
    platform's own scheduler (never a Claude Code loop — the platform has
    its own).

    ``scheduling_service`` is the SERVICE binding
    (``ananta.services.scheduling_service.SchedulingService``, resolved
    by the caller at call time — see the module docstring), never the
    provider plugin reached through ``plugin_manager``. Its
    ``create_cron_schedule``/``get_schedules_by_tag`` take the service's
    own friendly keyword shape (``cron_expression``, ``actions``, ``tag``,
    ``state``, ...), not the raw plugin's ``(params, state)`` wrapper.

    ``alert_role`` is required and keyword-only precisely so a caller
    cannot forget it by relying on a default. The caller passes the
    CONFIGURED role, because the installed cron fires with empty arguments
    and so resolves its target from config alone. A cron with no configured
    target would only ever
    produce ``alert_target_unconfigured`` on every tick, forever, which is
    strictly worse than not installing it at all. When falsy, this refuses
    (``status="refused"``, ``reason="alert_target_unconfigured"``) without
    contacting the scheduling service.

    Simpler than :func:`ensure_kb_retrieval_audit_schedule`'s own version of
    this idea: this checks for an existing schedule under ``tag`` via
    ``get_schedules_by_tag`` and creates one only if none exists. It does not
    attempt to normalize a stale or duplicate entry — a deliberate
    simplification for a first cut (the kickoff's "simplest sound mechanism"
    standard), named here rather than silently matched to the more thorough
    precedent.
    """
    if not alert_role:
        logger.error(
            "host_disk_guard: refusing to install the cron -- no alert_role "
            "configured (platform_health_plugin config key 'alert_role' is "
            "unset); installing it would only ever produce "
            "alert_target_unconfigured on every tick"
        )
        return {"status": "refused", "reason": REASON_ALERT_TARGET_UNCONFIGURED, "tag": tag}
    schedule_state = {
        "flow_id": _HOST_DISK_GUARD_FLOW_ID,
        "session_id": _HOST_DISK_GUARD_SESSION_ID,
    }
    existing = _unwrap_response(
        scheduling_service.get_schedules_by_tag(tag=tag, state=schedule_state),  # type: ignore[attr-defined]
        action="get_schedules_by_tag",
    )
    if existing.get("schedules"):
        return {"status": "already_present", "tag": tag}
    # LITERAL actions list and process_key: whole-tree gate C5.1 grants the
    # EDGE_SINK exemption only to a literal it can resolve (the
    # fleet_maintenance_plugin precedent). No result_processor_kind key --
    # omitting it is the EDGE_SINK-canonical shape.
    created = _unwrap_response(
        scheduling_service.create_cron_schedule(  # type: ignore[attr-defined]
            cron_expression=cron_expression,
            actions=[{
                "process_key": "plugin::platform_health_plugin::check_host_disk_headroom_cron",
                "arguments": {},
            }],
            label=label,
            tags=[tag],
            state=schedule_state,
        ),
        action="create_cron_schedule",
    )
    return {
        "status": "created",
        "tag": tag,
        "cron_expression": cron_expression,
        "schedule_id": created.get("schedule_id"),
    }


def _unwrap_response(result: object, *, action: str) -> dict[str, Any]:
    """The scheduling plugin's ``build_response`` shape: unwraps ``data`` on
    success and RAISES on an error-status response instead of returning it as
    though it were data — a logical scheduling failure must fail loudly here
    too, not be silently read as "no existing schedule found"."""
    if not isinstance(result, dict):
        raise RuntimeError(f"{action}: expected a dict response, got {type(result).__name__}")
    status = result.get("action_status")
    if status != "completed":
        raise RuntimeError(f"{action}: {result.get('error')!r} (action_status={status!r})")
    data = result.get("data")
    return data if isinstance(data, dict) else {}
