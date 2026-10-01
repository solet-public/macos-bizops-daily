"""iss_67472e3f, iss_49f37c32 (r66): no seed text producer can emit text the Manager's ``public_string`` refuses, and a long repair is cut visibly.

A text that breaks one ``public_string`` rule (a Homebrew keg path, secret-shaped text, an oversize or empty string) makes the Manager refuse the WHOLE
adapter envelope; r65 lanes 3 and 6 each shipped such text and only review caught it.  This gate drives every seed producer of envelope text with a
hostile corpus and parses the result with the Manager's REAL ``OperationResult.from_dict``:

- ``result()`` (repair), ``evidence()`` (status, summary, source, observed, expected), ``planned_action()`` (title, target, evidence ref), the
  ``describe_outcome`` repair text, the ``command_failure_reason`` stderr diagnostic, the model-doctor candidate, and the ``bootstrap_adapter``
  twins of the first three; each hostile text is first refused RAW by the same validator as a control, so the corpus is proven hostile;
- the ``bootstrap_adapter`` twin of ``public_text`` agrees with the plugin's on the whole corpus at every field limit;
- a census of the shipped seed sources finds every producer that builds an envelope field outside the choke points (the model-doctor candidate, the
  plugin-transition summary) and requires its text to pass through ``public_text``; a synthetic offender proves the census fires;
- a repair longer than ``REPAIR_LIMIT`` is cut in the middle behind a visible marker, keeps its head and its tail (the remedy is written last), and
  never exceeds the limit; a census of every statically resolvable repair template with each hole filled long proves each keeps the static text it ends
  with; the producers the issue names (``REPAIR_CLAUDE_CLI``, ``claude_plugin_list_failed``, the marketplace refusals) keep the command they tell the
  operator to run.
"""

from __future__ import annotations

import ast
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

_REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(_REPO / "plugins" / "github_midwife_plugin" / "src"), str(_REPO / "solet_cli" / "src"), str(_REPO / "solet_setup_contracts" / "src"), str(_REPO)]
import github_midwife_plugin.setup_adapter_contract as contract  # noqa: E402
import seed_public_text_gate_support as support  # noqa: E402
from github_midwife_plugin.existing_install_migrations import REPAIR_CLAUDE_CLI  # noqa: E402
from github_midwife_plugin.installation_model_doctor import _candidate  # noqa: E402, PLC2701
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, describe_outcome  # noqa: E402
from github_midwife_plugin.setup_operations import command_failure_reason  # noqa: E402
from solet_manager.adapter_protocol import OperationRequest, OperationResult  # noqa: E402
from solet_manager.adapter_validation import public_string  # noqa: E402
from solet_manager.errors import AdapterProtocolError  # noqa: E402

import bootstrap_adapter.protocol as boot  # noqa: E402

_CHECKS = 0
_SRC_ROOTS = (_REPO / "plugins" / "github_midwife_plugin" / "src" / "github_midwife_plugin", _REPO / "bootstrap_adapter")
_CHOKE_POINTS = {"setup_adapter_contract.py", "protocol.py"}
_REVISION = "a" * 40
_FINGERPRINT = "sha256:" + "b" * 64
_WIDE = "\U0001d518"  # 4 bytes in UTF-8
_KEG = "/opt/homebrew/Cellar/solet/0.1.0_64/libexec/bin/python3"
#: Every hostile text drives every field it can reach; each is refused raw by the real validator (the control) before the producer sees it.
_HOSTILE: dict[str, str] = {
    "keg path": f"python at {_KEG} exited 1",
    "password pair": "login failed: password=hunter2 for the account",
    "token colon": "rejected token: abc123def",
    "authorization header": "sent Authorization: Bearer abc.def-ghi",
    "bare bearer": "used bearer abc123 once",
    "uppercase bearer": "USED BEARER ABC123 ONCE",
    "uppercase flag": "ran with --TOKEN=ABC123",
    "private key": "private key: MIIBVgIBADANBg",
    "oversize ascii": "diagnosis " + "x" * 5000 + " then run `solet-manager update demo --dry-run` again.",
    "oversize wide": _WIDE * 3000,
    "empty": "",
    "secret inside a keg line": f"{_KEG} token=abc",
    "bearer then a label pair": "x Bearer token=abc",
    "oauth code": "retry with oauth code=zzz9",
    "authorization bearer": "Authorization: Bearer abc",
    "doubled bearer header": "Authorization: Bearer Bearer SEK1",
    "doubled bearer lowercase": "authorization:bearer bearer SEK2",
    "doubled bearer pair": "token=bearer bearer SEK3",
    "tripled bearer header": "Authorization: Bearer Bearer Bearer SEK4",
    "bare doubled bearer": "sent Bearer Bearer SEK5 once",
}
#: The secret each row carries: it must never appear in what a producer emits (a text can pass the Manager's rules and still leak, which is B1 of round 1).
_HOSTILE_SECRETS = {
    "password pair": "hunter2", "token colon": "abc123def", "authorization header": "abc.def-ghi", "bare bearer": "abc123", "uppercase bearer": "ABC123",
    "uppercase flag": "ABC123", "private key": "MIIBVgIBADANBg", "secret inside a keg line": "token=abc", "bearer then a label pair": "abc", "oauth code": "zzz9",
    "authorization bearer": "abc", "doubled bearer header": "SEK1", "doubled bearer lowercase": "SEK2", "doubled bearer pair": "SEK3", "tripled bearer header": "SEK4",
    "bare doubled bearer": "SEK5",
}
_FIELD_LIMITS = (256, 512, 2048, 4096)


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _request_pair(purpose: str = "preview") -> tuple[contract.AdapterRequest, OperationRequest]:
    request_id = str(uuid.uuid4())
    raw: dict[str, object] = {
        "protocol_version": 1, "kind": "operation_request", "request_id": request_id, "operation_id": "probe_demo", "operation_ref": "existing::probe.demo",
        "phase": "probe", "probe_purpose": purpose, "attempt": 1, "name": "demo", "target": "/Users/demo/Solets/demo", "flow_id": "existing-install",
        "flow_source_revision": _REVISION, "answers_fingerprint": _FINGERPRINT, "approval_fingerprint": None, "dry_run": True, "timeout_seconds": 30, "public_inputs": {},
    }
    manager = OperationRequest(request_id, "probe_demo", "existing::probe.demo", "probe", purpose, 1, "demo", "/Users/demo/Solets/demo", "existing-install", _REVISION, _FINGERPRINT, None, True, 30, {})
    return contract.AdapterRequest.from_dict(raw), manager


def _refused(envelope: dict[str, Any], manager: OperationRequest) -> bool:
    try:
        OperationResult.from_dict(cast(Any, envelope), manager)
    except AdapterProtocolError:
        return True
    return False


def _accepted(envelope: dict[str, Any], manager: OperationRequest, label: str) -> OperationResult:
    try:
        return OperationResult.from_dict(cast(Any, envelope), manager)
    except AdapterProtocolError as exc:
        raise AssertionError(f"{label}: the Manager refused the envelope: {exc}") from exc


def _contract_results(request: contract.AdapterRequest, text: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """One envelope per contract field the text can reach, built by the real producers."""

    def evidence(**fields: Any) -> dict[str, Any]:
        base: dict[str, Any] = {"evidence_id": "demo_fact", "kind": "demo", "status": "ok", "summary": "s", "observed": "o", "expected": "e", "source": "x"}
        return contract.result(request, status="verified", evidence_items=[contract.evidence(**{**base, **fields})])

    yield "repair", contract.result(request, status="blocked", error_kind="demo_blocked", repair=text)
    for field in ("status", "summary", "source", "observed", "expected"):
        yield f"evidence {field}", evidence(**{field: text})
    yield "evidence observed list", evidence(observed=[text, text + " "])
    yield "evidence expected list", evidence(expected=[text, text.upper() or "x"])
    action = contract.planned_action(action_id="demo.act", title=text, mutation_kind="file_write", target=text, evidence_ref=text)
    yield "planned action", contract.result(request, status="verified", actions=[action])
    yield "describe_outcome repair", contract.result(request, status="blocked", error_kind="demo_blocked", repair=describe_outcome(CommandOutcome(1, False, 1, "", text)))


def _reason_envelope(request: contract.AdapterRequest, text: str) -> dict[str, Any]:
    outcome = CommandOutcome(1, False, 1, "", text or "x")
    return contract.result(request, status="failed", error_kind="demo_failed", reason=command_failure_reason(outcome))


def _bootstrap_results(text: str) -> Iterator[tuple[str, dict[str, Any]]]:
    runtime = SimpleNamespace(now=lambda: datetime(2026, 10, 1, tzinfo=UTC))
    seed_request: dict[str, Any] = {"request_id": str(uuid.uuid4()), "operation_id": "probe_demo", "phase": "probe", "probe_purpose": "preview"}
    base: dict[str, Any] = {"evidence_id": "demo_fact", "kind": "demo", "status": "ok", "summary": "s", "observed": "o", "expected": "e", "source": "x"}
    yield "bootstrap repair", boot.result(seed_request, status="blocked", error_kind="demo_blocked", repair=text)
    for field in ("status", "summary", "source", "observed", "expected"):
        item = boot.evidence(runtime, **{**base, field: text})
        yield f"bootstrap evidence {field}", boot.result(seed_request, status="verified", evidence_items=[item])
    action = boot.planned_action("demo.act", text, "file_write", text, text)
    yield "bootstrap planned action", boot.result(seed_request, status="verified", planned_actions=[action])


def _bootstrap_manager(envelope: dict[str, Any]) -> OperationRequest:
    return OperationRequest(envelope["request_id"], "probe_demo", "existing::probe.demo", "probe", "preview", 1, "demo", "/Users/demo/Solets/demo", "existing-install", _REVISION, _FINGERPRINT, None, True, 30, {})


def _raw_control(text: str) -> None:
    """The corpus is hostile: the real validator refuses every text raw, in the field its limit applies to."""
    refused = False
    try:
        public_string(text, "control", maximum=512)
    except (TypeError, ValueError):
        refused = True
    wide_refused = False
    try:
        public_string(text, "control", maximum=4096)
    except (TypeError, ValueError):
        wide_refused = True
    _check(refused or wide_refused, f"control: the real public_string refuses {text[:30]!r} raw")


def _leg_producers() -> None:
    request, manager = _request_pair()
    for name, text in _HOSTILE.items():
        _raw_control(text)
        for field, envelope in _contract_results(request, text):
            _accepted(envelope, manager, f"{name} through {field}")
        _accepted(_reason_envelope(request, text), manager, f"{name} through the command_failure_reason stderr diagnostic")
        public_string(describe_outcome(CommandOutcome(1, False, 1, "", text)), f"{name} describe_outcome", maximum=512)
        for field, envelope in _bootstrap_results(text):
            _accepted(envelope, _bootstrap_manager(envelope), f"{name} through {field}")
    discovery, discovery_manager = _request_pair("decision_discovery")
    for name, text in _HOSTILE.items():
        candidate = _candidate("embedding_model", text, 1)
        envelope = contract.result(discovery, status="verified", candidates=[{"decision_id": "embedding_model", **{key: value for key, value in candidate.items() if key != "decision_id"}}])
        _accepted(envelope, discovery_manager, f"{name} through the model-doctor candidate")
    _check(True, "every producer of envelope text passes the Manager's real parser on the hostile corpus")


def _leg_diagnostic() -> None:
    """The stderr diagnostic keeps a label without its separator, and says so when redaction pushed it over its limit."""
    outcome = CommandOutcome(1, False, 1, "", "brew: password=hunter2 Authorization: Bearer abc.def api_key: k1 failed")
    reason = command_failure_reason(outcome)
    diagnostic = str(reason["stderr_diagnostic"])
    _check(diagnostic == "brew: password [redacted] Authorization [redacted] api_key [redacted] failed", f"labels stay readable, values and separators go: {diagnostic!r}")
    _check(reason["stderr_diagnostic_truncated"] is False, "a diagnostic that fits is not marked truncated")
    swollen = command_failure_reason(CommandOutcome(1, False, 1, "", "token=1 " * 128))
    _check(len(str(swollen["stderr_diagnostic"])) <= 1024 and swollen["stderr_diagnostic_truncated"] is True, "redaction that pushes the diagnostic past 1024 characters cuts it and says so")
    public_string(str(swollen["stderr_diagnostic"]), "swollen", maximum=1024)


def _leg_raw_envelopes_are_refused() -> None:
    """The positive control: the SAME text injected without the producers' guard is refused, so the leg above can fail."""
    request, manager = _request_pair()
    refused = 0
    for text in _HOSTILE.values():
        raw = contract.result(request, status="blocked", error_kind="demo_blocked", repair="ok")
        raw["repair"] = text
        refused += _refused(raw, manager)
    _check(refused == len(_HOSTILE), f"a raw hostile repair is refused by the Manager for every corpus text: {refused}/{len(_HOSTILE)}")


def _plain_cut(text: str, limit: int) -> str:
    return text[:limit]


def _drifted_twin(**changes: Any) -> SimpleNamespace:
    return SimpleNamespace(**{**vars(boot), **changes})


def _leg_twin_parity() -> None:
    for text in _HOSTILE.values():
        _check(contract.neutralized(text) == boot.neutralized(text), f"neutralized agrees across the twins for {text[:30]!r}")
        for limit in _FIELD_LIMITS:
            _check(contract.public_text(text, limit) == boot.public_text(text, limit), f"public_text agrees across the twins for {text[:30]!r} at {limit}")
    _check(contract.REPAIR_LIMIT == 512, "the plugin cuts a repair at 512, what an operator reads")
    _check(boot.REPAIR_LIMIT == 2048, "the bootstrap adapter holds a repair to the Manager's own maximum, as it never cut one")
    _check(support.twin_drift(contract, boot) == [], f"the twins hold the same patterns, constants and function bodies: {support.twin_drift(contract, boot)}")
    oauth = tuple(re.compile(item.pattern.replace("oauth[_ -]?code", "oauth[_-]?code"), item.flags) for item in boot._SECRET_SHAPED)  # noqa: SLF001
    _check(support.twin_drift(contract, _drifted_twin(_SECRET_SHAPED=oauth)) == ["_SECRET_SHAPED differs"], "control: a twin whose oauth pattern drifted is reported")
    _check(support.twin_drift(contract, _drifted_twin(_TAIL_SHARE=2)) == ["_TAIL_SHARE differs"], "control: a twin whose tail share drifted is reported")
    _check(support.twin_drift(contract, _drifted_twin(_fitted=_plain_cut)) == ["_fitted body differs"], "control: a twin whose cut drifted is reported")


def _leg_differential() -> None:
    """Nothing an r65 helper redacted may come out raw: round 1 put the bearer pattern first and leaked ``Bearer token=<secret>``."""
    corpus = list(support.differential_corpus())
    _check(len(corpus) >= 800, f"the corpus covers label x separator x shape: {len(corpus)}")
    for name, neutralize in (("contract.neutralized", contract.neutralized), ("contract.public_text", lambda text: contract.public_text(text, 4096)), ("boot.neutralized", boot.neutralized), ("boot.public_text", lambda text: boot.public_text(text, 4096))):
        _check(support.loosened(neutralize, corpus) == [], f"{name} hides every secret the r65 helpers hid: {support.loosened(neutralize, corpus)[:3]}")
    control = support.loosened(support.bearer_first_neutralize, corpus)
    _check(len(control) > 100, f"control: the round-1 bearer-first order loosens {len(control)} corpus texts, so this leg can fail")
    for text, _secret in corpus:
        public_string(contract.public_text(text, 4096), "corpus text", maximum=4096)
    _check(True, "every corpus text leaves the helper in a form the Manager's real public_string accepts")


def _helper_properties() -> None:
    for text in _HOSTILE.values():
        for limit in _FIELD_LIMITS:
            fitted = contract.public_text(text, limit)
            _check(contract.public_text(fitted, limit) == fitted, f"public_text is idempotent for {text[:30]!r} at {limit}")
            public_string(fitted, "fitted", maximum=limit)
    for name, secret in _HOSTILE_SECRETS.items():
        for helper_name, helper in (("contract", contract.public_text), ("bootstrap", boot.public_text)):
            _check(secret not in helper(_HOSTILE[name], 4096), f"{helper_name} public_text keeps no secret from {name!r}")
    _check(contract.neutralized("Authorization: Bearer abc") == "[REDACTED]", "Authorization: Bearer <token> is redacted in full")
    _check(contract.neutralized("x Bearer token=abc") == "x Bearer [REDACTED]", "a label pair after bearer is redacted, not left raw")
    _check("[REDACTED]" in contract.neutralized("--TOKEN=ABC") and "ABC" not in contract.neutralized("--TOKEN=ABC"), "an uppercase flag is redacted")
    _check("ABC" not in contract.neutralized("Authorization: BEARER ABC"), "an uppercase Bearer is redacted")
    _check(contract.neutralized(f"PATH={_KEG}/x") == "PATH=/opt/homebrew[keg path]0.1.0_64/libexec/bin/python3/x", "a keg path is replaced and the rest of the line stays readable")
    _check(contract.public_text("", 256) == "[empty]", "an empty text becomes a visible marker")
    _check(contract.public_value(["token=a", "token=b", "ok"], 4096) == ["[REDACTED]", "ok"], "list entries that became equal are dropped, order kept")
    _check(contract.public_value(7, 4096) == 7 and contract.public_value(None, 4096) is None, "a non-text value is left alone")


def _leg_census() -> None:
    found: list[str] = []
    scanned: list[str] = []
    for root in _SRC_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.name in _CHOKE_POINTS:
                continue
            scanned.append(path.name)
            found += support.offenders(path.read_text(encoding="utf-8"), f"{path.parent.name}/{path.name}")
    _check(len(scanned) > 50 and {"installation_model_doctor.py", "existing_install_plugin_transitions.py", "routes.py"} <= set(scanned), f"the census read the shipped seed sources, the known raw producers among them: {len(scanned)} files")
    _check(not found, f"every producer that builds an envelope text field itself passes it through public_text: {found}")
    synthetic = (
        'item["summary"] = text[:512]\n'
        'candidate = {"decision_id": d, "value": v[:256], "label": public_text(v, 256)}\n'
        'evidence = {"summary": public_text(s, 512), "digest": g, "status": st, "source": src}\n'
        'action = {"mutation_kind": k, "requires_confirmation": True, "title": t, "target": public_text(x, 512), "condition_or_evidence_ref": r}\n'
    )
    _check(len(support.offenders(synthetic, "synthetic")) == 6, "control: the census fires on a synthetic offender of each shape and field")


def _leg_cut() -> None:
    request, _ = _request_pair()
    long_repair = "The first thing wrong is " + "A" * 400 + ". " + "B" * 400 + ". Run `solet-manager update demo --dry-run` again."
    cut = cast(str, contract.result(request, status="blocked", error_kind="demo_blocked", repair=long_repair)["repair"])
    _check(len(cut) <= contract.REPAIR_LIMIT, f"a cut repair never exceeds the limit: {len(cut)}")
    _check(f"[... {len(long_repair)} characters, middle cut ...]" in cut, "the cut is visible and says how long the text was")
    _check(cut.startswith("The first thing wrong is") and cut.endswith("Run `solet-manager update demo --dry-run` again."), "the cut keeps the head and the remedy it ends with")
    fits = "Short repair. Run it again."
    _check(contract.result(request, status="blocked", error_kind="demo_blocked", repair=fits)["repair"] == fits, "a repair that fits is returned untouched")
    exact = "x" * contract.REPAIR_LIMIT
    _check(contract.result(request, status="blocked", error_kind="demo_blocked", repair=exact)["repair"] == exact, "a repair of exactly the limit is not cut")
    marker_cut = cast(str, contract.evidence(evidence_id="demo_fact", kind="demo", status="ok", summary="S" * 700 + " end.", observed="o", expected="e", source="x")["summary"])
    _check(len(marker_cut) <= 512 and "middle cut" in marker_cut and marker_cut.endswith(" end."), "an evidence summary is cut the same way, visibly")


def _leaf_pieces(node: ast.AST, constants: dict[str, ast.expr]) -> list[str | None] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in constants:
        return _pieces(constants[node.id], constants)
    if isinstance(node, ast.JoinedStr):
        return [value.value if isinstance(value, ast.Constant) else None for value in node.values]
    return None


def _pieces(node: ast.AST, constants: dict[str, ast.expr]) -> list[str | None] | None:
    """A repair expression as static text (``str``) and holes (``None``), or ``None`` when it is not a template."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _pieces(node.left, constants), _pieces(node.right, constants)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _format_pieces(_pieces(node.func.value, constants))
    return _leaf_pieces(node, constants)


def _format_pieces(base: list[str | None] | None) -> list[str | None] | None:
    if base is None:
        return None
    out: list[str | None] = []
    for piece in base:
        parts = [piece] if piece is None else piece.replace("{", "\x00{").split("\x00")
        for part in parts:
            if part is None or not part.startswith("{") or "}" not in part:
                out.append(part)
                continue
            out.append(None)
            rest = part[part.index("}") + 1 :]
            if rest:
                out.append(rest)
    return out


def _repair_templates() -> list[tuple[str, list[str | None]]]:
    templates: list[tuple[str, list[str | None]]] = []
    root = _SRC_ROOTS[0]
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {t.targets[0].id: t.value for t in ast.walk(tree) if isinstance(t, ast.Assign) and len(t.targets) == 1 and isinstance(t.targets[0], ast.Name)}
        for node in ast.walk(tree):
            expression = _repair_argument(node)
            pieces = None if expression is None else _pieces(expression, constants)
            if pieces:
                templates.append((f"{path.name}:{node.lineno}", pieces))
    return templates


def _repair_argument(node: ast.AST) -> ast.expr | None:
    if isinstance(node, ast.keyword) and node.arg == "repair":
        return node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"blocked", "_blocked"} and len(node.args) >= 3:
        return node.args[2]
    return None


def _leg_templates() -> None:
    hole = "H" * 200
    long_ones = 0
    templates = _repair_templates()
    _check(len(templates) > 40, f"the repair census resolved the statically known repair templates: {len(templates)}")
    for where, pieces in templates:
        text = "".join(hole if piece is None else piece for piece in pieces)
        if len(text) <= contract.REPAIR_LIMIT:
            continue
        long_ones += 1
        trailing = ""
        for piece in reversed(pieces):
            if piece is None:
                break
            trailing = piece + trailing
        fitted = contract.public_text(text, contract.REPAIR_LIMIT)
        _check(len(fitted) <= contract.REPAIR_LIMIT and "middle cut" in fitted, f"{where}: a long repair is cut visibly inside the limit")
        _check(fitted.endswith(trailing) and len(trailing) < contract.REPAIR_LIMIT * 2 // 5, f"{where}: the static text the repair ends with ({len(trailing)} characters) survives the cut")
        _check(fitted.startswith(text[:100]), f"{where}: the head survives the cut")
    _check(long_ones >= 5, f"enough templates are long with 200-character holes for the census to mean something: {long_ones}")


def _named_producers() -> None:
    home = "/Users/" + "a-very-long-account-name" * 4
    directories = ", ".join(("/opt/homebrew/bin", "/usr/local/bin", f"{home}/.local/bin", home + "/Applications/Claude/bin"))
    cli = REPAIR_CLAUDE_CLI.format(name="demo-solet", directories=directories)
    cli_cut = contract.public_text(cli, contract.REPAIR_LIMIT)
    _check(len(cli) > contract.REPAIR_LIMIT, f"control: REPAIR_CLAUDE_CLI with a long home is over the limit: {len(cli)}")
    _check("`brew install --cask claude-code`" in cli_cut or cli_cut.endswith("`solet-manager update demo-solet --dry-run` again."), "REPAIR_CLAUDE_CLI keeps the command it tells the operator to run")
    _check(cli_cut.endswith("`solet-manager update demo-solet --dry-run` again.") and "middle cut" in cli_cut, "REPAIR_CLAUDE_CLI keeps its closing remedy behind a visible marker")
    listed = f"`claude plugin list --json` did not answer ({'E' * 400}); the update never plans a registration off a failed read. Run it yourself, fix what it reports, then run `solet-manager update demo --dry-run` again."
    listed_cut = contract.public_text(listed, contract.REPAIR_LIMIT)
    _check(listed_cut.endswith("then run `solet-manager update demo --dry-run` again.") and "Run it yourself, fix what it reports" in listed_cut, "claude_plugin_list_failed keeps its remedy after a long failure")
    where = "/Users/demo/Solets/" + "nested-clone-directory/" * 12
    refusal = (
        f"A Claude marketplace named 'demo' is already registered from {where}, not from this clone ({where}x); the update never replaces it. Inspect `claude plugin marketplace list`; "
        "if that registration is stale, remove it yourself (`claude plugin marketplace remove demo`), then run `solet-manager update demo --dry-run` again."
    )
    refusal_cut = contract.public_text(refusal, contract.REPAIR_LIMIT)
    _check("(`claude plugin marketplace remove demo`), then run `solet-manager update demo --dry-run` again." in refusal_cut, "a marketplace refusal keeps the removal command and the retry")


def _leg_no_backtracking() -> None:
    """A run of ``bearer`` words must not make either pattern super-linear: measured at 1-9 ms for 150k characters, bounded here at 2 s."""
    shapes = {
        "bearer run": "bearer " * 21_500,
        "bearer run without a token": "bearer " * 21_500 + "!",
        "label with a bearer run": "token: " + "bearer " * 21_500,
        "label with bearer run and blanks": "token=" + "bearer " * 21_500 + "  ",
        "bearer then blanks": "bearer" + " " * 150_000,
        "repeated labels": "token= " * 21_500,
    }
    for name, text in shapes.items():
        _check(len(text) >= 140_000, f"{name}: the input is at least 140k characters: {len(text)}")
        for helper_name, helper in (("contract", contract.neutralized), ("bootstrap", boot.neutralized)):
            started = time.perf_counter()
            helper(text)
            elapsed = time.perf_counter() - started
            _check(elapsed < 2.0, f"{helper_name} neutralized stays linear on {name}: {elapsed * 1000:.1f} ms")


_LEGS: tuple[Callable[[], None], ...] = (
    _leg_producers,
    _leg_diagnostic,
    _leg_raw_envelopes_are_refused,
    _helper_properties,
    _leg_differential,
    _leg_census,
    _leg_cut,
    _leg_templates,
    _named_producers,
    _leg_twin_parity,
    _leg_no_backtracking,
)


def main() -> int:
    for leg in _LEGS:
        leg()
    print(f"seed_public_text_gate_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
