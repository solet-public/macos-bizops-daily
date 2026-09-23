# Operator Decision Briefs: the Plain-Language Convention for Asking the Operator to Rule

Tags: knowledge:tag:operator_communication, knowledge:tag:decision_briefs, knowledge:tag:governance

Article Layer: 1

Article Role: platform_constraint

Article Tags: planning-stage:always, evidence-category:constraint, domain:operator_communication, consumer_profile:both

Embedding Description: The mandatory plain-English format for any communication that asks the operator to decide, approve, or rule. Covers the eight brief parts of decision, why now, checked first, premises, options, recommendation, the ask, and deadline with default, plus the three rules of codename expansion, freshness marking, and never escalating settled questions, and the enforcement model where whoever forwards a brief gates its quality.

**When you need this**: drafting any message that asks the operator for a ruling or an approval; deciding whether a question even needs the operator or is already answered by a standing ruling; reviewing a decision request before it reaches the operator; understanding why a decision request was bounced back with a request to rewrite it.

---

## The problem this convention fixes

Agent communication to an operator drifts, over time, into a private language of codenames and compressed status jargon. When an agent then asks for a ruling, the operator is forced to interrogate at length before even understanding the question. Two compounding failure modes ride along:

1. **Settled-question escalations**: asking the operator to decide something an existing rule, ruling, or convention already decides.
2. **Imagined-context plans**: elaborate proposals built on stale or unverified premises, where the ruling requested is about a world that no longer exists.

The root defect is that written state fails to declare its audience and its freshness. Agent-to-agent shorthand is fine between agents; the moment text asks a human to decide, it must be self-contained plain language whose factual claims carry their verification. This convention is the operator-facing half of that fix.

## Scope

This convention is platform-wide and governs the operator relationship of every solet. It was adopted by operator ruling on 2026-07-18 and ships in the platform knowledge base so newborn solets apply it with their own operators from day one.

It applies to every communication that asks the operator to decide, approve, or rule: peer messages addressed to the operator, coordinator reports with a section that needs the operator's eyes, morning summaries that end in questions, and written proposals awaiting ratification.

It does not apply to agent-to-agent traffic. Dispatch briefs between agents are governed by the coordinator dispatch discipline in `ananta/knowledge_base/coordinator_dispatch_discipline.md`, though the premise-verification discipline below is good practice there too.

## The brief format

Every decision request contains these parts, in this order, in plain English:

| Part | Content |
|---|---|
| **DECISION** | One sentence stating what is being decided. No unexpanded codenames. |
| **WHY NOW** | Why this surfaced today, meaning what completed, broke, or changed. Two to four sentences. |
| **CHECKED FIRST** | What was consulted before escalating, stated auditably rather than asserted: the knowledge-base query strings run, the memories, files, and rulings reviewed, the specific top hit or ruling that did or did not answer the question, and the explicit claim "no existing ruling answers this because ...". |
| **PREMISES** | The load-bearing facts this decision rests on, each with its verification, for example "confirmed against the live tree on \<date\>". A premise nobody verified is labeled as such, and a load-bearing unverified premise cannot support a recommendation; if one exists, the only valid recommendation is "verify it first". |
| **OPTIONS** | Each realistic branch and what concretely happens on it: cost, risk, what it unblocks. Two or three options, not a survey. |
| **RECOMMENDATION** | Exactly one, argued in one to three sentences. |
| **THE ASK** | The smallest possible response that unblocks work, ideally one word or one choice. |
| **DEADLINE / DEFAULT** | When a ruling is needed by, and what happens if none arrives. The usual default is "nothing proceeds until you rule"; stating it explicitly prevents hidden urgency. |

A brief the operator can rule on from the brief alone, in under two minutes, is the bar. If ruling requires opening a file, asking a follow-up, or remembering a codename, the brief failed.

## The three rules

Three rules keep operator-facing text decidable, and each is cheap to apply at writing time.

1. **Codename expansion.** First use of any lane, ticket, or codename in operator-facing text expands it in place. After the first expansion the short form is fine.
2. **Freshness marking.** Every status claim carries its as-of date and source, for example "all smokes green (run 2026-07-18, output in the lane record)", never a bare "smokes are green".
3. **Settled questions don't get sent.** If CHECKED FIRST turns up a rule or ruling that answers the question, the brief is not sent; the work record cites the ruling and work proceeds. Escalating anyway, "just to confirm", is ratification theater and is prohibited.

## Enforcement

Whoever forwards a brief owns its quality. In a multi-session fleet, coordinators are the gate: a peer's decision request that does not meet this format is bounced back to the peer, not forwarded to the operator. A coordinator-authored brief follows the same checklist with no exemption; a high-impact ask gets a second pass from another session, or carries an explicit self-gated marker so the bypass is at least visible. In a single-session deployment the authoring session gates itself against the same checklist before sending.

The operator's bounce phrase closes the loop. Any non-compliant brief may be answered with just: *"rewrite this so I can decide from it alone."* No further explanation is owed, and operators are encouraged to use it liberally; it is the cheapest available training signal.

## Worked example

A generic example in the format, sized the way a real brief should be:

> **DECISION:** Whether to rotate the expiring calendar-integration credential now, with a brief interruption, or defer to the weekend maintenance window.
> **WHY NOW:** The provider emailed an expiry notice today; the credential dies in six days. Rotation takes about ten minutes, during which calendar queries fail.
> **CHECKED FIRST:** Knowledge-base search "credential rotation policy" (top hit: the vault conventions article, which covers storage but not timing); no standing ruling fixes rotation timing.
> **PREMISES:** Expiry date confirmed against the provider dashboard today; no scheduled calendar-dependent jobs before the weekend, confirmed against the scheduler.
> **OPTIONS:** (a) Rotate now: risk retired immediately, ten-minute interruption at a low-traffic hour. (b) Weekend window: zero perceived interruption, but five days of exposure to a forgotten-rotation failure.
> **RECOMMENDATION:** (a), because the interruption is trivial and the failure mode of (b) is silent breakage.
> **THE ASK:** "now" or "weekend".
> **DEADLINE / DEFAULT:** Ruling needed within five days; default is (b), already scheduled.

## Reference

- `knowledge_bases/ananta_platform/24_operator_communication/02_solet_charter_template.md` — the charter template whose governance section this convention operationalizes.
- `knowledge_bases/ananta_platform/24_operator_communication/03_collaboration_craft.md` — the broader operator-collaboration craft this convention is one part of.
- `ananta/knowledge_base/coordinator_dispatch_discipline.md` — the agent-to-agent counterpart of this operator-facing convention.
- `service_interface::knowledge_service::search` — the search surface CHECKED FIRST evidence comes from.
