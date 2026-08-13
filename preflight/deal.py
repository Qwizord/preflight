"""Сборка сделки из папки: кто есть кто среди файлов.

Именование в реальных папках не единое («Рез_», «Рез, фрез_», «Рез, фрез, грав_»,
«Рез_фрез_»; бэкапы то «Резервная_копия_», то «Backup_of_»), поэтому роль файла
определяется набором шаблонов, а не точным совпадением.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

CDR_EXT = {".cdr"}
VECTOR_EXT = {".ai", ".pdf", ".eps"}
RASTER_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


class Role(str, Enum):
    FINAL = "финалка"
    WORK = "рабочий"
    PRINT = "принт"
    CUT = "рез"
    RIBBON = "ленты"
    BACKUP = "бэкап"
    PREVIEW = "превью"
    OTHER = "прочее"


# Разделителем в именах служит «_», а он считается словообразующим символом,
# поэтому \b после кириллицы не срабатывает — гасим продолжение слова явно.
_NOT_WORD = r"(?![а-яёa-z])"

_BACKUP_RE = re.compile(r"^(резервная[_ ]копия|backup[_ ]of|копия)[_ ]", re.I)
_PRINT_RE = re.compile(rf"^принт{_NOT_WORD}|^print{_NOT_WORD}", re.I)
_CUT_RE = re.compile(rf"^рез{_NOT_WORD}|^фрез{_NOT_WORD}|^грав{_NOT_WORD}", re.I)
_RIBBON_RE = re.compile(rf"ленты?{_NOT_WORD}|lanyard{_NOT_WORD}", re.I)
# Финалку называют как угодно: «21608_ФИН _3_», «АФЛ Новокузнецк_Фин»,
# «вейк серфинг фин», «fin_dinamo_pavel», «_21565_..._фин_Монтажная_область_1»,
# иногда вместо «фин» пишут «СОГЛАСОВАНО».
_FINAL_RE = re.compile(
    rf"(?:^|[\s_.\-])(?:фин|fin|финал\w*|согласовано){_NOT_WORD}", re.I)
_DEAL_NO_RE = re.compile(r"(?:^|[\s_#])(\d{5})(?:[\s_.\-]|$)")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip()


@dataclass
class DealFile:
    path: Path
    role: Role
    size: int
    mtime: datetime
    # что удалось вытащить из самого имени файла
    hints: dict[str, object] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem_norm(self) -> str:
        """macOS хранит имена в NFD («Й» = И + бревис), сравнивать можно только после NFC."""
        return _norm(self.path.stem).lower()

    @property
    def stem_clean(self) -> str:
        """Имя без префикса роли и без расширения — для сопоставления сделки."""
        s = self.path.stem
        s = _BACKUP_RE.sub("", s)
        s = re.sub(r"^(принт|print|рез[,_ ]*(фрез)?[,_ ]*(грав)?|фрез|грав)[_ ]+", "", s, flags=re.I)
        s = re.sub(r"^фин[_ ]*", "", s, flags=re.I)
        return _norm(s).lower()

    @property
    def backup_origin(self) -> str:
        """Для бэкапа — имя файла, копией которого он является.

        Роль срезать нельзя: «Резервная_копия_Рез_X» и «Принт_X» дают одинаковый
        stem_clean, и бэкап реза ошибочно сматчится с принтом.
        """
        return _norm(_BACKUP_RE.sub("", self.path.stem)).lower()


@dataclass
class Deal:
    folder: Path
    files: list[DealFile] = field(default_factory=list)
    title: str = ""
    deadline: str | None = None

    def of(self, *roles: Role) -> list[DealFile]:
        return [f for f in self.files if f.role in roles]

    @property
    def finals(self) -> list[DealFile]:
        return self.of(Role.FINAL)

    @property
    def prints(self) -> list[DealFile]:
        return self.of(Role.PRINT)

    @property
    def cuts(self) -> list[DealFile]:
        return self.of(Role.CUT)

    @property
    def works(self) -> list[DealFile]:
        return self.of(Role.WORK)

    @property
    def ribbons(self) -> list[DealFile]:
        return self.of(Role.RIBBON)

    @property
    def backups(self) -> list[DealFile]:
        return self.of(Role.BACKUP)


def _ribbon_hints(stem: str) -> dict[str, object]:
    """«Униф Ленты_Петровская лига_две стороны_90 шт» → {sides: 2, qty: 90}."""
    h: dict[str, object] = {}
    low = stem.lower()
    if re.search(r"дв[ух]?[её]?\s*сторон|двусторон|2\s*сторон", low):
        h["sides"] = 2
    elif re.search(r"одн[ао]?\s*сторон|односторон|1\s*сторон", low):
        h["sides"] = 1
    if m := re.search(r"(\d[\d\s]{0,6})\s*шт", low):
        h["qty"] = int(re.sub(r"\s", "", m.group(1)))
    if m := re.search(r"(\d{3,4})\s*мм", low):
        h["length_mm"] = int(m.group(1))
    return h


def classify(path: Path) -> tuple[Role, dict[str, object]]:
    stem, ext = _norm(path.stem), path.suffix.lower()
    hints: dict[str, object] = {}

    if _BACKUP_RE.match(stem):
        return Role.BACKUP, hints
    if _RIBBON_RE.search(stem):
        return Role.RIBBON, _ribbon_hints(stem)
    if _PRINT_RE.match(stem):
        return Role.PRINT, hints
    if _CUT_RE.match(stem):
        # рядом с «Рез_X.cdr» часто лежит «Рез_X.png» — это превью, не рез
        return (Role.PREVIEW if ext in RASTER_EXT else Role.CUT), hints
    if m := _DEAL_NO_RE.search(stem):
        hints["deal_no"] = m.group(1)
    if _FINAL_RE.search(stem) and ext in RASTER_EXT | {".pdf"}:
        return Role.FINAL, hints
    if ext in CDR_EXT | VECTOR_EXT:
        return Role.WORK, hints
    if ext in RASTER_EXT:
        return Role.PREVIEW, hints
    return Role.OTHER, hints


_FOLDER_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<dl>\d{2}\.\d{2}\.\d{2,4})\)\s*$")


def parse_folder_name(name: str) -> tuple[str, str | None]:
    """«Петровская лига (13.08.26)» → («Петровская лига», «13.08.26»)."""
    if m := _FOLDER_RE.match(_norm(name)):
        return m.group("title").strip(), m.group("dl")
    return _norm(name), None


def is_ignored(path: Path, role: Role) -> bool:
    """Файлы, которые в производство не уходят и проверять их незачем.

    Бэкапы Corel создаёт сам и в CRM они не попадают. Исходник лент в .cdr —
    рабочая заготовка, в дело идёт только выгруженный из неё PDF.
    """
    if role is Role.BACKUP:
        return True
    if role is Role.RIBBON and path.suffix.lower() == ".cdr":
        return True
    return False


def load_deal(folder: str | Path) -> Deal:
    folder = Path(folder)
    deal = Deal(folder=folder, title=folder.name)

    if m := _FOLDER_RE.match(folder.name):
        deal.title = m.group("title").strip()
        deal.deadline = m.group("dl")

    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        role, hints = classify(p)
        if is_ignored(p, role):
            continue
        st = p.stat()
        deal.files.append(
            DealFile(
                path=p,
                role=role,
                size=st.st_size,
                mtime=datetime.fromtimestamp(st.st_mtime),
                hints=hints,
            )
        )
    return deal
