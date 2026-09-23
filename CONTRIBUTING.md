# Contributing

**Bug reports are welcome and wanted. Code contributions are not accepted.**

That combination is deliberate, and the reasoning is below.

## Report bugs — this is the contribution we actually want

If something in this seed is broken, misleading, or does not do what its
documentation says, please tell us. Use the issue forms:

- **Defect report** — something is broken or behaves incorrectly
- **Question** — something is unclear or undocumented
- **Feature request** — something is missing
- **Closure confirmation** — confirming a fix landed, or that it did not
- **Feedback round (parent issue)** — a batch of items from an ongoing adoption

The forms exist because the evidence they ask for is what makes a report
actionable. A defect report with the version, the command, and the actual
output is worth more to us than a patch.

**Security vulnerabilities do not go in an issue.** See
`.github/SECURITY.md` — there is a private channel, and a public issue would
disclose the problem before there is a fix.

## What to expect back

We are a very small operation with no support staff. **We read every report.**
We cannot promise a reply, a timeline, or that any particular item will be
worked. A filed feature request is information we are glad to have — it is not a
roadmap commitment, and we would rather say that plainly than let silence imply
otherwise.

## Why we do not accept pull requests or patches

Not because contributions are unwelcome in principle. Because of what this
software is.

A solet installs itself on the machine that runs it: it provisions a database,
writes credentials to the system keychain, installs a background service, and
modifies the shell environment. This seed is forked and installed by other
people, on their machines, with those permissions.

**Merging code we have not carefully reviewed would make this repository a
supply-chain path into every downstream install.** We do not have the staff to
review contributed code to that standard. Rather than review it badly, or merge
it unreviewed, we do not merge it at all.

That applies to pull requests, patches, and design proposals alike. Please do
not open one — not because it would be unwelcome, but because we would have to
decline it, and we would rather not waste your work.

## You do not need us to merge your change

This code is licensed under Apache-2.0 (see `LICENSE` and `NOTICE`). That licence
already gives you the right to modify and redistribute it.

A solet is meant to be **grown, not collaboratively edited**. Fork this seed and
grow your own: your instance evolves on your own machine, as its own entity,
carrying whatever changes you want. Nothing about our contribution policy limits
what you can build from it.

If your change fixes a bug that affects everyone, **file the bug report** — the
description is the useful part, and we can implement it in a form we are able to
stand behind.

## How the seed gets updated

Improvements are published here as new sealed commits, generated from an
upstream source tree. The repository is a published artifact, not a shared
development tree — which is the other reason pull requests against it do not
have anywhere to land.
