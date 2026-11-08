"""Собирает архив проекта для загрузки на bothost.ru (или любой другой хостинг с загрузкой zip).

    python make_bothost_zip.py

В архив НЕ попадают секреты и данные: .env, data/, logs/, backups/, .venv, кэши, тесты.
bot.py и requirements.txt лежат в корне архива (так требует bothost).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "telegram_telegraph_bot_bothost.zip"
SKIP_DIRS = {".venv", "venv", "data", "logs", "backups", "tests", "__pycache__", ".pytest_cache", ".git", ".idea",
             ".vscode", ".claude"}
SKIP_FILES = {".env", OUT.name, "requirements-dev.txt", "pytest.ini"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log", ".zip"}


def files() -> list[Path]:
    result = []
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.name in SKIP_FILES or path.suffix in SKIP_SUFFIXES or path.name.endswith((".db-wal", ".db-shm")):
            continue
        result.append(path)
    return result


def main() -> None:
    items = files()
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in items:
            # прямые слэши в путях — иначе на Linux файлы распакуются с «\» в имени
            zf.write(path, path.relative_to(ROOT).as_posix())
    names = {p.relative_to(ROOT).as_posix() for p in items}
    assert "bot.py" in names and "requirements.txt" in names
    print(f"Готово: {OUT.name} ({OUT.stat().st_size // 1024} КБ, файлов: {len(items)})")


if __name__ == "__main__":
    main()
