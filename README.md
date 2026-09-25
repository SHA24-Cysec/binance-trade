# binance-trade

Bot rotasi Binance Spot untuk mode PAPER dan LIVE. Bot memindai pair dengan quote asset yang sama, menyaring kandidat likuid yang sedang bergerak, lalu mencari setup pullback retest pada candle yang sudah tertutup.

## Status utama

- Satu entry long per rotasi, tanpa martingale dan tanpa averaging down.
- Entry memakai struktur pullback retest: breakout di atas swing high, pullback menyentuh level, lalu close kembali di atas level dan anchored VWAP.
- Exit memakai angka tetap dari config: Stop Loss, Take Profit, Breakeven, dan Trailing.
- Ukuran posisi memakai mode persen saldo atau nominal tetap, dengan plafon nominal opsional.
- Dashboard menyediakan kontrol mode, kredensial, setelan, backtest portofolio, watchlist, riwayat, dan reset akun PAPER.

## Cara jalan cepat

```bash
pip install -r requirements.txt
python pump_scanner_bot.py --selftest
python dashboard.py
```

Mode default adalah PAPER. Mode LIVE wajib memakai API key dan secret produksi yang valid.

## File penting

| File | Fungsi |
| --- | --- |
| `config.py` | Default config dan helper mode runtime |
| `settings_schema.py` | Schema dan validasi setelan dashboard |
| `strategy.py` | Struktur candle, parser klines, anchored VWAP, sizing, dan level exit tetap |
| `market_scanner.py` | Filter pasar, gerbang pump, dan deteksi setup pullback retest |
| `pump_scanner_bot.py` | Loop bot live atau paper |
| `backtest.py` | Backtest satu simbol dan selftest lokal |
| `portfolio_backtest.py` | Backtest portofolio lintas simbol |
| `dashboard.py` | Server dashboard Flask dan API internal |
| `templates/dashboard.html` | Tampilan dashboard |
| `watchlist_auto.py` | Penyegar watchlist read-only |

## Setup pullback retest

Urutan yang dicari oleh scanner:

1. Candle close menembus swing high valid.
2. Candle breakout menjadi anchor untuk anchored VWAP.
3. Harga kembali menyentuh level breakout.
4. Candle terakhir close kembali di atas level.
5. Close berada cukup tinggi dalam range candle sesuai `MIN_CLOSE_POSITION_IN_RANGE`.
6. Close berada di atas anchored VWAP.
7. Umur setup dibatasi oleh `MAX_BARS_BREAKOUT_TO_RETEST` dan jumlah sentuhan oleh `MAX_RETEST_TOUCHES`.

Parameter utama setup:

| Key | Makna |
| --- | --- |
| `CONFIRM_INTERVAL` | Interval candle konfirmasi |
| `CONFIRM_LOOKBACK_BARS` | Jumlah candle tertutup yang diambil untuk evaluasi |
| `SWING_LOOKBACK_BARS` | Lookback untuk mencari swing high |
| `SWING_PIVOT_WING_BARS` | Sayap kiri dan kanan pivot |
| `VWAP_MIN_BARS_AFTER_ANCHOR` | Minimum candle setelah breakout sebelum VWAP dipakai |
| `MAX_BARS_BREAKOUT_TO_RETEST` | Umur maksimum setup |
| `MAX_RETEST_TOUCHES` | Jumlah kunjungan ke level yang masih diterima |
| `MIN_CLOSE_POSITION_IN_RANGE` | Posisi minimum close di dalam range candle retest |

## Gerbang pump dan filter pasar

Sebelum setup dievaluasi, kandidat harus lolos:

- Quote asset sesuai `QUOTE_ASSET`.
- Bukan stablecoin, bukan leveraged token, bukan blacklist manual.
- Status simbol dapat diperdagangkan jika metadata tersedia.
- Volume 24 jam minimal `MIN_QUOTE_VOLUME_USDT_24H`.
- Kenaikan 24 jam minimal `PUMP_MIN_24H_CHANGE_PCT`.
- Volume berjalan minimal `PUMP_VOLUME_SURGE_MULT` kali rata-rata volume harian tertutup sebelumnya.
- Usia listing minimal `MIN_LISTING_AGE_DAYS` bila filter usia aktif.
- Spread order book maksimal `MAX_SPREAD_PCT` sebelum entry.

## Exit dan sizing

Exit yang tersedia:

| Key | Makna |
| --- | --- |
| `USE_STOP_LOSS` | Aktifkan Stop Loss |
| `SL_PCT` | Jarak Stop Loss tetap dari entry |
| `USE_TP` | Aktifkan Take Profit |
| `TP_PCT` | Target Take Profit tetap dari entry |
| `USE_BREAKEVEN` | Aktifkan Breakeven |
| `BE_TRIGGER_PCT` | Profit pemicu Breakeven |
| `BE_LOCK_PCT` | Profit yang dikunci saat Breakeven aktif |
| `USE_TRAILING` | Aktifkan Trailing |
| `TRAILING_START_PCT` | Profit awal untuk mengaktifkan Trailing |
| `TRAILING_STEP_PCT` | Jarak stop Trailing dari harga tertinggi |

Sizing yang tersedia:

| Key | Makna |
| --- | --- |
| `USE_RISK_PERCENT` | True memakai persentase saldo bebas |
| `RISK_PERCENT` | Persentase saldo bebas yang dipakai saat mode persen aktif |
| `POSITION_SIZE_USDT` | Nominal tetap saat mode persen mati |
| `MAX_POSITION_USDT` | Plafon nominal per posisi, 0 berarti tanpa plafon di PAPER |
| `BALANCE_BUFFER_PCT` | Saldo yang sengaja tidak dibelanjakan untuk fee dan pergerakan harga |
| `BACKTEST_INITIAL_EQUITY_USDT` | Modal awal simulasi backtest |

## Backtest

Selftest lokal tanpa jaringan:

```bash
python backtest.py --selftest
```

Backtest satu simbol memakai data publik Binance:

```bash
python backtest.py --symbol SOLUSDT --days 30
```

Dashboard menjalankan backtest portofolio lintas simbol melalui API internal, dengan satu job berjalan pada satu waktu agar tidak membebani rate limit.

## Dashboard

Jalankan:

```bash
python dashboard.py
```

Panel utama:

- Kontrol mode PAPER atau LIVE.
- Kredensial API.
- Status proses bot.
- Posisi aktif dan ringkasan akun.
- Setelan runtime per mode.
- Backtest portofolio.
- Watchlist read-only.
- Riwayat trade dan audit perubahan.

## File runtime

Nama file runtime dipisah per mode agar PAPER dan LIVE tidak tercampur, misalnya:

- `pump_bot_state_paper.json`
- `pump_bot_state_live.json`
- `pump_bot_runtime.json`
- `pump_bot_settings_paper.json`
- `pump_bot_settings_live.json`
- `watchlist_auto_paper.json`
- `watchlist_auto_live.json`

## Tes

Jalankan seluruh suite:

```bash
pytest -q
```

Selftest modul utama:

```bash
python pump_scanner_bot.py --selftest
python backtest.py --selftest
python portfolio_backtest.py --selftest
```

## Catatan risiko

Trading crypto berisiko tinggi. Gunakan mode PAPER lebih dulu, pakai plafon nominal di LIVE, aktifkan equity stop dan daily stop, serta uji setelan pada rentang data yang cukup panjang sebelum memakai uang sungguhan.
