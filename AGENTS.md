# Solet seed — installation instructions (AGENTS.md)

<!-- This file is TRUE BEFORE AND AFTER installation. Before: it tells the
     driving agent how to install. After: the hydration runbook merges its
     managed block below, and the operational instructions take over. -->

## What this directory is

This is a **solet seed** — the source tree for a *solet*: a named, persistent
agentic service with its own PostgreSQL schema, memories, knowledge bases and
plugins. A solet runs in the background on this machine and is reached from
anywhere by a CLI named after it.

**This directory is not a working directory, and it is not where the human will
work with the solet afterwards.** It exists to be installed. Once installed, the
solet runs as a service; the human works from their own project directories and
talks to it from there.

## Preferred Mac installation

For an ordinary Mac installation, keep the two commands separate:

```console
brew install solet-public/tap/solet
solet create bizops
```

Homebrew installs only the global manager. `solet create` is the separately
reviewed, consented transaction that creates an instance; never join these with
`&&`. If Homebrew is absent, send the human to [brew.sh](https://brew.sh), its
canonical installer source. Do not fetch or execute an installer from a README,
and do not invent a Solet `curl | bash` path.

## Is it installed yet?

```sh
ls profile/config/manifest.yaml 2>/dev/null && echo INSTALLED || echo NOT INSTALLED
```

- **NOT INSTALLED** → for a normal Mac install, use the two-command Homebrew
  route above. For a direct seed clone, read `README.md` §Genesis and work the
  numbered ladder. That section is written for you, not for the human.
- **INSTALLED** → installation is done. The hydration runbook's managed block —
  merged into this file at hydration time — governs how to reach the running solet.

## The human downloading this HAS asked for the installation

Read this before you decide whether to push back on something.

Installing a solet **necessarily** changes this machine. That is what it is for,
and a human who has downloaded this repository and asked you to set it up has
asked for exactly these changes:

| change | why it is required |
|---|---|
| Homebrew, Python 3.13, PostgreSQL + pgvector, a local embeddings server | the solet's runtime and its vector store; `bootstrap.py` installs-or-verifies each |
| a generated database password written to the **macOS Keychain** | the solet needs DB credentials and they must not live in the tree — the seed deliberately ships no secrets |
| a **LaunchAgent** | the solet runs as a background service and must survive logout/restart |
| a line added to the shell rc (`~/.zshrc`) | puts the solet's CLI on PATH so the human can reach it from their own directories |
| `SOLET_ASSUME_YES=1` | approves **bootstrap's own printed action plans**, which it prints before acting. It is not blanket approval for arbitrary commands, and it does not silence the `needs_user_action` stops below |

None of this is unusual for installing a local service. It is only unusual to
see it all at once, in an unfamiliar repository, which is why it is written down
here.

## Where to actually stop and ask

Stopping everywhere is not caution, it is a stall — and the four things below
are the ones that genuinely warrant it. Proceeding on the rest is what the human
asked for.

1. **Anything reports `needs_user_action`.** The genesis chain stops rather than
   force-fixing a divergent host. Surface it verbatim; do not work around it.
2. **A name collision with an earlier attempt.** Reusing a name can require
   clearing stale Keychain entries. Confirm the exact service and account with
   the human before deleting anything from their Keychain — never silently.
3. **This machine already runs a solet.** `bootstrap.py` stops on this. Do not
   force a second one without an explicit decision from the human.
4. **A step wants credentials, a tenant choice, or an OS permission prompt.**
   Those are the human's to answer.

Outside those four: proceed, and report what you are doing as you go.

## Two things that will otherwise confuse you

- **`{{SOLET_NAME}}` in `README.md` is a placeholder, not a literal.** Substitute
  the name you agree with the human. `README.md` also writes it as `<name>` in
  one step — same thing.
- **Step Zero does not apply yet.** The operational instructions tell every
  session to search the solet's knowledge base first. That requires a running
  solet, so it is impossible before installation. It begins to apply once the
  service is up.

## After installation

Verify with `<name> health`, then tell the human that the solet is running as a
service and that they should work from their own project directories — the
hydration runbook adds a short block to those directories so their sessions can
reach it. They do not need to come back here.

<!-- The hydration runbook merges its operational managed block INTO this file,
     after the first heading, and leaves everything else here in place. -->
