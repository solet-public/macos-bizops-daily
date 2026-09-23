#!/usr/bin/env python3
"""Unit smoke for ``rotation_thresholds.py`` (rotation-systematization P2
slice A/B, ruling 3, ratified 2026-08-07; ceiling table populated 2026-08-07
at the seat's closing-slice package review) -- the declared context-ceiling
table + conservative fallback + 50% threshold comparison.

Proves: an unknown model resolves to DEFAULT_CONSERVATIVE_CEILING, never a
KeyError or a silently-invented value; a known model resolves to ITS OWN
ceiling, not the fallback; is_rotation_due is false strictly below the
threshold fraction and true at/above it (the exact boundary, since a
>-only comparison would miss the crossing tick); MODEL_CONTEXT_CEILINGS
carries EXACTLY the seat-confirmed alias set (a structural drift guard --
neither a silent addition nor a silent removal passes); every REAL populated
alias resolves to its measured ceiling against the production data itself,
not a fixture; and a plausible-but-not-yet-added future model name still
falls back to the conservative ceiling rather than being silently exempted.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/rotation_thresholds_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import rotation_thresholds as rt  # noqa: E402

# ★ THE DISCRIMINATING SIZE -- DERIVED, NEVER A LITERAL (GAU-05, 2026-08-19).
#
# Several tests below need a token count that lands strictly BETWEEN H and the
# first warm band, because that window is the only place a "compares against H"
# implementation and a "compares against the 150K band" implementation give
# different answers. Everywhere else the two agree, so an assertion outside the
# window passes under the collapse it is supposed to catch.
#
# It used to be the literal 120,000, and the GAU-05 re-measurement is exactly
# the event that invalidates a literal: H moved 110,702 -> 146,139, which put
# 120,000 BELOW H. (H has since moved AGAIN -- 146,139 -> 81,889 on 2026-08-19,
# when the rehydration half was re-priced against the pointer-card pickup --
# which puts 120,000 back ABOVE H and would have stranded the same literal a
# second time, in the opposite direction. Twice in one day is the argument for
# deriving it.) Every assertion built on it would have flipped from
# `cold_above_h` to `cold_below_h` -- and a re-measurer whose only instinct is
# "make the tests green again" would have edited the EXPECTATIONS to match,
# destroying the discriminator while leaving a file that still reads like a
# live test. Deriving it means a future H measurement moves the probe with the
# threshold instead of silently stranding it on the wrong side.
#
# The window is NOT assumed to exist. `test_cold_cache_is_a_separate_axis_not_a_
# stricter_warm` asserts `WARM_BAND_KEEP_WORKING_TOKENS > POLICY_H_TOKENS`
# first, and `test_the_discriminating_window_is_real` below fails loudly if the
# window ever closes. That was a live risk at the GAU-05 values, where it was
# 3,861 tokens wide, down from 39,298; the 2026-08-19 rehydration
# re-measurement re-opened it to 68,111, so the guard is now armed rather than
# nearly tripped. It stays because the width is a MEASUREMENT, not a design
# choice, and it has now moved in both directions inside a single day.
_DISCRIMINATING_TOKENS: int = (
    rt.POLICY_H_TOKENS + rt.WARM_BAND_KEEP_WORKING_TOKENS
) // 2

# ★ THE BELOW-H SIZE -- DERIVED FOR THE SAME REASON, AND STRANDED FOR REAL
# (2026-08-19 rehydration re-measurement).
#
# `test_cold_is_a_different_question_not_a_stricter_band` needs a size that is
# below H AND below the first warm band, so that cold and warm agree there for
# DIFFERENT reasons. It was the literal 90,000, chosen when H was 110,702 and
# still correct at 146,139. The re-measurement to 81,889 put 90,000 ABOVE H,
# and the assertion went red -- the only red this whole change produced, and
# the useful kind: it is the literal announcing that it had stopped describing
# the case it was named for. Deriving it is the fix; editing the EXPECTATION
# from `cold_below_h` to `cold_above_h` would also have been green and would
# have deleted the below-H half of the asymmetry this test exists to state.
#
# Half of H is used rather than a fixed offset so the probe cannot be dragged
# to zero or below by a future measurement, and it is asserted to be inside
# both bounds at the point of use rather than assumed here.
_BELOW_H_TOKENS: int = rt.POLICY_H_TOKENS // 2

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def test_table_carries_exactly_the_seat_confirmed_keys() -> None:
    _check(
        set(rt.MODEL_CONTEXT_CEILINGS.keys()) == {
            "claude-fable-5", "claude-sonnet-5", "claude-opus-5",
            "claude-haiku-4-5", "claude-haiku-4-5-20251001",
        },
        "MODEL_CONTEXT_CEILINGS carries exactly the seat-confirmed alias set, "
        "no silent additions or removals",
    )


def test_real_aliases_resolve_to_their_measured_ceilings() -> None:
    """Distinct from the fixture-injection test below (which proves the
    MECHANISM works) -- this proves the PRODUCTION DATA itself is correct,
    a direct assertion against the real table, not a stubbed value."""
    _check(
        rt.resolve_ceiling("claude-sonnet-5") == 1_000_000,
        "claude-sonnet-5 resolves to its measured 1M ceiling, not the fallback",
    )
    _check(
        rt.resolve_ceiling("claude-fable-5") == 1_000_000,
        "claude-fable-5 resolves to its measured 1M ceiling",
    )
    _check(
        rt.resolve_ceiling("claude-opus-5") == 1_000_000,
        "claude-opus-5 resolves to its measured 1M ceiling",
    )
    _check(
        rt.resolve_ceiling("claude-haiku-4-5") == 200_000,
        "claude-haiku-4-5 resolves to its measured 200K ceiling",
    )
    _check(
        rt.resolve_ceiling("claude-haiku-4-5-20251001") == 200_000,
        "the dated full-ID form also resolves to 200K, defensively",
    )


def test_plausible_future_model_still_falls_back_conservative() -> None:
    """RED-FIRST regression against silent scope-creep exemptions: a
    plausible-looking future model name (not an obviously-fake fixture
    string) must still fall back to the conservative ceiling -- kills a
    mutation that special-cases "looks like a claude model" instead of
    exact-key lookup."""
    _check(
        rt.resolve_ceiling("claude-opus-6") == rt.DEFAULT_CONSERVATIVE_CEILING,
        "a plausible-but-not-yet-added future model name still falls back "
        "to the conservative ceiling, never silently exempted",
    )


def test_unknown_model_resolves_to_conservative_fallback() -> None:
    _check(
        rt.resolve_ceiling("some-model-nobody-populated-yet") == rt.DEFAULT_CONSERVATIVE_CEILING,
        "unknown model resolves to DEFAULT_CONSERVATIVE_CEILING, never a guess or KeyError",
    )


def test_known_model_resolves_to_its_own_ceiling_not_the_fallback() -> None:
    original = dict(rt.MODEL_CONTEXT_CEILINGS)
    try:
        rt.MODEL_CONTEXT_CEILINGS["test-model-x"] = 250_000
        _check(
            rt.resolve_ceiling("test-model-x") == 250_000,
            "a populated model resolves to its own ceiling, not DEFAULT_CONSERVATIVE_CEILING",
        )
    finally:
        rt.MODEL_CONTEXT_CEILINGS.clear()
        rt.MODEL_CONTEXT_CEILINGS.update(original)


def test_is_rotation_due_false_strictly_below_threshold() -> None:
    ceiling = rt.DEFAULT_CONSERVATIVE_CEILING
    just_below = int(ceiling * rt.ROTATION_THRESHOLD_FRACTION) - 1
    _check(
        rt.is_rotation_due(
            model="unknown", current_tokens=just_below, cache_cold=False,
        ) is False,
        "one token below the threshold fraction -> not due (and the band half is "
        "warm_keep at 49,999, so the fraction half is what is under test here)",
    )


def test_is_rotation_due_true_at_exact_threshold_boundary() -> None:
    ceiling = rt.DEFAULT_CONSERVATIVE_CEILING
    at_threshold = int(ceiling * rt.ROTATION_THRESHOLD_FRACTION)
    _check(
        rt.is_rotation_due(
            model="unknown", current_tokens=at_threshold, cache_cold=False,
        ) is True,
        "exactly at the threshold fraction -> due (kills a strict-greater-than mutation)",
    )


def test_watch_threshold_constants_match_ratified_values() -> None:
    """Structural drift guard, same pattern as the ceiling-table key-set test
    above -- these four values were individually ratified (P4(a) ratification
    condition 4); a silent edit to any of them should fail this smoke, not
    slip through unnoticed."""
    _check(
        rt.WATCH_POLL_INTERVAL_SECONDS == 60,
        "WATCH_POLL_INTERVAL_SECONDS matches its ratified value (60s)",
    )
    _check(
        rt.IDLE_POKE_THRESHOLD_SECONDS == 300,
        "IDLE_POKE_THRESHOLD_SECONDS matches its ratified value (300s)",
    )
    _check(
        rt.IDLE_ROTATE_THRESHOLD_SECONDS == 2100,
        "IDLE_ROTATE_THRESHOLD_SECONDS matches its ratified value (2100s)",
    )
    _check(
        rt.POKE_COOLDOWN_SECONDS == 1800,
        "POKE_COOLDOWN_SECONDS matches its ratified value (1800s)",
    )


def test_poke_threshold_is_strictly_below_rotate_threshold() -> None:
    """Load-bearing ordering, not just individual values: the poke path is
    supposed to have a chance to fire and drain pending mail well before the
    rotate path would otherwise trigger (P4(a).4 precedence design -- poke
    evaluated first, and only wins the race if its threshold is the smaller
    one). If a future edit ever inverted this ordering, poke would never have
    a window to act before rotate did instead."""
    _check(
        rt.IDLE_POKE_THRESHOLD_SECONDS < rt.IDLE_ROTATE_THRESHOLD_SECONDS,
        "poke threshold is strictly below rotate threshold, preserving the "
        "poke-fires-first design window",
    )


def test_rotate_threshold_is_margined_below_cache_ttl_floor() -> None:
    """The trade-off documented on IDLE_ROTATE_THRESHOLD_SECONDS is only real
    if the number itself actually sits below the fleet's conservative 45-min
    cache-TTL floor -- this pins that margin so it can't silently drift past
    the boundary it exists to stay ahead of."""
    conservative_ttl_floor_seconds = 45 * 60
    _check(
        rt.IDLE_ROTATE_THRESHOLD_SECONDS < conservative_ttl_floor_seconds,
        "rotate threshold fires strictly before the conservative 45-minute "
        "cache-TTL floor, not at or after it",
    )


def test_h_is_the_sum_of_its_measured_parts() -> None:
    """H is composed, not asserted. If someone edits one part and not the
    total, this catches it -- the failure mode for a constant whose value is
    a measurement rather than a choice."""
    _check(
        rt.POLICY_H_TOKENS == rt.POLICY_H_BOOT_TOKENS + rt.POLICY_H_REHYDRATION_TOKENS,
        "H equals boot payload + incremental rehydration, not an independent number",
    )
    _check(rt.POLICY_H_TOKENS == 81_889, "H is the measured 81,889 (2026-08-19)")
    # The COMPONENTS are pinned too, not just the total, and this file is where
    # that separation earns its keep. GAU-05 measured that the two move for
    # different reasons and at wildly different rates -- boot +1.4% while
    # rehydration +51.4% over the same three days -- so a total-only pin cannot
    # tell a real re-measurement from an edit that moved one part and
    # compensated in the other. The 2026-08-19 bug-wave re-measurement then
    # moved ONLY the rehydration half (102,665 -> 38,415, re-priced against the
    # pointer-card pickup that replaced the seat pickup prompt) and left the
    # boot half untouched, which is exactly the shape a total-only pin cannot
    # express.
    _check(rt.POLICY_H_BOOT_TOKENS == 43_474, "boot payload is the measured 43,474")
    _check(rt.POLICY_H_REHYDRATION_TOKENS == 38_415,
           "incremental rehydration is the measured 38,415")


def test_warm_bands_are_ordered_and_exhaustive() -> None:
    """Every warm size lands in exactly one band, and the boundaries ascend.
    An unordered band table silently swallows a whole range."""
    _check(
        rt.WARM_BAND_KEEP_WORKING_TOKENS
        < rt.WARM_BAND_TASK_BOUNDARY_TOKENS
        < rt.WARM_BAND_SAFE_CHECKPOINT_TOKENS,
        "warm band boundaries ascend",
    )
    seen = {rt.rotation_band(n, cache_cold=False)[0]
            for n in (0, 149_999, 150_000, 199_999, 200_000, 299_999, 300_000, 10**7)}
    _check(
        seen == {"warm_keep", "warm_task_boundary", "warm_safe_checkpoint", "warm_immediate"},
        "every warm size resolves to exactly one of the four bands",
    )


def test_band_boundaries_are_inclusive_where_the_policy_says_so() -> None:
    """The crossing tick is the one that matters: a >-only comparison misses
    the exact boundary, which is the same defect this file already guards for
    is_rotation_due."""
    _check(rt.rotation_band(149_999, cache_cold=False)[0] == "warm_keep",
           "just under 150K still keeps working")
    _check(rt.rotation_band(150_000, cache_cold=False)[0] == "warm_task_boundary",
           "exactly 150K has crossed into the task-boundary band")
    _check(rt.rotation_band(300_000, cache_cold=False)[0] == "warm_immediate",
           "exactly 300K rotates immediately, not at the next checkpoint")


def test_cold_is_a_different_question_not_a_stricter_band() -> None:
    """Cold compares against H, not against the warm bands. A context that
    would 'keep working' warm can still be worth rotating cold, and one that
    would rotate warm can be NOT worth rotating cold -- the asymmetry is the
    point, and collapsing cold into 'a stricter warm' loses it."""
    # The probe is DERIVED (see `_BELOW_H_TOKENS`); the two bounds it has to sit
    # inside are asserted here rather than trusted, so a future H that strands
    # it says so instead of quietly re-labelling the case.
    _check(_BELOW_H_TOKENS < rt.POLICY_H_TOKENS,
           f"the below-H probe {_BELOW_H_TOKENS:,} really is below H "
           f"({rt.POLICY_H_TOKENS:,})")
    _check(_BELOW_H_TOKENS < rt.WARM_BAND_KEEP_WORKING_TOKENS,
           f"...and below the first warm band ({rt.WARM_BAND_KEEP_WORKING_TOKENS:,}), "
           "so warm and cold agree there for DIFFERENT reasons, which is the "
           "case this test is about")
    _check(rt.rotation_band(_BELOW_H_TOKENS, cache_cold=True)[0] == "cold_below_h",
           "cold and under H -> keep working; a clear would cost more than it saves")
    _check(rt.rotation_band(250_000, cache_cold=True)[0] == "cold_above_h",
           "cold and over H -> rotate")
    _check(rt.rotation_band(_BELOW_H_TOKENS, cache_cold=False)[0] == "warm_keep",
           f"the same {_BELOW_H_TOKENS:,} warm is also keep -- but for a "
           "different reason")
    _check(rt.rotation_band(250_000, cache_cold=False)[0] == "warm_safe_checkpoint",
           "the same 250K warm waits for a safe checkpoint rather than rotating now")
    # THE DISCRIMINATING VALUE. The below-H probe and 250K cannot tell
    # "compares against H"
    # apart from "compares against the 150K warm band" -- both thresholds sort
    # them identically, so those assertions pass under a cold-is-just-stricter-
    # warm implementation. Only a value strictly BETWEEN H and
    # WARM_BAND_KEEP_WORKING_TOKENS separates the two, which is the whole claim
    # this test exists to make. Found by mutation: without this line the test
    # was blind to exactly the collapse it is named after. The probe is
    # DERIVED (see `_DISCRIMINATING_TOKENS`) so a future H re-measurement moves
    # it with the threshold rather than stranding it on the wrong side.
    _check(rt.WARM_BAND_KEEP_WORKING_TOKENS > rt.POLICY_H_TOKENS,
           "H sits BELOW the first warm band -- there is a range that separates them")
    _check(rt.rotation_band(_DISCRIMINATING_TOKENS, cache_cold=True)[0] == "cold_above_h",
           f"{_DISCRIMINATING_TOKENS:,} cold is ABOVE H -> rotate, even though the "
           "same size warm would keep working")
    _check(rt.rotation_band(_DISCRIMINATING_TOKENS, cache_cold=False)[0] == "warm_keep",
           f"{_DISCRIMINATING_TOKENS:,} warm keeps working -- the same size, "
           "opposite verdicts by cache state")


def test_the_discriminating_window_is_real() -> None:
    """The window between H and the first warm band EXISTS and the derived
    probe lands strictly inside it (GAU-05, 2026-08-19).

    Every "cold and warm disagree at the same size" assertion in this file is
    only a test while that window is non-empty. It is not a formality, and the
    history is the proof: GAU-05 narrowed it from 39,298 tokens to 3,861 -- a
    further ~2.6% rise in H would have closed it outright -- and the 2026-08-19
    rehydration re-measurement then re-opened it to 68,111. The width tracks a
    measured quantity and has moved in both directions, so it can close again
    without anyone deciding to close it. When it does, the honest
    outcome is a LOUD RED here naming the collapse -- not a set of downstream
    assertions that quietly start passing for the wrong reason, which is
    precisely what a hardcoded probe would have delivered.

    Checked as a property of the live constants rather than against copies of
    them, so it cannot pass by agreeing with a stale transcription.
    """
    width = rt.WARM_BAND_KEEP_WORKING_TOKENS - rt.POLICY_H_TOKENS
    _check(width > 0,
           f"the H-to-first-warm-band window is non-empty (width {width:,}) -- "
           "without it no size can tell the cold axis from the warm one")
    _check(rt.POLICY_H_TOKENS < _DISCRIMINATING_TOKENS < rt.WARM_BAND_KEEP_WORKING_TOKENS,
           f"the derived probe {_DISCRIMINATING_TOKENS:,} lands STRICTLY inside "
           f"({rt.POLICY_H_TOKENS:,}, {rt.WARM_BAND_KEEP_WORKING_TOKENS:,})")
    # The probe must also be low enough that the FRACTION half of the union
    # cannot be what decides the cold/warm split -- otherwise the cold-axis
    # tests would be green for a reason that has nothing to do with the cache.
    _check(
        _DISCRIMINATING_TOKENS
        < rt.resolve_ceiling("claude-opus-5") * rt.ROTATION_THRESHOLD_FRACTION,
        "the probe is below the fraction threshold on a 1M ceiling, so the "
        "fraction half of the union decides neither answer",
    )


def test_clearing_wins_needs_calls_to_amortise_over() -> None:
    """C > H + 20H/N. With no calls after the clear the rewrite is never
    amortised, so a clear cannot win no matter how large C is -- the guard
    against 'rotate at the very end of the work', which pays the rewrite and
    then throws it away."""
    _check(not rt.clearing_wins(10_000_000, 0),
           "N=0 -> clearing never wins, however large the context")
    _check(not rt.clearing_wins(10_000_000, -3), "negative N is refused too")
    _check(rt.clearing_wins(559_000, 50),
           "the 559K case with 50 calls ahead: clearing wins")
    # ★ MUST BE ABOVE H (GAU-05, 2026-08-19). This probe exists to exercise the
    # AMORTISATION term, so it has to be a context a clear could plausibly pay
    # for -- i.e. C > H -- and lose only because N is too small. The old literal
    # 120,000 was above the then-H of 110,702; after the re-measurement to
    # 146,139 it fell BELOW H, where `clearing_wins` returns False on the H
    # comparison alone. (At the 2026-08-19 H of 81,889 it would sit above H once
    # more -- but a literal that is only accidentally correct again is still not
    # a test.) Left as it was, this assertion would still be green with
    # the entire `20H/N` term deleted -- a test that had quietly stopped testing
    # its own subject. Derived from H for the same reason the discriminating
    # size is.
    _amortisation_probe = rt.POLICY_H_TOKENS + 50_000
    _check(_amortisation_probe > rt.POLICY_H_TOKENS,
           "the amortisation probe sits ABOVE H, so only N can decide it")
    _check(not rt.clearing_wins(_amortisation_probe, 2),
           f"{_amortisation_probe:,} (above H) with only 2 calls ahead: clearing "
           "loses -- the rewrite never amortises")
    _check(rt.clearing_wins(_amortisation_probe, 10_000),
           "...and the SAME context with plenty of calls ahead wins -- so N, not "
           "size, is what the previous assertion measured")


def test_the_superseded_fraction_survives_as_a_hint_only() -> None:
    """ROTATION_THRESHOLD_FRACTION is deliberately KEPT -- the verb still emits
    it. This pins that it was not silently repurposed as the policy: it is a
    fraction, the policy is absolute token counts, and the two must not drift
    into each other."""
    _check(isinstance(rt.ROTATION_THRESHOLD_FRACTION, float),
           "the legacy hint is still a FRACTION, not a token count")
    _check(rt.ROTATION_THRESHOLD_FRACTION == 0.5, "the hint is unchanged at 0.5")
    _check(isinstance(rt.WARM_BAND_KEEP_WORKING_TOKENS, int)
           and rt.WARM_BAND_KEEP_WORKING_TOKENS > 1_000,
           "the policy bands are absolute token counts, not fractions")


def _call(offset_s: float, *, cold: bool, base: datetime) -> rt.AssistantCall:
    return rt.AssistantCall(
        at=base + timedelta(seconds=offset_s),
        cache_read_tokens=0 if cold else 150_000,
    )


def test_post_clear_rewrite_is_not_evidence_of_a_cold_cache() -> None:
    """The dangerous one. A clear rewrites the prefix, so the first call after
    it is cold BY CONSTRUCTION. Counting it makes every rotation immediately
    recommend another rotation."""
    base = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    st = rt.classify_cache_state(
        [_call(1, cold=True, base=base)], base + timedelta(seconds=2), cleared_at=base)
    _check(not st.cold, "the post-clear rewrite alone is NOT a cold cache")
    st = rt.classify_cache_state(
        [_call(1, cold=True, base=base), _call(2, cold=False, base=base)],
        base + timedelta(seconds=3), cleared_at=base)
    _check(not st.cold, "post-clear rewrite then a warm call -> warm")


def test_isolated_cold_call_is_the_resolved_idle_case() -> None:
    base = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    st = rt.classify_cache_state(
        [_call(0, cold=False, base=base), _call(7200, cold=True, base=base)],
        base + timedelta(seconds=7210), cleared_at=None)
    _check(not st.cold and not st.overage_signature,
           "ONE cold call after a long gap = idle case resolved, warm bands apply")


def test_repeated_cold_calls_across_short_gaps_are_the_overage_signature() -> None:
    base = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    st = rt.classify_cache_state(
        [_call(0, cold=False, base=base), _call(60, cold=True, base=base),
         _call(120, cold=True, base=base)],
        base + timedelta(seconds=130), cleared_at=None)
    _check(st.cold and st.overage_signature,
           "TWO cold calls across sub-TTL gaps = overage signature")


def test_ttl_lapse_is_caught_even_when_the_last_call_read_cache() -> None:
    """The robustness half. cache_read==0 is structurally blind to a PARTIAL
    cache read after the TTL lapsed; the age comparison is not."""
    base = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    st = rt.classify_cache_state(
        [_call(0, cold=False, base=base)], base + timedelta(seconds=4000), cleared_at=None)
    _check(st.cold and not st.overage_signature,
           "past the TTL -> cold, even though that call read cache fine")


def test_transcript_parser_refuses_a_naive_timestamp() -> None:
    """Transcript stamps are AWARE (...Z); the peer registry's are NAIVE UTC.
    One helper for both is the 25,200-second failure, so this parser refuses
    rather than assuming."""
    try:
        rt.parse_transcript_timestamp("2026-08-16T19:12:13.529245")  # the REGISTRY shape
        _check(False, "a naive timestamp is REFUSED, never assumed to be UTC")
    except rt.TimestampAwarenessError:
        _check(True, "a naive timestamp is REFUSED, never assumed to be UTC")
    _check(rt.parse_transcript_timestamp("2026-08-16T20:04:13.422Z").tzinfo is not None,
           "an aware Z timestamp parses")


# ---------------------------------------------------------------------------
# GAU-08 (2026-08-18) -- `rotation_due` as the UNION of the two axes.
#
# The defect: the economics bands are ABSOLUTE token counts and saturate at
# `warm_immediate` at 300,000, while `is_rotation_due` was `fraction >= 0.5`
# of the model's own ceiling. On a 1M ceiling those are 300,000 and 500,000,
# so the most urgent band the policy has coexisted with `rotation_due=False`
# for 200,000 tokens. Measured 2026-08-18 on every live session in the fleet
# at every value any of them occupied that day: the field read False on all
# three live sessions measured, at 219,974 / 195,778 / 226,257 tokens, i.e. it
# was not merely wrong at a corner, it was wrong across the entire range in
# which anyone actually works. WHICH session held which number is not part of
# the argument -- the claim is that the field was False across the whole
# occupied range, and the three readings carry that on their own.


def test_rotation_due_covers_the_whole_actionable_range() -> None:
    """The gap GAU-08 names, asserted as the RANGE it spans, not one point.

    A single sampled value cannot distinguish "the threshold moved" from "the
    predicate changed shape", so both ends of the previously-blind interval
    are pinned along with the band that was already saturated throughout it.
    """
    ceiling = rt.resolve_ceiling("claude-opus-5")
    _check(ceiling == 1_000_000, "the model this range is stated against is a 1M ceiling")
    for tokens in (300_000, 350_000, 499_999):
        _check(
            rt.rotation_band(tokens, cache_cold=False)[0] == "warm_immediate",
            f"{tokens:,} was ALREADY the most urgent band the policy has",
        )
        _check(
            rt.is_rotation_due(
                model="claude-opus-5", current_tokens=tokens, cache_cold=False,
            ) is True,
            f"{tokens:,} on a 1M ceiling is rotation-due -- the band decides here, "
            f"and the old fraction hint said False for all 200,000 tokens of this range",
        )


def test_a_small_ceiling_model_is_still_due_at_its_own_fraction() -> None:
    """★ THE DECOY, AND THE REASON THIS IS A UNION RATHER THAN A RENAME.

    The obvious-looking fix -- redefine `rotation_due` as "the band is
    actionable" -- is WRONG, and wrong in the silent direction. The economics
    bands are MODEL-BLIND absolute token counts; `is_rotation_due` is
    model-AWARE via `resolve_ceiling`. On claude-haiku-4-5 (ceiling 200,000)
    the first actionable band does not arrive until 150,000, so a pure-band
    definition returns False at 100,000 -- which is that model's OWN halfway
    point, and the exact value the fraction hint exists to catch. It would
    have made `rotation_due` strictly LATER on every small-ceiling model while
    looking like a pure improvement on the 1M models everyone tests on.

    The three assertions are ordered so the test NAMES that mutation rather
    than merely failing under it: the band at 100,000 is shown to be
    non-actionable FIRST, so a reader can see that the True below cannot be
    coming from the band half.
    """
    ceiling = rt.resolve_ceiling("claude-haiku-4-5")
    _check(ceiling == 200_000, "the decoy is stated against a real 200K-ceiling model")
    _check(
        rt.rotation_band(100_000, cache_cold=False)[0] == "warm_keep",
        "at 100,000 the ECONOMICS band still says keep working -- so a pure-band "
        "definition of rotation_due returns False here",
    )
    _check(
        rt.is_rotation_due(
            model="claude-haiku-4-5", current_tokens=100_000, cache_cold=False,
        ) is True,
        "...and rotation_due is True anyway, because 100,000 is 0.5 of this model's "
        "OWN 200,000 ceiling -- the fraction half is load-bearing, not vestigial",
    )
    _check(
        rt.is_rotation_due(
            model="claude-haiku-4-5", current_tokens=99_999, cache_cold=False,
        ) is False,
        "one token below its own halfway point is NOT due -- the fraction half still "
        "has a real boundary, it was not widened into always-true",
    )


def test_the_union_can_only_ever_notify_more() -> None:
    """MONOTONICITY, over a grid rather than by argument.

    The union is `band_is_actionable OR fraction >= THRESHOLD`, so it is a
    strict superset of the predicate it replaces and no session that was
    served a notice before can lose one. That property is what makes this
    landable on a live shared surface without a migration, so it is asserted
    against the historical definition INLINE -- reproducing the old rule here
    rather than citing it, because a monotonicity claim checked against the
    new code would be checking the change against itself.
    """
    for model in ("claude-opus-5", "claude-haiku-4-5", "a-model-nobody-added"):
        ceiling = rt.resolve_ceiling(model)
        for tokens in (0, 1, 49_999, 50_000, 99_999, 100_000,
                       rt.POLICY_H_TOKENS, _DISCRIMINATING_TOKENS,
                       149_999, 150_000, 199_999, 200_000, 299_999, 300_000,
                       499_999, 500_000, 750_000, 900_000, 1_500_000):
            for cold in (True, False):
                historical = tokens >= ceiling * rt.ROTATION_THRESHOLD_FRACTION
                now = rt.is_rotation_due(
                    model=model, current_tokens=tokens, cache_cold=cold,
                )
                if historical and not now:
                    _check(
                        False,
                        f"MONOTONICITY BROKEN: {model} at {tokens:,} cold={cold} was "
                        f"due under the old fraction rule and is not due now",
                    )
                    return
    _check(True, "across 3 ceilings x 19 sizes x 2 cache states, every session the old "
                 "fraction rule called due is still due -- the union only ever adds")


def test_capacity_can_never_be_the_reason_a_session_is_due() -> None:
    """WHY THE UNION IS TWO-TERM AND NOT THREE -- enforced, not asserted in a
    comment.

    There are two model-aware quantities in this module, not one: the fraction
    hint at 0.5 and CAPACITY_BAND_APPROACHING_FRACTION at 0.75. Folding the
    capacity axis into the union looks like the more complete answer and buys
    exactly nothing, because 0.75 > 0.5 means the fraction term has already
    fired everywhere capacity could -- on EVERY ceiling, since both are
    fractions of the same denominator.

    Pinned as a test rather than written as a comment because it is a
    CONDITIONAL fact: it holds only while that inequality does. If anyone ever
    lowers the capacity fraction below the rotation hint, the union silently
    stops covering the capacity axis and no comment would say so. This breaks
    loudly instead.
    """
    _check(
        rt.CAPACITY_BAND_APPROACHING_FRACTION > rt.ROTATION_THRESHOLD_FRACTION,
        "the first capacity band (0.75) sits ABOVE the rotation fraction hint (0.5), "
        "so the fraction term fires first on every ceiling -- this inequality is what "
        "makes the two-term union complete, and the union must gain a capacity term "
        "if it is ever reversed",
    )
    for model in ("claude-opus-5", "claude-haiku-4-5", "a-model-nobody-added"):
        ceiling = rt.resolve_ceiling(model)
        approaching = int(ceiling * rt.CAPACITY_BAND_APPROACHING_FRACTION)
        _check(
            rt.capacity_band(approaching, ceiling)[0] == "capacity_approaching",
            f"{model}: {approaching:,} is where capacity first asks for a rotation",
        )
        _check(
            rt.is_rotation_due(
                model=model, current_tokens=approaching, cache_cold=False,
            ) is True,
            f"{model}: it is already due there for a reason that is NOT capacity",
        )


def test_a_cold_cache_above_h_is_due_where_the_same_size_warm_is_not() -> None:
    """The cold branch reaches the union, and it is a DIFFERENT question.

    `_DISCRIMINATING_TOKENS` is the size this file already identified for
    `rotation_band` (strictly between H and the first warm band) and it
    discriminates here for the same reason: cold it is `cold_above_h`
    and actionable, warm it is `warm_keep` and not, and the fraction is 0.12
    on a 1M ceiling so the fraction half cannot be what decides either answer.
    A union that dropped the cache axis would return False for both and this
    is the only size that would notice.
    """
    _check(
        rt.is_rotation_due(
            model="claude-opus-5", current_tokens=_DISCRIMINATING_TOKENS, cache_cold=True,
        ) is True,
        "the discriminating size with a COLD cache is due -- the context is "
        "re-paid at full price "
        "on the next call whether or not you rotate",
    )
    _check(
        rt.is_rotation_due(
            model="claude-opus-5", current_tokens=_DISCRIMINATING_TOKENS, cache_cold=False,
        ) is False,
        "the same size WARM is not due -- same size, opposite verdicts by cache "
        "state, and the fraction is identical in both",
    )


def test_unmeasured_cache_state_is_treated_as_warm_never_upgraded_to_cold() -> None:
    """`cache_cold=None` means NOT MEASURED, and it must not be promoted.

    A reporter predating cache attribution sends no cache state at all, and
    `_rotation_prose` already says in the delivered notice that such a band is
    "the warm default rather than a measurement". Reading None as cold would
    be the loud direction, but it would manufacture a measurement nobody took
    and make a session in the H-to-150,000 window due on the strength of an
    absent field -- the same manufactured-freshness pathology that got a
    detached gauge heartbeat refused on this lane. Quiet-but-honest is the
    ruling; the notice already carries the caveat that goes with it.
    """
    _check(
        rt.is_rotation_due(
            model="claude-opus-5", current_tokens=_DISCRIMINATING_TOKENS, cache_cold=None,
        ) is False,
        "an UNMEASURED cache state answers exactly as warm does, not as cold",
    )
    _check(
        rt.is_rotation_due(
            model="claude-opus-5", current_tokens=_DISCRIMINATING_TOKENS, cache_cold=None,
        ) == rt.is_rotation_due(
            model="claude-opus-5", current_tokens=_DISCRIMINATING_TOKENS, cache_cold=False,
        ),
        "...and it is the SAME answer as warm at the one size where warm and cold "
        "differ -- so None is the warm default, not a third behaviour",
    )


def test_overage_cannot_change_which_band_a_size_falls_in() -> None:
    """Why the union needs no `overage` argument -- checked, not assumed.

    `rotation_band` takes `overage`, so leaving it out of the rotation-due
    predicate looks like a dropped input. It is not: `overage` reaches only
    `_horizon_prose`, i.e. the break-even HORIZON quoted inside the guidance
    string, and never the band NAME. Since the union decides on the band name,
    overage cannot move the decision. Asserted over a grid so the omission is
    a measured fact about the code rather than a claim about it -- if anyone
    ever makes a band boundary overage-dependent, the union acquires a real
    missing input and this test says so.
    """
    for tokens in (0, _BELOW_H_TOKENS, rt.POLICY_H_TOKENS, _DISCRIMINATING_TOKENS,
                   149_999, 150_000, 200_000,
                   250_000, 300_000, 500_000):
        for cold in (True, False):
            nominal = rt.rotation_band(tokens, cache_cold=cold, overage=False)[0]
            collapsed = rt.rotation_band(tokens, cache_cold=cold, overage=True)[0]
            if nominal != collapsed:
                _check(
                    False,
                    f"overage MOVED a band at {tokens:,} cold={cold}: "
                    f"{nominal} -> {collapsed}; the union now needs an overage input",
                )
                return
    _check(True, "across 10 sizes x 2 cache states the collapsed-TTL premium changes the "
                 "quoted horizon but never the band NAME -- so it cannot change whether "
                 "a session is rotation-due")


def test_the_two_entry_points_agree_on_the_models_own_ceiling() -> None:
    """The ceiling-taking core and the model-taking delegate are ONE predicate.

    Two entry points exist so that a caller holding a STORED ceiling decides on
    the same denominator it prints (`session_context_status` and
    `_rotation_due_row` both compute a fraction from the stored ceiling and put
    that fraction in the notice) -- deciding on `resolve_ceiling(model)` while
    printing a stored-ceiling fraction would attach a true number to the wrong
    noun. This pins that they cannot drift apart where the two ceilings agree,
    which is the only place the equivalence is even claimed.
    """
    for model in ("claude-opus-5", "claude-haiku-4-5", "a-model-nobody-added"):
        ceiling = rt.resolve_ceiling(model)
        for tokens in (0, 99_999, 100_000, 149_999, 150_000, 299_999,
                       300_000, 499_999, 500_000, 900_000):
            for cold in (True, False, None):
                by_model = rt.is_rotation_due(
                    model=model, current_tokens=tokens, cache_cold=cold,
                )
                by_ceiling = rt.is_rotation_due_for_ceiling(
                    ceiling=ceiling, current_tokens=tokens, cache_cold=cold,
                )
                if by_model != by_ceiling:
                    _check(
                        False,
                        f"the two entry points DISAGREE for {model} at {tokens:,} "
                        f"cold={cold}: model-form {by_model}, ceiling-form {by_ceiling}",
                    )
                    return
    _check(True, "across 3 models x 10 sizes x 3 cache states the model-taking delegate "
                 "and the ceiling-taking core return the same verdict -- one predicate, "
                 "two entry points, not two predicates")


def test_the_frozen_out_of_tree_call_shape_still_works() -> None:
    """★ THE CROSS-COPY CONTRACT, and it is not hypothetical.

    The rotation hook exists in a THIRD copy this repository cannot edit: an
    installed plugin-cache copy under ~/.claude/plugins/cache (GAU-04). It
    imports THIS module out of the live checkout while carrying its own frozen
    call site, so this function's signature is a published interface across a
    boundary no landing here can migrate. Measured 2026-08-18: seven such
    copies present, the marketplace plugin enabled in settings, every copy
    calling the two-argument shape below.

    Requiring `cache_cold` would therefore have converted each of that copy's
    PostToolUse ticks into a TypeError. This pins the shape those copies
    actually call -- and pins that they inherit the union through it, rather
    than merely surviving it, which is the whole reason the default is `None`
    and not something that changes the answer.
    """
    _check(
        rt.is_rotation_due(model="claude-opus-5", current_tokens=350_000) is True,
        "the frozen two-argument call shape still resolves -- AND it now returns "
        "True at 350,000, so a copy nobody can update inherits the fix",
    )
    _check(
        rt.is_rotation_due(model="claude-opus-5", current_tokens=10_000) is False,
        "...without becoming always-true: the same frozen shape still says no "
        "where neither axis fires",
    )
    _check(
        rt.is_rotation_due(model="claude-opus-5", current_tokens=_DISCRIMINATING_TOKENS)
        == rt.is_rotation_due(
            model="claude-opus-5", current_tokens=_DISCRIMINATING_TOKENS, cache_cold=None,
        ),
        "...and omitting the argument is EXACTLY the unmeasured case, not a "
        "fourth behaviour: an absent argument means nobody measured, which is "
        "what None already means",
    )


def test_the_ceiling_form_refuses_a_non_positive_ceiling() -> None:
    """A missing ceiling is REFUSED, never defaulted -- the same posture
    `capacity_band` states for itself, and for the same reason: defaulting one
    here would answer a question about a specific model's window with a number
    nobody measured, and it would do it inside the predicate that decides
    whether a live session gets told to rotate.

    The model-taking form cannot reach this branch (`resolve_ceiling` always
    returns a positive ceiling, falling back to DEFAULT_CONSERVATIVE_CEILING),
    which is exactly why the refusal has to be tested through the core.
    """
    for bad in (0, -1):
        try:
            rt.is_rotation_due_for_ceiling(
                ceiling=bad, current_tokens=400_000, cache_cold=False,
            )
        except ValueError:
            _check(True, f"a ceiling of {bad} is refused rather than defaulted")
        else:
            _check(False, f"a ceiling of {bad} was ACCEPTED -- it must be refused")


def test_rotation_notice_boundaries_and_sources() -> None:
    """The operator rule is one derived boundary, never an economics band."""
    def verdict(
        *, model: str, window: int | None, tokens: int, hard: int = 400_000,
    ) -> rt.RotationNoticeDecision:
        return rt.rotation_notice_verdict(
            model=model, effort="high", current_tokens=tokens,
            runtime_window_tokens=window, configured_hard_clear_tokens=hard,
        )

    _check(not verdict(model="claude-fable-5", window=1_000_000, tokens=319_999).due,
           "319,999 is below the 400K hard-clear-derived notice")
    _check(verdict(model="claude-fable-5", window=1_000_000, tokens=320_000).due,
           "320,000 is the one 80% notice point")
    _check(not verdict(model="claude-fable-5", window=1_000_000, tokens=399_999, hard=500_000).due,
           "raising hard clear to 500K moves the boundary, not a copied literal")
    _check(verdict(model="claude-fable-5", window=1_000_000, tokens=400_000, hard=500_000).due,
           "500K hard clear notices at 400K")
    _check(not verdict(model="claude-haiku-4-5", window=200_000, tokens=143_999).due,
           "200K runtime window is quiet at 143,999")
    haiku = verdict(model="claude-haiku-4-5", window=200_000, tokens=144_000)
    _check(haiku.due and haiku.notice_at_tokens == 144_000,
           "200K runtime window uses the 180K compaction bound and notices at 144K")
    unknown = verdict(model="unrecognised", window=None, tokens=320_000)
    _check(unknown.due and unknown.unrecognized_model and unknown.window_tokens is None,
           "unknown model uses hard-clear fallback and surfaces that it was unrecognised")


def main() -> int:
    print("=== rotation_thresholds smoke ===")
    test_table_carries_exactly_the_seat_confirmed_keys()
    test_real_aliases_resolve_to_their_measured_ceilings()
    test_plausible_future_model_still_falls_back_conservative()
    test_unknown_model_resolves_to_conservative_fallback()
    test_known_model_resolves_to_its_own_ceiling_not_the_fallback()
    test_watch_threshold_constants_match_ratified_values()
    test_poke_threshold_is_strictly_below_rotate_threshold()
    test_rotate_threshold_is_margined_below_cache_ttl_floor()
    test_h_is_the_sum_of_its_measured_parts()
    test_warm_bands_are_ordered_and_exhaustive()
    test_band_boundaries_are_inclusive_where_the_policy_says_so()
    test_cold_is_a_different_question_not_a_stricter_band()
    test_the_discriminating_window_is_real()
    test_clearing_wins_needs_calls_to_amortise_over()
    test_the_superseded_fraction_survives_as_a_hint_only()
    test_post_clear_rewrite_is_not_evidence_of_a_cold_cache()
    test_isolated_cold_call_is_the_resolved_idle_case()
    test_repeated_cold_calls_across_short_gaps_are_the_overage_signature()
    test_ttl_lapse_is_caught_even_when_the_last_call_read_cache()
    test_transcript_parser_refuses_a_naive_timestamp()
    test_overage_cannot_change_which_band_a_size_falls_in()
    test_rotation_notice_boundaries_and_sources()
    test_the_ceiling_form_refuses_a_non_positive_ceiling()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
