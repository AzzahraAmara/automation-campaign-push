"""Sync master outlet dari Google Sheets ke BigQuery - khusus brand_1.

Bagian umum (baca sheet, upload replace) ada di core/sheets_sync.py. File
ini cuma berisi: sheet mana, kolom apa, tabel mana, dan aturan filter
yang spesifik brand_1. Dipanggil otomatis oleh main.py tiap pipeline jalan.

Menambah brand baru: copy folder brand/brand_1/ ini, ganti nilai di bawah
sesuai kebutuhan brand baru, sesuaikan _transform() kalau aturan filternya
beda. Tidak perlu menyentuh core/sheets_sync.py maupun main.py sama sekali.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core import config, sheets_sync  # noqa: E402

# =========================================================================
# ISI DULU setelah sheet-nya dibuat dan di-share ke service account
# =========================================================================
SPREADSHEET_ID = "YOUR_SPREADSHEET_ID"
SHEET_NAME = "YOUR_SHEET_TAB"
BQ_TABLE = "your-project.your_dataset.master_entity"

KOLOM_UPLOAD = {
    "Entity ID": "entity_id",
    "Entity Name": "entity_name",
    "Location Code": "location_code",
    "Location": "location",
}


def _transform(df: pd.DataFrame) -> pd.DataFrame:
    """Keep source entities with a non-empty identifier."""
    kurang = [k for k in KOLOM_UPLOAD if k not in df.columns]
    if kurang:
        raise ValueError(f"Kolom tidak ditemukan di sheet '{SHEET_NAME}': {kurang}")

    df_upload = cast(pd.DataFrame, df[list(KOLOM_UPLOAD.keys())]).rename(columns=KOLOM_UPLOAD)

    sebelum = len(df_upload)
    kolom_id = cast(pd.Series, df_upload["entity_id"])
    df_upload = df_upload[kolom_id.str.strip() != ""].copy()
    print(f"      Entities with an identifier: {len(df_upload)} of {sebelum}")

    duplikat = cast(pd.Series, df_upload["entity_id"]).duplicated().sum()
    if duplikat:
        print(f"      [PERINGATAN] {duplikat} entity_id duplicates - uploaded as-is.")

    return cast(pd.DataFrame, df_upload)


def sync() -> int:
    """Dipanggil main.py tiap run. Kalau SPREADSHEET_ID belum diisi,
    dilewati saja (bukan error) - supaya pipeline utama tetap jalan
    sebelum setup Google Sheets-nya selesai."""
    if not SPREADSHEET_ID:
        print("      [lewati] SPREADSHEET_ID belum diisi di sync_master_outlet.py")
        return 0

    config.load_env()
    return sheets_sync.jalankan(
        spreadsheet_id=SPREADSHEET_ID,
        sheet_name=SHEET_NAME,
        bq_table=BQ_TABLE,
        project=config.env("BQ_PROJECT", required=True),
        cred_path=config.env("GOOGLE_SHEETS_CREDENTIALS", required=True),
        transform=_transform,
    )


if __name__ == "__main__":
    sys.exit(sync())
