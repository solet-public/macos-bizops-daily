"""maintenance-verbs M1 (workbench
2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3; coordinator-seat ruling
on Q3, 2026-08-09) — `report_context_status` / `session_context_status`.

Shape (a), ratified: a runtime-native hook already computes context-window
occupancy client-side every tick; `report_context_status`
is a plain state upsert of that measurement (no file/subprocess I/O in this
handler — the file read already happened in the caller, sanctioned ms-scale
state work per D0.3 §1), and `session_context_status` is a trivial state read
of the cached row. Neither verb resolves a transcript path or reads a file
itself; both are pure over `session_context_status_store`.

Fraction / rotation-due / per-prompt-carriage are derived at READ time from
the live `rotation_thresholds` constants, never stored — a future change to
`ROTATION_THRESHOLD_FRACTION` must never require a backfill of stored rows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import rotation_thresholds
from .action_cost_profile import (
    ActionCalibrationUnavailableError,
    ActionCostCalibration,
    ActionCostProfileCatalog,
    ActionCostProfileValidationError,
    load_action_cost_profile_catalog,
)
from .model_profile import (
    ModelCapabilityProfile,
    ModelProfileCatalog,
    ProfileValidationError,
    load_model_profile_catalog,
)
from .session_context_status_store import (
    AmbiguousAgentSessionIdError,
    StaleContextReadingError,
    read_agent_session_id_for_binding,
    read_session_context_status,
    read_session_context_status_by_agent_session_id,
    upsert_session_context_status,
)
from .session_lifecycle_verbs import VerbError
from .usage_economics import (
    FlatRateQuotaStrategy,
    MeteredApiStrategy,
    ProjectedAction,
    RotationDecisionInput,
    RotationEconomicsDecision,
    UsageEconomicsProfileCatalog,
    UsageVector,
    load_usage_economics_profile_catalog,
)
from .usage_economics_profiles import (
    FlatRateQuotaProfile,
    MeteredApiProfile,
    UsageEconomicsProfile,
    UsageEconomicsProfileValidationError,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

# The per-prompt carriage-cost heuristic named in the operator's 2026-08-09
# ruling (feedback_rotation_economics_and_context_gauge): a cached-context
# read bills roughly this fraction of base input, EVERY turn, before any new
# work. A declared constant, not a guess dressed as one — matches the
# operator's own "doesn't 800k context result in 80k tokens used per prompt?"
# arithmetic exactly (800_000 * 0.1 = 80_000).
CACHE_READ_COST_FRACTION = 0.1

_PROFILE_ROOT = Path(__file__).resolve().parents[2] / "model_profiles"
_CAPABILITY_PROFILE_PATHS = tuple(sorted(_PROFILE_ROOT.glob("*_capabilities.v1.json")))
_ACTION_COST_PROFILE_PATH = _PROFILE_ROOT / "context_action_costs.v1.json"
_USAGE_ECONOMICS_PROFILE_PATH = _PROFILE_ROOT / "usage_economics.v1.json"

# The path CLASSES a reporting hook can identify itself as (2026-08-16). A
# CLASS, deliberately, not a path: the stored row must stay meaningful across
# machines, and an absolute path would encode one host's layout into shared
# state. 'unknown' is a first-class member rather than an error case — a hook
# that cannot classify its own location must be able to say exactly that,
# because the alternative is guessing, and a guessed surface is worse than an
# admitted unknown for the one job these columns have.
REPORTER_SURFACE_CHECKOUT = "checkout"
REPORTER_SURFACE_PLUGIN_CACHE = "plugin_cache"
REPORTER_SURFACE_UNKNOWN = "unknown"
# WIDENED 2026-08-17 (phase 1 of 2). The original three collapsed at least
# three genuinely distinct surfaces into `unknown`: the vendored source copy
# in the repo, the copy of it inside a deployed release tree, and -- not
# anticipated when the field shipped -- any checkout hook living in a
# SUBDIRECTORY of `.claude/hooks/`, which the reporter's `parents[N]` test
# failed to recognise. Measured consequence: a row's surface was observed
# ALTERNATING between `checkout` and `unknown` tick-by-tick on the same
# session, which is the shared-throttle race between two copies that both
# carry the field -- and the collapsed bucket made it impossible to say which
# copy the other one was.
#
# ★ THIS HALF LANDS AND DEPLOYS ALONE, BEFORE ANY REPORTER EMITS THE NEW
# VALUES. The verb rejects an unrecognised surface BEFORE any write, so a
# reporter that learned the new classes first would have every report refused
# and would write no row at all -- turning an attribution gap into a total
# reporting outage for exactly the sessions the change exists to illuminate.
# Widening is inert on its own: it rejects nothing that previously passed, and
# nothing emits these yet. The reporter half follows only once this is
# CONFIRMED SERVING (checked against the serving release's own identity, not
# master and not a deploy report -- deployed is not serving until the swap is
# confirmed).
REPORTER_SURFACE_VENDORED = "vendored"
REPORTER_SURFACE_RELEASE = "release"
REPORTER_SURFACES = frozenset(
    {
        REPORTER_SURFACE_CHECKOUT,
        REPORTER_SURFACE_PLUGIN_CACHE,
        REPORTER_SURFACE_VENDORED,
        REPORTER_SURFACE_RELEASE,
        REPORTER_SURFACE_UNKNOWN,
    },
)


def _require_report_identity(
    *,
    agent_instance_id: str,
    runtime_session_id: str,
    provider: str,
    runtime: str,
    model: str,
    effort: str,
    measured_at: str,
) -> None:
    required = {
        "agent_instance_id": agent_instance_id,
        "runtime_session_id": runtime_session_id,
        "provider": provider,
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "measured_at": measured_at,
    }
    missing = sorted(name for name, value in required.items() if not value.strip())
    if missing:
        raise VerbError(
            "missing_argument",
            "report_context_status requires non-empty " + ", ".join(missing) + ".",
        )


def _require_report_counts(
    *,
    current_tokens: int,
    ceiling: int,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
) -> None:
    optional_counts = (cache_read_tokens, cache_write_tokens)
    if current_tokens >= 0 and ceiling > 0 and all(
        value is None or value >= 0 for value in optional_counts
    ):
        return
    raise VerbError(
        "negative_tokens",
        f"report_context_status got current_tokens={current_tokens!r}, ceiling={ceiling!r}, "
        f"cache_read_tokens={cache_read_tokens!r}, cache_write_tokens={cache_write_tokens!r} "
        "— capacities must be positive and usage counts non-negative.",
    )


def _require_reporter_surface(reporter_surface: str | None) -> None:
    if reporter_surface is None or reporter_surface in REPORTER_SURFACES:
        return
    raise VerbError(
        "unknown_reporter_surface",
        f"report_context_status got reporter_surface={reporter_surface!r}, which is not one "
        f"of {sorted(REPORTER_SURFACES)}. Report 'unknown' when the hook cannot classify its "
        "own location — an invented surface silently poisons attribution.",
    )


def report_context_status(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    runtime_session_id: str,
    provider: str,
    runtime: str,
    model: str,
    effort: str,
    current_tokens: int,
    ceiling: int,
    measured_at: str,
    reading_at: str | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cache_cold: bool | None = None,
    cache_overage_signature: bool | None = None,
    reporter_surface: str | None = None,
    reporter_generation: int | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Overwrite the caller's own latest context-status snapshot. The caller
    already did the local transcript read and runtime-capacity lookup. This
    verb trusts the reported `ceiling`/`current_tokens` rather than recomputing
    them, so it never touches a file itself.

    The three cache fields are OPTIONAL and describe THE MOST RECENT
    ASSISTANT CALL — the same call `current_tokens` is summed from.
    Omitting them records NOT REPORTED, which the read-back verb surfaces
    as `null` rather than as a warm cache: a reporter that never looked
    and a reporter that looked and found the cache live are different
    facts, and collapsing them would let every un-upgraded hook assert a
    warm cache it never measured.

    `reporter_surface`/`reporter_generation` are OPTIONAL and identify WHICH
    COPY of the reporting hook produced this snapshot. More than one copy can
    be registered on the same event at once, they serialize on a shared
    throttle marker that records nothing about who claimed it, and this
    table keeps only the latest row — so without these a row is
    unattributable, and an absent cache field cannot be told apart from a
    stale copy having served that tick. Omitting them records a
    pre-attribution reporter, which is a positive finding, not missing data.

    `reading_at` is OPTIONAL and is the SECOND CLOCK (GAU-14 D3, 2026-08-19).
    `measured_at` says when the reporter LOOKED; this says when the reading it
    carries was PRODUCED — the transcript line's own timestamp. They differ by
    the age of that line at observation time, measured at ~34s on the sweep
    path and ~4 minutes on the prompt-surfaced path, which is enough to make
    two notices about one strictly-monotone series read as later-but-LOWER.
    Omitting it records NOT REPORTED — never a fabricated zero lag, the same
    discipline the cache fields above follow and for the same reason.

    `agent_session_id` is OPTIONAL and is the ROUTING JOIN (2026-08-18): the
    reporter's own stable `$AGENT_SESSION_ID`, stored so a consumer holding
    this row can reverse-resolve the session's live bridge binding through
    `peer_registry.resolve_by_agent_session_id`. Without it, a watcher-held
    worker is unreachable from its own gauge row -- the row keys on the LEDGER
    id and the binding keys on the WATCH id. Omitting it records NOT REPORTED,
    never "this session has no bridge".

    ★ UNLIKE `reporter_surface`, THIS FIELD IS NOT VALIDATED AGAINST AN
    ALLOWLIST, and that difference is deliberate. The surface allowlist is why
    that widening had to land and deploy ALONE, ahead of any reporter emitting
    it: a reporter upgrading first would have had every report REFUSED, turning
    an attribution gap into a total reporting outage. This field has no such
    edge -- MEASURED 2026-08-18 against the live pre-change verb, which
    accepted an undeclared `agent_session_id` and returned `recorded` while
    ignoring it. So both deploy orders degrade to NULL and neither loses a row,
    and this landing carries NO ordering constraint. Recorded because the
    precedent sitting a few lines above says the opposite, and a reader is
    entitled to assume it binds here too.

    Errors: `missing_argument` (any required identity/clock field empty/absent
    — fast-fail before any write), `negative_tokens` (a reported value that
    cannot be a real token count, catching a caller bug loud rather than
    silently caching garbage), `unknown_reporter_surface` (a surface outside
    the known classes — a typo'd or invented surface would quietly poison
    exactly the attribution these columns exist to provide, so it fails loud
    instead of being stored)."""
    _require_report_identity(
        agent_instance_id=agent_instance_id,
        runtime_session_id=runtime_session_id,
        provider=provider,
        runtime=runtime,
        model=model,
        effort=effort,
        measured_at=measured_at,
    )
    _require_report_counts(
        current_tokens=current_tokens,
        ceiling=ceiling,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    _require_reporter_surface(reporter_surface)
    try:
        recorded = upsert_session_context_status(
            state,
            agent_instance_id=agent_instance_id,
            claude_session_id=runtime_session_id,
            provider=provider,
            runtime=runtime,
            model=model,
            effort=effort,
            current_tokens=current_tokens,
            ceiling=ceiling,
            measured_at=measured_at,
            reading_at=reading_at,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_cold=cache_cold,
            cache_overage_signature=cache_overage_signature,
            reporter_surface=reporter_surface,
            reporter_generation=reporter_generation,
            agent_session_id=agent_session_id,
        )
    except StaleContextReadingError as exc:
        raise VerbError("stale_context_reading", str(exc)) from exc
    return {"status": "recorded" if recorded else "unchanged"}


def _tri_state(raw: object) -> bool | None:
    """Stored 1/0/NULL -> True/False/None. ``None`` means NOT REPORTED.

    Never coerces NULL to False. A reporter that never looked and a reporter
    that looked and found the cache live are different facts, and this is the
    boundary where collapsing them would become invisible to every caller.
    """
    return None if raw is None else bool(raw)


def _cache_view(row: dict[str, Any], current_tokens: int) -> dict[str, Any]:
    """Cache state plus the economic band it implies.

    The band is DERIVED at read time from the live policy constants, the same
    rule `fraction`/`rotation_due` already follow — a change to the bands must
    never require backfilling stored rows.

    When cache state was not reported the band is still computed, as the WARM
    band, and `cache_cold` is `null` so the caller can see the assumption
    rather than inherit it silently.
    """
    cold = _tri_state(row.get("cache_cold"))
    band, guidance = rotation_thresholds.rotation_band(
        current_tokens, cache_cold=bool(cold),
    )
    return {
        "cache_read_tokens": (
            None if row.get("cache_read_tokens") is None
            else int(row["cache_read_tokens"])
        ),
        "cache_write_tokens": (
            None if row.get("cache_write_tokens") is None
            else int(row["cache_write_tokens"])
        ),
        "cache_cold": cold,
        "cache_overage_signature": _tri_state(row.get("cache_overage_signature")),
        "rotation_band": band,
        "rotation_guidance": guidance,
    }


def _routing_view(row: dict[str, Any]) -> dict[str, Any]:
    """How to REACH this session, as opposed to how to describe it.

    Extracted rather than inlined beside its neighbours, and rather than
    allowlisted: adding this field inline took `session_context_status` to
    cyclomatic complexity 11, one over the gate. The file already answers that
    shape with `_cache_view`/`_reporter_view`, so this is the existing idiom
    rather than a new one -- and an allowlist entry would have frozen the
    complexity at the ceiling and handed the wall to whoever edits this verb
    next, which is the failure mode the L4c extraction was made to avoid.

    NOT `str(... or "")` like its neighbours in the caller: that idiom maps
    NULL to `""` and would erase the NOT-REPORTED state this column exists to
    carry. `resolve_by_agent_session_id` returns None for an empty string, so
    the collapse is invisible in the outcome -- a reporter that never sent a
    session id would become indistinguishable from a session that genuinely
    could not be routed, which is precisely the discrimination the L4c counts
    depend on.
    """
    agent_session_id = row.get("agent_session_id")
    return {
        "agent_session_id": (
            None if agent_session_id is None else str(agent_session_id)
        ),
    }


def _reporter_view(row: dict[str, Any]) -> dict[str, Any]:
    """Which copy of the reporting hook wrote this row, on two independent axes.

    Both `null` means the tick was served by a reporter predating this
    widening — which is a POSITIVE finding, not missing data: only a stale
    copy can produce it. That matters because this table keeps one row per
    session and the latest write wins, so an absent cache field is otherwise
    ambiguous between "the verbs are not deployed" and "a stale copy served
    this tick". These two fields are what makes those distinguishable, and
    they are deliberately NOT folded into one value — a current-generation
    hook running from the wrong surface and a stale-generation hook running
    from the right one are different failures with different fixes.
    """
    generation = row.get("reporter_generation")
    surface = row.get("reporter_surface")
    return {
        "reporter_surface": None if surface is None else str(surface),
        "reporter_generation": None if generation is None else int(generation),
    }


ID_RESOLUTION_DIRECT = "direct"
ID_RESOLUTION_VIA_BINDING = "resolved_via_binding"
ID_RESOLUTION_UNRESOLVED = "unresolved"


def resolve_status_row(
    state: StateManagementInterface, agent_instance_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """Find this session's gauge row from EITHER of the ids it is known by
    (GAU-07), returning the row and how it was reached.

    THE DEFECT. This table keys on the LEDGER id, but ``peer_list`` -- the
    documented way to enumerate live sessions -- publishes the WATCH id for
    any watcher-held session. Keyed on the id a caller actually has, the
    lookup found nothing, so a healthy, freshly-reporting worker read as
    gauge-less. Measured on three live sessions on 2026-08-18; three of five
    ``claude_code`` instances in that fleet were watcher-held.

    THE CHAIN, both hops STORED, no id derived from another:
    ``agent_instance_id`` -> ``peer_binding.agent_session_id`` -> the gauge
    row carrying that same stable session id. The watch id is a ONE-WAY
    sha256 digest of the session id, so the reverse simply does not exist as
    a computation -- and two session-id minting schemes are live at once
    (``ases-agi-<ledger id>`` from the spawned-worker launcher,
    ``ases-<epoch>-<pid>-<n>`` from the seat launcher), so a prefix-slice
    would work on the first and route to nothing on the second.

    The direct lookup is tried FIRST and short-circuits, so the overwhelmingly
    common ledger-keyed read costs exactly what it did before and can never be
    rerouted through the join.

    PUBLIC as of GAU-15 (2026-08-19) because a second caller appeared:
    ``gauge_series`` resolves the same two ids before reading a session's
    history. Re-deriving this chain there would put two copies of a routing
    rule in the tree, and the drift between two copies of a routing rule is
    silent -- each keeps returning a plausible row.
    """
    row = read_session_context_status(state, agent_instance_id)
    if row is not None:
        return row, ID_RESOLUTION_DIRECT
    agent_session_id = read_agent_session_id_for_binding(state, agent_instance_id)
    resolved = read_session_context_status_by_agent_session_id(state, agent_session_id)
    if resolved is None:
        return None, ID_RESOLUTION_UNRESOLVED
    return resolved, ID_RESOLUTION_VIA_BINDING


def _measurement(row: dict[str, Any]) -> tuple[int, int, float]:
    """The stored occupancy numbers and the fraction derived from them.

    Extracted for the complexity reason recorded on ``_identity_view``, but it
    earns its place on meaning too: these three MUST be derived together. The
    fraction's denominator is the same stored ``ceiling`` this verb publishes,
    so a reader can always check the published verdict against the published
    numbers and find them consistent -- computing the fraction against any
    other denominator is how ``rotation_due`` and ``rotation_band`` came to
    contradict each other on the same row (GAU-08).

    A stored ``ceiling`` of 0 means the reporter never resolved one, so the
    conservative default stands in rather than a division by zero; the guarded
    ``fraction`` keeps that substitution from silently becoming a real
    measurement if the default is ever itself zero.
    """
    current_tokens = int(row.get("current_tokens") or 0)
    ceiling = int(row.get("ceiling") or 0) or rotation_thresholds.DEFAULT_CONSERVATIVE_CEILING
    fraction = current_tokens / ceiling if ceiling else 0.0
    return current_tokens, ceiling, fraction


def _identity_view(
    row: dict[str, Any] | None, queried_agent_instance_id: str, id_resolution: str,
) -> dict[str, Any]:
    """WHICH SESSION this row describes, and HOW it was reached (GAU-07).

    Extracted rather than inlined for the reason this file's own
    ``_routing_view`` records: adding fields inline pushed
    ``session_context_status`` past the complexity gate, and an allowlist entry
    would freeze it AT the ceiling and hand the wall to whoever edits the verb
    next. Extraction is the idiom here; the allowlist is not.

    ``agent_instance_id`` is THE ROW'S OWN ledger id, never the caller's
    argument. Echoing the argument back was harmless while a direct hit was the
    only way to reach a row; once a WATCH id can resolve to a LEDGER-keyed row,
    echoing it would label the row with an id it is not keyed on -- seeding the
    next join error while claiming to have fixed this one. The queried id is
    reported alongside rather than dropped, so the join stays traceable and no
    caller loses the value it passed in.
    """
    resolved_id = None if row is None else str(row.get("agent_instance_id") or "")
    return {
        "agent_instance_id": resolved_id or queried_agent_instance_id,
        "queried_agent_instance_id": queried_agent_instance_id,
        "id_resolution": id_resolution,
    }


def _unresolved_status(agent_instance_id: str, id_resolution: str) -> dict[str, Any]:
    """The honest-gap shape, carrying the SAME key set as the resolved one.

    That identical key set is a standing contract, not an accident: a caller
    must not be able to ``KeyError`` its way through a legitimate
    ``resolved: false``. Every field is a null/zero PLACEHOLDER, never an
    estimate -- the repo rule against promoting an unknown into a fact applies
    most sharply exactly here, where a plausible-looking number would be
    indistinguishable from a measured one.
    """
    return {
        "resolved": False,
        "resolution_error": (
            f"no session_context_status report on file for {agent_instance_id!r} — "
            "either this session has not completed a reporting tick yet, or "
            "(for host=operator sessions) the seat-wiring design note has not "
            "been acted on yet. This id was ALSO resolved through the peer "
            "binding (the watch-id join) and still matched no row, so it is a "
            "genuine reporting gap rather than the GAU-07 lookup miss."
        ),
        **_identity_view(None, agent_instance_id, id_resolution),
        "runtime_session_id": "",
        "provider": "",
        "runtime": "",
        "model": "",
        "effort": "",
        "current_tokens": 0,
        "ceiling": 0,
        "fraction": 0.0,
        "per_prompt_carriage_estimate_tokens": 0,
        "rotation_due": False,
        "measured_at": "",
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cache_cold": None,
        "cache_overage_signature": None,
        "rotation_band": None,
        "rotation_guidance": None,
        "reporter_surface": None,
        "reporter_generation": None,
        "agent_session_id": None,
        "calculated_verdict": None,
    }


def _rotation_notice_due(
    row: dict[str, Any], *, current_tokens: int, ceiling: int,
) -> bool:
    """Return the shared durability-notice verdict for one stored gauge."""
    return rotation_thresholds.rotation_notice_verdict(
        model=str(row.get("model") or ""),
        effort=str(row.get("effort") or ""),
        current_tokens=current_tokens,
        runtime_window_tokens=ceiling if ceiling > 0 else None,
    ).due


def _calculation_text(request: dict[str, Any], field: str) -> str:
    value = request.get(field)
    if not isinstance(value, str) or not value.strip():
        raise VerbError(
            "invalid_calculation_request",
            f"calculation_request.{field} must be a non-empty string.",
        )
    return value.strip()


def _calculation_int(request: dict[str, Any], field: str) -> int:
    value = request.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise VerbError(
            "invalid_calculation_request",
            f"calculation_request.{field} must be an integer.",
        )
    return value


def _calculation_optional_float(request: dict[str, Any], field: str) -> float | None:
    value = request.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerbError(
            "invalid_calculation_request",
            f"calculation_request.{field} must be a finite number when supplied.",
        )
    result = float(value)
    if not isfinite(result) or result < 0:
        raise VerbError(
            "invalid_calculation_request",
            f"calculation_request.{field} must be finite and non-negative when supplied.",
        )
    return result


def _required_actions(request: dict[str, Any]) -> tuple[str, ...]:
    raw = request.get("required_actions")
    if not isinstance(raw, list) or not raw:
        raise VerbError(
            "invalid_calculation_request",
            "calculation_request.required_actions must be a non-empty list.",
        )
    if any(not isinstance(action, str) or not action.strip() for action in raw):
        raise VerbError(
            "invalid_calculation_request",
            "calculation_request.required_actions must contain non-empty strings.",
        )
    actions = tuple(action.strip() for action in raw)
    if len(actions) != len(set(actions)):
        raise VerbError(
            "invalid_calculation_request",
            "calculation_request.required_actions cannot contain duplicates.",
        )
    return actions


def _reported_calculation_identity(row: dict[str, Any]) -> dict[str, str]:
    identity = {
        name: str(row.get(name) or "")
        for name in ("provider", "runtime", "model", "effort")
    }
    missing = sorted(name for name, value in identity.items() if not value)
    if missing:
        raise VerbError(
            "calculation_identity_unreported",
            "the gauge row predates runtime-neutral identity fields: " + ", ".join(missing),
        )
    return identity


def _calculation_cache_state(cache_cold: bool | None) -> str:
    if cache_cold is None:
        return "unknown"
    return "observed_cold" if cache_cold else "observed_warm"


@dataclass(frozen=True)
class _TrustedCalculationEvidence:
    capability: ModelCapabilityProfile
    actions: tuple[ActionCostCalibration, ...]
    economics_catalog: UsageEconomicsProfileCatalog
    economics_profile: UsageEconomicsProfile
    expected_calls_after: int
    quality_floor: float | None
    keep_latency_seconds: float | None
    keep_quality_score: float | None


def _reject_untrusted_calculation_values(request: dict[str, Any]) -> None:
    calibrations = request.get("calibrations")
    if calibrations not in (None, []):
        raise VerbError(
            "untrusted_calculation_request",
            "calculation_request.calibrations is a legacy untrusted field; "
            "action measurements are resolved from the checked-in catalog.",
        )
    if request.get("cache_read_multiplier") is not None:
        raise VerbError(
            "untrusted_calculation_request",
            "calculation_request.cache_read_multiplier is caller-controlled; "
            "normalization is resolved from priced catalog records.",
        )


def _capability_catalog(*, as_of: datetime) -> ModelProfileCatalog:
    if not _CAPABILITY_PROFILE_PATHS:
        raise ProfileValidationError(f"no capability profiles under {_PROFILE_ROOT}")
    profiles = tuple(
        profile
        for path in _CAPABILITY_PROFILE_PATHS
        for profile in load_model_profile_catalog(path, as_of=as_of).profiles
    )
    return ModelProfileCatalog(profiles=profiles)


def _matching_action_rows(
    catalog: ActionCostProfileCatalog,
    *,
    capability: ModelCapabilityProfile,
    effort: str,
) -> tuple[ActionCostCalibration, ...]:
    return tuple(
        row
        for row in catalog.profiles
        if (
            row.provider,
            row.runtime,
            row.model,
            row.effort,
        )
        == (
            capability.provider,
            capability.runtime,
            capability.canonical_model_id,
            effort,
        )
    )


def _require_profile_claim(
    *,
    request: dict[str, Any],
    field: str,
    expected: str,
) -> None:
    actual = _calculation_text(request, field)
    if actual != expected:
        raise VerbError(
            "calculation_profile_mismatch",
            f"calculation_request.{field}={actual!r} does not match resolved "
            f"catalog value {expected!r}.",
        )


def _economics_objective(profile: UsageEconomicsProfile) -> str:
    if isinstance(profile, MeteredApiProfile):
        return MeteredApiStrategy.OBJECTIVE
    return FlatRateQuotaStrategy.OBJECTIVE


def _require_economics_scope(
    profile: UsageEconomicsProfile,
    capability: ModelCapabilityProfile,
) -> None:
    if isinstance(profile, MeteredApiProfile):
        matched = (
            profile.provider,
            profile.runtime,
            profile.model,
        ) == (
            capability.provider,
            capability.runtime,
            capability.canonical_model_id,
        )
    else:
        matched = (
            profile.provider == capability.provider
            and capability.runtime in profile.included_runtimes
        )
    if not matched:
        raise VerbError(
            "calculation_profile_mismatch",
            f"economics profile {profile.profile_id!r} does not apply to "
            f"{capability.provider}/{capability.runtime}/"
            f"{capability.canonical_model_id}.",
        )


def _trusted_calculation_evidence(
    *,
    reported_identity: dict[str, str],
    ceiling: int,
    request: dict[str, Any],
) -> _TrustedCalculationEvidence:
    _reject_untrusted_calculation_values(request)
    expected_calls_after = _calculation_int(request, "expected_calls_after")
    if expected_calls_after < 0:
        raise VerbError(
            "invalid_calculation_request",
            "calculation_request.expected_calls_after must be non-negative.",
        )
    quality_floor = _calculation_optional_float(request, "quality_floor")
    keep_latency_seconds = _calculation_optional_float(
        request,
        "keep_latency_seconds",
    )
    keep_quality_score = _calculation_optional_float(request, "keep_quality_score")
    as_of = datetime.now(UTC)
    try:
        capability = _capability_catalog(as_of=as_of).resolve(
            reported_identity["provider"],
            reported_identity["runtime"],
            reported_identity["model"],
        )
        capability.require_effort(reported_identity["effort"])
        action_catalog = load_action_cost_profile_catalog(
            _ACTION_COST_PROFILE_PATH,
            as_of=as_of,
        )
        economics_catalog = load_usage_economics_profile_catalog(
            _USAGE_ECONOMICS_PROFILE_PATH,
            as_of=as_of,
        )
        economics_profile = economics_catalog.resolve(
            _calculation_text(request, "usage_economics_profile_id"),
        )
        if capability.fetched_at > as_of:
            raise ProfileValidationError(
                f"capability provenance is from the future: "
                f"{capability.fetched_at.isoformat()}",
            )
        if economics_profile.fetched_at > as_of:
            raise UsageEconomicsProfileValidationError(
                f"economics provenance is from the future: "
                f"{economics_profile.fetched_at.isoformat()}",
            )
    except (
        ActionCostProfileValidationError,
        ProfileValidationError,
        UsageEconomicsProfileValidationError,
    ) as exc:
        raise VerbError("calculation_profile_unavailable", str(exc)) from exc
    if ceiling > capability.provider_context_ceiling:
        raise VerbError(
            "calculation_profile_mismatch",
            f"reported runtime ceiling {ceiling} exceeds provider catalog ceiling "
            f"{capability.provider_context_ceiling}.",
        )
    _require_profile_claim(
        request=request,
        field="capability_profile_id",
        expected=capability.profile_version,
    )
    _require_profile_claim(
        request=request,
        field="capability_profile_version",
        expected=capability.profile_version,
    )
    _require_profile_claim(
        request=request,
        field="usage_economics_profile_version",
        expected=economics_profile.profile_version,
    )
    _require_profile_claim(
        request=request,
        field="objective",
        expected=_economics_objective(economics_profile),
    )
    _require_economics_scope(economics_profile, capability)
    available = _matching_action_rows(
        action_catalog,
        capability=capability,
        effort=reported_identity["effort"],
    )
    required = _required_actions(request)
    available_actions = tuple(sorted(row.action for row in available))
    if tuple(sorted(required)) != available_actions:
        raise VerbError(
            "calculation_profile_mismatch",
            f"required_actions must name the complete applicable catalog set "
            f"{list(available_actions)!r}; got {list(required)!r}.",
        )
    by_action = {row.action: row for row in available}
    return _TrustedCalculationEvidence(
        capability=capability,
        actions=tuple(by_action[action] for action in required),
        economics_catalog=economics_catalog,
        economics_profile=economics_profile,
        expected_calls_after=expected_calls_after,
        quality_floor=quality_floor,
        keep_latency_seconds=keep_latency_seconds,
        keep_quality_score=keep_quality_score,
    )


def _unpriced_action_errors(evidence: _TrustedCalculationEvidence) -> list[str]:
    errors: list[str] = []
    for action in evidence.actions:
        try:
            action.require_priced()
        except ActionCalibrationUnavailableError as exc:
            errors.append(str(exc))
    return errors


def _metered_normalization_error(
    profile: MeteredApiProfile,
    actions: tuple[ActionCostCalibration, ...],
) -> str | None:
    if profile.input_per_mtok is None or profile.input_per_mtok <= 0:
        return "metered input price is unavailable or non-positive"
    if profile.cached_input_per_mtok is None or profile.cache_write_per_mtok is None:
        return "metered cached-input/cache-write prices are unavailable"
    expected_read = profile.cached_input_per_mtok / profile.input_per_mtok
    expected_write = profile.cache_write_per_mtok / profile.input_per_mtok
    for action in actions:
        if (
            action.cache_read_multiplier != expected_read
            or action.cache_write_multiplier != expected_write
        ):
            return (
                f"{action.action} normalization does not match selected profile: "
                f"read={action.cache_read_multiplier!r}/{expected_read!r}, "
                f"write={action.cache_write_multiplier!r}/{expected_write!r}"
            )
    return None


def _priced_evidence_error(evidence: _TrustedCalculationEvidence) -> str | None:
    errors = _unpriced_action_errors(evidence)
    if errors:
        return "action evidence is not priced: " + "; ".join(errors)
    profile = evidence.economics_profile
    if not isinstance(profile, MeteredApiProfile):
        return None
    return _metered_normalization_error(profile, evidence.actions)


def _pure_calibrations(
    evidence: _TrustedCalculationEvidence,
) -> tuple[rotation_thresholds.RotationActionCalibration, ...]:
    rows: list[rotation_thresholds.RotationActionCalibration] = []
    for action in evidence.actions:
        prefix = action.post_action_prefix.estimate_tokens
        boot = action.boot_prefix.estimate_tokens
        rehydration = action.rehydration_prefix.estimate_tokens
        multiplier = action.cache_write_multiplier
        assert prefix is not None and boot is not None and rehydration is not None
        assert multiplier is not None
        rows.append(rotation_thresholds.RotationActionCalibration(
            action=action.action,
            post_action_prefix_tokens=prefix,
            one_time_cost_units=(boot + rehydration) * multiplier,
            calibration_profile_id=(
                f"{action.provider}/{action.runtime}/{action.model}/"
                f"{action.effort}/{action.action}"
            ),
            calibration_profile_version=action.profile_version,
        ))
    return tuple(rows)


def _optional_row_count(row: dict[str, Any], field: str) -> int | None:
    value = row.get(field)
    return None if value is None else int(value)


def _quota_projection(profile: UsageEconomicsProfile) -> dict[str, float | None]:
    if not isinstance(profile, FlatRateQuotaProfile):
        return {}
    return {pool.pool_id: None for pool in profile.allowance_pools}


def _projected_actions(
    *,
    row: dict[str, Any],
    current_tokens: int,
    evidence: _TrustedCalculationEvidence,
) -> tuple[ProjectedAction, ...]:
    calls = evidence.expected_calls_after
    cached = _optional_row_count(row, "cache_read_tokens")
    cache_write = _optional_row_count(row, "cache_write_tokens")
    input_tokens = (
        None
        if cached is None or cached > current_tokens
        else (current_tokens - cached) * calls
    )
    quota = _quota_projection(evidence.economics_profile)
    actions = [ProjectedAction(
        name="keep",
        total_usage=UsageVector(
            input_tokens=input_tokens,
            cached_input_tokens=None if cached is None else cached * calls,
            cache_write_input_tokens=None if cache_write is None else cache_write * calls,
            output_tokens=None,
            reasoning_output_tokens=None,
            tool_calls=None,
        ),
        post_action_context_tokens=current_tokens,
        latency_seconds=evidence.keep_latency_seconds,
        quality_score=evidence.keep_quality_score,
        accepted_work_units=float(calls),
        quota_by_pool=quota,
    )]
    for action in evidence.actions:
        prefix = action.post_action_prefix.estimate_tokens
        boot = action.boot_prefix.estimate_tokens
        rehydration = action.rehydration_prefix.estimate_tokens
        actions.append(ProjectedAction(
            name=action.action,
            total_usage=UsageVector(
                input_tokens=None,
                cached_input_tokens=None if prefix is None else prefix * calls,
                cache_write_input_tokens=(
                    None if boot is None or rehydration is None else boot + rehydration
                ),
                output_tokens=None,
                reasoning_output_tokens=None,
                tool_calls=None,
            ),
            post_action_context_tokens=prefix,
            latency_seconds=None,
            quality_score=None,
            accepted_work_units=float(calls),
            quota_by_pool=quota,
        ))
    return tuple(actions)


def _economics_decision(
    *,
    row: dict[str, Any],
    current_tokens: int,
    ceiling: int,
    reported_identity: dict[str, str],
    evidence: _TrustedCalculationEvidence,
) -> RotationEconomicsDecision:
    inputs = RotationDecisionInput(
        provider=evidence.capability.provider,
        runtime=evidence.capability.runtime,
        model=evidence.capability.canonical_model_id,
        effort=reported_identity["effort"],
        current_context_tokens=current_tokens,
        capacity_tokens=ceiling,
        expected_calls_after=evidence.expected_calls_after,
        quality_floor=evidence.quality_floor,
        actions=_projected_actions(
            row=row,
            current_tokens=current_tokens,
            evidence=evidence,
        ),
    )
    profile = evidence.economics_profile
    if isinstance(profile, MeteredApiProfile):
        decision = MeteredApiStrategy(profile).decide(inputs)
    else:
        decision = FlatRateQuotaStrategy(
            profile,
            catalog=evidence.economics_catalog,
        ).decide(inputs)
    return replace(
        decision,
        projected_constraints=decision.projected_constraints + (
            "projection_basis=native_usage_only; missing dimensions remain null; "
            "no token conversion is inferred",
        ),
    )


def _calculated_verdict(
    *,
    row: dict[str, Any],
    current_tokens: int,
    ceiling: int,
    cache_cold: bool | None,
    request: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve trusted catalogs, then expose pure and selected-strategy evidence."""
    if request is None:
        return None
    reported_identity = _reported_calculation_identity(row)
    evidence = _trusted_calculation_evidence(
        reported_identity=reported_identity,
        ceiling=ceiling,
        request=request,
    )
    evidence_error = _priced_evidence_error(evidence)
    pure_calibrations = () if evidence_error is not None else _pure_calibrations(evidence)
    cache_multiplier = (
        None if evidence_error is not None else evidence.actions[0].cache_read_multiplier
    )
    verdict = rotation_thresholds.calculated_context_verdict(
        current_tokens=current_tokens,
        ceiling=ceiling,
        expected_calls_after=evidence.expected_calls_after,
        cache_state=_calculation_cache_state(cache_cold),
        cache_read_multiplier=cache_multiplier,
        calibrations=pure_calibrations,
        required_actions=tuple(action.action for action in evidence.actions),
        capability_profile_id=evidence.capability.profile_version,
        capability_profile_version=evidence.capability.profile_version,
        usage_economics_profile_id=evidence.economics_profile.profile_id,
        usage_economics_profile_version=evidence.economics_profile.profile_version,
        objective=_economics_objective(evidence.economics_profile),
        evidence_error=evidence_error,
    )
    economics = _economics_decision(
        row=row,
        current_tokens=current_tokens,
        ceiling=ceiling,
        reported_identity=reported_identity,
        evidence=evidence,
    )
    result = asdict(verdict)
    if not verdict.resolved or not economics.resolved:
        causes = [verdict.economic_cause]
        causes.append(f"selected economics strategy {economics.status}: {economics.explanation}")
        result.update(
            resolved=False,
            chosen_action=None,
            economic_choice=None,
            economic_cause="; ".join(causes),
        )
    else:
        result.update(
            economic_choice=economics.chosen_action,
            economic_cause=economics.explanation,
            chosen_action=(
                verdict.chosen_action
                if verdict.capacity_band == "capacity_critical"
                and economics.chosen_action == "keep"
                else economics.chosen_action
            ),
        )
    return {
        **result,
        "provider": reported_identity["provider"],
        "runtime": reported_identity["runtime"],
        "model": reported_identity["model"],
        "effort": reported_identity["effort"],
        "economics_decision": asdict(economics),
    }


def session_context_status(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    calculation_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read the cached snapshot for `agent_instance_id`. `resolved=False`
    (never a raised `VerbError`) is the expected, stable shape for "no report
    has landed yet for this session" — e.g. a fresh session pre-first-tick,
    or (until the seat-wiring design note is acted on) any `host=operator`
    seat. Callers must treat `resolved=False` as a loud, honest gap, never
    estimate a number in its place (the standing repo rule against silently
    promoting an unknown into a fact).

    Cache fields carry three states, not two: ``true``/``false``/``null``,
    where ``null`` is NOT REPORTED. `cache_cold` reflects the reporter's
    classification, which EXCLUDES the first call after a `/clear` — that call
    is cold by construction, and counting it would make every rotation
    recommend another one. `rotation_band` applies the ratified economic
    policy; with cache state unreported it is the WARM band, and the `null`
    beside it is how you can tell.

    `reporter_surface`/`reporter_generation` say which copy of the reporting
    hook wrote this row. Read them BEFORE concluding anything from an absent
    cache field: several hook copies can be registered at once and only the
    latest write survives here, so `null` cache state next to a stale or
    absent reporter means "a stale copy served this tick", which is a
    different fact — and a different fix — from "the verbs are undeployed".

    `agent_session_id` is the session's stable id, for callers that need to
    REACH the session rather than merely describe it. `null` means the
    reporter predates the column, NOT that the session has no bridge. Those
    must not be collapsed: the first is a coverage gap that heals on the next
    deploy, the second would be a live routing failure worth paging someone
    about.
    """
    if not agent_instance_id.strip():
        raise VerbError(
            "missing_argument", "session_context_status requires a non-empty agent_instance_id.",
        )
    try:
        row, id_resolution = resolve_status_row(state, agent_instance_id)
    except AmbiguousAgentSessionIdError as exc:
        raise VerbError("ambiguous_agent_session_id", str(exc)) from exc
    if row is None:
        return _unresolved_status(agent_instance_id, id_resolution)
    current_tokens, ceiling, fraction = _measurement(row)
    # The economics view remains separately visible, but ``rotation_due`` is
    # the single durability-notice decision used by every delivery surface.
    # It deliberately prefers this row's runtime-reported window over catalog
    # fallback, so a readback cannot disagree with the notice it explains.
    cache_view = _cache_view(row, current_tokens)
    rotation_due = _rotation_notice_due(
        row, current_tokens=current_tokens, ceiling=ceiling,
    )
    return {
        "resolved": True,
        "resolution_error": None,
        **_identity_view(row, agent_instance_id, id_resolution),
        "runtime_session_id": str(row.get("claude_session_id") or ""),
        "provider": str(row.get("provider") or ""),
        "runtime": str(row.get("runtime") or ""),
        "model": str(row.get("model") or ""),
        "effort": str(row.get("effort") or ""),
        "current_tokens": current_tokens,
        "ceiling": ceiling,
        "fraction": fraction,
        "per_prompt_carriage_estimate_tokens": round(current_tokens * CACHE_READ_COST_FRACTION),
        "rotation_due": rotation_due,
        "measured_at": str(row.get("measured_at") or ""),
        **_routing_view(row),
        **cache_view,
        **_reporter_view(row),
        "calculated_verdict": _calculated_verdict(
            row=row,
            current_tokens=current_tokens,
            ceiling=ceiling,
            cache_cold=cache_view["cache_cold"],
            request=calculation_request,
        ),
    }


__all__ = [
    "CACHE_READ_COST_FRACTION",
    "ID_RESOLUTION_DIRECT",
    "ID_RESOLUTION_UNRESOLVED",
    "ID_RESOLUTION_VIA_BINDING",
    "REPORTER_SURFACES",
    "REPORTER_SURFACE_CHECKOUT",
    "REPORTER_SURFACE_PLUGIN_CACHE",
    "REPORTER_SURFACE_RELEASE",
    "REPORTER_SURFACE_UNKNOWN",
    "REPORTER_SURFACE_VENDORED",
    "report_context_status",
    "resolve_status_row",
    "session_context_status",
]
