"""Горячее резервное копирование SQLite (безопасно во время работы бота)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def backup_sqlite(db_path: Path, backup_dir: Path, keep: int = 30) -> Path:
    """Создаёт согласованную копию БД через sqlite3 backup API и удаляет старые копии сверх keep."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    copies = sorted(backup_dir.glob("bot_*.db"))
    for old in copies[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass
    return target
