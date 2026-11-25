"""Ambil data dari BigQuery.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any, cast

# Kolom wajib UNIVERSAL - sama untuk semua brand, tidak peduli sumber
# datanya BigQuery atau Google Sheets. 'segmen' dan 'outlet_id_source' TIDAK
# dihardcode di sini - nama kolomnya ikut 'dimensi_kolom'/'id_kolom' per
# brand (lihat validate_contract).
REQUIRED_COLUMNS_DASAR = ("nama", "nomor_wa", "event_date")


class ContractError(Exception):
    """Hasil query tidak memenuhi kontrak kolom."""


def read_query(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def run_query(
    sql: str,
    *,
    project: str,
    location: str | None,
    as_of: dt.date,
    lookback_days: int,
) -> list[dict[str, Any]]:
    """Jalankan query di BigQuery dengan named parameter.

    Query boleh memakai @as_of dan @lookback_days. Parameter dikirim lewat
    mekanisme BigQuery, bukan string formatting - jadi tidak ada celah injeksi.
    """
    from google.cloud import bigquery

    client = bigquery.Client(project=project, location=location or None)

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("as_of", "DATE", as_of),
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ]
    )

    job = client.query(sql, job_config=job_config)
    rows = [dict(row) for row in job.result()]
    return rows


def validate_contract(
    rows: list[dict[str, Any]],
    *,
    dimensi_kolom: str = "segmen",
    id_kolom: str | None = "outlet_id_source",
    kolom_tambahan: tuple[str, ...] = (),
) -> None:
    """Pastikan query/sheet brand mematuhi kontrak kolom.

    dimensi_kolom  : nama kolom dimensi brand ini ('segmen' buat brand_1,
                      'kode_produk' buat brand_2, dst - lihat
                      brand/<brand>/config.yaml).
    id_kolom       : nama kolom identitas baris ('outlet_id_source' buat
                      brand_1, 'customer_id' dst buat brand lain). Boleh
                      None/kosong kalau brand tidak punya ID terpisah
                      (cuma nama+nomor WA, mis. brand_2).
    kolom_tambahan : kolom ekstra yang wajib ada, biasanya dari
                      'param_fields' (kolom yang dipakai isi parameter
                      template, mis. nama_produk/tanggal_pembelian).
    """
    if not rows:
        return
    wajib = REQUIRED_COLUMNS_DASAR + (dimensi_kolom,)
    if id_kolom:
        wajib += (id_kolom,)
    wajib += tuple(kolom_tambahan)
    kolom = set(rows[0].keys())
    kurang = [c for c in wajib if c not in kolom]
    if kurang:
        raise ContractError(
            "query.sql / sheet tidak memenuhi kontrak kolom.\n"
            f"  Kolom wajib : {', '.join(wajib)}\n"
            f"  Tidak ada   : {', '.join(kurang)}\n"
            f"  Yang ada    : {', '.join(sorted(kolom))}"
        )


def run_diagnostic_query(
    sql: str, *, project: str, location: str | None, hari: int
) -> list[dict[str, Any]]:
    """Jalankan query diagnostik (LEFT JOIN) dengan parameter @hari. Dipakai
    buat semua pengecekan opsional (SKU asing, outlet asing, dst) - murni
    informasi lewat alert email, tidak pernah mempengaruhi baris mana yang
    dikirim WA."""
    from google.cloud import bigquery

    client = bigquery.Client(project=project, location=location or None)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("hari", "INT64", hari)]
    )
    job = client.query(sql, job_config=job_config)
    return [dict(row) for row in job.result()]


def filter_by_outlet_csv(
    rows: list[dict[str, Any]],
    *,
    project: str,
    outlet_table: str,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Samakan perlakuan jalur CSV dengan jalur BigQuery asli.

    query.sql sudah JOIN ke tabel master outlet, jadi outlet yang belum
    terdaftar tidak pernah sampai ke Python. Jalur --from-csv baca file apa
    adanya, filter ini yang gantikan JOIN itu.

    Cuma jalan kalau kolom 'outlet_id_source' ada di CSV-nya. Kalau tidak
    ada, dilewati - supaya file dummy lama tetap bisa dipakai.

    Return: (baris yang lolos, jumlah baris yang dibuang, detail baris yang
    dibuang - outlet_id_source + nomor_wa aslinya, buat alert ke tim bisnis).
    """
    if not rows or "outlet_id_source" not in rows[0]:
        return rows, 0, []

    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    sql = f"SELECT DISTINCT outlet_id_source FROM `{outlet_table}`"
    outlet_valid = {
        r["outlet_id_source"] for r in client.query(sql).result() if r["outlet_id_source"]
    }

    lolos: list[dict[str, Any]] = []
    detail_dibuang: list[dict[str, Any]] = []
    for r in rows:
        if r.get("outlet_id_source") in outlet_valid:
            lolos.append(r)
        else:
            detail_dibuang.append(
                {
                    "outlet_id_source": r.get("outlet_id_source"),
                    "nilai_asli": r.get("nomor_wa"),
                }
            )
    return lolos, len(detail_dibuang), detail_dibuang


def load_from_csv(path: pathlib.Path) -> list[dict[str, Any]]:
    """Jalur alternatif untuk uji coba tanpa koneksi BigQuery.

    Dipakai lewat flag --from-csv. Nama flag-nya tetap 'csv' tapi file
    .xlsx/.xlsm juga diterima - dibaca otomatis sesuai ekstensi, supaya
    file yang diisi manual di Excel (mis. nomor WA) bisa langsung dipakai
    tanpa export ke CSV dulu. Isi file harus memenuhi kontrak kolom yang sama.
    """
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        import pandas as pd

        df = pd.read_excel(path, dtype=str)
        # df.to_dict() mengetik kunci sebagai Hashable (umum di pandas), tapi
        # kolom file Excel di sini SELALU string - aman di-cast.
        return cast(list[dict[str, Any]], df.to_dict(orient="records"))

    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))
