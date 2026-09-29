"""iss_a047d26c / iss_389fda4c: ``solet`` points at ``solet-manager`` and states what ``solet inspect`` does."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager.cli import build_parser, main as solet_main  # noqa: E402, I001


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            solet_main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def _top_help() -> str:
    return build_parser().format_help()


def _inspect_help() -> str:
    parser = build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices and "inspect" in a.choices)
    return " ".join(sub.choices["inspect"].format_help().split())


def _check_top_help() -> None:
    text = " ".join(_top_help().split())
    for verb in ("import", "update", "inspect"):
        assert f"solet-manager {verb}" in text, f"solet --help does not name `solet-manager {verb}`"


def _check_pointers() -> None:
    for verb, tail in (("update", ["fixture", "--dry-run"]), ("import", ["fixture", "--target", "/tmp/x", "--channel", "stable", "--dry-run"])):
        code, out, err = _invoke([verb, *tail])
        pointer = f"solet-manager {verb} {' '.join(tail)}"
        assert code != 0, f"solet {verb} exited 0"
        assert pointer in err, f"solet {verb}: stderr {err!r} lacks {pointer!r}"
        assert len(err.strip().splitlines()) == 1 and not out.strip(), f"solet {verb}: not a one-line pointer: {err!r}{out!r}"


def _check_inspect_help() -> None:
    text = _inspect_help()
    assert "Passively" not in text and "passive" not in text.lower().replace("not passive", ""), text
    for phrase in ("may run", "solet-manager inspect"):
        assert phrase in text, f"solet inspect help lacks {phrase!r}: {text}"


def main() -> int:
    _check_top_help()
    _check_pointers()
    _check_inspect_help()
    print("solet_cli_manager_pointer_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
