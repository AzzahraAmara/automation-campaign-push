"""Sync master produk dari Google Sheets ke BigQuery - khusus brand_1.

Bagian umum (baca sheet, upload replace) ada di core/sheets_sync.py. File
ini cuma berisi: sheet mana, kolom apa, tabel mana, dan pemetaan kategori yang
spesifik brand_1. Dipanggil otomatis oleh main.py tiap pipeline jalan.

Menambah brand baru: copy folder brand/brand_1/ ini, ganti nilai di bawah
sesuai kebutuhan brand baru, sesuaikan _transform() kalau aturannya beda.
Tidak perlu menyentuh core/sheets_sync.py maupun main.py sama sekali.
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
BQ_TABLE = "your-project.your_dataset.master_product"

# Contoh pemetaan kategori sumber -> dimensi generik.
GROUP_TO_SEGMENT = {
    "GROUP_A": "segment_01",
    "GROUP_B": "segment_02",
    "GROUP_C1": "segment_03",
    "GROUP_C2": "segment_03",
    "GROUP_C3": "segment_03",
}

KOLOM_SUMBER = [
    "ProductCode",
    "ProductName",
    "Weight",
    "Organization",
    "GroupCode",
    "Brand",
    "SubBrand",
    "Category",
    "Barcode",
    "Price",
]


def _transform(df: pd.DataFrame) -> pd.DataFrame:
    """Map source categories to generic segments and discard unknown values.

    Product names are retained for matching unregistered codes."""
    kurang = [k for k in KOLOM_SUMBER if k not in df.columns]
    if kurang:
        raise ValueError(f"Kolom tidak ditemukan di sheet '{SHEET_NAME}': {kurang}")

    df_upload = cast(pd.DataFrame, df[KOLOM_SUMBER]).copy()
    kolom_code = cast(pd.Series, df_upload["ProductCode"])
    df_upload["product_code"] = kolom_code.str.strip().str.upper()
    kolom_group = cast(pd.Series, df_upload["GroupCode"])
    df_upload["segment"] = kolom_group.str.strip().str.upper().map(GROUP_TO_SEGMENT)

    sebelum = len(df_upload)
    kolom_segment = cast(pd.Series, df_upload["segment"])
    tanpa_segment = cast(pd.Series, df_upload[kolom_segment.isna()]["GroupCode"]).value_counts()
    df_upload = df_upload[kolom_segment.notna()].copy()
    print(f"      Rows with recognized groups: {len(df_upload)} of {sebelum}")
    if not tanpa_segment.empty:
        print(f"      Kategori sumber yang tidak dipetakan: {dict(tanpa_segment)}")

    duplikat = cast(pd.Series, df_upload["product_code"]).duplicated().sum()
    if duplikat:
        print(f"      [PERINGATAN] {duplikat} product_code duplikat - tetap diupload apa adanya.")

    df_upload = cast(pd.DataFrame, df_upload)
    df_upload = df_upload.rename(
        columns={
            "ProductName": "product_name",
            "GroupCode": "group_code",
            "Brand": "brand",
            "SubBrand": "sub_brand",
            "Category": "category",
        }
    )
    kolom_akhir = [
        "product_code", "product_name", "group_code", "brand",
        "sub_brand", "category", "segment",
    ]
    return cast(pd.DataFrame, df_upload[kolom_akhir])


def sync() -> int:
    """Dipanggil main.py tiap run. Kalau SPREADSHEET_ID belum diisi,
    dilewati saja (bukan error) - supaya pipeline utama tetap jalan
    sebelum setup Google Sheets-nya selesai."""
    if not SPREADSHEET_ID:
        print("      [lewati] SPREADSHEET_ID belum diisi di sync_master_product.py")
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
