"""End-to-end offline: siklus momentum harian + event flush 4h + exit, dengan data tiruan."""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from fakes import FakeInfo, FakeKlines, meta_for, universe_candles
from rmf import control, jobs, notify, store

SIGNAL_BAR = pd.Timestamp("2026-10-03 20:00", tz="UTC")


def flush_frame(end=SIGNAL_BAR, seed=0):
    rng = np.random.default_rng(seed)
    c = list(100 + rng.normal(0, 0.3, 40)) + [96.0, 99.5]
    v = [100.0] * 41 + [500.0]
    ts = pd.date_range(end=end, periods=len(c), freq="4h")
    c = np.asarray(c)
    return pd.DataFrame({"ts": ts, "open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": v})


@pytest.fixture
def world(monkeypatch):
    c1d = universe_candles()
    spot = [f"S{i:02d}USDT" for i in range(12)]
    c4h = {f"S{i:02d}": flush_frame(seed=i) for i in range(12)}
    meta = meta_for(c1d)
    meta.update({k: {"szDecimals": 1, "maxLeverage": 5, "isDelisted": False, "onlyIsolated": False,
                     "markPx": 99.5, "midPx": 99.5, "dayNtlVlm": 1.0} for k in c4h})
    mids = {c: float(df["close"].iloc[-1]) for c, df in c1d.items()}
    mids.update({k: 99.5 for k in c4h})
    info = FakeInfo(c1d, c4h, meta=meta, mids=mids, funding=0.00001)
    kl = FakeKlines({s: flush_frame(seed=i) for i, s in enumerate(spot)})
    monkeypatch.setattr(jobs, "_symbols", lambda cfg: spot)
    return info, kl


def ctx_at(cfg, info, kl, now, momentum="paper", flush="paper", factory=None):
    return jobs.Ctx(cfg=cfg, info=info, klines=kl, ctrl=control.Control(momentum=momentum, flush=flush),
                    outbox=notify.Outbox(now), now=now, trader_factory=factory)


def test_momentum_day_once(state_dir, cfg, world):
    info, kl = world
    early = dt.datetime(2026, 10, 4, 0, 1, tzinfo=dt.timezone.utc)
    assert jobs.run_momentum(ctx_at(cfg, info, kl, early)) is None          # candle belum final
    now = dt.datetime(2026, 10, 4, 0, 12, tzinfo=dt.timezone.utc)
    c = ctx_at(cfg, info, kl, now)
    out = jobs.run_momentum(c)
    assert out and out["paper"]["positions"] and len(out["paper"]["positions"]) == 10
    assert len([o for o in store.read("orders") if o["side"] == "BUY"]) == 10
    eq = store.read("equity")
    assert len(eq) == 1 and eq[0]["regime_on"] == "1"
    # pesan harian langsung dikirim (bukan menunggu akhir siklus)
    assert any("laporan harian" in m["text"] for m in c.outbox.sent)
    assert store.load_json("momentum_paper.json").get("pending_record") is None
    # siklus berikutnya di hari yang sama: tidak ada apa-apa
    assert jobs.run_momentum(ctx_at(cfg, info, kl, now + dt.timedelta(minutes=10))) is None
    assert len(store.read("equity")) == 1


def test_live_without_secret_reports_halt_but_paper_runs(state_dir, cfg, world):
    info, kl = world
    now = dt.datetime(2026, 10, 4, 0, 12, tzinfo=dt.timezone.utc)
    c = ctx_at(cfg, info, kl, now, momentum="live")
    out = jobs.run_momentum(c)
    assert out["paper"] and any("HALT" in e for e in out["live"]["errors"])
    ls = store.load_json("momentum_live.json")
    assert ls.get("last_day") is None and ls["attempts"]["2026-10-04"] == 1


def test_flush_event_then_exit(state_dir, cfg, world):
    info, kl = world
    now = dt.datetime(2026, 10, 4, 0, 5, tzinfo=dt.timezone.utc)
    c = ctx_at(cfg, info, kl, now)
    out = jobs.run_flush(c)
    assert out["event"] and out["n_signals"] == 12
    st = store.load_json("flush_paper.json")
    assert 0 < len(st["book"]["positions"]) <= cfg.flush.max_coins
    assert st["last_bar"] == SIGNAL_BAR.isoformat()
    assert jobs.run_flush(ctx_at(cfg, info, kl, now + dt.timedelta(minutes=10))) is None   # bar sama

    # dua candle berikutnya: candle kedua menembus target
    for k, df in info.c4h.items():
        nxt = pd.DataFrame({"ts": [SIGNAL_BAR + pd.Timedelta(hours=4), SIGNAL_BAR + pd.Timedelta(hours=8)],
                            "open": [99.5, 100.0], "high": [100.0, 130.0], "low": [99.0, 99.8],
                            "close": [99.8, 120.0], "volume": [100.0, 100.0]})
        info.c4h[k] = pd.concat([df, nxt], ignore_index=True)
    later = dt.datetime(2026, 10, 4, 8, 5, tzinfo=dt.timezone.utc)
    out2 = jobs.run_flush(ctx_at(cfg, info, kl, later))
    assert len(out2["exits"]) == len(st["book"]["positions"])
    trades = store.read("flush_trades")
    assert trades and all(t["reason"] == "TP" for t in trades)
    assert all(float(t["r_multiple"]) > 0.8 for t in trades)     # TP 2 ATR = ~1R dikurangi biaya
    assert store.load_json("flush_paper.json")["book"]["positions"] == {}


def test_flush_sizing_uses_combined_equity(state_dir, cfg, world):
    info, kl = world
    jobs.run_momentum(ctx_at(cfg, info, kl, dt.datetime(2026, 10, 4, 0, 3, tzinfo=dt.timezone.utc)))
    out = jobs.run_flush(ctx_at(cfg, info, kl, dt.datetime(2026, 10, 4, 0, 5, tzinfo=dt.timezone.utc)))
    st = store.load_json("flush_paper.json")
    risk = [p["risk_usd"] for p in st["book"]["positions"].values()]
    assert risk and max(risk) <= 2 * cfg.capital_usdc * cfg.flush.risk_pct / 100 + 1e-6
    # total risiko event tidak dipotong (seperti simulasi riset, audit 2026-10-05 Q1)
    assert out["opened"]


def test_nothing_before_forward_start(state_dir, cfg, world):
    import dataclasses
    info, kl = world
    late = dataclasses.replace(cfg, forward_start="2026-10-05")
    now = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
    assert jobs.run_momentum(ctx_at(late, info, kl, now)) is None
    assert jobs.run_flush(ctx_at(late, info, kl, now)) is None
    assert store.read("orders") == []


def test_flush_evaluates_requested_bar_even_if_newer_data(state_dir, cfg, world):
    """Run 00:00:16 UTC (jeda 2 menit belum habis) -> bar = 16:00, tapi data sudah
    memuat candle 20:00. Sinyal harus dinilai di 16:00, bukan dibuang (0 simbol)."""
    info, kl = world
    for s, df in kl.frames.items():
        nxt = df.iloc[[-1]].copy()
        nxt["ts"] = nxt["ts"] + pd.Timedelta(hours=4)
        kl.frames[s] = pd.concat([df, nxt], ignore_index=True)
    now = dt.datetime(2026, 10, 4, 0, 0, 16, tzinfo=dt.timezone.utc)
    out = jobs.run_flush(ctx_at(cfg, info, kl, now))
    assert out["bar"] == pd.Timestamp("2026-10-03 16:00", tz="UTC").isoformat()
    assert out["n_symbols"] == 12


def test_live_owned_positions_persist_across_days(state_dir, cfg, world):
    """Hari 1 live membeli 10 koin -> state mencatatnya -> hari 2 tidak dianggap asing."""
    import dataclasses

    from fakes import FakeTrader
    info, kl = world
    ex = dataclasses.replace(cfg.execution, master_address="0xM", account_address="0xM",
                             agent_address="0xAGENT", agent_valid_until="2027-01-03")
    lcfg = dataclasses.replace(cfg, execution=ex)
    trader = FakeTrader(mids=info.all_mids())
    d1 = dt.datetime(2026, 10, 4, 0, 12, tzinfo=dt.timezone.utc)
    out = jobs.run_momentum(ctx_at(lcfg, info, kl, d1, momentum="live", factory=lambda: trader))
    assert not out["live"]["errors"] and len(trader.pos) == 10
    ls = store.load_json("momentum_live.json")
    assert sorted(ls["positions"]) == sorted(trader.pos) and ls["pending"] == []
    trader.pos["MEXCOIN"] = 1.0                       # posisi asing muncul (mis. MEX)
    store.save_json("momentum_live.json", {**ls, "last_day": None})      # paksa siklus live berikutnya
    d2 = d1 + dt.timedelta(minutes=10)
    out2 = jobs.run_momentum(ctx_at(lcfg, info, kl, d2, momentum="live", factory=lambda: trader))
    assert any("MEXCOIN" in e for e in out2["live"]["errors"])
    assert "MEXCOIN" in trader.pos and len(trader.sent) == 10      # tidak ada order baru



def test_message_sent_before_basket_funding(state_dir, cfg, monkeypatch):
    """Urutan: order -> pesan terkirim -> baru funding basket (±3 menit request)."""
    c1d = universe_candles(end_day="2026-10-04")          # termasuk candle hari ini (open E)
    info = FakeInfo(c1d, funding=0.0001)
    kl = FakeKlines({})
    order = []
    monkeypatch.setattr(notify, "send_now", lambda text: order.append("pesan") or True)
    real = info.funding_history

    def fh(coin, start, end=None):
        order.append("funding")
        return real(coin, start, end)
    monkeypatch.setattr(info, "funding_history", fh)
    jobs.run_momentum(ctx_at(cfg, info, kl, dt.datetime(2026, 10, 4, 0, 3, tzinfo=dt.timezone.utc)))
    # hari pertama belum ada posisi paper, jadi semua funding = funding basket
    assert "pesan" in order and "funding" in order
    assert order.index("pesan") < order.index("funding")
    v = store.load_json("momentum_view.json")
    assert v["bench_net"] and v["bench_n"] > 0
    assert abs(v["bench_ret"] - (v["bench_gross"] - 0.0001 * 24)) < 1e-9   # 24 jam funding
    row = store.read("equity")[-1]
    assert abs(float(row["bench_ret"]) - v["bench_ret"]) < 1e-7    # CSV dibulatkan 8 desimal


def test_pending_record_resumed_next_cycle(state_dir, cfg, world, monkeypatch):
    """Job mati setelah pesan terkirim tapi sebelum log ditulis -> siklus berikutnya melanjutkan."""
    info, kl = world
    calls = {"n": 0}
    real = jobs._record_day

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("runner mati")
        return real(*a, **k)
    monkeypatch.setattr(jobs, "_record_day", boom)
    now = dt.datetime(2026, 10, 4, 0, 3, tzinfo=dt.timezone.utc)
    with pytest.raises(RuntimeError):
        jobs.run_momentum(ctx_at(cfg, info, kl, now))
    assert store.read("equity") == [] and store.load_json("momentum_paper.json")["pending_record"]
    n_orders = len(store.read("orders"))
    out = jobs.run_momentum(ctx_at(cfg, info, kl, now + dt.timedelta(minutes=10)))
    assert out is not None and len(store.read("equity")) == 1
    assert len(store.read("orders")) == n_orders          # tidak trading ulang
    assert store.load_json("momentum_paper.json").get("pending_record") is None


def test_daily_report_total_equity_and_sections(state_dir, cfg, world):
    """Laporan harian: total paper = momentum + flush, satu blok per strategi dengan
    posisi terbuka + persen sejak entry; kolom total_equity di equity.csv sama."""
    info, kl = world
    jobs.run_flush(ctx_at(cfg, info, kl, dt.datetime(2026, 10, 4, 0, 5, tzinfo=dt.timezone.utc)))
    fb = store.load_json("flush_paper.json")["book"]
    assert fb["positions"]
    c = ctx_at(cfg, info, kl, dt.datetime(2026, 10, 4, 0, 12, tzinfo=dt.timezone.utc))
    out = jobs.run_momentum(c)
    msg = notify.plain([m["text"] for m in c.outbox.sent if "laporan harian" in m["text"]][0])
    row = store.read("equity")[-1]
    total = float(row["paper_equity"]) + float(row["flush_equity"])
    assert float(row["total_equity"]) == pytest.approx(total, abs=1e-6)
    assert f"Total {notify.usd(total)} USDC" in msg
    assert "Momentum Report" in msg and "Flush Report" in msg
    long_lines = [x for x in msg.splitlines() if x.startswith("🟢 Long")]
    assert len(long_lines) == 2                                   # momentum + flush
    assert long_lines[0].startswith(f"🟢 Long ({len(out['paper']['positions'])}):")
    assert long_lines[1].startswith(f"🟢 Long ({len(fb['positions'])}):")
    assert all("%)" in x for x in long_lines)
    assert len(msg) < 4000


def test_flush_exit_message_shows_total(state_dir, cfg, world):
    info, kl = world
    jobs.run_flush(ctx_at(cfg, info, kl, dt.datetime(2026, 10, 4, 0, 5, tzinfo=dt.timezone.utc)))
    for k, df in info.c4h.items():
        nxt = pd.DataFrame({"ts": [SIGNAL_BAR + pd.Timedelta(hours=4), SIGNAL_BAR + pd.Timedelta(hours=8)],
                            "open": [99.5, 100.0], "high": [100.0, 130.0], "low": [99.0, 99.8],
                            "close": [99.8, 120.0], "volume": [100.0, 100.0]})
        info.c4h[k] = pd.concat([df, nxt], ignore_index=True)
    c = ctx_at(cfg, info, kl, dt.datetime(2026, 10, 4, 8, 5, tzinfo=dt.timezone.utc))
    jobs.run_flush(c)
    exits = [m["text"] for m in c.outbox.sent + c.outbox.items if "exit" in m["text"]]
    assert exits and "Total paper" in exits[0] and "momentum 200.00" in exits[0]
