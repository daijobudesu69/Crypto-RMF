"""Flush basket 4h — logika murni, tanpa jaringan.

Sumber: `research/flush_spot.py` (sinyal spot Binance, eksekusi HYPE) dan
`trade_sim.simulate` (exit SL/TP/waktu). Di forward test ini flush jalan PAPER.

Satu siklus 4h (setelah candle 4h close):
  * sinyal per koin di candle yang baru close (bar i):
        close[i-1] <= BB bawah[i-1]  dan  close[i] > BB bawah[i]
        volume[i]  >  1,5 x SMA20(volume)[i]
  * event kalau >= 10 koin (dari 150 pair spot) bersinyal di bar yang sama
  * long semua koin itu yang ada di HYPE (maks 15, acak dengan seed = waktu bar),
    entry di open 4h berikutnya, SL/TP = entry -/+ 2 x ATR(14) 4h HYPE di bar
    sinyal, keluar paksa setelah 48 candle.
  * ukuran: risiko min(0,5%, 8% / n koin) ekuitas per koin; minimum order 10 USDC;
    tolak kalau minimum memaksa risiko > 2x target; total notional momentum +
    flush <= 2x ekuitas. Total risiko event TIDAK dipotong (sama dengan simulasi
    riset; bisa > 8% karena minimum order, maks ~11% di simulasi).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind

# Pair spot Binance yang di HYPE memakai nama "k" (1000 unit).
K_MAP = {"PEPE": "kPEPE", "SHIB": "kSHIB", "BONK": "kBONK", "FLOKI": "kFLOKI"}


def hype_name(symbol: str) -> str:
    """PEPEUSDT (spot) / 1000PEPEUSDT (futures) -> kPEPE; SOLUSDT -> SOL."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    if base.startswith("1000") and len(base) > 4:
        base = base[4:]
    return K_MAP.get(base, base)


def signal_at_last(df: pd.DataFrame, cfg) -> bool:
    """Sinyal di candle TERAKHIR df (df hanya berisi candle yang sudah close)."""
    f = cfg.flush
    if df is None or len(df) < max(f.bb_len, f.vol_len) + 2:
        return False
    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    bbl = ind.bb_lower(c, f.bb_len, f.bb_k)
    up = ind.cross_up(c, bbl)
    vs = ind.sma(v, f.vol_len)
    return bool(up[-1] and np.isfinite(vs[-1]) and v[-1] > f.vol_mult * vs[-1])


def pick(coins: list, bar_ts: pd.Timestamp, max_coins: int) -> list:
    """Pilih maks N koin secara acak tapi deterministik (seed = detik waktu bar)."""
    coins = sorted(set(coins))
    if len(coins) <= max_coins:
        return coins
    seed = int(pd.Timestamp(bar_ts).timestamp())
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(coins))[:max_coins]
    return sorted(coins[i] for i in idx)


def atr_at(df: pd.DataFrame, bar_ts: pd.Timestamp, n: int) -> tuple[float, float]:
    """(ATR, close) di bar dengan waktu open = bar_ts pada candle HYPE."""
    a = ind.atr(df["high"].values, df["low"].values, df["close"].values, n)
    ts = pd.DatetimeIndex(df["ts"])
    hit = np.where(ts == pd.Timestamp(bar_ts))[0]
    if not len(hit):
        return float("nan"), float("nan")
    i = int(hit[0])
    return float(a[i]), float(df["close"].values[i])


@dataclass
class Plan:
    coin: str
    entry_px: float
    atr: float
    stop: float
    target: float
    stop_frac: float
    notional: float
    risk_usd: float
    forced_min: bool


def size_event(cands: list, equity: float, gross_used: float, cfg) -> tuple[list, list]:
    """cands: [(coin, entry_px, atr)] -> (rencana yang diterima, [(coin, alasan tolak)]).

    Aturan v2 riset (small_capital.simulate): risiko per koin = min(risk_pct,
    max_event_risk_pct / n) dengan n = jumlah koin event, tolak koin kalau minimum
    order memaksa risiko > 2x target, batas gross 2x ekuitas. Total risiko event
    TIDAK dipotong: hard cap 8% dihapus atas keputusan pemilik 2026-10-05 setelah
    simulasi ulang (research/q1_event_cap.py: 410,6 tanpa cap vs 397,5 dengan
    cap; 20 seed: cap tidak pernah lebih baik). Lihat docs/audit/AUDIT_2026-10-05.md Q1.
    """
    f = cfg.flush
    min_usd = cfg.momentum.min_order_usdc
    n = max(len(cands), 1)
    risk_target = equity * min(f.risk_pct / 100, f.max_event_risk_pct / 100 / n)
    gross_cap = equity * f.gross_cap_x
    out, rej = [], []
    gross = gross_used
    for coin, px, a in cands:
        if not (np.isfinite(a) and np.isfinite(px)) or px <= 0:
            rej.append((coin, "ATR/harga tidak tersedia"))
            continue
        if a < 0.0005 * px:                       # sama dengan trade_sim: pasar mati/datar
            rej.append((coin, "ATR terlalu kecil"))
            continue
        stop_frac = f.sl_atr * a / px
        notional = risk_target / stop_frac
        forced = False
        if notional < min_usd:
            notional, forced = min_usd, True
        risk = notional * stop_frac
        if forced and risk > f.reject_forced_risk_x * risk_target:
            rej.append((coin, f"minimum order memaksa risiko {risk:.2f} > {f.reject_forced_risk_x:g}x target"))
            continue
        if gross + notional > gross_cap + 1e-9:
            rej.append((coin, "batas gross 2x ekuitas"))
            continue
        gross += notional
        out.append(Plan(coin, px, a, px - f.sl_atr * a, px + f.tp_atr * a, stop_frac, notional, risk, forced))
    return out, rej


def check_exit(pos: dict, bars: pd.DataFrame, max_bars: int):
    """Jalankan aturan exit trade_sim di candle 4h HYPE yang sudah close.

    pos: dict dengan entry_bar (ISO, open bar entry), stop, target, last_bar
         (ISO bar terakhir yang sudah dicek, atau None), bars_held.
    bars: candle 4h HYPE yang sudah close, urut waktu.
    Return (exit dict atau None, pos yang diperbarui). Urutan cek per bar sama
    dengan backtest: gap di bawah stop -> open; low <= stop -> stop;
    high >= target -> max(target, open); lalu batas 48 candle -> close.
    """
    pos = dict(pos)
    entry_bar = pd.Timestamp(pos["entry_bar"])
    last = pd.Timestamp(pos["last_bar"]) if pos.get("last_bar") else None
    ts = pd.DatetimeIndex(bars["ts"])
    sel = ts >= entry_bar
    if last is not None:
        sel &= ts > last
    stop, tgt = float(pos["stop"]), float(pos["target"])
    for _, b in bars[sel].iterrows():
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        pos["bars_held"] = int(pos.get("bars_held", 0)) + 1
        pos["last_bar"] = pd.Timestamp(b["ts"]).isoformat()
        pos["last_px"] = c                    # harga cadangan kalau mid HYPE tidak tersedia
        px = why = None
        if o <= stop:
            px, why = o, "SL (gap)"
        elif l <= stop:
            px, why = stop, "SL"
        elif h >= tgt:
            px, why = max(tgt, o), "TP"
        elif pos["bars_held"] >= max_bars:
            px, why = c, "waktu (48 candle)"
        if px is not None:
            close_time = pd.Timestamp(b["ts"]) + pd.Timedelta(hours=4)
            return {"exit_px": px, "reason": why, "exit_bar": pd.Timestamp(b["ts"]).isoformat(),
                    "exit_time": close_time.isoformat()}, pos
    return None, pos
