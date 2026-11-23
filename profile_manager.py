"""Persistent, isolated Minecraft instances for FiercyLauncher."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("fiercy.profiles")


class ProfileError(RuntimeError):
    """Ошибка пользовательской операции с игровым профилем."""


class ProfileManager:
    def __init__(self, game_root: str | Path):
        self.root = Path(game_root).expanduser().resolve()
        self.instances_root = self.root / "instances"
        self.metadata_dir = self.root / ".fiercy"
        self.registry = self.metadata_dir / "profiles.json"
        self.instances_root.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self._profiles: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.registry.read_text("utf-8")) if self.registry.exists() else {}
            raw = data.get("profiles", []) if isinstance(data, dict) else []
            for item in raw:
                if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                    continue
                instance = (self.instances_root / str(item["id"])).resolve()
                if instance.parent != self.instances_root:
                    continue
                profile = {
                    "id": str(item["id"]),
                    "name": str(item["name"])[:48],
                    "minecraft_version": str(item.get("minecraft_version", "")),
                    "loader": str(item.get("loader", "vanilla")),
                    "loader_version": str(item.get("loader_version", "")),
                    "installed_version_id": str(item.get("installed_version_id", "")),
                    "created_at": str(item.get("created_at", "")),
                    "description": str(item.get("description", ""))[:240],
                    "kind": str(item.get("kind", "minecraft")),
                    "storage_root": str(item.get("storage_root", "legacy")),
                    "modpack_source": str(item.get("modpack_source", "")),
                    "modpack_name": str(item.get("modpack_name", "")),
                }
                # Старые версии автоматически создавали пустой профиль Minecraft.
                # Это не пользовательская сборка: убираем его из списка, но не
                # удаляем папку instances/<id>, где могли остаться данные.
                is_legacy_placeholder = (
                    profile["name"].casefold() == "minecraft"
                    and profile["description"] == "Основной игровой профиль"
                    and not profile["minecraft_version"]
                    and not profile["installed_version_id"]
                    and profile["loader"] == "vanilla"
                    and not profile["loader_version"]
                )
                if is_legacy_placeholder:
                    continue
                self._profiles.append(profile)
        except (OSError, ValueError, TypeError) as exc:
            # Не затираем повреждённый реестр пустым файлом: бета-тестировщик
            # должен иметь возможность восстановить список своих сборок.
            self._profiles = []
            if self.registry.exists():
                try:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    backup = self.registry.with_name(f"profiles.corrupt-{stamp}.json.bak")
                    self.registry.replace(backup)
                    log.error("Повреждённый реестр профилей сохранён в %s: %s", backup, exc)
                except OSError:
                    log.exception("Не удалось сохранить резервную копию повреждённого реестра профилей")
        # Профили — необязательные сборки. Обычный Minecraft хранится в корне
        # game_root и не требует скрытого/автоматически созданного профиля.
        self._save()

    def _save(self) -> None:
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        temp = self.registry.with_suffix(".tmp")
        payload = json.dumps({"schema": 1, "profiles": self._profiles}, ensure_ascii=False, indent=2)
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
        temp.replace(self.registry)

    @staticmethod
    def slugify(name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-_").lower()
        return cleaned[:32] or "profile"

    def all(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self._profiles]

    def get(self, profile_id: str) -> dict[str, Any] | None:
        for profile in self._profiles:
            if profile["id"] == profile_id:
                return dict(profile)
        return None

    def create(self, name: str, description: str = "", kind: str = "minecraft",
               minecraft_version: str = "", loader: str = "vanilla",
               loader_version: str = "", storage_root: str = "shared",
               modpack_source: str = "", modpack_name: str = "") -> dict[str, Any]:
        name = " ".join(name.split()).strip()[:48]
        if not name:
            raise ProfileError("Укажи название сборки")
        if len(name) > 48:
            raise ProfileError("Название сборки слишком длинное")
        minecraft_version = str(minecraft_version or "").strip()
        loader_version = str(loader_version or "").strip()
        if len(minecraft_version) > 64 or (minecraft_version and not re.fullmatch(r"[A-Za-z0-9._+-]+", minecraft_version)):
            raise ProfileError("Укажи корректную версию Minecraft")
        if len(loader_version) > 80 or (loader_version and not re.fullmatch(r"[A-Za-z0-9._+-]+", loader_version)):
            raise ProfileError("Некорректная версия загрузчика")
        if any(p["name"].casefold() == name.casefold() for p in self._profiles):
            raise ProfileError("Профиль с таким названием уже существует")
        if storage_root not in ("shared", "legacy"):
            raise ProfileError("Неизвестный тип хранилища профиля")
        if loader not in ("vanilla", "forge", "neoforge", "fabric", "quilt"):
            raise ProfileError("Неизвестный загрузчик Minecraft")
        profile_id = uuid.uuid4().hex[:12]
        profile = {
            "id": profile_id,
            "name": name[:48],
            "minecraft_version": "",
            "loader": "vanilla",
            "loader_version": "",
            "installed_version_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "description": description[:240],
            "kind": kind,
            "storage_root": storage_root,
            "modpack_source": modpack_source,
            "modpack_name": modpack_name,
            "minecraft_version": minecraft_version,
            "loader": loader,
            "loader_version": loader_version,
        }
        self._profiles.append(profile)
        self.instance_dir(profile_id).mkdir(parents=True, exist_ok=True)
        self._save()
        return dict(profile)

    def update(self, profile_id: str, **fields: Any) -> dict[str, Any]:
        # Версия Minecraft и тип загрузчика задаются один раз при создании
        # сборки. После создания пользователь может менять только версию
        # выбранного загрузчика, имя и служебные метаданные.
        immutable = {"minecraft_version", "loader"}
        allowed = {"name", "loader_version", "installed_version_id", "description",
                   "kind", "storage_root", "modpack_source", "modpack_name"}
        for profile in self._profiles:
            if profile["id"] == profile_id:
                for key in immutable:
                    if key in fields and str(fields[key]) != str(profile.get(key, "")):
                        raise ProfileError(
                            "Версию Minecraft и загрузчик нельзя изменить после создания сборки. "
                            "Создай новую сборку с нужными параметрами."
                        )
                if "name" in fields:
                    new_name = " ".join(str(fields["name"]).split()).strip()[:48]
                    if not new_name:
                        raise ProfileError("Название профиля не может быть пустым")
                    if any(item["id"] != profile_id and item["name"].casefold() == new_name.casefold()
                           for item in self._profiles):
                        raise ProfileError("Профиль с таким названием уже существует")
                    fields["name"] = new_name
                if "storage_root" in fields and str(fields["storage_root"]) not in ("shared", "legacy"):
                    raise ProfileError("Неизвестный тип хранилища профиля")
                if "loader_version" in fields:
                    loader_version = str(fields["loader_version"] or "").strip()
                    if len(loader_version) > 80 or (loader_version and not re.fullmatch(r"[A-Za-z0-9._+-]+", loader_version)):
                        raise ProfileError("Некорректная версия загрузчика")
                    fields["loader_version"] = loader_version
                for key, value in fields.items():
                    if key in allowed:
                        profile[key] = str(value)
                self._save()
                return dict(profile)
        raise ProfileError("Профиль не найден")

    def delete(self, profile_id: str, delete_files: bool = False) -> None:
        profile = self.get(profile_id)
        if not profile:
            raise ProfileError("Профиль не найден")
        self._profiles = [p for p in self._profiles if p["id"] != profile_id]
        self._save()
        if delete_files:
            import shutil
            shutil.rmtree(self.instance_dir(profile_id), ignore_errors=True)

    def instance_dir(self, profile_id: str) -> Path:
        path = (self.instances_root / profile_id).resolve()
        if path.parent != self.instances_root:
            raise ProfileError("Недопустимый путь игрового профиля")
        path.mkdir(parents=True, exist_ok=True)
        return path
