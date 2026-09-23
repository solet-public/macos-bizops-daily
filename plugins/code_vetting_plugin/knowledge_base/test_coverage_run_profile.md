# Test-Coverage Run-Profile — measuring which source the test suite executes, as evidence

Article Layer: 2
Article Role: capability_reference
Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:code-vetting, domain:testing, consumer_profile:both

Embedding Description: How the code-vetting suite measures TEST coverage — the lines of owned source that the unit and smoke suite actually execute — as an opt-in, on-demand run-profile of `vet_codebase`. Covers what the coverage run-profile is and why it is opt-in rather than part of the fast `vet_codebase` verb, the hard rule that coverage is visibility evidence and never a gate or a percentage bar, how to invoke it on demand, the `COVERAGE_PROCESS_START` subprocess-capture mechanism that measures the real subprocesses the no-mock discipline spawns, the per-owner artifact shape (executed/total lines and zero-reach modules per plugin), and how that evidence feeds the report's Test Coverage table, the deterministic `test_reach` finding, and the `test_adequacy` critic's targeting — distinct from the scanner-coverage concept (`CoverageRecord`) that records whether a scanner ran.

**When you need this**: running a test-coverage sweep over the codebase and wanting the per-plugin picture of what the tests execute; deciding whether coverage belongs in a routine `vet_codebase` run (it does not — it is opt-in); understanding why the suite reports zero-reach modules but never a coverage percentage or a pass/fail bar; wiring the coverage artifact into the vetting report or the `test_adequacy` critic; distinguishing test coverage from the scanner-coverage evidence the deterministic scanners already record.

---

## What the coverage run-profile is

The coverage run-profile is an **opt-in axis of `vet_codebase`** that measures how much of the owned source (`ananta/src`, `plugins/<X>/src`) the unit and smoke suite actually executes, and attributes the result per owner. It is a run-profile in the A0 sense — the single vetting engine parameterized by `(target, rulebook, stakes, substrate)` plus scope — with coverage as one more axis, defaulting to **off**. It is not a second suite and not a new verb.

It is deliberately **not part of the fast `vet_codebase` verb**. A whole-suite run under instrumentation is minutes to tens of minutes, which would violate the same action-queue fast-return contract that already keeps the L2 critics and L3 verify stages out of the verb surface. So the coverage measurement runs in the same on-demand, agent-dispatched lane as those heavy stages, and the default bounded `vet_codebase` scan stays fast.

## Evidence, never a gate

Coverage here is **visibility evidence, never a gate**: the run-profile emits raw executed and total line counts and a list of zero-reach modules, and it renders no percentage, no threshold, no allowlist, and no pass/fail verdict. This is a hard platform rule, not a stylistic choice — the testing taxonomy explicitly rejects coverage-percentage mandates because "lines executed" is a weak proxy for whether the work is correct, and a percentage target manufactures exactly the vacuous, mock-heavy tests the no-mock discipline exists to prevent.

The value of the numbers is to **name a gap so a human or a critic can reason about it**, not to score the suite. A module with zero executed lines is a fact worth surfacing; "coverage is 72%" is not a fact worth acting on.

## How to invoke it

Coverage runs **on demand**, not on a bespoke schedule (any recurring cadence is decided by the platform health framework, not this plugin). The measurement is driven by the run-profile driver under the plugin's operator tooling:

```
.venv/bin/python3 plugins/code_vetting_plugin/tools/test_coverage/run_profile.py --root "$PWD" --out <artifact.json>
```

`--list` prints the assembled command set without running; `--units-only` measures just the in-package unit modules for a fast check. The `coverage` tool must be present on PATH (`.venv/bin/pip install coverage`, Apache-2.0). Coverage is a PATH-resolved, **absence-tolerant** tool exactly like the L1 scanners' bandit/pip-audit: if it is missing, the run-profile records a gap and the report says the coverage was not measured — it never crashes and never silently reports a clean result.

## What it measures, and the subprocess mechanism

The run-profile assembles the suite's test commands — the git-tracked in-package unit modules (`plugins/<X>/src/**/tests/test_*.py`, run as `python -m`) plus the registered gate smokes (`quality_gates/gate_smokes.txt`) — and runs each under coverage, scoped to the owned source. Discovery is over **git-tracked files only**, so a plugin's vendored nested virtualenv or bundled `site-packages` is excluded structurally rather than pulling in thousands of third-party test modules.

Because the platform's no-mock discipline means smokes drive **real Python subprocesses**, the harness does not just wrap the top-level test process. It sets `COVERAGE_PROCESS_START` and injects a `sitecustomize` hook on `PYTHONPATH` so `coverage.process_startup()` runs in every child process, then `coverage combine` merges the parallel-mode data. This is the only mechanism that captures source executed inside a smoke's spawned subprocesses; per-script `coverage run` alone would systematically under-measure exactly the real-substrate paths the no-mock rule exists to exercise.

## The artifact shape

The run-profile emits a bounded `test_coverage` artifact: a **per-owner rollup** — for each owner (`plugins/<X>` or `ananta-core`) the number of source files, lines executed, lines total, and the list of **zero-reach modules** (source files the suite executed zero lines of). Only the per-owner rollup and a capped zero-reach list are persisted on the `vetting_runs` metrics row's `test_coverage` key; the full per-file map is working data and is not persisted, so the metrics trail stays bounded.

This artifact is distinct from **scanner coverage**. Scanner coverage (`CoverageRecord`, the report's Scanner Coverage table) records whether each *scanner* ran — "did bandit execute, and if not why." Test coverage records which *source lines the tests executed*. The two never share a type or a table; they are different measurements that happen to share the English word "coverage."

## How the evidence is consumed

The artifact feeds three consumers, in increasing intelligence, and never as a percentage:

- **The report's Test Coverage table** renders the per-owner executed/total counts and the zero-reach count — pure visibility, alongside (and clearly separate from) the Scanner Coverage table.
- **The deterministic `test_reach` finding** fires for an owner that exposes verbs but whose source the suite executed zero lines of — a countable structural fact ("verbs present, zero test reach"), advisory severity, never a coverage number. It is its own finding dimension so the critic-rot survival metric stays clean.
- **The `test_adequacy` critic** consumes an owner's zero-reach module list as *targeting evidence*: coverage points the critic at an untested module, and the critic does its normal job of naming the specific untested behavior and the concrete regression it would miss. Coverage names the path; the critic supplies the reasoning. This is exactly what the critic's own guidance requires — a missing-coverage finding must name a specific untested behavior, never assert "coverage is low."
