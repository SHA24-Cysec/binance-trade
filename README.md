# Binance Trade

Bot Binance Spot dengan mode PAPER dan LIVE, monitoring pasar, pengelolaan posisi terbuka, kontrol manual, proteksi exit, serta riwayat transaksi.

## Status operasi

Repository ini tidak memiliki jalur pembukaan posisi baru. Bot hanya:

- memantau pasar dan watchlist secara read-only,
- merekonsiliasi posisi yang sudah ada,
- mengelola Stop Loss, Take Profit, breakeven, trailing, serta proteksi native exchange,
- menjalankan penjualan manual melalui dashboard,
- menyimpan state, log, dan riwayat secara terpisah per mode.

Backtest hanya memuat dan memvalidasi data historis. Backtest tidak membuat order atau trade baru.

## Struktur utama

- `config/`: konfigurasi aktif dan validasi settings.
- `market/`: filter semesta pasar, likuiditas, volume, spread, dan kondisi BTC untuk monitoring.
- `strategy/indicators.py`: parser candle, interval, ATR, serta perhitungan level exit.
- `trading/`: bot, client Binance, paper engine, rekonsiliasi, dan pengelolaan exit.
- `automation/`: penyegaran watchlist read-only.
- `backtesting/`: pengambilan data historis, cache, penyimpanan SQLite temporary, dan ringkasan tanpa trade.
- `web/` dan `templates/`: dashboard monitoring dan kontrol manual.
- `tests/`: pengujian filter pasar, konfigurasi, penyimpanan, paper engine, exit, dan kontrol bot.

## Konfigurasi penting

Nilai default berada di `config/config.py`. Override per mode disimpan oleh dashboard pada file settings masing-masing mode.

| Kelompok | Parameter |
|---|---|
| Sistem | `MODE`, `QUOTE_ASSET`, `LIVE_BASE_URL`, `USE_WEBSOCKET`, `MARKET_DATA_INTERVAL` |
| Monitoring pasar | `MIN_QUOTE_VOLUME_USDT_24H`, `PUMP_MIN_24H_CHANGE_PCT`, `PUMP_VOLUME_SURGE_MULT`, `BTC_FILTER_ENABLED`, `BTC_MAX_DROP_PCT`, `BTC_LOOKBACK_BARS`, `EXTRA_EXCLUDE_SYMBOLS` |
| Watchlist | `WATCHLIST_ENABLED`, `WATCHLIST_TOP_N` |
| Exit posisi | `USE_TP`, `TP_PCT`, `USE_STOP_LOSS`, `SL_PCT`, `USE_ATR_EXIT`, `ATR_PERIOD`, `ATR_MULT_SL`, `ATR_MULT_TP`, `USE_BREAKEVEN`, `BE_TRIGGER_PCT`, `BE_LOCK_PCT`, `USE_TRAILING`, `TRAILING_START_PCT`, `TRAILING_STEP_PCT` |
| Proteksi akun | `USE_EQUITY_STOP`, `MAX_DRAWDOWN_PERCENT`, `USE_DAILY_STOP`, `MAX_DAILY_LOSS_PERCENT`, `CLOSE_ALL_AT_LIMIT`, `DD_COOLDOWN_HOURS` |
| Backtest data | `BACKTEST_INITIAL_EQUITY_USDT`, `BACKTEST_CACHE_ENABLED`, `BACKTEST_CACHE_FILE`, `BACKTEST_CACHE_FRESH_HOURS`, `BACKTEST_CACHE_TTL_DAYS` |

API key dan secret tidak ditulis ke repository. Gunakan pengelolaan kredensial pada dashboard atau environment yang sesuai.

## Menjalankan

```bash
python run.py
```

Untuk selftest lokal tanpa jaringan:

```bash
python -m trading.pump_scanner_bot --selftest
python -m backtesting.backtest --selftest
python -m backtesting.portfolio_backtest --selftest
```

Untuk pengujian repository:

```bash
pytest -q
```

## Keamanan operasi

- Jalur order bot hanya menerima `SELL` untuk pengelolaan posisi yang sudah ada.
- Rekonsiliasi yang ambigu bersifat fail-closed dan meminta pemeriksaan manual.
- Watchlist tidak mengirim order dan berhenti ketika posisi terbuka sedang dikelola.
- Data PAPER dan LIVE menggunakan state, log, kontrol, dan settings yang terpisah.
- File cache dan database backtest bersifat lokal dan tidak digunakan oleh jalur trading live.

## Catatan pengembangan

Perubahan konfigurasi harus didaftarkan di `config/settings_schema.py`. Perubahan yang menyentuh order, state, rekonsiliasi, atau proteksi exit wajib disertai pengujian regresi. ZIP distribusi dibuat menggunakan nama repository `binance-trade.zip` setelah audit dan pengujian selesai.
