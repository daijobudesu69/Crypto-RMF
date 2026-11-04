"""Job harian momentum dan job 4h flush. Dipanggil run_cycle.py setiap siklus.

Keduanya idempoten: state mencatat hari / bar terakhir yang sudah diproses,
jadi watcher yang mengecek tiap 10 menit hanya bekerja sekali per hari / bar.
Semua akses jaringan lewat objek `ctx` supaya tes bisa memakai data tiruan.
"""
from __future__ import annotations

import datetime as dt
import os
import traceback
from dataclasses import dataclass, field, replace

import pandas as pd

from . import book as bk
from . import control as ctl
from . import flush as fl
from . import live
from . import momentum as mom
from . import notify, stoprules, store
from .config import path_in_repo

DAY_MS = 86_400_000
H4 = pd.Timedelta(hours=4)
# Kalau candle 1d sebagian koin gagal diambil, siklus momentum dibatalkan dan
# dicoba ulang (~10 menit) sampai jam ini (UTC). Tanpa ini koin yang gagal
# dianggap "tidak eligible": posisi koin itu dijual dan peringkat lain bergeser,
# hal yang tidak mungkin terjadi di riset (audit 2026-10-05 F1).
FETCH_RETRY_UNTIL_H = 3          # 03:00 UTC = 10:00 WIB


class DataIncomplete(RuntimeError):
    """Data pasar belum lengkap; siklus berikutnya mencoba lagi."""


@dataclass
class Ctx:
    cfg: object
    info: object                       # rmf.hype.InfoClient (atau tiruan)
    klines: object                     # rmf.binance.KlineClient (atau tiruan)
    ctrl: ctl.Control
    outbox: notify.Outbox
    now: dt.datetime
    trader_factory: object = None      # () -> trader; None = live tidak tersedia
    _meta: dict | None = None
    log: list = field(default_factory=list)
    _c1d: dict = field(default_factory=dict)     # candle 1d siklus ini, dipakai ulang buku pembanding

    def meta(self) -> dict:
        if self._meta is None:
            self._meta = self.info.meta()
        return self._meta

    def say(self, msg: str) -> None:
        print(msg)
        self.log.append(msg)


def utc_day(now: dt.datetime) -> pd.Timestamp:
    return pd.Timestamp(now).tz_convert("UTC").tz_localize(None).normalize()


def started(cfg, now: dt.datetime) -> bool:
    return not cfg.forward_start or utc_day(now) >= pd.Timestamp(cfg.forward_start)


# =========================================================================== #
#  MOMENTUM (harian)
# =========================================================================== #
def fetch_view(ctx: Ctx, exec_day: pd.Timestamp) -> mom.View:
    cfg = ctx.cfg
    meta = ctx.meta()
    alive = [c for c, m in meta.items() if not m["isDelisted"]]
    if cfg.universe.mode == "static":
        want = set(mom.static_universe(cfg)) | {cfg.momentum.regime_symbol}
        coins = [c for c in alive if c in want]
    else:
        coins = [c for c in alive if c not in cfg.universe.exclude]
    candles = _fetch_1d(ctx, coins)
    # Funding basket TIDAK diambil di sini: basket hanya dipakai aturan berhenti,
    # jadi diambil setelah order dan pesan terkirim (_finish_bench).
    return mom.market_view(candles, meta, exec_day, cfg, None)


def _fetch_1d(ctx: Ctx, coins: list, alarm: bool = True) -> dict:
    """Candle 1d per koin. Gagal sebagian -> DataIncomplete sampai FETCH_RETRY_UNTIL_H,
    setelah itu jalan tanpa koin itu (+ alarm Telegram kalau alarm=True)."""
    cfg = ctx.cfg
    now_ms = int(ctx.now.timestamp() * 1000)
    candles, failed = {}, []
    for c in coins:
        if c in ctx._c1d:
            candles[c] = ctx._c1d[c]
            continue
        days = cfg.universe.btc_fetch_days if c == cfg.momentum.regime_symbol else cfg.universe.fetch_days
        try:
            # include_open: open candle hari ini dipakai basket (open E-1 -> open E)
            candles[c] = ctx._c1d[c] = ctx.info.candles(c, "1d", now_ms - days * DAY_MS, include_open=True)
        except Exception as e:  # noqa: BLE001
            if c == cfg.momentum.regime_symbol:
                raise
            failed.append(f"{c} ({type(e).__name__})")
    if failed:
        names = ", ".join(failed[:20])
        give_up = utc_day(ctx.now) + pd.Timedelta(hours=FETCH_RETRY_UNTIL_H)
        if pd.Timestamp(ctx.now).tz_convert("UTC").tz_localize(None) < give_up:
            raise DataIncomplete(f"candle 1d gagal diambil untuk {len(failed)} koin ({names}); "
                                 f"dicoba ulang sampai {FETCH_RETRY_UNTIL_H:02d}:00 UTC")
        ctx.say(f"[momentum] candle gagal diambil: {names}")
        if alarm:
            ctx.outbox.add(f"🚨 <b>RMF — data candle tidak lengkap</b>\n"
                           f"Masih gagal setelah {FETCH_RETRY_UNTIL_H:02d}:00 UTC: {notify.esc(names)}\n"
                           "Siklus tetap jalan: koin itu dianggap tidak eligible (posisinya dijual). "
                           "Cek manual.")
    return candles


def _finish_bench(ctx: Ctx, view: mom.View, view_file: str = "momentum_view.json") -> None:
    """Kurangi funding riil dari return basket (definisi riset xsec_hl.py)."""
    if view.bench_net or not ctx.cfg.benchmark.include_funding or not view.bench_coins:
        view.bench_net = True
        return
    day = pd.Timestamp(view.exec_day)
    start = int(pd.Timestamp(day - pd.Timedelta(days=1), tz="UTC").value // 10**6)
    end = start + DAY_MS - 1
    total, miss = 0.0, 0
    for c in view.bench_coins:
        try:
            total += sum(r for _, r in ctx.info.funding_history(c, start, end))
        except Exception:  # noqa: BLE001
            miss += 1
    if miss:
        ctx.say(f"[momentum] funding basket gagal untuk {miss} koin (dianggap 0)")
    view.bench_ret = view.bench_gross - total / len(view.bench_coins)
    view.bench_net = True
    store.save_json(view_file, view.to_dict())


def _accrue_funding(ctx: Ctx, book: dict, mids: dict) -> float:
    """Funding riil HYPE untuk posisi paper sejak terakhir dihitung."""
    total = 0.0
    now_ms = int(ctx.now.timestamp() * 1000)
    for coin, p in book["positions"].items():
        start = int(p.get("funding_ms") or pd.Timestamp(p["entry_time"]).value // 10**6)
        if now_ms - start < 3_600_000:
            continue
        try:
            rates = ctx.info.funding_history(coin, start + 1, now_ms)
        except Exception as e:  # noqa: BLE001
            ctx.say(f"[momentum] funding {coin} gagal ({type(e).__name__}); dicoba besok")
            continue
        amt = bk.funding_cost(p["qty"], mids.get(coin, p["entry_px"]), rates)
        bk.charge_funding(book, coin, amt)
        p["funding_ms"] = max([t for t, _ in rates], default=start)
        total += amt
    return total


def momentum_due(ctx: Ctx) -> tuple[bool, bool, str]:
    """(paper perlu jalan, live perlu jalan, exec_day)."""
    cfg, now = ctx.cfg, ctx.now
    day = utc_day(now)
    if ctx.ctrl.momentum == "off" or not started(cfg, now):
        return False, False, str(day.date())
    if pd.Timestamp(now).tz_convert("UTC").tz_localize(None) < day + pd.Timedelta(minutes=cfg.momentum.run_after_minutes):
        return False, False, str(day.date())
    paper = store.load_json("momentum_paper.json") or {}
    need_paper = paper.get("last_day") != str(day.date()) or bool(paper.get("pending_record"))
    need_live = False
    if ctx.ctrl.live:
        ls = store.load_json("momentum_live.json") or {}
        tries = (ls.get("attempts") or {}).get(str(day.date()), 0)
        if ctx.ctrl.momentum == "flatten":
            need_live = bool(ls.get("positions")) or ls.get("flatten_day") != str(day.date())
        else:
            need_live = ls.get("last_day") != str(day.date()) and tries < cfg.execution.max_live_attempts_per_day
    return need_paper, need_live, str(day.date())


def run_momentum(ctx: Ctx) -> dict | None:
    need_paper, need_live, exec_day = momentum_due(ctx)
    need_roll = rolling_due(ctx, exec_day)
    if not (need_paper or need_live):
        if need_roll and store.load_json(ROLL_PAPER):   # buku utama sudah selesai hari ini
            mids = ctx.info.all_mids()
            main = (store.load_json("momentum_paper.json") or {}).get("book")
            _rolling_safe(ctx, _rolling_trade, exec_day, mids, ctx.now.isoformat(timespec="seconds"),
                          bk.equity(main, mids) if main else None)
            _rolling_safe(ctx, _rolling_finish)
        return None
    cfg = ctx.cfg
    _finish_stale_record(ctx, exec_day)
    vdoc = store.load_json("momentum_view.json")
    if vdoc and vdoc.get("exec_day") == exec_day:
        view = mom.View.from_dict(vdoc)
    else:
        view = fetch_view(ctx, pd.Timestamp(exec_day))
        store.save_json("momentum_view.json", view.to_dict())
    mids = ctx.info.all_mids()
    t = ctx.now.isoformat(timespec="seconds")
    delay = (pd.Timestamp(ctx.now).tz_convert("UTC").tz_localize(None) - pd.Timestamp(exec_day)).total_seconds() / 60
    out = {"exec_day": exec_day, "view": view, "delay_min": delay, "paper": None, "live": None}

    paper = store.load_json("momentum_paper.json") or {"book": bk.new_book(cfg.capital_usdc), "bench_index": 1.0}
    trade_paper = need_paper and paper.get("last_day") != exec_day
    if trade_paper:
        out["paper"] = _paper_day(ctx, paper, view, mids, t)
        paper["pending_record"] = {"exec_day": exec_day, "time_utc": t, "equity": out["paper"]["equity"],
                                   "gross": out["paper"]["gross"], "positions": len(out["paper"]["positions"]),
                                   "delay_min": round(delay, 1)}
    if need_live:
        out["live"] = _live_day(ctx, view, exec_day, t)
    if need_roll:                           # setelah live: buku pembanding tidak menunda order live
        if trade_paper:                     # buku utama tersimpan dulu sebelum fetch tambahan
            store.save_json("momentum_paper.json", paper)
        static_eq = out["paper"]["equity_before"] if out["paper"] else bk.equity(paper["book"], mids)
        out["rolling"] = _rolling_safe(ctx, _rolling_trade, exec_day, mids, t, static_eq, trade_paper)

    if trade_paper:
        # Urutan sengaja: order tercatat -> pesan dikirim SEKARANG -> baru funding
        # basket + log + aturan berhenti (±3 menit request ke HYPE).
        store.save_json("momentum_paper.json", paper)
        ctx.outbox.add(daily_message(ctx, view, out, paper, mids))
        ctx.outbox.flush()
    elif out["live"] is not None:
        ctx.outbox.add(_live_message(out["live"], ctx))
    if paper.get("pending_record"):
        vrec = view
        if view.exec_day != paper["pending_record"]["exec_day"]:
            vrec = mom.View.from_dict(store.load_json("momentum_view.json"))
        out["stop"] = _record_day(ctx, paper, vrec, mids, out)
        store.save_json("momentum_paper.json", paper)
    if need_roll:
        _rolling_safe(ctx, _rolling_finish)
    return out


def _finish_stale_record(ctx: Ctx, exec_day: str) -> None:
    """Pencatatan hari sebelumnya yang terputus diselesaikan SEBELUM hari baru
    menimpa momentum_view.json dan pending_record (audit 2026-10-05 F7)."""
    paper = store.load_json("momentum_paper.json") or {}
    rec = paper.get("pending_record")
    if not rec or rec.get("exec_day") == exec_day:
        return
    vdoc = store.load_json("momentum_view.json")
    if not vdoc or vdoc.get("exec_day") != rec["exec_day"]:
        ctx.say(f"[momentum] catatan {rec['exec_day']} tidak bisa diselesaikan (view hilang); dibuang")
        paper.pop("pending_record", None)
        store.save_json("momentum_paper.json", paper)
        return
    ctx.say(f"[momentum] menyelesaikan catatan {rec['exec_day']} yang terputus")
    _record_day(ctx, paper, mom.View.from_dict(vdoc), ctx.info.all_mids(), {})
    store.save_json("momentum_paper.json", paper)


def _paper_day(ctx: Ctx, paper: dict, view: mom.View, mids: dict, t: str,
               orders_log: str = "orders", book_name: str = "paper") -> dict:
    cfg = ctx.cfg
    b = paper["book"]
    funding = _accrue_funding(ctx, b, mids)
    bk.mark(b, mids)
    eq0 = bk.equity(b, mids)
    rb = mom.rebalance(view, list(b["positions"]), cfg)
    fills, warn = [], []
    for coin, why in rb.sells:
        mid = mids.get(coin) or b["positions"][coin].get("last_px")
        f = bk.close(b, coin, mid, cfg.costs)
        f.update(reason=why)
        fills.append(f)
    usd = mom.per_coin_usd(eq0, cfg)
    meta = ctx.meta()
    for coin in rb.buys:
        mid = mids.get(coin)
        if not mid or coin not in meta:
            warn.append(f"{coin}: mid/meta tidak ada, beli dilewati")
            continue
        qty = mom.order_size(usd, mid, meta[coin]["szDecimals"], cfg.momentum.min_order_usdc)
        f = bk.buy(b, coin, qty, mid, cfg.costs, t, extra={"funding_ms": int(ctx.now.timestamp() * 1000)})
        f.update(reason=f"peringkat {view.ranks().get(coin)}")
        fills.append(f)
    for f in fills:
        store.append(orders_log, {"time_utc": t, "book": book_name, "strategy": "momentum", "exec_day": view.exec_day,
                                "coin": f["coin"], "side": f["side"], "qty": f["qty"], "px": f["px"],
                                "mid": mids.get(f["coin"]), "notional": f["notional"], "fee": f["fee"],
                                "pnl": f.get("net_pnl", 0.0), "reason": f["reason"], "status": "paper"})
    bk.mark(b, mids)
    paper["last_day"] = view.exec_day
    return {"equity_before": eq0, "equity": bk.equity(b, mids), "gross": bk.gross(b, mids), "fills": fills,
            "warn": warn, "funding": funding, "positions": sorted(b["positions"]), "per_coin": usd,
            "skipped_buys": [c for c in rb.buys if c not in b["positions"]]}


def _live_day(ctx: Ctx, view: mom.View, exec_day: str, t: str) -> dict:
    cfg = ctx.cfg
    ls = store.load_json("momentum_live.json") or {}
    attempts = ls.setdefault("attempts", {})
    attempts = {k: v for k, v in attempts.items() if k >= exec_day}     # buang hari lama
    n = attempts.get(exec_day, 0)
    attempts[exec_day] = n + 1
    ls["attempts"] = attempts
    res = {"mode": ctx.ctrl.momentum, "errors": [], "orders": []}
    try:
        if ctx.trader_factory is None:
            raise live.Halt(f"secret API wallet ({cfg.execution.agent_secret}) belum diisi; live tidak bisa jalan")
        trader = ctx.trader_factory()
        eq_now, _ = trader.equity()
        # circuit breaker DD (aturan berhenti #1): tahan beli baru
        if ctx.ctrl.breaker_reset and ctx.ctrl.breaker_reset != ls.get("breaker_reset_seen"):
            ls["peak_equity"] = eq_now
            ls["breaker_reset_seen"] = ctx.ctrl.breaker_reset
            ctx.say(f"[live] breaker di-reset; puncak = {eq_now:.2f}")
        peak = max(float(ls.get("peak_equity") or 0), eq_now)
        ls["peak_equity"] = peak
        dd = (eq_now / peak - 1) * 100 if peak > 0 else 0.0
        breaker = dd < -cfg.stop_rules.max_drawdown_pct
        hold = ls.get("hold")
        block = breaker or bool(hold)
        why = "TAHAN: posisi ditutup di luar bot" if hold else "breaker DD aktif"
        only_iso = frozenset(c for c, m in ctx.meta().items() if m.get("onlyIsolated"))
        owned = set(ls.get("positions") or {}) | set(ls.get("pending") or [])

        def journal(coin):          # tulis dulu sebelum order: koin ini milik RMF
            ls.setdefault("pending", [])
            if coin not in ls["pending"]:
                ls["pending"].append(coin)
            store.save_json("momentum_live.json", ls)

        r = live.run_momentum(view, cfg, trader, ctx.ctrl.momentum, block, ctx.now, attempt=n,
                              only_isolated=only_iso, owned=frozenset(owned), journal=journal,
                              block_reason=why)
        res.update(r)
        res["dd_pct"], res["breaker"], res["hold"] = dd, breaker, hold
        for o in r["orders"]:
            store.append("orders", {"time_utc": t, "book": "live", "strategy": "momentum", "exec_day": exec_day,
                                    "coin": o["coin"], "side": o["side"], "qty": o["qty"], "px": o["px"],
                                    "mid": o["mid"], "notional": o["qty"] * o["px"], "fee": "",
                                    "pnl": "", "reason": o["reason"], "status": o["status"]})
        if "positions_after" in r:
            ls["positions"] = {c: v for c, v in r["positions_after"].items() if c in owned or c in (ls.get("pending") or [])}
            ls["pending"] = []
        ls["last_equity"] = r.get("equity_after", eq_now)
        ls["last_gross"] = r.get("gross_after")
        if ctx.ctrl.momentum == "flatten":
            ls["flatten_day"] = exec_day
        if not r["errors"]:
            ls["last_day"] = exec_day
    except live.Halt as e:
        res["errors"].append(f"HALT: {e}")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        res["errors"].append(f"live gagal: {type(e).__name__}")
    store.save_json("momentum_live.json", ls)
    return res


def _record_day(ctx: Ctx, paper: dict, view: mom.View, mids: dict, out: dict) -> dict:
    """Setelah pesan terkirim: funding basket, baris equity/ranking, aturan berhenti.
    Kalau job mati di tengah, pending_record membuat siklus berikutnya melanjutkan."""
    cfg = ctx.cfg
    rec = paper["pending_record"]
    b = paper["book"]
    _finish_bench(ctx, view)
    if paper.get("bench_day") != view.exec_day:          # basket 1x: satu langkah per hari
        paper["bench_index"] = float(paper.get("bench_index", 1.0)) * (1 + view.bench_ret)
        paper["bench_day"] = view.exec_day
    lv = out.get("live") or {}
    ls = store.load_json("momentum_live.json") or {}
    fb = (store.load_json("flush_paper.json") or {}).get("book")
    flush_eq = bk.equity(fb, mids) if fb else 0.0
    peak = float(b.get("peak_equity", rec["equity"]))
    store.append("equity", {
        "exec_day": view.exec_day, "time_utc": rec["time_utc"], "regime_on": view.regime_on,
        "btc_close": view.btc_close, "btc_ema": view.btc_ema, "paper_equity": rec["equity"],
        "paper_gross": rec["gross"], "paper_positions": rec["positions"],
        "live_equity": lv.get("equity_after", ls.get("last_equity")) if ctx.ctrl.live else "",
        "live_positions": len(ls.get("positions") or {}) if ctx.ctrl.live else "",
        "flush_equity": flush_eq,
        "bench_ret": view.bench_ret, "bench_index": paper["bench_index"], "paper_peak": peak,
        "paper_dd_pct": (rec["equity"] / peak - 1) * 100 if peak else 0.0, "delay_min": rec["delay_min"],
        "live_gross": lv.get("gross_after", ls.get("last_gross")) if ctx.ctrl.live else "",
        "total_equity": rec["equity"] + flush_eq})
    held_live = set((ls.get("positions") or {}).keys())
    for c, r, x in view.ranking[: max(20, cfg.momentum.exit_rank)]:
        store.append("ranking", {"exec_day": view.exec_day, "rank": r, "coin": c, "ret_14d_pct": round(x * 100, 2),
                                 "held_paper": c in b["positions"], "held_live": c in held_live}, mirror=False)
    stop = _stop_rules(ctx, paper)
    st = stop.get("paper")
    paper["months_behind"] = st.months_behind_streak if st else 0
    paper.pop("pending_record", None)
    return stop


def _stop_rules(ctx: Ctx, paper: dict) -> dict:
    rows = store.read("equity")
    res = {}
    if not rows:
        return res
    df = pd.DataFrame(rows)
    df["exec_day"] = pd.to_datetime(df["exec_day"])
    df = df.drop_duplicates("exec_day", keep="last").set_index("exec_day").sort_index()
    num = lambda c: pd.to_numeric(df[c], errors="coerce") if c in df else pd.Series(dtype=float)  # noqa: E731
    bench = num("bench_index")
    books = {"paper": (num("paper_equity"), num("paper_gross") / num("paper_equity"))}
    if ctx.ctrl.live:
        le = num("live_equity").dropna()
        if len(le):
            books["live"] = (le, (num("live_gross") / num("live_equity")).reindex(le.index))
    alerted = store.load_json("alerts.json", {}) or {}
    for name, (eq, expo) in books.items():
        st = stoprules.evaluate(eq, bench, ctx.cfg, exposure=expo)
        res[name] = st
        key = f"stop_{name}"
        sig = "|".join(sorted(x.split(" ")[0] for x in st.breaches))
        if st.breaches and alerted.get(key) != sig:
            ctx.outbox.add(f"🚨 <b>RMF — ATURAN BERHENTI tersentuh ({name})</b>\n\n"
                           + "\n".join(f"• {notify.esc(x)}" for x in st.breaches)
                           + "\n\n<i>Bot hanya menandai. Keputusan berhenti di tanganmu. "
                             "Tutup posisi live: gh workflow run control.yml -f momentum=flatten</i>")
        alerted[key] = sig
    store.save_json("alerts.json", alerted)
    return res


# --------------------------------------------------------------------------- #
#  Buku paper pembanding: universe rolling_monthly (universe.compare_rolling)
#  Hanya paper. Tidak ikut aturan berhenti, alarm Telegram, Sheets, atau live.
#  Gagal apa pun hanya dicatat di log; dicoba lagi siklus berikutnya.
# --------------------------------------------------------------------------- #
ROLL_PAPER = "momentum_paper_rolling.json"
ROLL_VIEW = "momentum_view_rolling.json"
ROLL_UNIVERSE = "universe_rolling.json"
ROLL_NAMES_MAX = 20          # nama koin masuk/keluar di pesan harian


def rolling_due(ctx: Ctx, exec_day: str) -> bool:
    cfg, now = ctx.cfg, ctx.now
    if not cfg.universe.compare_rolling or ctx.ctrl.momentum == "off" or not started(cfg, now):
        return False
    day = utc_day(now)
    if pd.Timestamp(now).tz_convert("UTC").tz_localize(None) < day + pd.Timedelta(minutes=cfg.momentum.run_after_minutes):
        return False
    st = store.load_json(ROLL_PAPER) or {}
    return st.get("last_day") != exec_day or bool(st.get("pending_record"))


def _rolling_safe(ctx: Ctx, fn, *args):
    try:
        return fn(ctx, *args)
    except Exception as e:  # noqa: BLE001
        if not isinstance(e, DataIncomplete):
            traceback.print_exc()
        ctx.say(f"[rolling] {fn.__name__} gagal ({type(e).__name__}: {str(e)[:200]}); dicoba siklus berikutnya")
        return None


def _rolling_view(ctx: Ctx, exec_day: str) -> mom.View:
    """View dengan universe bulanan. Refresh keanggotaan (sekali per bulan) di sini."""
    cfg = ctx.cfg
    vdoc = store.load_json(ROLL_VIEW)
    if vdoc and vdoc.get("exec_day") == exec_day:
        return mom.View.from_dict(vdoc)
    meta = ctx.meta()
    coins = [c for c, m in meta.items() if not m["isDelisted"] and c not in cfg.universe.exclude]
    if cfg.momentum.regime_symbol not in coins:
        coins.append(cfg.momentum.regime_symbol)
    candles = _fetch_1d(ctx, coins, alarm=False)
    day = pd.Timestamp(exec_day)
    # Belum ada state: mulai dari daftar riset, lalu refresh pertama langsung jalan.
    uni = store.load_json(ROLL_UNIVERSE) or {"members": mom.static_universe(cfg), "last_refresh": None, "log": []}
    uni, entry = mom.rolling_update(uni, mom.closed_only(candles, day - pd.Timedelta(days=1)), meta, day, cfg)
    if entry is not None:
        store.save_json(ROLL_UNIVERSE, uni)
        ctx.say(f"[rolling] universe diperbarui: masuk {len(entry[1])}, keluar {len(entry[2])}, "
                f"{len(uni['members'])} anggota")
    rcfg = replace(cfg, universe=replace(cfg.universe, mode="rolling_monthly"))
    view = mom.market_view(candles, meta, day, rcfg, None, members=uni["members"])
    store.save_json(ROLL_VIEW, view.to_dict())
    return view


def _rolling_trade(ctx: Ctx, exec_day: str, mids: dict, t: str, static_eq: float | None,
                   new_ok: bool = False) -> dict | None:
    """Satu hari buku pembanding: aturan momentum sama persis, universe bulanan.

    Buku baru hanya dibuat kalau new_ok (siklus yang sama dengan trading harian buku
    utama, ±07:02 WIB): flag yang dinyalakan siang hari mulai di open berikutnya,
    bukan di harga siang."""
    cfg = ctx.cfg
    st = store.load_json(ROLL_PAPER)
    if st is None and not new_ok:
        return None
    if st and st.get("last_day") == exec_day:
        return None
    if st and st.get("pending_record"):
        _rolling_finish(ctx)
        st = store.load_json(ROLL_PAPER)
    st = st or {"book": bk.new_book(cfg.capital_usdc), "bench_index": 1.0,
                "start_day": exec_day, "static_start_equity": static_eq}
    view = _rolling_view(ctx, exec_day)
    res = _paper_day(ctx, st, view, mids, t, orders_log="orders_rolling", book_name="paper_rolling")
    st["pending_record"] = {"exec_day": exec_day, "time_utc": t, "equity": res["equity"], "gross": res["gross"],
                            "positions": len(res["positions"]), "static_equity": static_eq}
    store.save_json(ROLL_PAPER, st)
    uni = store.load_json(ROLL_UNIVERSE) or {}
    log = uni.get("log") or []
    res.update(universe_size=view.universe_size, start_day=st["start_day"],
               start_capital=st["book"]["start_capital"], static_start_equity=st.get("static_start_equity"),
               refresh=log[-1] if log and uni.get("last_refresh") == exec_day else None)
    return res


def _rolling_finish(ctx: Ctx) -> None:
    """Setelah pesan: funding basket + baris equity_rolling.csv (tanpa aturan berhenti)."""
    st = store.load_json(ROLL_PAPER) or {}
    rec = st.get("pending_record")
    if not rec:
        return
    vdoc = store.load_json(ROLL_VIEW)
    if not vdoc or vdoc.get("exec_day") != rec["exec_day"]:
        ctx.say(f"[rolling] catatan {rec['exec_day']} tidak bisa diselesaikan (view hilang); dibuang")
        st.pop("pending_record", None)
        store.save_json(ROLL_PAPER, st)
        return
    view = mom.View.from_dict(vdoc)
    _finish_bench(ctx, view, ROLL_VIEW)
    if st.get("bench_day") != view.exec_day:
        st["bench_index"] = float(st.get("bench_index", 1.0)) * (1 + view.bench_ret)
        st["bench_day"] = view.exec_day
    peak = float(st["book"].get("peak_equity", rec["equity"]))
    store.append("equity_rolling", {
        "exec_day": view.exec_day, "time_utc": rec["time_utc"], "regime_on": view.regime_on,
        "paper_equity": rec["equity"], "paper_gross": rec["gross"], "paper_positions": rec["positions"],
        "universe_size": view.universe_size, "bench_ret": view.bench_ret, "bench_index": st["bench_index"],
        "paper_peak": peak, "paper_dd_pct": (rec["equity"] / peak - 1) * 100 if peak else 0.0,
        "static_equity": rec.get("static_equity")}, mirror=False)
    st.pop("pending_record", None)
    store.save_json(ROLL_PAPER, st)


def _rolling_lines(rl: dict, static_eq: float) -> list:
    ret = (rl["equity"] / rl["start_capital"] - 1) * 100
    s = f"  pembanding universe bulanan (paper): {notify.usd(rl['equity'])} USDC ({ret:+.2f}%"
    if rl.get("static_start_equity"):
        s += f" vs static {(static_eq / rl['static_start_equity'] - 1) * 100:+.2f}%"
    lines = [s + f" sejak {rl['start_day']}) · {len(rl['positions'])} posisi"]
    if rl.get("refresh"):
        _, ins, outs = rl["refresh"]

        def names(xs):
            more = f" +{len(xs) - ROLL_NAMES_MAX} lagi" if len(xs) > ROLL_NAMES_MAX else ""
            return notify.esc(", ".join(xs[:ROLL_NAMES_MAX]) + more) if xs else "-"
        lines.append(f"  universe bulanan diperbarui: masuk {len(ins)} ({names(ins)}) · "
                     f"keluar {len(outs)} ({names(outs)}) · {rl['universe_size']} koin")
    return lines


POS_NAMES_MAX = 15        # batas koin per baris posisi (pesan Telegram maks 4000 karakter)


def _pos_list(book: dict, mids: dict) -> str:
    """'ZEC (+4.4%) XMR (-2.6%) ...', urut dari yang paling untung. Persen = gerak
    harga sejak entry (mid sekarang, atau harga terakhir yang diketahui)."""
    rows = []
    for coin, p in book["positions"].items():
        px = mids.get(coin) or p.get("last_px") or p["entry_px"]
        rows.append((float(px) / float(p["entry_px"]) - 1, coin))
    rows.sort(key=lambda x: (-x[0], x[1]))
    s = " ".join(f"{notify.esc(c)} ({r * 100:+.1f}%)" for r, c in rows[:POS_NAMES_MAX])
    return s + (f" +{len(rows) - POS_NAMES_MAX} lagi" if len(rows) > POS_NAMES_MAX else "")


def _total_peak() -> float:
    """Puncak ekuitas gabungan (momentum + flush) dari snapshot harian equity.csv."""
    vals = [v for v in (store.total_equity(r) for r in store.read("equity")) if v is not None]
    return max(vals, default=0.0)


def _flush_closed_since(since: pd.Timestamp) -> list:
    out = []
    for r in store.read("flush_trades"):
        try:
            if pd.Timestamp(r["exit_time"]) > since:
                out.append(r)
        except (ValueError, TypeError):
            continue
    return out


def _dd(pct: float) -> str:
    """Drawdown dari puncak sebagai besaran positif: -22.2 -> '22.2%'."""
    return f"{abs(min(pct, 0.0)):.1f}%"


def daily_message(ctx: Ctx, view: mom.View, out: dict, paper: dict, mids: dict) -> str:
    """Laporan harian: total paper (momentum + flush) di atas, lalu satu blok per
    strategi dengan posisi terbuka dan gerak harganya sejak entry."""
    cfg = ctx.cfg
    p = out["paper"]
    b = paper["book"]
    start = float(b.get("start_capital", cfg.capital_usdc))
    m_peak = float(b.get("peak_equity", p["equity"]))
    m_dd = (p["equity"] / m_peak - 1) * 100 if m_peak else 0.0
    m_ret = (p["equity"] / start - 1) * 100 if start else 0.0
    fb = (store.load_json("flush_paper.json") or {}).get("book")
    f_eq = bk.equity(fb, mids) if fb else 0.0
    total = p["equity"] + f_eq
    t_peak = max(_total_peak(), total, start)
    t_ret = (total / start - 1) * 100 if start else 0.0
    t_dd = (total / t_peak - 1) * 100 if t_peak else 0.0

    day_n = ""
    if cfg.forward_start:
        n = (pd.Timestamp(view.exec_day) - pd.Timestamp(cfg.forward_start)).days + 1
        day_n = f" · hari ke-{n}"
    gap = (view.btc_close / view.btc_ema - 1) * 100 if view.btc_ema else 0.0
    reg = "🟢 ON" if view.regime_on else "🔴 OFF"
    lines = [
        f"📊 <b>RMF paper — laporan harian{day_n}</b>",
        f"<code>{notify.wib(ctx.now)} · candle {view.last_close_day} · telat {out['delay_min']:.0f} mnt</code>",
        "",
        f"💰 <b>Total {notify.usd(total)} USDC</b> · PnL {t_ret:+.1f}% · DD {_dd(t_dd)}",
        f"   momentum {notify.usd(p['equity'])} + flush {f_eq:+.2f}",
        "",
        f"<b>Momentum Report</b> · {m_ret:+.1f}% · DD {_dd(m_dd)}",
        f"Filter BTC {reg} · {view.btc_close:,.0f} vs EMA50 {view.btc_ema:,.0f} ({gap:+.1f}%)",
    ]
    mbook = {"positions": {c: b["positions"][c] for c in p["positions"] if c in b["positions"]}}
    if mbook["positions"]:
        lines.append(f"🟢 Long ({len(mbook['positions'])}): {_pos_list(mbook, mids)}")
        lines.append(f"   gross {notify.usd(p['gross'])} · {notify.usd(p['per_coin'])}/koin")
    elif not view.regime_on:
        lines.append("⚪ Cash · masuk lagi kalau BTC close > EMA50")
    else:
        lines.append("⚪ Tidak ada posisi")
    sells = [f"{f['coin']} {f.get('net_pnl', 0):+.2f} ({f['reason']})" for f in p["fills"] if f["side"] == "SELL"]
    buys = [f["coin"] for f in p["fills"] if f["side"] == "BUY"]
    if sells:
        lines.append(f"🔴 Jual: {notify.esc(' · '.join(sells))}")
    if buys:
        lines.append(f"➕ Beli: {notify.esc(' '.join(buys))}")
    if not sells and not buys:
        lines.append("   tidak ada perubahan posisi")
    top = " ".join(c for c, r, _ in view.ranking[: cfg.momentum.n_hold])
    res = " ".join(c for c, r, _ in view.ranking[cfg.momentum.n_hold: cfg.momentum.exit_rank])
    lines.append(f"   top {cfg.momentum.n_hold}: {notify.esc(top) or '-'}")
    lines.append(f"   cadangan {cfg.momentum.n_hold + 1}–{cfg.momentum.exit_rank}: {notify.esc(res) or '-'}")
    for w in p["warn"]:
        lines.append("⚠️ " + notify.esc(w))

    lines += ["", f"<b>Flush Report</b> (4h) · PnL {f_eq:+.2f} USDC"]
    if fb and fb.get("positions"):
        lines.append(f"🟢 Long ({len(fb['positions'])}): {_pos_list(fb, mids)}")
    else:
        lines.append("⚪ Tidak ada posisi")
    closed = _flush_closed_since(pd.Timestamp(ctx.now).tz_convert("UTC") - pd.Timedelta(hours=24))
    if closed:
        tot = sum(float(r["pnl_net"] or 0) for r in closed)
        items = " · ".join(f"{r['coin']} {r['reason']} {float(r['pnl_net'] or 0):+.2f}" for r in closed)
        lines.append(f"✅ Tutup 24 jam: {notify.esc(items)} (total {tot:+.2f})")

    lines.append("")
    if view.bench_net:
        lines.append(f"Basket semua koin ({view.bench_n} koin, 1x): {view.bench_ret * 100:+.2f}%")
    else:
        lines.append(f"Basket semua koin ({view.bench_n} koin, 1x): {view.bench_gross * 100:+.2f}% "
                     "(sebelum funding)")
    if paper.get("months_behind"):
        lines.append(f"Kalah dari basket {paper['months_behind']} bulan berturut-turut "
                     f"(batas {cfg.stop_rules.underperform_months})")
    if out.get("rolling"):
        lines += _rolling_lines(out["rolling"], p["equity"])
    if out["live"] is not None:
        lines += ["", _live_message(out["live"], ctx, short=True)]
    else:
        lines.append(f"Live: mode <b>{ctx.ctrl.momentum}</b>")
    return "\n".join(lines)


def daily_message_from_state(ctx: Ctx) -> str | None:
    """Bangun ulang ringkasan harian terakhir dari state/ (untuk kirim ulang / smoke test)."""
    vdoc = store.load_json("momentum_view.json")
    paper = store.load_json("momentum_paper.json")
    eq = [r for r in store.read("equity") if vdoc and r["exec_day"] == vdoc["exec_day"]]
    if not (vdoc and paper and eq):
        return None
    view, row = mom.View.from_dict(vdoc), eq[-1]
    fills = [{"coin": o["coin"], "side": o["side"], "reason": o["reason"], "net_pnl": float(o["pnl"] or 0)}
             for o in store.read("orders")
             if o["book"] == "paper" and o["strategy"] == "momentum" and o["exec_day"] == view.exec_day]
    equity = float(row["paper_equity"])
    out = {"delay_min": float(row["delay_min"] or 0), "live": None, "paper": {
        "equity": equity, "gross": float(row["paper_gross"]), "fills": fills, "warn": [],
        "positions": list(paper["book"]["positions"]), "per_coin": mom.per_coin_usd(equity, ctx.cfg)}}
    saved_now, ctx.now = ctx.now, dt.datetime.fromisoformat(row["time_utc"])
    try:
        return daily_message(ctx, view, out, paper, {})
    finally:
        ctx.now = saved_now


def _live_message(lv: dict, ctx: Ctx, short: bool = False) -> str:
    head = "" if short else f"🤖 <b>RMF live — {notify.wib(ctx.now)}</b>\n"
    s = (f"{head}<b>Live</b> ({lv.get('mode')}): ekuitas {notify.usd(lv.get('equity_after'))} USDC"
         f" · DD {lv.get('dd_pct', 0):+.1f}% · {len(lv.get('orders', []))} order")
    if lv.get("hold"):
        s += ("\n🛑 TAHAN aktif sejak " + notify.wib(lv["hold"]["since"]) + ": tidak ada pembelian, jual sesuai "
              "aturan tetap jalan (aktifkan kembali: control.yml -f resume=true)")
    if lv.get("breaker"):
        s += f"\n🛑 Breaker DD aktif: beli baru ditahan (reset: control.yml -f reset_breaker=true)"
    for o in lv.get("orders", []):
        s += f"\n  {o['side']} {notify.esc(o['coin'])} {o['qty']} @ {o['px']} ({o['status']})"
    for e in lv.get("errors", [])[:10]:
        s += f"\n⚠️ {notify.esc(e)}"
    return s


# =========================================================================== #
#  FLUSH (4h, paper)
# =========================================================================== #
def latest_bar(now: dt.datetime, run_after_min: int) -> pd.Timestamp:
    """Waktu open candle 4h terakhir yang sudah close (UTC, tz-aware)."""
    t = pd.Timestamp(now).tz_convert("UTC") - pd.Timedelta(minutes=run_after_min)
    return t.floor("4h") - H4


def _symbols(cfg) -> list:
    f = cfg.flush
    p = path_in_repo(f.spot_universe_file if f.signal_source == "binance_spot" else f.futures_universe_file)
    with open(p, encoding="utf-8") as fh:
        return [x.strip() for x in fh if x.strip() and not x.startswith("#")]


def run_flush(ctx: Ctx) -> dict | None:
    cfg = ctx.cfg
    if ctx.ctrl.flush == "off" or not started(cfg, ctx.now):
        return None
    bar = latest_bar(ctx.now, cfg.momentum.run_after_minutes)
    st = store.load_json("flush_paper.json") or {"book": bk.new_book(0.0), "last_bar": None, "events": 0}
    if st.get("last_bar") == bar.isoformat():
        return None
    t = ctx.now.isoformat(timespec="seconds")
    out = {"bar": bar.isoformat(), "exits": [], "event": False, "opened": [], "rejected": []}
    out["exits"] = _flush_exits(ctx, st, t)

    # ---- sinyal di bar yang baru close
    sig, n_sym = [], 0
    for sym in _symbols(cfg):
        try:
            k = ctx.klines.klines(sym, "4h", max(cfg.flush.bb_len, cfg.flush.vol_len) + 40)
        except Exception as e:  # noqa: BLE001
            ctx.say(f"[flush] {sym} gagal ({type(e).__name__})")
            continue
        # Data bisa sudah memuat candle yang lebih baru dari `bar` (mis. run tepat
        # setelah close, sebelum jeda run_after habis): nilai sinyal DI bar itu.
        if not k.empty:
            k = k[pd.DatetimeIndex(k["ts"]) <= bar]
        if k.empty or pd.Timestamp(k["ts"].iloc[-1]) != bar:
            continue
        n_sym += 1
        if fl.signal_at_last(k, cfg):
            sig.append(sym)
    event = len(sig) >= cfg.flush.min_coins
    out.update(n_signals=len(sig), n_symbols=n_sym, event=event, signals=sig)
    store.append("flush_signals", {"bar_utc": bar.isoformat(), "time_utc": t, "n_signals": len(sig),
                                   "n_symbols": n_sym, "event": event, "coins": " ".join(sig)},
                 mirror=event)
    if n_sym < 0.8 * len(_symbols(cfg)):
        ctx.say(f"[flush] hanya {n_sym} simbol punya candle {bar}; sumber data bermasalah?")
    if event:
        _flush_event(ctx, st, bar, sig, out, t)
    st["last_bar"] = bar.isoformat()
    store.save_json("flush_paper.json", st)
    return out


def _flush_exits(ctx: Ctx, st: dict, t: str) -> list:
    cfg = ctx.cfg
    b = st["book"]
    done = []
    now_ms = int(ctx.now.timestamp() * 1000)
    for coin in list(b["positions"]):
        p = b["positions"][coin]
        start = pd.Timestamp(p["entry_bar"])
        try:
            bars = ctx.info.candles(coin, "4h", int(start.value // 10**6))
        except Exception as e:  # noqa: BLE001
            ctx.say(f"[flush] candle {coin} gagal ({type(e).__name__}); exit dicek siklus berikutnya")
            continue
        ex, p2 = fl.check_exit(p, bars, cfg.flush.max_bars)
        b["positions"][coin].update(p2)
        if not ex:
            continue
        try:
            rates = ctx.info.funding_history(coin, int(pd.Timestamp(p["entry_time"]).value // 10**6),
                                             min(now_ms, int(pd.Timestamp(ex["exit_time"]).value // 10**6)))
        except Exception:  # noqa: BLE001
            rates = []
        fund = bk.funding_cost(p["qty"], p["entry_px"], rates)
        bk.charge_funding(b, coin, fund)
        f = bk.close(b, coin, ex["exit_px"], cfg.costs, exact_px=ex["exit_px"])
        pos = f["position"]
        fees = pos.get("fees", 0) + f["fee"]
        net = f["pnl"] - fees - fund
        row = {"entry_time": pos["entry_time"], "exit_time": ex["exit_time"], "coin": coin,
               "signal_bar": pos.get("signal_bar"), "entry_px": pos["entry_px"], "exit_px": f["px"],
               "stop": pos["stop"], "target": pos["target"], "qty": pos["qty"], "notional": pos["qty"] * pos["entry_px"],
               "risk_usd": pos["risk_usd"], "forced_min": pos.get("forced_min"), "bars_held": pos.get("bars_held"),
               "reason": ex["reason"], "pnl_gross": f["pnl"], "fees": fees, "funding": fund, "pnl_net": net,
               "r_multiple": net / pos["risk_usd"] if pos.get("risk_usd") else ""}
        store.append("flush_trades", row)
        store.append("orders", {"time_utc": t, "book": "paper", "strategy": "flush", "exec_day": ex["exit_bar"],
                                "coin": coin, "side": "SELL", "qty": pos["qty"], "px": f["px"], "mid": "",
                                "notional": f["notional"], "fee": f["fee"], "pnl": net, "reason": ex["reason"],
                                "status": "paper"})
        done.append(row)
    if done:
        tot = sum(r["pnl_net"] for r in done)
        lines = [f"🌊 <b>RMF flush (paper) — exit {len(done)}</b> · {tot:+.2f} USDC"]
        for r in done:
            rm = r["r_multiple"]
            rtxt = f" ({rm:+.2f}R)" if rm != "" else ""
            lines.append(f"  {notify.esc(r['coin'])}: {r['reason']} · {r['pnl_net']:+.2f} USDC{rtxt}")
        lines.append(_total_line(ctx, b))
        ctx.outbox.add("\n".join(lines))
    return done


def _total_line(ctx: Ctx, fbook: dict) -> str:
    """Total paper (momentum + flush) untuk pesan flush. Tanpa mid (gagal ambil),
    posisi dinilai di harga terakhir yang diketahui."""
    try:
        mids = ctx.info.all_mids()
    except Exception:  # noqa: BLE001
        mids = {}
    mbook = (store.load_json("momentum_paper.json") or {}).get("book") or bk.new_book(ctx.cfg.capital_usdc)
    m_eq, f_eq = bk.equity(mbook, mids), bk.equity(fbook, mids)
    return (f"💰 Total paper {notify.usd(m_eq + f_eq)} USDC "
            f"(momentum {notify.usd(m_eq)} + flush {f_eq:+.2f})")


def _flush_event(ctx: Ctx, st: dict, bar: pd.Timestamp, sig: list, out: dict, t: str) -> None:
    cfg = ctx.cfg
    b = st["book"]
    meta = ctx.meta()
    names = {}
    for s in sig:
        h = fl.hype_name(s)
        if h in meta and not meta[h]["isDelisted"] and h not in b["positions"]:
            names[h] = s
    chosen = fl.pick(list(names), bar, cfg.flush.max_coins)
    mids = ctx.info.all_mids()
    now_ms = int(ctx.now.timestamp() * 1000)
    cands = []
    for c in chosen:
        try:
            k = ctx.info.candles(c, "4h", now_ms - cfg.flush.atr_history_bars * 4 * 3_600_000)
            a, _ = fl.atr_at(k, bar, cfg.flush.atr_len)
        except Exception as e:  # noqa: BLE001
            ctx.say(f"[flush] ATR {c} gagal ({type(e).__name__})")
            a = float("nan")
        cands.append((c, mids.get(c, float("nan")), a))
    # ekuitas gabungan (momentum paper + PnL flush), seperti simulasi akun di riset
    mp = store.load_json("momentum_paper.json") or {}
    mbook = mp.get("book") or bk.new_book(cfg.capital_usdc)
    eq = bk.equity(mbook, mids) + bk.equity(b, mids)
    gross_used = bk.gross(mbook, mids) + bk.gross(b, mids)
    plans, rej = fl.size_event(cands, eq, gross_used, cfg)
    entry_bar = bar + H4
    late = pd.Timestamp(ctx.now).tz_convert("UTC") - entry_bar > pd.Timedelta(minutes=15)
    for pl in plans:
        qty = pl.notional / pl.entry_px
        f = bk.buy(b, pl.coin, qty, pl.entry_px, cfg.costs, t, extra={
            "entry_bar": entry_bar.isoformat(), "signal_bar": bar.isoformat(), "stop": pl.stop,
            "target": pl.target, "atr": pl.atr, "risk_usd": pl.risk_usd, "forced_min": pl.forced_min,
            # entry telat: candle entry sebagian sudah lewat sebelum masuk, jadi
            # range-nya tidak dipakai untuk SL/TP; dihitung mulai candle berikutnya.
            "last_bar": entry_bar.isoformat() if late else None, "bars_held": 1 if late else 0})
        store.append("orders", {"time_utc": t, "book": "paper", "strategy": "flush", "exec_day": bar.isoformat(),
                                "coin": pl.coin, "side": "BUY", "qty": qty, "px": f["px"], "mid": pl.entry_px,
                                "notional": f["notional"], "fee": f["fee"], "pnl": 0.0,
                                "reason": f"event {len(sig)} koin", "status": "paper"})
    st["events"] = int(st.get("events", 0)) + 1
    out["opened"] = [p.coin for p in plans]
    out["rejected"] = rej
    lines = [f"🌊 <b>RMF flush EVENT (paper)</b> — {len(sig)} koin bersinyal",
             f"<code>candle 4h {notify.wib(bar)} · sumber {cfg.flush.signal_source}</code>",
             f"Ekuitas gabungan {notify.usd(eq)} USDC · risiko {cfg.flush.risk_pct:g}%/koin",
             "Masuk: " + (notify.esc(", ".join(f"{p.coin} ({p.notional:.0f}$)" for p in plans)) or "-")]
    if rej:
        lines.append("Ditolak: " + notify.esc("; ".join(f"{c}: {w}" for c, w in rej)))
    ctx.outbox.add("\n".join(lines))


# =========================================================================== #
#  TAHAN: posisi RMF ditutup di luar bot (manual, likuidasi, ADL, delisting)
# =========================================================================== #
def watch_external_close(ctx: Ctx) -> dict | None:
    """Dipanggil SETIAP siklus selama mode live/manage/flatten, sebelum job momentum.

    Membandingkan posisi akun (hanya baca, tanpa kunci) dengan catatan posisi
    RMF di momentum_live.json. Posisi RMF yang hilang/mengecil padahal bot tidak
    menjualnya = ditutup di luar bot -> status TAHAN: tidak ada pembelian sampai
    pemilik mengaktifkan kembali (control.yml -f resume=true); jual sesuai aturan
    tetap jalan. Telegram dikirim SAAT ITU JUGA. Keputusan pemilik 2026-10-05.
    """
    ls = store.load_json("momentum_live.json")
    if ls is None:
        return None
    changed = False
    # Lepas TAHAN: nilai resume BARU di control/bot.yaml (pola sama dengan breaker_reset).
    if ctx.ctrl.resume and ctx.ctrl.resume != ls.get("resume_seen"):
        ls["resume_seen"] = ctx.ctrl.resume
        if ls.pop("hold", None):
            ctx.outbox.add("▶️ <b>RMF — TAHAN dilepas</b>\nPembelian normal lagi mulai siklus harian "
                           "berikutnya (±07:02 WIB).")
        changed = True
    rec = {c: float(v) for c, v in (ls.get("positions") or {}).items() if float(v) > 0}
    acct = ctx.cfg.execution.account_address
    if rec and acct:
        st = ctx.info.clearinghouse(acct)
        actual = {ap["position"]["coin"]: float(ap["position"]["szi"]) for ap in st.get("assetPositions", [])}
        gone = {}
        for c, want in rec.items():
            have = max(actual.get(c, 0.0), 0.0)
            if have < want * (1 - 1e-6):
                gone[c] = (want, have)
        if gone:
            for c, (_, have) in gone.items():
                if have > 0:
                    ls["positions"][c] = have
                else:
                    ls["positions"].pop(c, None)
            now = ctx.now.isoformat(timespec="seconds")
            hold = ls.get("hold") or {"since": now, "coins": {}}
            for c, (want, have) in gone.items():
                hold["coins"][c] = {"tercatat": want, "sekarang": have, "waktu": now}
            ls["hold"] = hold
            changed = True
            lines = ", ".join(f"{notify.esc(c)} ({'tutup penuh' if h == 0 else f'{w:g} → {h:g}'})"
                              for c, (w, h) in sorted(gone.items()))
            ctx.outbox.add(f"🛑 <b>RMF — posisi ditutup di luar bot</b>\n{lines}\n\n"
                           "Status <b>TAHAN</b>: bot TIDAK membeli apa pun sampai kamu aktifkan kembali. "
                           "Sisa posisi RMF tetap dijual sesuai aturan.\n"
                           "Penyebab bisa: tutup manual, likuidasi, ADL, atau delisting.\n"
                           "<i>Aktifkan kembali: gh workflow run control.yml -f resume=true</i>")
    if changed:
        store.save_json("momentum_live.json", ls)
        ctx.outbox.flush()                       # kirim sekarang, bukan di akhir siklus
    return ls.get("hold")


def agent_key() -> str:
    return os.environ.get("RMF_AGENT_KEY", "").strip()
