"""Email alert untuk campaign-push.
"""

from __future__ import annotations

import os
import smtplib
import traceback
from collections import Counter
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _clean(teks) -> str:
    if not teks:
        return ""
    return str(teks).replace("\u2060", "").replace("\ufeff", "")


def _config() -> tuple[str | None, str | None, list[str]]:
    """Dibaca ulang tiap dipanggil, bukan sekali saat import - supaya .env
    yang baru diubah langsung kepakai tanpa restart proses."""
    sender = os.getenv("EMAIL_SENDER")
    password = os.getenv("EMAIL_PASSWORD")
    penerima = [
        _clean(x.strip())
        for x in os.getenv("EMAIL_RECEIVER", "").split(",")
        if x.strip()
    ]
    return sender, password, penerima


def email_siap_dipakai() -> bool:
    sender, password, penerima = _config()
    return bool(sender and password and penerima)


def send_email_alert(subject: str, body: str) -> bool:
    """Kirim satu email alert. Return True kalau terkirim, False kalau tidak
    (config kosong atau gagal kirim) - TIDAK melempar exception, supaya
    kegagalan kirim alert tidak menutupi error aslinya yang mau dilaporkan.
    """
    sender, password, penerima = _config()

    if not (sender and password and penerima):
        print(
            "[alert] EMAIL_SENDER/EMAIL_PASSWORD/EMAIL_RECEIVER belum lengkap "
            "di .env - alert email DILEWATI, cuma tercetak di terminal."
        )
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = ", ".join(penerima)
        msg["Subject"] = _clean(subject)
        msg.attach(MIMEText(_clean(body), "plain"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, penerima, msg.as_string())

        print(f"[alert] Email terkirim ke {penerima}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(
            f"[alert] GAGAL login SMTP (cek EMAIL_PASSWORD - harus App "
            f"Password 16 digit Gmail, bukan password akun biasa): {e}"
        )
    except (smtplib.SMTPException, OSError) as e:
        print(f"[alert] GAGAL kirim (SMTP/jaringan - cek port 587 ke smtp.gmail.com): {e}")
    except Exception as e:  # noqa: BLE001 - alert tidak boleh ikut menjatuhkan proses utama
        print(f"[alert] GAGAL kirim (tidak terduga): {e}")
    return False


# =========================================================================
# Pola error txtParamResponse yang sudah dikenal, dengan saran tindakannya.
# Dicocokkan lewat substring (case-insensitive). Tambah baris baru kalau
# ketemu pola error lain di produksi.
# =========================================================================
_AKSI_DIKENAL: list[tuple[str, str]] = [
    (
        "rate limit",
        "Terlalu banyak pesan dikirim dalam waktu singkat ke Bahasa.Ai. "
        "Tindakan: kurangi jumlah baris per run (--limit), atau pecah jadi "
        "beberapa batch dengan jeda waktu. Kalau rutin kena ini, hubungi "
        "tim untuk cek/naikkan kuota workspace.",
    ),
    (
        "template",
        "Kemungkinan template belum/tidak lagi disetujui Meta, atau nama "
        "template salah ketik. Tindakan: cek status template. ",
    ),
    (
        "unauthorized",
        "Masalah autentikasi/API key ke Bahasa.Ai. Tindakan: hubungi admin "
        " - kemungkinan API key kedaluwarsa/dicabut.",
    ),
    (
        "workspace",
        "Masalah konfigurasi workspace di sisi Bahasa.Ai. Tindakan: cek "
        "dashboard Bahasa.Ai, pastikan workspace aktif.",
    ),
    (
        "invalid",
        "Bahasa.Ai menolak nomor/parameter meski lolos validasi pipeline "
        "ini. Tindakan: cek nomor satu per satu - kemungkinan tidak "
        "terdaftar WhatsApp atau format yang diterima Bahasa.Ai beda.",
    ),
]
_AKSI_TAK_DIKENAL = (
    "Pola error ini belum pernah tercatat di sini. Tindakan: cek dashboard "
    "Bahasa.Ai dan workspace sesuai brand untuk detailnya, atau hubungi tim "
    "Bahasa.Ai dengan menyertakan isi txtParamResponse di atas apa adanya."
)


def saran_tindakan(pesan_error: str) -> str:
    pesan_lower = (pesan_error or "").lower()
    for kata_kunci, saran in _AKSI_DIKENAL:
        if kata_kunci in pesan_lower:
            return saran
    return _AKSI_TAK_DIKENAL


# =========================================================================
# Pembangun subject/body siap-kirim untuk 2 jenis alert campaign-push.
# =========================================================================


def alert_pipeline_gagal(brand: str, mode: str, tahap: str, exc: Exception) -> tuple[str, str]:
    """Pipeline sendiri crash (ConfigError, TemplateError, dll) - belum
    sempat menulis apa pun ke tabel antrean, atau baru di tengah jalan."""
    subject = f"!!! [GAGAL] Campaign Push {brand} - {type(exc).__name__} !!!"
    body = f"""Waktu       : {datetime.now()}
Brand       : {brand}
Mode        : {mode}
Tahap gagal : {tahap}
Jenis error : {type(exc).__name__}
Pesan       : {exc}

Tindakan    : {_tindakan_pipeline(tahap)}

TRACEBACK:
{traceback.format_exc()}
"""
    return subject, body


def _tindakan_pipeline(tahap: str) -> str:
    peta = {
        "konfigurasi": (
            "Cek .env dan brand/<brand>/config.yaml - ada field wajib yang "
            "kosong/salah. Pesan error di atas menyebutkan field mana."
        ),
        "kontrak kolom": (
            "query.sql (atau file --from-csv) tidak menghasilkan kolom "
            "wajib (outlet_id_source, nama, nomor_wa, segmen, event_date). Cek "
            "query/file sumber datanya."
        ),
        "template": (
            "Ada segmen tanpa template/campaign terdaftar di "
            "brand/<brand>/config.yaml. Lengkapi dulu sebelum run ulang."
        ),
        "pemetaan antrian": (
            "config/queue.yaml belum lengkap (table/columns kosong), atau "
            "koneksi DB gagal. Cek QUEUE_DB_URL di .env dan coba lagi."
        ),
    }
    return peta.get(
        tahap,
        "Lihat traceback di bawah untuk lokasi persis errornya di kode.",
    )


def alert_hasil_gagal(brand: str, baris_gagal: list[dict]) -> tuple[str, str]:
    """Ringkasan baris di tabel antrean yang GAGAL dikirim provider pesan
    (bolSuccess=0, txtParamResponse berisi pesan error) - dipanggil dari
    mode --check-results, BUKAN otomatis setelah execute (enginenya butuh
    waktu memproses, tidak langsung tahu hasilnya detik itu juga)."""
    subject = (
        f"!!! [GAGAL] {len(baris_gagal)} pesan Campaign Push "
        f"{brand} ditolak Bahasa.Ai !!!"
    )

    # Kelompokkan per pesan error supaya tidak 1 baris 1 paragraf kalau
    # ternyata semuanya gagal karena 1 penyebab yang sama (umum terjadi,
    # mis. satu kali kena rate limit borongan).
    per_pesan: dict[str, list[dict]] = {}
    for b in baris_gagal:
        pesan = str(b.get("txtParamResponse") or "(kosong)")
        per_pesan.setdefault(pesan, []).append(b)

    blok = []
    for pesan, daftar in per_pesan.items():
        nomor = ", ".join(str(d.get("txtPhoneNo")) for d in daftar[:10])
        lebih = f" (+{len(daftar) - 10} lagi)" if len(daftar) > 10 else ""
        blok.append(
            f"--- {len(daftar)} baris ---\n"
            f"Pesan error : {pesan}\n"
            f"Nomor       : {nomor}{lebih}\n"
            f"Tindakan    : {saran_tindakan(pesan)}\n"
        )

    body = (
        f"Waktu  : {datetime.now()}\n"
        f"Brand  : {brand}\n"
        f"Total baris gagal: {len(baris_gagal)}\n\n"
        + "\n".join(blok)
    )
    return subject, body


def alert_produk_tanpa_campaign(brand: str, baris: list[dict]) -> tuple[str, str]:
    """Ringkas produk yang dilewati karena belum punya mapping campaign."""
    per_produk: Counter[tuple[str, str]] = Counter()
    for row in baris:
        kode = (row.get("kode_produk") or "").upper()
        nama = " ".join(str(row.get("nama_produk") or "").split())
        if nama.lower() in {"nan", "<na>"}:
            nama = ""
        per_produk[kode, nama] += 1

    detail = "\n".join(
        f"- {kode or '(kode kosong)'} | {nama or '(nama kosong)'} | {jumlah} baris"
        for (kode, nama), jumlah in sorted(per_produk.items())
    )
    subject = f"[ALERT] {len(per_produk)} produk belum punya campaign - {brand}"
    body = (
        f"Brand {brand} melewati {len(baris)} baris karena produknya belum punya campaign.\n\n"
        f"{detail}\n\n"
        f"Tambahkan mapping produknya di brand/{brand}/config.yaml."
    )
    return subject, body


def alert_sku_asing(brand: str, baris: list[dict], hari: int) -> tuple[str, str]:
    """SKU yang kemungkinan variant/duplikat kode dari produk yang SUDAH
    terdaftar di master produk (nama sama, kode beda) - bukan semua SKU
    yang tidak ketemu. Murni info, dikirim tiap kali pipeline jalan kalau
    ada temuan baru. Tidak ada baris WA yang tertahan gara-gara ini."""
    subject = f"[INFO] {len(baris)} kemungkinan duplikat kode SKU - {brand}"

    def _potong(teks, panjang=40):
        teks = str(teks or "(nama tidak ditemukan di source payload)")
        return teks if len(teks) <= panjang else teks[: panjang - 3] + "..."

    header = (
        f"  {'SKU Asing':<12}{'Nama Produk':<43}{'Mirip Kode':<12}"
        f"{'Jumlah':>8}  {'Pertama':<12}{'Terakhir'}"
    )
    garis = "  " + "-" * (len(header) - 2)
    daftar_baris = [header, garis]
    for b in baris:
        daftar_baris.append(
            f"  {b['sku_tidak_dikenal']:<12}{_potong(b.get('nama_produk')):<43}"
            f"{b['sku_mirip_di_master']:<12}{b['jumlah_transaksi']:>8}  "
            f"{str(b['pertama_muncul']):<12}{b['terakhir_muncul']}"
        )
    daftar = "\n".join(daftar_baris)

    body = (
        f"Waktu  : {datetime.now()}\n"
        f"Brand  : {brand}\n"
        f"Jendela: {hari} hari terakhir\n\n"
        f"SKU berikut punya NAMA PRODUK SAMA PERSIS dengan produk yang sudah\n"
        f"terdaftar di Master Produk, tapi KODE SKU-nya beda - kemungkinan ini\n"
        f"variant/duplikat kode dari produk yang sama, bukan produk baru:\n\n"
        f"{daftar}\n\n"
        f"Tindakan: cek manual apakah 'SKU Asing' dan 'Mirip Kode' memang produk\n"
        f"yang sama. Kalau ya, tambahkan 'SKU Asing' sebagai kode tambahan di\n"
        f"Google Sheets Master Produk, lalu jalankan brand/brand_1/sync_master_product.py\n"
        f"(atau tunggu sync otomatis di run berikutnya) supaya ikut terpetakan.\n"
        f"Transaksi dengan SKU ini TIDAK ikut campaign sampai ditambahkan.\n\n"
        f"Catatan: SKU lain yang tidak ketemu di Master Produk DAN namanya juga\n"
        f"tidak mirip apa pun di sini TIDAK dialert - itu wajar masuk kategori\n"
        f"produk baru/asing (\"Other\"), bukan sesuatu yang perlu ditindak."
    )
    return subject, body


def alert_outlet_asing(brand: str, baris: list[dict], hari: int) -> tuple[str, str]:
    """Outlet yang muncul di transaksi tapi belum terdaftar di master
    outlet - murni info, dikirim tiap kali pipeline jalan kalau ada
    temuan baru. Outlet begini memang gagal masuk campaign (INNER JOIN)
    sampai ditambahkan ke master outlet."""
    subject = f"[INFO] {len(baris)} outlet belum terdaftar - {brand}"

    header = (
        f"  {'Outlet ID':<16}{'Nama':<30}{'Nomor WA':<16}"
        f"{'Jumlah':>8}  {'Pertama':<12}{'Terakhir'}"
    )
    garis = "  " + "-" * (len(header) - 2)
    daftar_baris = [header, garis]
    for b in baris:
        daftar_baris.append(
            f"  {str(b['outlet_id_source']):<16}{str(b.get('nama') or '-'):<30}"
            f"{str(b.get('nomor_wa') or '-'):<16}{b['jumlah_transaksi']:>8}  "
            f"{str(b['pertama_muncul']):<12}{b['terakhir_muncul']}"
        )
    daftar = "\n".join(daftar_baris)

    body = (
        f"Waktu  : {datetime.now()}\n"
        f"Brand  : {brand}\n"
        f"Jendela: {hari} hari terakhir\n\n"
        f"Outlet berikut muncul di transaksi tapi belum terdaftar di Master\n"
        f"Outlet. Transaksi dari outlet ini TIDAK ikut campaign sampai\n"
        f"ditambahkan:\n\n"
        f"{daftar}\n\n"
        f"Tindakan: kalau outlet ini memang valid, tambahkan ke Google Sheets\n"
        f"Master Outlet, lalu jalankan brand/brand_1/sync_master_outlet.py (atau\n"
        f"tunggu sync otomatis di run berikutnya) supaya ikut terpetakan."
    )
    return subject, body


def alert_data_bermasalah(brand: str, detail: dict[str, list[dict]]) -> tuple[str, str]:
    """Baris yang gugur dari campaign karena masalah DATA (nomor WA tidak
    valid, outlet belum terdaftar) - bukan bug pipeline. Murni info buat
    tim bisnis, biar bisa dilacak & diperbaiki di sumber datanya. Dipanggil
    cuma kalau ADA baris yang gugur di run ini."""
    label = {
        "nomor_invalid": "Nomor WA tidak valid (bukan format nomor - mis. keisi nama orang)",
        "outlet_tidak_terdaftar": "Outlet belum terdaftar di master outlet",
    }
    total = sum(len(v) for v in detail.values())
    subject = f"[INFO] {total} baris gugur karena masalah data - {brand}"

    blok = []
    for kunci, daftar in detail.items():
        if not daftar:
            continue
        blok.append(f"--- {label.get(kunci, kunci)} ({len(daftar)} baris) ---")
        for b in daftar[:30]:
            label_kolom = b.get("id_kolom", "outlet_id_source")
            nilai_id = b.get("id_value", b.get("outlet_id_source"))
            blok.append(
                f"  {label_kolom}={nilai_id!r}  "
                f"nilai_asli={b.get('nilai_asli')!r}"
            )
        if len(daftar) > 30:
            blok.append(f"  ... dan {len(daftar) - 30} baris lain")
        blok.append("")

    body = (
        f"Waktu  : {datetime.now()}\n"
        f"Brand  : {brand}\n\n"
        f"Baris berikut gugur dari campaign karena masalah data, BUKAN karena\n"
        f"bug di pipeline. Cek manual di sumber datanya (ID di atas bisa\n"
        f"dipakai buat lacak baris aslinya):\n\n"
        + "\n".join(blok)
    )
    return subject, body
