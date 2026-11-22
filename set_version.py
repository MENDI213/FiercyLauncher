from __future__ import annotations
import re
import sys
from pathlib import Path
from packaging.version import Version, InvalidVersion

path = Path(__file__).resolve().with_name("version.py")
if len(sys.argv) != 2:
    raise SystemExit("Использование: py set_version.py 3.5.1")
new_version = sys.argv[1].strip().lstrip("vV")
try:
    Version(new_version)
except InvalidVersion as exc:
    raise SystemExit(f"Некорректная версия: {new_version}") from exc
source = path.read_text(encoding="utf-8")
updated, count = re.subn(r'LAUNCHER_VERSION\s*=\s*"[^"]+"', f'LAUNCHER_VERSION = "{new_version}"', source, count=1)
if count != 1:
    raise SystemExit("Не удалось найти LAUNCHER_VERSION в version.py")
path.write_text(updated, encoding="utf-8")

# Синхронизируем публичный JSON, чтобы страница конфигурации не показывала старую версию.
content_path = path.parent / "content" / "launcher.json"
if content_path.is_file():
    import json
    content = json.loads(content_path.read_text(encoding="utf-8"))
    launcher = content.setdefault("launcher", {})
    if isinstance(launcher, dict):
        launcher["latest_version"] = new_version
        content_path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

print(f"Version updated: {new_version}")
