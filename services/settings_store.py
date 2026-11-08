"""Настройки, изменяемые из бота, и служебное состояние (хранятся в SQLite)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database import AppState, Setting

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    type: type
    default: Any
    label: str
    group: str
    min: float | None = None
    max: float | None = None
    choices: tuple[Any, ...] | None = None
    help: str = ""


GROUPS = {
    "collect": "⏱ Сбор статистики",
    "create": "📝 Создание публикаций",
    "status": "📈 Пороги статусов",
    "notify": "🔔 Уведомления",
    "ui": "🖥 Интерфейс",
}

METRIC_LABELS = {"v": "всего просмотров", "d1": "прирост за 24 ч", "d7": "прирост за 7 дн.", "d30": "прирост за 30 дн."}

SPECS: list[SettingSpec] = [
    # --- сбор
    SettingSpec("snapshot_interval_minutes", int, 30, "Интервал снимков просмотров, мин", "collect", 5, 1440,
                (15, 30, 45, 60, 120), "Как часто запрашивать getViews для каждой страницы. Рекомендуется 30–60."),
    SettingSpec("daily_sync_hours", int, 6, "Синхронизация данных по дням, ч", "collect", 1, 24, (3, 6, 12, 24),
                "Как часто загружать фактические просмотры за календарные сутки (getViews с датой)."),
    SettingSpec("snapshot_retention_days", int, 45, "Хранить все снимки, дней", "collect", 32, 3650, (35, 45, 90, 365),
                "Старше этого срока остаётся один снимок в сутки (нужно ≥ 32 для расчёта 30 дней)."),
    # --- создание
    SettingSpec("creation_delay_seconds", float, 1.0, "Пауза между созданием страниц, с", "create", 0, 30,
                (0.5, 1.0, 2.0, 5.0), "Защита от ограничений частоты Telegraph."),
    SettingSpec("max_batch_size", int, 100, "Максимум авторов в партии", "create", 1, 500, (50, 100, 200, 500)),
    SettingSpec("verify_after_create", bool, True, "Проверять страницу после создания (getPage)", "create",
                help="Дополнительный запрос к API getPage. Страница при этом не открывается и просмотр не засчитывается."),
    SettingSpec("fetch_youtube_titles", bool, True, "Загружать названия YouTube-видео", "create",
                help="Через публичный oEmbed YouTube. Ссылка в Telegraph не добавляется."),
    # --- статусы
    SettingSpec("status_min_age_hours", int, 48, "Мин. возраст публикации для статуса, ч", "status", 24, 720,
                (24, 48, 72, 168), "Пока публикация младше — статус «Мало данных»."),
    SettingSpec("status_min_views", int, 10, "Мин. просмотров для оценки динамики", "status", 1, 1_000_000,
                (5, 10, 20, 50, 100), "Ниже этого объёма активность не считается ни ростом, ни спадом."),
    SettingSpec("status_growth_ratio", float, 1.5, "Порог роста (× к обычной скорости)", "status", 1.01, 100,
                (1.2, 1.3, 1.5, 2.0, 3.0)),
    SettingSpec("status_decline_ratio", float, 0.5, "Порог спада (доля от обычной скорости)", "status", 0.01, 0.99,
                (0.3, 0.5, 0.6, 0.7)),
    SettingSpec("dead_views_24h", int, 3, "«Почти перестала»: не более N за 24 ч", "status", 0, 100000, (0, 3, 5, 10)),
    SettingSpec("trending_min_views_24h", int, 20, "Мин. прирост за 24 ч для «🔥 набирает»", "status", 1, 1_000_000,
                (10, 20, 50, 100, 300)),
    # --- уведомления
    SettingSpec("notify_daily_enabled", bool, False, "Публикация набрала N за сутки", "notify"),
    SettingSpec("notify_daily_threshold", int, 100, "Порог N просмотров за сутки", "notify", 1, 10_000_000,
                (50, 100, 300, 500, 1000)),
    SettingSpec("notify_leader_enabled", bool, False, "Новый лидер по приросту за 24 ч", "notify"),
    SettingSpec("notify_author_top_enabled", bool, False, "Новый автор на 1-м месте рейтинга", "notify"),
    SettingSpec("author_top_metric", str, "d7", "Метрика рейтинга для уведомления", "notify",
                choices=("v", "d1", "d7", "d30")),
    SettingSpec("notify_trending_enabled", bool, False, "Публикация начала быстро расти", "notify"),
    SettingSpec("notify_stalled_enabled", bool, False, "Публикация почти перестала набирать", "notify"),
    SettingSpec("notify_cooldown_hours", int, 24, "Не повторять уведомление чаще, ч", "notify", 1, 720,
                (6, 12, 24, 48, 72)),
    # --- интерфейс
    SettingSpec("page_size", int, 10, "Публикаций на странице", "ui", 3, 20, (5, 8, 10, 15, 20)),
]

SPEC_BY_KEY = {s.key: s for s in SPECS}

ChangeListener = Callable[[str, Any], Awaitable[None]]


def coerce(spec: SettingSpec, raw: Any) -> Any:
    """Приводит и проверяет значение; ValueError с понятным текстом при ошибке."""
    if spec.type is bool:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in {"1", "true", "да", "on", "вкл"}:
            return True
        if text in {"0", "false", "нет", "off", "выкл"}:
            return False
        raise ValueError("Ожидается да/нет")
    if spec.type is int:
        try:
            value: Any = int(float(str(raw).replace(",", ".").replace(" ", "")))
        except ValueError as exc:
            raise ValueError("Ожидается целое число") from exc
    elif spec.type is float:
        try:
            value = float(str(raw).replace(",", ".").replace(" ", ""))
        except ValueError as exc:
            raise ValueError("Ожидается число") from exc
    else:
        value = str(raw).strip()
        if spec.choices and value not in spec.choices:
            raise ValueError(f"Допустимые значения: {', '.join(map(str, spec.choices))}")
        return value
    if spec.min is not None and value < spec.min:
        raise ValueError(f"Минимум: {spec.min:g}")
    if spec.max is not None and value > spec.max:
        raise ValueError(f"Максимум: {spec.max:g}")
    return value


class SettingsStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], overrides: dict[str, Any] | None = None):
        self._sessionmaker = sessionmaker
        self._values: dict[str, Any] = {s.key: s.default for s in SPECS}
        if overrides:
            self._values.update(overrides)
        self._listeners: list[ChangeListener] = []

    async def load(self) -> None:
        async with self._sessionmaker() as session:
            rows = (await session.execute(select(Setting))).scalars().all()
        for row in rows:
            spec = SPEC_BY_KEY.get(row.key)
            if spec is None:
                continue
            try:
                self._values[row.key] = coerce(spec, json.loads(row.value))
            except (ValueError, json.JSONDecodeError):
                logger.warning("Некорректное значение настройки %s в БД, используется по умолчанию", row.key)

    def get(self, key: str) -> Any:
        return self._values[key]

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def on_change(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    async def set(self, key: str, raw: Any) -> Any:
        spec = SPEC_BY_KEY[key]
        value = coerce(spec, raw)
        async with self._sessionmaker() as session:
            row = await session.get(Setting, key)
            if row is None:
                session.add(Setting(key=key, value=json.dumps(value)))
            else:
                row.value = json.dumps(value)
            await session.commit()
        old = self._values.get(key)
        self._values[key] = value
        if old != value:
            logger.info("Настройка %s: %r -> %r", key, old, value)
            for listener in self._listeners:
                try:
                    await listener(key, value)
                except Exception:  # noqa: BLE001
                    logger.exception("Ошибка обработчика изменения настройки %s", key)
        return value

    # ---------------------------------------------------------------- состояние

    async def get_state(self, key: str, default: Any = None) -> Any:
        async with self._sessionmaker() as session:
            row = await session.get(AppState, key)
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return default

    async def set_state(self, key: str, value: Any) -> None:
        async with self._sessionmaker() as session:
            row = await session.get(AppState, key)
            payload = json.dumps(value, ensure_ascii=False, default=str)
            if row is None:
                session.add(AppState(key=key, value=payload))
            else:
                row.value = payload
            await session.commit()


def display_value(spec: SettingSpec, value: Any) -> str:
    if spec.type is bool:
        return "✅ вкл" if value else "❌ выкл"
    if spec.key == "author_top_metric":
        return METRIC_LABELS.get(value, str(value))
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)
