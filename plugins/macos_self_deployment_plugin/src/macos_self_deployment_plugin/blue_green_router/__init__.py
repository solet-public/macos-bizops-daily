"""Local blue-green router (Slice C of Step 5 lifecycle matrix).

Operator-side infrastructure under `deployment/` per Architect's Step 5
§12.5 framing. Outlives any the solet instance; the
`macos_self_deployment_plugin` (Slice E) consumes it via the Unix-domain
mgmt socket at `~/.ananta/runtime/<solet>.router.sock`.
"""
