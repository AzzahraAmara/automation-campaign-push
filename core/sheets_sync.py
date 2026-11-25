"""Baca Google Sheets, upload REPLACE ke BigQuery.

Generik - berlaku sama untuk brand apa pun. Satu-satunya bagian yang beda
per brand adalah: sheet mana, kolom apa, tabel BigQuery mana, dan
transformasi datanya - itu semua ditentukan oleh fungsi sync() masing-
masing brand di brand/<brand>/sync_*.py, bukan di sini.

Menambah brand baru = copy folder brand/brand_1/, ganti nilai konfigurasi
(SPREADSHEET_ID, SHEET_NAME, BQ_TABLE, nama kolom) dan isi _transform()
sesuai kebutuhan brand itu. Mekanisme baca-Sheets/upload-BigQuery di file
ini tidak perlu disentuh sama sekali.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd


def baca_sheet(cred_path: str, spreadsheet_id: str, sheet_name: str) -> pd.DataFrame:
    """Ambil semua baris dari satu tab Google Sheets, baris pertama jadi nama kolom."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        cred_path, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    service = build("sheets", "v4", credentials=creds)
    hasil = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=sheet_name)
        .execute()
    )
    baris = hasil.get("values", [])
    if not baris:
        return pd.DataFrame()

    header, *data = baris
    # Baris di sheet bisa lebih pendek dari header kalau sel kosong di akhir -
    # genapkan dulu supaya pd.DataFrame tidak error.
    data_rapi = [r + [""] * (len(header) - len(r)) for r in data]
    return pd.DataFrame(data_rapi, columns=header)


def upload_replace(df: pd.DataFrame, table: str, project: str) -> None:
    """Replace total isi tabel BigQuery dengan isi dataframe ini."""
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    job = client.load_table_from_dataframe(df, table, job_config=job_config)
    job.result()


def jalankan(
    *,
    spreadsheet_id: str,
    sheet_name: str,
    bq_table: str,
    project: str,
    cred_path: str,
    transform: Callable[[pd.DataFrame], pd.DataFrame],
) -> int:
    """Alur baku: baca sheet -> transform (khusus brand) -> upload REPLACE.

    `transform` adalah satu-satunya bagian yang beda per brand (rename
    kolom, filter baris, mapping nilai) - lempar exception biasa kalau
    datanya tidak valid, nanti ikut tertangkap mekanisme retry+alert di
    main.py seperti kegagalan tahap lain.

    Return: jumlah baris yang diupload.
    """
    df = baca_sheet(cred_path, spreadsheet_id, sheet_name)
    print(f"      Baca sheet '{sheet_name}': {len(df)} baris")

    df_upload = transform(df)

    upload_replace(df_upload, bq_table, project)
    print(f"      Upload selesai: {len(df_upload)} baris ke {bq_table} (REPLACE)")
    return len(df_upload)
