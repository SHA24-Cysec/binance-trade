import socket

import pytest

from config.config import PUMP_CONFIG
from market import market_scanner as market
from strategy.indicators import Kline
from trading import pump_scanner_bot as bot


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Uji diagnostik tidak boleh mengakses jaringan")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def sample_candle(close_time=1_792_248_299_999):
    return Kline(
        close_time - 299_999, 100.0, 101.0, 99.0, 100.5,
        close_time, 1000.0, 100_000.0,
    )


def sample_candidate():
    return market.Candidate("SOLUSDT", "SOL", 12.0, 10_000_000.0, 100.5)


def test_four_candidates_from_sixty_seven_are_expected_without_entry():
    tickers = [
        {
            "symbol": f"AAA{i:03d}USDT",
            "priceChangePercent": "12" if i < 4 else "2",
            "quoteVolume": "6000000",
            "lastPrice": "100",
        }
        for i in range(67)
    ]
    diag = market.ScanDiagnostics()
    result = market.find_best_candidate(
        tickers, lambda symbol: [sample_candle()], PUMP_CONFIG,
        diagnostics=diag,
    )
    assert result is None
    assert diag.market_candidates == 4
    assert diag.checked_candidates == 4
    assert diag.rejection_counts == {"HISTORY_CONFIRM": 4}
    assert diag.log_lines(PUMP_CONFIG)[0].startswith(
        "Diagnostik entry: 4 dari 4 kandidat pasar diperiksa"
    )
    assert "gerbang pertama" in diag.log_lines(PUMP_CONFIG)[1]
    assert "riwayat 5m kurang: 4 kandidat" in diag.log_lines(PUMP_CONFIG)[2]
    second = market.ScanDiagnostics()
    assert market.find_best_candidate(
        tickers, lambda symbol: [sample_candle()], PUMP_CONFIG,
        diagnostics=second,
    ) is None
    assert second.signature() == diag.signature()


@pytest.mark.parametrize("stage", [
    "TREND_HTF", "DEMAND_HTF", "DEMAND_M15", "TREND_M15",
])
def test_downstream_gate_rejection_has_its_own_reason(monkeypatch, stage):
    candidate = sample_candidate()
    monkeypatch.setattr(market, "filter_and_rank_candidates", lambda *args: [candidate])
    monkeypatch.setattr(
        market, "detect_entry_setup",
        lambda *args: market.SetupResult(True, "sinyal valid", 100.5, .2),
    )

    def verdict(name):
        return lambda *args: {
            "ok": stage != name,
            "interval": "1h" if name.endswith("HTF") else "15m",
            "reason": f"alasan {name}",
        }

    monkeypatch.setattr(market, "trend_verdict", verdict("TREND_HTF"))
    monkeypatch.setattr(market, "htf_demand_verdict", verdict("DEMAND_HTF"))
    monkeypatch.setattr(market, "m15_demand_verdict", verdict("DEMAND_M15"))
    monkeypatch.setattr(market, "m15_trend_verdict", verdict("TREND_M15"))
    diag = market.ScanDiagnostics()
    result = market.find_best_candidate(
        [], lambda symbol: [sample_candle()], PUMP_CONFIG,
        diagnostics=diag,
    )
    assert result is None
    assert candidate.confirmed is False
    assert diag.rejection_counts == {stage: 1}
    assert diag.examples[stage] == ("SOLUSDT", f"alasan {stage}")
    assert any("SOLUSDT" in line and f"alasan {stage}" in line
               for line in diag.log_lines(PUMP_CONFIG))


@pytest.mark.parametrize("reason,stage", [
    ("rolling volume ditolak: 1.00x di bawah 1.3x", "VOLUME_CONFIRM"),
    ("zona demand ditolak: harga terlalu jauh", "DEMAND_CONFIRM"),
    ("data candle kurang: 1 dari 60", "HISTORY_CONFIRM"),
    ("close candle tidak valid", "SETUP_CONFIRM"),
])
def test_confirmation_rejection_is_classified(monkeypatch, reason, stage):
    monkeypatch.setattr(market, "filter_and_rank_candidates",
                        lambda *args: [sample_candidate()])
    monkeypatch.setattr(market, "detect_entry_setup",
                        lambda *args: market.SetupResult(False, reason))
    diag = market.ScanDiagnostics()
    assert market.find_best_candidate(
        [], lambda symbol: [sample_candle()], PUMP_CONFIG,
        diagnostics=diag,
    ) is None
    assert diag.rejection_counts == {stage: 1}
    assert diag.examples[stage] == ("SOLUSDT", reason)


def test_failed_candle_download_is_distinct_and_fails_closed(monkeypatch):
    monkeypatch.setattr(market, "filter_and_rank_candidates",
                        lambda *args: [sample_candidate()])

    def broken(symbol):
        raise OSError("timeout")

    diag = market.ScanDiagnostics()
    assert market.find_best_candidate([], broken, PUMP_CONFIG,
                                      diagnostics=diag) is None
    assert diag.rejection_counts == {"CANDLE_CONFIRM": 1}
    assert "timeout" in diag.examples["CANDLE_CONFIRM"][1]
    assert diag.latest_signal_close_ms == 0


def test_diagnostics_do_not_change_entry_decision(monkeypatch):
    monkeypatch.setattr(market, "filter_and_rank_candidates",
                        lambda *args: [sample_candidate()])
    monkeypatch.setattr(
        market, "detect_entry_setup",
        lambda *args: market.SetupResult(True, "entry valid", 100.5, .2),
    )
    good = lambda *args: {"ok": True, "interval": "15m", "reason": "lolos"}
    for name in ("trend_verdict", "htf_demand_verdict",
                 "m15_demand_verdict", "m15_trend_verdict"):
        monkeypatch.setattr(market, name, good)
    baseline = market.find_best_candidate([], lambda symbol: [sample_candle()],
                                          PUMP_CONFIG)
    diag = market.ScanDiagnostics()
    with_diagnostics = market.find_best_candidate(
        [], lambda symbol: [sample_candle()], PUMP_CONFIG, diagnostics=diag,
    )
    assert baseline is not None and baseline.confirmed is True
    assert with_diagnostics is not None and with_diagnostics.confirmed is True
    assert with_diagnostics.confirm_reason == baseline.confirm_reason
    assert not diag.rejection_counts


def test_bounded_log_lines_and_five_minute_throttle():
    diag = market.ScanDiagnostics(market_candidates=4, checked_candidates=4)
    diag.observe_signal([sample_candle()])
    diag.reject("VOLUME_CONFIRM", "SOLUSDT", "  volume\n" + "s" * 300)
    diag.reject("VOLUME_CONFIRM", "ADAUSDT", "alasan lain")
    diag.reject("TREND_HTF", "BNBUSDT", "EMA turun")
    assert diag.rejection_counts == {"VOLUME_CONFIRM": 2, "TREND_HTF": 1}
    assert diag.examples["VOLUME_CONFIRM"][0] == "SOLUSDT"
    assert "\n" not in diag.examples["VOLUME_CONFIRM"][1]
    assert len(diag.examples["VOLUME_CONFIRM"][1]) <= 140
    assert len(diag.log_lines(PUMP_CONFIG)) == 4
    now_ms = 1_792_248_300_000
    signature = diag.signature()
    assert bot.should_log_scan_diagnostics(diag, None, 0, now_ms)
    assert not bot.should_log_scan_diagnostics(
        diag, signature, now_ms, now_ms + 30_000,
    )
    assert bot.should_log_scan_diagnostics(
        diag, signature, now_ms, now_ms + 300_000,
    )
    diag.latest_signal_close_ms += 300_000
    assert bot.should_log_scan_diagnostics(
        diag, signature, now_ms, now_ms + 30_000,
    )
    assert not bot.should_log_scan_diagnostics(
        market.ScanDiagnostics(), None, 0, now_ms,
    )
    diag.latest_signal_close_ms = 10**30
    assert "waktu candle tidak valid" in diag.log_lines(PUMP_CONFIG)[0]


def test_reused_diagnostics_are_reset_between_scans(monkeypatch):
    monkeypatch.setattr(market, "filter_and_rank_candidates",
                        lambda *args: [sample_candidate()])
    diag = market.ScanDiagnostics()
    assert market.find_best_candidate(
        [], lambda symbol: [sample_candle()], PUMP_CONFIG,
        diagnostics=diag,
    ) is None
    assert diag.rejection_counts == {"HISTORY_CONFIRM": 1}
    assert market.find_best_candidate(
        [], lambda symbol: [sample_candle()], PUMP_CONFIG,
        diagnostics=diag,
    ) is None
    assert diag.rejection_counts == {"HISTORY_CONFIRM": 1}


def test_mixed_rejections_count_first_failed_gate_only(monkeypatch):
    names = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")
    candidates = [market.Candidate(sym, sym[:-4], 12.0, 10_000_000, 100.0)
                  for sym in names]
    monkeypatch.setattr(market, "filter_and_rank_candidates",
                        lambda *args: candidates)
    closes = {sym: float(index + 101) for index, sym in enumerate(names)}

    def fetch(symbol):
        bar = sample_candle()
        return [Kline(bar.open_time, bar.open, bar.high, bar.low,
                      closes[symbol], bar.close_time, bar.volume, bar.quote_volume)]

    def setup(klines, config):
        if klines[-1].close == closes["AAAUSDT"]:
            return market.SetupResult(False, "rolling volume ditolak: 1.00x")
        return market.SetupResult(True, "entry valid", klines[-1].close, .2)

    def passed(symbol, rejected):
        return {"ok": symbol != rejected, "interval": "15m",
                "reason": f"{symbol} ditolak" if symbol == rejected else "lolos"}

    monkeypatch.setattr(market, "detect_entry_setup", setup)
    monkeypatch.setattr(market, "trend_verdict",
                        lambda symbol, *args: passed(symbol, "BBBUSDT"))
    monkeypatch.setattr(market, "htf_demand_verdict",
                        lambda symbol, *args: passed(symbol, ""))
    monkeypatch.setattr(market, "m15_demand_verdict",
                        lambda symbol, *args: passed(symbol, "CCCUSDT"))
    monkeypatch.setattr(market, "m15_trend_verdict",
                        lambda symbol, *args: passed(symbol, "DDDUSDT"))
    diag = market.ScanDiagnostics()
    assert market.find_best_candidate([], fetch, PUMP_CONFIG,
                                      diagnostics=diag) is None
    assert diag.market_candidates == diag.checked_candidates == 4
    assert diag.rejection_counts == {
        "VOLUME_CONFIRM": 1, "TREND_HTF": 1,
        "DEMAND_M15": 1, "TREND_M15": 1,
    }
    assert sum(diag.rejection_counts.values()) == diag.checked_candidates
    assert [diag.examples[stage][0] for stage in (
        "VOLUME_CONFIRM", "TREND_HTF", "DEMAND_M15", "TREND_M15",
    )] == list(names)
    assert len(diag.log_lines(PUMP_CONFIG)) == 6
