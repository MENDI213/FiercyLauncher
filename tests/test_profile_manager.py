import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from profile_manager import ProfileError, ProfileManager


class ProfileManagerTests(unittest.TestCase):
    def test_create_keeps_version_and_loader(self):
        with TemporaryDirectory() as temp:
            manager = ProfileManager(temp)
            profile = manager.create(
                "Fiercy Survival", minecraft_version="1.21.1", loader="neoforge",
                loader_version="21.1.228", storage_root="shared",
            )
            self.assertEqual(profile["minecraft_version"], "1.21.1")
            self.assertEqual(profile["loader"], "neoforge")
            self.assertEqual(profile["loader_version"], "21.1.228")
            self.assertEqual(profile["instance_folder"], "Fiercy Survival")
            self.assertTrue((Path(temp) / "instances" / "Fiercy Survival").is_dir())

    def test_profile_names_are_unique_case_insensitively(self):
        with TemporaryDirectory() as temp:
            manager = ProfileManager(temp)
            manager.create("Fiercy City")
            with self.assertRaises(ProfileError):
                manager.create("fiercy city")

    def test_metadata_round_trips(self):
        with TemporaryDirectory() as temp:
            manager = ProfileManager(temp)
            profile = manager.create("Imported", minecraft_version="1.20.1", loader="fabric")
            reloaded = ProfileManager(temp)
            saved = reloaded.get(profile["id"])
            self.assertIsNotNone(saved)
            self.assertEqual(saved["minecraft_version"], "1.20.1")
            self.assertEqual(saved["loader"], "fabric")
            registry = json.loads((Path(temp) / ".fiercy" / "profiles.json").read_text("utf-8"))
            self.assertEqual(registry["schema"], 2)
            self.assertEqual(saved["instance_folder"], "Imported")

    def test_profiles_are_optional_for_plain_minecraft(self):
        with TemporaryDirectory() as temp:
            manager = ProfileManager(temp)
            self.assertEqual(manager.all(), [])
            self.assertFalse((Path(temp) / "versions").is_dir())

    def test_legacy_empty_default_profile_is_hidden_without_deleting_data(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "instances" / "legacy-id").mkdir(parents=True)
            (root / "instances" / "legacy-id" / "keep.txt").write_text("data", "utf-8")
            metadata = root / ".fiercy"
            metadata.mkdir()
            (metadata / "profiles.json").write_text(json.dumps({
                "schema": 1,
                "profiles": [{
                    "id": "legacy-id", "name": "Minecraft",
                    "description": "Основной игровой профиль",
                    "minecraft_version": "", "loader": "vanilla",
                    "loader_version": "", "installed_version_id": "",
                }],
            }), "utf-8")
            manager = ProfileManager(root)
            self.assertEqual(manager.all(), [])
            self.assertEqual((root / "instances" / "legacy-id" / "keep.txt").read_text("utf-8"), "data")

    def test_last_custom_profile_can_be_deleted(self):
        with TemporaryDirectory() as temp:
            manager = ProfileManager(temp)
            only = manager.create("Only build")
            manager.delete(only["id"])
            self.assertEqual(manager.all(), [])


if __name__ == "__main__":
    unittest.main()


def test_minecraft_version_and_loader_are_immutable_after_creation():
    with TemporaryDirectory() as temp:
        manager = ProfileManager(temp)
        profile = manager.create("Immutable", minecraft_version="1.21.1", loader="neoforge",
                                 loader_version="21.1.267")
        with __import__("pytest").raises(ProfileError):
            manager.update(profile["id"], minecraft_version="1.21.4")
        with __import__("pytest").raises(ProfileError):
            manager.update(profile["id"], loader="fabric")
        changed = manager.update(profile["id"], loader_version="21.1.268")
        assert changed["minecraft_version"] == "1.21.1"
        assert changed["loader"] == "neoforge"
        assert changed["loader_version"] == "21.1.268"


def test_corrupt_registry_is_backed_up_instead_of_overwritten():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        meta = root / ".fiercy"
        meta.mkdir()
        registry = meta / "profiles.json"
        registry.write_text("{ this is not json", encoding="utf-8")
        manager = ProfileManager(root)
        assert manager.all() == []
        backups = list(meta.glob("profiles.corrupt-*.json.bak"))
        assert backups and backups[0].read_text("utf-8") == "{ this is not json"
        assert registry.read_text("utf-8").startswith("{")


def test_renaming_profile_migrates_folder_and_noop_rename_keeps_it():
    with TemporaryDirectory() as temp:
        manager = ProfileManager(temp)
        profile = manager.create("Survival")
        folder = manager.instance_dir(profile["id"])
        (folder / "saves.txt").write_text("keep", encoding="utf-8")
        unchanged = manager.update(profile["id"], name="Survival")
        assert unchanged["instance_folder"] == "Survival"
        assert manager.instance_dir(profile["id"]) == folder
        renamed = manager.update(profile["id"], name="Zombie World")
        assert renamed["instance_folder"] == "Zombie World"
        assert not folder.exists()
        assert (manager.instance_dir(profile["id"]) / "saves.txt").read_text("utf-8") == "keep"


def test_legacy_uuid_instance_is_migrated_to_readable_name():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        old_id = "0182ef00a67d"
        legacy = root / "instances" / old_id
        legacy.mkdir(parents=True)
        (legacy / "mods.jar").write_text("keep", encoding="utf-8")
        meta = root / ".fiercy"
        meta.mkdir()
        (meta / "profiles.json").write_text(json.dumps({
            "schema": 1,
            "profiles": [{"id": old_id, "name": "Zombie World", "minecraft_version": "1.21.1",
                          "loader": "neoforge", "loader_version": "21.1.268"}],
        }), encoding="utf-8")
        manager = ProfileManager(root)
        profile = manager.get(old_id)
        assert profile["instance_folder"] == "Zombie World"
        assert not legacy.exists()
        assert (root / "instances" / "Zombie World" / "mods.jar").read_text("utf-8") == "keep"


def test_legacy_profile_with_instance_folder_equal_to_id_migrates_to_name():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        profile_id = "0182ef00a67d"
        old = root / "instances" / profile_id
        (old / "saves" / "World").mkdir(parents=True)
        (old / "saves" / "World" / "level.dat").write_bytes(b"world")
        meta = root / ".fiercy"
        meta.mkdir()
        (meta / "profiles.json").write_text(json.dumps({
            "schema": 2,
            "profiles": [{
                "id": profile_id, "name": "Zombie Survival",
                "minecraft_version": "1.21.1", "loader": "neoforge",
                "loader_version": "21.1.268", "installed_version_id": "",
                "storage_root": "legacy", "instance_folder": profile_id,
            }],
        }), encoding="utf-8")
        manager = ProfileManager(root)
        profile = manager.get(profile_id)
        assert profile["instance_folder"] == "Zombie Survival"
        assert manager.instance_dir(profile_id) == root / "instances" / "Zombie Survival"
        assert (manager.instance_dir(profile_id) / "saves" / "World" / "level.dat").read_bytes() == b"world"
        assert not old.exists()


def test_legacy_profile_with_separate_hex_folder_migrates_to_name():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        profile_id = "profile-abc123"
        legacy_folder = "a4b7c219d03e"
        old = root / "instances" / legacy_folder
        old.mkdir(parents=True)
        (old / "options.txt").write_text("gamma:1.0", encoding="utf-8")
        meta = root / ".fiercy"
        meta.mkdir()
        (meta / "profiles.json").write_text(json.dumps({
            "schema": 2,
            "profiles": [{
                "id": profile_id, "name": "Fiercy Tech",
                "minecraft_version": "1.20.1", "loader": "forge",
                "loader_version": "47.4.0", "storage_root": "legacy",
                "instance_folder": legacy_folder,
            }],
        }), encoding="utf-8")
        manager = ProfileManager(root)
        assert manager.instance_dir(profile_id) == root / "instances" / "Fiercy Tech"
        assert (manager.instance_dir(profile_id) / "options.txt").read_text("utf-8") == "gamma:1.0"
        assert not old.exists()


def test_unknown_saved_hex_folder_falls_back_to_profile_id_directory():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        profile_id = "abcd1234ef567890"
        actual = root / "instances" / profile_id
        actual.mkdir(parents=True)
        (actual / "options.txt").write_text("forceUnicodeFont:true", encoding="utf-8")
        metadata = root / ".fiercy"
        metadata.mkdir()
        (metadata / "profiles.json").write_text(json.dumps({
            "schema": 2,
            "profiles": [{
                "id": profile_id, "name": "Legacy Pack",
                "minecraft_version": "1.21.1", "loader": "neoforge",
                "loader_version": "21.1.268", "storage_root": "legacy",
                "instance_folder": "deadbeef12345678",
            }],
        }), encoding="utf-8")
        manager = ProfileManager(root)
        assert manager.instance_dir(profile_id) == root / "instances" / "Legacy Pack"
        assert (manager.instance_dir(profile_id) / "options.txt").read_text("utf-8") == "forceUnicodeFont:true"
        assert not actual.exists()
