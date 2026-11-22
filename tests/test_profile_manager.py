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
