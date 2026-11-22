"""Microsoft OAuth (PKCE) authentication for Minecraft Java Edition."""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit
import webbrowser

import requests
from PyQt5.QtCore import QThread, pyqtSignal, QSettings
from version import USER_AGENT
try:
    import keyring
except ImportError:  # Keep the launcher usable when dependencies were not installed via the .bat file.
    keyring = None
import minecraft_launcher_lib as mll

log = logging.getLogger("fiercy.auth")
SERVICE_NAME = "FiercyLauncher"
REDIRECT_URI = "http://localhost:8765"


class _CallbackServer(HTTPServer):
    allow_reuse_address = True


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.callback_url = f"http://localhost:8765{self.path}"
        body = ("<html><meta charset='utf-8'><title>FiercyLauncher</title>"
                "<body style='font-family:Segoe UI;background:#101522;color:#fff;text-align:center;padding:48px'>"
                "<h2>Авторизация завершена</h2><p>Вернись в FiercyLauncher. Это окно можно закрыть.</p></body></html>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class AuthManager:
    def __init__(self):
        self.settings = QSettings("Fiercy", "FiercyLauncher")

    @property
    def client_id(self) -> str:
        return str(self.settings.value("auth/microsoft_client_id", "")).strip()

    def account(self) -> dict | None:
        name = str(self.settings.value("auth/player_name", "")).strip()
        uuid = str(self.settings.value("auth/player_uuid", "")).strip()
        access_token = None
        if keyring is not None:
            try:
                access_token = keyring.get_password(SERVICE_NAME, "minecraft_access_token")
            except Exception as exc:
                log.debug("Не удалось прочитать кеш Minecraft access token: %s", exc)
        # Кеш нужен, чтобы после первого подтверждённого входа не открывать браузер
        # при каждом запуске лаунчера. Сам токен хранится в системном хранилище.
        if name and uuid and access_token:
            return {"name": name, "id": uuid, "access_token": access_token}
        return None

    def refresh_token(self) -> str | None:
        if keyring is None:
            return None
        try:
            return keyring.get_password(SERVICE_NAME, "microsoft_refresh_token")
        except Exception:
            return None

    def save_account(self, account: dict) -> None:
        if keyring is None:
            raise RuntimeError(
                "Не установлена библиотека keyring. Для сохранения сессии установи зависимости "
                "из requirements.txt и перезапусти приложение. До закрытия лаунчера текущий "
                "вход может использоваться только в оперативной памяти."
            )
        keyring.set_password(SERVICE_NAME, "microsoft_refresh_token", account["refresh_token"])
        keyring.set_password(SERVICE_NAME, "minecraft_access_token", account["access_token"])
        self.settings.setValue("auth/player_name", account["name"])
        self.settings.setValue("auth/player_uuid", account["id"])
        self.settings.sync()

    def clear(self) -> None:
        if keyring is not None:
            for key in ("microsoft_refresh_token", "minecraft_access_token"):
                try:
                    keyring.delete_password(SERVICE_NAME, key)
                except Exception as exc:
                    log.debug("Не удалось удалить %s из keyring: %s", key, exc)
        self.settings.remove("auth/player_name")
        self.settings.remove("auth/player_uuid")
        self.settings.sync()


class LoginWorker(QThread):
    completed = pyqtSignal(dict)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, client_id: str, refresh: bool = False):
        super().__init__()
        self.client_id = client_id.strip()
        self.refresh = refresh

    def run(self):
        try:
            if not self.client_id:
                raise RuntimeError("Сначала укажи Microsoft Client ID в настройках лаунчера")
            if self.refresh:
                refresh_token = AuthManager().refresh_token()
                if not refresh_token:
                    raise RuntimeError("Сохранённая сессия не найдена")
                self.status.emit("Обновляем сессию Microsoft…")
                account = mll.microsoft_account.complete_refresh(self.client_id, None, None, refresh_token)
            else:
                self.status.emit("Ожидаем вход в окне браузера Microsoft…")
                server = _CallbackServer(("localhost", 8765), _CallbackHandler)
                server.timeout = 1
                server.callback_url = None
                try:
                    login_url, state, verifier = mll.microsoft_account.get_secure_login_data(self.client_id, REDIRECT_URI)
                    webbrowser.open(login_url, new=2, autoraise=True)
                    deadline = 180
                    while deadline > 0 and not server.callback_url:
                        if self.isInterruptionRequested():
                            raise RuntimeError("Вход отменён")
                        server.handle_request()
                        deadline -= 1
                    if not server.callback_url:
                        raise RuntimeError("Время ожидания входа истекло. Попробуй ещё раз")
                    callback_url = server.callback_url
                finally:
                    server.server_close()
                auth_code = mll.microsoft_account.parse_auth_code_url(callback_url, state)
                self.status.emit("Проверяем аккаунт Minecraft…")
                account = mll.microsoft_account.complete_login(self.client_id, None, REDIRECT_URI, auth_code, verifier)
            result = dict(account)
            required = ("name", "id", "access_token", "refresh_token")
            if not all(result.get(k) for k in required):
                raise RuntimeError("Microsoft вернула неполный профиль Minecraft")
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class AvatarWorker(QThread):
    """Асинхронно загружает только официальную текстуру профиля Minecraft."""
    loaded = pyqtSignal(bytes)
    failed = pyqtSignal(str)

    def __init__(self, texture_url: str):
        super().__init__()
        parsed = urlsplit(str(texture_url).strip())
        if parsed.hostname != "textures.minecraft.net" or parsed.scheme not in ("http", "https"):
            raise ValueError("Неизвестный адрес скина Minecraft")
        if not parsed.path.startswith("/texture/"):
            raise ValueError("Некорректный адрес текстуры Minecraft")
        self.texture_url = "https://textures.minecraft.net" + parsed.path

    def run(self):
        try:
            response = requests.get(
                self.texture_url, timeout=(5, 15), stream=True,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            length = int(response.headers.get("Content-Length", "0") or 0)
            if content_type and not content_type.startswith("image/"):
                raise RuntimeError("Сервис скинов вернул файл неизвестного формата")
            if length > 4 * 1024 * 1024:
                raise RuntimeError("Файл скина слишком большой")
            chunks = bytearray()
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                chunks.extend(chunk)
                if len(chunks) > 4 * 1024 * 1024:
                    raise RuntimeError("Файл скина превышает допустимый размер")
            if not chunks:
                raise RuntimeError("Сервис скинов вернул пустой файл")
            self.loaded.emit(bytes(chunks))
        except Exception as exc:
            log.debug("Не удалось загрузить текстуру профиля: %s", exc)
            self.failed.emit(str(exc))
