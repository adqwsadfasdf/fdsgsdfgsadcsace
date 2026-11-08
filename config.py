"""Загрузка конфигурации из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Ошибка в настройках окружения."""


def _str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"Переменная {name} должна быть числом, получено: {raw!r}") from exc


def _path(name: str, default: str) -> Path:
    raw = _str(name, default) or default
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    telegraph_token: str | None
    telegraph_short_name: str
    telegraph_author_url: str | None
    telegraph_api_url: str
    telegraph_timeout: float
    telegraph_min_interval: float
    telegraph_max_retries: int
    telegraph_concurrency: int
    database_path: Path
    backup_dir: Path
    log_level: str
    log_dir: Path
    tz_name: str
    timezone: ZoneInfo
    default_snapshot_interval: int


def load_config() -> Config:
    bot_token = _str("BOT_TOKEN")
    if not bot_token or ":" not in bot_token:
        raise ConfigError("Не задан корректный BOT_TOKEN в файле .env (получите его у @BotFather).")

    admin_raw = _str("ADMIN_ID")
    if not admin_raw:
        raise ConfigError("Не задан ADMIN_ID в файле .env (ваш числовой Telegram ID).")
    admin_ids: set[int] = set()
    for part in admin_raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            admin_ids.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"ADMIN_ID должен быть числом (или списком чисел через запятую): {part!r}") from exc
    if not admin_ids:
        raise ConfigError("ADMIN_ID пуст.")

    tz_name = _str("TIMEZONE", "Europe/Moscow") or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"Неизвестный часовой пояс TIMEZONE={tz_name!r}. Пример: Europe/Moscow. "
            "На Windows убедитесь, что установлен пакет tzdata."
        ) from exc

    interval = _int("SNAPSHOT_INTERVAL_MINUTES", 30)
    if not 5 <= interval <= 1440:
        raise ConfigError("SNAPSHOT_INTERVAL_MINUTES должен быть в диапазоне 5..1440")

    return Config(
        bot_token=bot_token,
        admin_ids=frozenset(admin_ids),
        telegraph_token=_str("TELEGRAPH_TOKEN"),
        telegraph_short_name=(_str("TELEGRAPH_SHORT_NAME", "TelegraphBot") or "TelegraphBot")[:32],
        telegraph_author_url=_str("TELEGRAPH_AUTHOR_URL"),
        telegraph_api_url=(_str("TELEGRAPH_API_URL", "https://api.telegra.ph") or "").rstrip("/"),
        telegraph_timeout=max(3.0, _float("TELEGRAPH_TIMEOUT", 20.0)),
        telegraph_min_interval=max(0.0, _float("TELEGRAPH_MIN_INTERVAL", 0.35)),
        telegraph_max_retries=max(0, _int("TELEGRAPH_MAX_RETRIES", 4)),
        telegraph_concurrency=max(1, _int("TELEGRAPH_CONCURRENCY", 3)),
        database_path=_path("DATABASE_PATH", "data/bot.db"),
        backup_dir=_path("BACKUP_DIR", "backups"),
        log_level=(_str("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_dir=_path("LOG_DIR", "logs"),
        tz_name=tz_name,
        timezone=tz,
        default_snapshot_interval=interval,
    )
