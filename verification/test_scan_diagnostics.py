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


def cfg_tanpa_timeframe_besar(**tambahan):
    cfg = {
        **PUMP_CONFIG,
        "DIRECTION_FILTER_ENABLED": False,
        "DIRECTION_ZONE_FILTER_ENABLED": False,
        "ZONE_FILTER_ENABLED": False,
    }
    cfg.update(tambahan)
    return cfg



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
    cfg = cfg_tanpa_timeframe_besar()
    result = market.find_best_candidate(
        tickers, lambda symbol: [sample_candle()], cfg,
        diagnostics=diag,
    )
    assert result is None
    assert diag.market_candidates == 4
    assert diag.checked_candidates == 4
    assert diag.rejection_counts == {"HISTORY_CONFIRM": 4}
    assert diag.log_lines(cfg)[0].startswith(
        "Diagnostik entry: 4 dari 4 kandidat pasar diperiksa"
    )
    assert "gerbang pertama" in diag.log_lines(cfg)[1]
    assert "riwayat 5m kurang: 4 kandidat" in diag.log_lines(cfg)[2]
    second = market.ScanDiagnostics()
    assert market.find_best_candidate(
        tickers, lambda symbol: [sample_candle()], cfg,
        diagnostics=second,
    ) is None
    assert second.signature() == diag.signature()


@pytest.mark.parametrize("stage,interval", [
    ("DIRECTION", "1d"), ("DAILY_ZONE", "1d"), ("ZONE", "1h"), ("TIMING", "5m"),
])
def test_downstream_gate_rejection_has_its_own_reason(monkeypatch, stage, interval):
    candidate = sample_candidate()
    monkeypatch.setattr(market, "filter_and_rank_candidates", lambda *args: [candidate])
    monkeypatch.setattr(
        market, "detect_entry_setup",
        lambda *args: market.SetupResult(True, "sinyal valid", 100.5, .2),
    )

    def verdict(name):
        return lambda *args: {
            "ok": stage != name,
            "interval": interval,
            "reason": f"alasan {name}",
        }

    monkeypatch.setattr(market, "direction_verdict", verdict("DIRECTION"))
    monkeypatch.setattr(market, "daily_zone_verdict", verdict("DAILY_ZONE"))
    monkeypatch.setattr(market, "zone_verdict", verdict("ZONE"))
    monkeypatch.setattr(market, "timing_verdict", verdict("TIMING"))
    diag = market.ScanDiagnostics()
    result = market.find_best_candidate(
        [], lambda symbol: [sample_candle()], PUMP_CONFIG,
        direction_provider=lambda symbol: [sample_candle()],
        zone_provider=lambda symbol: [sample_candle()],
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
        [], lambda symbol: [sample_candle()], cfg_tanpa_timeframe_besar(),
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
    assert market.find_best_candidate([], broken, cfg_tanpa_timeframe_besar(),
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
    good = lambda *args: {"ok": True, "interval": "5m", "reason": "lolos"}
    for name in (
        "direction_verdict", "daily_zone_verdict", "zone_verdict", "timing_verdict",
    ):
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
    diag.reject("DIRECTION", "BNBUSDT", "EMA turun")
    assert diag.rejection_counts == {"VOLUME_CONFIRM": 2, "DIRECTION": 1}
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
    cfg = cfg_tanpa_timeframe_besar()
    diag = market.ScanDiagnostics()
    assert market.find_best_candidate(
        [], lambda symbol: [sample_candle()], cfg,
        diagnostics=diag,
    ) is None
    assert diag.rejection_counts == {"HISTORY_CONFIRM": 1}
    assert market.find_best_candidate(
        [], lambda symbol: [sample_candle()], cfg,
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
        return {"ok": symbol != rejected, "interval": "5m",
                "reason": f"{symbol} ditolak" if symbol == rejected else "lolos"}

    monkeypatch.setattr(market, "detect_entry_setup", setup)
    monkeypatch.setattr(market, "direction_verdict",
                        lambda symbol, *args: passed(symbol, "BBBUSDT"))
    monkeypatch.setattr(market, "daily_zone_verdict",
                        lambda *args: {"ok": True, "interval": "1d", "reason": "lolos"})
    monkeypatch.setattr(market, "zone_verdict",
                        lambda symbol, *args: passed(symbol, "DDDUSDT"))
    monkeypatch.setattr(
        market, "timing_verdict",
        lambda config, klines, *args: {
            "ok": klines[-1].close != closes["CCCUSDT"],
            "interval": "5m",
            "reason": (
                "CCCUSDT ditolak"
                if klines[-1].close == closes["CCCUSDT"]
                else "lolos"
            ),
        },
    )
    diag = market.ScanDiagnostics()
    assert market.find_best_candidate([], fetch, PUMP_CONFIG,
                                      diagnostics=diag) is None
    assert diag.market_candidates == diag.checked_candidates == 4
    assert diag.rejection_counts == {
        "VOLUME_CONFIRM": 1, "DIRECTION": 1, "ZONE": 1, "TIMING": 1,
    }
    assert sum(diag.rejection_counts.values()) == diag.checked_candidates
    assert [diag.examples[stage][0] for stage in (
        "VOLUME_CONFIRM", "DIRECTION", "ZONE", "TIMING",
    )] == ["AAAUSDT", "BBBUSDT", "DDDUSDT", "CCCUSDT"]
    assert len(diag.log_lines(PUMP_CONFIG)) == 6


def test_candle_konfirmasi_hanya_diunduh_untuk_yang_lolos_timeframe_besar(monkeypatch):
    names = ("AAAUSDT", "BBBUSDT")
    candidates = [
        market.Candidate(sym, sym[:-4], 12.0, 10_000_000.0, 100.0)
        for sym in names
    ]
    monkeypatch.setattr(market, "filter_and_rank_candidates",
                        lambda *args: candidates)
    monkeypatch.setattr(market, "direction_verdict",
                        lambda symbol, *args: {
                            "ok": symbol != "AAAUSDT",
                            "interval": "1d",
                            "reason": "AAAUSDT ditolak" if symbol == "AAAUSDT" else "lolos",
                        })
    monkeypatch.setattr(market, "daily_zone_verdict",
                        lambda symbol, *args: {"ok": True, "interval": "1d", "reason": "lolos"})
    monkeypatch.setattr(market, "zone_verdict",
                        lambda symbol, *args: {"ok": True, "interval": "1h", "reason": "lolos"})
    monkeypatch.setattr(market, "detect_entry_setup",
                        lambda *args: market.SetupResult(True, "entry valid", 100.5, .2))
    unduh = []

    def fetch(symbol):
        unduh.append(symbol)
        return [sample_candle()]

    diag = market.ScanDiagnostics()
    best = market.find_best_candidate(
        [], fetch, {**PUMP_CONFIG, "TIMING_FILTER_ENABLED": False},
        direction_provider=lambda symbol: [sample_candle()],
        zone_provider=lambda symbol: [sample_candle()],
        diagnostics=diag,
    )
    assert best is not None
    assert best.symbol == "BBBUSDT"
    assert unduh == ["BBBUSDT"]
    assert diag.rejection_counts == {"DIRECTION": 1}
