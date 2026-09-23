"""Hermetic exact-tag acquisition and identity-negative smoke."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.errors import SourceError, SourceIdentityError  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.source_acquisition import (  # noqa: E402
    ReplaceRunner,
    RunResult,
    _final_git_http_status,
    materialize_locked_seed,
)

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class FakeGit:
    def __init__(
        self,
        seed: SeedLock,
        *,
        commit: str | None = None,
        tree: str | None = None,
        race_parent: Path | None = None,
        provenance_bundle: str | None = None,
        fetch_result: RunResult | None = None,
    ) -> None:
        self.seed = seed
        self.commit = commit or seed.commit
        self.tree = tree or seed.tree_hash
        self.race_parent = race_parent
        self.provenance_bundle = provenance_bundle or seed.profile
        self.fetch_result = fetch_result
        self.commands: list[tuple[str, ...]] = []
        origin_id = "123e4567-e89b-12d3-a456-426614174001"
        manifest_sha256 = "d" * 64
        seed_id = str(uuid.uuid5(uuid.UUID(origin_id), f"{seed.commit}:{manifest_sha256}::"))
        self.provenance_bytes = (
            json.dumps(
                {
                    "schema_version": 1,
                    "seed_id": seed_id,
                    "origin_id": origin_id,
                    "source_commit": seed.commit,
                    "manifest_sha256": manifest_sha256,
                    "bundle": {"name": self.provenance_bundle, "platform": "local"},
                    "source_date": "2026-09-15T00:00:00+00:00",
                    "lineage": [],
                    "ancestry": [],
                    "signature": None,
                },
                indent=2,
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n"
        )

    def __call__(self, command: Sequence[str], cwd: Path | None, _timeout: int) -> RunResult:
        cmd = tuple(command)
        self.commands.append(cmd)
        fetch_result = self._fetch_result(cmd)
        if fetch_result is not None:
            return fetch_result
        self._write_provenance(cmd, cwd)
        provenance_result = self._provenance_result(cmd)
        if provenance_result is not None:
            return provenance_result
        revision_result = self._revision_result(cmd)
        if revision_result is not None:
            return revision_result
        remote_result = self._remote_result(cmd)
        return remote_result or RunResult(0, "", "")

    def _fetch_result(self, command: tuple[str, ...]) -> RunResult | None:
        if command[:2] == ("git", "fetch"):
            return self.fetch_result
        return None

    def _write_provenance(self, command: tuple[str, ...], cwd: Path | None) -> None:
        if command[:2] != ("git", "checkout") or cwd is None:
            return
        (cwd / "PROVENANCE.json").write_text(self.provenance_bytes, encoding="utf-8")

    def _provenance_result(self, command: tuple[str, ...]) -> RunResult | None:
        if command == ("git", "show", "HEAD:PROVENANCE.json"):
            return RunResult(0, self.provenance_bytes, "")
        if command == ("git", "show", "-s", "--format=%B", "HEAD"):
            stamp = json.loads(self.provenance_bytes)
            return RunResult(
                0,
                "Seed bundle (factory-sealed)\n\n"
                f"Seed-Id: {stamp['seed_id']}\nOrigin-Id: {stamp['origin_id']}\n"
                f"Manifest-SHA256: {stamp['manifest_sha256']}\n"
                f"Assembled-Ref: {stamp['source_commit']}\n"
                "License-Policy: public_apache\nMinted-At: 2026-09-15T00:00:00+00:00\n",
                "",
            )
        return None

    def _revision_result(self, command: tuple[str, ...]) -> RunResult | None:
        if self._tag_commit_command(command) or command[:3] == (
            "git",
            "rev-parse",
            "FETCH_HEAD^{commit}",
        ):
            return RunResult(0, f"{self.commit}\n", "")
        if command[:3] == ("git", "rev-parse", f"{self.seed.commit}^{{tree}}"):
            return RunResult(0, f"{self.tree}\n", "")
        if command[:3] == ("git", "rev-parse", "main^{commit}"):
            self._replace_race_parent()
            return RunResult(0, f"{self.seed.commit}\n", "")
        return None

    def _tag_commit_command(self, command: tuple[str, ...]) -> bool:
        return self.seed.release_tag is not None and command[:3] == (
            "git",
            "rev-parse",
            f"refs/tags/{self.seed.release_tag}^{{commit}}",
        )

    def _replace_race_parent(self) -> None:
        if self.race_parent is None:
            return
        displaced = self.race_parent.with_name(f"{self.race_parent.name}-displaced")
        self.race_parent.rename(displaced)
        self.race_parent.mkdir(mode=0o700)

    def _remote_result(self, command: tuple[str, ...]) -> RunResult | None:
        if command[:4] == ("git", "remote", "get-url", "origin"):
            return RunResult(0, f"{self.seed.repository}\n", "")
        return None


# The authenticated viewer login and the repository OWNER must be the SAME
# value here, and that is load-bearing rather than cosmetic: source_acquisition
# classifies a 404 as MISSING only when the viewer owns the repository, and as
# PRIVATE_OR_MISSING otherwise. Splitting these into two different fixture
# constants silently downgrades the missing-repository case.
_FIXTURE_OWNER = "fixture-owner"


class FakeRepositoryProbe:
    def __init__(
        self,
        repository: str,
        *,
        http_status: int,
        visibility: str | None = None,
        viewer: str = _FIXTURE_OWNER,
    ) -> None:
        self.repository = repository
        self.http_status = http_status
        self.visibility = visibility
        self.viewer = viewer
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str], _cwd: Path | None, _timeout: int) -> RunResult:
        cmd = tuple(command)
        self.commands.append(cmd)
        if cmd[:3] == ("gh", "auth", "status"):
            return RunResult(0, "", "")
        if cmd[:3] == ("gh", "api", "--include"):
            body = json.dumps({"visibility": self.visibility})
            return RunResult(
                0 if self.http_status == 200 else 1,
                f"HTTP/2.0 {self.http_status} status\n\n{body}\n",
                "credential-like diagnostic must remain private",
            )
        if cmd == ("gh", "api", "user", "--jq", ".login"):
            return RunResult(0, f"{self.viewer}\n", "")
        raise AssertionError(f"unexpected repository-probe command: {cmd!r}")


def _seed_at(repository: str) -> SeedLock:
    return SeedLock(
        repository,
        "release-2026-08-24.5",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "fixture-profile",
    )


def _tagless_seed() -> SeedLock:
    return SeedLock(
        "https://github.com/solet-public/tagless-seed.git",
        None,
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "tagless-profile",
    )


def _capture_fetch_failure(
    seed: SeedLock,
    target: Path,
    cache: Path,
    *,
    fetch_detail: str,
    probe: FakeRepositoryProbe,
    fetch_http_status: int | None = None,
) -> SourceError:
    try:
        materialize_locked_seed(
            seed,
            target,
            cache_dir=cache,
            runner=FakeGit(
                seed,
                fetch_result=RunResult(128, "", fetch_detail, fetch_http_status),
            ),
            repository_probe_runner=probe,
        )
    except SourceError as exc:
        return exc
    raise AssertionError("failed fetch was not refused")


def _check_repository_classification(root: Path) -> None:
    private_seed = _seed_at(f"https://github.com/{_FIXTURE_OWNER}/fixture-private-repo.git")
    missing_seed = _seed_at(f"https://github.com/{_FIXTURE_OWNER}/fixture-missing-repo.git")
    ambiguous_fetch = (
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
    )
    private_probe = FakeRepositoryProbe(
        private_seed.repository,
        http_status=200,
        visibility="private",
    )
    private_error = _capture_fetch_failure(
        private_seed,
        root / "private",
        root / "cache-private",
        fetch_detail=ambiguous_fetch,
        probe=private_probe,
        fetch_http_status=401,
    )
    missing_probe = FakeRepositoryProbe(missing_seed.repository, http_status=404)
    missing_error = _capture_fetch_failure(
        missing_seed,
        root / "missing",
        root / "cache-missing",
        fetch_detail=ambiguous_fetch,
        probe=missing_probe,
        fetch_http_status=404,
    )
    _check(private_error.error_kind == "source_error", "private keeps source_error taxonomy")
    _check(missing_error.error_kind == "source_error", "missing keeps source_error taxonomy")
    _check(getattr(private_error, "reason", None) == "seed_repository_private", "private reason")
    _check(getattr(missing_error, "reason", None) == "seed_repository_missing", "missing reason")
    _check(str(private_error) != str(missing_error), "private and missing messages differ")
    _check(private_error.repair != missing_error.repair, "private and missing repairs differ")
    _check(
        private_error.repair is not None and "authorized Git access" in private_error.repair,
        "private recovery prioritizes authorized access",
    )
    _check(
        missing_error.repair is not None and "publish" in missing_error.repair,
        "missing recovery prioritizes publication or reference correction",
    )
    _check(not (root / "private").exists(), "private refusal promotes no target")
    _check(not (root / "missing").exists(), "missing refusal promotes no target")


def _check_non_access_false_positive(root: Path) -> None:
    public_seed = _seed_at("https://github.com/solet-public/public-seed.git")
    probe = FakeRepositoryProbe(
        public_seed.repository,
        http_status=200,
        visibility="public",
    )
    misleading = (
        "proxy TLS interruption while relaying text: repository not found; "
        "credential=https://token-do-not-copy@example.invalid"
    )
    error = _capture_fetch_failure(
        public_seed,
        root / "proxy-failure",
        root / "cache-proxy",
        fetch_detail=misleading,
        probe=probe,
        fetch_http_status=502,
    )
    _check(getattr(error, "reason", None) is None, "public transport failure has no access reason")
    _check(error.repair is None, "public transport failure retains generic recovery")
    _check("not anonymously readable" not in str(error), "stderr token cannot select refusal")

    successful_probe = FakeRepositoryProbe(
        public_seed.repository,
        http_status=200,
        visibility="public",
    )
    successful_target = root / "successful-fetch-with-stderr"
    result = materialize_locked_seed(
        public_seed,
        successful_target,
        cache_dir=root / "cache-successful-stderr",
        runner=FakeGit(public_seed, fetch_result=RunResult(0, "", misleading)),
        repository_probe_runner=successful_probe,
    )
    _check(result == successful_target, "successful fetch ignores access-like stderr")
    _check(not successful_probe.commands, "successful fetch performs no repository probe")

    missing_ref_probe = FakeRepositoryProbe(
        public_seed.repository,
        http_status=200,
        visibility="public",
    )
    missing_ref_error = _capture_fetch_failure(
        public_seed,
        root / "missing-ref",
        root / "cache-missing-ref",
        fetch_detail="fatal: could not find remote ref refs/tags/release-does-not-exist",
        probe=missing_ref_probe,
        fetch_http_status=200,
    )
    _check(getattr(missing_ref_error, "reason", None) is None, "missing ref is not missing repo")
    _check(missing_ref_error.repair is None, "missing ref retains generic recovery")
    _check(not missing_ref_probe.commands, "missing ref performs no repository probe")

    private_seed = _seed_at(f"https://github.com/{_FIXTURE_OWNER}/fixture-private-repo.git")
    private_missing_ref_probe = FakeRepositoryProbe(
        private_seed.repository,
        http_status=200,
        visibility="private",
    )
    private_missing_ref_error = _capture_fetch_failure(
        private_seed,
        root / "private-missing-ref",
        root / "cache-private-missing-ref",
        fetch_detail="fatal: could not find remote ref refs/tags/release-does-not-exist",
        probe=private_missing_ref_probe,
        fetch_http_status=200,
    )
    _check(
        getattr(private_missing_ref_error, "reason", None) is None,
        "missing ref in private repo is not an access failure",
    )
    _check(
        private_missing_ref_error.repair is None,
        "missing ref in private repo retains generic recovery",
    )
    _check(
        not private_missing_ref_probe.commands,
        "missing ref in private repo performs no repository probe",
    )

    missing_seed = _seed_at(f"https://github.com/{_FIXTURE_OWNER}/fixture-missing-repo.git")
    missing_transport_probe = FakeRepositoryProbe(missing_seed.repository, http_status=404)
    missing_transport_error = _capture_fetch_failure(
        missing_seed,
        root / "missing-repo-transport-failure",
        root / "cache-missing-repo-transport-failure",
        fetch_detail="TLS record corruption while contacting upload proxy",
        probe=missing_transport_probe,
    )
    _check(
        getattr(missing_transport_error, "reason", None) is None,
        "transport corruption against missing repo is not an access failure",
    )
    _check(
        missing_transport_error.repair is None,
        "transport corruption against missing repo retains generic recovery",
    )
    _check(
        not missing_transport_probe.commands,
        "transport corruption against missing repo performs no repository probe",
    )


def _check_pre_fetch_failures(root: Path) -> None:
    seed = _seed_at("https://github.com/solet-public/public-seed.git")
    for name, failure in (
        ("spawn", FileNotFoundError("git executable unavailable")),
        ("timeout", subprocess.TimeoutExpired(("git", "init"), 120)),
    ):
        probe = FakeRepositoryProbe(seed.repository, http_status=200, visibility="public")

        def failing_runner(
            _command: Sequence[str],
            _cwd: Path | None,
            _timeout: int,
            *,
            error: BaseException = failure,
        ) -> RunResult:
            raise error

        try:
            materialize_locked_seed(
                seed,
                root / f"{name}-failure",
                cache_dir=root / f"cache-{name}-failure",
                runner=failing_runner,
                repository_probe_runner=probe,
            )
        except SourceError as exc:
            _check(getattr(exc, "reason", None) is None, f"{name} has no access reason")
            _check(exc.repair is None, f"{name} retains generic recovery")
        else:
            _check(False, f"{name} failure is refused")
        _check(not probe.commands, f"{name} performs no repository probe")


def _check_private_stderr_redaction(root: Path) -> None:
    seed = _seed_at(f"https://github.com/{_FIXTURE_OWNER}/fixture-private-repo.git")
    probe = FakeRepositoryProbe(seed.repository, http_status=200, visibility="private")
    secret_markers = (
        "ghp_DO_NOT_COPY",
        "helper=/Users/operator/Library/Keychains/login.keychain-db",
        "https://user:password@github.com",
    )
    error = _capture_fetch_failure(
        seed,
        root / "redacted",
        root / "cache-redacted",
        fetch_detail=" ".join(secret_markers),
        probe=probe,
        fetch_http_status=401,
    )
    public_text = f"{error} {error.repair}"
    _check(
        not any(marker in public_text for marker in secret_markers),
        "private refusal never copies raw Git stderr",
    )


def _check_structured_transport_status(root: Path) -> None:
    redirected_trace = root / "redirected-curl.trace"
    redirected_trace.write_text(
        "18:00:00 <= Recv header: HTTP/2 301\n"
        "18:00:00 <= Recv header: location: https://github.com/next\n"
        "18:00:01 <= Recv header: HTTP/2 200\n",
        encoding="utf-8",
    )
    _check(
        _final_git_http_status(redirected_trace) == 200,
        "actual fetch classification uses the final structured HTTP response",
    )

    diagnostic_only_trace = root / "diagnostic-only-curl.trace"
    diagnostic_only_trace.write_text(
        "18:00:02 == Info: repository not found after TLS interruption\n",
        encoding="utf-8",
    )
    _check(
        _final_git_http_status(diagnostic_only_trace) is None,
        "unstructured diagnostics cannot manufacture an HTTP status",
    )


def _check_unreadable_seed_refusal(
    seed: SeedLock,
    target: Path,
    cache: Path,
) -> None:
    unreadable_error: SourceError | None = None
    try:
        materialize_locked_seed(
            seed,
            target,
            cache_dir=cache,
            runner=FakeGit(
                seed,
                fetch_result=RunResult(
                    128,
                    "",
                    (
                        "fatal: could not read Username for "
                        "'https://github.com': Device not configured"
                    ),
                    401,
                ),
            ),
            repository_probe_runner=FakeRepositoryProbe(
                seed.repository,
                http_status=404,
            ),
        )
    except SourceError as exc:
        unreadable_error = exc
    _check(unreadable_error is not None, "unreadable seed is refused")
    assert unreadable_error is not None
    _check(
        unreadable_error.error_kind == "source_error",
        "unreadable seed preserves the source_error taxonomy",
    )
    _check(
        all(
            fragment in str(unreadable_error)
            for fragment in (
                "not anonymously visible",
                seed.profile,
                seed.repository,
                seed.release_tag,
            )
        ),
        "unreadable seed refusal names the cause and exact locked seed; "
        f"observed={unreadable_error!r}",
    )
    _check(unreadable_error.repair is not None, "unreadable seed provides repair guidance")
    assert unreadable_error.repair is not None
    _check(
        all(
            fragment in unreadable_error.repair
            for fragment in (
                "--seed <name>",
                "--seed-lock <path>",
                "authorized Git access",
            )
        ),
        "unreadable seed refusal states public-seed and private-seed recovery; "
        f"repair={unreadable_error.repair!r}",
    )
    _check(not target.exists(), "unreadable seed never creates final target")


def _check_tagless_acquisition(root: Path) -> None:
    seed = _tagless_seed()
    target = root / "tagless"
    fake = FakeGit(seed)
    result = materialize_locked_seed(
        seed,
        target,
        cache_dir=root / "cache-tagless",
        runner=fake,
    )
    _check(result == target and target.is_dir(), "tagless seed materialized")
    _check(
        ("git", "fetch", "--no-tags", "origin", seed.commit) in fake.commands,
        "tagless seed fetches its exact locked commit",
    )
    _check(
        not any(
            "refs/tags/" in command or "refs/heads/main" in command for command in fake.commands
        ),
        "tagless seed never relies on an advertised tag or moving head",
    )
    _check_identity_failure(
        seed,
        root / "tagless-mismatched-commit",
        root / "cache-tagless-mismatched-commit",
        FakeGit(seed, commit="e" * 40),
    )
    _check_identity_failure(
        seed,
        root / "tagless-mismatched-tree",
        root / "cache-tagless-mismatched-tree",
        FakeGit(seed, tree="f" * 40),
    )


def main() -> int:
    seed = SeedLock(
        "https://github.com/solet-public/macos-samantha.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-samantha",
    )
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ordinary_parent = root / "Solets"
        ordinary_parent.mkdir(mode=0o755)
        ordinary_parent.chmod(0o755)
        target = ordinary_parent / "bizops"
        fake = FakeGit(seed)
        seam_checked = False

        def same_filesystem_replace(
            source: Path,
            destination: Path,
            _expected_parent: tuple[int, int],
        ) -> None:
            nonlocal seam_checked
            _check(source.parent == destination.parent, "staging is a private target sibling")
            _check(
                source.parent.stat().st_dev == destination.parent.stat().st_dev,
                "replace seam stays on one filesystem",
            )
            seam_checked = True
            os.replace(source, destination)

        result = materialize_locked_seed(
            seed,
            target,
            cache_dir=root / "cache",
            runner=fake,
            replace_runner=same_filesystem_replace,
        )
        _check(result == target and target.is_dir(), "materialized target")
        _check(seam_checked, "cross-filesystem replace seam exercised")
        _check(
            stat.S_IMODE(ordinary_parent.stat().st_mode) == 0o755,
            "existing 0755 target parent accepted unchanged",
        )
        _check(
            any(
                f"refs/tags/{seed.release_tag}:refs/tags/{seed.release_tag}" in command
                for command in fake.commands
            ),
            "fetches exact tag ref",
        )
        _check(
            not any("refs/heads/main" in command for command in fake.commands),
            "never fetches moving main",
        )
        _check(
            ("git", "remote", "add", "origin", seed.repository) in fake.commands,
            "retains locked origin",
        )
        _check(
            ("git", "branch", "--force", "main", seed.commit) in fake.commands, "pins local main"
        )
        _check((target / "PROVENANCE.json").is_file(), "provenance retained")

        _check_unreadable_seed_refusal(
            seed,
            ordinary_parent / "unreadable",
            root / "cache-unreadable",
        )
        _check_repository_classification(ordinary_parent)
        _check_non_access_false_positive(ordinary_parent)
        _check_pre_fetch_failures(ordinary_parent)
        _check_private_stderr_redaction(ordinary_parent)
        _check_structured_transport_status(ordinary_parent)
        _check_tagless_acquisition(ordinary_parent)

        missing_parent = root / "created-parent"
        mismatch_target = missing_parent / "bad-commit"
        _check_identity_failure(
            seed, mismatch_target, root / "cache2", FakeGit(seed, commit="e" * 40)
        )
        _check(
            stat.S_IMODE(missing_parent.stat().st_mode) == 0o700,
            "missing target parent created 0700",
        )
        tree_target = ordinary_parent / "bad-tree"
        _check_identity_failure(seed, tree_target, root / "cache3", FakeGit(seed, tree="f" * 40))

        provenance_target = ordinary_parent / "bad-provenance"
        replace_calls: list[tuple[Path, Path]] = []

        def forbidden_replace(
            source: Path,
            destination: Path,
            _expected_parent: tuple[int, int],
        ) -> None:
            replace_calls.append((source, destination))

        _check_identity_failure(
            seed,
            provenance_target,
            root / "cache-provenance",
            FakeGit(seed, provenance_bundle="samantha-standard"),
            replace_runner=forbidden_replace,
        )
        _check(
            not replace_calls,
            "provenance mismatch never calls the final-target replacement seam",
        )

        hostile_parent = root / "hostile-parent"
        hostile_parent.mkdir(mode=0o700)
        hostile_parent.chmod(0o777)
        try:
            materialize_locked_seed(
                seed,
                hostile_parent / "blocked",
                cache_dir=root / "cache4",
                runner=FakeGit(seed),
            )
        except SourceError:
            _check(True, "group/world-writable target parent rejected")
        else:
            _check(False, "group/world-writable target parent rejected")

        linked_parent = root / "linked-parent"
        linked_parent.symlink_to(ordinary_parent, target_is_directory=True)
        try:
            materialize_locked_seed(
                seed,
                linked_parent / "blocked",
                cache_dir=root / "cache5",
                runner=FakeGit(seed),
            )
        except SourceError:
            _check(True, "symlink target parent rejected")
        else:
            _check(False, "symlink target parent rejected")

        race_parent = root / "race-parent"
        race_parent.mkdir(mode=0o700)
        race_target = race_parent / "blocked"
        try:
            materialize_locked_seed(
                seed,
                race_target,
                cache_dir=root / "cache6",
                runner=FakeGit(seed, race_parent=race_parent),
            )
        except SourceError:
            _check(True, "target-parent identity swap detected before finalization")
        else:
            _check(False, "target-parent identity swap detected before finalization")
        _check(
            not race_target.exists(), "target-parent race never installs at the replacement path"
        )

    print(f"source_acquisition_smoke OK: {_CHECKS} checks passed")
    return 0


def _check_identity_failure(
    seed: SeedLock,
    target: Path,
    cache: Path,
    fake: FakeGit,
    *,
    replace_runner: ReplaceRunner | None = None,
) -> None:
    try:
        materialize_locked_seed(
            seed,
            target,
            cache_dir=cache,
            runner=fake,
            replace_runner=replace_runner,
        )
    except SourceIdentityError:
        _check(True, f"identity mismatch refused for {target.name}")
    else:
        _check(False, f"identity mismatch refused for {target.name}")
    _check(not target.exists(), f"identity mismatch never creates final target {target.name}")
    _check(
        any(
            path.name.startswith(f".{target.name}.solet-acquire-")
            for path in target.parent.iterdir()
        ),
        f"failed private sibling staging retained for {target.name}",
    )


if __name__ == "__main__":
    sys.exit(main())
