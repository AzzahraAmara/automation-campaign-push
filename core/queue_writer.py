"""Penulisan ke tabel antrean SQL Server.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

import pandas as pd


class QueueMappingError(Exception):
    """Pemetaan kolom di config/queue.yaml tidak bisa diterjemahkan."""


def build_engine(db_url: str):
    """Bikin SQLAlchemy engine. Engine-agnostic: Postgres / SQL Server sama saja."""
    from sqlalchemy import create_engine

    if not db_url:
        raise QueueMappingError(
            "QUEUE_DB_URL belum diisi di .env - tidak bisa konek ke tabel antrian."
        )
    return create_engine(db_url, pool_pre_ping=True, future=True)


def _resolve(spec: Any, row: dict[str, Any], runtime: dict[str, Any]) -> Any:
    """Terjemahkan satu nilai pemetaan kolom.

    Bentuk yang dikenali:
      nama_field        -> ambil dari baris data / runtime
      const:NILAI       -> nilai tetap
      now               -> timestamp sekarang
      today             -> tanggal hari ini
      uuid              -> UUID acak
      null              -> NULL
    """
    if spec is None:
        return None
    if not isinstance(spec, str):
        return spec  # angka / boolean literal di YAML

    if spec.startswith("const:"):
        return spec[len("const:"):]
    if spec == "now":
        return dt.datetime.now()
    if spec == "today":
        return dt.date.today()
    if spec == "uuid":
        return str(uuid.uuid4())
    if spec == "null":
        return None

    if spec in row:
        nilai = row[spec]
        if isinstance(nilai, float) and pd.isna(nilai):
            return None
        if nilai is pd.NaT:
            return None
        return nilai
    if spec in runtime:
        return runtime[spec]

    raise QueueMappingError(
        f"config/queue.yaml menunjuk field '{spec}' yang tidak ada di data. "
        f"Field tersedia: {', '.join(sorted(set(row) | set(runtime)))}"
    )


def build_rows(
    df: pd.DataFrame,
    queue_cfg: dict[str, Any],
    runtime: dict[str, Any],
) -> list[dict[str, Any]]:
    """Ubah dataframe hasil pipeline jadi baris siap-INSERT sesuai pemetaan."""
    columns: dict[str, Any] = queue_cfg["columns"]
    hasil: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        # df.to_dict() mengetik kunci sebagai Hashable (umum di pandas),
        # tapi kolom dataframe di pipeline ini SELALU string - aman di-cast.
        baris = cast(dict[str, Any], row)
        hasil.append({kolom: _resolve(spec, baris, runtime) for kolom, spec in columns.items()})
    return hasil


def _target_name(queue_cfg: dict[str, Any], schema_env: str | None) -> str:
    schema = schema_env or queue_cfg.get("schema")
    tabel = queue_cfg["table"]
    return f"{schema}.{tabel}" if schema else tabel


def write(
    rows: list[dict[str, Any]],
    queue_cfg: dict[str, Any],
    engine,
    *,
    mode: str,
    schema_env: str | None = None,
    chunk_size: int = 500,  # teknis: pecah INSERT besar, bukan batas blast
    capture_ids: bool = False,
) -> dict[str, Any]:
    """Tulis ke tabel antrian.

    mode='rollback' sengaja dibatalkan di akhir - dipakai untuk testing tingkat 3.
    """
    from sqlalchemy import text

    if mode not in {"rollback", "execute"}:
        raise ValueError(f"mode tidak dikenal untuk write(): {mode}")
    if not rows:
        return {"mode": mode, "ditulis": 0, "committed": False}

    target = _target_name(queue_cfg, schema_env)
    kolom = list(rows[0].keys())
    placeholders = ", ".join(f":{k}" for k in kolom)
    stmt = text(f"INSERT INTO {target} ({', '.join(kolom)}) VALUES ({placeholders})")

    ditulis = 0
    queue_ids: list[int] = []
    capture_ids = capture_ids and mode == "execute"
    if capture_ids:
        chunk_size = min(chunk_size, 2000 // len(kolom), 1000)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            for i in range(0, len(rows), chunk_size):
                batch = rows[i : i + chunk_size]
                if capture_ids:
                    values = []
                    params = {}
                    for index, row in enumerate(batch):
                        params.update({f"{k}_{index}": row[k] for k in kolom})
                        values.append("(" + ", ".join(f":{k}_{index}" for k in kolom) + ")")
                    capture = text(
                        "SET NOCOUNT ON; "
                        "DECLARE @queue_ids TABLE (queue_id BIGINT); "
                        f"INSERT INTO {target} ({', '.join(kolom)}) "
                        "OUTPUT INSERTED.intQueueId INTO @queue_ids "
                        f"VALUES {', '.join(values)}; SELECT queue_id FROM @queue_ids"
                    )
                    queue_ids.extend(conn.execute(capture, params).scalars().all())
                else:
                    conn.execute(stmt, batch)
                ditulis += len(batch)

            if capture_ids and len(queue_ids) != ditulis:
                raise QueueMappingError("Jumlah ID yang dikembalikan tabel antrean berbeda dari jumlah baris INSERT.")
            if mode == "execute":
                trans.commit()
                committed = True
            else:
                trans.rollback()
                committed = False
        except Exception:
            trans.rollback()
            raise

    result = {"mode": mode, "ditulis": ditulis, "committed": committed, "target": target}
    if capture_ids:
        result["queue_ids"] = queue_ids
    return result


def delete_by_campaign(
    engine,
    queue_cfg: dict[str, Any],
    campaign_codes: str | list[str],
    schema_env: str | None = None,
) -> int:
    """Jalur darurat: hapus baris yang dimasukkan pipeline ini.

    Hanya menghapus baris dengan campaign dari brand ini, supaya tidak
    menyentuh data proses lain yang berbagi tabel ini. Baris CSI meninggalkan
    kolom campaign NULL, jadi tidak akan pernah ikut terhapus.

    Terima satu nama atau daftar nama - satu brand punya beberapa campaign.
    """
    from sqlalchemy import bindparam, text

    kolom_penanda = queue_cfg.get("campaign_code_column")
    if not kolom_penanda:
        raise QueueMappingError(
            "config/queue.yaml: 'campaign_code_column' belum diisi, "
            "penghapusan darurat tidak bisa dijalankan dengan aman."
        )

    if isinstance(campaign_codes, str):
        campaign_codes = [campaign_codes]
    if not campaign_codes:
        return 0

    target = _target_name(queue_cfg, schema_env)
    stmt = text(
        f"DELETE FROM {target} WHERE {kolom_penanda} IN :campaign_codes"
    ).bindparams(bindparam("campaign_codes", expanding=True))

    with engine.connect() as conn:
        trans = conn.begin()
        hasil = conn.execute(stmt, {"campaign_codes": list(campaign_codes)})
        trans.commit()
    return hasil.rowcount or 0
