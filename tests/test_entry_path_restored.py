"""Penjaga jalur entry dan mesin backtest.

Ditambahkan 1 Oktober 2026 setelah pemulihan commit 0b6ca1d.

LATAR BELAKANG
--------------
Commit 0b6ca1d berjudul "Hapus sinyal entry EMA, dll" membuang 18 fungsi inti
dan 1.419 baris logika, sehingga:

  1. Bot kehilangan seluruh jalur beli. `open_position()` lenyap.
  2. `run_backtest()` dan `run_portfolio_backtest()` menjadi stub yang selalu
     mengembalikan nol untuk semua metrik.
  3. Beberapa test justru ditulis untuk MENGUNCI perilaku rusak itu
     ("selalu tanpa trade"), sehingga kerusakan tampak seperti keputusan
     desain dan suite tetap hijau.

Poin ketiga itulah yang paling berbahaya, dan berkas ini ada untuk mencegah
pola yang sama terulang. Test di sini menegaskan bahwa kemampuan intinya
BENAR BENAR ADA, bukan sekadar bahwa fungsinya bisa dipanggil.

Sekaligus dipastikan bahwa pemulihan entry TIDAK menghapus gerbang keselamatan
yang ditambahkan pada audit 30 September 2026.
"""

from __future__ import annotations

import copy
import inspect

import pytest


# =====================================================================
# 1. Jalur entry harus ada
# =====================================================================
def test_open_position_ada_dan_mengirim_buy():
    import trading.pump_scanner_bot as bot

    assert hasattr(bot, "open_position"), "jalur beli hilang lagi"
    src = inspect.getsource(bot.open_position)
    assert "BUY" in src, "open_position tidak mengirim order BUY"


def test_run_benar_benar_memanggil_open_position():
    """Fungsi yang ada tapi tidak pernah dipanggil sama saja dengan tidak ada."""
    import trading.pump_scanner_bot as bot

    src = inspect.getsource(bot.run)
    assert "open_position(" in src, "run() tidak pernah membuka posisi"


def test_fungsi_deteksi_setup_tersedia():
    from market import market_scanner as scanner

    for nama in ("detect_pullback_retest", "confirm_entry", "find_best_candidate",
                 "score_entry_signal", "setup_quality_key"):
        assert hasattr(scanner, nama), f"{nama} hilang dari market_scanner"


def test_indikator_pendukung_tersedia():
    from strategy import indicators as strategy

    for nama in ("ema", "rsi", "macd", "atr", "confirm_window_bars",
                 "required_lookback_bars", "resolve_position_notional",
                 "backtest_buy_execution_price", "backtest_sell_execution_price"):
        assert hasattr(strategy, nama), f"{nama} hilang dari indicators"


def test_kunci_config_jalur_entry_tersedia():
    from config.config import PUMP_CONFIG

    wajib = (
        "CONFIRM_INTERVAL", "CONFIRM_LOOKBACK_BARS", "COOLDOWN_MINUTES_AFTER_CLOSE",
        "SWING_LOOKBACK_BARS", "SWING_PIVOT_WING_BARS", "MAX_BARS_BREAKOUT_TO_RETEST",
        "MAX_RETEST_TOUCHES", "MIN_CLOSE_POSITION_IN_RANGE", "MAX_CHASE_PCT",
        "MAX_SPREAD_PCT", "MIN_LISTING_AGE_DAYS", "TOP_N_CANDIDATES_TO_CONFIRM",
        "POSITION_SIZE_USDT", "USE_RISK_PERCENT", "RISK_PERCENT",
        "BACKTEST_ENTRY_SPREAD_PCT", "BACKTEST_SLIPPAGE_PCT",
        "BACKTEST_ENTRY_DELAY_BARS",
    )
    hilang = [k for k in wajib if k not in PUMP_CONFIG]
    assert not hilang, f"kunci config jalur entry hilang: {hilang}"


def test_setiap_kunci_config_punya_entri_schema():
    """Tanpa entri schema, parameter tidak bisa diatur dari dashboard."""
    import config.settings_schema as ss
    from config.config import PUMP_CONFIG

    hilang = sorted(set(PUMP_CONFIG) - set(ss.PARAMETER_SCHEMA))
    assert not hilang, f"kunci tanpa entri schema: {hilang}"


# =====================================================================
# 2. Backtest harus benar benar mensimulasikan
# =====================================================================
@pytest.fixture()
def data_backtest():
    from backtesting.synthetic_data import (
        cfg_gerbang_pump_nonaktif, riwayat_harian, seri_banyak_setup,
    )
    from config.config import PUMP_CONFIG

    kl = seri_banyak_setup(harga=100.0, siklus=12, bar_datar=288)
    daily = riwayat_harian(kl, hari=7)
    cfg = cfg_gerbang_pump_nonaktif(copy.deepcopy(PUMP_CONFIG))
    cfg["_symbol"] = "TESTUSDT"
    cfg["MIN_QUOTE_VOLUME_USDT_24H"] = 1_000_000
    cfg["ROLLING_VOLUME_FILTER_ENABLED"] = False
    return kl, daily, cfg


def test_backtest_menghasilkan_trade(data_backtest):
    from backtesting import backtest as bt

    kl, daily, cfg = data_backtest
    hasil = bt.run_backtest(kl, cfg, warmup_bars=288, daily_klines=daily)
    assert hasil.trades, "backtest kembali menjadi stub tanpa trade"
    assert hasil.bars_usable > 0


def test_metrik_backtest_tidak_semuanya_nol(data_backtest):
    """Stub lama mengembalikan nol yang ditulis mati untuk SEMUA metrik."""
    from backtesting import backtest as bt

    kl, daily, cfg = data_backtest
    ringkas = bt.summarize(bt.run_backtest(kl, cfg, warmup_bars=288,
                                           daily_klines=daily))
    angka = [ringkas["total_trades"], ringkas["win_rate"],
             ringkas["total_return_pct"], ringkas["equity_curve"]]
    assert any(bool(a) for a in angka), f"semua metrik nol, mesin masih stub: {ringkas}"


def test_parameter_berbeda_memberi_hasil_berbeda(data_backtest):
    """Syarat minimum sebuah backtest yang bermakna."""
    from backtesting import backtest as bt

    kl, daily, cfg = data_backtest
    hasil = set()
    for sl, tp in [(1.0, 2.0), (3.0, 6.0), (5.0, 12.0)]:
        c = dict(cfg)
        c["USE_ATR_EXIT"] = False
        c["SL_PCT"] = sl
        c["TP_PCT"] = tp
        r = bt.summarize(bt.run_backtest(kl, c, warmup_bars=288, daily_klines=daily))
        hasil.add((r["total_trades"], round(r["total_return_pct"], 6)))
    assert len(hasil) > 1, f"parameter tidak berpengaruh sama sekali: {hasil}"


def test_ringkasan_tidak_memakai_nol_yang_ditulis_mati():
    """Menangkap persis bentuk regresi yang terjadi pada commit 0b6ca1d.

    Saat itu summarize() mengembalikan literal 0.0 untuk setiap metrik tanpa
    membaca isi result sama sekali.
    """
    from backtesting import backtest as bt

    src = inspect.getsource(bt.summarize)
    # Sebuah ringkasan yang benar wajib membaca result.trades.
    assert "trades" in src
    assert src.count('"total_trades": 0') == 0, \
        "summarize() menulis nol secara harfiah, mesin kembali menjadi stub"


# =====================================================================
# 3. Gerbang keselamatan audit 30 September 2026 harus tetap berlaku
# =====================================================================
def test_gerbang_risiko_akun_memblokir_live_tanpa_rem():
    """KRITIS-01. Ini pengaman paling penting sekarang bot bisa membeli."""
    import trading.pump_scanner_bot as bot
    from config.config import PUMP_CONFIG

    cfg = copy.deepcopy(PUMP_CONFIG)
    cfg["MODE"] = "live"
    cfg["USE_EQUITY_STOP"] = False
    cfg["USE_DAILY_STOP"] = False
    ok, alasan = bot.account_risk_gate(cfg)
    assert not ok, "LIVE tanpa rem kerugian tidak diblokir"
    assert alasan


def test_gerbang_risiko_meloloskan_live_dengan_rem_aktif():
    import trading.pump_scanner_bot as bot
    from config.config import PUMP_CONFIG

    cfg = copy.deepcopy(PUMP_CONFIG)
    cfg["MODE"] = "live"
    cfg["USE_EQUITY_STOP"] = True
    cfg["USE_DAILY_STOP"] = False
    ok, _ = bot.account_risk_gate(cfg)
    assert ok


def test_run_memeriksa_gerbang_risiko_sebelum_membeli():
    """Gerbang harus dipanggil di run(), bukan sekadar tersedia."""
    import trading.pump_scanner_bot as bot

    src = inspect.getsource(bot.run)
    assert "account_risk_gate(" in src
    # Gerbang wajib dievaluasi SEBELUM titik pembukaan posisi.
    assert src.index("account_risk_gate(") < src.index("open_position("), \
        "gerbang risiko dievaluasi setelah bot sempat membeli"


def test_default_rem_kerugian_tetap_aktif():
    from config.config import PUMP_CONFIG

    assert PUMP_CONFIG["USE_EQUITY_STOP"] is True
    assert PUMP_CONFIG["USE_DAILY_STOP"] is True


def test_entry_diblokir_saat_rekonsiliasi_atau_order_menggantung():
    import trading.pump_scanner_bot as bot

    src = inspect.getsource(bot.run)
    assert "reconciliation_required" in src
    assert "pending_order" in src


def test_batas_chase_dan_spread_ditegakkan():
    """Tanpa keduanya bot bisa mengejar harga yang sudah terbang."""
    import trading.pump_scanner_bot as bot

    src = inspect.getsource(bot.run)
    assert "MAX_CHASE_PCT" in src
    assert "MAX_SPREAD_PCT" in src


def test_filter_usia_listing_ditegakkan():
    import trading.pump_scanner_bot as bot

    assert hasattr(bot, "listing_age_days")
    assert "MIN_LISTING_AGE_DAYS" in inspect.getsource(bot.run)


def test_paper_dan_live_memakai_client_berbeda():
    """Isolasi PAPER dijaga lewat kelas terpisah, bukan sekadar flag."""
    import trading.pump_scanner_bot as bot

    src = inspect.getsource(bot.create_exchange_client)
    assert "PaperClient" in src and "LiveClient" in src
    assert "require_valid_mode" in src, "mode tidak divalidasi keras"


def test_mode_exit_dilaporkan_dari_state_bukan_config():
    """TINGGI-01. Log tidak boleh mengklaim mode yang tidak dipakai runtime."""
    import trading.pump_scanner_bot as bot

    src = inspect.getsource(bot.run)
    assert "describe_exit_mode(" in src
    # Dipanggil setelah state dimuat, kalau tidak nilainya belum ada.
    assert src.index("load_pump_state(") < src.index("describe_exit_mode(")


def test_perbaikan_lock_windows_tidak_ikut_hilang():
    """KRITIS-02 tidak boleh tergerus oleh pemulihan besar ini."""
    import infrastructure.network.rate_limiter as rl
    import infrastructure.storage.atomic_io as aio

    for nama in ("_open_lock_fd", "_acquire_lock", "_release_lock"):
        assert hasattr(aio, nama)
        assert getattr(rl, nama).__module__ == "infrastructure.storage.atomic_io"

    # Diperiksa lewat AST, bukan pencarian teks, supaya komentar dan docstring
    # yang menyebut O_APPEND tidak ikut terhitung sebagai pemakaian.
    import ast
    import textwrap

    pohon = ast.parse(textwrap.dedent(inspect.getsource(aio._open_lock_fd)))
    flag = {
        n.attr for n in ast.walk(pohon)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        and n.value.id == "os"
    }
    assert "O_APPEND" not in flag, "O_APPEND dipakai lagi, bug lock Windows kembali"
