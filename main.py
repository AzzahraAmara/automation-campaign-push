"""Pintu masuk pipeline push campaign.

    python main.py --mode execute                   # semua brand aktif
    python main.py --brand brand_2 --mode rollback # satu brand

Pakai --mode rollback kalau cuma mau uji coba tanpa menulis permanen.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import sys
import time
import uuid

from core import config, guard, queue_writer, recap, runlog, source_bq, template, transform, alert

MAX_PERCOBAAN = 3
JEDA_RETRY_DETIK = 15


def cycle_key(tanggal: dt.date) -> str:
    """Penanda siklus biweekly. Blast tanggal 16 dan 30/31."""
    paruh = "A" if tanggal.day <= 16 else "B"
    return f"{tanggal:%Y%m}-{paruh}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="campaign-push",
        description="Tarik data brand dari BigQuery, olah, dorong ke tabel antrian blast.",
    )
    p.add_argument(
        "--brand",
        help="Pilih satu brand. check-results juga bisa memilih brand nonaktif. "
        "Jika kosong, jalankan semua brand aktif.",
    )
    p.add_argument(
        "--mode",
        choices=["execute", "rollback", "check-results", "backfill-recap"],
        help=(
            "Wajib jika --brand kosong. execute: insert betulan | rollback: insert lalu "
            "dibatalkan, nol jejak | check-results: cek baris yang "
            "GAGAL dikirim Bahasa.Ai, perbarui rekap, kirim alert email | "
            "backfill-recap: tulis rekap dari antrean untuk tanggal --as-of"
        ),
    )
    p.add_argument("--as-of", help="Tanggal acuan YYYY-MM-DD (default: hari ini)")
    p.add_argument("--limit", type=int, help="Batasi jumlah baris, menimpa batch_limit di config")
    p.add_argument(
        "--from-csv",
        help="Lewati BigQuery, baca data dari CSV/XLSX (harus memenuhi kontrak kolom)",
    )
    p.add_argument(
        "--tag",
        help=(
            "Tandai txtSegmentName di tabel antrean dengan teks ini (mis. untuk uji "
            "coba internal). Kolom itu normalnya selalu NULL, jadi aman dipakai "
            "sebagai penanda - TIDAK muncul di isi pesan WA, cuma di baris DB. "
            "Hanya berlaku untuk run ini saja, tidak mengubah config/queue.yaml."
        ),
    )
    p.add_argument("--list-brands", action="store_true", help="Tampilkan brand aktif")
    p.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="Khusus --mode check-results: jendela jam untuk alert kegagalan (default 24)",
    )
    args = p.parse_args(argv)
    if args.brand and args.mode is None:
        args.mode = "execute"
    return args


def _jalankan_sync_brand(brand_cfg: dict) -> None:
    """Jalankan semua brand/<brand>/sync_*.py sebelum tarik data campaign.

    Kalau brand tidak punya file sync_*.py, dilewati saja.
    """
    for path in sorted(brand_cfg["_dir"].glob("sync_*.py")):
        print(f"[0/5] Sync: {path.name}...")
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            continue
        modul = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modul)
        modul.sync()


def _jalankan_check_results(args, brand_cfg, queue_cfg, engine) -> int:
    """Perbarui status rekap dan laporkan kegagalan pengiriman."""
    from sqlalchemy import bindparam, text

    query = queue_cfg.get("check_results_query")
    if not query:
        print(
            "\n[GAGAL] config/queue.yaml: 'check_results_query' belum diisi.",
            file=sys.stderr,
        )
        return 2

    stmt = text(query).bindparams(bindparam("campaign_codes", expanding=True))
    with engine.connect() as conn:
        hasil = conn.execute(
            stmt,
            {
                "campaign_codes": list(brand_cfg["campaign_codes"]),
                "sejak_jam": args.since_hours,
            },
        )
        baris_gagal = [dict(row._mapping) for row in hasil]

    if brand_cfg.get("recap_enabled"):
        try:
            queue_ids = recap.refresh_queue_status(engine, queue_cfg, brand_cfg)
            baris_gagal = [baris for baris in baris_gagal if baris["intQueueId"] in queue_ids]
            print(f"[check-results] Status {len(queue_ids)} queue_id rekap diperiksa.")
        except Exception as exc:
            print(f"[GAGAL] Sinkronisasi status rekap: {exc}", file=sys.stderr)
            subject, body = alert.alert_pipeline_gagal(args.brand, args.mode, "rekap BigQuery", exc)
            alert.send_email_alert(subject, body)
            return 6

    print(
        f"[check-results] {len(baris_gagal)} baris GAGAL ditemukan "
        f"({args.since_hours} jam terakhir, brand={args.brand})"
    )

    if not baris_gagal:
        print("[check-results] Tidak ada kegagalan pengiriman.")
        return 0

    for b in baris_gagal[:10]:
        print(f"  - {b['txtPhoneNo']}: {b['txtParamResponse']}")
    if len(baris_gagal) > 10:
        print(f"  ... dan {len(baris_gagal) - 10} baris lain")

    subject, body = alert.alert_hasil_gagal(args.brand, baris_gagal)
    alert.send_email_alert(subject, body)
    return 1


def _jalankan_pipeline(args: argparse.Namespace) -> int:
    """Isi sebenarnya dari satu kali jalan pipeline. Dipisah dari main()
    supaya main() bisa membungkusnya dengan satu try/except alert."""
    run_id = str(uuid.uuid4())
    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()

    brand_cfg = config.load_brand(args.brand, allow_disabled=args.mode == "check-results")
    if args.limit is not None:
        brand_cfg["batch_limit"] = args.limit

    queue_cfg = config.load_queue_config()
    engine = queue_writer.build_engine(config.queue_db_url())

    # ---------- check-results membaca antrean dan memperbarui tabel rekap --
    if args.mode == "check-results":
        return _jalankan_check_results(args, brand_cfg, queue_cfg, engine)
    if args.mode == "backfill-recap":
        if not brand_cfg.get("recap_enabled") or not args.as_of:
            raise config.ConfigError("backfill-recap memerlukan recap_enabled dan --as-of.")
        jumlah = recap.backfill(engine, queue_cfg, brand_cfg, config.LOGS_DIR, as_of)
        print(f"[rekap] {jumlah} baris disinkronkan untuk tanggal blast {as_of}.")
        return 0

    # ---------- 0. sync data master brand (opsional, sebelum tarik data) --
    if not args.from_csv:
        _jalankan_sync_brand(brand_cfg)

    # ---------- 1. ambil data ----------------------------------------
    dibuang_outlet = 0
    detail_bermasalah: dict[str, list[dict]] = {
        "nomor_invalid": [],
        "outlet_tidak_terdaftar": [],
    }
    if args.from_csv:
        import pathlib

        rows = source_bq.load_from_csv(pathlib.Path(args.from_csv))
        print(f"[1/5] Baca {len(rows)} baris dari CSV: {args.from_csv}")

        # Samakan perlakuan dengan jalur BigQuery asli: query.sql sudah
        # JOIN ke master outlet, jalur CSV ini perlu filter ulang secara
        # eksplisit (hanya aktif kalau CSV-nya punya kolom outlet_id_source).
        if brand_cfg.get("outlet_table"):
            rows, dibuang_outlet, detail_outlet = source_bq.filter_by_outlet_csv(
                rows,
                project=config.env("BQ_PROJECT", required=True),
                outlet_table=brand_cfg["outlet_table"],
            )
            detail_bermasalah["outlet_tidak_terdaftar"] = detail_outlet
            if dibuang_outlet:
                print(
                    f"      [filter outlet] {dibuang_outlet} baris dibuang - "
                    "outlet_id_source belum terdaftar di master outlet."
                )
    else:
        sql = source_bq.read_query(brand_cfg["_query_path"])
        print(f"[1/5] Jalankan query BigQuery untuk brand '{args.brand}' (as_of={as_of})...")
        rows = source_bq.run_query(
            sql,
            project=config.env("BQ_PROJECT", required=True),
            location=config.env("BQ_LOCATION"),
            as_of=as_of,
            lookback_days=int(brand_cfg["lookback_days"]),
        )
        print(f"      {len(rows)} baris diterima.")

    rows = transform.tambah_kode_produk(rows, brand_cfg)
    rows = transform.tambah_periode_garansi(rows, brand_cfg)
    rows = transform.format_tanggal_parameter(rows, brand_cfg)

    source_bq.validate_contract(
        rows,
        dimensi_kolom=brand_cfg.get("dimensi_kolom", "segmen"),
        id_kolom=brand_cfg.get("id_kolom", "outlet_id_source"),
        kolom_tambahan=tuple(brand_cfg.get("param_fields") or ()),
    )

    tanpa_campaign = []
    if brand_cfg.get("product_catalog"):
        campaigns = brand_cfg["campaigns"]
        tanpa_campaign = [row for row in rows if row.get("kode_produk") not in campaigns]
        if tanpa_campaign:
            rows = [row for row in rows if row.get("kode_produk") in campaigns]
            print(f"      [!] {len(tanpa_campaign)} baris produk belum punya campaign.")
            subject, body = alert.alert_produk_tanpa_campaign(args.brand, tanpa_campaign)
            alert.send_email_alert(subject, body)

    # ---------- 1.5 cek SKU asing (diagnostik, tidak pernah menahan WA) --
    # Query terpisah ke tabel transaksi BigQuery asli - jalan terus, tidak
    # peduli sumber data utama (CSV atau BigQuery).
    if brand_cfg.get("_sku_check_path"):
        print("[1.5/5] Cek SKU yang belum terdaftar di master produk...")
        sql_cek = source_bq.read_query(brand_cfg["_sku_check_path"])
        sku_asing = source_bq.run_diagnostic_query(
            sql_cek,
            project=config.env("BQ_PROJECT", required=True),
            location=config.env("BQ_LOCATION"),
            hari=int(brand_cfg["sku_check_days"]),
        )
        if sku_asing:
            print(f"      [!] {len(sku_asing)} SKU belum terdaftar - alert dikirim.")
            subject, body = alert.alert_sku_asing(
                args.brand, sku_asing, int(brand_cfg["sku_check_days"])
            )
            alert.send_email_alert(subject, body)
        else:
            print("      Semua SKU sudah terdaftar.")

    # ---------- 1.55 cek outlet asing (diagnostik, tidak pernah menahan WA) --
    # Sama seperti SKU check - query terpisah, jalan terus tanpa peduli
    # sumber data utama.
    if brand_cfg.get("_outlet_check_path"):
        print("[1.55/5] Cek outlet yang belum terdaftar di master outlet...")
        sql_cek_outlet = source_bq.read_query(brand_cfg["_outlet_check_path"])
        outlet_asing = source_bq.run_diagnostic_query(
            sql_cek_outlet,
            project=config.env("BQ_PROJECT", required=True),
            location=config.env("BQ_LOCATION"),
            hari=int(brand_cfg["sku_check_days"]),
        )
        if outlet_asing:
            print(
                f"      [!] {len(outlet_asing)} outlet belum terdaftar "
                "(ini disengaja, bukan masalah - tidak ada email dikirim)."
            )
        else:
            print("      Semua outlet sudah terdaftar.")

    # ---------- 1.6 contoh bulanan (opsional, tanggal 1 saja) ----------
    if not args.from_csv:
        sebelum_contoh = len(rows)
        rows = transform.tambah_contoh_bulanan(rows, brand_cfg, as_of)
        if len(rows) > sebelum_contoh:
            print("[1.6/5] Tambah 1 baris contoh bulanan.")

    # ---------- 2. transform -----------------------------------------
    print("[2/5] Normalisasi nomor, validasi, dedup per konsumen...")
    df, stats, detail_nomor_invalid = transform.apply(rows, brand_cfg)
    detail_bermasalah["nomor_invalid"] = detail_nomor_invalid
    stats["dibuang_outlet_tidak_terdaftar"] = dibuang_outlet
    if tanpa_campaign:
        stats["baris_dari_sumber"] += len(tanpa_campaign)
        stats["dibuang_produk_tanpa_campaign"] = len(tanpa_campaign)

    # ---------- alert data bermasalah (nomor invalid) ------------------
    # Outlet belum terdaftar SENGAJA tidak dialert - itu kondisi normal,
    # bukan masalah data yang perlu ditindak. detail_bermasalah tetap
    # menyimpannya (kalau nanti perlu lihat lagi), cuma tidak ikut
    # menghitung pemicu email.
    total_bermasalah = len(detail_bermasalah["nomor_invalid"])
    if total_bermasalah:
        print(f"      [!] {total_bermasalah} baris bermasalah - alert dikirim.")
        subject, body = alert.alert_data_bermasalah(
            args.brand, {"nomor_invalid": detail_bermasalah["nomor_invalid"]}
        )
        alert.send_email_alert(subject, body)

    # ---------- 3. guard ---------------------------------------------
    print("[3/5] Saring cooldown, batas volume...")
    df, stats_guard, warnings = guard.apply(df, brand_cfg, queue_cfg, engine)
    stats.update(stats_guard)

    # ---------- 4. template ------------------------------------------
    print("[4/5] Tentukan template per baris...")
    df, stats_tpl = template.apply(df, brand_cfg)
    stats.update(stats_tpl)

    runtime = {
        "campaign_name": brand_cfg["campaign_name"],
        "sender": brand_cfg["sender"],
        "workspace": brand_cfg.get("workspace"),
        "brand": brand_cfg["brand"],
        "run_id": run_id,
        "cycle_key": cycle_key(as_of),
        "as_of": as_of,
    }
    # campaign_code TIDAK ada di runtime: nilainya beda per segmen dan sudah
    # ditulis per baris oleh template.apply().
    for kunci, nilai in runtime.items():
        if kunci not in df.columns:
            df[kunci] = nilai

    # --tag: override txtSegmentName HANYA untuk run ini (in-memory), bukan
    # mengubah config/queue.yaml. Dibuat salinan dict-nya supaya aman.
    if args.tag and queue_cfg.get("columns"):
        runtime["_tag_segmen"] = args.tag
        queue_cfg = dict(queue_cfg)
        queue_cfg["columns"] = dict(queue_cfg["columns"])
        queue_cfg["columns"]["txtSegmentName"] = "_tag_segmen"

    entry = {
        "run_id": run_id,
        "brand": args.brand,
        "mode": args.mode,
        "as_of": as_of,
        "cycle_key": runtime["cycle_key"],
        "template_mode": brand_cfg["template_mode"],
        "campaigns": brand_cfg["campaign_codes"],
        "stats": stats,
        "final_rows": len(df),
        "warnings": warnings,
        "waktu": dt.datetime.now(),
        "from_csv": bool(args.from_csv),
    }

    # ---------- 5. tulis ke tabel antrean ------------------------------
    logs_dir = config.ensure_logs_dir()

    if not queue_cfg.get("columns"):
        print(
            "\n[GAGAL] config/queue.yaml belum punya pemetaan kolom. "
            "Isi dulu setelah struktur tabel antrean yang diotorisasi dikonfigurasi.",
            file=sys.stderr,
        )
        return 2
    print(f"[5/5] Mode {args.mode} - menulis {len(df)} baris...")
    rows_out = queue_writer.build_rows(df, queue_cfg, runtime)
    entry["write"] = queue_writer.write(
        rows_out,
        queue_cfg,
        engine,
        mode=args.mode,
        schema_env=config.env("QUEUE_DB_SCHEMA"),
        capture_ids=bool(brand_cfg.get("recap_enabled")),
    )

    try:
        runlog.catat(logs_dir, entry)
        runlog.cetak_ringkasan(entry, warnings)
        if brand_cfg.get("recap_enabled") and entry["write"]["committed"]:
            jumlah = recap.sync_run(
                engine, queue_cfg, brand_cfg, entry, source=recap.source_lookup(df)
            )
            print(f"[rekap] {jumlah} baris disinkronkan ke BigQuery.")
    except Exception as exc:
        if not entry["write"]["committed"]:
            raise
        print(f"[GAGAL] Penulisan ke tabel antrean sudah commit. Tahap setelah commit gagal: {exc}", file=sys.stderr)
        subject, body = alert.alert_pipeline_gagal(args.brand, args.mode, "setelah commit", exc)
        alert.send_email_alert(subject, body)
        return 6
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config.load_env()

    if args.list_brands:
        for b in config.list_brands():
            print(b)
        return 0

    if args.brand:
        return _jalankan_dengan_retry(args)

    if not args.mode:
        print("\n[GAGAL] Sebutkan --mode untuk menjalankan semua brand aktif.", file=sys.stderr)
        return 2
    if args.from_csv or args.mode == "backfill-recap":
        print("\n[GAGAL] --from-csv dan backfill-recap memerlukan --brand.", file=sys.stderr)
        return 2

    brands = config.list_brands()
    if not brands:
        print("Tidak ada brand aktif di config/brands.yaml.")
        return 0

    if args.as_of is None:
        args.as_of = dt.date.today().isoformat()
    status = 0
    for brand in brands:
        args.brand = brand
        print(f"Menjalankan {brand} ({args.mode})", flush=True)
        try:
            status = max(status, _jalankan_dengan_retry(args))
        except Exception as exc:
            print(f"[GAGAL] {brand}: {exc}", file=sys.stderr)
            status = max(status, 1)
    return status


def _jalankan_dengan_retry(args: argparse.Namespace) -> int:
    tahap_exc = {
        config.ConfigError: "konfigurasi",
        source_bq.ContractError: "kontrak kolom",
        template.TemplateError: "template",
        queue_writer.QueueMappingError: "pemetaan antrian",
    }

    error_terakhir: Exception | None = None
    tahap_terakhir = "tidak diketahui"
    for percobaan in range(1, MAX_PERCOBAAN + 1):
        try:
            return _jalankan_pipeline(args)
        except tuple(tahap_exc.keys()) as exc:
            error_terakhir = exc
            tahap_terakhir = tahap_exc[type(exc)]
        except Exception as exc:  # noqa: BLE001 - mau tangkap apa adanya buat retry
            error_terakhir = exc
            tahap_terakhir = "tidak diketahui"

        print(
            f"[retry] Percobaan {percobaan}/{MAX_PERCOBAAN} gagal "
            f"({tahap_terakhir}): {error_terakhir}",
            file=sys.stderr,
        )
        if percobaan < MAX_PERCOBAAN:
            print(f"[retry] Tunggu {JEDA_RETRY_DETIK} detik, lalu ulangi dari awal...")
            time.sleep(JEDA_RETRY_DETIK)

    # Semua percobaan gagal - baru alert & lempar error asli.
    assert error_terakhir is not None
    subject, body = alert.alert_pipeline_gagal(args.brand, args.mode, tahap_terakhir, error_terakhir)
    alert.send_email_alert(subject, body)
    raise error_terakhir


if __name__ == "__main__":
    try:
        sys.exit(main())
    except config.ConfigError as exc:
        print(f"\n[KONFIGURASI] {exc}", file=sys.stderr)
        sys.exit(2)
    except source_bq.ContractError as exc:
        print(f"\n[KONTRAK KOLOM] {exc}", file=sys.stderr)
        sys.exit(3)
    except template.TemplateError as exc:
        print(f"\n[TEMPLATE] {exc}", file=sys.stderr)
        sys.exit(4)
    except queue_writer.QueueMappingError as exc:
        print(f"\n[PEMETAAN ANTRIAN] {exc}", file=sys.stderr)
        sys.exit(5)
