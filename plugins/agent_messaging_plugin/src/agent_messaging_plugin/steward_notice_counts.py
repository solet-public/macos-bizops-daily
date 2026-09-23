"""The DETECTED-versus-DELIVERED counts a steward-notice sweep leg reports.

Its own module rather than a block inside ``session_sweep`` for two reasons,
one structural and one measured. Structurally it is shared vocabulary: three
legs in ``session_sweep`` fill it and the rider in ``plugin.py`` renders it, so
it belongs to neither. Measured: ``session_sweep`` ranks MI **B at 9.40** on the
gate's own invocation, which is close enough to the C floor that adding this
type inline pushed the whole module to **C at 7.48** -- a gate red caused by
where the code sat rather than by what it did.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StewardNoticeCounts:
    """What one steward-notice leg DETECTED versus what it DELIVERED (GAU-25).

    ★ THE DEFECT THIS EXISTS FOR, measured 2026-08-19 17:47Z. Three legs below
    (:func:`sweep_rotation_due_sessions`, :func:`sweep_gauge_coverage`,
    :func:`sweep_gauge_staleness`) return ONE number, and that number is the
    count of notices whose DELIVERY SUCCEEDED. A detection whose steward binding
    does not resolve is recorded durably and counted nowhere, so the
    operator-facing sweep line scored ``L4d=0`` on the very tick that caught a
    real arrested gauge -- confirmed by two independent instruments against a
    line that said nothing happened. ``L4d=0`` was therefore consistent with any
    number of real, undelivered detections, and the line could not answer the
    only question a reader brings to it: DID THE DETECTOR FIRE.

    So this carries both numbers, and no reading of the new line can collapse
    them back together:

    * ``detected`` -- sessions matching the leg's condition this tick, INCLUDING
      the ones a latch then suppressed. This is the detector's own output, and
      it is the number that was previously unobservable.
    * ``delivered`` -- of the non-suppressed ones, how many reached a steward.
      This is exactly the legacy return value, unchanged in meaning, so nothing
      that already reads the int reads it differently now.
    * ``undelivered`` -- non-suppressed detections whose notify attempt FAILED.
      This is the population GAU-25 was blind to, and it is the actionable one:
      a non-zero value here is an alarm nobody received.

    ``detected - delivered - undelivered`` is the latch-suppressed remainder.
    It is deliberately NOT a field: a session suppressed this tick was, by the
    latch's own definition, already reported in an earlier one, so promoting it
    to a headline number would invite a reader to double-count one episode.

    WHY A CALLER-SUPPLIED SINK rather than a richer return type. The three legs'
    ``int`` return is read at ~86 call sites across four test modules, three of
    which belong to other lanes' leases and one of which monkeypatches the legs
    with ``lambda *a, **k: 0``. Changing the return type would break all of them
    for a cosmetic gain. The sink adds the second number without touching a
    single existing reader, and it is a SINK rather than a second scan on
    purpose: two scans of the same condition can disagree, and an instrument
    that can disagree with itself is the thing this entry exists to stop.
    """

    detected: int = 0
    delivered: int = 0
    undelivered: int = 0

    @property
    def actionable(self) -> int:
        """Detections this tick that were not latch-suppressed.

        This is what gates the per-leg WARNING escalation, and the choice is
        load-bearing in both directions. Gating on ``delivered`` (the pre-GAU-25
        behaviour) means a tick with three detections and zero deliveries warns
        about NOTHING -- the same defect as the counter, one level up. Gating on
        ``detected`` would warn every 300s for the whole of a long outage, which
        is precisely what the latch exists to prevent. ``delivered +
        undelivered`` warns on a fresh detection and on a failed delivery, and
        stays quiet across an outage already reported.
        """
        return self.delivered + self.undelivered


def fill_counts(
    counts: StewardNoticeCounts | None,
    *,
    detected: int,
    delivered: int,
    undelivered: int,
) -> None:
    """Fill a caller-supplied :class:`StewardNoticeCounts`, or do nothing.

    A ``None`` sink is the legacy caller that only ever wanted the ``int``, and
    it is a deliberate no-op rather than a constructed-and-discarded object:
    the three legs must stay byte-equivalent for a caller that does not opt in.
    Spelled once here rather than as three ``if counts is not None`` blocks so a
    fourth leg cannot half-adopt it -- the shape that produced GAU-25 in the
    first place was three copies of one pattern with the important line missing
    from the summary.
    """
    if counts is None:
        return
    counts.detected = detected
    counts.delivered = delivered
    counts.undelivered = undelivered
