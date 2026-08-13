"""Проверка текста без внешних сервисов.

Три независимых приёма, все офлайн и бесплатные:

1. Орфография по словарю (pyspellchecker, русский + английский).
2. Корпусная аномалия: слово, встретившееся ровно в одной сделке из сотен,
   подозрительно, даже если словарь его пропустил. Так ловятся опечатки
   в названиях турниров и спонсоров, которых нет ни в одном словаре.
3. Сверка текстов между файлами сделки: что есть в резе, но пропало в принте.

Домены вроде «ФГАУ», «АССК», «МДФ», «УФ-печать» в словаре отсутствуют, поэтому
белый список набирается из самого корпуса, а не пишется руками.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-]{2,}")

# Служебные слова макетов — не текст изделия, проверять их бессмысленно.
STOPWORDS = {
    "формат", "детали", "печать", "гравировка", "цвет", "кол", "во", "прочее",
    "итого", "шт", "мм", "лицевая", "обратная", "задняя", "сторона", "стороны",
    "вид", "сбоку", "сверху", "снизу", "лица", "бэка", "выборка", "глубину",
    "скос", "паз", "рез", "фрез", "принт", "лента", "ленты", "деталь", "детали",
    "мдф", "акрил", "металл", "сталь", "фанера", "дерево", "пластик", "уф",
    "зеркале", "зеркало", "лак", "глянец", "контур", "белый", "шайба", "шильда",
    "награда", "награды", "медаль", "медали", "кубок", "кубки", "подложка",
}


@dataclass
class TextIssue:
    word: str
    kind: str          # spelling | rare
    context: str
    where: str
    suggestions: list[str] = field(default_factory=list)


@dataclass
class Corpus:
    """Частоты слов по всем сделкам — основа проверки на аномалию."""
    deal_freq: Counter = field(default_factory=Counter)   # в скольких сделках слово
    total: int = 0

    def add_deal(self, text: str) -> None:
        self.total += 1
        for w in {m.group(0).lower() for m in WORD_RE.finditer(text)}:
            self.deal_freq[w] += 1

    def is_common(self, word: str, min_deals: int = 2) -> bool:
        return self.deal_freq.get(word.lower(), 0) >= min_deals

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            "\n".join(f"{w}\t{n}" for w, n in self.deal_freq.most_common()),
            encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Corpus":
        c = cls()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "\t" in line:
                w, n = line.rsplit("\t", 1)
                c.deal_freq[w] = int(n)
        return c


CYR = "А-Яа-яЁё"
LAT = "A-Za-z"
_MIXED_RE = re.compile(rf"[{CYR}]+[{LAT}][{CYR}{LAT}]*|[{LAT}]+[{CYR}][{CYR}{LAT}]*")

# Латиница, неотличимая на глаз от кириллицы — главный источник таких опечаток.
LOOKALIKE = {"a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
             "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
             "O": "О", "P": "Р", "T": "Т", "X": "Х", "f": "а"}


def find_mixed_script(text: str, where: str = "") -> list[TextIssue]:
    """Слова, где кириллица перемешана с латиницей.

    Правило детерминированное: «333 комплектf медалей» — тут f вместо а.
    Такое не увидит ни один корректор глазами и не поймает словарь.
    """
    out, seen = [], set()
    for m in _MIXED_RE.finditer(text):
        w = m.group(0)
        if len(w) < 4 or w.lower() in seen:
            continue
        seen.add(w.lower())
        fixed = "".join(LOOKALIKE.get(ch, ch) for ch in w)
        lo, hi = max(0, m.start() - 25), min(len(text), m.end() + 25)
        out.append(TextIssue(
            word=w, kind="mixed_script", where=where,
            context=" ".join(text[lo:hi].split()),
            suggestions=[fixed] if fixed != w else []))
    return out


class Speller:
    """Орфография по морфологии (pymorphy3) + белый список из корпуса.

    Простой список слов для русского не годится: он не знает словоформ и ругается
    на «мяча», «передней», «серебре». Морфологический разбор эту проблему снимает.
    """

    def __init__(self, corpus: Corpus | None = None):
        import pymorphy3
        self.morph = pymorphy3.MorphAnalyzer()
        self.corpus = corpus
        try:
            from spellchecker import SpellChecker
            self.en = SpellChecker(language="en", distance=1)
        except Exception:
            self.en = None

    def _known(self, w: str) -> bool:
        low = w.lower()
        if low in STOPWORDS or len(low) < 4:
            return True
        if any(ch.isdigit() for ch in w):
            return True
        if w.isupper():                      # аббревиатуры: ФГАУ, АССК, MVP
            return True
        if self.corpus and self.corpus.is_common(low):
            return True
        if re.fullmatch(rf"[{LAT}\-]+", w):
            return self.en is None or not self.en.unknown([low])
        return self.morph.word_is_known(low.replace("ё", "е"))

    def check(self, text: str, where: str = "") -> list[TextIssue]:
        out: list[TextIssue] = []
        seen: set[str] = set()
        for m in WORD_RE.finditer(text):
            w = m.group(0)
            if w.lower() in seen or self._known(w):
                continue
            seen.add(w.lower())
            lo, hi = max(0, m.start() - 25), min(len(text), m.end() + 25)
            out.append(TextIssue(
                word=w, kind="spelling", where=where,
                context=" ".join(text[lo:hi].split()),
                suggestions=_nearest(w.lower(), self.corpus, 1) if self.corpus else []))
        return out

    def rare_words(self, text: str, where: str = "") -> list[TextIssue]:
        """Слова, которых нет больше нигде в корпусе, — кандидаты в опечатки."""
        if not self.corpus:
            return []
        out, seen = [], set()
        for m in WORD_RE.finditer(text):
            w = m.group(0).lower()
            if w in seen or w in STOPWORDS or len(w) < 5 or any(c.isdigit() for c in w):
                continue
            seen.add(w)
            if self.corpus.deal_freq.get(w, 0) > 1:
                continue
            near = _nearest(w, self.corpus, max_dist=1)
            if not near:
                continue
            lo, hi = max(0, m.start() - 25), min(len(text), m.end() + 25)
            out.append(TextIssue(
                word=m.group(0), kind="rare", where=where,
                context=" ".join(text[lo:hi].split()), suggestions=near))
        return out


def _nearest(word: str, corpus: Corpus, max_dist: int = 1) -> list[str]:
    """Похожее частое слово из корпуса — признак, что это его опечатка."""
    try:
        from rapidfuzz.distance import Levenshtein
    except ImportError:
        return []
    out = []
    for cand, n in corpus.deal_freq.items():
        if n < 3 or abs(len(cand) - len(word)) > max_dist or cand == word:
            continue
        if Levenshtein.distance(word, cand) <= max_dist:
            out.append(cand)
    return out[:3]


def compare_texts(a_text: str, b_text: str, *, min_len: int = 4) -> list[str]:
    """Значимые слова, которые есть в первом тексте и отсутствуют во втором."""
    def words(t: str) -> set[str]:
        return {m.group(0).lower() for m in WORD_RE.finditer(t)
                if len(m.group(0)) >= min_len and m.group(0).lower() not in STOPWORDS}
    return sorted(words(a_text) - words(b_text))
