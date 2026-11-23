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
            self.assertTrue((Path(temp) / "instances" / profile["id"]).is_dir())

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
            self.assertEqual(registry["schema"], 1)

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
