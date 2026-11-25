"""Jalankan satu mode campaign untuk semua brand aktif di Gitea Actions."""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]
WIB = dt.timezone(dt.timedelta(hours=7))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["execute", "check-results"])
    args = parser.parse_args()

    command = [sys.executable, "main.py"]
    listed = subprocess.run(
        [*command, "--list-brands"], cwd=ROOT, capture_output=True, text=True
    )
    if listed.returncode:
        print(listed.stderr, file=sys.stderr, end="")
        return listed.returncode
    brands = listed.stdout.splitlines()
    if not brands:
        print("Tidak ada brand aktif di config/brands.yaml.")
        return 0

    env = os.environ.copy()
    if not env.get("GOOGLE_APPLICATION_CREDENTIALS_JSON"):
        print("Isi secret GOOGLE_APPLICATION_CREDENTIALS_JSON di Gitea.", file=sys.stderr)
        return 2

    options = ["--mode", args.mode]
    if args.mode == "execute":
        options += ["--as-of", dt.datetime.now(WIB).date().isoformat()]

    with TemporaryDirectory(prefix="automation-campaign-") as tmp:
        for key in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_SHEETS_CREDENTIALS"):
            content = env.pop(f"{key}_JSON", "")
            if content:
                path = Path(tmp) / f"{key}.json"
                path.write_text(content, encoding="utf-8")
                env[key] = str(path)

        return subprocess.run([*command, *options], cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
