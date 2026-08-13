"""Детерминированные проверки сделки.

Здесь только то, что считается кодом и не требует модели: комплектность,
сверка количеств, толщины материалов против инструкции, структура слоёв,
свежесть файлов. Всё, что требует зрения и смысла (опечатки, «тот ли логотип»),
живёт отдельно и на этот модуль не влияет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from .cdr import read_cdr
from .deal import Deal, DealFile, Role
from .spec import (MATERIALS, PartSpec, find_materials, find_quantities, parse_specs,
                   total_from_itogo)


class Severity(IntEnum):
    NOTE = 1     # к сведению
    WARN = 2     # проверить глазами
    STOP = 3     # так в производство нельзя


@dataclass
class Finding:
    code: str
    severity: Severity
    title: str
    detail: str = ""
    files: list[str] = field(default_factory=list)
    confidence: float = 1.0        # 1.0 — арифметика, ниже — эвристика

    def __str__(self) -> str:
        mark = {Severity.STOP: "СТОП", Severity.WARN: "ВНИМ", Severity.NOTE: "инфо"}[self.severity]
        s = f"[{mark}] {self.title}"
        if self.detail:
            s += f"\n        {self.detail}"
        if self.files:
            s += f"\n        файлы: {', '.join(self.files)}"
        return s


@dataclass
class DealAnalysis:
    deal: Deal
    findings: list[Finding] = field(default_factory=list)
    specs: dict[str, list[PartSpec]] = field(default_factory=dict)
    totals: dict[str, list[int]] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    @property
    def worst(self) -> Severity | None:
        return max((f.severity for f in self.findings), default=None)


# ── вспомогательное ─────────────────────────────────────────────────────────

def _read_text(f: DealFile) -> str:
    """Текст из файла любого поддерживаемого типа."""
    ext = f.path.suffix.lower()
    try:
        if ext == ".cdr":
            return read_cdr(f.path).text
        if ext in {".pdf", ".ai", ".eps"}:
            import pymupdf
            with pymupdf.open(f.path) as d:
                return " ".join(d[i].get_text() for i in range(d.page_count))
    except Exception:
        return ""
    return ""


def _shares_words(a_name: str, b_name: str) -> bool:
    """Есть ли у двух названий общее значимое слово (регистр и хвосты не важны)."""
    def words(s: str) -> set[str]:
        return {w[:5] for w in re.findall(r"[^\W\d_]{3,}", s.lower())}
    return bool(words(a_name) & words(b_name))


def _escalate(sev: Severity, tirazh: int | None) -> Severity:
    """Одна мелочь на 5 шт — мелочь; она же на 900 шт — переделка тиража."""
    if tirazh and tirazh >= 100 and sev == Severity.WARN:
        return Severity.STOP
    return sev


# ── сами проверки ───────────────────────────────────────────────────────────

def analyze(deal: Deal) -> DealAnalysis:
    a = DealAnalysis(deal=deal)

    for f in deal.of(Role.PRINT, Role.CUT, Role.WORK, Role.RIBBON):
        a.texts[f.name] = _read_text(f)
    for f in deal.of(Role.PRINT, Role.CUT):
        a.specs[f.name] = parse_specs(a.texts.get(f.name, ""))

    tirazh = _tirazh(a)

    _check_completeness(a)
    _check_staleness(a, tirazh)
    _check_specs(a, tirazh)
    _check_quantities(a, tirazh)
    _check_ribbons(a, tirazh)
    _check_layers(a)

    # один и тот же файл лент часто лежит и .cdr, и .pdf — не дублируем замечание
    seen: set[tuple[str, str]] = set()
    unique: list[Finding] = []
    for f in a.findings:
        key = (f.code, f.title)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    a.findings = sorted(unique, key=lambda f: (-f.severity, f.code))
    return a


def _tirazh(a: DealAnalysis) -> int | None:
    """Самое большое правдоподобное количество по сделке — оценка тиража."""
    ns: list[int] = []
    for f in a.deal.prints + a.deal.cuts:
        ns += [q.n for q in total_from_itogo(a.texts.get(f.name, ""))]
        ns += [q.n for q in find_quantities(a.texts.get(f.name, ""))]
    for f in a.deal.ribbons:
        if (q := f.hints.get("qty")):
            ns.append(int(q))
    ns = [n for n in ns if 0 < n < 100_000]
    return max(ns) if ns else None


def _check_completeness(a: DealAnalysis) -> None:
    d = a.deal
    if not d.finals:
        # Финалку могли удалить из папки — источник истины всё равно сделка в CRM.
        a.add(Finding("NO_FINAL", Severity.NOTE,
                      "В папке нет финалки",
                      "Возьмите утверждённую финалку из сделки в CRM — сверять надо с ней.",
                      confidence=0.9))
    # Бывают сделки из одних лент — там принта и реза и не должно быть.
    ribbons_only = bool(d.ribbons) and not d.prints and not d.cuts
    if not d.prints and not d.cuts and not ribbons_only:
        a.add(Finding("NO_PROD", Severity.STOP, "Нет ни принта, ни реза",
                      "В папке отсутствуют производственные файлы."))
    if not ribbons_only:
        if not d.cuts:
            a.add(Finding("NO_CUT", Severity.WARN, "Нет файла реза",
                          "Частый тип брака: «отсутствует рез на вид медалей», «отсутствует рез на шильды»."))
        if not d.prints:
            a.add(Finding("NO_PRINT", Severity.WARN, "Нет файла принта",
                          "Частый тип брака: «отсутствует принт», «нет принта на обратную сторону»."))
    if not d.works:
        a.add(Finding("NO_WORK", Severity.NOTE, "Нет рабочего файла (.ai/.cdr)",
                      "Для дозаказа это норма; для новой сделки — повод проверить."))

    # ленты упомянуты в текстах, но файла лент нет
    mentions = [n for n, t in a.texts.items() if re.search(r"\bлент", t, re.I)]
    if mentions and not d.ribbons:
        a.add(Finding("NO_RIBBON", Severity.WARN, "Ленты упоминаются, но файла лент нет",
                      "Тип брака «отсутствие файла на ленты».", files=mentions))



def _check_staleness(a: DealAnalysis, tirazh: int | None) -> None:
    """Финалка новее производственных файлов — печатали по старому макету.

    Прямо из журнала брака: «напечатали по старому макету, новый закинули когда
    уже всё было принято в работу», «взяли старый неправильный файл».
    """
    prod = a.deal.prints + a.deal.cuts
    if not prod or not a.deal.finals:
        return
    newest_final = max(a.deal.finals, key=lambda f: f.mtime)
    for f in prod:
        gap = (newest_final.mtime - f.mtime).total_seconds() / 3600
        if gap > 1:
            a.add(Finding(
                "STALE_PRODUCTION", _escalate(Severity.WARN, tirazh),
                f"Финалка новее, чем «{f.name}»",
                f"финалка {newest_final.mtime:%d.%m %H:%M}, файл {f.mtime:%d.%m %H:%M} "
                f"(разница {gap:.0f} ч). Проверьте, что производственный файл собран "
                "по актуальной финалке.",
                files=[newest_final.name, f.name], confidence=0.8))


def _check_specs(a: DealAnalysis, tirazh: int | None) -> None:
    for f in a.deal.cuts:
        specs = a.specs.get(f.name, [])
        if not specs:
            # Инфо-блок обязателен только для МДФ и дерева; металл, акрил и
            # прочее подписывают иначе, требовать блок там нельзя.
            text = a.texts.get(f.name, "")
            kinds = {m.kind for m in find_materials(text)}
            if kinds & {"мдф", "дерево"}:
                a.add(Finding("SPEC_MISSING", Severity.WARN,
                              f"В резе нет инфо-блока, хотя есть МДФ/дерево: {f.name}",
                              "Для МДФ и дерева деталь подписывается блоком ФОРМАТ ДЕТАЛИ / "
                              "ПЕЧАТЬ / ГРАВИРОВКА / ЦВЕТ / КОЛ-ВО / ПРОЧЕЕ.",
                              files=[f.name], confidence=0.8))
            continue
        for i, s in enumerate(specs, 1):
            if s.is_empty:
                a.add(Finding("SPEC_EMPTY", Severity.WARN,
                              f"Инфо-блок детали {i} пустой ({f.name})",
                              files=[f.name]))
                continue
            if not s.materials:
                a.add(Finding("SPEC_NO_MATERIAL", Severity.NOTE,
                              f"У детали {i} не распознан материал: «{s.format_raw}»",
                              files=[f.name], confidence=0.6))
            for m in s.materials:
                if not m.is_known_thickness:
                    a.add(Finding(
                        "MATERIAL_THICKNESS", _escalate(Severity.WARN, tirazh),
                        f"Толщина вне списка инструкции: {m}",
                        f"деталь {i}, «{s.format_raw}». По инструкции допустимо: "
                        + ", ".join(f"{x:g}" for x in MATERIALS[m.kind]) + " мм.",
                        files=[f.name]))
            if s.qty is None:
                a.add(Finding("SPEC_NO_QTY", Severity.WARN,
                              f"У детали {i} не заполнено КОЛ-ВО ({f.name})",
                              files=[f.name]))


def _check_quantities(a: DealAnalysis, tirazh: int | None) -> None:
    """Тираж лент против количеств в производственных файлах.

    Требовать одно общее число во всех файлах нельзя: в одной сделке законно
    соседствуют 90 медалей, 4 награды и 90 лент. Надёжно сверяется только то,
    что вынесено в имя файла лент, — с числами из принта и реза.
    """
    prod: set[int] = set()
    for f in a.deal.prints:
        prod |= {q.n for q in total_from_itogo(a.texts.get(f.name, ""))}
        prod |= {q.n for q in find_quantities(a.texts.get(f.name, ""))}
    for f in a.deal.cuts:
        prod |= {s.qty for s in a.specs.get(f.name, []) if s.qty}
        prod |= {q.n for q in find_quantities(a.texts.get(f.name, ""))}
    if not prod:
        return

    # Лент законно бывает больше тиража: несколько призовых мест, запас, +1 в офис.
    # Тревожно только обратное — лент меньше, чем изделий.
    biggest = max(prod)
    for f in a.deal.ribbons:
        q = f.hints.get("qty")
        if not q:
            continue
        q = int(q)
        if q >= biggest or q in prod:
            continue
        a.add(Finding(
            "RIBBON_QTY_SHORT", _escalate(Severity.WARN, tirazh),
            f"Лент меньше, чем изделий: {q} шт против {biggest}",
            f"в принте и резе найдены количества: {sorted(prod)[:10]}. "
            "Больше лент — норма (места, запас), меньше — повод сверить.",
            files=[f.name], confidence=0.7))


def _check_ribbons(a: DealAnalysis, tirazh: int | None) -> None:
    for f in a.deal.ribbons:
        sides = f.hints.get("sides")
        text = " ".join(a.texts.get(x.name, "") for x in a.deal.prints + a.deal.cuts)
        if sides == 2 and re.search(r"односторон|одна сторона|1\s*сторон", text, re.I):
            a.add(Finding("RIBBON_SIDES", _escalate(Severity.WARN, tirazh),
                          "Ленты двусторонние, но в файлах упомянута односторонняя печать",
                          files=[f.name], confidence=0.7))
        if sides == 1 and re.search(r"двусторон|две стороны|2\s*сторон", text, re.I):
            a.add(Finding("RIBBON_SIDES", _escalate(Severity.WARN, tirazh),
                          "Ленты односторонние, но в файлах упомянута двусторонняя печать",
                          files=[f.name], confidence=0.7))


_CONTENT_LAYERS = ("контур", "цвет", "белый", "лак")


def _check_layers(a: DealAnalysis) -> None:
    """Работает по PDF-экспорту принта: Corel сохраняет слои как OCG."""
    from .vector import read_vector
    for f in a.deal.prints:
        if f.path.suffix.lower() not in {".pdf", ".ai"}:
            continue
        try:
            v = read_vector(f.path)
        except Exception as e:
            a.add(Finding("PDF_UNREADABLE", Severity.NOTE,
                          f"Не удалось разобрать {f.name}: {e}", files=[f.name]))
            continue
        if not v.layers:
            a.add(Finding("NO_LAYERS", Severity.WARN,
                          f"В PDF нет слоёв: {f.name}",
                          "Экспортируйте PDF с сохранением слоёв, иначе принт не проверить.",
                          files=[f.name]))
            continue

        for h in v.group_headers:
            names = [l.name.strip().lower() for l in v.content_layers_of(h)]
            has = {k: any(k in n for n in names) for k in _CONTENT_LAYERS}
            if has["цвет"] and not has["белый"]:
                a.add(Finding("NO_WHITE_UNDER_COLOR", Severity.STOP,
                              f"«{h.material_group}»: есть Цвет, нет Белого",
                              "Без белой подложки цвет на прозрачном/зеркальном материале "
                              "напечатается грязным.", files=[f.name]))
            if not has["контур"]:
                a.add(Finding("NO_CONTOUR", Severity.WARN,
                              f"«{h.material_group}»: нет слоя Контур", files=[f.name]))
            if "зеркал" in (h.material_group or "").lower():
                if not re.search(r"зеркал", " ".join(a.texts.values()), re.I):
                    a.add(Finding("MIRROR_UNCONFIRMED", Severity.WARN,
                                  f"«{h.material_group}»: зеркало заявлено в слое, "
                                  "но нигде в тексте не подтверждено",
                                  files=[f.name], confidence=0.6))

        for l in v.layers:
            if l.empty and not l.is_group_header and l.name.strip().lower() != "инфо":
                a.add(Finding("LAYER_EMPTY", Severity.WARN,
                              f"Слой «{l.name}» пустой ({f.name})",
                              "Слой заведён, но ничего не печатает.", files=[f.name]))
