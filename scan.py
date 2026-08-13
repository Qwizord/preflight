#!/usr/bin/env python3
"""Preflight — проверка папки сделки.

    python scan.py "/путь/к/папке сделки"
    python scan.py --all "/путь/к/месяцу"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from preflight.checks import Severity, analyze
from preflight.deal import Role, load_deal

MARK = {Severity.STOP: "🛑", Severity.WARN: "⚠️ ", Severity.NOTE: "ℹ️ "}


def report(folder: Path, *, verbose: bool = False) -> int:
    deal = load_deal(folder)
    a = analyze(deal)

    print(f"\n{'━' * 76}")
    print(f"  {deal.title}" + (f"   ·   дедлайн {deal.deadline}" if deal.deadline else ""))
    print(f"{'━' * 76}")

    roles = [Role.FINAL, Role.WORK, Role.PRINT, Role.CUT, Role.RIBBON]
    have = {r: deal.of(r) for r in roles}
    print("  Комплект: " + "  ".join(
        f"{'✓' if have[r] else '✗'} {r.value}" for r in roles))

    if verbose:
        for f in deal.of(Role.PRINT, Role.CUT):
            specs = a.specs.get(f.name, [])
            if specs:
                print(f"\n  Ведомость из «{f.name}»:")
                for i, s in enumerate(specs, 1):
                    print(f"     {i}. {s}")

    if not a.findings:
        print("\n  Замечаний нет.\n")
        return 0

    print()
    for f in a.findings:
        conf = "" if f.confidence >= 0.95 else f"  (уверенность {f.confidence:.0%})"
        print(f"  {MARK[f.severity]} {f.title}{conf}")
        if f.detail:
            for line in _wrap(f.detail, 70):
                print(f"       {line}")
        if f.files:
            print(f"       ↳ {', '.join(f.files)}")
        print()

    n_stop = sum(1 for f in a.findings if f.severity == Severity.STOP)
    n_warn = sum(1 for f in a.findings if f.severity == Severity.WARN)
    print(f"  Итог: {n_stop} стоп, {n_warn} внимание, "
          f"{len(a.findings) - n_stop - n_warn} к сведению\n")
    return n_stop


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--all", action="store_true", help="обойти все подпапки")
    ap.add_argument("-v", "--verbose", action="store_true", help="показать ведомость")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"Нет такого пути: {args.path}", file=sys.stderr)
        return 2

    folders = (sorted(p for p in args.path.iterdir() if p.is_dir())
               if args.all else [args.path])
    return sum(report(f, verbose=args.verbose) for f in folders)


if __name__ == "__main__":
    raise SystemExit(main())
