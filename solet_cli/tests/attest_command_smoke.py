"""``solet attest`` writes one attestation that says what it measured.

Drives ``run_attest`` end to end over a constructed manager home: a
registered instance whose target is a real throwaway git checkout with a
fake ``.venv/bin/solet-bridge`` answering ``attest_runtime_code``, a
package directory standing in for the installed ``solet_manager``, and a
keg ``share/solet`` holding the v3 seed lock, the v1 receipt and a schema-v1
manifest whose digests were computed from those fixture bytes.

Pinned: every section is present with its grade; a fully matching
installation is ``verified`` at exit 0; one edited manager file turns the
result ``drifted`` at exit 3 with the path named; a stopped solet makes the
runtime section ``unattestable[solet_not_running]`` (never verified) while
the disk sections still verify; no manifest gives ``partial`` with the
measurements still recorded; the document carries the NOT CRYPTOGRAPHIC
header; ``--against <tag>`` resolves through an injected ``gh`` and refuses
a tag without a repository; the CLI parser accepts the documented flags.

Offline: the runner seam answers ``sw_vers``/``brew``/``gh``; git and the
fake bridge run for real inside the temporary tree; the model endpoint
points at a closed loopback port so its probe records an error, not a
guess.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(Path(__file__).resolve().parent)]

import json  # noqa: E402
import platform  # noqa: E402
import tempfile  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from typing import Any  # noqa: E402

from _release_identity_fixture import SOURCE_COMMIT, build_package, build_seed_checkout, fake_bridge, manifest, seed_lock_v3, write_json, write_receipt  # noqa: E402
from solet_manager.attest import ATTESTATION_FILE_NAME, AttestRequest, AttestSeams, run_attest  # noqa: E402
from solet_manager.cli import build_parser  # noqa: E402
from solet_manager.errors import SourceError  # noqa: E402
from solet_manager.models import InstanceRecord, JsonValue  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_identity import CommandOutcome, run_command  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256, write_transaction  # noqa: E402

_CHECKS = 0
_SURFACE = "sha256:" + "7" * 64
_CLOSED_ENDPOINT = "http://127.0.0.1:9/api/v0/models"


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths = ManagerPaths.resolve(explicit_home=root / "home")
        self.target = root / "Solets" / "fixture"
        self.seed = build_seed_checkout(self.target)
        self.package = root / "pkg"
        self.digests = build_package(self.package)
        share = root / "keg" / "share" / "solet"
        self.seed_lock = write_json(share / "seed.lock.json", seed_lock_v3(self.seed))
        self.receipt = write_receipt(share / "install-source.json")
        self.manifest_path = write_json(share / "release_manifest.json", manifest(seed=self.seed, file_digests=self.digests, surface_sha256=_SURFACE))
        self.gh_calls: list[Sequence[str]] = []
        self.brew_calls: list[tuple[str, ...]] = []
        self._register()

    def _register(self) -> None:
        record = InstanceRecord(
            name="fixture", target=str(self.target), launcher=str(self.target / "client" / "bin" / "fixture"),
            seed_repository="https://github.com/solet-public/macos-bizops.git", seed_tag="release-2026-09-19-fixture",
            seed_commit=self.seed["commit"], seed_tree_hash=self.seed["tree_hash"], profile="macos-bizops",
            flow_id="fixture-flow", flow_source_revision="a" * 40, flow_contract_digest="sha256:" + "c" * 64,
            created_at="2026-09-19T00:00:00+00:00", updated_at="2026-09-19T00:00:00+00:00",
        )
        InstanceRegistry(self.paths.registry_path).add(record)
        seed = SeedLock("https://github.com/solet-public/macos-bizops.git", "release-2026-09-19-fixture", self.seed["commit"], self.seed["tree_hash"], "e" * 64, "macos-bizops")
        answers: dict[str, JsonValue] = {"decisions": {}, "consents": {}}
        transaction = Transaction.create(name="fixture", target=self.target, input_fingerprint=canonical_sha256({"name": "fixture"}), answers=answers, seed=seed, flow_id="fixture-flow", flow_source_revision="a" * 40, flow_contract_digest="sha256:" + "c" * 64, stage_ids=("install",), completion_probe_ids=())
        write_transaction(self.paths.transaction_path("fixture"), transaction)

    def runner(self, argv: Sequence[str], cwd: Path | None, timeout: int) -> CommandOutcome:
        head = Path(argv[0]).name
        if head == "sw_vers":
            return CommandOutcome(0, "ProductName:\t\tmacOS\nProductVersion:\t\t26.0\nBuildVersion:\t\t25A354\n", "")
        if head == "brew":
            self.brew_calls.append(tuple(argv))
            return CommandOutcome(0, "Homebrew 5.0.0\n" if argv[1] == "--version" else "git 2.51.0\n", "")
        if head == "gh":
            self.gh_calls.append(tuple(argv))
            destination = Path(argv[argv.index("--dir") + 1]) / "release_manifest.json"
            write_json(destination, manifest(seed=self.seed, file_digests=self.digests, surface_sha256=_SURFACE))
            return CommandOutcome(0, "", "")
        return run_command(argv, cwd, timeout)

    def seams(self, *, manifest_path: Path | None = None) -> AttestSeams:
        return AttestSeams(runner=self.runner, package_root=self.package, install_source_path=self.receipt, manifest_path=self.manifest_path if manifest_path is None else manifest_path, seed_lock_path=self.seed_lock, models_endpoint=_CLOSED_ENDPOINT)

    def attest(self, *, manifest_path: Path | None = None, **request: Any) -> tuple[Any, dict[str, Any]]:
        result = run_attest(AttestRequest(self.paths, **request), self.seams(manifest_path=manifest_path))
        document = json.loads(Path(str(result.data["output"])).read_text(encoding="utf-8"))
        return result, document


def _assert_verified_installation(fixture: _Fixture) -> None:
    fake_bridge(fixture.target, {"schema_version": 1, "status": "attested", "release_surface_sha256": _SURFACE, "served_by_self": True, "self_pid": 4242})
    result, document = fixture.attest()
    _check(result.kind == "installation_attestation" and result.status == "verified" and int(result.exit_code) == 0, f"a matching installation is verified at exit 0: {result.status} {result.message}")
    _check(Path(str(result.data["output"])) == fixture.paths.state_dir / "attestations" / ATTESTATION_FILE_NAME, "the attestation lands in manager state by default")
    _check(document["format"] == "installation-attestation-v1" and "NOT CRYPTOGRAPHIC" in document["not_cryptographic"], "the document carries its format and the header")
    _check("NOT CRYPTOGRAPHIC" in result.message, "the header is printed on every result, passing ones included")
    _check(document["manifest"]["status"] == "loaded" and document["manifest"]["release_label"] == "r44" and str(document["manifest"]["sha256"]).startswith("sha256:"), "the manifest section names what was compared against")
    manager = document["manager"]
    _check(manager["status"] == "verified" and manager["files_hashed"] == len(fixture.digests) and manager["install_source"]["source_commit"] == SOURCE_COMMIT, f"the manager section verifies with real digests: {manager['status']}/{manager['reason']}")
    _check(document["pairing"]["verdict"] == "paired", "the pairing verdict is recorded")
    _assert_verified_instance_and_environment(fixture, document)


def _assert_verified_instance_and_environment(fixture: _Fixture, document: dict[str, Any]) -> None:
    instance = document["instances"][0]
    _check(instance["name"] == "fixture" and instance["seed_checkout"]["status"] == "verified", f"the seed checkout verifies: {instance['seed_checkout']['reason']}")
    components = {row["component"]: row["status"] for row in instance["seed_checkout"]["components"]}
    _check(components == {"plugin:alpha": "verified", "plugin:beta": "verified", "platform_base": "not_compared"}, "per-plugin subtree hashes are compared and non-plugin components are not")
    runtime = instance["runtime"]
    _check(runtime["status"] == "verified" and runtime["observed"]["self_pid"] == 4242, f"the RUNNING process's surface digest verifies through the target bridge: {runtime}")
    journal = instance["journal"]
    _check(journal["transaction"]["seed_commit"] == fixture.seed["commit"] and journal["registry"]["seed_tree_hash"] == fixture.seed["tree_hash"] and journal["applied_updates"] == [], "the journal identity and (empty) update history are recorded")
    environment = document["environment"]
    _check(environment["os"]["build_version"] == "25A354" and environment["homebrew"]["formulae"] == {"git": "2.51.0"}, f"environment facts come from the live queries: {environment}")
    _assert_closure_and_manager_python(fixture, environment)
    _check(environment["models_served"]["models"] is None and "unreachable" in environment["models_served"]["error"], "an unreachable inference server is an error, not an empty list")


def _assert_closure_and_manager_python(fixture: _Fixture, environment: dict[str, Any]) -> None:
    listed = [call for call in fixture.brew_calls if call[1:3] == ("list", "--versions")]
    _check(
        environment["homebrew"]["closure"] == ["git"] and listed != [] and all(call[3:] == ("git",) for call in listed),
        f"the queried closure is the Formula's own, which no longer names python@3.13 (r61): {environment['homebrew']} {listed}",
    )
    manager_python = environment["manager_python"]
    _check(
        manager_python["resolved"] == str(Path(sys.executable).resolve())
        and manager_python["executable"] == sys.executable
        and manager_python["version"] == platform.python_version()
        and manager_python["error"] is None,
        f"the attestation records the interpreter the manager venv actually runs on: {manager_python}",
    )


def _assert_drift_is_named(fixture: _Fixture) -> None:
    (fixture.package / "models.py").write_text("MANAGER_VERSION = 'edited'\n", encoding="utf-8")
    result, document = fixture.attest(output=fixture.root / "out" / "drift.json")
    _check(result.status == "drifted" and int(result.exit_code) == 3 and result.error_kind == "release_identity_drift", f"one edited manager file turns the result drifted at exit 3: {result.status}")
    _check(document["manager"]["drifted_paths"] == ["solet_cli/src/solet_manager/models.py"], "the drifted path is named in the document")
    _check(Path(str(result.data["output"])) == fixture.root / "out" / "drift.json", "--output relocates the document")
    (fixture.package / "models.py").write_text("MANAGER_VERSION = '0.1.0'\n", encoding="utf-8")


def _assert_stopped_solet_is_not_verified(fixture: _Fixture) -> None:
    fake_bridge(fixture.target, None, exit_code=4)
    result, document = fixture.attest(name="fixture")
    runtime = document["instances"][0]["runtime"]
    _check(runtime["status"] == "unattestable" and str(runtime["reason"]).startswith("solet_not_running"), f"a stopped solet is unattestable with its reason: {runtime}")
    _check(result.status == "partial" and int(result.exit_code) == 0 and document["manager"]["status"] == "verified", "the disk sections still verify and the overall result is partial, not verified")


def _assert_no_manifest_still_measures(fixture: _Fixture) -> None:
    result, document = fixture.attest(manifest_path=fixture.root / "absent.json")
    _check(document["manifest"]["status"] == "absent" and document["manager"]["status"] == "unattestable" and document["manager"]["reason"] == "release_manifest_absent", "no manifest is unattestable, named")
    _check(document["manager"]["files_hashed"] == len(fixture.digests) and document["instances"][0]["seed_checkout"]["head_commit"] == fixture.seed["commit"], "the measurements are recorded regardless")
    _check(result.status == "partial", "nothing verified means partial, never verified")


def _assert_against_tag(fixture: _Fixture) -> None:
    try:
        fixture.attest(against="manager-v0.1.0-r44")
    except SourceError as exc:
        _check("--release-repository" in str(exc), "a tag without a repository is refused, not guessed")
    else:
        raise AssertionError("red: a tag resolved without a repository")
    result, document = fixture.attest(against="manager-v0.1.0-r44", release_repository="solet-public/manager-releases")
    _check(len(fixture.gh_calls) == 1 and "solet-public/manager-releases" in fixture.gh_calls[0] and "release_manifest.json" in fixture.gh_calls[0], f"the tag resolves through ambient gh: {fixture.gh_calls}")
    _check(document["manifest"]["status"] == "loaded" and result.status in {"verified", "partial"}, "the downloaded manifest is what the comparison used")
    try:
        fixture.attest(against="../not a tag")
    except SourceError:
        _check(True, "a value that is neither a file nor a tag is refused")
    else:
        raise AssertionError("red: a malformed --against was accepted")


def _assert_cli_surface() -> None:
    args = build_parser().parse_args(["attest", "fixture", "--against", "manager-v0.1.0-r44", "--release-repository", "o/r", "--output", "/tmp/x.json", "--json"])
    _check(args.command == "attest" and args.name == "fixture" and args.against == "manager-v0.1.0-r44" and args.release_repository == "o/r" and args.json is True, "the documented flags parse")
    _check(build_parser().parse_args(["attest"]).name is None, "the instance name is optional")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _Fixture(Path(tmp))
        _assert_verified_installation(fixture)
        _assert_drift_is_named(fixture)
        _assert_stopped_solet_is_not_verified(fixture)
        _assert_no_manifest_still_measures(fixture)
        _assert_against_tag(fixture)
    _assert_cli_surface()
    print(f"attest_command_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
