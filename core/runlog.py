"""Pencatatan tiap eksekusi: ringkasan dicetak ke terminal + 1 baris JSONL
riwayat. Tidak ada file .log per run - cukup terminal dan runlog.jsonl.

logs/runlog.jsonl - riwayat semua run, 1 baris JSON per run.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

RUNLOG_FILE = "runlog.jsonl"


def catat(logs_dir: pathlib.Path, entry: dict[str, Any]) -> pathlib.Path:
    """Riwayat ringkas machine-readable, 1 baris JSON per run."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / RUNLOG_FILE
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return path


def _format_ringkasan(entry: dict[str, Any], warnings: list[str]) -> str:
    baris: list[str] = []
    garis = "=" * 62
    baris.append(garis)
    baris.append(f"  RINGKASAN  |  brand={entry['brand']}  mode={entry['mode']}")
    baris.append(garis)
    baris.append(f"  run_id     : {entry['run_id']}")
    baris.append(f"  as_of      : {entry['as_of']}")
    baris.append(f"  cycle      : {entry.get('cycle_key')}")
    baris.append(f"  tpl mode   : {entry.get('template_mode')}")
    for nama in entry.get("campaigns") or []:
        baris.append(f"  campaign   : {nama}")
    baris.append("-" * 62)
    for kunci, nilai in entry.get("stats", {}).items():
        baris.append(f"  {kunci:<32}: {nilai:>8}")
    baris.append("-" * 62)
    baris.append(f"  {'SIAP KIRIM':<32}: {entry.get('final_rows', 0):>8}")

    tulis = entry.get("write") or {}
    if tulis:
        if tulis.get("ditulis", 0) == 0:
            status = "TIDAK ADA DATA"
        elif tulis.get("committed"):
            status = "DI-COMMIT"
        else:
            status = "DIBATALKAN (rollback)"
        baris.append(
            f"  {'ditulis ke ' + str(tulis.get('target', '-')):<32}: "
            f"{tulis.get('ditulis', 0):>8}  [{status}]"
        )

    if warnings:
        baris.append("-" * 62)
        for w in warnings:
            baris.append(f"  [!] {w}")

    baris.append(garis)
    return "\n".join(baris)


def cetak_ringkasan(entry: dict[str, Any], warnings: list[str]) -> None:
    print("\n" + _format_ringkasan(entry, warnings) + "\n")
