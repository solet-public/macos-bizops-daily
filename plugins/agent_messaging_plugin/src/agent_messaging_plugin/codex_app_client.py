# pyright: reportUnusedClass=false
"""JSON-RPC client for one persistent managed Codex app-server."""

from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .codex_common import _DEFAULT_RPC_TIMEOUT_SECONDS
from .headless_adapter import _sigterm_then_kill

if TYPE_CHECKING:
    from pathlib import Path


class _RpcError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _AppServerLaunch:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    developer_instructions: str
    model: str


class _CodexAppServerClient:
    """Small stdlib-only JSON-RPC client for one persistent app-server."""

    def __init__(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        developer_instructions: str,
        model: str,
        popen_fn: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        request_timeout_seconds: float = _DEFAULT_RPC_TIMEOUT_SECONDS,
    ) -> None:
        self._launch = _AppServerLaunch(
            argv=argv,
            cwd=cwd,
            env=dict(env),
            developer_instructions=developer_instructions,
            model=model,
        )
        self._popen_fn = popen_fn
        self._request_timeout_seconds = request_timeout_seconds
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self._waiters: dict[str, queue.Queue[dict[str, Any] | BaseException]] = {}
        self._request_sequence = 0
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._thread_id = ""
        self._active_turn_id = ""
        self._pending_driver_text: str | None = None
        self._completed_turn_ids: set[str] = set()
        self._proc: subprocess.Popen[str] | None = None

    @property
    def pid(self) -> int:
        if self._proc is None:
            raise RuntimeError("Codex app-server has not started")
        return self._proc.pid

    def start(self) -> None:
        self._proc = self._popen_fn(
            self._launch.argv,
            cwd=str(self._launch.cwd),
            env=self._launch.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            start_new_session=True,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        threading.Thread(target=self._reader_loop, daemon=True).start()
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "solet_managed_sessions",
                    "title": "Managed sessions",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self._notify("initialized")
        self._start_thread()

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def send(self, text: str) -> None:
        self.insert(text)
        self.submit()

    def insert(self, text: str) -> None:
        self._pending_driver_text = text

    def submit(self) -> None:
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        text = self._pending_driver_text
        if text is None:
            raise DriverChannelSendError("Codex app-server submit called before insert")
        try:
            with self._dispatch_lock:
                if text == "/clear":
                    self._interrupt_active_turn()
                    self._start_thread()
                    return
                if text == "/compact":
                    self._interrupt_active_turn()
                    self._request("thread/compact/start", {"threadId": self._thread_id})
                    return
                self._send_turn(text)
        except (OSError, _RpcError, queue.Empty, ValueError) as exc:
            raise DriverChannelSendError(
                f"Codex app-server driver channel rejected the dispatch: {exc}",
            ) from exc

    def close(self, grace_seconds: float) -> None:
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()
        _sigterm_then_kill(proc.pid, proc, grace_seconds)

    def _start_thread(self) -> None:
        params: dict[str, object] = {
            "approvalPolicy": "never",
            "cwd": str(self._launch.cwd),
            "developerInstructions": self._launch.developer_instructions,
            "ephemeral": False,
            "sandbox": "danger-full-access",
        }
        if self._launch.model:
            params["model"] = self._launch.model
        result = self._request("thread/start", params)
        thread = result.get("thread")
        if not isinstance(thread, Mapping) or not str(thread.get("id") or ""):
            raise _RpcError(f"thread/start returned no thread id: {result!r}")
        with self._state_lock:
            self._thread_id = str(thread["id"])
            self._active_turn_id = ""

    def _send_turn(self, text: str) -> None:
        input_items = [{"type": "text", "text": text}]
        with self._state_lock:
            thread_id = self._thread_id
            active_turn_id = self._active_turn_id
        if active_turn_id:
            self._request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": active_turn_id,
                    "input": input_items,
                },
            )
            return
        result = self._request(
            "turn/start",
            {"threadId": thread_id, "input": input_items},
        )
        turn = result.get("turn")
        if not isinstance(turn, Mapping) or not str(turn.get("id") or ""):
            raise _RpcError(f"turn/start returned no turn id: {result!r}")
        turn_id = str(turn["id"])
        with self._state_lock:
            if turn_id not in self._completed_turn_ids:
                self._active_turn_id = turn_id

    def _interrupt_active_turn(self) -> None:
        with self._state_lock:
            thread_id = self._thread_id
            turn_id = self._active_turn_id
        if not turn_id:
            return
        self._request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )
        with self._state_lock:
            self._active_turn_id = ""

    def _request(self, method: str, params: Mapping[str, object]) -> dict[str, Any]:
        with self._state_lock:
            self._request_sequence += 1
            request_id = str(self._request_sequence)
            waiter: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
            self._waiters[request_id] = waiter
        try:
            self._write({"id": request_id, "method": method, "params": dict(params)})
        except BaseException:
            with self._state_lock:
                self._waiters.pop(request_id, None)
            raise
        try:
            item = waiter.get(timeout=self._request_timeout_seconds)
        except queue.Empty:
            with self._state_lock:
                self._waiters.pop(request_id, None)
            raise
        if isinstance(item, BaseException):
            raise item
        if "error" in item:
            raise _RpcError(f"{method} failed: {item['error']!r}")
        result = item.get("result")
        if not isinstance(result, dict):
            raise _RpcError(f"{method} returned a non-object result: {result!r}")
        return result

    def _notify(self, method: str) -> None:
        self._write({"method": method})

    def _write(self, payload: Mapping[str, object]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise _RpcError("Codex app-server stdin is closed")
        with self._write_lock:
            proc.stdin.write(json.dumps(dict(payload)) + "\n")
            proc.stdin.flush()

    def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._dispatch_message(json.loads(line))
            raise _RpcError(
                "Codex app-server closed stdout; stderr tail: "
                + " | ".join(self._stderr_tail)[-2000:],
            )
        except BaseException as exc:  # noqa: BLE001 - wake every blocked request
            self._fail_waiters(exc)

    def _dispatch_message(self, message: object) -> None:
        if not isinstance(message, dict):
            raise _RpcError(f"invalid JSON-RPC message: {message!r}")
        if "method" in message and "id" in message:
            self._write(
                {
                    "id": message["id"],
                    "result": self._server_request_result(str(message["method"])),
                },
            )
            return
        if "method" in message:
            self._handle_notification(message)
            return
        if "id" not in message:
            return
        with self._state_lock:
            waiter = self._waiters.pop(str(message["id"]), None)
        if waiter is not None:
            waiter.put(message)

    def _fail_waiters(self, exc: BaseException) -> None:
        with self._state_lock:
            waiters = list(self._waiters.values())
            self._waiters.clear()
        for waiter in waiters:
            waiter.put(exc)

    def _handle_notification(self, message: Mapping[str, object]) -> None:
        if message.get("method") != "turn/completed":
            return
        params = message.get("params")
        if not isinstance(params, Mapping):
            return
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            return
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            return
        with self._state_lock:
            self._completed_turn_ids.add(turn_id)
            if self._active_turn_id == turn_id:
                self._active_turn_id = ""

    @staticmethod
    def _server_request_result(method: str) -> dict[str, object]:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"decision": "accept"}
        if method == "tool/requestUserInput":
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline"}
        return {}

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr_tail.append(line.rstrip("\n"))
