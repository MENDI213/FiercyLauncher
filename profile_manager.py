"""Persistent, isolated Minecraft instances for FiercyLauncher."""
from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("fiercy.profiles")


class ProfileError(RuntimeError):
    """Ошибка пользовательской операции с игровым профилем."""


class ProfileManager:
    RESERVED_WINDOWS_NAMES = {
        "con", "prn", "aux", "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }

    def __init__(self, game_root: str | Path):
        self.root = Path(game_root).expanduser().resolve()
        self.instances_root = self.root / "instances"
        self.metadata_dir = self.root / ".fiercy"
        self.registry = self.metadata_dir / "profiles.json"
        self.instances_root.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self._profiles: list[dict[str, Any]] = []
        self._load()

    @classmethod
    def safe_folder_name(cls, name: str) -> str:
        """Create a readable Windows-safe directory name, preserving Unicode and spaces."""
        value = str(name or "").strip()
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
        value = re.sub(r"\s+", " ", value).strip(" .")
        value = value[:72].rstrip(" .")
        if not value:
            value = "Сборка"
        if value.split(".", 1)[0].casefold() in cls.RESERVED_WINDOWS_NAMES:
            value = f"{value}-profile"
        return value

    def _used_folder_names(self, exclude_profile_id: str = "") -> set[str]:
        return {
            str(profile.get("instance_folder") or self.safe_folder_name(profile.get("name", ""))).casefold()
            for profile in self._profiles
            if str(profile.get("id", "")) != str(exclude_profile_id)
        }

    def _unique_folder_name(self, name: str, exclude_profile_id: str = "") -> str:
        base = self.safe_folder_name(name)
        used = self._used_folder_names(exclude_profile_id)
        candidate = base
        index = 2
        while candidate.casefold() in used or (self.instances_root / candidate).exists():
            candidate = f"{base} ({index})"
            index += 1
        return candidate

    def _migrate_instance_folder(self, profile_id: str, name: str, saved_folder: str = "") -> str:
        """Migrate historical UUID/hex instance folders to instances/<profile-name>."""
        profile_id = str(profile_id)
        id_path = (self.instances_root / profile_id).resolve()
        if id_path.parent != self.instances_root:
            raise ProfileError("Недопустимый путь игрового профиля")

        saved = self.safe_folder_name(saved_folder) if saved_folder else ""
        # A previous build persisted the random profile ID itself into
        # instance_folder. Treat that value as a legacy path, not as the final
        # human-readable folder name. Also support older 12-32 char UUID folders.
        saved_is_legacy_id = bool(saved) and (
            saved.casefold() == profile_id.casefold()
            or bool(re.fullmatch(r"[0-9a-fA-F]{12,32}", saved))
        )
        source_name = saved if saved_is_legacy_id else (saved or profile_id)
        source_path = (self.instances_root / source_name).resolve()
        if source_path.parent != self.instances_root:
            raise ProfileError("Недопустимый путь игровой папки профиля")
        # Some intermediate builds stored a random folder field before moving
        # the actual files. If that field has no matching directory, fall back
        # to the original profile-ID directory instead of creating an empty
        # named folder and leaving the world data behind.
        if not source_path.exists() and id_path.exists():
            source_name = profile_id
            source_path = id_path

        preferred = saved if saved and not saved_is_legacy_id else self.safe_folder_name(name)
        used = self._used_folder_names(profile_id)
        target_name = preferred
        target = (self.instances_root / target_name).resolve()
        if target.parent != self.instances_root:
            raise ProfileError("Недопустимое имя папки игрового профиля")

        if target_name.casefold() in used:
            target_name = self._unique_folder_name(name, profile_id)
            target = self.instances_root / target_name

        if source_path != target and source_path.exists():
            if not target.exists():
                try:
                    source_path.rename(target)
                    log.info("Папка сборки переименована: %s -> %s", source_path, target)
                except OSError as exc:
                    # Files may be open by a running Minecraft process. Keep the
                    # actual legacy path rather than silently creating an empty one.
                    log.warning("Не удалось переименовать папку сборки %s: %s", source_path, exc)
                    target_name = source_name
            else:
                # Never overwrite an existing directory that may contain user data.
                target_name = self._unique_folder_name(f"{name} ({profile_id[:4]})", profile_id)
                target = self.instances_root / target_name
                try:
                    source_path.rename(target)
                    log.warning("Для сборки %s выбрана свободная папка %s: целевая папка уже существует",
                                name, target)
                except OSError as exc:
                    log.warning("Не удалось перенести старую папку сборки %s: %s", source_path, exc)
                    target_name = source_name
        return target_name

    def _load(self) -> None:
        try:
            data = json.loads(self.registry.read_text("utf-8")) if self.registry.exists() else {}
            raw = data.get("profiles", []) if isinstance(data, dict) else []
            for item in raw:
                if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                    continue
                profile_id = str(item["id"])
                name = str(item["name"])[:48]
                old_instance = (self.instances_root / profile_id).resolve()
                if old_instance.parent != self.instances_root:
                    continue
                profile = {
                    "id": profile_id,
                    "name": name,
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
                    "instance_folder": "",
                }
                # Старые версии автоматически создавали пустой профиль Minecraft.
                # Не удаляем папку: там могли остаться пользовательские данные.
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
                profile["instance_folder"] = self._migrate_instance_folder(
                    profile_id, name, str(item.get("instance_folder", "")),
                )
        except (OSError, ValueError, TypeError) as exc:
            # Не затираем повреждённый реестр пустым файлом.
            self._profiles = []
            if self.registry.exists():
                try:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    backup = self.registry.with_name(f"profiles.corrupt-{stamp}.json.bak")
                    self.registry.replace(backup)
                    log.error("Повреждённый реестр профилей сохранён в %s: %s", backup, exc)
                except OSError:
                    log.exception("Не удалось сохранить резервную копию повреждённого реестра профилей")
        self._save()

    def _save(self) -> None:
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        temp = self.registry.with_suffix(".tmp")
        payload = json.dumps({"schema": 2, "profiles": self._profiles}, ensure_ascii=False, indent=2)
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
        temp.replace(self.registry)

    @staticmethod
    def slugify(name: str) -> str:
        """Compatibility helper retained for existing callers/tests."""
        cleaned = re.sub(r"[^\w -]+", "", str(name or ""), flags=re.UNICODE)
        cleaned = re.sub(r"\s+", "-", cleaned.strip()).strip("-_").lower()
        return cleaned[:48] or "profile"

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
        if any(p["name"].casefold() == name.casefold() for p in self._profiles):
            raise ProfileError("Профиль с таким названием уже существует")
        minecraft_version = str(minecraft_version or "").strip()
        loader_version = str(loader_version or "").strip()
        if len(minecraft_version) > 64 or (minecraft_version and not re.fullmatch(r"[A-Za-z0-9._+-]+", minecraft_version)):
            raise ProfileError("Укажи корректную версию Minecraft")
        if len(loader_version) > 80 or (loader_version and not re.fullmatch(r"[A-Za-z0-9._+-]+", loader_version)):
            raise ProfileError("Некорректная версия загрузчика")
        if storage_root not in ("shared", "legacy"):
            raise ProfileError("Неизвестный тип хранилища профиля")
        if loader not in ("vanilla", "forge", "neoforge", "fabric", "quilt"):
            raise ProfileError("Неизвестный загрузчик Minecraft")
        profile_id = uuid.uuid4().hex[:12]
        instance_folder = self._unique_folder_name(name)
        profile = {
            "id": profile_id,
            "name": name,
            "minecraft_version": minecraft_version,
            "loader": loader,
            "loader_version": loader_version,
            "installed_version_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "description": str(description or "")[:240],
            "kind": str(kind or "minecraft"),
            "storage_root": storage_root,
            "modpack_source": str(modpack_source or ""),
            "modpack_name": str(modpack_name or ""),
            "instance_folder": instance_folder,
        }
        self._profiles.append(profile)
        self.instance_dir(profile_id).mkdir(parents=True, exist_ok=True)
        self._save()
        return dict(profile)

    def update(self, profile_id: str, **fields: Any) -> dict[str, Any]:
        immutable = {"minecraft_version", "loader"}
        allowed = {"name", "loader_version", "installed_version_id", "description",
                   "kind", "storage_root", "modpack_source", "modpack_name"}
        for profile in self._profiles:
            if profile["id"] != profile_id:
                continue
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
                old_path = self.instance_dir(profile_id)
                current_folder = str(profile.get("instance_folder") or profile["id"])
                if self.safe_folder_name(new_name).casefold() == current_folder.casefold():
                    new_folder = current_folder
                else:
                    new_folder = self._unique_folder_name(new_name, profile_id)
                new_path = self.instances_root / new_folder
                if old_path != new_path and old_path.exists():
                    try:
                        old_path.rename(new_path)
                    except OSError as exc:
                        raise ProfileError(f"Не удалось переименовать папку сборки: {exc}") from exc
                profile["name"] = new_name
                profile["instance_folder"] = new_folder
            if "storage_root" in fields and str(fields["storage_root"]) not in ("shared", "legacy"):
                raise ProfileError("Неизвестный тип хранилища профиля")
            if "loader_version" in fields:
                loader_version = str(fields["loader_version"] or "").strip()
                if len(loader_version) > 80 or (loader_version and not re.fullmatch(r"[A-Za-z0-9._+-]+", loader_version)):
                    raise ProfileError("Некорректная версия загрузчика")
                fields["loader_version"] = loader_version
            for key, value in fields.items():
                if key in allowed and key != "name":
                    profile[key] = str(value)
            self._save()
            return dict(profile)
        raise ProfileError("Профиль не найден")

    def delete(self, profile_id: str, delete_files: bool = False) -> None:
        profile = self.get(profile_id)
        if not profile:
            raise ProfileError("Профиль не найден")
        folder = str(profile.get("instance_folder") or profile_id)
        self._profiles = [p for p in self._profiles if p["id"] != profile_id]
        self._save()
        if delete_files:
            path = (self.instances_root / folder).resolve()
            if path.parent == self.instances_root:
                shutil.rmtree(path, ignore_errors=True)

    def instance_dir(self, profile_id: str) -> Path:
        profile = self.get(str(profile_id))
        folder = str(profile.get("instance_folder") if profile else profile_id)
        path = (self.instances_root / folder).resolve()
        if path.parent != self.instances_root:
            raise ProfileError("Недопустимый путь игрового профиля")
        path.mkdir(parents=True, exist_ok=True)
        return path
