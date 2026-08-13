"""Чтение CorelDRAW .cdr (версии 17-22) без CorelDRAW.

Файл .cdr — это ZIP-архив. Геометрия внутри лежит в закрытом бинарном формате
(content/*.dat) и не парсится, но всё остальное доступно как XML:
метаданные, весь текст, цветовые флаги, палитра и превью-рендер.
"""

from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class CdrColor:
    space: str          # CMYK / RGB / Gray / Spot
    name: str | None
    tints: tuple[float, ...]

    @property
    def is_rgb(self) -> bool:
        return self.space.upper() == "RGB"


@dataclass
class CdrDoc:
    path: Path
    author: str | None = None
    created: datetime | None = None
    modified: datetime | None = None
    color_model: str | None = None
    has_rgb: bool = False
    has_cmyk: bool = False
    has_gray: bool = False
    profiles: dict[str, str] = field(default_factory=dict)
    palette: list[CdrColor] = field(default_factory=list)
    # (break, text): break — разрыв ПЕРЕД фрагментом: para / line / none
    runs: list[tuple[str, str]] = field(default_factory=list)
    page_captions: list[str] = field(default_factory=list)
    preview_png: bytes | None = None
    fonts: list[str] = field(default_factory=list)

    @property
    def text_runs(self) -> list[str]:
        return [t for _, t in self.runs]

    @property
    def text(self) -> str:
        """Весь текст документа.

        Corel режет надпись на фрагменты по смене начертания, поэтому «Металл»
        приезжает как ['М', 'еталл 2мм']. Склеивать всё через пробел нельзя —
        разрыв описан атрибутом break: none означает продолжение той же строки.
        """
        out: list[str] = []
        for i, (br, t) in enumerate(self.runs):
            if i and br != "none":
                out.append("\n")
            out.append(t)
        s = re.sub(r"[ \t]+", " ", "".join(out))
        # Внутри одной строки Corel слепляет соседние надписи: «стороныВыборка»,
        # «бэкаГРАВИРОВКА». В русском заглавная внутри слова невозможна,
        # поэтому такой стык — всегда граница двух надписей.
        s = re.sub(r"(?<=[а-яё])(?=[А-ЯЁ])", "\n", s)
        return s.strip()

    @property
    def is_backup(self) -> bool:
        n = self.path.name.lower()
        return n.startswith("резервная_копия_") or n.startswith("backup_of_")


def _xml(z: zipfile.ZipFile, name: str) -> str | None:
    try:
        return z.read(name).decode("utf-8", errors="replace")
    except KeyError:
        return None


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def read_cdr(path: str | Path) -> CdrDoc:
    path = Path(path)
    doc = CdrDoc(path=path)

    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())

        # --- метаданные: автор и даты -------------------------------------
        if (meta := _xml(z, "META-INF/metadata.xml")):
            if m := re.search(r"<rdf:li>([^<]+)</rdf:li>", meta):
                doc.author = html.unescape(m.group(1)).strip() or None
            doc.created = _dt(_tag(meta, "xmp:CreateDate"))
            doc.modified = _dt(_tag(meta, "xmp:ModifyDate"))

        # --- цветовые флаги: основа проверки «принт только CMYK» ----------
        if (color := _xml(z, "color/color.xml")):
            doc.color_model = _tag(color, "ColorModel")
            doc.has_rgb = _tag(color, "HasRgbObjects") == "true"
            doc.has_cmyk = _tag(color, "HasCmykObjects") == "true"
            doc.has_gray = _tag(color, "HasGrayscaleObjects") == "true"
            for pid, prof in re.findall(
                r'<ColorProfile id="([^"]+)">([^<]+)</ColorProfile>', color
            ):
                doc.profiles[pid] = prof

        # --- палитра документа --------------------------------------------
        if (pal := _xml(z, "color/docPalette.xml")):
            for m in re.finditer(r"<color\b([^/>]*)/?>", pal):
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
                tints = tuple(
                    float(x) for x in attrs.get("tints", "").split(",") if x.strip()
                )
                doc.palette.append(
                    CdrColor(
                        space=attrs.get("cs", "?"),
                        name=html.unescape(attrs["name"]) if "name" in attrs else None,
                        tints=tints,
                    )
                )

        # --- весь текст документа -----------------------------------------
        if (ti := _xml(z, "META-INF/textinfo.xml")):
            for attrs, body in re.findall(
                r"<TextRun\b([^>]*)>(.*?)</TextRun>", ti, re.S
            ):
                m = re.search(r'break="([^"]*)"', attrs)
                doc.runs.append((m.group(1) if m else "none", html.unescape(body)))

        # --- подписи страниц ----------------------------------------------
        if (cont := _xml(z, "META-INF/container.xml")):
            doc.page_captions = [
                html.unescape(c) for c in re.findall(r'crl:caption="([^"]*)"', cont)
            ]

        # --- превью --------------------------------------------------------
        for cand in ("previews/page1.png", "previews/thumbnail.png"):
            if cand in names:
                doc.preview_png = z.read(cand)
                break

        # --- шрифты (fontTable.dat бинарный, тянем читаемые строки) --------
        if "font/fontTable.dat" in names:
            doc.fonts = _strings_from_font_table(z.read("font/fontTable.dat"))

    return doc


def _tag(xml: str, tag: str) -> str | None:
    m = re.search(rf"<{re.escape(tag)}>([^<]*)</{re.escape(tag)}>", xml)
    return m.group(1).strip() if m else None


_FONT_RE = re.compile(rb"[\x20-\x7e]{4,}")


def _strings_from_font_table(blob: bytes) -> list[str]:
    """Имена шрифтов лежат в бинарнике; вытаскиваем и utf-16le, и ascii."""
    found: list[str] = []
    try:
        for s in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", blob):
            v = s.decode("utf-16le", errors="ignore").strip()
            if v:
                found.append(v)
    except Exception:
        pass
    for s in _FONT_RE.findall(blob):
        v = s.decode("ascii", errors="ignore").strip()
        if v:
            found.append(v)
    # чистим мусор и дубли, сохраняя порядок
    seen, out = set(), []
    for f in found:
        if len(f) < 3 or f.lower() in seen:
            continue
        if not re.search(r"[A-Za-zА-Яа-я]", f):
            continue
        seen.add(f.lower())
        out.append(f)
    return out
