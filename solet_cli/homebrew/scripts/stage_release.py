"""Stage a Homebrew manager payload and an independently identified seed lock.

Builds the deterministic manager-payload archive (the public-distribution
``LICENSE`` and ``NOTICE``, ``solet_cli/``, and the birth-spine setup
contracts), computes its checksum, resolves the seed's own git identity at the
given ref, records the manager source identity, writes ``release_metadata.json``
and a draft ``release_manifest.json`` (schema v1 per the `publish_release`
design §7.1 — seed and manager sections only; the remaining sections are
finalised by later stages of that verb, out of this script's job), and then
calls the existing ``render_release_payload.py`` to produce the real
``Formula/solet.rb`` and ``solet_cli/homebrew/seed.lock.json`` -- the Formula
also installs this same draft into the keg as
``share/solet/release_manifest.json``, which is what lets the consumption-side
pairing gate (``solet_manager.release_identity_gate``) read it (iss_18c47206).

Deliberately does not touch git, GitHub, or any credential: it reads a
worktree that has already been checked out (by the workflow, or by hand for
local testing) and writes only under ``--output-root``. Nothing here creates
a tap, cuts a release, or uploads an asset — those remain separate,
explicitly authorized acts outside this script's job.

The payload excludes ``solet_cli/homebrew/seed.lock.json`` deliberately: the
rendered lock's own ``archive_sha256`` field names this payload archive's
checksum, so the archive cannot also contain that field's value without
being self-referential. The Formula template embeds the same rendered lock
fields and writes them inside Homebrew's build sandbox, rather than reading
the tap checkout or the downloaded payload. This keeps the checksum binding
non-circular without crossing the sandbox boundary.

``stage()`` is the importable entry point the `publish_release` verb calls
in-process (sealed repo as ``seed_checkout``, minting checkout at
``source_ref`` as ``manager_checkout``); ``main()`` is a thin CLI wrapper
over it. ``stage()`` re-validates its own inputs rather than trusting a
caller, since it is a public function, not just an argparse target.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_S = 30

_PAYLOAD_SOURCE_PATHS = ("LICENSE", "NOTICE", "solet_setup_contracts", "solet_cli")
# Shipped inside the Manager payload as an acquisition aid only: the Manager
# proves the transition bundle from the exact candidate commit, never from
# its own packaged copy (existing-install design section 2.3).
_FORMULA_ONLY_CONTRACT_PATHS = (
    "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json",
    "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.schema.json",
)
# The closed existing-install transition bundle (design section 2.3): digested
# with the same filename/NUL/bytes/NUL discipline as the create bundle, over a
# DISTINCT file set that never includes macos_setup_flow.json.  Its digest is
# what seed-lock v3 binds as existing_install_contract.bundle_digest.
_TRANSITION_BUNDLE_PATHS = (
    "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json",
    "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_adapter_envelope.schema.json",
)
_DIGESTED_CONTRACT_PATHS = (
    "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_flow.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_answers.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_journal.schema.json",
    "plugins/github_midwife_plugin/knowledge_base/setup_adapter_envelope.schema.json",
)
_CONTRACT_PATHS = (
    *_DIGESTED_CONTRACT_PATHS,
    "plugins/github_midwife_plugin/knowledge_base/permissions_manifest.json",
)
_EXCLUDED_FROM_PAYLOAD = "solet_cli/homebrew/seed.lock.json"
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MOVING_RELEASE_NAMES = frozenset({"head", "main", "master", "latest"})
_PROFILE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_SEED_REPOSITORY = re.compile(
    r"^https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
    r"/[A-Za-z0-9_.-]{1,100}\.git$"
)
# Release-label discriminator ("r<NN>"): folded into the asset filename so two
# releases of the same manager version never share a name (closes
# iss_ba7f7103 together with the version-agreement check below).
_RELEASE_LABEL = re.compile(r"^r[0-9]+$")
_MANAGER_MODELS_RELATIVE_PATH = Path("solet_cli") / "src" / "solet_manager" / "models.py"
_RELEASE_MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class _GitIdentity:
    commit: str
    tree_hash: str


@dataclass(frozen=True)
class StageResult:
    """Everything a caller (the CLI, or `publish_release` in-process) needs
    back from a completed stage."""

    output_root: Path
    payload_path: Path
    payload_sha256: str
    metadata_path: Path
    manifest_path: Path
    manifest: dict[str, object]
    formula_path: Path
    lock_path: Path
    install_mode: str
    manager_commit: str
    contract_digest: str
    transition_digest: str


def main() -> int:
    args = _parser().parse_args()
    _require_tag(args.release_tag)
    _require_seed_repository(args.seed_repository)
    _require_profile(args.seed_profile)
    seed_checkout = args.seed_checkout.resolve()
    output_root = _output_root(args.output_root, seed_checkout)

    if args.lock_only:
        return _emit_lock_only(seed_checkout, output_root, args)

    if args.manager_checkout is None:
        raise SystemExit("--manager-checkout is required unless --lock-only is used")
    manager_checkout = args.manager_checkout.resolve()
    output_root = _output_root(args.output_root, seed_checkout, manager_checkout)

    result = stage(
        seed_checkout=seed_checkout,
        release_tag=args.release_tag,
        seed_repository=args.seed_repository,
        seed_profile=args.seed_profile,
        seed_channel_id=args.seed_channel_id,
        manager_checkout=manager_checkout,
        manager_ref=args.manager_ref,
        manager_source_repository=args.manager_source_repository,
        manager_repository=args.manager_repository,
        manager_release_tag=args.manager_release_tag,
        formula_revision=args.formula_revision,
        dev_mode=args.dev_mode,
        output_root=output_root,
        release_label=args.release_label,
        allow_manager_seed_skew=args.allow_manager_seed_skew,
    )

    print(f"payload archive:  {result.payload_path} ({result.payload_sha256})")
    print(f"release metadata: {result.metadata_path}")
    print(f"release manifest: {result.manifest_path}")
    print(f"rendered formula: {result.formula_path}")
    print(f"rendered lock:    {result.lock_path}")
    print(f"install mode:     {result.install_mode} ({result.manager_commit})")
    print(f"setup contracts:  {result.contract_digest} (manager payload == seed artifact)")
    print(f"transition bundle: {result.transition_digest} (seed artifact existing-install)")
    print("Nothing was pushed, tagged, released, or uploaded.")
    return 0


def stage(
    *,
    seed_checkout: Path,
    release_tag: str,
    seed_repository: str,
    seed_profile: str,
    seed_channel_id: str,
    manager_checkout: Path,
    manager_ref: str | None,
    manager_source_repository: str | None,
    manager_repository: str | None,
    manager_release_tag: str | None,
    formula_revision: int,
    dev_mode: bool,
    output_root: Path,
    release_label: str | None = None,
    allow_manager_seed_skew: str | None = None,
) -> StageResult:
    """Stage a manager payload + seed lock pair (design §5.4 stage 5).

    Called by the CLI (thin wrapper, see `main()`) and, in-process, by the
    `publish_release` verb with the sealed repo as `seed_checkout` and the
    minting checkout at `source_ref` as `manager_checkout`. Re-validates every
    input itself: a public function must not trust its caller's argparse
    already having done so.
    """
    _require_tag(release_tag)
    _require_seed_repository(seed_repository)
    _require_profile(seed_profile)
    manager_repository_value = None if dev_mode else _require_manager_repository(manager_repository)
    manager_release_tag_value = (
        None if dev_mode else _require_manager_release_tag(manager_release_tag)
    )
    manager_ref_value = _require_manager_ref(manager_ref)
    manager_source_repository_value = _require_manager_source_repository(manager_source_repository)
    if release_label is not None:
        _require_release_label(release_label)
    _require_paths_present(manager_checkout)
    seed_identity = _resolve_identity(seed_checkout, release_tag, "seed release tag")
    seed_provenance = _seed_provenance_at_commit(seed_checkout, seed_identity.commit)
    manager_identity = _resolve_identity(manager_checkout, manager_ref_value, "manager ref")
    _require_manager_checkout_pinned(manager_checkout, manager_identity, manager_ref_value)
    _require_manager_checkout_clean(manager_checkout)
    version = _read_manager_version(manager_checkout)
    _require_manager_version_agreement(manager_checkout, version)
    contract_digest = _require_contract_pair(
        manager_checkout=manager_checkout,
        manager_commit=manager_identity.commit,
        seed_checkout=seed_checkout,
        seed_commit=seed_identity.commit,
    )
    transition_digest = _require_transition_bundle(seed_checkout, seed_identity.commit)
    _require_same_source_revision(
        manager_source_commit=manager_identity.commit,
        seed_provenance_source_commit=seed_provenance["source_commit"],
        allow_manager_seed_skew=allow_manager_seed_skew,
    )
    asset_name = (
        f"solet-{version}.tar.gz"
        if release_label is None
        else f"solet-{version}-{release_label}.tar.gz"
    )

    payload_dir = output_root / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_path = payload_dir / asset_name
    payload_sha256, file_digests = _build_payload_archive(
        manager_checkout, manager_identity.commit, payload_path
    )

    install_mode = "dev" if dev_mode else "release"
    manager_url = (
        payload_path.as_uri()
        if dev_mode
        else (
            f"https://github.com/{_owner_repo(_require_value(manager_repository_value))}/releases/download/"
            f"{_require_value(manager_release_tag_value)}/{asset_name}"
        )
    )
    metadata = {
        "formula_revision": formula_revision,
        "install_mode": install_mode,
        "manager_url": manager_url,
        "manager_source_repository": manager_source_repository_value,
        "manager_source_ref": manager_ref_value,
        "manager_source_commit": manager_identity.commit,
        "manager_source_tree_hash": manager_identity.tree_hash,
        "release_archive_sha256": payload_sha256,
        "seed_repository": seed_repository,
        "seed_release_tag": release_tag,
        "seed_commit": seed_identity.commit,
        "seed_tree_hash": seed_identity.tree_hash,
        "seed_profile": seed_profile,
        "seed_channel_id": seed_channel_id,
        "seed_provenance": seed_provenance,
        "existing_install_contract": {
            "flow_id": "existing-install",
            "flow_schema_version": 1,
            "bundle_digest": transition_digest,
        },
        "allowed_repository_migrations": [],
    }
    metadata_path = output_root / "release_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    manifest = _build_release_manifest_draft(
        release_label=release_label,
        manager_release_tag=manager_release_tag_value,
        seed_repository=seed_repository,
        seed_release_tag=release_tag,
        seed_identity=seed_identity,
        seed_profile=seed_profile,
        seed_channel_id=seed_channel_id,
        seed_provenance=seed_provenance,
        manager_source_repository=manager_source_repository_value,
        manager_identity=manager_identity,
        version=version,
        asset_name=asset_name,
        manager_url=manager_url,
        payload_sha256=payload_sha256,
        file_digests=file_digests,
        contract_digest=contract_digest,
        transition_digest=transition_digest,
        allow_manager_seed_skew=allow_manager_seed_skew,
    )
    manifest_path = output_root / "release_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    _render(metadata_path, manifest_path, output_root)

    return StageResult(
        output_root=output_root,
        payload_path=payload_path,
        payload_sha256=payload_sha256,
        metadata_path=metadata_path,
        manifest_path=manifest_path,
        manifest=manifest,
        formula_path=output_root / "Formula" / "solet.rb",
        lock_path=output_root / "solet_cli" / "homebrew" / "seed.lock.json",
        install_mode=install_mode,
        manager_commit=manager_identity.commit,
        contract_digest=contract_digest,
        transition_digest=transition_digest,
    )


def _emit_lock_only(checkout: Path, output_root: Path, args: argparse.Namespace) -> int:
    """Bare seed.lock.json for a seed that ships no Homebrew payload of its
    own — no manager archive, no Formula. `archive_sha256` is OMITTED, not
    null: absence means "this seed has no separate payload archive," never
    "unknown" or "unverified" — the commit/tree-hash check below is what
    actually protects the seed's content, independent of this field.
    """
    identity = _resolve_identity(checkout, args.release_tag, "seed release tag")
    lock = {
        "schema_version": 1,
        "repository": args.seed_repository,
        "release_tag": args.release_tag,
        "commit": identity.commit,
        "tree_hash": identity.tree_hash,
        "profile": args.seed_profile,
    }
    output_path = output_root / "seed.lock.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(f"seed lock: {output_path}")
    print("Nothing was pushed, tagged, released, or uploaded.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed-checkout",
        type=Path,
        required=True,
        help="path to a working tree already checked out at --release-tag",
    )
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--seed-repository", required=True, help="HTTPS GitHub .git URL")
    parser.add_argument("--seed-profile", required=True)
    parser.add_argument("--seed-channel-id", default="stable")
    parser.add_argument(
        "--manager-repository",
        help="public GitHub HTTPS .git URL that will host the manager asset",
    )
    parser.add_argument(
        "--manager-release-tag",
        help="immutable manager-specific release tag that will host the asset",
    )
    parser.add_argument(
        "--manager-checkout",
        type=Path,
        help="path to the manager source tree whose bytes will be archived",
    )
    parser.add_argument(
        "--manager-ref",
        help="immutable manager source commit; --manager-checkout must be pinned to it",
    )
    parser.add_argument(
        "--manager-source-repository",
        help="HTTPS GitHub .git URL identifying the manager source tree",
    )
    parser.add_argument("--formula-revision", type=int, default=0)
    parser.add_argument(
        "--dev-mode",
        action="store_true",
        help=(
            "render a Formula overlay whose URL is the staged local payload and whose "
            "installed receipt is explicitly dev-mode; never use this for published-pair acceptance"
        ),
    )
    parser.add_argument(
        "--lock-only",
        action="store_true",
        help=(
            "emit a bare seed.lock.json only — no payload archive, no Formula. "
            "For an additional seed that ships no Homebrew asset of its own; "
            "the checkout need not contain solet_cli/ or the manager contracts."
        ),
    )
    parser.add_argument(
        "--output-root",
        required=True,
        help="non-empty path outside the seed and manager source trees",
    )
    parser.add_argument(
        "--release-label",
        help=(
            "release discriminator 'r<NN>' folded into the payload asset filename "
            "(solet-<version>-<label>.tar.gz) so two releases never share a name; "
            "omit only for a legacy caller that still expects the undiscriminated name"
        ),
    )
    parser.add_argument(
        "--allow-manager-seed-skew",
        metavar="REASON",
        help=(
            "explicit override recorded on the release manifest: stage anyway when "
            "the manager source commit and the seed's PROVENANCE.source_commit differ"
        ),
    )
    return parser


def _require_tag(tag: str) -> None:
    if _TAG.fullmatch(tag) is None:
        raise SystemExit(f"--release-tag has an invalid immutable identity shape: {tag!r}")


def _require_seed_repository(repository: str) -> None:
    if _SEED_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit(
            f"--seed-repository must be an explicit GitHub HTTPS .git URL: {repository!r}"
        )


def _require_manager_repository(repository: str | None) -> str:
    if repository is None or _SEED_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit("--manager-repository must be an explicit GitHub HTTPS .git URL")
    return repository


def _require_manager_source_repository(repository: str | None) -> str:
    if repository is None or _SEED_REPOSITORY.fullmatch(repository) is None:
        raise SystemExit("--manager-source-repository must be an explicit GitHub HTTPS .git URL")
    return repository


def _require_release_label(label: str) -> None:
    if _RELEASE_LABEL.fullmatch(label) is None:
        raise SystemExit(f"--release-label has an invalid shape (expected 'r<NN>'): {label!r}")


def _read_manager_declared_version(checkout: Path) -> str:
    """Parse `MANAGER_VERSION` out of `solet_manager/models.py`'s source via
    `ast`, never by importing the package — this script stays free of any
    runtime dependency on `solet-manager`/`ananta`."""
    models_path = checkout / _MANAGER_MODELS_RELATIVE_PATH
    try:
        source = models_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read {models_path} to verify MANAGER_VERSION: {exc}") from exc
    tree = ast.parse(source, filename=str(models_path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "MANAGER_VERSION"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise SystemExit(f"{models_path} has no module-level MANAGER_VERSION string constant")


def _require_manager_version_agreement(checkout: Path, pyproject_version: str) -> None:
    """Refuse a manager checkout where the declared runtime version
    (`solet_manager.models.MANAGER_VERSION`) and the packaged version
    (`solet_cli/pyproject.toml`'s `[project].version`) have drifted apart —
    closes iss_ba7f7103 at the point of use."""
    declared = _read_manager_declared_version(checkout)
    if declared != pyproject_version:
        raise SystemExit(
            "manager version disagreement: solet_manager.models.MANAGER_VERSION "
            f"{declared!r} != solet_cli/pyproject.toml [project].version {pyproject_version!r} "
            "(iss_ba7f7103 — the two must move together)"
        )


def _require_same_source_revision(
    *,
    manager_source_commit: str,
    seed_provenance_source_commit: object,
    allow_manager_seed_skew: str | None,
) -> None:
    """Refuse to pair a manager payload with a seed minted from a different
    source commit — closes iss_da99a951. The only way past it is an explicit,
    recorded `--allow-manager-seed-skew <reason>`."""
    if manager_source_commit == seed_provenance_source_commit:
        return
    if allow_manager_seed_skew:
        return
    raise SystemExit(
        "manager/seed revision skew: manager source commit "
        f"{manager_source_commit!r} != seed PROVENANCE.source_commit "
        f"{seed_provenance_source_commit!r}; pass --allow-manager-seed-skew <reason> "
        "to record an explicit override"
    )


def _require_manager_release_tag(tag: str | None) -> str:
    if tag is None or _TAG.fullmatch(tag) is None or tag.casefold() in _MOVING_RELEASE_NAMES:
        raise SystemExit("--manager-release-tag has an invalid immutable identity shape")
    return tag


def _require_manager_ref(ref: str | None) -> str:
    if ref is None or _COMMIT.fullmatch(ref) is None:
        raise SystemExit("--manager-ref has an invalid immutable identity shape")
    return ref


def _require_profile(profile: str) -> None:
    if _PROFILE.fullmatch(profile) is None:
        raise SystemExit(f"--seed-profile has an invalid identity shape: {profile!r}")


def _seed_provenance_at_commit(checkout: Path, commit: str) -> dict[str, object]:
    """Read descriptor facts only from the pinned seed commit, never its worktree."""
    content = _git_blob(checkout, commit, "PROVENANCE.json", "seed artifact")
    try:
        raw: object = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"seed artifact commit {commit} has malformed PROVENANCE.json: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise SystemExit("seed artifact PROVENANCE.json must be an object")
    bundle = raw.get("bundle")
    required = frozenset(
        {
            "schema_version",
            "seed_id",
            "origin_id",
            "source_commit",
            "manifest_sha256",
            "bundle",
            "source_date",
            "lineage",
            "ancestry",
            "signature",
        }
    )
    if frozenset(raw) != required or not isinstance(bundle, dict):
        raise SystemExit("seed artifact PROVENANCE.json does not match the closed v1 shape")
    try:
        return {
            "schema_version": 1,
            "provenance_sha256": hashlib.sha256(content).hexdigest(),
            "seed_id": raw["seed_id"],
            "origin_id": raw["origin_id"],
            "manifest_sha256": raw["manifest_sha256"],
            "bundle_name": bundle["name"],
            "platform": bundle["platform"],
            "source_commit": raw["source_commit"],
            "source_date": raw["source_date"],
        }
    except KeyError as exc:
        raise SystemExit("seed artifact PROVENANCE.json lacks a required descriptor fact") from exc


def _owner_repo(seed_repository: str) -> str:
    return seed_repository.removeprefix("https://github.com/").removesuffix(".git")


def _require_value(value: str | None) -> str:
    """Narrow an already validated release-only CLI value for the renderer."""
    if value is None:
        raise AssertionError("release-only argument was not validated")
    return value


def _require_paths_present(checkout: Path) -> None:
    missing = [
        path
        for path in (*_PAYLOAD_SOURCE_PATHS, *_CONTRACT_PATHS)
        if not (checkout / path).exists()
    ]
    if missing:
        raise SystemExit("seed checkout is missing required payload paths: " + ", ".join(missing))


def _resolve_identity(checkout: Path, ref: str, label: str) -> _GitIdentity:
    commit = _git(checkout, "rev-list", "-n", "1", ref).strip()
    if not commit:
        raise SystemExit(f"{label} {ref!r} did not resolve to a commit")
    tree_hash = _git(checkout, "rev-parse", f"{commit}^{{tree}}").strip()
    return _GitIdentity(commit=commit, tree_hash=tree_hash)


def _require_manager_checkout_pinned(
    checkout: Path, identity: _GitIdentity, manager_ref: str
) -> None:
    checkout_identity = _resolve_identity(checkout, "HEAD", "manager checkout HEAD")
    if checkout_identity.commit != identity.commit:
        raise SystemExit(
            "--manager-checkout must be a Git-Controller-provided worktree pinned "
            f"at --manager-ref {manager_ref}; HEAD is {checkout_identity.commit}"
        )


def _require_manager_checkout_clean(checkout: Path) -> None:
    checkout_status = _git(checkout, "status", "--porcelain").strip()
    if checkout_status:
        raise SystemExit(
            f"--manager-checkout must be clean before staging; found changes: {checkout_status}"
        )


def _require_contract_pair(
    *,
    manager_checkout: Path,
    manager_commit: str,
    seed_checkout: Path,
    seed_commit: str,
) -> str:
    """Refuse a release whose manager and locked seed disagree at birth time."""

    manager_digest = _contract_digest_at_commit(
        manager_checkout,
        manager_commit,
        "manager payload",
    )
    seed_digest = _contract_digest_at_commit(
        seed_checkout,
        seed_commit,
        "seed artifact",
    )
    if manager_digest != seed_digest:
        raise SystemExit(
            "manager/seed setup contract mismatch; refusing to stage an "
            "unbornable release: "
            f"manager_commit={manager_commit}, manager_digest={manager_digest}, "
            f"seed_commit={seed_commit}, seed_digest={seed_digest}"
        )
    return manager_digest


def _require_transition_bundle(seed_checkout: Path, seed_commit: str) -> str:
    """Digest the committed transition bundle and refuse a malformed or create-shaped one."""
    files = {
        Path(relative_path).name: _git_blob(seed_checkout, seed_commit, relative_path, "seed artifact")
        for relative_path in _TRANSITION_BUNDLE_PATHS
    }
    try:
        flow: object = json.loads(files["existing_install_flow.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"seed artifact commit {seed_commit} has a malformed existing_install_flow.json: {exc}") from exc
    if not isinstance(flow, dict) or flow.get("flow_id") != "existing-install" or flow.get("schema_version") != 1:
        raise SystemExit("seed artifact existing_install_flow.json does not declare the existing-install flow v1")
    digest = _digest_files(files)
    if digest == _contract_digest_at_commit(seed_checkout, seed_commit, "seed artifact"):
        raise SystemExit("transition bundle digest must differ from the create bundle digest")
    return digest


def _digest_files(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _contract_digest_at_commit(
    checkout: Path,
    commit: str,
    label: str,
) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(_DIGESTED_CONTRACT_PATHS, key=lambda value: Path(value).name):
        name = Path(relative_path).name
        content = _git_blob(checkout, commit, relative_path, label)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _git_blob(checkout: Path, commit: str, relative_path: str, label: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{commit}:{relative_path}"],
        cwd=checkout,
        check=False,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise SystemExit(
            f"{label} commit {commit} does not provide required setup contract "
            f"{relative_path}: {detail}"
        )
    return result.stdout


def _output_root(raw: str, *source_trees: Path) -> Path:
    if not raw.strip():
        raise SystemExit("--output-root must be a non-empty path")
    output_root = Path(raw).resolve()
    for source_tree in source_trees:
        try:
            output_root.relative_to(source_tree)
        except ValueError:
            continue
        raise SystemExit(
            "--output-root must be outside every source tree; refusing to write into "
            f"source tree {source_tree}"
        )
    return output_root


def _read_manager_version(checkout: Path) -> str:
    pyproject = checkout / "solet_cli" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"{pyproject} has no [project].version")
    return version


def _build_payload_archive(
    checkout: Path, commit: str, destination: Path
) -> tuple[str, dict[str, str]]:
    """Deterministic tar.gz of the payload paths at the resolved commit, plus
    a per-file sha256 digest of every member (release manifest §7.1
    `manager.file_digests`).

    Uses ``git archive`` (content addressed by the commit, no local-clock
    mtimes) piped through gzip with no embedded timestamp, so re-running
    against the same commit reproduces byte-identical output regardless of
    the checkout's own working-tree state.
    """
    tar_bytes = subprocess.run(
        [
            "git",
            "archive",
            "--format=tar",
            commit,
            *_PAYLOAD_SOURCE_PATHS,
            *_CONTRACT_PATHS,
            *_FORMULA_ONLY_CONTRACT_PATHS,
        ],
        cwd=checkout,
        check=True,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
    ).stdout
    _refuse_excluded_member(tar_bytes)
    file_digests = _payload_file_digests(tar_bytes)
    compressed = _gzip_no_timestamp(tar_bytes)
    destination.write_bytes(compressed)
    return hashlib.sha256(compressed).hexdigest(), file_digests


def _payload_file_digests(tar_bytes: bytes) -> dict[str, str]:
    digests: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SystemExit(
                    f"payload archive member {member.name} could not be read for digesting"
                )
            digests[member.name] = f"sha256:{hashlib.sha256(extracted.read()).hexdigest()}"
    return digests


def _build_release_manifest_draft(
    *,
    release_label: str | None,
    manager_release_tag: str | None,
    seed_repository: str,
    seed_release_tag: str,
    seed_identity: _GitIdentity,
    seed_profile: str,
    seed_channel_id: str,
    seed_provenance: dict[str, object],
    manager_source_repository: str,
    manager_identity: _GitIdentity,
    version: str,
    asset_name: str,
    manager_url: str,
    payload_sha256: str,
    file_digests: dict[str, str],
    contract_digest: str,
    transition_digest: str,
    allow_manager_seed_skew: str | None,
) -> dict[str, object]:
    """Stage-5 partial draft of `release_manifest.json` (schema v1, design
    §7.1): the `seed` and `manager` sections this stage can populate. Every
    section a LATER stage of `publish_release` contributes — `components`,
    `bundle_verdict`, `guest_validation`, `tap`, `surface_digests`,
    `produced_by`, `factory_signature` — is null until that stage finalises
    it (§5.4 stage 13); this script never runs those stages."""
    return {
        "schema_version": _RELEASE_MANIFEST_SCHEMA_VERSION,
        "release_label": release_label,
        "manager_release_tag": manager_release_tag,
        "seed": {
            "repository": seed_repository,
            "release_tag": seed_release_tag,
            "commit": seed_identity.commit,
            "tree_hash": seed_identity.tree_hash,
            "seed_id": seed_provenance["seed_id"],
            "manifest_sha256": seed_provenance["manifest_sha256"],
            "source_commit": seed_provenance["source_commit"],
            "profile": seed_profile,
            "channel_id": seed_channel_id,
            "provenance": seed_provenance,
        },
        "components": None,
        "bundle_verdict": None,
        "guest_validation": None,
        "manager": {
            "source_repository": manager_source_repository,
            "source_commit": manager_identity.commit,
            "source_tree_hash": manager_identity.tree_hash,
            "version": version,
            "payload_asset": {
                "name": asset_name,
                "url": manager_url,
                "sha256": f"sha256:{payload_sha256}",
            },
            "file_digests": file_digests,
            "contract_digest": contract_digest,
            "transition_bundle_digest": transition_digest,
            "allow_manager_seed_skew": allow_manager_seed_skew,
        },
        "tap": None,
        "surface_digests": None,
        "produced_by": None,
        "factory_signature": None,
    }


def _refuse_excluded_member(tar_bytes: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        names = archive.getnames()
    if _EXCLUDED_FROM_PAYLOAD in names:
        raise SystemExit(
            f"{_EXCLUDED_FROM_PAYLOAD} must not be present in the seed checkout's "
            "solet_cli/ before archiving — it is rendered separately and ships "
            "in the tap, never inside the payload archive"
        )


def _gzip_no_timestamp(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(data)
    return buffer.getvalue()


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
    )
    return result.stdout


def _render(metadata_path: Path, manifest_path: Path, output_root: Path) -> None:
    renderer = Path(__file__).resolve().parent / "render_release_payload.py"
    subprocess.run(
        [
            "python3",
            str(renderer),
            "--metadata",
            str(metadata_path),
            "--manifest",
            str(manifest_path),
            "--output-root",
            str(output_root),
        ],
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
