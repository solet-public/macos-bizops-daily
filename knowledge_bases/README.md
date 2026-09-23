# Knowledge bases — the discovery directory

This directory is the platform's **knowledge-base discovery surface**. It holds
no authored content of its own. Every knowledge base is authored where it is
owned — beside the plugin or the platform package that ships it — and is
*discovered* here through a symlink that points back at that owning directory.

## Why the directory ships with only this file

A published seed carries no symlinks: the assemble step bans them, because they
do not survive archive extraction intact. A newly born solet therefore has
nothing to discover until genesis rebuilds the links, and a directory that
contains nothing cannot be published at all — an empty directory is not a
tracked object, so it would simply be absent from the tree.

This file is that anchor. It is the one tracked entry under the discovery
directory, and it exists so the directory itself is present from the moment the
tree is unpacked, before genesis has run and before any link has been created.
The root manifest declares this directory as a universal part of every
tree; without a tracked file inside it, that declaration is unsatisfiable in a
freshly published tree, and the structural check that reads the manifest
reports the directory missing.

## What genesis puts here

At birth, the genesis step that materializes discovery links walks the tree for
every knowledge-base directory that carries a `manifest.yaml` — those beside a
plugin, and those under the platform package — and creates one relative symlink
here for each. The link's **name** is the knowledge base's own declared name
from its manifest, which is not always the name of the directory it points at.

The step is an idempotent repair, not a rebuild. Running it again is a no-op for
links that are already correct; a link with a wrong or dangling target is
relinked; and a real file or directory sitting at a link name is never touched.
That last rule is why this file is safe: it is a real file, so the step leaves
it exactly as it found it, on the first run and on every run after.

## Working in this directory

Do not author content here, and do not commit a symlink here — the link set is
derived at birth, never declared, so a committed link would be a second,
drifting statement of something the tree can already work out for itself. To add
a knowledge base, author it in its owning package with a `manifest.yaml`; it
will be discovered here automatically.
