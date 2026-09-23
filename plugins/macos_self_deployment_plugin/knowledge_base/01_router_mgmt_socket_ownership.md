# Router Mgmt Socket Ownership — Who May Unlink `<name>.router.sock`, and On What Evidence

Article Layer: 2

Article Role: local_ops_contract

Tags: knowledge:tag:local_ops_contract, knowledge:tag:self_deployment, knowledge:tag:blue_green, knowledge:tag:router, knowledge:tag:unix_socket

Article Tags: planning-stage:solet-lifecycle, evidence-category:local-ops-contract, domain:self-deployment, domain:local-solet

Embedding Description: Why a solet crash-loops with "router socket not found" after a restart even though the router process is alive, what the ownership rule for the blue-green router's Unix management socket is (only the server that bound a socket file may remove it, proven by st_dev/st_ino, and a socket path that still answers the status verb must never be taken), why an overlapping launchctl restart is the window that breaks both rules, and why router liveness must be tested by speaking the status verb rather than by checking whether the socket file exists.

## Purpose

The blue-green router's management plane is a Unix-domain socket at
`~/.ananta/runtime/<name>.router.sock`. Two routers briefly coexist on that one
path during any overlapping restart, and the rules for who may create and remove
the file were never written down. That gap is not academic: it produced adopter
issue #4 (`solet-public/macos-bizops`), where a solet crash-looped on a socket
its own live router was still holding.

This article states the ownership contract, the evidence each rule requires, and
the failure each rule prevents.

## The failure this contract prevents

An overlapping restart — `launchctl bootout` immediately followed by
`bootstrap`, and `launchctl kickstart -k`, which is overlapping *by design* —
runs two routers against one socket path for a moment:

1. Old router R1 holds socket S1 at path P.
2. R2 starts, takes P, and binds its own socket S2 there.
3. R1's shutdown runs and removes P — which is now **S2's file**, not S1's.
4. R2 keeps serving S2's file descriptor. It is alive to `lsof`, and its path is
   gone from disk.
5. The platform child waits for the router socket, checks whether the path
   exists, never sees it, and fails fatally at its deadline. The solet
   crash-loops while a perfectly healthy router serves an unreachable socket.

The signature to recognise: **the router process is alive, `lsof -U` shows it
holding the socket, and the socket path does not exist.**

## The ownership contract

**Rule 1 — only the binder may unlink.** A server may remove the socket file at
its path only if that file is still the one it bound. The proof is the
`(st_dev, st_ino)` pair captured immediately after bind and compared immediately
before unlink. Rebinding a path creates a *new inode*, so a mismatch is exactly
the condition "another server owns this now", and the correct response is to
leave the file alone and log that fact. Path equality is not ownership — the
path is a name, and the name gets reused.

**Rule 2 — never take a socket that answers.** Before claiming a path that
already exists, speak the `status` verb to it. A path that answers belongs to a
router that is still running, and taking it strands that router exactly as in
Rule 1's failure. Only a path that does not answer may be reclaimed.

**Rule 2a — but wait out a legitimate restart.** An answering incumbent is
*normal* during the seconds a restart takes, so refusing on sight would convert
`kickstart -k` from a working operator path into a hard failure. Wait the
incumbent out for a bounded window; refuse loudly only if it is still answering
past it. That wait must stay well under the platform child's own router-socket
deadline, or the trade is a worse failure: the router politely waiting while the
child times out and the platform crash-loops anyway. The two timeouts are one
budget, not two independent knobs.

**Rule 3 — liveness is a conversation, not a stat.** Never test router health by
checking whether the socket file exists. That check is wrong in both directions:

- A **stale socket file** left by a crashed router exists, so the check reads
  healthy and the caller proceeds against nothing.
- A **held-but-unlinked socket** does not exist as a path, so the check reads
  absent while the router is fine.

Connect and speak `status`. The reply is the only evidence of liveness.

## What a path-based check can and cannot do

Rule 3 fixes the stale-file direction completely. It does **not** fix the
held-but-unlinked direction, and no path-based check can: an unlinked socket is
unreachable by name, so nothing that starts from the path can find it. For that
state a probe can only improve the *diagnosis* — the fingerprint is an absent
`<name>.router.sock` beside a surviving `<name>.router.port`, confirmable with
`lsof -U | grep <name>.router.sock`. Rule 1 is what prevents the state from
happening. Do not let a better error message be mistaken for a fix.

## Implementation note — asyncio helps on one end and not the other

Two measured behaviours of `asyncio` (CPython 3.13) matter when reading this
code, and they point in opposite directions:

- **Bind is unguarded.** `start_unix_server` **removes any existing socket
  file** at the bind path before binding, and does *not* check whether anything
  is listening on it first. The event loop will steal a live router's socket
  without complaint. The `status` probe in Rule 2 runs before that call and is
  therefore the only thing standing between an incoming router and a live
  incumbent.
- **Teardown is already guarded.** `Server.close()` unlinks the socket path,
  but *only when it still owns it*: `_UnixSelectorEventLoop._stop_serving`
  records the inode at bind time and unlinks only if
  `os.stat(path).st_ino == prev_ino`. A path that has been rebound by another
  process is left alone.

The second point is easy to state too strongly, so state it precisely: asyncio
does **not** blindly unlink on close. It performs exactly the ownership check
this article requires — which is why the check belongs here as a rule rather
than as a local trick, and is a useful confirmation that the rule is the
conventional one rather than something invented for this codebase.

The consequence for the original defect is unchanged, and actually sharper:
because teardown already cleans up correctly and only when it should, an
explicit `unlink()` adds nothing for ordinary cleanup and is only ever capable
of removing a file that now belongs to someone else. The plugin's own
unconditional unlink was therefore the sole culprit, independent of anything
asyncio does. Any explicit unlink that remains should be ownership-checked and
logged, and understood as a statement of intent rather than as the mechanism
that does the work.

## Recovery

If a solet is crash-looping on a missing router socket:

1. Check whether the router is alive and holding an unlinked socket:
   `lsof -U | grep <name>.router.sock` with the path absent from
   `~/.ananta/runtime/`. If so, restart the router LaunchAgent — that re-creates
   the path. Do not delete the port file; it is the router's own discovery
   state.
2. If a socket file is present but nothing answers `status`, a router died
   without cleaning up. Remove the stale file and restart the router agent.
3. If neither the socket nor `<name>.router.port` is present, no router is
   installed for this solet — install it with
   `plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/install_router.py`
   before binding `deployment_service` to this plugin.

## Related

- The mgmt protocol itself: one JSON object per line,
  `{"verb": "<name>", "args": {...}}`, one JSON reply per line.
- Free-tier solets (`macos_free_minimal`) are single-color and have **no
  router**; an absent socket is expected there, not a fault.
