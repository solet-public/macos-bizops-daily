import logging

logger = logging.getLogger(__name__)


class ServiceInjector:
    def __init__(self, services: dict[str, object]):
        self._services = services
        self._injection_map = {
            "set_state_service": "state_service",
            "set_file_service": "file_service",
            "set_orchestrator": "orchestrator",
        }

    def inject_services(self, plugins: dict[str, object]) -> None:
        for plugin_name, plugin in plugins.items():
            self._inject_plugin_services(plugin_name, plugin)

    def _inject_plugin_services(self, plugin_name: str, plugin: object) -> None:
        injected_services: list[str] = []

        for method_name, service_key in self._injection_map.items():
            if hasattr(plugin, method_name) and service_key in self._services:
                try:
                    method = getattr(plugin, method_name)
                    if callable(method):
                        method(self._services[service_key])
                        injected_services.append(service_key)
                except Exception as e:
                    logger.error(f"Failed to inject {service_key} into {plugin_name}: {e}")

        if injected_services:
            logger.debug(
                f"Injected {len(injected_services)} services into {plugin_name}: {', '.join(injected_services)}"
            )
        else:
            pass

    def add_service(self, key: str, service: object) -> None:
        self._services[key] = service

    def remove_service(self, key: str) -> object | None:
        return self._services.pop(key, None)

    def has_service(self, key: str) -> bool:
        return key in self._services

    def get_service(self, key: str) -> object | None:
        return self._services.get(key)

    def register_injection_method(self, method_name: str, service_key: str) -> None:
        self._injection_map[method_name] = service_key
