"""Sinkronisasi rekap BigQuery dari baris yang sudah masuk tabel antrean."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from . import config, queue_writer, runlog, source_bq, transform

_WIB = dt.timezone(dt.timedelta(hours=7))
_COMMON_FIELDS = {
    "run_id": "STRING",
    "nama": "STRING",
    "nomor_wa": "STRING",
    "nama_campaign": "STRING",
    "nama_template": "STRING",
    "nama_workspace": "STRING",
    "queue_id": "INT64",
    "tag": "STRING",
    "params": "STRING",
    "insertdate_to_trwaqueue": "DATETIME",
    "updatedate_queue": "DATETIME",
    "update_date": "DATETIME",
}
_FIELDS = {
    "brand_2": {
        **_COMMON_FIELDS,
        "nama_produk": "STRING",
        "kode_produk": "STRING",
        "tanggal_input": "DATE",
        "periode_garansi": "DATE",
        "status_queue": "BOOL",
    },
    "brand_1": {
        **_COMMON_FIELDS,
        "outlet_id_source": "STRING",
        "segmen": "STRING",
        "receipt_date": "DATE",
        "followup_date": "DATE",
        "status_queue": "STRING",
    },
}
_QUEUE_FIELDS = (
    "intQueueId, txtPhoneNo, TxtCampaignName, txtTemplateName, txtWorkspaceName, "
    "txtSegmentName, txtParams, bolSuccess, txtParamResponse, dtInserted, dtUpdated"
)


def _datetime_wib(value: dt.datetime | None) -> dt.datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(_WIB).replace(tzinfo=None)


def _queue_status(row, *, brand_1: bool) -> dict[str, Any]:
    success = row["bolSuccess"]
    if brand_1:
        status = "Success" if success else "Pending"
        if not success and row["txtParamResponse"] is not None:
            status = "Failed"
    else:
        status = bool(success) if success is not None else None
    return {
        "status_queue": status,
        "updatedate_queue": _datetime_wib(row["dtUpdated"]),
        "update_date": dt.datetime.now(_WIB).replace(tzinfo=None),
    }


def source_lookup(df) -> dict[tuple[str, str], dict[str, Any]]:
    """Simpan metadata sumber dari baris yang sudah lolos deduplikasi."""
    columns = ["nomor_wa", "campaign_code", "event_date", "nama", "outlet_id_source", "segmen"]
    source = df[[name for name in columns if name in df]].copy()
    source["event_date"] = source["event_date"].dt.date
    rows = source.astype(object).where(source.notna(), None).to_dict(orient="records")
    return {(row["nomor_wa"], row["campaign_code"]): row for row in rows}


def _reload_brand_1_source(brand_cfg, as_of: dt.date):
    """Ambil kembali identitas outlet untuk pemulihan rekap brand_1."""
    rows = source_bq.run_query(
        source_bq.read_query(brand_cfg["_query_path"]),
        project=config.env("BQ_PROJECT", required=True),
        location=config.env("BQ_LOCATION"),
        as_of=as_of,
        lookback_days=int(brand_cfg["lookback_days"]),
    )
    rows = transform.tambah_contoh_bulanan(rows, brand_cfg, as_of)
    df, _, _ = transform.apply(rows, brand_cfg)
    if df.empty:
        return {}
    df["campaign_code"] = df["segmen"].map(brand_cfg["campaigns"])
    return source_lookup(df)


def _read_queue(engine, queue_cfg, entry, *, next_run=None) -> list[dict[str, Any]]:
    from sqlalchemy import bindparam, text

    target = queue_writer._target_name(queue_cfg, config.env("QUEUE_DB_SCHEMA"))
    write = entry["write"]
    ids = write.get("queue_ids")
    with engine.connect() as conn:
        if ids:
            stmt = text(
                f"SELECT {_QUEUE_FIELDS} FROM {target} WHERE intQueueId IN :ids"
            ).bindparams(bindparam("ids", expanding=True))
            rows = []
            for i in range(0, len(ids), 500):
                rows.extend(conn.execute(stmt, {"ids": ids[i : i + 500]}).mappings())
        else:
            waktu = dt.datetime.fromisoformat(str(entry["waktu"]))
            toleransi = dt.timedelta(milliseconds=2)
            akhir = waktu + dt.timedelta(minutes=5)
            if next_run:
                akhir = min(akhir, next_run - toleransi)
            stmt = text(
                f"SELECT {_QUEUE_FIELDS} FROM {target} "
                "WHERE TxtCampaignName IN :campaigns "
                "AND dtInserted >= CAST(:awal AS datetime2) "
                "AND dtInserted < CAST(:akhir AS datetime2)"
            ).bindparams(bindparam("campaigns", expanding=True))
            rows = conn.execute(stmt, {
                "campaigns": entry["campaigns"],
                "awal": (waktu - toleransi).isoformat(),
                "akhir": akhir.isoformat(),
            }).mappings().all()

    if len(rows) != write["ditulis"]:
        raise ValueError(
            f"Run {entry['run_id']} ({entry['as_of']}) mencatat {write['ditulis']} "
            f"baris tetapi tabel antrean mengembalikan {len(rows)}. Rekap ditunda."
        )
    return [dict(row) for row in rows]


def _build_rows(rows, brand_cfg, entry, source=None) -> list[dict[str, Any]]:
    as_of = dt.date.fromisoformat(str(entry["as_of"]))
    brand_1 = brand_cfg["brand"] == "brand_1"
    if brand_1 and source is None:
        source = _reload_brand_1_source(brand_cfg, as_of)
    default_date = as_of - dt.timedelta(days=int(brand_cfg["lookback_days"]))
    dimensions = {campaign: dim for dim, campaign in brand_cfg["campaigns"].items()}
    hasil = []
    for row in rows:
        phone, campaign = row["txtPhoneNo"], row["TxtCampaignName"]
        common = {
            "run_id": str(entry["run_id"]),
            "nomor_wa": phone,
            "nama_campaign": campaign,
            "nama_template": row["txtTemplateName"],
            "nama_workspace": row["txtWorkspaceName"],
            "queue_id": row["intQueueId"],
            "tag": row["txtSegmentName"],
            "params": row["txtParams"],
            "insertdate_to_trwaqueue": _datetime_wib(row["dtInserted"]),
            **_queue_status(row, brand_1=brand_1),
        }
        metadata = source.get((phone, campaign)) if source is not None else None
        if source is not None and metadata is None:
            raise ValueError(f"Data sumber untuk queue_id {row['intQueueId']} tidak ditemukan.")
        tanggal = metadata["event_date"] if metadata is not None else default_date
        if brand_1:
            hasil.append({
                **common,
                "nama": metadata["nama"],
                "outlet_id_source": metadata["outlet_id_source"],
                "segmen": metadata["segmen"],
                "receipt_date": tanggal,
                "followup_date": as_of,
            })
        else:
            params = json.loads(row["txtParams"])
            hasil.append({
                **common,
                "nama": params[0],
                "nama_produk": params[1],
                "kode_produk": dimensions.get(campaign),
                "tanggal_input": tanggal,
                "periode_garansi": (
                    transform.tanggal_akhir_garansi(tanggal, brand_cfg["warranty_period"])
                    if tanggal is not None
                    else None
                ),
            })
    return hasil


def _table_ref(table: str) -> str:
    if not re.fullmatch(r"[\w-]+\.[\w]+\.[\w-]+", table, re.ASCII):
        raise config.ConfigError("recap_table harus berformat project.dataset.table.")
    return f"`{table}`"


def _bq_client():
    from google.cloud import bigquery

    return bigquery.Client(
        project=config.env("BQ_PROJECT", required=True), location=config.env("BQ_LOCATION")
    )


def _json_params(rows: list[dict[str, Any]]):
    from google.cloud import bigquery

    payload = [json.dumps(row, ensure_ascii=False, default=str) for row in rows]
    return bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("rows", "STRING", payload)
    ])


def _merge(
    table: str, rows: list[dict[str, Any]], fields: dict[str, str], *, insert_missing: bool = True
) -> None:
    table_ref = _table_ref(table)
    client = _bq_client()
    select_fields = ", ".join(
        f"CAST(JSON_VALUE(item, '$.{name}') AS {kind}) AS {name}"
        for name, kind in fields.items()
    )
    insert = ""
    if insert_missing:
        names = ", ".join(fields)
        values = ", ".join(f"source.{name}" for name in fields)
        insert = f"WHEN NOT MATCHED THEN INSERT ({names}) VALUES ({values})"
    sql = f"""
        MERGE {table_ref} AS target
        USING (SELECT {select_fields} FROM UNNEST(@rows) AS item) AS source
        ON target.queue_id = source.queue_id
        WHEN MATCHED AND (
            target.updatedate_queue IS NULL
            OR source.updatedate_queue >= target.updatedate_queue
        ) AND (
            target.status_queue IS DISTINCT FROM source.status_queue
            OR target.updatedate_queue IS DISTINCT FROM source.updatedate_queue
        ) THEN UPDATE SET
            status_queue = source.status_queue,
            updatedate_queue = source.updatedate_queue,
            update_date = source.update_date
        {insert}
    """
    for i in range(0, len(rows), 500):
        client.query(sql, job_config=_json_params(rows[i : i + 500])).result()


def _fondasi_source(table: str, *, brand_1: bool) -> str:
    date_fields = {
        "dtsendwa": "fondasi.dtsendwa",
        "dtdelivered": "fondasi.dtdelivered",
        "dtread": "fondasi.dtread",
        "dtfailed": "fondasi.dtfailed",
        "updatedate_log": "COALESCE(fondasi.dtupdate, fondasi.dtinsert)",
    }
    dates = ", ".join(f"{expr} AS {name}" for name, expr in date_fields.items())
    log_id = "fondasi.id" if brand_1 else "CAST(fondasi.id AS STRING)"
    return f"""
        WITH messages AS (
            SELECT CAST(JSON_VALUE(item, '$.queue_id') AS INT64) AS queue_id,
                   JSON_VALUE(item, '$.message_id') AS message_id
            FROM UNNEST(@rows) AS item
        )
        SELECT messages.queue_id, {log_id} AS id_log_fondasi,
               fondasi.txtstatus AS status_blast, {dates}, fondasi.txtfailedreason
        FROM messages JOIN {_table_ref(table)} AS fondasi
          ON fondasi.txtmessageid = messages.message_id
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY messages.queue_id
            ORDER BY COALESCE(fondasi.dtupdate, fondasi.dtinsert) DESC,
                     fondasi.job_insertdate DESC, fondasi.id DESC
        ) = 1
    """


def _refresh_fondasi(brand_cfg, messages: list[dict[str, Any]]) -> None:
    brand_1 = brand_cfg["brand"] == "brand_1"
    table = _table_ref(brand_cfg["recap_table"])
    source = _fondasi_source(brand_cfg["fondasi_log_table"], brand_1=brand_1)
    fields = (
        "id_log_fondasi", "status_blast", "dtsendwa", "dtdelivered", "dtread",
        "dtfailed", "txtfailedreason", "updatedate_log",
    )
    differences = " OR ".join(f"target.{name} IS DISTINCT FROM source.{name}" for name in fields)
    updates = ", ".join(f"{name} = source.{name}" for name in fields)
    sql = f"""
        MERGE {table} AS target
        USING ({source}) AS source
        ON target.queue_id = source.queue_id
        WHEN MATCHED AND (
            target.updatedate_log IS NULL
            OR source.updatedate_log >= target.updatedate_log
        ) AND ({differences}) THEN
            UPDATE SET {updates}, update_date = CURRENT_DATETIME('Asia/Jakarta')
    """
    client = _bq_client()
    for i in range(0, len(messages), 5000):
        client.query(sql, job_config=_json_params(messages[i : i + 5000])).result()


def sync_run(engine, queue_cfg, brand_cfg, entry, *, source=None, next_run=None) -> int:
    """Isi rekap dari satu execute yang sudah commit."""
    queue_rows = _read_queue(engine, queue_cfg, entry, next_run=next_run)
    rows = _build_rows(queue_rows, brand_cfg, entry, source)
    _merge(brand_cfg["recap_table"], rows, _FIELDS[brand_cfg["brand"]])
    return len(rows)


def refresh_queue_status(engine, queue_cfg, brand_cfg) -> set[int]:
    """Cocokkan seluruh queue_id rekap dengan antrean dan perbarui statusnya."""
    from sqlalchemy import bindparam, text

    table = brand_cfg["recap_table"]
    existing = _bq_client().query(
        f"SELECT queue_id, status_queue, updatedate_queue FROM {_table_ref(table)} "
        "WHERE queue_id IS NOT NULL"
    ).result(page_size=500)

    target = queue_writer._target_name(queue_cfg, config.env("QUEUE_DB_SCHEMA"))
    stmt = text(
        "SELECT intQueueId, bolSuccess, txtParamResponse, dtUpdated, txtMessageId "
        f"FROM {target} WHERE intQueueId IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    brand_1 = brand_cfg["brand"] == "brand_1"
    matched: set[int] = set()
    changes: dict[int, dict[str, Any]] = {}
    messages: dict[int, dict[str, Any]] = {}
    with engine.connect() as conn:
        for page in existing.pages:
            previous = {row.queue_id: row for row in page}
            if not previous:
                continue
            for row in conn.execute(stmt, {"ids": list(previous)}).mappings():
                queue_id = row["intQueueId"]
                matched.add(queue_id)
                if row["txtMessageId"]:
                    messages[queue_id] = {"queue_id": queue_id, "message_id": row["txtMessageId"]}
                status = _queue_status(row, brand_1=brand_1)
                updated = status["updatedate_queue"]
                old = previous[queue_id]
                if status["status_queue"] != old.status_queue or updated != old.updatedate_queue:
                    changes[queue_id] = {"queue_id": queue_id, **status}

    if changes:
        fields = _FIELDS[brand_cfg["brand"]]
        status_fields = {
            name: fields[name] for name in ("queue_id", "status_queue", "updatedate_queue", "update_date")
        }
        _merge(table, list(changes.values()), status_fields, insert_missing=False)
    if messages and brand_cfg.get("fondasi_log_table"):
        _refresh_fondasi(brand_cfg, list(messages.values()))
    return matched


def backfill(engine, queue_cfg, brand_cfg, logs_dir: Path, as_of: dt.date) -> int:
    """Rekap ulang execute yang tercatat untuk satu tanggal blast."""
    with (logs_dir / runlog.RUNLOG_FILE).open(encoding="utf-8") as file:
        history = [json.loads(line) for line in file if line.strip()]
    runs = sorted(
        (r for r in history if r["brand"] == brand_cfg["brand"] and r["mode"] == "execute"),
        key=lambda r: r["waktu"],
    )
    total = 0
    for i, entry in enumerate(runs):
        if entry["as_of"] != str(as_of) or not entry.get("write", {}).get("committed"):
            continue
        if entry.get("from_csv"):
            continue
        next_run = dt.datetime.fromisoformat(runs[i + 1]["waktu"]) if i + 1 < len(runs) else None
        total += sync_run(engine, queue_cfg, brand_cfg, entry, next_run=next_run)
    return total
