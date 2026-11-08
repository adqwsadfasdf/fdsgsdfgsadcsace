"""Резервное копирование базы из командной строки: python backup.py"""
from __future__ import annotations

import sys

from config import ConfigError, load_config
from utils.backup import backup_sqlite


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}")
        return 1
    if not config.database_path.exists():
        print(f"База данных не найдена: {config.database_path}")
        return 1
    target = backup_sqlite(config.database_path, config.backup_dir)
    print(f"Резервная копия создана: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
