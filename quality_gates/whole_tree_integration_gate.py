#!/usr/bin/env python3
"""Whole-tree integration gate (W-INT) — structural mode (Cycle 1).

Walks every service interface, plugin, call-site, and scheduling-action
construction across the platform's quality surface and validates the
structural invariants the process registry depends on. Catches the
failure class that produced the 2026-06-06/07 vault `write_state` P0
(missing decorator + KB JSON), the Task #21 namespace bound-skip
divergence at call-sites, and the `result_processor_kind` propagation
gap in scheduling submissions.

Scope (Cycle 1, structural mode only — no live solet needed):
  C1.* — service-interface decorator/JSON/ABC consistency
         (walks ananta/src/ananta/services/*/interfaces/public.py +
          ananta/src/ananta/interfaces/*_service_interface.py +
          ananta/knowledge_base/processes/<svc>/*.json)
  C2.* — plugin @platform_process + KB JSON consistency
         (walks plugins/*/src/*/plugin.py +
          plugins/*/knowledge_base/processes/*.json)
  C3.* — call-site references to bound-plugin process_keys
         (AST-walks ananta/src + plugins/*/src + ananta/tests and reports each
          string literal whose ENTIRE value is "plugin::<X>::<verb>" or
          "service_interface::<svc>::<verb>"). Executable context only:
          comments are not AST nodes, docstrings are excluded, and a key quoted
          inside a longer literal (a "Returns:" example, a `description=`
          format example) is prose, not a reference. Two ASSEMBLED-key forms
          are out of scope, for DIFFERENT reasons, and neither is an
          oversight: an f-string with interpolation
          (f"plugin::{ns}::post_message") and a runtime concatenation
          ("::".join(...)) are not statically resolvable at all, so they are
          permanently out of scope and the live registry is what proves them;
          IMPLICIT adjacent-literal concatenation
          ("plugin::x::" "verb") IS statically provable — Python folds it at
          parse time — and is excluded only because a C3 finding must name a
          key some author wrote as one greppable token. That second one is a
          known gate-completeness gap, tracked as a low-priority follow-on
          (Architect ruling 2026-09-05, arm-1a787994); it is NOT a live miss,
          since its only current instance sits in a file unt_cf2d70d7 prunes.
          See _scan_single_file_for_refs and _is_single_string_token. A
          test-file line carrying the inline
          marker "# wint:negative-fixture" is treated as an intentional
          unregistered-key fixture and skipped (test-path-only, line-scoped —
          see _NEGATIVE_FIXTURE_MARKER + the KB negative-fixture-convention
          article).
  C5.* — scheduled-action result_processor_kind propagation
         (AST-walks scheduling-plugin construction sites; asserts every
          action dict literal includes `result_processor_kind`)
  C6.* — root_manifest.yaml drift (F1 integration; always-on per
          D-W-INT-C6-RUNMODE). Loads <repo_root>/root_manifest.yaml +
          delegates to ananta.core.root_manifest.classify_root_entries;
          BLOCKING findings on unknown/missing-universal/sunset-overdue/
          schema-violation. No-op when manifest absent. An unknown root
          entry that Git's index does not track (a lane's scratch file)
          is reported on stderr, not a finding (iss_ce45713b).

C4.* (IAM-resource-coupled cross-check) is DEFERRED to Cycle 2 per
operator-approved 2026-06-07 dispatch — needs live registry + bindings
inspection. Cycle 2 also adds the `--live-registry` flag.

Exit codes (mirrors `radon_*_check.py` + `god_class_check.py`):
  0  — clean, or every finding is allowlisted
  1  — one or more non-allowlisted findings
  2  — harness error (cannot construct scanner, missing required paths)
  64 — usage error (bad arguments)

Allowlist file format (one entry per line, `#` comments + blank lines OK):

  <check_id>::<scope_qualifier>::<specifier>

The first two `::` separators are structural; the specifier captures
the rest of the line verbatim (so embedded `::` in process keys works
cleanly). Per the KB "Gate Allowlist Conventions": allowlisted findings
are STILL printed (prefixed `[allowlisted]`) so the gate stays honest;
they do not contribute to the exit-1 verdict.

Per [[feedback_kb_first_discipline]], the gate generalizes the per-service
smoke at `ananta/tests/platform/state_service_write_state_registration_smoke.py`
across the whole tree. Removing an allowlist entry is the unit of
remediation progress; adding entries without operator approval defeats
the gate's purpose.

Reference: the whole-tree-integration-gate design record (dev-checkout
workbench — not part of the shipped tree).
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ananta.core.services.service_interface_decorator import (  # noqa: E402
    is_service_interface_process_enabled,
)

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402


def _rel(path: Path) -> str:
    """Make `path` repo-relative when possible; fall back to absolute string.

    Used in every finding message so the gate's output stays terse for
    in-tree paths while still working for out-of-tree fixtures (the meta-
    smoke uses temp dirs to construct synthetic ServiceSurface objects).
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()

_SERVICES_DIR = REPO_ROOT / "ananta" / "src" / "ananta" / "services"
_SERVICE_KB_DIR = REPO_ROOT / "ananta" / "knowledge_base" / "processes"
_PLUGINS_DIR = REPO_ROOT / "plugins"
_DEFAULT_BINDINGS = REPO_ROOT / "profile" / "config" / "service_bindings.json"

_CALL_SITE_ROOTS = (
    REPO_ROOT / "ananta" / "src",
    REPO_ROOT / "plugins",
    REPO_ROOT / "ananta" / "tests",
)
_CALL_SITE_PRUNE_DIRS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache"})
_CALL_SITE_BUNDLED_VENV_PREFIX = ".venv"

# Operator-tooling segments excluded per "Peer Pre-Completion Gate Procedure".
# Matches a path-segment EQUALITY check inside `plugins/<X>/<segment>/...`.
_OPERATOR_TOOLING_PLUGIN_SEGMENTS = frozenset({
    "research", "tools", "migrations", "parity_tests",
})

# Negative-fixture convention (2026-07-02): a test that PROVES an unregistered
# process_key is rejected necessarily contains an unregistered key literal, so
# the C3.* call-site detector would flag it. A test author marks such a line
# with this inline pragma; the detector then treats that line's process_key
# literal(s) as an intentional fixture. Recognized ONLY in test-path files
# (never weakens src detection) and scoped to the marked LINE (not a blanket
# file wildcard) — it names exactly the deliberate fixtures, not "ignore this
# file". See the C3.* docstring above + the KB negative-fixture-convention
# article. Migrating a per-key allowlist entry to this marker is remediation
# progress (the allowlist entry is REMOVED); adding the marker is a reviewable,
# in-tree, author≠verifier act.
_NEGATIVE_FIXTURE_MARKER = "# wint:negative-fixture"

# Executable-context C3 matching (2026-09-05, iss_3dc2ecc3). A process_key is a
# REFERENCE only when some string literal's ENTIRE value is that key. The
# previous detector applied an equivalent pattern to each RAW LINE, so a key
# quoted inside prose — a docstring "Returns:" example, a `description=` kwarg
# carrying a format example, a `#` comment — was reported as a call site. In a
# born clone those prose hits are the Group A/C findings; in the origin checkout
# they are permanent allowlist entries that describe documentation, not debt.
#
# Anchoring the pattern (^...$) and applying it to `ast.Constant` values rather
# than to text is what makes the distinction STRUCTURAL instead of heuristic: a
# comment is not an AST node at all, and a key embedded in a longer literal
# cannot match an anchored pattern. See `_scan_single_file_for_refs`.
_WHOLE_PROCESS_KEY_RE = re.compile(
    r"""^(plugin|service_interface)       # namespace
        ::([A-Za-z_][A-Za-z0-9_]*)        # provider
        ::([A-Za-z_][A-Za-z0-9_]*)$       # verb
    """,
    re.VERBOSE,
)

_SCHEDULING_VERB_NAMES = frozenset({
    "create_cron_schedule",
    "execute_in_seconds",
    "ensure_global_heartbeat",
    "ensure_periodic_poll_schedule",
    "ensure_periodic_summarize_schedule",
})

_RESULT_PROCESSOR_KIND_KEY = "result_processor_kind"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    check_id: str
    scope_qualifier: str
    specifier: str
    message: str
    file_path: str = ""
    lineno: int = 0


@dataclass(frozen=True)
class AllowlistEntry:
    check_id: str
    scope_qualifier: str
    specifier: str


@dataclass
class Allowlist:
    entries: frozenset[AllowlistEntry] = field(default_factory=frozenset)

    def covers(self, finding: Finding) -> bool:
        """True if any entry matches finding by (check_id, scope, specifier).

        Supports `*` wildcard on the specifier (and only the specifier):
        an entry whose specifier is `*` matches any specifier under the
        same `(check_id, scope_qualifier)` pair.
        """
        for entry in self.entries:
            if entry.check_id != finding.check_id:
                continue
            if entry.scope_qualifier != finding.scope_qualifier:
                continue
            if entry.specifier in ("*", finding.specifier):
                return True
        return False


# ---------------------------------------------------------------------------
# Allowlist parsing
# ---------------------------------------------------------------------------


def load_allowlist(path: Path) -> Allowlist:
    """Mandatory owner/reason/expires schema (D-3-structural, iss_23fa51b5)."""

    if not path.exists():
        return Allowlist()
    entries: set[AllowlistEntry] = set()
    for key in _load_tagged_allowlist(path):
        parts = key.split("::", 2)
        if len(parts) < 3:
            print(
                f"WARN: malformed allowlist line (need <check>::<scope>::<spec>): {key!r}",
                file=sys.stderr,
            )
            continue
        entries.add(AllowlistEntry(parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return Allowlist(frozenset(entries))


# ---------------------------------------------------------------------------
# Bindings loading
# ---------------------------------------------------------------------------


class BindingsResolutionError(RuntimeError):
    """The gate cannot determine which service→plugin bindings to check against.

    A `RuntimeError` subclass on purpose: `main` maps that to exit 2 (harness
    error). An unresolvable binding set is a harness failure, never a clean run.
    """


def _bindings_from_profile_template(root: Path) -> dict[str, str]:
    """Derive bindings from the SAME shipped profile template genesis uses.

    A born clone has no `profile/config/service_bindings.json`: the seed
    manifest never copies it, and genesis writes it at birth. Answering `{}`
    there (the pre-2026-09-05 behaviour, iss_06a5d953) is the worst available
    answer — with an empty map every bound-plugin reference reclassifies from
    C3.2 to C3.1, so the gate reports a tree-wide wave of drift that is really
    one missing file, and an origin allowlist keyed on C3.2 cannot cover a
    single row of it.

    This deliberately does NOT re-declare the bundle→profile map: a second copy
    would drift from the one genesis obeys, and a gate measuring against a
    profile the newborn will not actually be born into is worse than no gate.
    It calls genesis's own `resolve_profile_name` and the same
    `config_materialize.load_profile` that writes the bindings file, so the
    gate and birth read one source.

    Provenance absence is checked HERE rather than delegated, because
    `resolve_profile_name` answers a provenance-less tree with the free-tier
    default. That is right for birthing a legacy seed and wrong for a gate: the
    gate must say "I cannot know" instead of silently measuring every call site
    against a profile nobody selected.
    """
    provenance_path = root / "PROVENANCE.json"
    if not provenance_path.is_file():
        raise BindingsResolutionError(
            f"no materialized service_bindings.json and no {provenance_path.name} at {root} — "
            "cannot determine which profile's bindings to check against. Run against a born "
            "clone (which carries PROVENANCE.json) or a tree with materialized bindings."
        )
    try:
        from github_midwife_plugin.config_materialize import load_profile  # noqa: PLC0415
        from github_midwife_plugin.genesis import resolve_profile_name  # noqa: PLC0415
    except ImportError as exc:  # birth spine absent/unimportable — cannot resolve
        raise BindingsResolutionError(
            f"cannot import the birth spine to resolve the profile template: {exc}"
        ) from exc

    # Both raise RuntimeError subclasses (GenesisError / ConfigMaterializeError)
    # on an unknown bundle or a missing template; `main` renders those as exit 2.
    profile_name = resolve_profile_name(root)
    # Mirrors genesis `_resolve_kb_root`: the birth spine's own knowledge base.
    kb_root = root / "plugins" / "github_midwife_plugin" / "knowledge_base"
    profile = load_profile(kb_root, profile_name)

    raw_bindings = profile.get("service_bindings")
    if not isinstance(raw_bindings, dict) or not raw_bindings:
        raise BindingsResolutionError(
            f"profile template {profile_name!r} declares no non-empty 'service_bindings' mapping"
        )
    return {str(k): str(v) for k, v in raw_bindings.items()}


def load_bindings(profile_name: str) -> dict[str, str]:
    """Return service→plugin map for the requested profile.

    Prefers the materialized `profile/config/service_bindings.json`. When that
    file is absent — the born-clone case, pre-genesis — falls back to the
    shipped profile template rather than to an empty map; see
    `_bindings_from_profile_template` for why an empty map is not a safe
    default here.
    """
    if profile_name == "local":
        bindings_path = _DEFAULT_BINDINGS
    else:
        bindings_path = REPO_ROOT / "deployment" / "04_aws_provisioning" / "profile" / "config" / "service_bindings.json"
    if not bindings_path.is_file():
        return _bindings_from_profile_template(REPO_ROOT)
    payload = json.loads(bindings_path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in payload.items()}


# ---------------------------------------------------------------------------
# AST + module helpers
# ---------------------------------------------------------------------------


def _parse_source_safely(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(f"WARN: cannot parse {path}: {exc}", file=sys.stderr)
        return None


def _walk_python_files(root: Path) -> Iterator[Path]:
    """Yield Python files under root, pruning caches, bundled venvs, operator-tooling."""
    if not root.exists():
        return
    for path in root.rglob("*.py"):
        if any(part in _CALL_SITE_PRUNE_DIRS for part in path.parts):
            continue
        if any(part.startswith(_CALL_SITE_BUNDLED_VENV_PREFIX) for part in path.parts):
            continue
        if _is_operator_tooling_path(path):
            continue
        yield path


def _is_operator_tooling_path(path: Path) -> bool:
    """Match `plugins/<X>/{research,tools,migrations,parity_tests}/...` paths.

    The KB "Peer Pre-Completion Gate Procedure": operator-tooling under per-plugin
    research/tools/migrations/parity_tests is excluded from the platform's
    quality surface. The gate respects that — findings inside those
    segments would be noise from sketch / replay / debug code that is not
    load-bearing for production.
    """
    parts = path.parts
    try:
        plugins_idx = parts.index("plugins")
    except ValueError:
        return False
    if plugins_idx + 2 >= len(parts):
        return False
    segment = parts[plugins_idx + 2]
    return segment in _OPERATOR_TOOLING_PLUGIN_SEGMENTS


def _is_test_path(path: Path) -> bool:
    """True for files under a ``tests/`` directory (``ananta/tests/…`` or
    ``plugins/<X>/tests/…``).

    The ``# wint:negative-fixture`` marker is honored ONLY here, so it can never
    suppress a real registry-drift finding in production ``src`` code — that is
    the structural guarantee that the convention does not weaken the gate.
    """
    return "tests" in path.parts


def _decorator_name(decorator: ast.expr) -> str | None:
    """Return the bare callable name of a decorator (e.g. 'platform_process')."""
    if isinstance(decorator, ast.Call):
        return _decorator_name(decorator.func)
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    return None


def _decorator_kwarg_str(decorator: ast.Call, key: str) -> str | None:
    """Return a string-valued keyword argument from a decorator call."""
    for kw in decorator.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _decorator_kwarg_attr(decorator: ast.Call, key: str) -> str | None:
    """Return the attribute name for kwarg shaped `key=Enum.NAME`.

    Used to read `processor_policy_category=ProcessorPolicyCategory.EDGE_SINK`
    from a decorator without importing the enum at gate runtime.
    """
    for kw in decorator.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Attribute):
            return kw.value.attr
    return None


def _function_decorator_call(
    func: ast.FunctionDef, decorator_name: str,
) -> ast.Call | None:
    """Return the `ast.Call` for the named decorator on `func` (or None)."""
    for decorator in func.decorator_list:
        if isinstance(decorator, ast.Call) and _decorator_name(decorator) == decorator_name:
            return decorator
    return None


def _function_decorator_calls(
    module: ast.Module, decorator_name: str,
) -> Iterator[tuple[ast.FunctionDef, ast.Call]]:
    """Yield (FunctionDef, decorator_Call) for every method decorated `decorator_name`."""
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and _decorator_name(decorator) == decorator_name:
                if isinstance(node, ast.FunctionDef):
                    yield node, decorator


def _decorator_name_is_static(decorator: ast.Call) -> bool:
    """True if `name=` kwarg is absent OR a string-literal constant.

    When `name=` is a non-literal expression (e.g. `name=SomeEnum.X.value`),
    the registered name is resolved at runtime and the gate cannot
    statically verify the decorator-vs-KB-JSON mapping. Skip emitting
    findings for that decorator rather than producing false positives.
    """
    for kw in decorator.keywords:
        if kw.arg != "name":
            continue
        return isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
    return True


def _service_decorator_is_enabled(decorator: ast.Call) -> bool:
    """Evaluate the literal ``is_enabled`` marker through the shared predicate."""
    for kw in decorator.keywords:
        if kw.arg == "is_enabled":
            return (
                isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, bool)
                and is_service_interface_process_enabled(kw.value.value)
            )
    return is_service_interface_process_enabled(True)


# ---------------------------------------------------------------------------
# C1.* — Service interface consistency
# ---------------------------------------------------------------------------


@dataclass
class ServiceSurface:
    name: str
    public_path: Path
    decorated: dict[str, ast.FunctionDef]  # method_name -> def node
    kb_jsons: dict[str, Path]  # method_name -> json path


def _discover_service_surfaces() -> list[ServiceSurface]:
    surfaces: list[ServiceSurface] = []
    if not _SERVICES_DIR.exists():
        return surfaces
    for service_dir in sorted(_SERVICES_DIR.iterdir()):
        if not service_dir.is_dir():
            continue
        interfaces_dir = service_dir / "interfaces"
        public_path = interfaces_dir / "public.py"
        if not public_path.exists():
            continue
        # Walk every .py in interfaces/ to support post-2026-06-14 ABC
        # decomposition (W5.Q+R+S+T pattern): the @service_interface_process
        # decorators may live on the aggregate `public.py` OR on sub-ABC
        # files like `lifecycle.py`, `search.py`, etc. Pre-decomposition
        # services keep all decorators on `public.py` and this still picks
        # them up. `public_path` stays the canonical anchor for messaging;
        # decorators are aggregated across every file in `interfaces/`.
        decorated: dict[str, ast.FunctionDef] = {}
        for iface_path in sorted(interfaces_dir.glob("*.py")):
            if iface_path.name == "__init__.py":
                continue
            module = _parse_source_safely(iface_path)
            if module is None:
                continue
            decorated.update(_collect_service_interface_decorated(module))
        kb_dir = _SERVICE_KB_DIR / service_dir.name
        kb_jsons = _collect_kb_jsons(kb_dir)
        surfaces.append(ServiceSurface(service_dir.name, public_path, decorated, kb_jsons))
    return surfaces


def _collect_service_interface_decorated(
    module: ast.Module,
) -> dict[str, ast.FunctionDef]:
    out: dict[str, ast.FunctionDef] = {}
    for func, decorator in _function_decorator_calls(module, "service_interface_process"):
        if not _decorator_name_is_static(decorator) or not _service_decorator_is_enabled(decorator):
            continue
        name = _decorator_kwarg_str(decorator, "name") or func.name
        out[name] = func
    return out


def _collect_kb_jsons(kb_dir: Path) -> dict[str, Path]:
    if not kb_dir.exists():
        return {}
    return {p.stem: p for p in sorted(kb_dir.glob("*.json"))}


def check_services(surfaces: list[ServiceSurface]) -> list[Finding]:
    findings: list[Finding] = []
    for surface in surfaces:
        findings.extend(_check_service_decorator_vs_kb(surface))
        findings.extend(_check_service_kb_process_keys(surface))
    return findings


def _check_service_decorator_vs_kb(surface: ServiceSurface) -> list[Finding]:
    """C1.5/C1.7 — decorated methods ↔ KB JSON files, one-to-one."""
    findings: list[Finding] = []
    decorated_names = set(surface.decorated.keys())
    json_names = set(surface.kb_jsons.keys())

    for missing in sorted(decorated_names - json_names):
        func = surface.decorated[missing]
        findings.append(Finding(
            check_id="C1.5",
            scope_qualifier=surface.name,
            specifier=missing,
            message=(
                f"@service_interface_process method '{missing}' on "
                f"{_rel(surface.public_path)} has no KB JSON "
                f"at ananta/knowledge_base/processes/{surface.name}/{missing}.json"
            ),
            file_path=_rel(surface.public_path),
            lineno=func.lineno,
        ))
    for orphan in sorted(json_names - decorated_names):
        path = surface.kb_jsons[orphan]
        findings.append(Finding(
            check_id="C1.7",
            scope_qualifier=surface.name,
            specifier=orphan,
            message=(
                f"orphan KB JSON {_rel(path)} — no matching "
                f"@service_interface_process method named '{orphan}' on "
                f"{_rel(surface.public_path)}"
            ),
            file_path=_rel(path),
        ))
    return findings


def _check_service_kb_process_keys(surface: ServiceSurface) -> list[Finding]:
    """C1.6 — JSON process_key matches the canonical service-interface shape."""
    findings: list[Finding] = []
    for name, path in surface.kb_jsons.items():
        expected = f"service_interface::{surface.name}::{name}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            findings.append(Finding(
                check_id="C1.6",
                scope_qualifier=surface.name,
                specifier=name,
                message=f"cannot parse {_rel(path)}: {exc}",
                file_path=_rel(path),
            ))
            continue
        actual = payload.get("process_key", "")
        if actual != expected:
            findings.append(Finding(
                check_id="C1.6",
                scope_qualifier=surface.name,
                specifier=name,
                message=(
                    f"{_rel(path)} process_key={actual!r} "
                    f"does not match expected {expected!r}"
                ),
                file_path=_rel(path),
            ))
    return findings


# ---------------------------------------------------------------------------
# C2.* — Plugin @platform_process consistency
# ---------------------------------------------------------------------------


@dataclass
class PluginSurface:
    name: str
    plugin_py: Path
    decorated: dict[str, ast.FunctionDef]
    kb_jsons: dict[str, Path]


def _discover_plugin_surfaces() -> list[PluginSurface]:
    surfaces: list[PluginSurface] = []
    if not _PLUGINS_DIR.exists():
        return surfaces
    for plugin_dir in sorted(_PLUGINS_DIR.iterdir()):
        if not plugin_dir.is_dir():
            continue
        plugin_py = plugin_dir / "src" / plugin_dir.name / "plugin.py"
        if not plugin_py.exists():
            continue
        module = _parse_source_safely(plugin_py)
        if module is None:
            continue
        decorated = _collect_platform_process_decorated(module)
        kb_dir = plugin_dir / "knowledge_base" / "processes"
        kb_jsons = _collect_kb_jsons(kb_dir)
        surfaces.append(PluginSurface(plugin_dir.name, plugin_py, decorated, kb_jsons))
    return surfaces


def _collect_platform_process_decorated(
    module: ast.Module,
) -> dict[str, ast.FunctionDef]:
    out: dict[str, ast.FunctionDef] = {}
    for func, decorator in _function_decorator_calls(module, "platform_process"):
        if not _decorator_name_is_static(decorator):
            continue
        name = _decorator_kwarg_str(decorator, "name") or func.name
        out[name] = func
    return out


def check_plugins(surfaces: list[PluginSurface]) -> list[Finding]:
    findings: list[Finding] = []
    for surface in surfaces:
        findings.extend(_check_plugin_decorator_vs_kb(surface))
        findings.extend(_check_plugin_kb_process_keys(surface))
    return findings


def _check_plugin_decorator_vs_kb(surface: PluginSurface) -> list[Finding]:
    """C2.2/C2.4 — decorated methods ↔ KB JSON files, one-to-one."""
    findings: list[Finding] = []
    decorated_names = set(surface.decorated.keys())
    json_names = set(surface.kb_jsons.keys())

    for missing in sorted(decorated_names - json_names):
        func = surface.decorated[missing]
        findings.append(Finding(
            check_id="C2.2",
            scope_qualifier=surface.name,
            specifier=missing,
            message=(
                f"@platform_process method '{missing}' on "
                f"{_rel(surface.plugin_py)} has no KB JSON "
                f"at plugins/{surface.name}/knowledge_base/processes/{missing}.json"
            ),
            file_path=_rel(surface.plugin_py),
            lineno=func.lineno,
        ))
    for orphan in sorted(json_names - decorated_names):
        path = surface.kb_jsons[orphan]
        findings.append(Finding(
            check_id="C2.4",
            scope_qualifier=surface.name,
            specifier=orphan,
            message=(
                f"orphan KB JSON {_rel(path)} — no matching "
                f"@platform_process method named '{orphan}' on "
                f"{_rel(surface.plugin_py)}"
            ),
            file_path=_rel(path),
        ))
    return findings


def _check_plugin_kb_process_keys(surface: PluginSurface) -> list[Finding]:
    """C2.3 — JSON process_key matches the canonical plugin shape."""
    findings: list[Finding] = []
    for name, path in surface.kb_jsons.items():
        expected = f"plugin::{surface.name}::{name}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            findings.append(Finding(
                check_id="C2.3",
                scope_qualifier=surface.name,
                specifier=name,
                message=f"cannot parse {_rel(path)}: {exc}",
                file_path=_rel(path),
            ))
            continue
        actual = payload.get("process_key", "")
        if actual != expected:
            findings.append(Finding(
                check_id="C2.3",
                scope_qualifier=surface.name,
                specifier=name,
                message=(
                    f"{_rel(path)} process_key={actual!r} "
                    f"does not match expected {expected!r}"
                ),
                file_path=_rel(path),
            ))
    return findings


# ---------------------------------------------------------------------------
# C3.* — Call-site references to bound-plugin process_keys
# ---------------------------------------------------------------------------


@dataclass
class CallSiteRef:
    namespace: str   # "plugin" or "service_interface"
    provider: str    # plugin/service name
    verb: str        # method/function name
    file_path: Path
    lineno: int

    @property
    def process_key(self) -> str:
        return f"{self.namespace}::{self.provider}::{self.verb}"


def _scan_call_sites() -> list[CallSiteRef]:
    """Grep every Python file under the call-site roots for process_key literals."""
    refs: list[CallSiteRef] = []
    seen_files: set[Path] = set()
    for root in _CALL_SITE_ROOTS:
        for path in _walk_python_files(root):
            if path in seen_files:
                continue
            seen_files.add(path)
            refs.extend(_scan_single_file_for_refs(path))
    return refs


def _executable_string_constants(module: ast.Module) -> Iterator[tuple[ast.Constant, str]]:
    """Every string Constant in executable position, NOT descending into f-strings.

    An f-string's literal chunks are `ast.Constant` nodes too, so a plain
    `ast.walk` reports the prefix of
    `f"plugin::audio_processing_plugin::ffmpeg_{name}"` as though it were a
    whole key. It is not: the verb is computed at runtime, and the static
    prefix references nothing on its own.
    """
    stack: list[ast.AST] = [module]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.JoinedStr):
            continue
        if isinstance(node, ast.Constant):
            # Bind the narrowed value: `ast.Constant.value` is typed as the
            # union of every literal type, and the narrowing does not survive
            # the yield unless it is carried out explicitly.
            value = node.value
            if isinstance(value, str):
                yield node, value
        stack.extend(ast.iter_child_nodes(node))


def _is_single_string_token(source: str, node: ast.Constant) -> bool:
    """False when this Constant was built by IMPLICIT concatenation.

    Python folds adjacent literals at parse time, so
    `"plugin::audio_processing_plugin::" "ffmpeg_aphaser"` reaches the AST as
    one Constant indistinguishable from a single literal. The value is
    statically knowable, but it is still an ASSEMBLED key, and the C3 contract
    puts assembled keys out of scope: the detector reports keys an author wrote
    as one token, so that a finding always names something greppable in the
    source. Re-tokenizing the node's own source segment is the only way to tell
    the two apart after parsing.

    Parenthesizing the segment makes a multi-line fragment tokenizable on its
    own; an unreadable segment is treated as a single token (report it) rather
    than silently dropped, so this can never hide a real reference.
    """
    segment = ast.get_source_segment(source, node)
    if segment is None:
        return True
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(f"({segment})").readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return True
    return sum(1 for tok in tokens if tok.type == tokenize.STRING) == 1


def _docstring_constant_ids(module: ast.Module) -> set[int]:
    """`id()` of every Constant node serving as a module/class/function docstring.

    A docstring is documentation, never an executable reference — including the
    degenerate case where its whole value happens to be a process key.
    """
    docstring_ids: set[int] = set()
    doc_owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(module):
        if not isinstance(node, doc_owners) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_ids.add(id(first.value))
    return docstring_ids


def _negative_fixture_lines(path: Path, text: str) -> frozenset[int]:
    """Line numbers whose process_key literals the author marked as fixtures.

    A deliberate negative fixture: the test author marked this line's
    process_key literal as intentionally unregistered (exercising a rejection
    path). Test-path-only + line-scoped, so `src` detection is untouched and
    the file is never blanket-wildcarded.
    """
    if not _is_test_path(path):
        return frozenset()
    return frozenset(
        lineno
        for lineno, line in enumerate(text.splitlines(), start=1)
        if _NEGATIVE_FIXTURE_MARKER in line
    )


def _scan_single_file_for_refs(path: Path) -> Iterator[CallSiteRef]:
    """Yield one ref per string literal whose ENTIRE value is a process_key.

    Executable context only (iss_3dc2ecc3): comments never reach the AST,
    docstrings are excluded explicitly, and a key embedded in a longer literal
    — a "Returns: (e.g., 'plugin::x::y')" line, a `description=` kwarg quoting
    the key as a format example — cannot match the anchored pattern. Those
    three shapes were the Group A/C born-clone false positives.

    OUT OF SCOPE by construction: an ASSEMBLED key, in all three forms —
    an f-string (`f"plugin::{ns}::post_message"`), a runtime concatenation
    (`"::".join(parts)`, `prefix + verb`), and IMPLICIT adjacent-literal
    concatenation (`"plugin::audio_processing_plugin::" "ffmpeg_aphaser"`).
    The third one is the subtle case: Python folds it at parse time, so it
    reaches the AST as a single Constant and only re-tokenizing the source
    segment distinguishes it (see `_is_single_string_token`). It is excluded
    with the other two so that every C3 finding names a key some author wrote
    as one greppable token — a finding a reader cannot locate by searching for
    the string it reports is a finding they cannot act on.
    """
    module = _parse_source_safely(path)
    if module is None:
        # `_parse_source_safely` already warned. A file the AST cannot read is
        # reported there, not silently swallowed here — the WARN is the signal.
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    suppressed_lines = _negative_fixture_lines(path, text)
    docstring_ids = _docstring_constant_ids(module)
    for node, value in _executable_string_constants(module):
        if id(node) in docstring_ids:
            continue
        match = _WHOLE_PROCESS_KEY_RE.match(value)
        if match is None:
            continue
        if not _is_single_string_token(text, node):
            continue
        # Line-scoped exactly as the raw-line detector was: a whole-value key
        # literal is a single token, so the node's `lineno` IS the line the
        # author marked.
        if node.lineno in suppressed_lines:
            continue
        yield CallSiteRef(
            namespace=match.group(1),
            provider=match.group(2),
            verb=match.group(3),
            file_path=path,
            lineno=node.lineno,
        )


def check_call_sites(
    refs: list[CallSiteRef],
    bindings: dict[str, str],
    service_surfaces: list[ServiceSurface],
    plugin_surfaces: list[PluginSurface],
) -> list[Finding]:
    findings: list[Finding] = []
    bound_plugins = set(bindings.values())
    services_by_name = {s.name: s for s in service_surfaces}
    plugins_by_name = {p.name: p for p in plugin_surfaces}

    for ref in refs:
        if _ref_in_self_referential_source(ref):
            continue
        findings.extend(_classify_call_site_ref(
            ref, bound_plugins, services_by_name, plugins_by_name,
        ))
    return findings


def _ref_in_self_referential_source(ref: CallSiteRef) -> bool:
    """Skip refs inside the gate, the design doc smoke, and similar meta sources."""
    posix = ref.file_path.as_posix()
    meta_markers = (
        "/quality_gates/whole_tree_integration_gate.py",
        "/ananta/tests/platform/whole_tree_integration_gate_smoke.py",
        "/ananta/tests/platform/state_service_write_state_registration_smoke.py",
    )
    return any(marker in posix for marker in meta_markers)


def _classify_call_site_ref(
    ref: CallSiteRef,
    bound_plugins: set[str],
    services_by_name: dict[str, ServiceSurface],
    plugins_by_name: dict[str, PluginSurface],
) -> list[Finding]:
    if ref.namespace == "service_interface":
        return _check_service_interface_ref(ref, services_by_name)
    return _check_plugin_namespace_ref(ref, bound_plugins, plugins_by_name)


def _check_service_interface_ref(
    ref: CallSiteRef, services_by_name: dict[str, ServiceSurface],
) -> list[Finding]:
    surface = services_by_name.get(ref.provider)
    if surface is None:
        return [Finding(
            check_id="C3.1",
            scope_qualifier=_rel(ref.file_path),
            specifier=ref.process_key,
            message=(
                f"call-site references {ref.process_key} but no service "
                f"named '{ref.provider}' has a public.py surface"
            ),
            file_path=_rel(ref.file_path),
            lineno=ref.lineno,
        )]
    if ref.verb not in surface.decorated:
        return [Finding(
            check_id="C3.1",
            scope_qualifier=_rel(ref.file_path),
            specifier=ref.process_key,
            message=(
                f"call-site references {ref.process_key} but service "
                f"{ref.provider!r} has no @service_interface_process verb "
                f"named '{ref.verb}'"
            ),
            file_path=_rel(ref.file_path),
            lineno=ref.lineno,
        )]
    return []


def _check_plugin_namespace_ref(
    ref: CallSiteRef,
    bound_plugins: set[str],
    plugins_by_name: dict[str, PluginSurface],
) -> list[Finding]:
    findings: list[Finding] = []
    if ref.provider in bound_plugins:
        findings.append(Finding(
            check_id="C3.2",
            scope_qualifier=_rel(ref.file_path),
            specifier=ref.process_key,
            message=(
                f"call-site references {ref.process_key} but '{ref.provider}' "
                "is a bound ServiceProvider; runtime _should_skip_plugin() "
                "will drop the plugin:: registration. Migrate to the "
                "service_interface:: surface or allowlist as W-NS-SWEEP debt"
            ),
            file_path=_rel(ref.file_path),
            lineno=ref.lineno,
        ))
        return findings
    surface = plugins_by_name.get(ref.provider)
    if surface is None:
        findings.append(Finding(
            check_id="C3.1",
            scope_qualifier=_rel(ref.file_path),
            specifier=ref.process_key,
            message=(
                f"call-site references {ref.process_key} but no plugin "
                f"named '{ref.provider}' has a plugin.py surface"
            ),
            file_path=_rel(ref.file_path),
            lineno=ref.lineno,
        ))
        return findings
    if ref.verb not in surface.decorated:
        findings.append(Finding(
            check_id="C3.1",
            scope_qualifier=_rel(ref.file_path),
            specifier=ref.process_key,
            message=(
                f"call-site references {ref.process_key} but plugin "
                f"{ref.provider!r} has no @platform_process verb "
                f"named '{ref.verb}'"
            ),
            file_path=_rel(ref.file_path),
            lineno=ref.lineno,
        ))
    return findings


# ---------------------------------------------------------------------------
# C6.* — Root manifest drift (F1 integration; always-on per D-W-INT-C6-RUNMODE)
# ---------------------------------------------------------------------------


def check_root_manifest_drift(repo_root: Path) -> list[Finding]:
    """Detect drift between ``root_manifest.yaml`` and the working tree.

    No-op when the manifest is absent (per D-W-INT-C6-RUNMODE = always-on:
    F1 IMPL may not have landed at every solet).  All C6.\\* findings
    are BLOCKING by default; INFO-only surfaces (C6.3 missing sanctioned,
    cleanup-overdue) are emitted by the dedicated pre-commit consumer at
    ``ananta.core.root_manifest.pre_commit`` and the diagnostic consumer
    at ``ananta.core.root_manifest.diagnostic``, NOT by this gate.  The one
    C6.1 surface this gate reports without blocking is an undeclared root
    entry that Git's index does not track (iss_ce45713b): see
    :attr:`Classification.tracked_unknown_entries`.
    """
    from ananta.core.root_manifest import MANIFEST_FILENAME, classify_root_entries

    manifest_path = repo_root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return []

    classification = classify_root_entries(manifest_path, repo_root)
    findings: list[Finding] = []

    if classification.schema_validation_error is not None:
        findings.append(Finding(
            check_id="C6.6", scope_qualifier="manifest", specifier="schema",
            message=classification.schema_validation_error,
            file_path=str(manifest_path),
        ))
        # Schema-failed manifests do not surface further drift findings —
        # the classification's other fields are empty in that branch.
        return findings

    # C6.1 blocks on what the repository CARRIES.  An undeclared root entry
    # that Git's index does not know — a lane's scratch note, a handover
    # file — is not this landing's drift, or any landing's (iss_ce45713b):
    # it is reported, never blocking.  When no index could be consulted the
    # untracked set is empty and every unknown entry blocks, as before.
    for name in classification.tracked_unknown_entries:
        findings.append(Finding(
            check_id="C6.1", scope_qualifier="root", specifier=name,
            message="root entry not declared in root_manifest.yaml",
            file_path=str(repo_root / name),
        ))
    if classification.untracked_entries:
        print(
            "INFO: C6.1::root — undeclared root entries untracked by git, "
            f"not blocking: {', '.join(classification.untracked_entries)}",
            file=sys.stderr,
        )
    for name in classification.missing_universal:
        findings.append(Finding(
            check_id="C6.2", scope_qualifier="universal", specifier=name,
            message="universal entry declared in manifest but missing from tree",
            file_path=str(manifest_path),
        ))
    for entry in classification.sunset_overdue:
        findings.append(Finding(
            check_id="C6.5", scope_qualifier="sunset", specifier=entry.path,
            message=(
                f"sunset_target {entry.sunset_target} is past by "
                f"{entry.days_overdue} day(s); path still present"
            ),
            file_path=str(manifest_path),
        ))
    return findings


# ---------------------------------------------------------------------------
# C5.* — Scheduled-action result_processor_kind propagation
# ---------------------------------------------------------------------------


def check_scheduling_rpk_propagation(
    roots: Iterable[Path],
    services: list[ServiceSurface] | None = None,
    plugins: list[PluginSurface] | None = None,
) -> list[Finding]:
    """C5.1 — scheduled-action result_processor_kind propagation.

    Walks Python files for AST calls to scheduling verbs; for each
    ``actions=[{...}]`` dict literal, requires ``result_processor_kind`` UNLESS
    the target verb's ``processor_policy_category`` is ``EDGE_SINK`` (per the
    Phase 1 cron-action contract: ``omit RPK`` is canonical for EDGE_SINK
    targets because ``action_queue_poller._decide_dispatch`` short-circuits via
    the EDGE_SINK_SKIP branch when ``result_processor_kind is None and
    result_processor is None``).

    ``services`` and ``plugins`` are passed by the caller for target-category
    resolution; when omitted (e.g. ad-hoc invocation), category lookup is
    skipped and the gate falls back to "RPK required" for every target.
    """
    services_by_name: dict[str, ServiceSurface] = (
        {s.name: s for s in services} if services is not None else {}
    )
    plugins_by_name: dict[str, PluginSurface] = (
        {p.name: p for p in plugins} if plugins is not None else {}
    )
    findings: list[Finding] = []
    seen: set[Path] = set()
    for root in roots:
        for path in _walk_python_files(root):
            if path in seen:
                continue
            seen.add(path)
            module = _parse_source_safely(path)
            if module is None:
                continue
            findings.extend(
                _check_scheduling_in_module(module, path, services_by_name, plugins_by_name)
            )
    return findings


def _check_scheduling_in_module(
    module: ast.Module,
    path: Path,
    services_by_name: dict[str, ServiceSurface],
    plugins_by_name: dict[str, PluginSurface],
) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        verb = _scheduling_verb_name(node.func)
        if verb is None:
            continue
        actions_arg = _find_actions_kwarg(node)
        if actions_arg is None:
            continue
        findings.extend(
            _check_actions_literal(actions_arg, verb, path, services_by_name, plugins_by_name)
        )
    return findings


def _scheduling_verb_name(func: ast.expr) -> str | None:
    """Return the scheduling verb name if `func` is a call to one of the tracked verbs."""
    name = None
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    if name in _SCHEDULING_VERB_NAMES:
        return name
    return None


def _find_actions_kwarg(node: ast.Call) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == "actions":
            return kw.value
    return None


def _check_actions_literal(
    actions: ast.expr,
    verb: str,
    path: Path,
    services_by_name: dict[str, ServiceSurface],
    plugins_by_name: dict[str, PluginSurface],
) -> list[Finding]:
    """Inspect an `actions=[...]` literal.

    Every dict literal must include `result_processor_kind` UNLESS the target
    verb (resolved from the dict's `process_key` value) declares
    `processor_policy_category=ProcessorPolicyCategory.EDGE_SINK`. Per the
    Phase 1 cron-action contract, EDGE_SINK targets reach the EDGE_SINK_SKIP
    branch at `action_queue_poller._decide_dispatch` before any result-
    processor dispatch fires, so omitting RPK is canonical (and required by
    the registration validator at `default_scheduling_plugin/validation.py`,
    which rejects `result_processor_kind="inference"` for cron-fired
    actions).
    """
    if not isinstance(actions, ast.List):
        # Non-literal (variable, function call, list comp): skip — out of scope
        # for the AST gate. Cycle 2 may add taint-style propagation.
        return []
    findings: list[Finding] = []
    for index, element in enumerate(actions.elts):
        if not isinstance(element, ast.Dict):
            continue
        if _dict_has_string_key(element, _RESULT_PROCESSOR_KIND_KEY):
            continue
        process_key = _extract_dict_string_value(element, "process_key")
        if process_key is not None:
            category = _resolve_action_target_category(
                process_key, services_by_name, plugins_by_name
            )
            if category == "EDGE_SINK":
                # Canonical EDGE_SINK shape — RPK omission is required, not a
                # violation. See Phase 1 design memo + KB article
                # 21_scheduling_service/01_template_flow_record_lifecycle.md.
                continue
        findings.append(Finding(
            check_id="C5.1",
            scope_qualifier=_rel(path),
            specifier=f"{verb}#{index}",
            message=(
                f"{_rel(path)}:{actions.lineno} "
                f"{verb}(actions=[...]) entry #{index} omits "
                f"'{_RESULT_PROCESSOR_KIND_KEY}'; "
                "EDGE-category cron targets MUST carry one (EDGE_SINK targets "
                "are exempt; declare the target verb's "
                "processor_policy_category=ProcessorPolicyCategory.EDGE_SINK "
                "to take the exemption)"
            ),
            file_path=_rel(path),
            lineno=actions.lineno,
        ))
    return findings


def _dict_has_string_key(dict_node: ast.Dict, key: str) -> bool:
    for k in dict_node.keys:
        if isinstance(k, ast.Constant) and k.value == key:
            return True
    return False


def _extract_dict_string_value(dict_node: ast.Dict, key: str) -> str | None:
    """Return the string-literal value for `key` in a dict literal (or None)."""
    for k, v in zip(dict_node.keys, dict_node.values, strict=False):
        if not (isinstance(k, ast.Constant) and k.value == key):
            continue
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            return v.value
        return None
    return None


def _resolve_action_target_category(
    process_key: str,
    services_by_name: dict[str, ServiceSurface],
    plugins_by_name: dict[str, PluginSurface],
) -> str | None:
    """Resolve the target verb's `processor_policy_category` attribute name.

    Returns the attribute name (e.g. ``"EDGE_SINK"``, ``"EDGE"``, ``"VERTEX"``)
    or ``None`` if unresolvable (malformed key, unknown namespace, missing
    surface, missing verb, or non-Attribute category expression).
    """
    parts = process_key.split("::")
    if len(parts) != 3:
        return None
    namespace, provider, verb = parts
    if namespace == "service_interface":
        surface = services_by_name.get(provider)
        if surface is None or verb not in surface.decorated:
            return None
        decorator = _function_decorator_call(
            surface.decorated[verb], "service_interface_process"
        )
    elif namespace == "plugin":
        surface = plugins_by_name.get(provider)
        if surface is None or verb not in surface.decorated:
            return None
        decorator = _function_decorator_call(
            surface.decorated[verb], "platform_process"
        )
    else:
        return None
    if decorator is None:
        return None
    return _decorator_kwarg_attr(decorator, "processor_policy_category")


# ---------------------------------------------------------------------------
# Reporting + main
# ---------------------------------------------------------------------------


def _emit_human(
    findings: list[Finding], allowlist: Allowlist,
) -> tuple[int, int]:
    """Print findings; return (blocking_count, allowlisted_count)."""
    blocking = 0
    allowlisted = 0
    for finding in findings:
        is_allowlisted = allowlist.covers(finding)
        marker = " [allowlisted]" if is_allowlisted else ""
        print(f"{finding.check_id}::{finding.scope_qualifier}::{finding.specifier}{marker}")
        print(f"   {finding.message}")
        if is_allowlisted:
            allowlisted += 1
        else:
            blocking += 1
    return blocking, allowlisted


def _emit_json(
    findings: list[Finding], allowlist: Allowlist,
) -> tuple[int, int]:
    blocking = 0
    allowlisted = 0
    payload_findings: list[dict[str, object]] = []
    for finding in findings:
        is_allowlisted = allowlist.covers(finding)
        if is_allowlisted:
            allowlisted += 1
        else:
            blocking += 1
        payload_findings.append({
            "check_id": finding.check_id,
            "scope_qualifier": finding.scope_qualifier,
            "specifier": finding.specifier,
            "message": finding.message,
            "file_path": finding.file_path,
            "lineno": finding.lineno,
            "allowlisted": is_allowlisted,
        })
    print(json.dumps({
        "blocking": blocking,
        "allowlisted": allowlisted,
        "findings": payload_findings,
    }, indent=2))
    return blocking, allowlisted


def _filter_by_scope(
    services: list[ServiceSurface],
    plugins: list[PluginSurface],
    service_filter: str | None,
    plugin_filter: str | None,
) -> tuple[list[ServiceSurface], list[PluginSurface]]:
    s = services if service_filter is None else [x for x in services if x.name == service_filter]
    p = plugins if plugin_filter is None else [x for x in plugins if x.name == plugin_filter]
    return s, p


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", type=Path, default=None,
                        help="Path to tracked-debt allowlist file.")
    parser.add_argument("--profile", default="local",
                        help="Profile name for bindings (default: local).")
    parser.add_argument("--service", default=None,
                        help="Scope to a single service for fast iteration.")
    parser.add_argument("--plugin", default=None,
                        help="Scope to a single plugin for fast iteration.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of human text.")
    parser.add_argument("--skip-call-sites", action="store_true",
                        help="Skip C3.* — useful when iterating on C1/C2/C5.")
    parser.add_argument("--skip-scheduling", action="store_true",
                        help="Skip C5.* — useful when iterating on C1/C2/C3.")
    return parser


def _run_all_checks(args: argparse.Namespace) -> list[Finding]:
    services = _discover_service_surfaces()
    plugins = _discover_plugin_surfaces()
    services, plugins = _filter_by_scope(services, plugins, args.service, args.plugin)
    bindings = load_bindings(args.profile)

    findings: list[Finding] = []
    findings.extend(check_services(services))
    findings.extend(check_plugins(plugins))
    if not args.skip_call_sites:
        refs = _scan_call_sites()
        findings.extend(check_call_sites(refs, bindings, services, plugins))
    if not args.skip_scheduling:
        findings.extend(check_scheduling_rpk_propagation(
            [REPO_ROOT / "ananta" / "src", REPO_ROOT / "plugins"],
            services=services,
            plugins=plugins,
        ))
    findings.extend(check_root_manifest_drift(REPO_ROOT))
    return findings


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    allowlist = load_allowlist(args.allowlist) if args.allowlist else Allowlist()

    try:
        findings = _run_all_checks(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: harness failure: {exc}", file=sys.stderr)
        return 2

    findings.sort(key=lambda f: (f.check_id, f.scope_qualifier, f.specifier))

    if args.json:
        blocking, allowlisted = _emit_json(findings, allowlist)
    else:
        blocking, allowlisted = _emit_human(findings, allowlist)

    if not args.json:
        if blocking == 0:
            if not findings:
                print("OK: 0 findings; whole-tree integration gate clean.")
            else:
                print(
                    f"OK: {len(findings)} finding(s) — all allowlisted; "
                    "whole-tree integration gate clean."
                )
        else:
            print(
                f"\n{len(findings)} whole-tree integration finding(s) "
                f"({allowlisted} allowlisted; {blocking} still failing).",
                file=sys.stderr,
            )
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
