"""Pembacaan konfigurasi: .env, config/queue.yaml, brand/<brand>/config.yaml.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Literal, overload

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAND_DIR = ROOT / "brand"
CONFIG_DIR = ROOT / "config"
LOGS_DIR = ROOT / "logs"


class ConfigError(Exception):
    """Konfigurasi tidak lengkap atau tidak valid."""


def load_env() -> None:
    """Muat .env kalau ada. Variable yang sudah ada di environment tidak ditimpa."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(ROOT / ".env", override=False)


@overload
def env(name: str, default: str | None = None, *, required: Literal[True]) -> str: ...
@overload
def env(name: str, default: str | None = None, required: bool = False) -> str | None: ...
def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise ConfigError(
            f"Environment variable '{name}' belum diisi. "
            f"Salin .env.example jadi .env lalu isi nilainya."
        )
    return value


def queue_db_url() -> str:
    """Bangun connection string ke database antrean dari .env.

    Dua cara mengisi .env - pilih SALAH SATU:
      (a) QUEUE_DB_SERVER / QUEUE_DB_NAME / QUEUE_DB_USER / QUEUE_DB_PASSWORD
          terpisah - direkomendasikan, password di-encode OTOMATIS di sini
          jadi tidak perlu mikirin karakter spesial (@ : / ? #) sendiri.
      (b) QUEUE_DB_URL - connection string lengkap jadi satu, untuk kasus
          yang butuh opsi SQLAlchemy lebih lanjut (mis. driver ODBC custom).

    (a) dicek duluan kalau keduanya ada isinya.
    """
    server = env("QUEUE_DB_SERVER")
    database = env("QUEUE_DB_NAME")
    username = env("QUEUE_DB_USER")
    password = env("QUEUE_DB_PASSWORD")

    if server and database and username and password:
        from urllib.parse import quote_plus

        port = env("QUEUE_DB_PORT") or "1433"
        return (
            f"mssql+pymssql://{quote_plus(username)}:{quote_plus(password)}"
            f"@{server}:{port}/{database}"
        )

    url_langsung = env("QUEUE_DB_URL")
    if url_langsung:
        return url_langsung

    raise ConfigError(
        "Koneksi ke database antrean belum lengkap. Isi SALAH SATU di .env:\n"
        "  (a) QUEUE_DB_SERVER, QUEUE_DB_NAME, QUEUE_DB_USER, QUEUE_DB_PASSWORD\n"
        "  (b) QUEUE_DB_URL (connection string lengkap)\n"
    )


def _read_yaml(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"File konfigurasi tidak ditemukan: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Isi {path} harus berupa mapping YAML.")
    return data


def _read_brands_registry() -> dict[str, Any]:
    """Baca sakelar brand dari config/brands.yaml."""
    return _read_yaml(CONFIG_DIR / "brands.yaml")


def list_brands() -> list[str]:
    """Brand yang terdaftar DAN enabled. Brand yang di-disable di
    config/brands.yaml tidak ikut muncul, meski foldernya masih ada."""
    if not BRAND_DIR.exists():
        return []
    registry = _read_brands_registry()
    semua = sorted(
        p.name
        for p in BRAND_DIR.iterdir()
        if p.is_dir() and not p.name.startswith("_") and (p / "config.yaml").exists()
    )
    return [b for b in semua if (registry.get(b) or {}).get("enabled") is True]


# Field yang wajib ada di brand/<brand>/config.yaml
_REQUIRED_BRAND_KEYS = (
    "brand",
    "campaign_name",
    "campaigns",
    "sender",
    "lookback_days",
    "template_mode",
    "templates",
)

_TEMPLATE_MODES = ("single", "by_name", "multi_param")


def load_brand(brand: str, *, allow_disabled: bool = False) -> dict[str, Any]:
    """Baca aturan main satu brand. Isinya aturan, bukan logika."""
    brand_dir = BRAND_DIR / brand
    if not brand_dir.is_dir():
        tersedia = ", ".join(list_brands()) or "(belum ada)"
        raise ConfigError(f"Brand '{brand}' tidak ada. Brand tersedia: {tersedia}")

    registry = _read_brands_registry()
    if not allow_disabled and (registry.get(brand) or {}).get("enabled") is not True:
        raise ConfigError(
            f"Brand '{brand}' belum aktif. Set 'enabled: true' untuk brand ini "
            f"di config/brands.yaml."
        )

    cfg = _read_yaml(brand_dir / "config.yaml")

    missing = [k for k in _REQUIRED_BRAND_KEYS if k not in cfg]
    if missing:
        raise ConfigError(
            f"brand/{brand}/config.yaml kurang field: {', '.join(missing)}"
        )

    query_path = brand_dir / "query.sql"
    if not query_path.exists():
        raise ConfigError(f"Query tidak ditemukan: {query_path}")

    # --- validasi template_mode --------------------------------------
    if cfg["template_mode"] not in _TEMPLATE_MODES:
        raise ConfigError(
            f"template_mode '{cfg['template_mode']}' tidak dikenal. "
            f"Pilihannya: {', '.join(_TEMPLATE_MODES)}"
        )

    # --- validasi campaign & template lengkap per nilai dimensi ------
    # Nama kolom dimensinya ikut 'dimensi_kolom' (default 'segmen' - brand_1
    # pakai segmen bisnis, brand lain bisa pakai kolom lain mis. kode_produk).
    #
    # 'segment_priority' OPSIONAL. Kalau brand tidak isi (tidak ada urutan
    # prioritas bisnis yang nyata, mis. brand_2), otomatis diturunkan dari
    # kunci 'campaigns' - TIDAK PERLU ditulis ulang manual, itu cuma
    # duplikasi info yang sudah ada di 'campaigns'.
    dim_col = str(cfg.get("dimensi_kolom", "segmen"))
    daftar_prioritas = cfg.get("segment_priority") or sorted(cfg["campaigns"].keys())
    segmen = [str(s).strip().lower() for s in daftar_prioritas]
    cfg["segment_priority"] = segmen  # simpan balik, biar konsisten dipakai core/

    kurang_campaign = [s for s in segmen if s not in cfg["campaigns"]]
    if kurang_campaign:
        raise ConfigError(
            f"brand/{brand}/config.yaml: 'campaigns' belum punya entri untuk "
            f"{dim_col}: {', '.join(kurang_campaign)}"
        )

    if cfg["template_mode"] == "single":
        kunci_tpl = "single"
    elif cfg["template_mode"] == "multi_param":
        kunci_tpl = "multi_param"
        if not cfg.get("param_fields"):
            raise ConfigError(
                f"brand/{brand}/config.yaml: template_mode='multi_param' butuh "
                "field 'param_fields' (daftar kolom buat {1}, {2}, dst)."
            )
    else:
        kunci_tpl = "with_name"

    if cfg.get("template_shared"):
        # 'templates' FLAT - 1 template dipakai semua nilai dimensi,
        # tidak perlu diulang per nilai dimensi (lihat core/template.py).
        if not (cfg["templates"] or {}).get(kunci_tpl):
            raise ConfigError(
                f"brand/{brand}/config.yaml: template_shared=true tapi 'templates' "
                f"belum punya kunci '{kunci_tpl}'."
            )
    else:
        kurang_tpl = [
            s
            for s in segmen
            if not (cfg["templates"].get(s) or {}).get(kunci_tpl)
        ]
        if kurang_tpl:
            raise ConfigError(
                f"brand/{brand}/config.yaml: template_mode='{cfg['template_mode']}' "
                f"butuh kunci '{kunci_tpl}', tapi belum ada untuk {dim_col}: "
                f"{', '.join(kurang_tpl)}"
            )

    cfg["_dir"] = brand_dir
    cfg["_query_path"] = query_path
    sku_check_path = brand_dir / "sku_check.sql"
    cfg["_sku_check_path"] = sku_check_path if sku_check_path.exists() else None
    outlet_check_path = brand_dir / "outlet_check.sql"
    cfg["_outlet_check_path"] = outlet_check_path if outlet_check_path.exists() else None
    cfg["campaign_codes"] = [cfg["campaigns"][s] for s in segmen]
    cfg.setdefault("cooldown_days", 30)
    cfg.setdefault("batch_limit", 0)  # 0 = tanpa batas
    cfg.setdefault("segment_selection", "fixed")  # brand lain: perilaku lama
    cfg.setdefault("sku_check_days", 30)
    cfg.setdefault("dimensi_kolom", "segmen")  # default: perilaku lama (brand_1)
    cfg.setdefault("id_kolom", "outlet_id_source")  # default: perilaku lama (brand_1)
    return cfg


def load_queue_config() -> dict[str, Any]:
    """Baca pemetaan kolom tabel antrean."""
    cfg = _read_yaml(CONFIG_DIR / "queue.yaml")
    if not cfg.get("table"):
        raise ConfigError("config/queue.yaml: field 'table' wajib diisi.")
    if not cfg.get("columns"):
        raise ConfigError(
            "config/queue.yaml: 'columns' masih kosong. "
            "Isi dulu setelah struktur tabel antrean yang diotorisasi dikonfigurasi."
        )
    cfg.setdefault("schema", None)
    cfg.setdefault("cooldown_query", None)
    cfg.setdefault("check_results_query", None)
    return cfg


def ensure_logs_dir() -> pathlib.Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR
