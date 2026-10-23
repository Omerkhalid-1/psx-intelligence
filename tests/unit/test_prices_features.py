"""Corporate-action adjustment, discontinuity detection, point-in-time features and targets."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.services.research.features import FEATURE_SETS, build_features, build_targets
from app.services.research.prices import adjustment_factors, check_action_consistency, price_frame_from_raw
from tests.factories import business_days, synthetic_ohlcv


@dataclass
class FakeAction:
    ex_date: date
    share_multiplier: Decimal
    action_type: str = "bonus"


def raw_frame(n=400, seed=1, start=date(2024, 1, 1), drop: set[int] | None = None) -> pd.DataFrame:
    df = synthetic_ohlcv(n, seed)
    df.index = pd.to_datetime(business_days(start, n))
    df["quality_flags"] = [[] for _ in range(n)]
    df["revision"] = 1
    if drop:
        df = df.drop(df.index[sorted(drop)])
    return df.astype({"volume": float})


def test_adjustment_factor_applies_only_before_ex_date():
    idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
    f = adjustment_factors(idx, [FakeAction(date(2024, 1, 3), Decimal("2")), FakeAction(date(2024, 1, 4),
                                                                                         Decimal("1.1"))])
    assert f.tolist() == pytest.approx([2.2, 2.2, 1.1, 1.0])


def test_adjusted_series_removes_bonus_discontinuity():
    raw = raw_frame()
    pos = 200
    raw.iloc[:pos, raw.columns.get_indexer(["open", "high", "low", "close"])] *= 2.0  # unadjusted 100% bonus
    ex = raw.index[pos].date()
    pf = price_frame_from_raw("SYS", raw, [FakeAction(ex, Decimal("2"))], is_index=False)
    assert abs(pf.frame["ret"].iloc[pos]) < 0.1  # adjusted return is ordinary
    assert pf.frame["action_on_date"].iloc[pos]
    assert not pf.frame["unexplained_gap"].any()
    # raw prices are untouched
    assert pf.frame["close"].iloc[0] == raw["close"].iloc[0]
    assert pf.frame["adj_close"].iloc[0] == pytest.approx(raw["close"].iloc[0] / 2)


def test_unrecorded_split_is_flagged_and_excluded():
    raw = raw_frame()
    pos = 250
    raw.iloc[:pos, raw.columns.get_indexer(["open", "high", "low", "close"])] *= 5.0  # unrecorded split
    bench = raw_frame(seed=9)
    pf = price_frame_from_raw("SYS", raw, [], is_index=False, benchmark=bench)
    assert pf.frame["unexplained_gap"].iloc[pos]
    assert pf.frame["near_unexplained_gap"].iloc[pos:pos + 60].all()
    cal = bench.index
    t = build_targets(pf, cal, (5,))
    window_before = t["y_5"].iloc[pos - 5:pos]
    assert window_before.isna().all()  # labels whose window crosses the gap are dropped


def test_small_gap_within_limit_is_only_suspected():
    raw = raw_frame()
    pos = 150
    raw.iloc[pos:, raw.columns.get_indexer(["open", "high", "low", "close"])] *= 0.91  # -9% gap, within limit
    bench = raw_frame(seed=9)
    pf = price_frame_from_raw("FFC", raw, [], is_index=False, benchmark=bench)
    if pf.frame["gap"].iloc[pos] < -0.07:
        assert pf.frame["suspected_gap"].iloc[pos]
    assert not pf.frame["unexplained_gap"].iloc[pos]


def test_action_consistency_check():
    raw = raw_frame()
    pos = 100
    raw.iloc[:pos, raw.columns.get_indexer(["open", "high", "low", "close"])] *= 1.5
    ok = check_action_consistency(raw, FakeAction(raw.index[pos].date(), Decimal("1.5")))
    bad = check_action_consistency(raw, FakeAction(raw.index[pos].date(), Decimal("3")))
    assert ok[0] == "consistent" and bad[0] == "inconsistent"
    assert check_action_consistency(raw, FakeAction(date(1999, 1, 1), Decimal("2")))[0] == "no_data"


def _frames():
    bench = price_frame_from_raw("KSE100", raw_frame(seed=9), [], is_index=True)
    stock = price_frame_from_raw("SYS", raw_frame(seed=3), [], is_index=False, benchmark=bench.frame)
    return stock, bench


def test_features_are_point_in_time():
    """Features at date t must be identical whether or not later bars exist."""
    stock, bench = _frames()
    full = build_features(stock, bench)
    cut = stock.frame.index[320]
    raw_cols = ["open", "high", "low", "close", "volume", "quality_flags", "revision"]
    stock_past = price_frame_from_raw("SYS", stock.frame.loc[:cut, raw_cols], [], is_index=False,
                                      benchmark=bench.frame.loc[:cut])
    bench_past = price_frame_from_raw("KSE100", bench.frame.loc[:cut, raw_cols], [], is_index=True)
    past = build_features(stock_past, bench_past)
    cols = FEATURE_SETS["tech_v1"]
    pd.testing.assert_frame_equal(full.loc[:cut, cols], past[cols], check_exact=False, rtol=1e-12)


def test_features_are_scale_free_under_adjustment():
    """Rescaling all history by a constant (what a later bonus adjustment does) leaves features unchanged."""
    stock, bench = _frames()
    scaled_raw = stock.frame[["open", "high", "low", "close", "volume", "quality_flags", "revision"]].copy()
    scaled_raw[["open", "high", "low", "close"]] *= 3.0
    scaled = price_frame_from_raw("SYS", scaled_raw, [], is_index=False, benchmark=bench.frame)
    a, b = build_features(stock, bench), build_features(scaled, bench)
    cols = [c for c in FEATURE_SETS["tech_v1"] if c != "rel_volume20"]
    pd.testing.assert_frame_equal(a[cols], b[cols], check_exact=False, rtol=1e-9)


def test_targets_are_forward_returns_and_skip_missing_sessions():
    bench = price_frame_from_raw("KSE100", raw_frame(seed=9), [], is_index=True)
    raw = raw_frame(seed=3, drop={100})  # symbol missed exchange session 100
    pf = price_frame_from_raw("SYS", raw, [], is_index=False, benchmark=bench.frame)
    t = build_targets(pf, bench.frame.index, (1, 5))
    c = pf.frame["adj_close"]
    assert t["y_1"].iloc[10] == pytest.approx(c.iloc[11] / c.iloc[10] - 1)
    assert t["y_5"].iloc[10] == pytest.approx(c.iloc[15] / c.iloc[10] - 1)
    # windows spanning the missing session are invalid rather than silently longer
    assert np.isnan(t["y_1"].iloc[99]) and np.isnan(t["y_5"].iloc[97])
    assert t["y_1"].iloc[-1] != t["y_1"].iloc[-1]  # last row has no future: NaN


def test_consistency_accounts_for_cash_dividend_and_bonus():
    idx = pd.to_datetime(["2024-01-01", "2024-01-02"])
    frame = pd.DataFrame({"open": [100.0, 82.0], "close": [100.0, 82.0]}, index=idx)

    @dataclass
    class Div:
        ex_date: date
        share_multiplier: Decimal
        cash_dividend_per_share: Decimal | None

    # An 18% ex-dividend drop is consistent once the Rs18 dividend is counted, not without it.
    assert check_action_consistency(frame, Div(date(2024, 1, 2), Decimal("1"), Decimal("18")))[0] == "consistent"
    assert check_action_consistency(frame, Div(date(2024, 1, 2), Decimal("1"), None))[0] == "inconsistent"
    # 20% bonus + Rs1.6 cash: (82 * 1.2 + 1.6) / 100 - 1 = 0%
    assert check_action_consistency(frame, Div(date(2024, 1, 2), Decimal("1.2"), Decimal("1.6")))[0] == "consistent"


def test_actions_file_validates_cash_dividends(tmp_path):
    from app.services.research.corporate_actions import CorporateActionFileError, parse_actions_file

    def write(entry: str):
        p = tmp_path / "a.yaml"
        p.write_text("actions:\n" + entry)
        return p

    ok = write('  - {symbol: NBP, ex_date: 2026-03-17, action_type: cash_dividend, share_multiplier: "1",\n'
               '     cash_dividend_per_share: "35", status: unverified, evidence: "test (synthetic)"}\n')
    assert parse_actions_file(ok)[0]["action_type"] == "cash_dividend"
    for bad in ('  - {symbol: NBP, ex_date: 2026-03-17, action_type: cash_dividend, share_multiplier: "1",\n'
                '     status: unverified, evidence: "no amount"}\n',
                '  - {symbol: NBP, ex_date: 2026-03-17, action_type: cash_dividend, share_multiplier: "2",\n'
                '     cash_dividend_per_share: "5", status: unverified, evidence: "multiplier must be 1"}\n'):
        with pytest.raises(CorporateActionFileError, match="cash_dividend"):
            parse_actions_file(write(bad))
