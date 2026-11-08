"""Проверка и разбор пользовательского ввода."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import unquote, urlparse

TITLE_MAX = 256
AUTHOR_MAX = 128
LABEL_MAX = 64

_BULLET_RE = re.compile(r"^\s*(?:[-•*·–—]+|\d{1,3}[.)])\s+")
_SPACES_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ValidationError(ValueError):
    """Ошибка проверки ввода с понятным пользователю текстом."""


def clean_line(text: str) -> str:
    text = _CONTROL_RE.sub("", text)
    return _SPACES_RE.sub(" ", text).strip()


def name_key(name: str) -> str:
    """Ключ для сравнения имён авторов без учёта регистра и лишних пробелов."""
    return clean_line(name).casefold().replace("ё", "е")


def validate_title(text: str | None) -> str:
    if not text:
        raise ValidationError("Заголовок не может быть пустым.")
    title = clean_line(text)
    if not title:
        raise ValidationError("Заголовок не может быть пустым.")
    if len(title) > TITLE_MAX:
        raise ValidationError(f"Заголовок слишком длинный: {len(title)} символов (максимум {TITLE_MAX}).")
    return title


def validate_author(text: str) -> str:
    name = clean_line(text)
    if not name:
        raise ValidationError("Имя автора не может быть пустым.")
    if len(name) > AUTHOR_MAX:
        raise ValidationError(f"Имя «{name[:30]}…» длиннее {AUTHOR_MAX} символов.")
    return name


def validate_label(text: str) -> str | None:
    label = clean_line(text)
    if label in {"", "-", "—"}:
        return None
    if len(label) > LABEL_MAX:
        raise ValidationError(f"Метка слишком длинная (максимум {LABEL_MAX} символов).")
    return label


@dataclass
class AuthorsParseResult:
    authors: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_authors(text: str, limit: int) -> AuthorsParseResult:
    """Каждый автор с новой строки. Убирает маркеры списков, пустые строки и дубли внутри списка."""
    result = AuthorsParseResult()
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = _BULLET_RE.sub("", raw)
        line = clean_line(line)
        if not line:
            continue
        try:
            name = validate_author(line)
        except ValidationError as exc:
            result.errors.append(str(exc))
            continue
        key = name_key(name)
        if key in seen:
            result.duplicates.append(name)
            continue
        seen.add(key)
        result.authors.append(name)
    if len(result.authors) > limit:
        result.errors.append(
            f"Слишком много авторов: {len(result.authors)}. Максимум за одну партию — {limit} "
            "(можно изменить в настройках)."
        )
    return result


_DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y")


def parse_date(text: str) -> date:
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # ДД.ММ без года — текущий год
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", text)
    if m:
        try:
            return date(date.today().year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    raise ValidationError(f"Не удалось распознать дату «{text}». Формат: ДД.ММ.ГГГГ")


def parse_date_range(text: str) -> tuple[date, date]:
    """«08.11.2026» или «01.11.2026-08.11.2026» (также через « - », «—», «..»)."""
    text = text.strip()
    try:
        d = parse_date(text)
        return d, d
    except ValidationError:
        pass
    candidates: list[tuple[str, str]] = []
    for sep in ("..", "—", "–", " - "):
        if sep in text:
            left, _, right = text.partition(sep)
            candidates.append((left, right))
    for idx, ch in enumerate(text):
        if ch == "-":
            candidates.append((text[:idx], text[idx + 1 :]))
    for left, right in candidates:
        try:
            d1, d2 = parse_date(left), parse_date(right)
        except ValidationError:
            continue
        if d2 < d1:
            d1, d2 = d2, d1
        return d1, d2
    raise ValidationError("Формат периода: ДД.ММ.ГГГГ или ДД.ММ.ГГГГ-ДД.ММ.ГГГГ")


def looks_like_date(text: str) -> bool:
    try:
        parse_date_range(text)
        return True
    except ValidationError:
        return False


_TELEGRAPH_HOSTS = {"telegra.ph", "www.telegra.ph", "graph.org", "www.graph.org", "te.legra.ph"}


def parse_telegraph_path(text: str) -> str | None:
    """Возвращает path страницы из ссылки Telegraph или None, если это не ссылка Telegraph."""
    text = (text or "").strip()
    if not text:
        return None
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _TELEGRAPH_HOSTS:
        return None
    path = unquote(parsed.path).strip("/")
    return path or None
