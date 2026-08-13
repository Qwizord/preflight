"""Разбор текста из файлов: спецификации деталей, материалы, количества.

В Рез-файлах тех дизайнер заполняет инфо-блок по шаблону из инструкции:
    ФОРМАТ ДЕТАЛИ: / ПЕЧАТЬ: / ГРАВИРОВКА: / ЦВЕТ: / КОЛ-ВО: / ПРОЧЕЕ:
В Принт-файлах итог обычно записан строкой «Итого: 90 униф медалей ...».
Это и есть машиночитаемая ведомость, которую можно сверять с финалкой и сделкой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── нормализация ────────────────────────────────────────────────────────────

def norm_text(s: str) -> str:
    """Схлопывает пробелы и чинит разрядку вокруг двоеточий из TextRun-ов."""
    s = s.replace(" ", " ").replace("꞉", ":")
    s = re.sub(r"\s*:\s*", ": ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── материалы ───────────────────────────────────────────────────────────────

MATERIALS = {
    "мдф":     [6.0, 19.0, 38.0],
    "дерево":  [20.0, 40.0],
    "металл":  [2.0, 3.0],
    "сталь":   [2.0, 3.0],
    "акрил":   [2.0, 5.0, 10.0],
    "фанера":  [6.0],
    "пластик": [],
}
_MAT_ALIAS = {"мдф": "мдф", "mdf": "мдф", "дерев": "дерево", "метал": "металл",
              "сталь": "сталь", "акрил": "акрил", "фанер": "фанера", "пластик": "пластик"}

_MAT_RE = re.compile(
    r"(?P<a>мдф|mdf|дерев\w*|метал\w*|сталь\w*|акрил\w*|фанер\w*|пластик\w*)"
    r"[\s,]*(?P<t>\d+(?:[.,]\d+)?)\s*(?:мм|mm)?"
    r"|(?P<t2>\d+(?:[.,]\d+)?)\s*(?:мм|mm)?\s*(?P<b>мдф|дерев\w*|метал\w*|сталь\w*|акрил\w*|фанер\w*)",
    re.I,
)


@dataclass(frozen=True)
class Material:
    kind: str            # мдф / металл / акрил ...
    thickness: float     # мм
    raw: str

    @property
    def is_known_thickness(self) -> bool:
        allowed = MATERIALS.get(self.kind, [])
        return not allowed or self.thickness in allowed

    def __str__(self) -> str:
        return f"{self.kind} {self.thickness:g}мм"


def _canon(word: str) -> str | None:
    w = word.lower()
    for pref, canon in _MAT_ALIAS.items():
        if w.startswith(pref):
            return canon
    return None


def find_materials(text: str) -> list[Material]:
    out: list[Material] = []
    for m in _MAT_RE.finditer(norm_text(text)):
        word = m.group("a") or m.group("b")
        thick = m.group("t") or m.group("t2")
        if not word or not thick:
            continue
        kind = _canon(word)
        if not kind:
            continue
        try:
            val = float(thick.replace(",", "."))
        except ValueError:
            continue
        if not (0.5 <= val <= 100):
            continue
        out.append(Material(kind=kind, thickness=val, raw=m.group(0).strip()))
    return out


# ── спецификация детали (инфо-блок) ─────────────────────────────────────────

_FIELDS = ["ФОРМАТ ДЕТАЛИ", "ПЕЧАТЬ", "ГРАВИРОВКА", "ЦВЕТ", "КОЛ-ВО", "ПРОЧЕЕ"]
_FIELD_ALT = {"КОЛ-ВО": r"КОЛ[-\s]?ВО", "ФОРМАТ ДЕТАЛИ": r"ФОРМАТ\s+ДЕТАЛИ"}
_ANY_FIELD = "|".join(_FIELD_ALT.get(f, re.escape(f)) for f in _FIELDS)


@dataclass
class PartSpec:
    format_raw: str = ""
    print: str = ""
    engraving: str = ""
    color: str = ""
    qty_raw: str = ""
    other: str = ""
    materials: list[Material] = field(default_factory=list)

    @property
    def qty(self) -> int | None:
        """«2 шт» → 2; «2 + 2 детали» → 4."""
        nums = [int(n) for n in re.findall(r"\d+", self.qty_raw)]
        if not nums:
            return None
        return sum(nums) if "+" in self.qty_raw else nums[0]

    @property
    def is_empty(self) -> bool:
        return not any((self.format_raw.strip(" -"), self.print.strip(" -"),
                        self.color.strip(" -"), self.qty_raw.strip(" -")))

    def __str__(self) -> str:
        bits = [f"формат={self.format_raw or '—'}"]
        if self.print.strip(" -"):
            bits.append(f"печать={self.print}")
        if self.engraving.strip(" -"):
            bits.append(f"грав={self.engraving}")
        if self.color.strip(" -"):
            bits.append(f"цвет={self.color}")
        if self.qty is not None:
            bits.append(f"кол-во={self.qty}")
        return ", ".join(bits)


def parse_specs(text: str) -> list[PartSpec]:
    t = norm_text(text)
    starts = [m.start() for m in re.finditer(_FIELD_ALT["ФОРМАТ ДЕТАЛИ"], t, re.I)]
    specs: list[PartSpec] = []
    for i, s in enumerate(starts):
        chunk = t[s: starts[i + 1] if i + 1 < len(starts) else len(t)]
        spec = PartSpec()
        for name in _FIELDS:
            pat = _FIELD_ALT.get(name, re.escape(name))
            m = re.search(rf"{pat}\s*:\s*(.*?)(?=(?:{_ANY_FIELD})\s*:|$)", chunk, re.I)
            val = norm_text(m.group(1)) if m else ""
            if name == "ФОРМАТ ДЕТАЛИ":
                spec.format_raw = val
            elif name == "ПЕЧАТЬ":
                spec.print = val
            elif name == "ГРАВИРОВКА":
                spec.engraving = val
            elif name == "ЦВЕТ":
                spec.color = val
            elif name == "КОЛ-ВО":
                spec.qty_raw = val
            else:
                spec.other = val
        spec.materials = find_materials(spec.format_raw)
        specs.append(spec)
    return specs


# ── количества ──────────────────────────────────────────────────────────────

_ITOGO_RE = re.compile(r"итого\s*:?\s*(.{0,120})", re.I)
_QTY_RE = re.compile(r"(\d[\d\s]{0,6})\s*(?:шт|компл\w*|коробок|коробки)\b", re.I)
# между числом и предметом часто стоит уточнение: «90 униф медалей»,
# «4 награды из гнутого металла», «15 комплектов наград»
_COUNTED_NOUN_RE = re.compile(
    r"(\d[\d\s]{0,6})\s*(?:[а-яё]{2,10}\.?\s+){0,2}"
    r"(наград\w*|медал\w*|кубк\w*|коробо?к\w*|лент\w*|шильд\w*|плакет\w*|значк\w*|комплект\w*)",
    re.I,
)


@dataclass
class QtyMention:
    n: int
    unit: str
    context: str


def find_quantities(text: str) -> list[QtyMention]:
    t = norm_text(text)
    seen: set[tuple[int, str]] = set()
    out: list[QtyMention] = []
    for rx, default_unit in ((_COUNTED_NOUN_RE, None), (_QTY_RE, "шт")):
        for m in rx.finditer(t):
            n = int(re.sub(r"\s", "", m.group(1)))
            unit = (m.group(2).lower() if default_unit is None else default_unit)
            if n <= 0 or (n, unit) in seen:
                continue
            seen.add((n, unit))
            lo, hi = max(0, m.start() - 30), min(len(t), m.end() + 20)
            out.append(QtyMention(n=n, unit=unit, context=t[lo:hi].strip()))
    return out


def total_from_itogo(text: str) -> list[QtyMention]:
    """Количества именно из строки «Итого: ...» — она обычно и есть тираж."""
    t = norm_text(text)
    out: list[QtyMention] = []
    for m in _ITOGO_RE.finditer(t):
        out.extend(find_quantities(m.group(1)))
    return out
