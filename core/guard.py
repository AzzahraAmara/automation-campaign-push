"""Pengaman sebelum data masuk antrian.

Dua lapis:
  1. cooldown     - konsumen yang baru saja dikirimi pesan
  2. batas jumlah - batas volume per sekali jalan
"""

from __future__ import annotations

from typing import Any, cast

import pandas as pd

from . import transform


def _muat_dari_sql(engine, query: str, params: dict[str, Any]) -> set[str]:
    """Jalankan query pengecualian di DB tujuan.

    Query wajib mengembalikan satu kolom berisi nomor WA. Parameter bernilai
    list otomatis dijadikan expanding bindparam, supaya bisa dipakai dengan
    IN (:nama_param) - dibutuhkan karena satu brand punya beberapa campaign.
    """
    from sqlalchemy import bindparam, text

    stmt = text(query)
    melebar = [k for k, v in params.items() if isinstance(v, (list, tuple, set))]
    if melebar:
        stmt = stmt.bindparams(*[bindparam(k, expanding=True) for k in melebar])

    hasil: set[str] = set()
    with engine.connect() as conn:
        for baris in conn.execute(stmt, params):
            nomor = transform.normalize_phone(baris[0])
            if nomor:
                hasil.add(nomor)
    return hasil


def apply(
    df: pd.DataFrame,
    brand_cfg: dict[str, Any],
    queue_cfg: dict[str, Any],
    engine=None,
) -> tuple[pd.DataFrame, dict[str, int], list[str]]:
    """Kembalikan (df bersih, statistik, daftar peringatan)."""
    stats: dict[str, int] = {}
    warnings: list[str] = []

    if df.empty:
        return df, stats, warnings

    # ---------- 1. cooldown ------------------------------------------
    cooldown_days = int(brand_cfg.get("cooldown_days") or 0)
    cooldown_query = queue_cfg.get("cooldown_query")

    if cooldown_days <= 0:
        warnings.append("cooldown_days = 0, pengecekan kirim-ulang dilewati.")
    elif not cooldown_query:
        warnings.append(
            f"COOLDOWN {cooldown_days} HARI TIDAK DICEK. 'cooldown_query' di "
            "config/queue.yaml masih kosong - butuh pemetaan tabel antrean. "
            "Konsumen yang baru saja diblast bisa kena kirim lagi."
        )
    elif engine is None:
        warnings.append(
            f"COOLDOWN {cooldown_days} HARI TIDAK DICEK (tidak ada koneksi DB)."
        )
    else:
        baru_dikirim = _muat_dari_sql(
            engine,
            cooldown_query,
            {
                "campaign_codes": list(brand_cfg["campaign_codes"]),
                "cooldown_days": cooldown_days,
            },
        )
        sebelum = len(df)
        kolom_nomor = cast(pd.Series, df["nomor_wa"])
        df = cast(pd.DataFrame, df[~kolom_nomor.isin(list(baru_dikirim))]).copy()
        stats["dibuang_cooldown"] = sebelum - len(df)

    # ---------- 3. batas volume --------------------------------------
    batas = int(brand_cfg.get("batch_limit") or 0)
    if batas > 0 and len(df) > batas:
        stats["dipotong_batch_limit"] = len(df) - batas
        warnings.append(
            f"Hasil {len(df)} baris melebihi batch_limit {batas}. "
            f"Hanya {batas} baris pertama yang diproses."
        )
        df = df.head(batas).copy()

    stats["lolos_guard"] = len(df)
    return df.reset_index(drop=True), stats, warnings
