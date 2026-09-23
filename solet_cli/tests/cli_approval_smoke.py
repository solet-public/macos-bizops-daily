"""Noninteractive approval-fingerprint optimistic-concurrency smoke."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import solet_manager.cli as cli_module  # noqa: E402
from solet_manager.decision_prompt import (  # noqa: E402
    prompt_discovered_decisions,
)
from solet_manager.errors import (  # noqa: E402
    ApprovalFingerprintMalformedError,
    ApprovalFingerprintRequiredError,
)
from solet_manager.models import CommandResult, ExitCode  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_CONTRACTS = _REPO / "plugins" / "github_midwife_plugin" / "knowledge_base"
_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _raises(error: type[BaseException], callback: object, label: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except error:
        _check(True, label)
    else:
        _check(False, label)


class _Tty:
    @staticmethod
    def isatty() -> bool:
        return True


def _write_seed(path: Path, *, release_tag: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "https://github.com/solet-public/macos-bizops.git",
                "release_tag": release_tag,
                "commit": "a" * 40,
                "tree_hash": "b" * 40,
                "archive_sha256": "c" * 64,
                "profile": "macos-bizops",
            }
        ),
        encoding="utf-8",
    )


def _selected_decisions(result: CommandResult) -> dict[str, object]:
    raw = result.data.get("decisions")
    if not isinstance(raw, list):
        return {}
    return {
        str(item["id"]): item.get("selected")
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _decision_activation_regression(base: list[str]) -> str:
    bare = cli_module.run([*base, "--dry-run", "--json"])
    _check(
        bare.status == "awaiting_user"
        and bare.error_kind == "decisions_required"
        and bare.data["unresolved_decisions"]
        == ["inference_implementation", "session_sources", "git_mutation_control"]
        and _selected_decisions(bare)["execution_topology"] == "solo",
        "bare macos-bizops preview prompts every unresolved decision after defaulting solo",
    )

    prompted_replies = [
        "--decision",
        "inference_implementation=lm_studio",
        "--decision",
        "git_mutation_control=single_session",
        "--decision",
        "session_sources=",
    ]
    reviewed = cli_module.run([*base, *prompted_replies, "--dry-run", "--json"])
    _check(
        reviewed.status == "preview_ready"
        and _selected_decisions(reviewed)["execution_topology"] == "solo"
        and _selected_decisions(reviewed)["git_mutation_control"] == "single_session",
        "the exact prompted replies resolve against the silently defaulted parent",
    )

    child_first = cli_module.run(
        [
            *base,
            "--decision",
            "git_mutation_control=single_session",
            "--decision",
            "execution_topology=solo",
            "--decision",
            "inference_implementation=lm_studio",
            "--decision",
            "session_sources=",
            "--dry-run",
            "--json",
        ]
    )
    _check(
        child_first.status == "preview_ready"
        and _selected_decisions(child_first)["execution_topology"] == "solo"
        and _selected_decisions(child_first)["git_mutation_control"] == "single_session",
        "child-before-parent decision flags remain order independent",
    )
    return str(reviewed.data["approval_fingerprint"])


def _prompt_discovered_decisions_ui_helper() -> None:
    """Exercise prompt rendering only; lifecycle coverage owns model timing."""
    existing = {"inference_implementation": "lm_studio"}
    unresolved = CommandResult(
        kind="create_preview",
        status="awaiting_user",
        message="select discovered candidates",
        exit_code=ExitCode.HUMAN_ACTION,
        error_kind="decisions_required",
        data={
            "decision_prompts": [
                {
                    "id": "embedding_model",
                    "title": "Embedding model",
                    "prompt": "Choose an embedding model",
                    "candidates": [
                        {
                            "value": "embedding_model.recommended",
                            "label": "Recommended embedding model",
                            "metadata": {},
                        }
                    ],
                },
                {
                    "id": "inference_model",
                    "title": "Inference model",
                    "prompt": "Choose an inference model",
                    "candidates": [
                        {
                            "value": "inference_model.recommended",
                            "label": "Recommended inference model",
                            "metadata": {},
                        }
                    ],
                },
            ]
        },
    )
    prompt_output = StringIO()
    with (
        patch("builtins.input", side_effect=["1", "1"]) as prompted,
        redirect_stdout(prompt_output),
    ):
        interactive_selections = prompt_discovered_decisions(unresolved, existing)
    _check(
        prompted.call_count == 2
        and prompt_output.getvalue().count("Select candidate number") == 0,
        "UI helper consumes exactly two deterministic model indexes",
    )
    _check(
        existing == {"inference_implementation": "lm_studio"}
        and interactive_selections
        == {
            **existing,
            "embedding_model": "embedding_model.recommended",
            "inference_model": "inference_model.recommended",
        },
        "UI helper returns choices without mutating ambient selections",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        seed = root / "seed.lock.json"
        _write_seed(seed, release_tag="release-1")
        target = root / "Solets" / "bizops"
        base = [
            "--home",
            str(root / "manager"),
            "--contract-dir",
            str(_CONTRACTS),
            "--seed-lock",
            str(seed),
            "create",
            "bizops",
            "--target",
            str(target),
        ]
        prompted_replies = [
            "--decision",
            "inference_implementation=lm_studio",
            "--decision",
            "git_mutation_control=single_session",
            "--decision",
            "session_sources=",
        ]
        resolved_base = [*base, *prompted_replies]
        fingerprint = _decision_activation_regression(base)
        decisions_blocked = cli_module.run([*base, "--dry-run", "--json"])
        _check(
            decisions_blocked.error_kind == "decisions_required"
            and isinstance(decisions_blocked.data.get("approval_fingerprint"), str),
            "blocking decisions JSON carries its computed approval fingerprint",
        )
        approval_blocked = cli_module.run([*resolved_base, "--json"])
        _check(
            approval_blocked.error_kind == "approval_required"
            and approval_blocked.data.get("approval_fingerprint") == fingerprint,
            "blocking approval JSON preserves the reviewed approval fingerprint",
        )

        original_preview = cli_module.CreateManager.preview
        with patch.object(
            cli_module.CreateManager,
            "preview",
            side_effect=AssertionError("argument validation must precede probes"),
        ):
            _raises(
                ApprovalFingerprintRequiredError,
                lambda: cli_module.run([*resolved_base, "--yes"]),
                "plain --yes is an exit-2 carrier error before probes",
            )
            _raises(
                ApprovalFingerprintRequiredError,
                lambda: cli_module.run([*resolved_base, "--approval-fingerprint", "sha256:" + "0" * 64]),
                "fingerprint without --yes is an exit-2 carrier error before probes",
            )
            _raises(
                ApprovalFingerprintMalformedError,
                lambda: cli_module.run([*resolved_base, "--yes", "--approval-fingerprint", "sha256:not-valid"]),
                "malformed fingerprint is an exit-2 carrier error before probes",
            )
            os.environ["SOLET_ASSUME_YES"] = "1"
            try:
                _raises(
                    ApprovalFingerprintRequiredError,
                    lambda: cli_module.run([*resolved_base, "--yes"]),
                    "ambient assume-yes cannot bypass the approval carrier",
                )
            finally:
                os.environ.pop("SOLET_ASSUME_YES", None)
        cli_module.CreateManager.preview = original_preview

        mismatch = cli_module.run([*resolved_base, "--yes", "--approval-fingerprint", "sha256:" + "0" * 64])
        _check(
            mismatch.error_kind == "probe_drift" and mismatch.data["approval_fingerprint"] == fingerprint,
            "well-formed mismatch returns probe_drift with the fresh preview token",
        )

        awaiting = CommandResult(
            kind="create_preview",
            status="awaiting_user",
            message="select a candidate",
            exit_code=ExitCode.HUMAN_ACTION,
            error_kind="decisions_required",
            data={
                "decision_prompts": [
                    {
                        "id": "embedding_model",
                        "title": "Embedding model",
                        "prompt": "Choose",
                        "candidates": [
                            {
                                "value": "model.one",
                                "label": "Model One",
                                "metadata": {},
                            }
                        ],
                    }
                ]
            },
        )
        with (
            patch.object(cli_module.CreateManager, "preview", return_value=awaiting),
            patch("builtins.input", side_effect=AssertionError("JSON mode must not call input")),
            patch.object(sys, "stdin", _Tty()),
        ):
            side_channel = StringIO()
            with redirect_stdout(side_channel):
                json_awaiting = cli_module.run([*resolved_base, "--json"])
            _check(
                json_awaiting.error_kind == "decisions_required" and side_channel.getvalue() == "",
                "TTY JSON returns the typed candidate result with no prompt output or input",
            )

        with (
            patch("builtins.input", side_effect=AssertionError("dry-run JSON must not call input")),
            patch.object(sys, "stdin", _Tty()),
        ):
            rendered = StringIO()
            try:
                with redirect_stdout(rendered):
                    cli_module.main([*resolved_base, "--dry-run", "--json"])
            except SystemExit as exc:
                _check(exc.code == 0, "dry-run JSON exits successfully")
            raw_document = rendered.getvalue()
            document, consumed = json.JSONDecoder().raw_decode(raw_document)
            _check(
                document["status"] == "preview_ready" and not raw_document[consumed:].strip(),
                "dry-run TTY JSON emits exactly one JSON document",
            )

        called: list[str] = []
        original = cli_module.CreateManager.create

        def accepted(
            _manager: object,
            _config: object,
            *,
            approved_fingerprint: str,
            decision_selections: dict[str, object] | None = None,
            decision_source: str = "flag",
            decision_sources: dict[str, str] | None = None,
        ) -> CommandResult:
            del decision_selections, decision_source, decision_sources
            called.append(approved_fingerprint)
            return CommandResult(
                kind="fixture",
                status="verified",
                message="accepted",
                exit_code=ExitCode.OK,
            )

        try:
            cli_module.CreateManager.create = accepted  # type: ignore[method-assign]
            exact = cli_module.run([*resolved_base, "--yes", "--approval-fingerprint", fingerprint])
            _check(
                exact.status == "verified" and called == [fingerprint],
                "exact reviewed token reaches the create boundary",
            )

            _write_seed(seed, release_tag="release-2")
            drift = cli_module.run([*resolved_base, "--yes", "--approval-fingerprint", fingerprint])
            _check(
                drift.error_kind == "probe_drift" and len(called) == 1,
                "changed source probe refuses before create boundary",
            )
        finally:
            cli_module.CreateManager.create = original  # type: ignore[method-assign]

    _prompt_discovered_decisions_ui_helper()

    print(f"cli_approval_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
