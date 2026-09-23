#!/bin/sh
# Emit the fleet user.role tag (OSC 1337 SetUserVar), tmux-aware.
#
# Production hardening of the R1 spike reference impl
# (workbench/2026-08-03_r1_tmux_single_substrate_spike/emit_role_tag.sh,
# MEASURED green there) — identical logic, shipped as the in-repo artifact
# the tmux host adapter (tmux_adapter.py) invokes at spawn time and the
# launcher fix (dispatch-brief scope item 2) points the ~/.zshrc delta at.
#
# tmux SWALLOWS a raw (unwrapped) OSC 1337 SetUserVar emitted inside it —
# measured, not assumed (FINDINGS.md phase 2/3, negative control). The fix
# is DCS tmux-passthrough wrapping: inner ESCs doubled, terminated with ST.
# `allow-passthrough on` must be set on the tmux server for the wrapped form
# to reach the outer terminal at all (tmux_adapter.py sets this at spawn).
#
# Usage: emit_role_tag.sh <label> [--raw]
#   <label>  the fleet inspectability label (lane_id, or agent_instance_id
#            when no lane_id — NOT an authoritative role claim; L0 owns
#            identity, this tag is presentation-only and does not replay to
#            late-attaching clients, per FINDINGS.md).
#   --raw    force the unwrapped form even inside tmux (negative control —
#            without the DCS passthrough wrapper, tmux must swallow the OSC
#            and the tag must NOT land; used by the smoke suite).
role_b64=$(printf %s "$1" | base64)
if [ -n "$TMUX" ] && [ "$2" != "--raw" ]; then
    printf '\033Ptmux;\033\033]1337;SetUserVar=role=%s\007\033\\' "$role_b64"
else
    printf '\033]1337;SetUserVar=role=%s\007' "$role_b64"
fi
