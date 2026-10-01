"""iss_d1f3371b (r65): the seed-side half of adopting a hand-made LaunchAgent plist (Dax Part 57 section 57.2, public #85).

Hermetic, on the same fake ``Runtime`` and Git target ``existing_install_operations_smoke`` builds.  Legs:

- Dax's case: this solet's own ``legacy_direct`` plist, hand-made (reordered keys, 4-space indent, a narrower ``PATH``, no stamp), probes
  ``pending`` with ``unknown_origin``/``render_whole``, the digest of the bytes it replaces, and a bounded diff that shows the ``PATH`` change
  both ways; the apply writes the stamped render and the next probe reads ``stamped_current`` with no diff;
- refusals: a materialized-supervisor plist and a plist for another label still block with no diff and no write, and the repair names the
  runbook step; so does a plist the seed cannot parse (truncated or mismatched XML, a bad integer, date or real, a key with no value, an unknown
  encoding, binary garbage, a non-dict root), which blocks cleanly instead of raising;
- the diff is at most 80 lines of at most 200 characters and never carries the value of a key named like a secret, whether the key and value share
  a line, the string spans lines, the value is ``<data>``, it sits in a nested dict, the key ends in ``_PAT``, ``_PASS``, ``_KEY`` or names a
  passphrase, or the value is written with ``&apos;``/``&quot;``, a character reference, CDATA, ``xml:space``, a line break in the tags or a CDATA
  that holds a closing tag, including the same secret repeated under a plain key in another encoding, inside a longer string, or a short one;
  whole-word names such as ``KEYBOARD`` and ``PATH`` stay readable; the diff is of the parsed values, so key order and layout are no change; and no
  diff at all goes out when a plist cannot be parsed; every diff line passes the Manager's real ``public_string`` (a Homebrew keg path, a
  secret-shaped plain argument and the byte bound, each refused raw as a control);
- only a ``launchd_plist`` artifact is adopted: plist-shaped bytes in the ``/feedback`` skill are reported and left.
"""

from __future__ import annotations

import base64
import plistlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch
from xml.parsers.expat import ExpatError

_REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(_REPO / "solet_cli" / "src"), str(_REPO / "solet_setup_contracts" / "src")]
import existing_install_operations_smoke as base  # noqa: E402
from existing_install_operations_smoke import FakeRuntime, _apply, _inputs, _probe, _request, _skill_target, _state, _target  # noqa: E402
from github_midwife_plugin.autostart import render_launchagent_plist  # noqa: E402
from github_midwife_plugin.managed_render import sha256_bytes  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from solet_manager.adapter_validation import public_string, public_value  # noqa: E402

_NAME = base._NAME  # noqa: SLF001 -- the fixture's solet name
_CHECKS = 0
_ARTIFACT = "instance_launchagent_plist"


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _dax_plist(target: Path, runtime: FakeRuntime, *, label: str | None = None, path_env: str = "/usr/bin:/bin:/usr/sbin:/sbin") -> bytes:
    """Dax's shape (Part 57 section 57.2): this solet's legacy_direct launch, hand-made: reordered keys, 4-space indent, a narrower PATH, no stamp."""
    arguments = [f"{target}/.venv/bin/python3", "-m", "ananta.cli", "--app-home", f"{target}/profile"]
    hand_made = {
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {"PATH": path_env, "SOLET_NAME": _NAME},
        "WorkingDirectory": f"{runtime.home}/.ananta/runtime/{_NAME}",
        "ProgramArguments": arguments,
        "Label": label or f"local.solet.{_NAME}",
    }
    return plistlib.dumps(hand_made, sort_keys=False).replace(b"\t", b"    ")


def _diff_lines(result: dict[str, Any]) -> list[str]:
    for item in result["evidence"]:
        if item["id"] == "adopt_diff.instance_launchagent_plist":
            return [line.partition("=")[2] for line in item["observed"]]
    return []


def _adopt_fixture(root: Path) -> tuple[Path, FakeRuntime, Path]:
    target, _ = _target(root)
    runtime = FakeRuntime(root / "home")
    (runtime.home / "Library" / "LaunchAgents").mkdir(parents=True)
    return target, runtime, runtime.home / "Library" / "LaunchAgents" / f"local.solet.{_NAME}.plist"


_ADOPT_REF = "existing::autostart.reconcile"


def _check_dax_diff(lines: list[str]) -> None:
    _check(any(line.startswith("-") and "<string>/usr/bin:/bin:/usr/sbin:/sbin</string>" in line for line in lines), "the diff shows the narrower PATH being removed")
    _check(any(line.startswith("+") and "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:" in line for line in lines), "the diff shows the full PATH being added")
    _check(len(lines) <= 81 and all(len(line) <= 200 for line in lines), "the diff is bounded")


def _check_plist_adopt(root: Path) -> None:
    """iss_d1f3371b: a Manager-owned plist that matches no release's render is adopted through ``render_whole`` when this solet owns it."""
    target, runtime, plist = _adopt_fixture(root)
    candidate = render_launchagent_plist(_NAME, target, runtime.home, template_text=None, stamp=None, stamped=True)
    # A1: Dax's case is pending with an adopt action and a diff that shows the PATH change both ways.
    dax = _dax_plist(target, runtime)
    plist.write_bytes(dax)
    probe = _probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist")
    state = _state(probe, "instance_launchagent_plist")
    _check((probe["checkpoint_status"], state["state"], state["action"], state["conflict"]) == ("pending", "unknown_origin", "render_whole", "none"), f"Dax's plist is adopted, not blocked: {probe['checkpoint_status']} {state}")
    _check(state["current_sha256"] == sha256_bytes(dax) and state["expected_sha256"] == sha256_bytes(candidate), "the row binds the digest of the bytes it replaces and of the render that replaces them")
    _check_dax_diff(_diff_lines(probe))
    _check(plist.read_bytes() == dax, "a probe never writes")
    # A2: the apply writes the render, and the file then reads back as current.
    applied = _apply(target, runtime, _ADOPT_REF, "instance_launchagent_plist")
    _check(applied["checkpoint_status"] == "applied" and plist.read_bytes() == candidate, "the apply replaces Dax's plist with the stamped render")
    after = _probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist", purpose="post_apply")
    _check(after["checkpoint_status"] == "verified" and _state(after, "instance_launchagent_plist")["state"] == "stamped_current" and not _diff_lines(after), "after the apply the plist is stamped_current with no diff")


def _check_plist_adopt_refusals(root: Path) -> None:
    """A4/A5: a plist this solet does not own as a legacy_direct launch (a supervisor, another label) still blocks, with no diff and no write."""
    target, runtime, plist = _adopt_fixture(root)
    supervisor = plistlib.dumps({"Label": f"local.solet.{_NAME}", "ProgramArguments": [f"{runtime.home}/.ananta/releases/current/venv/bin/python3", "-m", "macos_self_deployment_plugin.supervisor"]})
    plist.write_bytes(supervisor)
    blocked = _probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist")
    _check(blocked["error_kind"] == "managed_block_unknown_origin" and not _diff_lines(blocked) and plist.read_bytes() == supervisor, "a materialized_supervisor plist still blocks, with no diff")
    foreign = _dax_plist(target, runtime, label="local.solet.other")
    plist.write_bytes(foreign)
    wrong_label = _probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist")
    _check(wrong_label["error_kind"] == "managed_block_unknown_origin" and not _diff_lines(wrong_label), "a plist with the wrong label still blocks")
    refused = cast(dict[str, Any], dispatch_request(_request(target, _ADOPT_REF, phase="apply", inputs=_inputs(runtime, target, "instance_launchagent_plist")), runtime))
    _check(refused["error_kind"] == "managed_block_unknown_origin" and plist.read_bytes() == foreign, "an apply over the wrong-label plist refuses and writes nothing")
    _check("not a legacy_direct plist for this solet" in wrong_label["repair"] and "Part C Step 5" in wrong_label["repair"], "the guard refusal's repair says why and names the runbook step")
    # the r64 repair stays for a block: a plist is the only artifact whose repair text changed
    _check(_state(wrong_label, "instance_launchagent_plist")["action"] == "none", "a refused plist plans no write")


def _dax_xml(target: Path, env_extra: str, *, path_env: str = "/usr/bin:/bin") -> bytes:
    """Dax's hand-made plist written as XML text, the compact way a person writes one, with ``env_extra`` inside ``EnvironmentVariables``."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>local.solet.{_NAME}</string>
    <key>ProgramArguments</key>
    <array><string>{target}/.venv/bin/python3</string><string>-m</string><string>ananta.cli</string><string>--app-home</string><string>{target}/profile</string></array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{path_env}</string>
        {env_extra}
    </dict>
</dict>
</plist>
""".encode()


_SECRET_DATA = b"secret-bytes-for-the-data-value"
_ENCODED_DATA = base64.b64encode(_SECRET_DATA).decode()
_LEAK_LAYOUTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("key and value on one line", "<key>API_TOKEN</key><string>SEKRIT-ONE-LINE</string>", ("SEKRIT-ONE-LINE",)),
    ("a multi-line string", "<key>GITHUB_TOKEN</key>\n<string>MULTI-LINE-SECRET-A\nMULTI-LINE-SECRET-B</string>", ("MULTI-LINE-SECRET-A", "MULTI-LINE-SECRET-B")),
    ("a wrapped data value", f"<key>PRIVATE_KEY</key>\n<data>\n{_ENCODED_DATA[:20]}\n{_ENCODED_DATA[20:]}\n</data>", (_ENCODED_DATA[:20], _ENCODED_DATA[20:])),
    ("a nested dict under a secret-named key", "<key>CREDENTIALS</key><dict><key>user</key><string>NESTED-SECRET-USER</string><key>inner</key><dict><key>deep</key><string>DEEP-SECRET</string></dict></dict>", ("NESTED-SECRET-USER", "DEEP-SECRET")),
    ("a PAT", "<key>GH_PAT</key><string>ghp_PAT-SECRET-VALUE</string>", ("ghp_PAT-SECRET-VALUE",)),
    ("a PASS", "<key>DB_PASS</key><string>DB-PASS-SECRET-VALUE</string>", ("DB-PASS-SECRET-VALUE",)),
    ("a PASSPHRASE", "<key>SSH_PASSPHRASE</key><string>ssh-passphrase-secret-value</string>", ("ssh-passphrase-secret-value",)),
    ("a *_KEY suffix", "<key>STRIPE_KEY</key><string>sk_live_STRIPE-SECRET-VALUE</string>", ("sk_live_STRIPE-SECRET-VALUE",)),
    ("&apos; and &quot; in the secret", "<key>API_TOKEN</key><string>ab&apos;cd&quot;ef9876</string>", ("ab&apos;cd&quot;ef9876", "ef9876")),
    ("a decimal character reference", "<key>GITHUB_TOKEN</key><string>&#103;hp_NUMERIC5555</string>", ("&#103;hp_NUMERIC5555", "NUMERIC5555")),
    ("a hex character reference in the middle", "<key>GITHUB_TOKEN</key><string>ghp_HEX&#x41;MID7777</string>", ("HEX&#x41;MID7777", "MID7777")),
    (
        "the secret repeated under a plain key in another encoding",
        "<key>API_TOKEN</key><string>x&amp;y_SECRET_88</string>\n        <key>MIRROR</key><string>x&#38;y_SECRET_88</string>",
        ("x&amp;y_SECRET_88", "x&#38;y_SECRET_88", "SECRET_88"),
    ),
    (
        "the secret inside a longer string under a plain key",
        "<key>API_TOKEN</key><string>x&amp;y_SECRET_88</string>\n        <key>CMDLINE</key><string>run --with x&#38;y_SECRET_88 now</string>",
        ("x&amp;y_SECRET_88", "x&#38;y_SECRET_88", "SECRET_88"),
    ),
    ("xml:space on the string element", '<key>API_TOKEN</key><string xml:space="preserve">&#83;ECRET-XMLSPACE-77</string>', ("ECRET-XMLSPACE-77",)),
    ("a line break inside the string tags", "<key>API_TOKEN</key><string\n>NEWLINE-TAG-SECRET-55</string\n>", ("NEWLINE-TAG-SECRET-55",)),
    ("a CDATA holding a closing string tag, then an entity tail", "<key>API_TOKEN</key><string><![CDATA[</string>]]>tail&#65;&#66;-SECRET-CDATA-44</string>", ("SECRET-CDATA-44", "&#65;&#66;")),
    (
        "secret repeated as a list item under a plain key",
        "<key>API_TOKEN</key><string>LISTED-SECRET-66</string>\n        <key>ARGS</key><array><string>run</string><string>LISTED-SECRET-66</string></array>",
        ("LISTED-SECRET-66",),
    ),
    ("an integer under a secret-named key", "<key>PIN_TOKEN</key><integer>48151623</integer>", ("48151623",)),
    (
        "a data secret repeated under a plain key",
        f"<key>ENC_KEY</key><data>{_ENCODED_DATA}</data>\n        <key>COPY</key><data>{_ENCODED_DATA}</data>",
        (_ENCODED_DATA[:20],),
    ),
    ("secret-shaped text under a plain key", "<key>CMDLINE</key><string>run --token=PLAINKEY-TOKEN-99 now</string>", ("PLAINKEY-TOKEN-99",)),
    ("an uppercase Bearer header under a plain key", "<key>HEADER</key><string>Bearer UPPER-BEARER-55</string>", ("UPPER-BEARER-55",)),
    ("an uppercase --TOKEN= argument under a plain key", "<key>CMDLINE</key><string>run --TOKEN=UPPER-TOKEN-66 now</string>", ("UPPER-TOKEN-66",)),
    ("a 3-character CDATA secret", "<key>HF_TOKEN</key><string><![CDATA[a<b]]></string>", ("CDATA", "a<b")),
    ("a short secret repeated under a plain key", "<key>HF_TOKEN</key><string>a&lt;b</string>\n        <key>ECHO</key><string>a&#60;b</string>", ("a&lt;b", "a&#60;b")),
)
#: Names that look like secrets and are not: their values stay readable, so the name set stays whole-word.
_READABLE_LAYOUT = "<key>KEYBOARD_LAYOUT</key><string>us-layout-visible</string>\n        <key>KEYCHAIN_NAME</key><string>login-keychain-visible</string>\n        <key>PASSENGER_COUNT</key><string>passengers-visible</string>\n        <key>PATCH_LEVEL</key><string>patch-level-visible</string>"


def _check_plist_adopt_redaction(root: Path) -> None:
    """The diff lands in the preview and the journal, so the value under a secret-named key never reaches it, in any layout a plist can be written in."""
    for index, (label, layout, leaks) in enumerate(_LEAK_LAYOUTS):
        target, runtime, plist = _adopt_fixture(root / f"layout-{index}")
        plist.write_bytes(_dax_xml(target, f"{layout}\n        <key>LOG_LEVEL</key><string>debug-visible</string>"))
        probe = _probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist")
        diff = "\n".join(_diff_lines(probe))
        _check(probe["checkpoint_status"] == "pending" and diff, f"{label}: the plist is still adopted")
        _check(not [leak for leak in leaks if leak in diff], f"{label}: the secret value never reaches the diff")
        _check("[REDACTED]" in diff and "debug-visible" in diff and "<string>/usr/bin:/bin</string>" in diff, f"{label}: only the secret is redacted; the rest of the diff stays readable")
    _check("PATH" not in "\n".join(line for line in _diff_lines(probe) if "REDACTED" in line), "PATH is not mistaken for a PAT")


def _check_plist_adopt_readable_names(root: Path) -> None:
    """The secret-name set is whole-word: KEYBOARD, KEYCHAIN, PASSENGER, PATCH and PATH values stay in the diff."""
    target, runtime, plist = _adopt_fixture(root)
    plist.write_bytes(_dax_xml(target, _READABLE_LAYOUT))
    diff = "\n".join(_diff_lines(_probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist")))
    for visible in ("us-layout-visible", "login-keychain-visible", "passengers-visible", "patch-level-visible", "<string>/usr/bin:/bin</string>"):
        _check(visible in diff, f"{visible} stays readable: its key is not secret-named")


def _check_plist_adopt_diff_withheld_when_unreadable(root: Path) -> None:
    """If the secret values cannot be collected, no diff goes out (fail closed); the adopt decision itself does not change."""
    target, runtime, plist = _adopt_fixture(root)
    plist.write_bytes(_dax_xml(target, "<key>API_TOKEN</key><string>SEKRIT-WITHHELD-VALUE</string>"))
    with patch.object(plistlib, "loads", side_effect=ExpatError("the secret scan could not parse the plist")):
        probe = _probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist")
    state = _state(probe, "instance_launchagent_plist")
    _check((probe["checkpoint_status"], state["state"], state["action"], state["conflict"]) == ("pending", "unknown_origin", "render_whole", "none"), "the plist is still adopted; only its diff is withheld")
    lines = _diff_lines(probe)
    _check(len(lines) == 1 and lines[0].startswith("diff withheld") and "SEKRIT" not in lines[0], f"one line says the diff was withheld and why, and carries no plist text ({lines})")


def _diff_item(result: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in result["evidence"] if item["id"] == "adopt_diff.instance_launchagent_plist")


def _manager_refuses(text: str) -> bool:
    """Whether the Manager's real ``public_string`` refuses ``text`` as evidence (the control: the input really breaks a rule)."""
    try:
        public_string(text, "evidence observed", maximum=4096)
    except (TypeError, ValueError):
        return True
    return False


_KEG_PATH = "/opt/homebrew/Cellar/solet/0.1.0_64/libexec/bin:/usr/bin:/bin"
_WIDE = "\U0001f600" * 2100  # 2100 characters, 8400 bytes: inside the 4096-character bound, over the 8192-byte one
#: (label, plist ``PATH``, extra environment XML, text the Manager refuses raw, text the diff keeps)
_EVIDENCE_CASES: tuple[tuple[str, str, str, str, str], ...] = (
    ("Dax's Homebrew keg PATH", _KEG_PATH, "", _KEG_PATH, "[keg path]0.1.0_64/libexec/bin"),
    ("a secret-shaped plain argument", "/usr/bin:/bin", "<key>EXTRA_ARGS</key><string>run --token=PLAIN-ARG-SECRET-7 now</string>", "run --token=PLAIN-ARG-SECRET-7 now", "<key>EXTRA_ARGS</key>"),
    ("an uppercase Bearer header", "/usr/bin:/bin", "<key>HEADER</key><string>Bearer UPPER-BEARER-77</string>", "Bearer UPPER-BEARER-77", "<key>HEADER</key>"),
    ("an uppercase --TOKEN= argument", "/usr/bin:/bin", "<key>EXTRA_ARGS</key><string>run --TOKEN=UPPER-TOKEN-88 now</string>", "run --TOKEN=UPPER-TOKEN-88 now", "<key>EXTRA_ARGS</key>"),
    ("the byte-bound worst case", "/usr/bin:/bin", f"<key>BIG</key><string>{_WIDE}</string>", _WIDE, "<key>BIG</key>"),
)


def _check_plist_adopt_diff_is_valid_evidence(root: Path) -> None:
    """The diff lands in the Manager's evidence, which refuses a keg path, secret-shaped text and too many bytes by raising; the real validator accepts every line."""
    for index, (label, path_env, extra, control, kept) in enumerate(_EVIDENCE_CASES):
        target, runtime, plist = _adopt_fixture(root / f"evidence-{index}")
        _check(_manager_refuses(control), f"{label}: control: the Manager's public_string refuses the raw text")
        plist.write_bytes(_dax_xml(target, extra, path_env=path_env))
        probe = _probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist")
        _check(probe["checkpoint_status"] == "pending", f"{label}: the plist is still adopted")
        item = _diff_item(probe)
        try:
            public_value(item["observed"], "evidence observed")
            accepted = True
        except (TypeError, ValueError):
            accepted = False
        _check(accepted, f"{label}: every adopt_diff line passes the Manager's real public_string")
        diff = "\n".join(_diff_lines(probe))
        _check(kept in diff and "/Cellar/solet/" not in diff and "PLAIN-ARG-SECRET-7" not in diff and "UPPER-BEARER-77" not in diff and "UPPER-TOKEN-88" not in diff, f"{label}: the diff stays readable ({kept!r}) and carries no refused text")


def _check_plist_adopt_diff_is_of_values(root: Path) -> None:
    """The diff compares parsed values, sorted by key: the same values in another key order, layout or indent are no change at all."""
    target, runtime, plist = _adopt_fixture(root)
    candidate = plistlib.loads(render_launchagent_plist(_NAME, target, runtime.home, template_text=None, stamp=None, stamped=True))
    reordered = dict(reversed(list(candidate.items())))
    plist.write_bytes(plistlib.dumps(reordered, sort_keys=False).replace(b"\t", b"    "))
    probe = _probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist")
    _check(_state(probe, "instance_launchagent_plist")["action"] == "render_whole", "a plist with the render's values in another key order is still adopted")
    lines = _diff_lines(probe)
    _check(len(lines) == 1 and "same values" in lines[0], f"key order and layout are no change ({lines})")


_UNPARSEABLE: tuple[tuple[str, bytes], ...] = (
    ("truncated XML", b'<?xml version="1.0"?><plist version="1.0"><dict><key>Label</key><string>local.solet.iris'),
    ("a mismatched tag", b'<?xml version="1.0"?><plist version="1.0"><dict><key>Label</key></array></dict></plist>'),
    ("a bad integer", b'<plist version="1.0"><dict><key>a</key><integer>x</integer></dict></plist>'),
    ("a bad date", b'<plist version="1.0"><dict><key>a</key><date>nope</date></dict></plist>'),
    ("a bad real", b'<plist version="1.0"><dict><key>a</key><real>zz</real></dict></plist>'),
    ("a key with no value", b'<plist version="1.0"><dict><key>a</key></dict></plist>'),
    ("an unknown encoding", b'<?xml version="1.0" encoding="UTY-8"?><plist version="1.0"><dict/></plist>'),
    ("binary garbage that decodes as text", b"bplist00" + b"\x01\x02" * 20),  # non-UTF-8 bytes raise in ``read_text`` on base too: iss_68bc97bb-bd9e-41ed-9d81-fcabb93ea751
    ("a non-dict root", b'<plist version="1.0"><array/></plist>'),
)


def _check_plist_adopt_unparseable(root: Path) -> None:
    """A plist the seed cannot parse is not this solet's to adopt: it blocks cleanly with the existing repair, the way it did before r65."""
    for index, (label, raw) in enumerate(_UNPARSEABLE):
        target, runtime, plist = _adopt_fixture(root / f"case-{index}")
        plist.write_bytes(raw)
        probe = _probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist")
        _check(probe["error_kind"] == "managed_block_unknown_origin" and not _diff_lines(probe), f"{label}: blocks as managed_block_unknown_origin with no diff")
        _check("Part C Step 5" in probe["repair"] and _state(probe, "instance_launchagent_plist")["action"] == "none", f"{label}: the repair names the runbook step and no write is planned")
        refused = cast(dict[str, Any], dispatch_request(_request(target, _ADOPT_REF, phase="apply", inputs=_inputs(runtime, target, "instance_launchagent_plist")), runtime))
        _check(refused["error_kind"] == "managed_block_unknown_origin" and plist.read_bytes() == raw, f"{label}: an apply refuses and writes nothing")


def _check_plist_adopt_bounds(root: Path) -> None:
    """The diff is at most 80 lines of at most 200 characters however much differs, and only a ``launchd_plist`` artifact is ever adopted."""
    target, runtime, plist = _adopt_fixture(root)
    heavy = plistlib.loads(_dax_plist(target, runtime))
    heavy["EnvironmentVariables"].update({f"EXTRA_{index:03d}": "x" * 400 for index in range(150)})
    plist.write_bytes(plistlib.dumps(heavy, sort_keys=False).replace(b"\t", b"    "))
    lines = _diff_lines(_probe(target, runtime, _ADOPT_REF, "instance_launchagent_plist"))
    _check(len(lines) == 81 and "more diff lines not shown" in lines[-1], f"a 300-line diff is cut at 80 lines and says how much was not shown ({len(lines)})")
    _check(max(len(line) for line in lines[:-1]) == 200, "a long line is cut at 200 characters")


def _check_plist_adopt_kind_guard(root: Path) -> None:
    """Plist-shaped bytes in the /feedback skill (a ``rendered_whole`` file) are reported and left, never adopted."""
    target = _skill_target(root)
    runtime = FakeRuntime(root / "home")
    skill = runtime.home / ".claude" / "skills" / "feedback" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(_dax_plist(target, runtime))
    probe = _probe(target, runtime, "existing::hydration.reconcile", "feedback_skill")
    state = _state(probe, "feedback_skill")
    _check((state["state"], state["action"], state["conflict"]) == ("unknown_origin", "none", "none") and not _diff_lines(probe), "plist-shaped bytes in a non-plist artifact are never adopted")


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _check_plist_adopt(root / "adopt")
        _check_plist_adopt_refusals(root / "adopt-refusals")
        _check_plist_adopt_redaction(root / "adopt-redaction")
        _check_plist_adopt_readable_names(root / "adopt-readable")
        _check_plist_adopt_diff_withheld_when_unreadable(root / "adopt-withheld")
        _check_plist_adopt_diff_is_of_values(root / "adopt-values")
        _check_plist_adopt_diff_is_valid_evidence(root / "adopt-evidence")
        _check_plist_adopt_unparseable(root / "adopt-unparseable")
        _check_plist_adopt_bounds(root / "adopt-bounds")
        _check_plist_adopt_kind_guard(root / "adopt-kind")
    print(f"existing_install_plist_adopt_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
