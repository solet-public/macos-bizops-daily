# Solet Homebrew release payload

This directory is the source-controlled, release-ready payload for the external
`solet-public/tap` repository. It is deliberately not an installable formula
until a release lane supplies real immutable release identities.

Run the included release-payload renderer with a reviewed metadata JSON
document. It emits an ordinary Homebrew formula and the immutable seed lock that
must be included in the same uploaded manager source asset. One uploaded sealed
archive supplies both checksums, so the metadata has one checksum field and the
two identities cannot drift. The renderer rejects moving refs, invalid hashes,
unknown keys, and unresolved template markers. Do not hand-edit its output.

The formula installs only the manager, its flow contracts, and its seed lock in
the formula-owned virtualenv. It has no `post_install` action. Instance creation
remains the separate, reviewed `solet create <name>` transaction. Installation
and setup are deliberately two commands:

```console
HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13 solet-public/tap/solet
solet create bizops
```

Homebrew prints `Next: run solet create` as a conventional caveat; it does not
launch the wizard or mutate instance/user state during `brew install`.

### The Manager uses the `python@3.13` already on the Mac

The Formula does not declare `depends_on "python@3.13"` (from r61). A source
formula's dependency is satisfied only by the latest version, so the old
declaration upgraded a working `python@3.13` on every install and upgrade. That
replaced the ad-hoc-signed interpreter every venv linking Homebrew's framework
resolves to, and a Keychain ACL pins that binary's exact code-directory hash:
any solet whose `.venv` linked it was refused on every credential read
(`-25293`) at its next restart. Instead, `install` builds the Manager venv on
the first of these that reports Python 3.13, in this order, and never installs
or upgrades Python itself:

1. `/opt/homebrew/opt/python@3.13/bin/python3.13`
2. `/usr/local/bin/python3.13`
3. `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13`

- **Install command:** `HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13
  solet-public/tap/solet`. Naming `python@3.13` installs it when it is missing
  and marks it installed on request; with the variable set, an existing
  `python@3.13` is left at its version. When one is already installed Homebrew
  prints `Error: python@3.13 <version> is already installed` or a `Warning:`
  that it is already installed and up to date, and still exits 0 and installs
  the Manager. That line is expected.
- **An older Python 3.13 is fine:** Homebrew's `Virtualenv#pip_install` runs
  the base interpreter's pip with `--uploaded-prior-to`, which pip older than
  26.1 rejects. The Formula pins a `pip` wheel resource, installs it into the
  venv by running pip from its own wheel under the venv's interpreter, and then
  rebinds the installer (`virtualenv_create(libexec, venv_python)`) so every
  later install uses that pip. `ci/pip_bootstrap_proof.py --old-pip-wheel
  <pip-25.3 wheel>` proves both sides on a host, without Homebrew.
- **If the install stops with `No Python 3.13 found`:** none of the three paths
  answered. Run the install command above, which installs `python@3.13` first.
- **Keep it from being autoremoved:** an install made before r61 recorded
  `python@3.13` as `installed_on_request=false`, kept only by the old
  dependency. Without that dependency, `brew autoremove` (run by `brew
  upgrade`'s periodic cleanup) would delete the interpreter every solet venv
  links. Every upgrade therefore runs
  `brew tab --installed-on-request python@3.13 && brew upgrade solet`; the tab
  changes only the receipt flag and installs and upgrades nothing.
  `solet-manager create` and `update` plan the same step, and `solet doctor
  <name>` reports `doctor::python_installed_on_request_v1` with the exact
  command until it is set.
- **Your own upgrades still move it:** a `brew upgrade` you run yourself can
  still upgrade `python@3.13`. `brew pin python@3.13` prevents that, and no
  longer blocks installing or upgrading the Manager.
- **Recovery:** in a logged-in GUI session (not SSH), read each Keychain item
  the affected solet owns once under its current interpreter and answer
  **Always Allow**; plain Allow is asked again on every spawn. After
  `solet-manager import <name> --target <clone>`, `solet doctor <name>` reports
  `doctor::python_interpreter_drift_v1` for a venv created under one
  `python@3.13` version that now resolves to another.
- **Limit of that check:** it compares the version the venv recorded with the
  Cellar version it resolves to. It does not see a same-version revision bump
  and it cannot tell that the items were re-authorized afterwards.

The public distribution route is the upstream `solet-public/homebrew-tap`
repository (Homebrew shorthand `solet-public/tap`). That route does not require
acceptance into `homebrew/core`. Before publication, acceptance uses a
disposable local/private tap on a fresh CI runner. The later release lane tests
the actual published upstream tap; the local fixture is not publication proof.

The focused `solet_cli/homebrew/tests/release_payload_smoke.py` check is
host-dependent by design:
it fails rather than skips unless `brew` is available and `python@3.13`, the
first interpreter the Formula looks for, is installed locally. Its no-index regression mirrors
Homebrew `virtualenv_create` exactly with `python -m venv
--system-site-packages --without-pip`, then proves the system-site build backend
can install the manager with `PIP_NO_INDEX=1`.

The external release lane must run the included acceptance harness on a fresh,
disposable GitHub Actions runner. Homebrew 6's `brew test-bot` supports GitHub
Actions only, so the harness fails before mutation unless `CI=true`,
`GITHUB_ACTIONS=true`, and `SOLET_ACCEPT_LIFECYCLE_MUTATION=1` are present. It
requires every
release-dependent input explicitly: previous/current Formula references,
previous/current manager versions and seed-lock SHA-256 values, the current
bottle, tap name, formula-scoped Brewfile, create-config template, and a new or
empty fixture root. The Formula references are reviewed source artifacts, not
arguments to Homebrew commands. The harness creates the disposable tap with
`brew tap-new`, stages each artifact as `Formula/solet.rb` with byte-equality
verification, and runs source Formula operations through the fully qualified
name. It then records only Formula-scoped trust in the isolated XDG Homebrew
trust store and verifies that whole-tap trust was not granted before style or
audit. For example:

```console
CI=true GITHUB_ACTIONS=true SOLET_ACCEPT_LIFECYCLE_MUTATION=1 \
  ./ci/acceptance.sh \
  --tap solet-public/tap \
  --formula solet-public/tap/solet \
  --previous-formula /ci/previous/Formula/solet.rb \
  --current-formula /ci/current/Formula/solet.rb \
  --current-bottle /ci/bottles/solet--0.2.0.arm64.bottle.tar.gz \
  --previous-version 0.1.0 \
  --current-version 0.2.0 \
  --previous-lock-sha256 <64-lowercase-hex> \
  --current-lock-sha256 <64-lowercase-hex> \
  --brewfile ./ci/Brewfile.enterprise.example \
  --create-config-template ./ci/lifecycle_config.toml.template \
  --fixture-root "$RUNNER_TEMP/solet-lifecycle"
```

The harness executes Homebrew style/audit, a clean source install preceded by
`HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13` (the Formula declares
no Python dependency, so a fresh runner must supply one), a bottle install,
Formula `test do`, an explicit previous-to-current Formula/manager/seed-lock
upgrade in that tap, a deterministic simulated Python 3.13 dependency
replacement, uninstall/reinstall preservation, registry and manual-target
`instance_unmanaged` checks, no-keg-path scans, formula-scoped `brew bundle`,
and BrewTestBot. The Python simulation runs `brew reinstall python@3.13`, then
repairs the Solet Formula by fully qualified name, compares preserved state,
executes the manager, and verifies status/doctor. It does not claim that the
Python version changed. A real previous-to-current Python version upgrade
remains a separate release-dependent cold-runner proof.

`brew tap-new` initializes and commits the tap skeleton. The harness verifies
that committed `HEAD` before the final test-bot phase and runs
`brew test-bot --only-formulae --tap ... --testing-formulae ...` from that tap
repository. The explicit fully qualified `--testing-formulae` selection makes
the byte-verified working-tree Formula detectable without claiming a committed
Formula diff. Homebrew test-bot itself temporarily trusts its tested tap inside
its GitHub Actions-local HOME; the harness never issues whole-tap trust and the
pre-test style/audit/install path remains Formula-scoped.

Every lifecycle no-keg scan resolves both the fully qualified Solet Formula
Cellar and the `python@3.13` Cellar. It scans path names, file bytes,
and symlink targets under only the preserved instance target, manager/registry,
user launcher, and LaunchAgent roots. Unrelated Homebrew caches and logs under
the isolated HOME remain outside preservation evidence. The Brewfile phase
first uninstalls the current Formula so `brew bundle install` must perform an
installation, then verifies the manager version, installed seed-lock digest,
Formula test, preserved instance health, and the complete Solet/Python scan.

The harness accepts supplied previous/current Formula files but does not prove
which immutable tap revisions produced them. The release lane must separately
retain tap-ref-to-Formula provenance evidence; no such binding is accepted,
unused, or claimed here. The previous/current release artifacts, uploaded
asset, and bottle do not exist yet, so the current state is only
`source-present` and `fixture-verified`. It is not published or
`cold-host-proven`, and the mutation-enabled harness was not executed in this
source-amendment lane. The later live release lane must still prove the actual
release asset, bottle, supported architectures, tap publication, published-tap
lifecycle, real Python version transition, and cold-machine install/create/
resume/doctor flow. The disposable runner retains its isolated fixture as
evidence.

Two phases follow BrewTestBot and are the only place the existing-install
path is measured across a real process boundary and a real host (Step 7 of the
existing-install design; `solet_cli/tests/homebrew_*_smoke.py` cover the same
sequence in-process against a fake keg). `existing-install-import-update`
installs the previous Formula, builds a real-style clone with
`solet_cli/homebrew/ci/real_style_fixture.py` — `git clone` of the repository
the installed seed lock (the formula writes it under the keg's libexec share
directory) names at its `commit` (tree hash verified), then the seed's own genesis writers and the `solet create` shell
shape, so the clone carries a newborn's tracked and untracked local state —
and drives it with the canonical Manager name: `solet-manager inspect`,
`import --dry-run`, `import --yes`, `doctor`, then the Formula upgrade to the
current release, `update --dry-run`/`--yes` through the source stage and again
through the runtime stage to `promoted`, `doctor`, `brew reinstall`, `doctor`,
and `reconcile --dry-run` (which must answer `no_active_update`, proving the
pointer was released). `cold-host-degraded` then uninstalls `python@3.13`
(`doctor` reports `host_python_313 missing`; `update --dry-run` refuses
`host_requirement_missing`), restores it, and boots the instance's LaunchAgent
out (`doctor` reports the service rows `unknown service_offline`; a zero-delta
`update --dry-run` is `already_current`, and one with a newer release is
refused before the single-colour transition). `<reviewed>` placeholders are
resolved from the preceding `--dry-run` JSON exactly as `create` is. The
fixture builder imports `solet_cli/tests/_step7_support.py` by path, so the
harness checkout must carry `solet_cli/tests/`; the clone's bytes come from the
seed archive, never from that checkout.

The included enterprise Brewfile demonstrates formula-scoped trust without
granting whole-tap trust. The harness records no claim of cross-architecture
coverage; CI must supply Apple Silicon and Intel macOS evidence separately.
