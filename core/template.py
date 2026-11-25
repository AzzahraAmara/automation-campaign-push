"""Penentuan template WhatsApp + nama campaign per nilai dimensi brand.

Aturannya ada di brand/<brand>/config.yaml, bukan di sini. Core hanya
mencocokkan nilai dimensi -> template dan nilai dimensi -> nama campaign.
Nama kolom dimensinya ikut 'dimensi_kolom' di config brand (default
'segmen' - brand_1 memakai segmen, brand_2 memakai kode produk).

TIGA MODE TEMPLATE (diatur lewat 'template_mode' di config brand):

  single      satu template per nilai dimensi, TANPA parameter.
              txtParams selalu []. Nama konsumen tidak dipakai sama sekali.
              Ini contoh template yang tidak memakai parameter.

  by_name     dua template per nilai dimensi: v1 untuk yang ada nama (1
              parameter), v2 untuk yang tidak. Ini bentuk template lama
              (v1_rev / v2_rev).

  multi_param satu template per nilai dimensi, parameter BANYAK (2+),
              daftar kolom sumbernya diatur lewat 'param_fields' di config
              brand - urutan {{1}}, {{2}}, dst ikut urutan list itu.

TEMPLATE SATU UNTUK SEMUA NILAI DIMENSI (template_shared: true):

  Kalau brand cuma punya SATU template dipakai semua nilai dimensi (mis.
  brand_2 - beberapa produk, 1 template yang sama), set 'template_shared: true'
  di config brand. 'templates' jadi FLAT, tidak perlu diulang per nilai
  dimensi:

      template_shared: true
      templates:
        multi_param: YOUR_TEMPLATE_NAME

  'campaigns' berisi mapping eksplisit per nilai dimensi.

Ganti modenya = ganti satu kata di config. Tidak ada kode yang perlu disentuh.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd


class TemplateError(Exception):
    """Template atau campaign untuk suatu nilai dimensi tidak terdaftar di config."""


def _ada_nama(nilai: Any) -> bool:
    """Cek nama benar-benar terisi.

    Hati-hati: bool(float('nan')) itu True, jadi pengecekan truthiness biasa
    akan salah memilih template v1 untuk baris yang namanya kosong.
    """
    if nilai is None:
        return False
    try:
        if pd.isna(nilai):
            return False
    except (TypeError, ValueError):
        pass
    return bool(str(nilai).strip())


def apply(df: pd.DataFrame, brand_cfg: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, int]]:
    stats: dict[str, int] = {}
    if df.empty:
        return df, stats

    mode: str = brand_cfg["template_mode"]
    templates: dict[str, Any] = brand_cfg["templates"]
    template_shared = bool(brand_cfg.get("template_shared", False))
    campaigns: dict[str, str] = brand_cfg["campaigns"]
    dim_col = str(brand_cfg.get("dimensi_kolom", "segmen"))

    df = df.copy()
    df["_ada_nama"] = df["nama"].apply(_ada_nama)

    # --- nama campaign per nilai dimensi ------------------------------
    # Satu brand punya BEBERAPA campaign, satu per nilai dimensi - ini
    # SELALU per-dimensi (tidak ada mode 'shared'), karena kode campaign
    # memang berbeda-beda per produk/segmen, itu data asli bukan duplikasi.
    df["campaign_code"] = df[dim_col].map(campaigns)
    tanpa_campaign = df["campaign_code"].isna()
    if tanpa_campaign.any():
        bermasalah = sorted(df.loc[tanpa_campaign, dim_col].unique())
        raise TemplateError(
            "Ada baris tanpa nama campaign. Lengkapi 'campaigns' di "
            f"brand/{brand_cfg['brand']}/config.yaml untuk {dim_col}: "
            f"{', '.join(bermasalah)}"
        )

    # --- template per nilai dimensi (atau shared, lihat docstring) -----
    def pilih(row) -> str | None:
        per_dim = templates if template_shared else templates.get(row[dim_col])
        if not per_dim:
            return None
        if mode in ("single", "multi_param"):
            return per_dim.get(mode)
        kunci = "with_name" if row["_ada_nama"] else "without_name"
        return per_dim.get(kunci)

    df["template_name"] = df.apply(pilih, axis=1)

    tanpa_template = df["template_name"].isna()
    if tanpa_template.any():
        bermasalah = sorted(df.loc[tanpa_template, dim_col].unique())
        raise TemplateError(
            f"Ada baris tanpa template (mode '{mode}'). Lengkapi 'templates' di "
            f"brand/{brand_cfg['brand']}/config.yaml untuk {dim_col}: "
            f"{', '.join(bermasalah)}"
        )

    # --- parameter ----------------------------------------------------
    # Tabel antrean menyimpan parameter sebagai STRING berisi array JSON,
    # Mode 'single' -> selalu []
    if mode == "single":
        df["param_1"] = None
        df["params_json"] = "[]"
        stats["template_tanpa_parameter"] = len(df)
    elif mode == "multi_param":
        param_fields: list[str] = brand_cfg.get("param_fields") or []
        if not param_fields:
            raise TemplateError(
                f"template_mode 'multi_param' butuh 'param_fields' (daftar kolom "
                f"buat {{1}}, {{2}}, dst) di brand/{brand_cfg['brand']}/config.yaml."
            )
        hilang = [f for f in param_fields if f not in df.columns]
        if hilang:
            raise TemplateError(
                f"'param_fields' menunjuk kolom yang tidak ada di data: {', '.join(hilang)}"
            )

        def susun_params(row) -> list[Any]:
            return [row[f] for f in param_fields]

        df["param_1"] = None  # tidak dipakai di mode ini, dibiarkan ada biar kolom konsisten
        df["params_json"] = df.apply(
            lambda r: json.dumps(susun_params(r), ensure_ascii=False, default=str), axis=1
        )
        stats["template_dengan_banyak_parameter"] = len(df)
    else:
        df["param_1"] = df.apply(
            lambda r: r["nama"] if r["_ada_nama"] else None, axis=1
        )
        df["params_json"] = df.apply(
            lambda r: json.dumps(
                [r["nama"]] if r["_ada_nama"] else [], ensure_ascii=False
            ),
            axis=1,
        )
        stats["pakai_template_ada_nama"] = int(df["_ada_nama"].sum())
        stats["pakai_template_tanpa_nama"] = int((~df["_ada_nama"]).sum())

    df = df.drop(columns=["_ada_nama"])
    return df, stats
