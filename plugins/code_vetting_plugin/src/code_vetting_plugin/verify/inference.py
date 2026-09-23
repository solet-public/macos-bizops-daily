"""Wave-2 inference substrate for the L3 refute-harness (design brief §3.3, §5c).

The intended L3 adversary is *inference*: N independent reviewer sessions, each
seeded with one lens's rulebook refute prompt, dispatched over the platform's
peer-messaging bridge (``peer_send`` / ``peer_inbox`` — subscription-covered,
no metered key). This module is the inference side of the pluggable
:class:`~.dispatch.SkepticDispatcher` seam; it slots in with zero change to
:mod:`.verifier`.

**Why a transport seam.** The peer-messaging bridge tools are MCP tools the
orchestrating *agent* invokes; a standalone Python process cannot call them, and
a dispatched peer message is asynchronous (its reply arrives via a peer_inbox
notification, not a return value). So the dispatcher does not embed the bridge
call — it depends on a :class:`SkepticTransport` (`prompt → raw reply text`). The
orchestrator drives the actual bridge inference and supplies the replies through
the transport; :class:`RecordedTransport` replays gathered replies so a live run
is reproducible and the parse→aggregate path is exercised deterministically. A
future in-process ``BridgeTransport`` (once a Python bridge client exists) is a
drop-in for the same Protocol.

**Precision bias.** A reply whose vote cannot be read is ``UNCERTAIN`` — which
the aggregator counts toward refutation. Unreadable skepticism is treated as
doubt, and doubt drops the finding.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..toolrun import ToolOutcome
from ..toolrun import run as run_tool
from .dispatch import SkepticRequest, SkepticResponse, SkepticVote
from .lenses import SkepticLens

_DEFAULT_CLAUDE_BIN = "claude"
_DEFAULT_ALLOWED_TOOLS = "Read,Grep,Glob"
_DEFAULT_SKEPTIC_TIMEOUT_S = 300

_VOTE = re.compile(r"vote\s*[:=]\s*\**\s*(REFUTE|UPHOLD|UNCERTAIN)", re.IGNORECASE)
_VOTE_BARE = re.compile(r"\b(REFUTE|UPHOLD|UNCERTAIN)\b")
_DISPOSITIVE = re.compile(r"dispositive\s*[:=]\s*\**\s*(true|yes|false|no)", re.IGNORECASE)
_RULE_ID = re.compile(r"rule_id\s*[:=]\s*\**\s*([^\n*]+)", re.IGNORECASE)
_RATIONALE = re.compile(r"rationale\s*[:=]\s*\**\s*([^\n]+)", re.IGNORECASE)
_NULLISH: frozenset[str] = frozenset({"", "null", "none", "n/a", "na", "-"})


class TransportLocality(StrEnum):
    """Where a skeptic transport actually sends the review — the GROUND TRUTH for
    redaction (RIDER-1). ``off_operator`` is derived from THIS, never from a substrate
    label, so a transport cannot be mislabeled into forwarding raw evidence off-machine.
    """

    ON_MACHINE = "on_machine"
    """Runs on the operator's machine; code never leaves it (local models, replay)."""
    OFF_MACHINE = "off_machine"
    """Forwards to a remote substrate — subscription (claude -p → Anthropic) or bridge."""


class SkepticTransport(Protocol):
    """`prompt → raw reply text`. The bridge (or a replay of it) plugs in here."""

    @property
    def locality(self) -> TransportLocality:
        """ON/OFF machine — the ground truth that drives off-operator redaction (RIDER-1)."""
        ...

    def infer(self, request: SkepticRequest) -> str: ...


def _parse_vote(text: str) -> SkepticVote:
    match = _VOTE.search(text) or _VOTE_BARE.search(text)
    if match is None:
        return SkepticVote.UNCERTAIN
    return SkepticVote(match.group(1).lower())


def _parse_dispositive(text: str) -> bool:
    match = _DISPOSITIVE.search(text)
    return match is not None and match.group(1).lower() in {"true", "yes"}


def _parse_rule_id(text: str) -> str | None:
    match = _RULE_ID.search(text)
    if match is None:
        return None
    value = match.group(1).strip()
    return None if value.lower() in _NULLISH else value


def _parse_rationale(text: str) -> str:
    match = _RATIONALE.search(text)
    if match is not None:
        return match.group(1).strip()
    return text.strip()[:400]


def parse_skeptic_reply(text: str, lens: SkepticLens) -> SkepticResponse:
    """Parse one skeptic's structured reply into a :class:`SkepticResponse`.

    An unreadable vote resolves to ``UNCERTAIN`` (precision bias). A dispositive
    flag is only honored on a REFUTE — an "uphold, dispositive" is not a kill.
    """
    vote = _parse_vote(text)
    dispositive = _parse_dispositive(text) and vote is SkepticVote.REFUTE
    return SkepticResponse(
        lens=lens,
        vote=vote,
        dispositive=dispositive,
        rationale=_parse_rationale(text),
        rule_id=_parse_rule_id(text),
    )


def reply_key(finding_id: str, lens: SkepticLens) -> str:
    """Stable key for a recorded (finding, lens) reply."""
    return f"{finding_id}::{lens.value}"


@dataclass(frozen=True, slots=True)
class RecordedTransport:
    """Replays gathered skeptic replies keyed by :func:`reply_key`.

    Fast-fail: a request with no recorded reply raises — every dispatched skeptic
    must be recorded, or the run is incomplete and must not silently proceed.
    """

    replies: Mapping[str, str]

    @property
    def locality(self) -> TransportLocality:
        return TransportLocality.ON_MACHINE  # replay only — nothing is forwarded anywhere live

    def infer(self, request: SkepticRequest) -> str:
        key = reply_key(request.finding.finding_id, request.lens)
        if key not in self.replies:
            raise KeyError(f"no recorded skeptic reply for {key}")
        return self.replies[key]


@dataclass(frozen=True, slots=True)
class SubprocessSkepticTransport:
    """Automated live transport: one read-only ``claude -p`` subprocess per skeptic.

    The design's cost model (design brief §5c) — orchestrate the already-paid-for
    Claude Code subscription, $0 marginal — for when the peer-messaging bridge
    is unavailable. Reuses the vetting suite's shared ``toolrun`` runner
    (no reinvented subprocess handling). ``--allowedTools Read,Grep,Glob`` grants NO
    Bash/Edit/Write, so a skeptic structurally cannot run git or mutate the
    worktree — the Git-Controller policy is honored by construction. A skeptic that
    times out returns a no-verdict marker → parsed as ``UNCERTAIN`` (precision
    bias), never a silent uphold; a missing ``claude`` binary fails loud (toolrun
    raises). ``runner`` is injectable so the dispatch path is testable without a
    live subprocess. Invoked without a shell, so ``claude`` resolves to the real
    binary on PATH (no shell alias) — a clean, isolated reviewer.
    """

    cwd: str
    claude_bin: str = _DEFAULT_CLAUDE_BIN
    allowed_tools: str = _DEFAULT_ALLOWED_TOOLS
    timeout_s: int = _DEFAULT_SKEPTIC_TIMEOUT_S
    runner: Callable[..., ToolOutcome] = run_tool

    @property
    def locality(self) -> TransportLocality:
        return TransportLocality.OFF_MACHINE  # claude -p → Anthropic; the review leaves the machine

    def infer(self, request: SkepticRequest) -> str:
        argv = [self.claude_bin, "-p", request.prompt, "--allowedTools", self.allowed_tools]
        outcome = self.runner(argv, cwd=self.cwd, timeout_s=self.timeout_s, raise_on_timeout=False)
        if outcome.timed_out:
            return f"SKEPTIC TIMED OUT after {self.timeout_s}s — no verdict produced"
        return outcome.stdout if outcome.stdout.strip() else outcome.stderr


@dataclass(frozen=True, slots=True)
class LocalInferenceSkepticTransport:
    """Binding-2 substrate (A0 R3): the platform's OWN inference service — local models.

    The overflow + privacy absorber. When subscription sessions throttle, or when a
    privacy-profile run requires that code never leave the machine, the skeptic runs
    on the platform-configured local model (LM Studio / Ollama). Because the model is
    local, this is NOT an off-operator forward — the selector leaves ``off_operator``
    False, so the prompt carries full evidence (redacting a local reviewer would blind
    it for nothing).

    Wraps an injected ``infer_fn`` (prompt -> completion text) so the dispatch path is
    testable without a live inference service (mirrors :class:`SubprocessSkepticTransport`'s
    injectable ``runner``); the plugin/joseki supplies a closure over
    ``inference_service.generate_completion`` (``use_structured_output=False`` for a
    free-text skeptic reply). A metered API key is STRUCTURALLY absent (A0 R3 binding 3):
    the local model is configured platform-side, and this transport declares NO key/token
    field — there is nothing here that could accept one.
    """

    infer_fn: Callable[[str], str]

    @property
    def locality(self) -> TransportLocality:
        return TransportLocality.ON_MACHINE  # local models; code never leaves the machine

    def infer(self, request: SkepticRequest) -> str:
        reply = self.infer_fn(request.prompt)
        return reply if reply.strip() else "LOCAL INFERENCE RETURNED EMPTY — no verdict produced"


@dataclass(frozen=True, slots=True)
class InferenceSkepticDispatcher:
    """Dispatches each request through the transport and parses the reply.

    Drop-in for :class:`~.dispatch.SkepticDispatcher`. Compose it *after* the
    heuristic pre-screen so inference only adjudicates the ambiguous residue.
    """

    transport: SkepticTransport

    def evaluate_batch(self, requests: Sequence[SkepticRequest]) -> list[SkepticResponse]:
        return [parse_skeptic_reply(self.transport.infer(request), request.lens) for request in requests]
