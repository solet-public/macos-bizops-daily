# Verify Local Deploy Health

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:post-approval, planning-stage:wbs-execution, evidence-category:joseki, domain:self-deployment, domain:platform-operations


JOSEKI_KEY: verify_local_deploy_health
DESCRIPTION: Verify the live local platform is healthy after a blue-green deploy cutover. Confirms the router holds exactly one active color with no swap in progress and no lingering drain entries, then lists every plugin's status on the active release so not-ready, unexpectedly disabled, or stopped-service plugins surface immediately. Use right after a deploy completes, or any time a quick whole-platform health snapshot is needed. Fully closed-world: no bindings required.
EMBEDDING_DESCRIPTION: Check that the local platform is healthy after a blue-green deploy: confirm the router reports a single active color with the swap finished and the old color fully drained, then review the plugin roster for any plugin that is not ready, unexpectedly disabled, or whose lifecycle services stopped running. Routine post-deploy health verification snapshot with zero inputs.

## Input Contract

- A running local platform: router up, exactly one active color serving
- No caller inputs; the card is closed-world with zero binding slots

## Output Contract

- Router state recorded on the run flow: active color and instance id, swap_in_progress false, drain entries empty
- Full plugin roster recorded on the run flow: per-plugin status, enabled flag, and lifecycle is_running for review

## Sequence

[ ] 1. Confirm the router has one active color and no swap in progress
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Confirm router swap state (service_interface::local_self_deployment_service::swap_status)
        Arguments:
        {}

[ ] 2. Verify plugin health on the active release
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) List every plugin's health (service_interface::lifecycle_management_service::list_plugins)
        Arguments:
        {}

## Expected Step Count

2 steps.

## Binding Guidance

- No binding slots. The card runs as-is; both steps take empty arguments by design.

## Coherence Obligations

- A healthy verdict requires BOTH: swap_in_progress false with empty drain entries, AND no plugin row with status error or a lifecycle-managed plugin not running. The steps record evidence; the consumer of the run evidence applies the verdict.
- If a swap is still in progress, the snapshot is not a health verdict — re-run after the drain completes rather than interpreting a mid-swap roster.

## Next Joseki

Explicitly absent — a failing roster today routes to operator diagnosis; a repair card should be authored when a repair procedure stabilizes.

## Repair Joseki

Explicitly absent as a card. On an unhealthy roster the platform repair verbs are service_interface::local_self_deployment_service::swap_rollback (inside the drain window) and rollback_release (after); route to the operator.
