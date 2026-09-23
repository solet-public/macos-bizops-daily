# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.** A public
report discloses the problem to everyone before there is a fix, including to the
people running this software on their own machines.

Two private channels, either is fine:

- **GitHub private vulnerability reporting** — use the **Report a vulnerability**
  button on this repository's Security tab. This opens a private advisory draft
  visible only to the maintainers.
- **Email** — <info@solet.ai>

## What to include

Whatever you have. A report we can reproduce is worth far more than a polished
one:

- what you did, and what happened
- which seed you are running — `PROVENANCE.json` at the repository root carries
  `source_commit` (the upstream commit this seed was built from), `seed_id`, and
  `bundle.name`; quoting that file is the most precise answer
- the impact you believe it has, and how you established that
- anything that narrows it down — a specific file, a specific command

If you are unsure whether something is a security issue or an ordinary defect,
send it here rather than to the public tracker. We would rather triage a
non-issue privately than have a real one disclosed publicly.

## What to expect

We are a very small operation with no support staff, so we cannot commit to a
response time. We do read what arrives here, and we treat this channel as
higher priority than the public tracker.

If a report is valid we will fix it, publish an advisory, and credit you unless
you would prefer we did not.

## Scope

This policy covers the code in this repository.

Note what this software does, because it shapes what counts as a vulnerability:
a solet provisions a local database, writes credentials to the system keychain,
installs a background service, and modifies the shell environment on the machine
that installs it. Anything that lets an untrusted party influence those
operations, reach those credentials, or execute code through them is in scope
and is exactly what we want to hear about.

Findings in a solet you have grown and modified yourself are yours to handle —
though if the underlying cause is in the seed, please tell us.

## Please do not send patches

Fixes are not accepted as pull requests, for the reasons in `CONTRIBUTING.md`.
A clear description of the vulnerability is the contribution we can act on.
