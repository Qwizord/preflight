"""Поиск папок со сделками под указанным корнем.

Раскладка у всех разная: у кого-то `2026/Август/Сделка (19.08.26)`, у кого-то
`ВИКА МАКЕТЫ/Запуск рез. коп./2026/июль/...`, а кто-то просто складывает
«июль 2026» на рабочий стол. Поэтому папка со сделкой опознаётся по содержимому
(внутри лежат макеты), а не по глубине вложенности.

Месяц для фильтра берётся из дедлайна в названии, иначе из пути, иначе из даты
изменения файлов.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .deal import parse_folder_name

# Файлы, по которым папка опознаётся как сделка.
DESIGN_EXT = {".cdr", ".ai", ".pdf", ".eps", ".jpg", ".jpeg", ".png",
              ".tif", ".tiff", ".bmp", ".svg"}

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
# в путях встречается и «июль», и «июля», и «07»
_MONTH_STEMS = [m.lower()[:4] for m in MONTHS]

SKIP_DIRS = {"__macosx", "node_modules", ".git", "$recycle.bin",
             "system volume information", "windows", "program files"}

MAX_DEPTH = 6
MAX_DEALS = 4000


def month_index(name: str) -> int | None:
    low = name.strip().lower()
    for i, stem in enumerate(_MONTH_STEMS):
        if low.startswith(stem):
            return i
    return None


def _is_deal(folder: Path) -> bool:
    """Папка со сделкой — та, в которой лежат сами макеты."""
    try:
        for f in folder.iterdir():
            if f.is_file() and f.suffix.lower() in DESIGN_EXT and not f.name.startswith("."):
                return True
    except (PermissionError, OSError):
        pass
    return False


def _period_from_deadline(deadline: str | None) -> tuple[int, int] | None:
    """«19.08.26» → (2026, 7). Месяц с нуля."""
    if not deadline:
        return None
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{2,4})$", deadline)
    if not m:
        return None
    _, mm, yy = m.groups()
    mm = int(mm)
    if not 1 <= mm <= 12:
        return None
    year = int(yy)
    if year < 100:
        year += 2000
    return year, mm - 1


def _period_from_path(parts: list[str]) -> tuple[int, int] | None:
    """Ищет в отрезках пути год и месяц, в любом порядке и в одном сегменте."""
    year = mon = None
    for seg in reversed(parts):
        if mon is None and (i := month_index(seg)) is not None:
            mon = i
        if y := re.search(r"(20\d{2})", seg):
            year = year or int(y.group(1))
        if year is not None and mon is not None:
            break
    if year is not None and mon is not None:
        return year, mon
    return None


def _period_from_mtime(folder: Path) -> tuple[int, int] | None:
    try:
        newest = max((f.stat().st_mtime for f in folder.iterdir() if f.is_file()),
                     default=None)
    except (PermissionError, OSError):
        return None
    if newest is None:
        return None
    d = datetime.fromtimestamp(newest)
    return d.year, d.month - 1


def scan(root: str | Path) -> list[dict]:
    """Все папки со сделками под корнем, независимо от глубины."""
    root = Path(root)
    out: list[dict] = []
    if not root.is_dir():
        return out

    # корень сам может быть одной сделкой
    if _is_deal(root):
        out.append(_describe(root, root))
        return out

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and len(out) < MAX_DEALS:
        folder, depth = stack.pop()
        try:
            children = sorted(p for p in folder.iterdir() if p.is_dir())
        except (PermissionError, OSError):
            continue
        for child in children:
            if child.name.startswith(".") or child.name.lower() in SKIP_DIRS:
                continue
            if _is_deal(child):
                out.append(_describe(child, root))
            elif depth + 1 < MAX_DEPTH:
                stack.append((child, depth + 1))

    out.sort(key=lambda d: (-d["year"], -d["month"], d["title"].lower()))
    return out


def _describe(folder: Path, root: Path) -> dict:
    title, deadline = parse_folder_name(folder.name)
    try:
        rel = folder.relative_to(root)
        parts = list(rel.parts[:-1])
    except ValueError:
        parts = []

    # Сначала — куда человек положил папку: так список совпадает с тем, что он
    # видит в проводнике. Дедлайн вторым: сделка из июльской папки со сроком
    # в августе не должна уезжать в другой месяц.
    period = (_period_from_path(parts)
              or _period_from_deadline(deadline)
              or _period_from_mtime(folder))
    if period:
        year, mon = period
        label = f"{year} · {MONTHS[mon]}"
    else:
        year, mon, label = 0, 0, "Без даты"

    return {"path": str(folder), "name": folder.name, "title": title,
            "deadline": deadline, "year": year, "month": mon, "period": label,
            "where": " / ".join(parts)}
