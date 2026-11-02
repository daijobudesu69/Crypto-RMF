import os

import numpy as np
import pandas as pd
import pytest

from rmf import book as bk
from rmf import stoprules


def test_paper_buy_close_pnl_fees(cfg):
    b = bk.new_book(200)
    bk.buy(b, "SOL", 1.0, 10.0, cfg.costs, "t0")
    px_in = 10.0 * (1 + cfg.costs.paper_slippage)
    assert abs(b["positions"]["SOL"]["entry_px"] - px_in) < 1e-12
    eq = bk.equity(b, {"SOL": 11.0})
    assert abs(eq - (200 - px_in * cfg.costs.taker_fee + (11 - px_in))) < 1e-9
    f = bk.close(b, "SOL", 11.0, cfg.costs)
    px_out = 11.0 * (1 - cfg.costs.paper_slippage)
    assert abs(f["pnl"] - (px_out - px_in)) < 1e-12
    assert not b["positions"]
    # biaya bolak-balik ~0,07% per sisi, sama dengan asumsi backtest
    per_side = cfg.costs.taker_fee + cfg.costs.paper_slippage
    assert abs(per_side - 0.0007) < 1e-12


def test_funding_charged_to_cash(cfg):
    b = bk.new_book(200)
    bk.buy(b, "X", 10.0, 1.0, cfg.costs, "t0")
    cash = b["cash"]
    bk.charge_funding(b, "X", bk.funding_cost(10.0, 1.0, [(0, 0.0001)] * 24))
    assert abs(cash - b["cash"] - 0.024) < 1e-12


def _series(vals, start="2026-01-01"):
    return pd.Series(vals, pd.date_range(start, periods=len(vals), freq="D"))


def test_dd_breach(cfg):
    eq = _series([200, 260, 150])
    st = stoprules.evaluate(eq, _series([1, 1, 1]), cfg)
    assert st.dd_breached and abs(st.dd_pct - (150 / 260 - 1) * 100) < 1e-9


def test_nine_month_streak(cfg):
    days = pd.date_range("2026-01-01", "2026-11-15", freq="D")
    strat = pd.Series(np.linspace(200, 210, len(days)), days)        # +5% total
    bench = pd.Series(np.linspace(1, 2, len(days)), days)            # +100% total
    st = stoprules.evaluate(strat, bench, cfg)
    assert st.months_behind_streak >= 9
    assert any("berturut-turut" in x for x in st.breaches)
    # bulan berjalan (November) tidak dihitung
    assert st.monthly[-1][0] == "2026-10"


def test_six_month_floor(cfg):
    days = pd.date_range("2026-01-01", "2026-07-15", freq="D")
    st = stoprules.evaluate(pd.Series(np.linspace(200, 170, len(days)), days),
                            pd.Series(1.0, days), cfg)
    assert st.ret_6m_pct is not None and st.ret_6m_pct < -10
    assert any("6 bulan" in x for x in st.breaches)
    early = stoprules.evaluate(pd.Series(np.linspace(200, 170, 60), days[:60]), pd.Series(1.0, days[:60]), cfg)
    assert early.ret_6m_pct is None                # belum 6 bulan: tidak dinilai


def test_sleeve_compares_1x_with_basket(cfg):
    """Akun 0,5x naik 1%/bulan, basket 1x naik 1,5%/bulan: sleeve 1x (~2%) menang."""
    days = pd.date_range("2026-01-01", "2026-11-15", freq="D")
    eq = pd.Series(200 * (1.01 ** (np.arange(len(days)) / 30)), days)
    bench = pd.Series(1.015 ** (np.arange(len(days)) / 30), days)
    raw = stoprules.evaluate(eq, bench, cfg)
    sleeve = stoprules.evaluate(eq, bench, cfg, exposure=pd.Series(0.5, days))
    assert raw.months_behind_streak >= 9 and sleeve.months_behind_streak == 0
    cash = stoprules.sleeve_index(pd.Series([200.0, 200.0, 210.0], days[:3]), pd.Series([0.0, 0.5, 0.5], days[:3]))
    assert list(cash.round(6)) == [1.0, 1.0, 1.1]


def test_equity_csv_new_column_migrated_in_place(state_dir):
    """Kolom baru di akhir (total_equity): riwayat lama tetap di equity.csv (tidak
    dirotasi ke .v1), baris lama diisi paper_equity + flush_equity."""
    import csv

    from rmf import store
    old = store.COLUMNS["equity"][:-1]
    assert store.COLUMNS["equity"][-1] == "total_equity"
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "equity.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=old, lineterminator="\n")
        w.writeheader()
        w.writerow({"exec_day": "2026-10-30", "paper_equity": "165.84176586", "flush_equity": "-2.6234508"})
        w.writerow({"exec_day": "2026-10-31", "paper_equity": "165.84176586", "flush_equity": ""})
    store.append("equity", {"exec_day": "2026-11-01", "paper_equity": 165.84, "flush_equity": 2.72,
                            "total_equity": 168.56}, mirror=False)
    rows = store.read("equity")
    assert [r["exec_day"] for r in rows] == ["2026-10-30", "2026-10-31", "2026-11-01"]
    assert float(rows[0]["total_equity"]) == pytest.approx(165.84176586 - 2.6234508)
    assert float(rows[1]["total_equity"]) == pytest.approx(165.84176586)
    assert rows[2]["total_equity"] == "168.56"
    assert sorted(os.listdir(state_dir)) == ["equity.csv"]
