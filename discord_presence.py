"""Discord Rich Presence для FiercyLauncher.

При наличии общего Discord Application ID используется RPC с подробным статусом.
Без ID остаётся штатное автоопределение зарегистрированного FiercyLauncher.exe в Discord.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

log = logging.getLogger("fiercy.discord")


class DiscordPresenceManager:
    """Выполняет Discord IPC вне GUI-потока и повторно подключается после старта Discord."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="FiercyDiscordRPC", daemon=True)
        self._thread.start()

    def configure(self, enabled: bool, client_id: str) -> None:
        self._queue.put(("configure", bool(enabled), str(client_id or "").strip()))

    def set_idle(self) -> None:
        self._queue.put(("idle",))

    def set_playing(self, version: str, nickname: str, started_at: Optional[int] = None) -> None:
        # pypresence ожидает Unix timestamp в секундах, не миллисекундах.
        self._queue.put(("playing", str(version), str(nickname), int(started_at or time.time())))

    def shutdown(self) -> None:
        self._queue.put(("shutdown",))
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

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

    @staticmethod
    def _apply(client, desired: tuple) -> None:
        if desired[0] == "playing":
            _, version, nickname, started_at = desired
            client.update(
                details=f"Minecraft · {version}"[:128],
                state=(f"Игрок: {nickname}" if nickname else "Играет в Minecraft")[:128],
                start=int(started_at),
            )
        else:
            client.update(details="В меню лаунчера", state="FiercyLauncher")

    def _run(self) -> None:
        enabled = False
        client_id = ""
        client = None
        desired = ("idle",)
        last_attempt = 0.0
        retry_interval = 12.0

        while True:
            try:
                command = self._queue.get(timeout=1.0)
            except queue.Empty:
                command = None

            if command is not None:
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
                    last_attempt = 0.0
                elif kind == "idle":
                    desired = ("idle",)
                elif kind == "playing":
                    desired = command
                else:
                    continue

            if not enabled:
                if client is not None:
                    self._close_client(client)
                    client = None
                continue
            if not client_id:
                # Базовый статус без Application ID показывает сам Discord после
                # однократного добавления FiercyLauncher.exe в Registered Games.
                continue
            if client is None:
                now = time.monotonic()
                if now - last_attempt < retry_interval:
                    continue
                last_attempt = now
                try:
                    from pypresence import Presence
                    client = Presence(client_id)
                    client.connect()
                    log.info("Discord RPC connected.")
                except Exception as exc:
                    log.info("Discord RPC пока недоступен; повторное подключение будет позже: %s", exc)
                    self._close_client(client)
                    client = None
                    continue
            try:
                self._apply(client, desired)
            except Exception as exc:
                log.warning("Discord Rich Presence потерял соединение: %s", exc)
                self._close_client(client)
                client = None
                last_attempt = time.monotonic()
