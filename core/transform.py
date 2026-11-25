"""Pembersihan & perapian data. Berlaku sama untuk semua brand.

Nomor HP yang tidak valid dibuang, bukan dikirim apa adanya.
Hasilnya 1 baris per konsumen, bukan per transaksi - supaya tidak kirim
pesan berkali-kali ke orang yang sama.
"""

from __future__ import annotations

import calendar
import re
from typing import Any

import pandas as pd

# Nilai yang sering muncul di kolom nama tapi bukan nama sungguhan.
_NAMA_PLACEHOLDER = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "null",
    "none",
    "nan",
    "nama",
    "tidak ada",
    "tidak diketahui",
    "unknown",
    "customer",
    "konsumen",
    "pelanggan",
    "bapak",
    "ibu",
    "pak",
    "bu",
}

_DIGIT_RE = re.compile(r"\D")
_HURUF_RE = re.compile(r"[A-Za-z]")


def _normalisasi_kunci_produk(raw: Any) -> str | None:
    """Normalisasi nama produk untuk pencocokan ke katalog brand.

    Nama aslinya tidak diubah; fungsi ini hanya dipakai sebagai kunci lookup.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    teks = " ".join(str(raw).split()).strip()
    return teks.lower() if teks else None


def tambah_kode_produk(
    rows: list[dict[str, Any]], brand_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Gunakan kode sumber atau cari kode dari nama produk di katalog."""
    katalog = brand_cfg.get("product_catalog") or {}
    if not katalog:
        return rows

    lookup = {
        kunci: kode
        for nama, kode in katalog.items()
        if (kunci := _normalisasi_kunci_produk(nama)) is not None
    }
    hasil: list[dict[str, Any]] = []
    for baris in rows:
        salinan = dict(baris)
        nama = _normalisasi_kunci_produk(salinan.get("nama_produk"))
        kode = _normalisasi_kunci_produk(salinan.get("kode_produk"))
        if not kode and nama:
            kode = lookup.get(nama)
        salinan["kode_produk"] = kode
        if kode and not nama:
            salinan["nama_produk"] = kode.upper()
        hasil.append(salinan)
    return hasil


_BULAN_INDONESIA = (
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
)


def _format_tanggal_indonesia(raw: Any) -> str | None:
    """Ubah nilai tanggal/timestamp menjadi ``16 November 2026``."""
    if raw is None:
        return None
    tanggal = pd.to_datetime(str(raw), errors="coerce")
    if pd.isna(tanggal):
        return None
    return f"{tanggal.day} {_BULAN_INDONESIA[tanggal.month - 1]} {tanggal.year}"


def format_tanggal_parameter(
    rows: list[dict[str, Any]], brand_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Format field tanggal konfiguratif sebelum dibuat menjadi parameter WA."""
    fields = brand_cfg.get("date_display_fields") or []
    if not fields:
        return rows

    hasil: list[dict[str, Any]] = []
    for baris in rows:
        salinan = dict(baris)
        for field in fields:
            if field in salinan:
                salinan[field] = _format_tanggal_indonesia(salinan[field])
        hasil.append(salinan)
    return hasil


def tambah_periode_garansi(
    rows: list[dict[str, Any]], brand_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Hitung tanggal akhir garansi jika brand mengaturnya di config.

    Untuk brand yang mengaktifkan warranty_period, tanggal akhir dapat
    dihitung dari tanggal input dan diselaraskan ke akhir bulan.
    """
    aturan = brand_cfg.get("warranty_period")
    if not aturan:
        return rows

    source_date = str(aturan.get("source_date", "event_date"))

    hasil: list[dict[str, Any]] = []
    for baris in rows:
        salinan = dict(baris)
        nilai_tanggal = salinan.get(source_date)
        # Konversi ke str sekaligus mempersempit tipe untuk overload Pandas.
        # Nilai None ditangani terpisah agar tidak diparse sebagai "None".
        tanggal = (
            pd.to_datetime(str(nilai_tanggal), errors="coerce")
            if nilai_tanggal is not None
            else pd.NaT
        )
        if pd.isna(tanggal):
            salinan["periode_garansi"] = None
        else:
            salinan["periode_garansi"] = _format_tanggal_indonesia(
                tanggal_akhir_garansi(tanggal, aturan)
            )
        hasil.append(salinan)
    return hasil


def tanggal_akhir_garansi(tanggal, aturan: dict[str, Any]):
    years = int(aturan.get("years", 0))
    if years <= 0:
        raise ValueError("warranty_period.years harus lebih dari 0.")
    akhir = pd.Timestamp(tanggal) + pd.DateOffset(years=years)
    if aturan.get("end_of_month"):
        akhir = akhir.replace(day=calendar.monthrange(akhir.year, akhir.month)[1])
    return akhir.date()


def normalize_phone(raw: Any) -> str | None:
    """Rapikan nomor WA Indonesia ke format 62. Kembalikan None kalau tidak valid.

    Formatnya 62xxxxxxxxx, sesuai format nomor WhatsApp yang diterima sistem antrean.

    Nomor yang tidak valid dibuang, bukan dikirim apa adanya.
    Contoh yang dibuang: "Pak wahyu" atau "0812-xxx (rumah)".
    """
    if raw is None:
        return None
    if isinstance(raw, float) and pd.isna(raw):
        return None

    teks = str(raw).strip()
    if not teks:
        return None

    # kalau satu sel memuat
    # dua nomor (dipisah / , ; atau "atau"), ambil nomor PERTAMA saja.
    # Nomor kedua diabaikan sepenuhnya - tidak digabung, tidak dipakai.
    teks = re.split(r"[/,;]| atau ", teks, maxsplit=1)[0]

    digits = _DIGIT_RE.sub("", teks)
    if not digits:
        return None

    # Kadang dua nomor nempel langsung tanpa pemisah sama sekali. Nomor
    # tunggal paling panjang (sebelum 0/62 dirapikan) cuma 14 digit - kalau
    # lebih dari itu, cari awal nomor baru ("08" atau "628") di tengah
    # string, ambil cuma sampai sebelum titik itu.
    if len(digits) > 14:
        cocok = re.search(r"08|628", digits[9:])
        if cocok:
            digits = digits[: 9 + cocok.start()]

    # Buang 0 berlebih di depan (misal hasil ketik "0062...")
    if digits.startswith("00"):
        digits = digits.lstrip("0")

    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits

    # Seluler Indonesia selalu 628xx.
    if not digits.startswith("628"):
        return None
    if not (11 <= len(digits) <= 15):
        return None

    return digits


def clean_name(raw: Any) -> str | None:
    """Kembalikan nama yang layak dipakai di sapaan, atau None."""
    if raw is None:
        return None
    if isinstance(raw, float) and pd.isna(raw):
        return None

    teks = " ".join(str(raw).split()).strip(" .,-")
    if not teks:
        return None
    if teks.lower() in _NAMA_PLACEHOLDER:
        return None
    # Harus mengandung huruf. Menolak "12345", "0812...", "-- 99 --".
    if not _HURUF_RE.search(teks):
        return None
    # Nama satu huruf tetap dianggap valid (mis. "A"), tidak dibuang cuma
    # karena pendek.
    return teks.title()


def tambah_contoh_bulanan(
    rows: list[dict[str, Any]], brand_cfg: dict[str, Any], as_of
) -> list[dict[str, Any]]:
    """Tambah 1 baris contoh bulanan kalau brand config punya 'monthly_sample'
    dan hari ini tanggal 1. Nilai dimensinya bergantian tiap bulan, mengikuti
    urutan di 'urutan_segmen' (siklus sepanjang daftar itu)."""
    cfg = brand_cfg.get("monthly_sample")
    if not cfg or not cfg.get("nomor_wa") or as_of.day != 1:
        return rows

    dim_col = str(brand_cfg.get("dimensi_kolom", "segmen"))
    id_col = brand_cfg.get("id_kolom", "outlet_id_source")  # boleh None/kosong
    urutan = cfg["urutan_segmen"]
    nilai_dimensi = urutan[(as_of.month - 1) % len(urutan)]

    baris_baru: dict[str, Any] = {
        "nama": cfg.get("nama") or None,
        "nomor_wa": cfg["nomor_wa"],
        dim_col: nilai_dimensi,
        "event_date": as_of,
        "total_qty": 1,
    }
    if id_col:
        baris_baru[id_col] = "MONTHLY_SAMPLE"
    return rows + [baris_baru]


def apply(
    rows: list[dict[str, Any]], brand_cfg: dict[str, Any]
) -> tuple[pd.DataFrame, dict[str, int], list[dict[str, Any]]]:
    """Normalisasi -> validasi -> dedup per konsumen berdasarkan nilai dimensi
    pemenang (nama kolom dimensinya ikut 'dimensi_kolom' di config brand,
    default 'segmen').

    Return ketiga: detail baris yang gugur gara-gara nomor WA tidak valid
    (outlet_id_source + nilai aslinya) - dipakai buat alert ke tim bisnis,
    biar bisa dilacak & diperbaiki di sumber datanya.
    """
    stats: dict[str, int] = {}
    detail_nomor_invalid: list[dict[str, Any]] = []
    dim_col = str(brand_cfg.get("dimensi_kolom", "segmen"))
    id_col = brand_cfg.get("id_kolom", "outlet_id_source")  # boleh None/kosong

    df = pd.DataFrame(rows)
    stats["baris_dari_sumber"] = len(df)

    if df.empty:
        return df, stats, detail_nomor_invalid

    # --- normalisasi -------------------------------------------------
    nomor_asli = df["nomor_wa"].copy()
    df["nomor_wa"] = df["nomor_wa"].apply(normalize_phone)
    df["nama"] = df["nama"].apply(clean_name)
    df[dim_col] = df[dim_col].astype(str).str.strip().str.lower()
    df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce")

    # --- buang nomor tidak valid -------------------------------------
    # Kalau brand tidak punya id_kolom (mis. brand_2 - cuma nama+nomor,
    # tidak ada ID outlet/customer terpisah), pakai nama sebagai pengenal
    # cadangan di alert, supaya tetap bisa dilacak manual.
    sebelum = len(df)
    gagal = df[df["nomor_wa"].isna()]
    for i in gagal.index:
        detail_nomor_invalid.append(
            {
                "id_kolom": id_col if id_col else "nama",
                "id_value": df.at[i, id_col] if id_col else df.at[i, "nama"],
                "nilai_asli": nomor_asli.at[i],
            }
        )
    df = df[df["nomor_wa"].notna()].copy()
    stats["dibuang_nomor_invalid"] = sebelum - len(df)

    # --- buang nilai dimensi yang tidak dikenal -----------------------
    prioritas: list[str] = [str(s).strip().lower() for s in brand_cfg["segment_priority"]]
    sebelum = len(df)
    df = df[df[dim_col].isin(prioritas)].copy()
    stats["dibuang_segmen_tak_dikenal"] = sebelum - len(df)

    if df.empty:
        return df, stats, detail_nomor_invalid

    # --- dedup per konsumen --------------------------------------------
    # Default: satu nomor WA = satu pesan (subset dedup = ["nomor_wa"]).
    # Kalau brand butuh lain (mis. brand_2 - 1 nomor boleh dapat BANYAK
    # pesan asal beda produk, baru dianggap duplikat kalau produk+nomor
    # SAMA PERSIS), set 'dedup_subset' di config brand, mis.:
    #   dedup_subset: [nomor_wa, kode_produk]
    # Kalau ada >1 baris TERSISA setelah subset ini (mis. brand_1: nomor sama,
    # segmen beda), 'segment_selection' yang menentukan baris pemenangnya.
    dedup_subset: list[str] = list(brand_cfg.get("dedup_subset") or ["nomor_wa"])

    rank = {nilai: i for i, nilai in enumerate(prioritas)}
    df["_rank_prioritas"] = df[dim_col].map(rank)

    mode_pilih = brand_cfg.get("segment_selection", "fixed")
    if mode_pilih == "quantity":
        if "total_qty" not in df.columns:
            raise ValueError(
                "segment_selection: quantity butuh kolom 'total_qty' dari sumber "
                "data (query.sql / CSV), tapi kolom itu tidak ada."
            )
        # Lebih besar quantity = lebih diutamakan
        df["_qty_urut"] = -pd.to_numeric(df["total_qty"], errors="coerce").fillna(0)
        kolom_urut = dedup_subset + ["_qty_urut", "_rank_prioritas", "event_date"]
        arah_naik = [True] * len(dedup_subset) + [True, True, False]
    elif mode_pilih == "fixed":
        kolom_urut = dedup_subset + ["_rank_prioritas", "event_date"]
        arah_naik = [True] * len(dedup_subset) + [True, False]
    else:
        raise ValueError(
            f"segment_selection tidak dikenal: '{mode_pilih}'. "
            "Pakai 'quantity' atau 'fixed'."
        )

    sebelum = len(df)
    df = (
        df.sort_values(by=kolom_urut, ascending=arah_naik, kind="mergesort")
        .drop_duplicates(subset=dedup_subset, keep="first")
        .drop(columns=[c for c in ("_rank_prioritas", "_qty_urut") if c in df.columns])
        .reset_index(drop=True)
    )
    stats["dibuang_duplikat_konsumen"] = sebelum - len(df)
    stats["setelah_transform"] = len(df)

    return df, stats, detail_nomor_invalid
