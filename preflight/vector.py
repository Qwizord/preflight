"""Чтение PDF и .ai со слоями (OCG).

Corel при экспорте в PDF сохраняет слои как Optional Content Groups, поэтому
каждый слой (Контур / Цвет / Белый / Лак) можно погасить и отрендерить отдельно.
Illustrator .ai в подавляющем большинстве случаев PDF-совместим и читается тем же путём.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

PT_PER_MM = 72.0 / 25.4


def pt2mm(v: float) -> float:
    return v / PT_PER_MM


@dataclass
class Layer:
    number: int
    name: str
    on: bool
    is_group_header: bool = False   # «--- Металл 2мм» — заголовок группы, контента нет
    empty: bool = False             # ничего не рисует
    bbox_mm: tuple[float, float, float, float] | None = None
    ink_ratio: float = 0.0          # доля закрашенных пикселей страницы

    @property
    def material_group(self) -> str | None:
        """Для заголовка «--- Акрил 10мм (Зеркало)» вернёт «Акрил 10мм (Зеркало)»."""
        m = re.match(r"^-{2,}\s*(.+)$", self.name.strip())
        return m.group(1).strip() if m else None


@dataclass
class PageInfo:
    index: int
    width_mm: float
    height_mm: float
    n_drawings: int
    n_images: int
    text: str


@dataclass
class VectorDoc:
    path: Path
    pages: list[PageInfo] = field(default_factory=list)
    layers: list[Layer] = field(default_factory=list)
    fonts: list[str] = field(default_factory=list)
    # операторы задания цвета в контент-стриме
    uses_rgb_ops: bool = False
    uses_cmyk_ops: bool = False
    uses_gray_ops: bool = False
    stroke_widths_mm: list[float] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(p.text for p in self.pages)

    @property
    def group_headers(self) -> list[Layer]:
        return [l for l in self.layers if l.is_group_header]

    def content_layers_of(self, header: Layer) -> list[Layer]:
        """Контентные слои, идущие после заголовка группы и до следующего заголовка."""
        out: list[Layer] = []
        seen = False
        for l in self.layers:
            if l is header:
                seen = True
                continue
            if not seen:
                continue
            if l.is_group_header:
                break
            out.append(l)
        return out


def read_vector(path: str | Path, *, layer_dpi: int = 36) -> VectorDoc:
    path = Path(path)
    doc = VectorDoc(path=path)
    d = pymupdf.open(path)

    for i in range(d.page_count):
        pg = d[i]
        doc.pages.append(
            PageInfo(
                index=i,
                width_mm=pt2mm(pg.rect.width),
                height_mm=pt2mm(pg.rect.height),
                n_drawings=len(pg.get_drawings()),
                n_images=len(pg.get_images()),
                text=pg.get_text(),
            )
        )

    doc.fonts = sorted({f[3] for i in range(d.page_count) for f in d[i].get_fonts()})
    _scan_color_operators(d, doc)
    _collect_stroke_widths(d, doc)

    cfgs = d.layer_ui_configs()
    if cfgs:
        _measure_layers(d, doc, cfgs, layer_dpi)

    d.close()
    return doc


def _measure_layers(d, doc: VectorDoc, cfgs, dpi: int) -> None:
    """Гасим каждый слой по очереди и смотрим, что пропало со страницы.

    Так получаем bbox и «вес» слоя, не полагаясь на принадлежность путей к OCG.
    """
    pg = d[0]
    full = pg.get_pixmap(dpi=dpi)
    base = _mask(full)
    page_px = full.width * full.height

    for c in cfgs:
        layer = Layer(
            number=c["number"],
            name=c["text"],
            on=bool(c.get("on", 1)),
            is_group_header=bool(re.match(r"^-{2,}\s*\S", c["text"].strip())),
        )
        d.set_layer_ui_config(c["number"], action=2)      # OFF
        without = _mask(pg.get_pixmap(dpi=dpi))
        d.set_layer_ui_config(c["number"], action=1)      # ON

        diff = [i for i, (a, b) in enumerate(zip(base, without)) if a != b]
        if not diff:
            layer.empty = True
        else:
            layer.ink_ratio = len(diff) / page_px
            xs = [i % full.width for i in diff]
            ys = [i // full.width for i in diff]
            scale = 25.4 / dpi
            layer.bbox_mm = (
                min(xs) * scale, min(ys) * scale,
                (max(xs) + 1) * scale, (max(ys) + 1) * scale,
            )
        doc.layers.append(layer)


def _mask(pix) -> bytes:
    """Грубая маска «есть чернила»: 1 байт на пиксель, 0 — фон."""
    n = pix.n
    s = pix.samples
    return bytes(
        0 if all(s[i + k] > 245 for k in range(min(3, n))) else 1
        for i in range(0, len(s), n)
    )


_COLOR_OPS = re.compile(rb"(?<![A-Za-z])(rg|RG|k|K|g|G)(?![A-Za-z])")


def _raw_streams(d) -> bytes:
    out = bytearray()
    for xref in range(1, d.xref_length()):
        try:
            if d.xref_is_stream(xref):
                out += d.xref_stream(xref)
        except Exception:
            continue
        if len(out) > 6_000_000:      # хватит для определения
            break
    return bytes(out)


def _scan_color_operators(d, doc: VectorDoc) -> None:
    """DeviceRGB (rg/RG) против DeviceCMYK (k/K) прямо в контент-стриме."""
    try:
        blob = _raw_streams(d)
    except Exception:
        return
    ops = set(_COLOR_OPS.findall(blob))
    doc.uses_rgb_ops = b"rg" in ops or b"RG" in ops
    doc.uses_cmyk_ops = b"k" in ops or b"K" in ops
    doc.uses_gray_ops = b"g" in ops or b"G" in ops


def _collect_stroke_widths(d, doc: VectorDoc) -> None:
    ws: set[float] = set()
    for i in range(min(d.page_count, 5)):
        for dr in d[i].get_drawings():
            w = dr.get("width")
            if w:
                ws.add(round(pt2mm(w), 3))
    doc.stroke_widths_mm = sorted(ws)
