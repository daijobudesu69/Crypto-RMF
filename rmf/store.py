"""State JSON dan log CSV di folder state/ (di-commit ke repo oleh workflow).

* JSON ditulis atomik (file sementara lalu rename): job yang mati di tengah
  penulisan tidak boleh meninggalkan file setengah jadi.
* CSV append-only. .gitattributes memakai merge=union untuk state/*.csv, jadi
  dua run yang menambah baris bersamaan tidak bentrok saat rebase.
* Setiap baris CSV juga dicerminkan ke Google Sheets kalau dikonfigurasi
  (lihat rmf/sheets.py). Gagal mirror tidak pernah menggagalkan run.
"""
from __future__ import annotations

import csv
import json
import os
import tempfile

from . import sheets
from .config import ROOT

STATE_DIR = os.environ.get("RMF_STATE_DIR") or os.path.join(ROOT, "state")

# Kolom tetap per log. Menambah kolom: tambahkan di AKHIR. File lama yang header-nya
# awalan dari kolom baru dimigrasi di tempat (riwayat tetap di file yang sama, kolom
# baru diisi BACKFILL atau kosong); header lain yang berbeda -> file dirotasi.
COLUMNS = {
    "runs": ["time_utc", "commit", "momentum_mode", "flush_mode", "momentum", "flush", "live",
             "errors", "duration_s"],
    "orders": ["time_utc", "book", "strategy", "exec_day", "coin", "side", "qty", "px", "mid",
               "notional", "fee", "pnl", "reason", "status"],
    "equity": ["exec_day", "time_utc", "regime_on", "btc_close", "btc_ema", "paper_equity",
               "paper_gross", "paper_positions", "live_equity", "live_positions", "flush_equity",
               "bench_ret", "bench_index", "paper_peak", "paper_dd_pct", "delay_min", "live_gross",
               "total_equity"],
    "ranking": ["exec_day", "rank", "coin", "ret_14d_pct", "held_paper", "held_live"],
    "flush_signals": ["bar_utc", "time_utc", "n_signals", "n_symbols", "event", "coins"],
    "flush_trades": ["entry_time", "exit_time", "coin", "signal_bar", "entry_px", "exit_px", "stop",
                     "target", "qty", "notional", "risk_usd", "forced_min", "bars_held", "reason",
                     "pnl_gross", "fees", "funding", "pnl_net", "r_multiple"],
    # buku paper pembanding universe bulanan (universe.compare_rolling)
    "equity_rolling": ["exec_day", "time_utc", "regime_on", "paper_equity", "paper_gross", "paper_positions",
                       "universe_size", "bench_ret", "bench_index", "paper_peak", "paper_dd_pct", "static_equity"],
    "orders_rolling": ["time_utc", "book", "strategy", "exec_day", "coin", "side", "qty", "px", "mid",
                       "notional", "fee", "pnl", "reason", "status"],
}
# Log yang dicerminkan ke Sheets (runs tidak: terlalu sering; *_rolling tidak:
# buku pembanding tidak boleh mengubah Sheets buku utama).
MIRROR = {"orders", "equity", "flush_trades", "flush_signals"}


def _num(r: dict, k: str) -> float:
    try:
        return float(r.get(k) or 0)
    except ValueError:
        return 0.0


def total_equity(row: dict) -> float | None:
    """Ekuitas paper gabungan satu baris equity.csv: momentum + PnL flush.
    Baris lama (sebelum kolom total_equity) dihitung dari kedua kolom itu."""
    v = row.get("total_equity")
    if v not in (None, ""):
        return float(v)
    if row.get("paper_equity") in (None, ""):
        return None
    return _num(row, "paper_equity") + _num(row, "flush_equity")


# Nilai kolom baru untuk baris lama saat migrasi di tempat.
BACKFILL = {"equity": {"total_equity": total_equity}}


def path(name: str) -> str:
    return os.path.join(STATE_DIR, name)


def load_json(name: str, default=None):
    p = path(name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(name: str, data) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, sort_keys=True, default=str)
        fh.write("\n")
    os.replace(tmp, path(name))


def _rotate_if_needed(p: str, cols: list, log: str = "") -> None:
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8", newline="") as fh:
        head = next(csv.reader(fh), None)
    if head and len(head) < len(cols) and cols[:len(head)] == head:
        _extend_in_place(p, cols, BACKFILL.get(log, {}))
        return
    if head and head != cols:
        base, ext = os.path.splitext(p)
        i = 1
        while os.path.exists(f"{base}.v{i}{ext}"):
            i += 1
        os.replace(p, f"{base}.v{i}{ext}")


def _extend_in_place(p: str, cols: list, fill: dict) -> None:
    """Kolom baru ditambah di akhir: tulis ulang file dengan header baru (atomik)."""
    with open(p, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k, fn in fill.items():
            if r.get(k) in (None, ""):
                r[k] = _fmt(fn(r))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".tmp_", suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows({k: r.get(k) if r.get(k) is not None else "" for k in cols} for r in rows)
    os.replace(tmp, p)


def append(log: str, row: dict, mirror: bool = True) -> None:
    cols = COLUMNS[log]
    os.makedirs(STATE_DIR, exist_ok=True)
    p = path(f"{log}.csv")
    _rotate_if_needed(p, cols, log)
    new = not os.path.exists(p)
    clean = {k: _fmt(row.get(k)) for k in cols}
    with open(p, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(clean)
    if mirror and log in MIRROR:
        sheets.append(log, cols, clean)


def read(log: str) -> list[dict]:
    p = path(f"{log}.csv")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return round(v, 8)
    return v
