"""Non-blocking Discord Rich Presence integration for FiercyLauncher."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

log = logging.getLogger("fiercy.discord")


class DiscordPresenceManager:
    """Keeps Discord RPC work off the Qt UI thread and retries after disconnects."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="FiercyDiscordRPC", daemon=True)
        self._thread.start()

    def configure(self, enabled: bool, client_id: str) -> None:
        self._queue.put(("configure", bool(enabled), str(client_id or "").strip()))

    def set_idle(self) -> None:
        self._queue.put(("idle",))

    def set_playing(self, version: str, nickname: str, started_at: Optional[int] = None) -> None:
        self._queue.put(("playing", str(version), str(nickname), int(started_at or time.time())))

    def shutdown(self) -> None:
        self._queue.put(("shutdown",))
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.5)

    @staticmethod
    def _close_client(client) -> None:
        if client is not None:
            try:
                client.clear()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

    def _run(self) -> None:
        enabled = False
        client_id = ""
        client = None
        desired = ("idle",)
        while True:
            command = self._queue.get()
            kind = command[0]
            if kind == "shutdown":
                self._close_client(client)
                return
            if kind == "configure":
                _, enabled, new_client_id = command
                if new_client_id != client_id or not enabled:
                    self._close_client(client)
                    client = None
                client_id = new_client_id
                desired = ("idle",)
                if enabled and not client_id:
                    log.info("Discord custom Rich Presence без Application ID отключён; используйте Registered Games для отображения EXE.")
            elif kind == "idle":
                desired = ("idle",)
            elif kind == "playing":
                desired = command
            else:
                continue

            if not enabled:
                self._close_client(client)
                client = None
                continue
            if not client_id:
                continue
            try:
                if client is None:
                    from pypresence import Presence
                    client = Presence(client_id)
                    client.connect()
                    log.info("Discord RPC connected.")
                if desired[0] == "playing":
                    _, version, nickname, started_at = desired
                    client.update(details=f"Minecraft · {version}",
                                  state=f"Игрок: {nickname}" if nickname else "Играет в Minecraft",
                                  start=started_at)
                else:
                    client.update(details="В меню лаунчера", state="FiercyLauncher")
            except Exception as exc:
                log.warning("Discord Rich Presence недоступен: %s", exc)
                self._close_client(client)
                client = None
