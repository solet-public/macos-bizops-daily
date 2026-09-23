"""Regression smoke for bridge-port restoration across a router birth race.

The production seam is ``MacosSelfDeploymentPlugin.prepare_for_readiness``:
it scrubs a stale bridge pointer, then waits for the independently launched
router's management socket.  A router that becomes live during that wait must
leave a re-materialized pointer, not the absent file produced by the former
scrub-plus-one-shot-restore ordering.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from macos_self_deployment_plugin import plugin as plugin_module  # noqa: E402
from macos_self_deployment_plugin import stale_runtime_cleanup  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="prepare-readiness-race-") as temp_dir:
        runtime = Path(temp_dir) / ".ananta" / "runtime"
        runtime.mkdir(parents=True)
        bridge_port = runtime / "race.bridge.port"
        bridge_port.write_text("8100", encoding="utf-8")
        (runtime / "race.router.port").write_text("8801", encoding="utf-8")

        router_live = [False]
        original_runtime_dir = stale_runtime_cleanup.runtime_dir
        original_status = stale_runtime_cleanup.router_mgmt_status
        original_wait = plugin_module._wait_for_router_socket
        old_name = os.environ.get("SOLET_NAME")
        old_color = os.environ.get("SOLET_COLOR")
        try:
            stale_runtime_cleanup.runtime_dir = lambda: runtime
            def router_mgmt_status(socket_path: Path) -> dict[str, object] | None:
                del socket_path
                return {"ok": True} if router_live[0] else None

            stale_runtime_cleanup.router_mgmt_status = router_mgmt_status

            def wait_until_router_is_live(_path: Path) -> None:
                router_live[0] = True

            plugin_module._wait_for_router_socket = wait_until_router_is_live  # type: ignore[assignment]
            os.environ["SOLET_NAME"] = "race"
            os.environ["SOLET_COLOR"] = "blue"
            instance = plugin_module.MacosSelfDeploymentPlugin()
            instance._reconcile_releases = lambda: None  # type: ignore[method-assign]
            instance.set_ready = lambda: None  # type: ignore[method-assign]
            instance.prepare_for_readiness()
        finally:
            stale_runtime_cleanup.runtime_dir = original_runtime_dir
            stale_runtime_cleanup.router_mgmt_status = original_status
            plugin_module._wait_for_router_socket = original_wait
            _restore_env("SOLET_NAME", old_name)
            _restore_env("SOLET_COLOR", old_color)

        restored = bridge_port.exists() and bridge_port.read_text(encoding="utf-8") == "8801"
        print(f"router-birth bridge-port restored: {'PASS' if restored else 'FAIL'}")
        return 0 if restored else 1


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


if __name__ == "__main__":
    raise SystemExit(main())
