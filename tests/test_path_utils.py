import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from path_utils import migrate_game_data, resolve_launcher_root, selected_base_from_root


class LauncherPathTests(unittest.TestCase):
    def test_parent_folder_gets_launcher_directory(self):
        with TemporaryDirectory() as temp:
            base = Path(temp) / "dodo"
            expected = base / "FiercyLauncher"
            self.assertEqual(resolve_launcher_root(base), expected.resolve())

    def test_existing_launcher_folder_is_not_nested_twice(self):
        with TemporaryDirectory() as temp:
            selected = Path(temp) / "FiercyLauncher"
            self.assertEqual(resolve_launcher_root(selected), selected.resolve())
            self.assertEqual(selected_base_from_root(selected), selected.parent.resolve())

    def test_data_migration_merges_and_preserves_destination_files(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "old"
            new = root / "new" / "FiercyLauncher"
            old_world = old / "instances" / "profile-1" / "saves" / "World"
            old_world.mkdir(parents=True)
            (old_world / "level.dat").write_text("old-world", encoding="utf-8")
            (old / ".fiercy").mkdir()
            (old / ".fiercy" / "profiles.json").write_text("{}", encoding="utf-8")
            new_world = new / "instances" / "profile-1" / "saves" / "World"
            new_world.mkdir(parents=True)
            (new_world / "keep.txt").write_text("keep", encoding="utf-8")

            migrate_game_data(old, new)

            self.assertEqual((new_world / "level.dat").read_text(encoding="utf-8"), "old-world")
            self.assertEqual((new_world / "keep.txt").read_text(encoding="utf-8"), "keep")
            self.assertTrue((new / ".fiercy" / "profiles.json").is_file())

    def test_nested_migration_is_rejected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp) / "FiercyLauncher"
            root.mkdir()
            with self.assertRaises(ValueError):
                migrate_game_data(root, root / "nested" / "FiercyLauncher")


if __name__ == "__main__":
    unittest.main()
