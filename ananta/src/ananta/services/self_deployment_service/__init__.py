"""Self-deployment service public discovery surface.

The actual contract lives at
``ananta.interfaces.self_deployment_service_interface.SelfDeploymentServiceInterface``;
this package only carries the ``@service_interface_process`` registration
wrapper that makes
``service_interface::self_deployment_service::restart_with_manifest``
discoverable + callable via the platform's standard service-interface
dispatch path. See ``interfaces/public.py``.

The 4-verb cloud extension surface lives in the sibling
``ananta.services.cloud_self_deployment_service`` package — those verbs
are not bound on macOS profiles so the namespace separation keeps
``process_search('self_deployment')`` returning only the 1-verb base on
macOS-bound solets.
"""
