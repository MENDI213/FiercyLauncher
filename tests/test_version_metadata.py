import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from version_metadata import read_version_metadata


class VersionMetadataTests(unittest.TestCase):
    def _write_version(self, root: Path, version_id: str, payload: dict) -> None:
        folder = root / "versions" / version_id
        folder.mkdir(parents=True)
        (folder / f"{version_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_vanilla_version(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_version(root, "1.21.1", {"id": "1.21.1"})
            meta = read_version_metadata(root, "1.21.1")
            self.assertEqual(meta["minecraft_version"], "1.21.1")
            self.assertEqual(meta["loader"], "vanilla")
            self.assertEqual(meta["loader_version"], "")

    def test_neoforge_uses_parent_minecraft_version(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            version_id = "neoforge-21.1.228"
            self._write_version(root, version_id, {
                "id": version_id,
                "inheritsFrom": "1.21.1",
            })
            meta = read_version_metadata(root, version_id)
            self.assertEqual(meta["minecraft_version"], "1.21.1")
            self.assertEqual(meta["loader"], "neoforge")
            self.assertEqual(meta["loader_version"], "21.1.228")

    def test_fabric_version_uses_parent_minecraft_version(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            version_id = "fabric-loader-0.16.9-1.20.1"
            self._write_version(root, version_id, {
                "id": version_id,
                "inheritsFrom": "1.20.1",
            })
            meta = read_version_metadata(root, version_id)
            self.assertEqual(meta["minecraft_version"], "1.20.1")
            self.assertEqual(meta["loader"], "fabric")
            self.assertEqual(meta["loader_version"], "0.16.9")


    def test_neoforge_1_20_1_coordinate_extracts_loader_not_minecraft_version(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            version_id = "neoforge-1.20.1-47.1.106"
            self._write_version(root, version_id, {
                "id": version_id,
                "inheritsFrom": "1.20.1",
            })
            meta = read_version_metadata(root, version_id)
            self.assertEqual(meta["minecraft_version"], "1.20.1")
            self.assertEqual(meta["loader"], "neoforge")
            self.assertEqual(meta["loader_version"], "47.1.106")

    def test_rejects_path_traversal(self):
        with TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                read_version_metadata(temp, "../outside")


if __name__ == "__main__":
    unittest.main()
