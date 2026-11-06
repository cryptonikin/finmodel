"""Поиск колонки по заголовку (TASK_F0 §5).

Нормализация: trim, схлопывание пробелов и переносов, ё→е, без учёта регистра.
Сначала точное совпадение, затем «заголовок начинается с». Больше одного кандидата без occurrence → ошибка.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_WS = re.compile(r"\s+")


class HeaderError(Exception):
    pass


class ColumnNotFound(HeaderError):
    pass


class AmbiguousColumn(HeaderError):
    def __init__(self, name: str, indices: list[int]) -> None:
        letters = ", ".join(column_letter(i) for i in indices)
        super().__init__(f"заголовок «{name}» неоднозначен: колонки {letters}; укажите occurrence")
        self.indices = indices


def normalize(text: object) -> str:
    if text is None:
        return ""
    s = str(text).replace(" ", " ").replace("ё", "е").replace("Ё", "Е")
    return _WS.sub(" ", s).strip().casefold()


def column_letter(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    if index < 0:
        raise ValueError("index < 0")
    n, out = index + 1, ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def candidates(headers: Sequence[object], name: str) -> tuple[list[int], str]:
    """Индексы подходящих колонок и способ совпадения: exact | prefix | none."""
    target = normalize(name)
    if not target:
        return [], "none"
    norm = [normalize(h) for h in headers]
    exact = [i for i, h in enumerate(norm) if h == target]
    if exact:
        return exact, "exact"
    prefix = [i for i, h in enumerate(norm) if h and h.startswith(target)]
    return prefix, ("prefix" if prefix else "none")


def find_column(headers: Sequence[object], name: str, occurrence: int | None = None) -> int:
    """Индекс колонки (0-based). occurrence — какое по счёту совпадение взять (1, 2…)."""
    found, _ = candidates(headers, name)
    if not found:
        raise ColumnNotFound(f"заголовок «{name}» не найден")
    if occurrence is not None:
        if occurrence < 1:
            raise ValueError("occurrence должен быть ≥ 1")
        if occurrence > len(found):
            raise ColumnNotFound(f"заголовок «{name}»: нет {occurrence}-го совпадения (всего {len(found)})")
        return found[occurrence - 1]
    if len(found) > 1:
        raise AmbiguousColumn(name, found)
    return found[0]


def find_duplicates(headers: Sequence[object]) -> dict[str, list[int]]:
    """Повторяющиеся (после нормализации) непустые заголовки: исходный текст первого → индексы."""
    seen: dict[str, list[int]] = {}
    first: dict[str, str] = {}
    for i, h in enumerate(headers):
        n = normalize(h)
        if not n:
            continue
        seen.setdefault(n, []).append(i)
        first.setdefault(n, _WS.sub(" ", str(h)).strip())
    return {first[n]: idx for n, idx in seen.items() if len(idx) > 1}


def last_nonempty_index(values: Sequence[object]) -> int:
    """Индекс последнего непустого значения, -1 если пусто."""
    for i in range(len(values) - 1, -1, -1):
        if normalize(values[i]):
            return i
    return -1
