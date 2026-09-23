"""Declared context-window ceilings for the rotation-due trigger (P2 slice B,
ruling 3, P1 ratification 2026-08-07 --
`workbench/2026-08-07_rotation_systematization_findings_rotation-impl.md`).

**Populated 2026-08-07** (seat-supplied at package review, per ruling 3's
"ship empty-or-conservative, confirm at review" contract) -- any model NOT
listed below (e.g. a future release not yet added here) still falls back to
``DEFAULT_CONSERVATIVE_CEILING``, which any threshold check must treat as
the fail-safe floor (a conservative UNDER-estimate, so an unrecognized model
reports "rotation due" EARLIER than it would with its real, larger ceiling
-- never later).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


# Model identifier -> total context-window token ceiling. Populated 2026-08-07
# (seat-supplied, ruling 3 review). Citable sources:
#   1. Anthropic model catalog: platform.claude.com/docs/en/about-claude/models/overview.md
#   2. claude-api reference skill's catalog, cached 2026-06-24
#   3. Runtime-confirmable via the Models API's `max_input_tokens` field
# Key format: the transcript `message.model` field carries the BARE ALIAS (not a
# dated full-ID) on this fleet today -- seat-measured directly against 4 recent
# transcripts (2 seat-class sessions on claude-fable-5, 2 worker-class sessions on
# claude-sonnet-5), all exact on the alias with no dated suffix. A dated full-ID
# form is included defensively for claude-haiku-4-5, whose alias can also resolve
# that way. Any model not listed here falls through to DEFAULT_CONSERVATIVE_CEILING
# below -- never a guess.
# Corroborating field evidence: the 2026-08-04 operator-flagged "774k ≈ 77% context"
# catch is arithmetically consistent with a 1,000,000-token window -- the live
# fleet was already operating against these exact ceilings before this table
# existed to name them.
@dataclass(frozen=True)
class NoticeModelWindow:
    """One static fallback window, with its source kept beside the value."""

    window_tokens: int
    provenance: str


# ONE DECLARATIVE NOTICE POLICY BLOCK (operator ruling 2026-08-29).
# Runtime-reported windows win whenever the reporter supplies one. These rows
# are static fallbacks only; they deliberately contain no Codex entry because
# its catalog maximum and effective runtime window are different quantities.
# The key reserves the second slot for a future effort-specific override.
ROTATION_NOTICE_FRACTION: float = 0.80
COMPACTION_RESERVE_FRACTION: float = 0.10
DEFAULT_HARD_CLEAR_TOKENS: int = 400_000
NOTICE_MODEL_WINDOWS: dict[tuple[str, str | None], NoticeModelWindow] = {
    ("claude-fable-5", None): NoticeModelWindow(
        1_000_000, "Anthropic model catalog; tracked 2026-08-07",
    ),
    ("claude-sonnet-5", None): NoticeModelWindow(
        1_000_000, "Anthropic model catalog; tracked 2026-08-07",
    ),
    ("claude-opus-5", None): NoticeModelWindow(
        1_000_000, "Anthropic model catalog; tracked 2026-08-07",
    ),
    ("claude-haiku-4-5", None): NoticeModelWindow(
        200_000, "Anthropic model catalog; tracked 2026-08-07",
    ),
    ("claude-haiku-4-5-20251001", None): NoticeModelWindow(
        200_000, "Anthropic model catalog; tracked 2026-08-07",
    ),
}

# Legacy capacity callers retain this public map, derived from the declarative
# notice table so a static window is never defined twice.
MODEL_CONTEXT_CEILINGS: dict[str, int] = {
    model: entry.window_tokens
    for (model, effort), entry in NOTICE_MODEL_WINDOWS.items()
    if effort is None
}

# Conservative fallback for any model NOT present in MODEL_CONTEXT_CEILINGS
# above (e.g. a future release not yet added). Chosen deliberately small so
# an unrecognized model trips the rotation-due threshold EARLY rather than
# late -- a false-early rotation-due signal is far cheaper than a missed one
# (the brief's own 2026-08-04 catch: 77% context before anyone noticed).
# Unchanged by the 2026-08-07 ceiling-table population (seat's own
# instruction) -- not itself a measured per-model value, a deliberate floor.
DEFAULT_CONSERVATIVE_CEILING: int = 100_000

# Fraction of the resolved ceiling (MODEL_CONTEXT_CEILINGS[model] or the
# DEFAULT_CONSERVATIVE_CEILING fallback) at which the rotation-due hook
# (P2 slice B) reports to the steward. 0.5 per the brief's own standing
# framing ("first slice boundary past ~50% of context").
#
# SINCE GAU-08 (2026-08-18) THIS IS ONE TERM OF TWO, not the whole predicate.
# `is_rotation_due` is the UNION of this fraction and an actionable economics
# band -- see `is_rotation_due_for_ceiling` for why neither half alone is
# correct. This term is what keeps the predicate reachable on a SMALL-ceiling
# model, whose window the model-blind bands do not fit; it is not vestigial
# and removing it makes rotation-due strictly later on every such model.
ROTATION_THRESHOLD_FRACTION: float = 0.5

# ---------------------------------------------------------------------------
# ECONOMIC ROTATION POLICY (operator-ratified 2026-08-16). These supersede the
# fraction above AS POLICY. ROTATION_THRESHOLD_FRACTION is retained as the
# coarse `rotation_due` HINT the verb has always emitted -- it is not the
# policy, and the context-gauge card says so explicitly.
#
# The bands are ABSOLUTE token counts, not fractions of a ceiling, because the
# cost being managed is the carriage re-read on every model CALL -- which is a
# function of how many tokens are in the window, not of how full the window is.
# The unit is the MODEL CALL, not the operator prompt: the superseded
# "~80k token-equivalents per prompt" arithmetic priced the wrong event.
# THESE BANDS ARE TIER-INVARIANT, AND THE PARAGRAPH THAT USED TO STAND HERE
# SAID THE OPPOSITE. Recording the withdrawn reasoning rather than only the
# conclusion, because a comment that states just the new answer invites the
# next reader to "fix" it back to the old one.
#
# WHAT IT USED TO SAY (2026-08-16 -> 2026-08-17): that the bands were
# MODEL-BLIND, derived from Fable-tier economics, and that a cheaper tier
# carrying the same context for roughly an order of magnitude less meant the
# bands overstated urgency for it -- citing a live observation, a sonnet
# session at 224,840 tokens read as past-200K-rotate-now. It named two fixes
# (scale the bands by tier, or declare them seat-tier policy) and shipped
# neither.
#
# THE OBSERVATION WAS REAL. THE INFERENCE FROM IT WAS WRONG. Two different
# questions were fused:
#   1. WOULD A CLEAR PAY FOR ITSELF?  -- TIER-INVARIANT.
#   2. HOW MANY DOLLARS ARE AT STAKE? -- tier-dependent.
# The bands answer (1), and they answer it correctly for every model. What
# read wrong on that sonnet session was the IMPERATIVE attached to the band
# ("rotate immediately"), which is a claim about (2). The tier-wrong thing was
# never the number; it was the adjective. See `rotation_band`'s guidance
# strings, which now state the break-even fact rather than an order.
#
# WHY (1) IS TIER-INVARIANT -- the derivation, in four lines. Let p be THIS
# model's base input price. Over the next N calls:
#     keep working: cost_A = N * C * CACHE_READ_MULTIPLIER * p
#     clear now:    cost_B = H * CACHE_WRITE_MULTIPLIER_1H * p
#                          + N * H * CACHE_READ_MULTIPLIER * p
#     cost_B < cost_A  <=>  2pH + 0.1pNH < 0.1pNC
#     divide by 0.1pN  <=>  C > H + 20H/N
# p CANCELS EXACTLY. That is the whole argument, and it is also where the 20
# comes from: CACHE_WRITE_PREMIUM_MULTIPLIER = 2.0 / 0.1, a RATIO OF TWO
# MULTIPLIERS, not a price. **A ratio of two multipliers on the same base
# price cannot carry a tier.** Vendor source for both multipliers, checked
# rather than recalled: cache reads cost ~0.1x base input price, cache writes
# 1.25x at 5-minute TTL and 2x at 1-hour TTL -- stated once, as multipliers on
# that model's own base input price, with no per-model variation.
#
# SO SCALING THE BANDS BY TIER WOULD BREAK THEM. It would have told that same
# sonnet session at 224,840 to keep working, suppressing a signal that was
# arithmetically correct -- a failure in the expensive direction, and the exact
# one this surface exists to prevent. A tier-scaling scheme must also classify
# every model into a price tier, so an unclassified model is guessed or
# defaulted, and a "cheap" default is silently quiet: a fail-OPEN mode that a
# scheme with no tiers cannot have.
#
# THE GENUINE MODEL-DEPENDENCE IS CAPACITY, NOT PRICE, and it is on its own
# axis -- see CAPACITY_BAND_* below. claude-haiku-4-5's ceiling is 200,000, so
# these three bands sit at 75% / 100% / 150% of its window and the 300K band is
# unreachable: the session runs out of room first. That is what a small-ceiling
# model needs protecting from, and it has nothing to do with what its tokens
# cost.
#
# THE ONE THING IN THE FORMULA THAT LEGITIMATELY VARIES IS CACHE TTL, not
# model -- see CACHE_WRITE_PREMIUM_MULTIPLIER_OVERAGE.
WARM_BAND_KEEP_WORKING_TOKENS: int = 150_000      # under this: keep working
WARM_BAND_TASK_BOUNDARY_TOKENS: int = 200_000     # 150-200K: next natural boundary
WARM_BAND_SAFE_CHECKPOINT_TOKENS: int = 300_000   # over 200K: first safe checkpoint
                                                  # over 300K: immediately

# ---------------------------------------------------------------------------
# CAPACITY BANDS (2026-08-17). The genuine model-dependence, on its own axis.
#
# The warm bands above ask "would a clear pay for itself" -- a question about
# ECONOMICS, answered in absolute tokens, identical on every model. These ask
# "is this session about to run out of room" -- a question about CAPACITY,
# answered as a fraction of the model's own declared ceiling. A session can be
# in trouble for either reason independently, so they are separate bands rather
# than one blended number, and the notice names which one fired.
#
# Chosen so that on a 1M-ceiling model capacity NEVER binds before economics:
# 0.75 of 1,000,000 is 750,000, far past the 300K point at which the warm bands
# already saturate at "rotate immediately". On claude-haiku-4-5 (ceiling
# 200,000) it binds at 150,000 / 180,000, which is exactly the range the warm
# bands cannot serve -- their 300K step is unreachable inside a 200K window.
#
# FAIL-SAFE COMES FREE, and in the right direction. An unrecognised model
# already resolves to DEFAULT_CONSERVATIVE_CEILING = 100,000 (see
# `resolve_ceiling`), so it trips capacity_approaching at 75,000 -- EARLIER
# than any warm band. An unknown model is therefore loud, never quiet. This is
# the same discipline `resolve_ceiling` states for itself, and it is the reason
# capacity is expressed as a fraction while economics is expressed in absolute
# tokens: a fraction inherits the conservative ceiling automatically.
CAPACITY_BAND_APPROACHING_FRACTION: float = 0.75
CAPACITY_BAND_CRITICAL_FRACTION: float = 0.90

# H -- the post-rotation prefix a `/clear` re-writes, and the quantity the
# cold-cache rule compares against. H is boot payload PLUS incremental
# rehydration: the boot payload is re-paid on every clear, so it belongs
# inside H rather than beside it.
#
# ★ H IS A DISTRIBUTION, NOT A CONSTANT, and the single number below is a
# POLICY SIMPLIFICATION of it. Do not read the digits as precision. Nine
# measured pickups span 28,294-58,513 on the rehydration half -- a 2.07x
# spread -- because rehydration is set by how busy the pickup WINDOW was, not
# by anything the boot surface or this repo fixes. A reader who treats 81,889
# as "the" restart cost of any particular session will be wrong by tens of
# thousands of tokens in either direction; what it is, is the value that makes
# the fleet-wide rotation decision come out right most often.
#
# ---------------------------------------------------------------------------
# THE REHYDRATION HALF -- RE-MEASURED 2026-08-19 (bug-wave lane E), 102,665 ->
# 38,415. This is the file's OWN shelf-life trigger firing exactly as written
# (see below): the seat pickup prompt was replaced wholesale by the
# pointer-card protocol, and 102,665 prices the PRE-pointer-card pickup.
# Source data: the SURFACE-1 RESULT entries in the 2026-08-19 H-reduction
# link-first charter under workbench/, nine live link-first seat pickups,
# v21 through v29, all on 2026-08-19:
#
#   v21 36,999 · v22 40,573 · v23 58,513 · v24 38,415 · v25 43,586
#   v26 28,294 · v27 35,435 · v28 33,302 · v29 54,943 (see the datum-#9 note)
#
#   sorted: 28,294 · 33,302 · 35,435 · 36,999 · [38,415] · 40,573 · 43,586
#           · 54,943 · 58,513          MEDIAN 38,415   mean 41,118
#
# WHY THE MEDIAN, and not the mean. Two of the nine are self-labelled UPPER
# BOUNDS by their own authors: #7 took two substantive lane messages inside the
# pickup window ("live-work tokens no rehydration design removes"), and #9
# disclosed an over-read queue file plus a doubled inbox pull. The mean absorbs
# that contamination; the median is robust to it. The claim is not merely
# asserted here, it is DEMONSTRATED: datum #9 was filed as 54,943 and is
# corrected to ~60,533 (its reported "H_boot" was the first gauge sample of the
# session, already carrying the pickup prompt, not a controlled boot reading).
# That correction moves the mean 41,118 -> 41,739 and moves the median NOT AT
# ALL. Same discipline, same reason, as the 2026-08-16/19 measurements that
# preceded this one -- "the median rather than the max".
#
# WHY ERR LOW, which is the tie-break and is asymmetric in the code below, not
# just in argument. H is a FLOOR: `rotation_band` rotates on
# `current_tokens > POLICY_H_TOKENS`, and `clearing_wins` returns False on the
# `<= H` comparison alone, for any N. So OVERSTATING H raises the bar to
# rotate, and the excess context a session then keeps carrying is re-read at
# the cache-read price on EVERY SUBSEQUENT CALL -- an unbounded cost.
# UNDERSTATING H clears a session that marginally should not have cleared --
# one bounded, one-time H. The old value erred the unbounded way, which is why
# sessions sat fat. The median (38,415) is below the mean (41,118), so both
# arguments point the same direction and there is no tie left to break.
#
# ★ THE POPULATION IS THE SEAT-PICKUP CLASS, BY INHERITANCE, AND LANE
# REHYDRATION HAS NEVER BEEN MEASURED. Stated because the constant governs
# rotation economics for EVERY session and the data behind it does not.
# All nine measurements above are SEAT pickups under the pointer-card
# protocol. That is not a narrowing introduced here: the value being replaced
# was itself the median of THREE seat pickups (v17/v18/v19), chosen after its
# author explicitly set aside the other eleven rows of a fourteen-row survey.
# Same class, same measurement point, three times the sample -- which is what
# makes this an apples-to-apples re-measurement rather than a redefinition.
#
# The survey rows that were set aside are NOT a competing estimate of H, and
# should not be resurrected as one. They record tokens accumulated by the time
# a fresh session reached steady state -- for the largest of them, 243, 352
# and 390 calls of actual lane WORK. H is a floor, not a session total: in
# `C > H + kH/N` both C and H are context sizes at the SAME MOMENT, so work not
# yet done sits on both sides and cancels, exactly as the price-per-token does.
# Those rows are the right instrument for aiming the rehydration diet, which is
# what their author used them for, and the wrong quantity for this constant.
#
# WHAT IS GENUINELY UNKNOWN, recorded rather than papered over: what a LANE
# re-reads after a clear. The only lane-side rehydration figures anyone has
# taken are bootstrap rows at H_rehyd = 0 (one or two calls in), and a lane's
# real post-clear cost is boot plus its brief -- plausibly well under a seat's,
# never measured. So the seat-derived figure most likely OVERSTATES for lanes,
# which is the same direction of error the correction above reduces. Do not
# read that as a measurement. Anyone who takes one should re-open this comment.
#
# NO TREND, so "just use the latest" is not available. Read chronologically
# v21..v29 the series oscillates rather than drifts -- it tracks pickup
# workload, not calendar time. That is what makes the distribution framing
# above honest rather than a hedge.
#
# ---------------------------------------------------------------------------
# THE BOOT HALF -- 43,474, measured 2026-08-19 (GAU-05,
# workbench/2026-08-19_gau05_policy_h_remeasurement_lane_gau_notice.md),
# DELIBERATELY UNCHANGED by the 2026-08-19 re-measurement above. It is the
# fable-5 seat class. Specimen, so the number is checkable rather than quoted:
# seat session `c36fb2e4`, boot 2026-08-19T00:45:17Z, claude-fable-5, v19
# pickup prompt. THE INSTRUMENT WAS CONTROLLED, not just re-run: the same
# scanner re-applied to the 2026-08-16 transcript reproduces that measurement
# EXACTLY -- 42,873 and 110,702, same model, same line numbers. A future
# re-measurement that cannot reproduce those two numbers has a different
# instrument and its result is not comparable.
#
# ★ THIS CONSTANT HAS A SHELF LIFE, AND THE TWO HALVES EXPIRE FOR DIFFERENT
# REASONS -- which is why they are recorded separately and must stay that way.
# The trigger once read "when CLAUDE.md, MEMORY.md or the hook set changes
# materially". Measurement says those three moved H by 1.4% in three days
# while the SEAT PICKUP PROMPT moved it by 31%, and then by -63% when the
# pointer card replaced it. The boot component is what the hook set and the
# instruction files determine; the rehydration component is what the pickup
# turn's own workload determines, it is the dominant term, and it changes every
# seat generation. So: re-measure when CLAUDE.md, MEMORY.md, the hook set OR
# THE SEAT PICKUP PROMPT changes materially -- and record the two components
# separately, because a single-number re-measurement cannot tell which one
# moved and will mis-attribute the drift, as the GAU-05 backlog entry did.
#
# ★ ONE H, AND IT IS NOT UNIFORM ACROSS TIERS -- stated because it is measured,
# not fixed here. `POLICY_H_BOOT_TOKENS` varies by MODEL: on byte-identical
# input (same prompt, same hooks, same instruction files, four lanes inside 45
# seconds) fable-5 measured 43,474, opus-5 44,723/44,720 and sonnet-5
# 53,941/53,947 -- single-digit spread within a tier, 9,218 tokens between
# them, which is the vendor's own system prompt and tool schemas rather than
# anything this repo controls. Making H per-tier moves a ratified policy input,
# so it is an OPERATOR decision (GAU-14 axis 2, candidate B3). It was put to
# the operator on 2026-08-19 and explicitly DEFERRED -- "not now, just fix with
# the data you already have" -- which is the scope of the re-measurement above
# and the reason the boot half is untouched. Do not take it here.
#
# ★ WHAT THIS RE-MEASUREMENT MOVED DOWNSTREAM, named because the asserts that
# guard these went on passing and only their MARGINS changed:
#   · `WARM_BAND_KEEP_WORKING_TOKENS - H` widens 3,861 -> 68,111. The
#     cold-vs-warm discriminating window is no longer nearly closed; the
#     "born 3,861 tokens under the first band" argument that motivated the
#     GAU-14 (B2) floor line is NO LONGER TRUE and that line now rests on the
#     weaker ground that a reader cannot decompose the number unaided.
#   · The gap between H and the 75,000 point where an unrecognised model trips
#     `capacity_approaching` narrows 71,139 -> 6,889, and that fence now decides
#     whether the below-H branch of the rotation self-notice is reachable at
#     all. The QUIETEST of the nine pickups above (v26, 28,294) already implies
#     an H of 71,768, i.e. BELOW 75,000. Further success at reducing
#     rehydration -- which is an active programme, not a hypothetical -- closes
#     that branch. `rotation_self_notice_smoke.py` carries an explicit margin
#     guard that reds loudly when it inverts, rather than letting it pass.
POLICY_H_BOOT_TOKENS: int = 43_474
POLICY_H_REHYDRATION_TOKENS: int = 38_415
POLICY_H_TOKENS: int = POLICY_H_BOOT_TOKENS + POLICY_H_REHYDRATION_TOKENS  # 81,889

# The two vendor cache multipliers the break-even is built from. Both are
# multipliers on THAT MODEL'S OWN base input price, uniform across models --
# which is precisely why the ratio below carries no tier. Sourced from the
# vendor's prompt-caching pricing reference (checked 2026-08-17, not recalled):
# "Cache reads cost ~0.1x base input price. Cache writes cost 1.25x for
# 5-minute TTL, 2x for 1-hour TTL."
#
# They are named constants rather than inlined digits so the premium below is
# visibly a QUOTIENT OF TWO SOURCED FACTS rather than a magic number. The old
# bare `20` was correct and unexplainable; a reader had no way to tell whether
# it was measured, ratified, or guessed.
CACHE_READ_MULTIPLIER: float = 0.1
CACHE_WRITE_MULTIPLIER_1H: float = 2.0
CACHE_WRITE_MULTIPLIER_5M: float = 1.25

# Break-even multiplier for `C > H + (CACHE_WRITE_PREMIUM_MULTIPLIER * H) / N`.
# A clear re-writes the whole prefix at the 1-hour cache-write premium, and that
# write is amortised over the N calls that follow it -- so a rotation pays for
# itself only when the carried context C is large enough relative to what the
# rewrite costs. = CACHE_WRITE_MULTIPLIER_1H / CACHE_READ_MULTIPLIER = 20.
#
# Kept as an int rather than computed from the two floats above: it is a
# ratified policy input, and 2.0/0.1 in IEEE-754 is 19.999999999999996, which
# would move every derived threshold by a few tokens for no reason anyone could
# later explain. The equality is asserted at import instead (see below), so the
# constant cannot drift out of agreement with its own derivation silently.
CACHE_WRITE_PREMIUM_MULTIPLIER: int = 20

# THE SAME BREAK-EVEN UNDER A COLLAPSED CACHE TTL. This is the ONE quantity in
# the formula that legitimately varies -- and it varies with cache TTL, never
# with model tier.
#
#   1-hour TTL:   write 2.0x  / read 0.1x  -> 20
#   5-minute TTL: write 1.25x / read 0.1x  -> 12.5
#
# Same cancellation, different ratio. A LOWER multiplier means a LOWER
# threshold, so under a collapsed TTL clearing wins SOONER, not later: at
# N = 25 the break-even is ~166,000 rather than ~199,000.
#
# This is not hypothetical. The prompt-cache TTL collapses to ~5 minutes while
# an account is in usage overage (OVERAGE_TTL_SECONDS below), and
# `classify_cache_state` ALREADY DETECTS THAT and already returns
# `overage_signature` as a field distinct from `cold`. The measurement existed
# and nothing consumed it for this purpose until 2026-08-17.
CACHE_WRITE_PREMIUM_MULTIPLIER_OVERAGE: float = (
    CACHE_WRITE_MULTIPLIER_5M / CACHE_READ_MULTIPLIER
)

# Prompt-cache lifetimes. The policy's words are "idle" and "in usage overage";
# the code measures CACHE EXPIRY, because overage MANIFESTS as cache expiry and
# the account state itself is not observable to this platform (searched
# 2026-08-16: no signal found -- which is not the same claim as none existing).
CACHE_TTL_SECONDS: int = 3_600        # nominal 1-hour prompt-cache TTL
OVERAGE_TTL_SECONDS: int = 300        # TTL collapses to ~5 min while in overage
OVERAGE_MIN_COLD_CALLS: int = 2       # "repeated", never a single cold call

# P4 -- idle-boundary seat watcher thresholds (P4(a) ratification, condition 4,
# 2026-08-07). The watcher polls the seat's idle state out-of-process; these
# four constants are its only tunable surface.
#
# Early-vs-late rotation trade-off (documentation REQUIRED by ratification
# condition 4, not optional commentary): IDLE_ROTATE_THRESHOLD_SECONDS fires
# BEFORE the measured ~45-60 minute prompt-cache TTL, not at or after it.
#   - Rotating EARLY (pre-expiry) spends some of the still-warm cache on a
#     rotation that technically didn't NEED to happen yet -- the session was
#     still inside its warm window. The payoff: it GUARANTEES that any poke
#     fired after this point lands in a freshly-cleared, cheap context rather
#     than risking a poke landing just past the expiry boundary and paying a
#     full cold re-warm on the very message that was time-sensitive enough to
#     need a poke in the first place.
#   - Rotating LATE (at/after expiry) preserves the warm window for longer
#     (more turns before any rotation cost is paid at all), but risks exactly
#     the 2026-08-07 incident this watcher exists to prevent: a poke or an
#     operator turn arriving just after expiry pays the full re-warm cost
#     regardless, with no advance warning and no cheaper alternative available.
# The chosen threshold (2100s = 35 min) trades a small amount of warm-window
# time for eliminating that late-poke worst case -- deliberate, not a rounding
# choice. See the per-constant comment below for the specific margin math.
WATCH_POLL_INTERVAL_SECONDS = 60
"""How often the watcher wakes to check idle state. Cheap per tick (one
`ps eww`, one file stat, occasionally one iTerm2 read or one `peer_inbox`
call) -- 60s balances promptness against the watcher's own steady-state cost,
never spending a model/inference call at any interval."""

IDLE_POKE_THRESHOLD_SECONDS = 300
"""Idle duration (5 min) past which, IF the seat's role inbox has pending
entries, the watcher injects a short wake turn (poke) rather than waiting for
the rotate threshold. Deliberately much shorter than the rotate threshold --
draining real pending mail promptly is the point of the poke path; a poke
also naturally resets the idle clock, deferring the rotate question."""

IDLE_ROTATE_THRESHOLD_SECONDS = 2100
"""Idle duration (35 min) past which the watcher fires the full rotation
helper, with no pending-inbox precondition. Margined below the fleet's own
measured ~45-60 minute prompt-cache TTL (memory:
no-session-idles-past-cache-expiry) by roughly a 10-minute floor under the
conservative 45-minute figure -- room for the watcher's own poll latency
(<=WATCH_POLL_INTERVAL_SECONDS) plus the rotation sequence's own worst-case
run time (settle timeout up to 120s + the submit-confirm step), so the
rotation completes and lands the seat in a fresh session BEFORE the cache
would have expired anyway. See the trade-off note above for why firing
before expiry, not at it, is the entire point of this threshold existing at
all. Not live-measured against a real cache-expiry event this pass --
proposed with rationale (P4(a).4), ratified as-is (condition 4)."""

POKE_COOLDOWN_SECONDS = 1800
"""After a poke fires, the watcher holds off re-poking for 30 min even if the
inbox still shows pending entries -- prevents spamming a seat that is idle
for a reason (e.g. the operator stepped away) with repeated pokes for the
same still-unread mail. A successful poke that produces activity naturally
resets the idle clock anyway; this cooldown covers the case where the poke
lands but the seat stays idle regardless."""


def resolve_ceiling(model: str) -> int:
    """The declared ceiling for ``model``, or the conservative fallback when
    ``model`` has no entry in :data:`MODEL_CONTEXT_CEILINGS` -- e.g. a future
    model release not yet added here."""
    return MODEL_CONTEXT_CEILINGS.get(model, DEFAULT_CONSERVATIVE_CEILING)


@dataclass(frozen=True)
class RotationNoticeInput:
    """All evidence used by the pure, swappable notice decision."""

    model: str
    effort: str
    window_tokens: int | None
    current_tokens: int
    configured_hard_clear_tokens: int | None
    window_source: str


@dataclass(frozen=True)
class RotationNoticeDecision:
    """The one notice point and the provenance needed to explain it later."""

    due: bool
    notice_at_tokens: int
    limiting_tokens: int
    hard_clear_tokens: int
    window_tokens: int | None
    window_source: str
    unrecognized_model: bool


def configured_hard_clear_tokens() -> int:
    """Read the operator-owned hard-clear knob, or its declared fallback.

    This is deliberately outside :func:`rotation_notice_decision`: config I/O
    is an adapter concern, while the decision itself receives explicit facts.
    """
    policy_path = Path.home() / ".claude" / "seat_hooks" / "rotation_policy.json"
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_HARD_CLEAR_TOKENS
    value = raw.get("hard_threshold_tokens") if isinstance(raw, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_HARD_CLEAR_TOKENS


def resolve_notice_window(
    *, model: str, effort: str, runtime_window_tokens: int | None,
) -> tuple[int | None, str, bool]:
    """Prefer a runtime window; otherwise return a sourced static fallback.

    An absent static row remains absent rather than becoming a guessed window.
    The decision can still use the hard-clear bound and reports that fact.
    """
    if runtime_window_tokens is not None:
        if runtime_window_tokens <= 0:
            raise ValueError(
                f"runtime_window_tokens must be positive, got {runtime_window_tokens}",
            )
        return runtime_window_tokens, "runtime-reported", False
    entry = NOTICE_MODEL_WINDOWS.get((model, effort)) or NOTICE_MODEL_WINDOWS.get(
        (model, None),
    )
    if entry is None:
        return None, "unrecognized-model", True
    return entry.window_tokens, entry.provenance, False


def rotation_notice_decision(input: RotationNoticeInput) -> RotationNoticeDecision:
    """Return the single 80%-of-first-ending-bound notice decision.

    This strategy entry point performs no environment, file, or module-global
    read. Runtime/window lookup and hard-clear loading happen at the adapters;
    a later strategy registry can therefore replace this calculation without
    changing its inputs or callers.
    """
    if input.current_tokens < 0:
        raise ValueError(f"current_tokens must be non-negative, got {input.current_tokens}")
    hard_clear = input.configured_hard_clear_tokens
    if hard_clear is None:
        raise ValueError("configured_hard_clear_tokens must be supplied explicitly")
    if hard_clear <= 0:
        raise ValueError(f"hard clear must be positive, got {hard_clear}")
    limiting_tokens = hard_clear
    if input.window_tokens is not None:
        if input.window_tokens <= 0:
            raise ValueError(f"window_tokens must be positive, got {input.window_tokens}")
        compaction_point = int(input.window_tokens * (1 - COMPACTION_RESERVE_FRACTION))
        limiting_tokens = min(hard_clear, compaction_point)
    notice_at = int(limiting_tokens * ROTATION_NOTICE_FRACTION)
    return RotationNoticeDecision(
        due=input.current_tokens >= notice_at,
        notice_at_tokens=notice_at,
        limiting_tokens=limiting_tokens,
        hard_clear_tokens=hard_clear,
        window_tokens=input.window_tokens,
        window_source=input.window_source,
        unrecognized_model=input.window_source == "unrecognized-model",
    )


def rotation_notice_verdict(
    *, model: str, effort: str, current_tokens: int,
    runtime_window_tokens: int | None, configured_hard_clear_tokens: int | None = None,
) -> RotationNoticeDecision:
    """Resolve notice evidence then call the pure strategy entry point."""
    window, source, unrecognized = resolve_notice_window(
        model=model, effort=effort, runtime_window_tokens=runtime_window_tokens,
    )
    decision = rotation_notice_decision(RotationNoticeInput(
        model=model,
        effort=effort,
        window_tokens=window,
        current_tokens=current_tokens,
        configured_hard_clear_tokens=(
            configured_hard_clear_tokens
            if configured_hard_clear_tokens is not None
            else configured_hard_clear_tokens_from_runtime()
        ),
        window_source=source,
    ))
    if decision.unrecognized_model != unrecognized:
        raise ValueError("notice window recognition changed during decision")
    return decision


def configured_hard_clear_tokens_from_runtime() -> int:
    """Named adapter seam for callers that need the live configured knob."""
    return configured_hard_clear_tokens()


def is_rotation_due(
    *, model: str, current_tokens: int, cache_cold: bool | None = None,
) -> bool:
    """``True`` when either rotation axis says rotate, against ``model``'s
    resolved ceiling (:func:`resolve_ceiling`).

    The model-taking entry point onto :func:`is_rotation_due_for_ceiling`,
    which carries the definition and the whole of the reasoning. Use this form
    when a model name is all you hold -- the P2 slice B hook measures a live
    transcript and has no stored row to read a ceiling from. A caller that
    ALREADY has a ceiling (any stored gauge row) must call the core directly,
    so that its decision and any fraction it goes on to print are computed
    against the same denominator.

    Pure comparison -- ``current_tokens`` and ``cache_cold`` both come from the
    caller's own measurement; this function makes no I/O call of its own.

    ``cache_cold`` DEFAULTS TO ``None``, and the default is not a convenience.
    ``None`` is a real value in this domain -- "nobody measured the cache
    state" -- and it is exactly what an omitted argument means, so the default
    states a fact rather than substituting for one. That matters because this
    function has callers THIS REPOSITORY CANNOT EDIT: the rotation hook exists
    in a third, installed plugin-cache copy (GAU-04) which resolves
    ``rotation_thresholds`` out of the live checkout while carrying its own
    frozen call site. Measured 2026-08-18: seven such copies are on this
    machine, the marketplace plugin is enabled in the user's settings, and
    every one of them calls ``is_rotation_due(model=..., current_tokens=...)``.
    A required keyword would have turned each of their ticks into a TypeError
    -- a live break in a copy no landing here can reach.

    So the default is also what carries the fix ACROSS that boundary: a frozen
    copy that cannot be updated still gets the union, because the union lives
    behind the signature it already calls. It gets the band half and the
    conservative warm reading of a cache state it never sends, which is
    strictly better than the fraction-only answer it gets today.
    """
    return is_rotation_due_for_ceiling(
        ceiling=resolve_ceiling(model),
        current_tokens=current_tokens,
        cache_cold=cache_cold,
    )


__all__ = [
    "ACTIONABLE_BAND_RANK",
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER_1H",
    "CACHE_WRITE_MULTIPLIER_5M",
    "CACHE_WRITE_PREMIUM_MULTIPLIER",
    "CACHE_WRITE_PREMIUM_MULTIPLIER_OVERAGE",
    "CAPACITY_BAND_APPROACHING_FRACTION",
    "CAPACITY_BAND_CRITICAL_FRACTION",
    "DEFAULT_CONSERVATIVE_CEILING",
    "RotationDueVerdict",
    "CalculatedActionEvaluation",
    "CalculatedContextVerdict",
    "RotationActionCalibration",
    "RotationVerdict",
    "band_is_actionable",
    "break_even_horizon",
    "capacity_band",
    "calculated_context_verdict",
    "rotation_due_verdict",
    "rotation_surface_verdict",
    "write_premium_multiplier",
    "IDLE_POKE_THRESHOLD_SECONDS",
    "IDLE_ROTATE_THRESHOLD_SECONDS",
    "MODEL_CONTEXT_CEILINGS",
    "POKE_COOLDOWN_SECONDS",
    "ROTATION_THRESHOLD_FRACTION",
    "WATCH_POLL_INTERVAL_SECONDS",
    "is_rotation_due",
    "is_rotation_due_for_ceiling",
    "resolve_ceiling",
]


@dataclass(frozen=True)
class RotationActionCalibration:
    """Measured action inputs in base-input-token-equivalent cost units.

    ``post_action_prefix_tokens`` is H. ``one_time_cost_units`` is the measured
    action cost after provider-specific output/cache-write prices have been
    normalized by the selected economics profile. Keeping that normalization
    outside this module prevents a model-price branch from leaking into the
    core keep/compact/clear comparison.
    """

    action: str
    post_action_prefix_tokens: int
    one_time_cost_units: float
    calibration_profile_id: str
    calibration_profile_version: str


@dataclass(frozen=True)
class CalculatedActionEvaluation:
    action: str
    post_action_prefix_tokens: int
    one_time_cost_units: float
    projected_total_cost_units: float
    break_even_horizon_calls: float | None
    calibration_profile_id: str
    calibration_profile_version: str


@dataclass(frozen=True)
class CalculatedContextVerdict:
    """Truthful N-aware keep/compact/clear answer with its evidence boundary."""

    resolved: bool
    chosen_action: str | None
    economic_choice: str | None
    economic_cause: str
    capacity_band: str
    capacity_cause: str
    current_tokens: int
    ceiling: int
    expected_calls_after: int
    cache_state: str
    cache_read_multiplier: float | None
    keep_projected_cost_units: float | None
    action_evaluations: tuple[CalculatedActionEvaluation, ...]
    capability_profile_id: str
    capability_profile_version: str
    usage_economics_profile_id: str
    usage_economics_profile_version: str
    objective: str


def _validate_calculation_inputs(
    *,
    current_tokens: int,
    ceiling: int,
    expected_calls_after: int,
    required_actions: tuple[str, ...],
    identity_fields: dict[str, str],
) -> None:
    if current_tokens < 0:
        raise ValueError(f"current_tokens must be non-negative, got {current_tokens}")
    if ceiling <= 0:
        raise ValueError(f"ceiling must be positive, got {ceiling}")
    if expected_calls_after < 0:
        raise ValueError(
            f"expected_calls_after must be non-negative, got {expected_calls_after}",
        )
    blank = sorted(name for name, value in identity_fields.items() if not value.strip())
    if blank:
        raise ValueError(f"calculated verdict requires profile identity: {', '.join(blank)}")
    if not required_actions or len(required_actions) != len(set(required_actions)):
        raise ValueError("required_actions must be a non-empty unique tuple")


def _action_evaluation(
    calibration: RotationActionCalibration,
    *,
    current_tokens: int,
    ceiling: int,
    expected_calls_after: int,
    cache_read_multiplier: float,
) -> CalculatedActionEvaluation:
    action = calibration.action
    if calibration.post_action_prefix_tokens <= 0:
        raise ValueError(
            f"{action}.post_action_prefix_tokens must be positive, got "
            f"{calibration.post_action_prefix_tokens}",
        )
    if calibration.one_time_cost_units < 0:
        raise ValueError(
            f"{action}.one_time_cost_units must be non-negative, got "
            f"{calibration.one_time_cost_units}",
        )
    if calibration.post_action_prefix_tokens >= ceiling:
        raise ValueError(
            f"{action}.post_action_prefix_tokens must be below ceiling {ceiling}",
        )
    if (
        not calibration.calibration_profile_id.strip()
        or not calibration.calibration_profile_version.strip()
    ):
        raise ValueError(f"{action} calibration needs profile id and version")
    recurring = (
        expected_calls_after
        * calibration.post_action_prefix_tokens
        * cache_read_multiplier
    )
    denominator = (
        (current_tokens - calibration.post_action_prefix_tokens)
        * cache_read_multiplier
    )
    horizon = (
        calibration.one_time_cost_units / denominator
        if denominator > 0
        else None
    )
    return CalculatedActionEvaluation(
        action=action,
        post_action_prefix_tokens=calibration.post_action_prefix_tokens,
        one_time_cost_units=calibration.one_time_cost_units,
        projected_total_cost_units=calibration.one_time_cost_units + recurring,
        break_even_horizon_calls=horizon,
        calibration_profile_id=calibration.calibration_profile_id,
        calibration_profile_version=calibration.calibration_profile_version,
    )


def _calibrations_by_action(
    calibrations: tuple[RotationActionCalibration, ...],
) -> dict[str, RotationActionCalibration]:
    by_action = {calibration.action: calibration for calibration in calibrations}
    if len(by_action) != len(calibrations):
        raise ValueError("calibration actions must be unique")
    return by_action


def _choose_calculated_action(
    evaluations: list[CalculatedActionEvaluation],
    *,
    keep_cost: float,
    capacity_band_name: str,
) -> tuple[str, str]:
    economic_action = min(
        evaluations,
        key=lambda evaluation: (evaluation.projected_total_cost_units, evaluation.action),
    )
    economic_choice = (
        economic_action.action
        if economic_action.projected_total_cost_units < keep_cost
        else "keep"
    )
    chosen = (
        economic_choice
        if capacity_band_name != "capacity_critical"
        else economic_action.action
    )
    return economic_choice, chosen


def calculated_context_verdict(
    *,
    current_tokens: int,
    ceiling: int,
    expected_calls_after: int,
    cache_state: str,
    cache_read_multiplier: float | None,
    calibrations: tuple[RotationActionCalibration, ...],
    required_actions: tuple[str, ...],
    capability_profile_id: str,
    capability_profile_version: str,
    usage_economics_profile_id: str,
    usage_economics_profile_version: str,
    objective: str,
    evidence_error: str | None = None,
) -> CalculatedContextVerdict:
    """Calculate keep versus measured actions without runtime/model branches.

    For action ``a``, ``cost(a) = one_time(a) + N * H(a) * r`` and
    ``cost(keep) = N * C * r``.  The caller supplies provider-price-normalized
    one-time costs, so metered and quota strategies can project actions using
    their own objective before reaching this pure boundary.

    Missing cache evidence or any required action calibration is unresolved,
    never a warm-cache or zero-cost assumption. Capacity is evaluated
    independently: a critical/full window removes ``keep`` from the eligible
    set even when it is economically cheapest.
    """
    identity_fields = {
        "capability_profile_id": capability_profile_id,
        "capability_profile_version": capability_profile_version,
        "usage_economics_profile_id": usage_economics_profile_id,
        "usage_economics_profile_version": usage_economics_profile_version,
        "objective": objective,
    }
    _validate_calculation_inputs(
        current_tokens=current_tokens,
        ceiling=ceiling,
        expected_calls_after=expected_calls_after,
        required_actions=required_actions,
        identity_fields=identity_fields,
    )
    cap_band, cap_guidance = capacity_band(current_tokens, ceiling)

    def unresolved(cause: str, keep_cost: float | None) -> CalculatedContextVerdict:
        return CalculatedContextVerdict(
            resolved=False,
            chosen_action=None,
            economic_choice=None,
            economic_cause=cause,
            capacity_band=cap_band,
            capacity_cause=cap_guidance,
            current_tokens=current_tokens,
            ceiling=ceiling,
            expected_calls_after=expected_calls_after,
            cache_state=cache_state,
            cache_read_multiplier=cache_read_multiplier,
            keep_projected_cost_units=keep_cost,
            action_evaluations=(),
            capability_profile_id=capability_profile_id,
            capability_profile_version=capability_profile_version,
            usage_economics_profile_id=usage_economics_profile_id,
            usage_economics_profile_version=usage_economics_profile_version,
            objective=objective,
        )

    if evidence_error is not None:
        return unresolved(evidence_error, None)
    if cache_state not in {"observed_warm", "observed_cold"}:
        return unresolved(
            (
                f"cache state is {cache_state!r}; keep/action carriage cannot be "
                "priced without observed_warm or observed_cold evidence"
            ),
            None,
        )
    if cache_read_multiplier is None or cache_read_multiplier <= 0:
        return unresolved(
            "cache_read_multiplier is unknown or non-positive",
            None,
        )
    by_action = _calibrations_by_action(calibrations)
    missing = [action for action in required_actions if action not in by_action]
    if missing:
        return unresolved(
            (
                "missing measured action calibration for " + ", ".join(missing)
            ),
            expected_calls_after * current_tokens * cache_read_multiplier,
        )
    evaluations = [
        _action_evaluation(
            by_action[action],
            current_tokens=current_tokens,
            ceiling=ceiling,
            expected_calls_after=expected_calls_after,
            cache_read_multiplier=cache_read_multiplier,
        )
        for action in required_actions
    ]
    keep_cost = expected_calls_after * current_tokens * cache_read_multiplier
    economic_choice, chosen = _choose_calculated_action(
        evaluations,
        keep_cost=keep_cost,
        capacity_band_name=cap_band,
    )
    return CalculatedContextVerdict(
        resolved=True,
        chosen_action=chosen,
        economic_choice=economic_choice,
        economic_cause=(
            f"keep={keep_cost:.3f}; "
            + "; ".join(
                f"{evaluation.action}={evaluation.projected_total_cost_units:.3f} "
                f"with H={evaluation.post_action_prefix_tokens} and "
                f"break_even_N={evaluation.break_even_horizon_calls}"
                for evaluation in evaluations
            )
            + "; actions must be strictly cheaper than keep"
        ),
        keep_projected_cost_units=keep_cost,
        action_evaluations=tuple(evaluations),
        capacity_band=cap_band,
        capacity_cause=cap_guidance,
        current_tokens=current_tokens,
        ceiling=ceiling,
        expected_calls_after=expected_calls_after,
        cache_state=cache_state,
        cache_read_multiplier=cache_read_multiplier,
        capability_profile_id=capability_profile_id,
        capability_profile_version=capability_profile_version,
        usage_economics_profile_id=usage_economics_profile_id,
        usage_economics_profile_version=usage_economics_profile_version,
        objective=objective,
    )


def rotation_band(
    current_tokens: int, *, cache_cold: bool, overage: bool = False,
) -> tuple[str, str]:
    """``(band, guidance)`` for a measured context size and cache state.

    ``overage`` selects the cache-TTL premium the guidance's stated horizon is
    computed from, and it exists to stop ONE quantity being reported as TWO
    NUMBERS. The guidance string embeds the break-even horizon; a caller that
    also reports :func:`break_even_horizon` itself would otherwise print the
    overage-aware figure beside this string's nominal one, and two sources that
    can disagree about the same fact teach the reader to trust neither. Found
    exactly that way, in a rendered notice reading "~4 more calls" on one line
    and "~3" on the next. Keyword-only and defaulting False so every
    pre-2026-08-17 caller keeps the 1-hour behaviour it was written against.

    The COLD branch is not a stricter version of the warm bands, it is a
    different question. Warm, the carried context is cheap to re-read and the
    only cost of continuing is the per-call carriage, so the bands are about
    when that carriage stops being worth it. Cold, the context will be re-paid
    at full price on the very next call whether you rotate or not -- so the
    reason to keep a long context has gone, and the comparison is against H,
    the prefix a clear would rewrite.
    """
    if cache_cold:
        if current_tokens > POLICY_H_TOKENS:
            return ("cold_above_h", (
                f"cache is cold and context ({current_tokens:,}) exceeds H "
                f"({POLICY_H_TOKENS:,}) -- rotate: the carried context will be "
                "re-paid at full price either way"))
        return ("cold_below_h", (
            f"cache is cold but context ({current_tokens:,}) is under H "
            f"({POLICY_H_TOKENS:,}) -- keep working, a clear would cost more "
            "than it saves"))
    if current_tokens < WARM_BAND_KEEP_WORKING_TOKENS:
        return ("warm_keep", (
            f"keep working -- at {current_tokens:,} a clear would not pay for "
            f"itself unless you still had "
            f"{_horizon_prose(current_tokens, overage=overage)} to make"))
    if current_tokens < WARM_BAND_TASK_BOUNDARY_TOKENS:
        return ("warm_task_boundary", (
            f"rotate at the next natural task boundary -- at {current_tokens:,} "
            f"a clear pays for itself with as few as "
            f"{_horizon_prose(current_tokens, overage=overage)} left to make"))
    if current_tokens < WARM_BAND_SAFE_CHECKPOINT_TOKENS:
        return ("warm_safe_checkpoint", (
            f"rotate at the first safe checkpoint -- at {current_tokens:,} "
            f"a clear pays for itself with as few as "
            f"{_horizon_prose(current_tokens, overage=overage)} left to make"))
    # The ratified instruction for this band is kept VERBATIM and leads the
    # string. The 2026-08-17 reframe attaches each band's justification to it;
    # it does not soften what any band asks for, and this is the band where
    # softening would cost the most -- an earlier draft of that reframe
    # accidentally restated the safe-checkpoint action here, collapsing two
    # bands that ask for different things into one.
    return ("warm_immediate", (
        f"rotate immediately -- finish only the in-flight tool action; at "
        f"{current_tokens:,} a clear pays for itself with as few as "
        f"{_horizon_prose(current_tokens, overage=overage)} left to make"))


def write_premium_multiplier(*, overage: bool) -> float:
    """The break-even multiplier for the cache TTL currently in force.

    THE ONLY QUANTITY IN THE BREAK-EVEN THAT LEGITIMATELY VARIES. It varies
    with cache TTL, never with model tier -- see the derivation beside
    :data:`CACHE_WRITE_PREMIUM_MULTIPLIER_OVERAGE`. ``overage`` is the
    ``overage_signature`` :func:`classify_cache_state` already measures: while
    an account is in usage overage the prompt-cache TTL collapses to ~5
    minutes, and the cheaper write premium that comes with it makes clearing
    win SOONER (12.5 rather than 20), not later.
    """
    if overage:
        return CACHE_WRITE_PREMIUM_MULTIPLIER_OVERAGE
    return float(CACHE_WRITE_PREMIUM_MULTIPLIER)


def clearing_wins(
    current_tokens: int, expected_calls_after: int, *, overage: bool = False,
) -> bool:
    """``C > H + kH/N`` -- whether a clear pays for itself.

    ``expected_calls_after`` is N, the calls the rewritten prefix will be
    amortised over. N <= 0 means the rewrite is never amortised, so a clear
    cannot win regardless of how large C is.

    ``k`` is :func:`write_premium_multiplier` -- 20 at the nominal 1-hour cache
    TTL, 12.5 when ``overage`` says the TTL has collapsed to ~5 minutes.
    ``overage`` is keyword-only and defaults to False so every pre-2026-08-17
    caller keeps the exact 1-hour behaviour it was written against.

    THE PRICE PER TOKEN DOES NOT APPEAR because it cancels. Both sides of the
    comparison are the same model's base input price times a fixed multiplier,
    so this is tier-invariant -- see the ECONOMIC ROTATION POLICY block above
    for the four-line derivation and for the tier-scaling proposal it retired.
    """
    if expected_calls_after <= 0:
        return False
    threshold = POLICY_H_TOKENS + (
        write_premium_multiplier(overage=overage) * POLICY_H_TOKENS
    ) / expected_calls_after
    return current_tokens > threshold


def break_even_horizon(current_tokens: int, *, overage: bool = False) -> float | None:
    """``N = kH/(C-H)`` -- the horizon at which a clear starts paying for itself.

    The INVERSE of :func:`clearing_wins`, and the number that lets a notice
    carry its own justification: not "rotate, you are over 300,000" but "at
    300,000 a clear pays for itself even with only ~12 calls left to make". A
    threshold that states the horizon it was derived from is one a reader can
    argue with, which is what stops it becoming furniture.

    ``None`` when ``current_tokens <= H``: below the prefix a clear would
    rewrite there is no horizon at which clearing wins, however long you run.
    That is a genuinely different answer from a large N, so it is a distinct
    return value rather than ``inf`` -- callers must say "never", not "a lot".
    """
    if current_tokens <= POLICY_H_TOKENS:
        return None
    return (
        write_premium_multiplier(overage=overage) * POLICY_H_TOKENS
    ) / (current_tokens - POLICY_H_TOKENS)


def _horizon_prose(current_tokens: int, *, overage: bool) -> str:
    """:func:`break_even_horizon` as a bare phrase a band can frame itself.

    Deliberately carries NO framing word of its own. N is the MINIMUM remaining
    calls at which a clear starts winning, so the same number reads in opposite
    directions either side of a band edge: below `warm_keep`'s edge it is a bar
    you have not cleared ("you would need ~56 still to make"), above it a bar
    you have ("it pays off with as few as ~9 left"). An earlier draft baked
    "only" in here and made the keep-working band argue for rotating.
    """
    horizon = break_even_horizon(current_tokens, overage=overage)
    if horizon is None:
        return (
            f"no number of calls -- it is under H ({POLICY_H_TOKENS:,}), the "
            "prefix a clear would rewrite"
        )
    return f"~{horizon:.0f} more call(s)"


def capacity_band(current_tokens: int, ceiling: int) -> tuple[str, str]:
    """``(band, guidance)`` for how FULL the window is, as distinct from how
    expensive it is.

    A different question from :func:`rotation_band` on a different axis, which
    is why it is a separate function rather than another branch inside that
    one. The warm bands ask whether a clear pays for itself -- absolute tokens,
    identical on every model. This asks whether the session is about to run out
    of room -- a fraction of the model's OWN ceiling, and the only part of this
    module where the model legitimately changes the answer.

    Both bands are unreachable-before-economics on a 1M ceiling by
    construction, and both bind early on a small or unrecognised one. See the
    CAPACITY BANDS block above for why those two facts are the same fact.

    A non-positive ``ceiling`` is refused rather than defaulted: it means the
    caller has no ceiling, and inventing one here would silently answer a
    capacity question with a number nobody measured.
    """
    if ceiling <= 0:
        raise ValueError(f"capacity_band needs a positive ceiling, got {ceiling}")
    fraction = current_tokens / ceiling
    if fraction >= CAPACITY_BAND_CRITICAL_FRACTION:
        return ("capacity_critical", (
            f"{current_tokens:,} is {fraction:.0%} of this model's "
            f"{ceiling:,}-token window -- rotate ahead of new work: this is "
            "room running out, which no amount of cache economics offsets"))
    if fraction >= CAPACITY_BAND_APPROACHING_FRACTION:
        return ("capacity_approaching", (
            f"{current_tokens:,} is {fraction:.0%} of this model's "
            f"{ceiling:,}-token window -- rotate at the next task boundary "
            "while there is still room to finish one"))
    return ("capacity_ok", (
        f"{current_tokens:,} is {fraction:.0%} of this model's "
        f"{ceiling:,}-token window -- room is not the constraint"))


class TimestampAwarenessError(ValueError):
    """A transcript timestamp arrived naive. Refuse rather than assume UTC.

    The peer registry's ``updated_at`` IS naive-UTC and the rotation hook
    deliberately attaches UTC to it. Doing the same here would be a coincidence
    of behaviour rather than a shared rule, and the next person to reuse one
    helper for the other pays the whole UTC offset -- 25,200s on a PDT host,
    two orders of magnitude larger than any bound either side compares against.
    So each parser states its own contract and refuses input that does not meet
    it.
    """


def parse_transcript_timestamp(raw: str) -> datetime:
    """Parse a transcript timestamp, which is tz-AWARE (``...Z``)."""
    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise TimestampAwarenessError(f"transcript timestamp is naive: {raw!r}")
    return stamp


@dataclass(frozen=True)
class AssistantCall:
    """One assistant usage block, reduced to what the cache decision needs."""

    at: datetime
    cache_read_tokens: int

    @property
    def was_cold(self) -> bool:
        """True when this call read NOTHING from cache -- it paid full price."""
        return self.cache_read_tokens == 0


@dataclass(frozen=True)
class CacheState:
    cold: bool
    overage_signature: bool
    reason: str


def _scored_calls(
    calls: list[AssistantCall], cleared_at: datetime | None,
) -> list[AssistantCall]:
    """Calls that count as evidence of cache behaviour.

    THE FIRST CALL AFTER A CLEAR IS COLD BY CONSTRUCTION -- the clear rewrites
    the prefix -- so counting it would make every rotation immediately
    recommend another rotation. This exclusion is coupled to the counting in
    :func:`classify_cache_state`: drop it and the excluded rewrite becomes the
    second cold call that trips the overage threshold. Proven by mutation, so
    it lives in its own function with its own assertion rather than inline.
    """
    scored = [c for c in calls if cleared_at is None or c.at > cleared_at]
    if cleared_at is not None and scored:
        return scored[1:]
    return scored


def _short_gap_cold_count(scored: list[AssistantCall]) -> int:
    """Cold calls that follow their predecessor by LESS than a full TTL.

    A cold call after a long gap is ordinary expiry. A cold call soon after
    another call means the cache did not survive its nominal window, which is
    what usage overage looks like from outside -- the account state is not
    observable to this platform, but its consequence is.
    """
    return sum(
        1
        for older, newer in zip(scored, scored[1:], strict=False)
        if newer.was_cold and (newer.at - older.at).total_seconds() <= CACHE_TTL_SECONDS
    )


def classify_cache_state(
    calls: list[AssistantCall], now: datetime, *, cleared_at: datetime | None,
) -> CacheState:
    """Prompt-cache state, from the transcript's own usage blocks.

    An ISOLATED cold call after a long gap is the idle case, already resolved:
    the rewrite happened, the cache is warm again, warm bands apply. REPEATED
    cold calls across short gaps are the overage signature.
    """
    scored = _scored_calls(calls, cleared_at)
    if not scored:
        return CacheState(False, False, "no scoreable assistant calls -- nothing measured")
    idle_seconds = (now - scored[-1].at).total_seconds()
    if idle_seconds > CACHE_TTL_SECONDS:
        return CacheState(
            True, False,
            f"last call {idle_seconds:.0f}s ago, past the {CACHE_TTL_SECONDS}s TTL",
        )
    short_gap_colds = _short_gap_cold_count(scored)
    if short_gap_colds >= OVERAGE_MIN_COLD_CALLS:
        return CacheState(
            True, True,
            f"{short_gap_colds} cold calls across sub-TTL gaps -- the cache is not "
            "surviving its nominal window (the overage signature)",
        )
    if scored[-1].was_cold:
        return CacheState(False, False, "one cold call, cache re-warmed since -- warm bands apply")
    return CacheState(False, False, "cache is warm")


# ---------------------------------------------------------------------------
# THE COMBINED VERDICT (2026-08-17) -- one object carrying both axes, used by
# the L4c self-notice leg. Kept separate from `rotation_band` rather than
# folded into it: that function's two-tuple contract has callers
# (`_rotation_due_row`, `session_context_status`) that want the economics band
# alone, and widening a returned shape to serve a new consumer is how a stable
# read-back verb acquires fields its own docstring cannot explain.

# Relative urgency, used ONLY to pick which axis a notice leads with. Both
# bands are always reported; this decides the headline, never what is shown.
# Ranks are deliberately shared across axes -- capacity_approaching and
# warm_task_boundary are the same call to action ("rotate at a boundary"),
# arrived at for unrelated reasons, so they rank equal and economics wins the
# tie as the ratified policy.
_BAND_URGENCY: dict[str, int] = {
    "capacity_ok": 0,
    "cold_below_h": 0,
    "warm_keep": 0,
    "capacity_approaching": 1,
    "warm_task_boundary": 1,
    "cold_above_h": 2,
    "warm_safe_checkpoint": 2,
    "capacity_critical": 3,
    "warm_immediate": 3,
}


# The urgency rank at which a band stops saying "keep working" and starts
# asking for a rotation of some kind. `warm_task_boundary` and
# `capacity_approaching` both sit here: "rotate at the next boundary" is still
# a request to rotate, and a predicate that fired only on the TOP rank would
# have re-created GAU-08 exactly one band lower -- silent through the whole
# 200,000-300,000 stretch instead of the whole 300,000-500,000 one.
ACTIONABLE_BAND_RANK: int = 1


def band_is_actionable(band: str) -> bool:
    """Whether ``band`` asks for a rotation at all, as against keeping work.

    Derived from :data:`_BAND_URGENCY` rather than from a second list of band
    names. A hand-kept "these ones count" set would be a copy of that map with
    nothing forcing the two to agree, and its failure mode is SILENT: a band
    added to the map and forgotten here would simply never make a session
    rotation-due, which is the same shape of quiet gap this function exists to
    close.

    An unranked band name raises ``KeyError`` deliberately. A band this module
    has not ranked is one whose urgency nobody has decided, and answering "not
    actionable" for it would be a guess in the quiet direction -- the exact
    direction this whole landing is correcting.
    """
    return _BAND_URGENCY[band] >= ACTIONABLE_BAND_RANK


def is_rotation_due_for_ceiling(
    *, ceiling: int, current_tokens: int, cache_cold: bool | None,
) -> bool:
    """THE ROTATION-DUE PREDICATE (GAU-08, ruled 2026-08-18): the ECONOMICS
    BAND is actionable, OR ``current_tokens`` has crossed
    :data:`ROTATION_THRESHOLD_FRACTION` of ``ceiling``.

    A UNION, and both halves are load-bearing. Neither alone is correct, and
    each is wrong for the reason the other is right.

    WHY NOT THE FRACTION ALONE -- the defect this replaces. The economics
    bands are ABSOLUTE token counts and saturate at ``warm_immediate`` at
    300,000; the fraction hint does not cross 0.5 of a 1M ceiling until
    500,000. So for 200,000 tokens the most urgent band this policy has
    coexisted with ``rotation_due=False``, and that is not a corner: measured
    2026-08-18 across the live fleet, the field read False on EVERY session at
    EVERY value any of them occupied that day (219,974 / 195,778 / 226,257).
    It was false throughout the entire range in which anyone actually works.

    WHY NOT THE BAND ALONE -- the decoy, and the reason this is a union rather
    than a rename. The bands are MODEL-BLIND while this predicate is
    model-AWARE through its ceiling. On a 200,000-token window the first
    actionable band arrives at 150,000, i.e. 75% of the window, so a pure-band
    rule returns False at 100,000 -- that model's own halfway point, and
    precisely what the fraction hint exists to catch. It would have made
    rotation-due strictly LATER on every small-ceiling model while looking
    like a clean improvement on the 1M models it would have been tested on.

    MONOTONE BY CONSTRUCTION: a strict superset of the fraction rule it
    replaces, so no session that was served a notice before can lose one. That
    is what makes it landable on a live shared surface with no migration and
    no backfill of stored rows.

    THE ECONOMICS BAND, NOT THE EFFECTIVE ONE. Folding in the capacity axis
    looks more complete and buys nothing: :data:`CAPACITY_BAND_APPROACHING_
    FRACTION` (0.75) is above :data:`ROTATION_THRESHOLD_FRACTION` (0.5) and
    both are fractions of the same denominator, so the fraction term has
    already fired everywhere capacity could, on every ceiling. That is a
    CONDITIONAL fact rather than a permanent one -- it holds only while that
    inequality does -- so it is pinned by a test rather than left here, where
    it could rot silently if anyone lowered the capacity fraction.

    NO ``overage`` ARGUMENT, and its absence is checked rather than assumed:
    :func:`rotation_band` takes one, but it reaches only ``_horizon_prose``,
    the break-even horizon quoted INSIDE the guidance string, and never the
    band NAME. Since this decides on the name, a collapsed cache TTL cannot
    move the verdict.

    ``cache_cold=None`` means NOT MEASURED and is treated exactly as warm. It
    is never promoted to cold: a reporter predating cache attribution sends no
    cache state, and reading its absence as cold would make a session in the
    110,702-150,000 window due on the strength of a field nobody wrote -- a
    measurement manufactured out of a gap. The delivered notice already
    carries that caveat in words (see ``_rotation_prose``).

    A non-positive ``ceiling`` is REFUSED rather than defaulted, the same
    posture :func:`capacity_band` states for itself and for the same reason:
    inventing one here would decide whether a live session is told to rotate
    using a window size nobody measured.
    """
    return rotation_due_verdict(
        ceiling=ceiling, current_tokens=current_tokens, cache_cold=cache_cold,
    ).due


@dataclass(frozen=True)
class RotationDueVerdict:
    """The rotation-due answer TOGETHER WITH which axis produced it.

    Exists so that a notice can say WHY it fired without restating the rule.
    The hook's notice previously read ``threshold_fraction=0.5 (crossed at
    30.0% of ceiling)`` -- a sentence that refutes itself, and one that the
    union turns from unreachable into the common case, since the entire
    actionable 300,000-500,000 range on a 1M ceiling fires on the band with
    the fraction hint uncrossed. The obvious repair is to recompute
    ``current_tokens >= ceiling * ROTATION_THRESHOLD_FRACTION`` in the prose
    builder, and that is the trap: it puts a second copy of the rule in a
    second file, so a later edit to the comparison would desynchronise the
    notice from the decision it describes and nothing would say so. Returning
    the decomposition keeps ONE evaluation of each term and lets every caller
    read the parts it needs.
    """

    due: bool
    band: str
    band_actionable: bool
    fraction: float
    fraction_crossed: bool


def rotation_due_verdict(
    *, ceiling: int, current_tokens: int, cache_cold: bool | None,
) -> RotationDueVerdict:
    """Compatibility view of the single rotation-notice decision.

    ``cache_cold`` remains accepted for installed callers, but it no longer
    triggers a notice. The supplied ceiling is a runtime-reported window for
    this legacy entry point; modern callers use :func:`rotation_notice_verdict`
    to retain model, effort, and source provenance.
    """
    if ceiling <= 0:
        raise ValueError(
            f"rotation_due_verdict needs a positive ceiling, got {ceiling}",
        )
    decision = rotation_notice_verdict(
        model="", effort="", current_tokens=current_tokens,
        runtime_window_tokens=ceiling,
    )
    return RotationDueVerdict(
        due=decision.due,
        band="notice_due" if decision.due else "notice_below_threshold",
        band_actionable=decision.due,
        fraction=current_tokens / ceiling,
        fraction_crossed=decision.due,
    )


@dataclass(frozen=True)
class RotationVerdict:
    """Both rotation axes for one measured session, plus the headline.

    ``effective_band`` is what the notice latches on, and it is a BAND NAME,
    never a fraction of a ceiling. That is the whole point of this landing:
    `sweep_rotation_due_sessions` gates on `fraction >= 0.5`, which on a
    1M-ceiling model is 500,000 -- so a session sitting anywhere between
    300,000 and 500,000 is in the saturated `warm_immediate` band while the
    fraction gate still reads False, and the notice is silent exactly where it
    is most needed. Keying on the band closes that window by construction.
    """

    economics_band: str
    economics_guidance: str
    capacity_band: str
    capacity_guidance: str
    effective_band: str
    headline: str
    horizon_calls: float | None
    overage: bool


def rotation_surface_verdict(
    *,
    current_tokens: int,
    ceiling: int,
    cache_cold: bool,
    overage: bool = False,
) -> RotationVerdict:
    """Both bands for one gauge reading, and which of them leads.

    ``ceiling`` must already be resolved (``resolve_ceiling(model)``); this
    function does no model lookup of its own, so a caller cannot accidentally
    get a capacity verdict against a ceiling it never checked.
    """
    econ_band, econ_guidance = rotation_band(
        current_tokens, cache_cold=cache_cold, overage=overage,
    )
    cap_band, cap_guidance = capacity_band(current_tokens, ceiling)
    econ_rank = _BAND_URGENCY[econ_band]
    cap_rank = _BAND_URGENCY[cap_band]
    # >= keeps economics as the tie-break: it is the ratified policy, and a tie
    # means both axes are asking for the same action anyway.
    if econ_rank >= cap_rank:
        effective, headline = econ_band, econ_guidance
    else:
        effective, headline = cap_band, cap_guidance
    return RotationVerdict(
        economics_band=econ_band,
        economics_guidance=econ_guidance,
        capacity_band=cap_band,
        capacity_guidance=cap_guidance,
        effective_band=effective,
        headline=headline,
        horizon_calls=break_even_horizon(current_tokens, overage=overage),
        overage=overage,
    )


def _assert_premium_matches_its_derivation() -> None:
    """Fail LOUD at import if the ratified premium and the two sourced vendor
    multipliers it is derived from ever disagree.

    :data:`CACHE_WRITE_PREMIUM_MULTIPLIER` is an int by choice (see its
    comment), so it cannot simply BE the quotient. Without this guard, editing
    :data:`CACHE_WRITE_MULTIPLIER_1H` to track a vendor price change would
    leave the premium -- and therefore every band's stated horizon -- silently
    describing the old pricing. An `assert` would be stripped under `python
    -O`, and this is exactly the check that must not vanish in the environment
    where it matters.
    """
    derived = CACHE_WRITE_MULTIPLIER_1H / CACHE_READ_MULTIPLIER
    if abs(derived - CACHE_WRITE_PREMIUM_MULTIPLIER) > 1e-6:
        raise ValueError(
            f"CACHE_WRITE_PREMIUM_MULTIPLIER={CACHE_WRITE_PREMIUM_MULTIPLIER} no "
            f"longer equals CACHE_WRITE_MULTIPLIER_1H/CACHE_READ_MULTIPLIER="
            f"{derived}; the bands' stated break-even horizons are derived from "
            "this ratio and would silently describe retired pricing",
        )


_assert_premium_matches_its_derivation()
