"""Cloud Self-Deployment Service public discovery surface.

The actual contract lives at
``ananta.interfaces.cloud_self_deployment_service_interface.CloudSelfDeploymentServiceInterface``;
this package only carries the ``@service_interface_process`` registration
wrappers that make ``service_interface::cloud_self_deployment_service::*``
discoverable + callable via the platform's standard service-interface
dispatch path. See ``interfaces/public.py``.
"""
