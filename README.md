# binance-trade

Bot rotasi Binance Spot untuk mode PAPER dan LIVE. Bot memindai pair dengan quote asset yang sama, menyaring kandidat pump yang likuid, lalu mencari momentum scalping pada candle yang sudah tertutup.

## Status utama

- Satu entry long per rotasi, tanpa martingale dan tanpa averaging down.
- Entry membutuhkan minimal 3 dari 4 konfirmasi: EMA9 cross EMA21, RSI sehat, MACD histogram menguat, dan higher low.
- Volume harian dan rolling volume candle menjadi gerbang pump berlapis.
- Exit default memakai ATR untuk menyesuaikan volatilitas, dengan fallback ke persentase lama.
- Ukuran posisi memakai mode persen saldo atau nominal tetap, dengan plafon nominal opsional.
- Dashboard menyediakan kontrol mode, kredensial, setelan, backtest portofolio, watchlist, riwayat, dan reset akun PAPER.

## Cara jalan cepat

Instalasi reproducible memakai lock file ber-hash:

```bash
python -m pip install --require-hashes -r requirements.lock
```

Untuk instalasi development yang mengikuti rentang versi sumber:

```bash
python -m pip install -r requirements.txt
python pump_scanner_bot.py --selftest
python dashboard.py
```

Mode default adalah PAPER. Mode LIVE wajib memakai API key dan secret produksi yang valid.

## File penting

| File | Fungsi |
| --- | --- |
| `config.py` | Default config dan helper mode runtime |
| `settings_schema.py` | Schema dan validasi setelan dashboard |
| `strategy.py` | Struktur candle, parser klines, indikator momentum, sizing, dan level exit ATR |
| `market_scanner.py` | Filter pasar, gerbang pump, rolling volume, dan deteksi sinyal momentum |
| `pump_scanner_bot.py` | Loop bot live atau paper |
| `backtest.py` | Backtest satu simbol dan selftest lokal |
| `portfolio_backtest.py` | Backtest portofolio lintas simbol |
| `dashboard.py` | Server dashboard Flask dan API internal |
| `templates/dashboard.html` | Tampilan dashboard |
| `watchlist_auto.py` | Penyegar watchlist read-only |

## Sinyal momentum pump

Sinyal entry hanya dievaluasi dari candle yang sudah close dan kronologis. Bot membutuhkan minimal 3 dari 4 konfirmasi berikut:

1. EMA9 baru cross ke atas EMA21.
2. RSI(14) berada pada zona 50 sampai 75.
3. MACD histogram naik atau baru cross ke area positif.
4. Higher low terbentuk setelah momentum awal.

Sebelum konfirmasi indikator, volume candle konfirmasi harus memenuhi gerbang rolling:

- `ROLLING_VOLUME_LOOKBACK_BARS` candle sebelumnya menjadi rata-rata pembanding.
- Volume candle konfirmasi minimal `ROLLING_VOLUME_SURGE_MULT` kali rata-rata tersebut.
- Jumlah candle yang wajib memenuhi syarat diatur oleh `ROLLING_VOLUME_CONFIRMATION_BARS`.
- Candle yang sedang berjalan tidak boleh masuk ke perhitungan.

Parameter utama:

| Key | Makna |
| --- | --- |
| `CONFIRM_INTERVAL` | Interval candle konfirmasi |
| `CONFIRM_LOOKBACK_BARS` | Jumlah candle tertutup yang diambil |
| `SWING_PIVOT_WING_BARS` | Sayap kiri dan kanan untuk pivot low |
| `ROLLING_VOLUME_FILTER_ENABLED` | Mengaktifkan filter volume rolling |
| `ROLLING_VOLUME_LOOKBACK_BARS` | Jumlah candle pembanding volume |
| `ROLLING_VOLUME_SURGE_MULT` | Pengali minimum volume konfirmasi |
| `ROLLING_VOLUME_CONFIRMATION_BARS` | Jumlah candle terakhir yang wajib lolos |

## Gerbang pump dan filter pasar

Sebelum setup dievaluasi, kandidat harus lolos:

- Quote asset sesuai `QUOTE_ASSET`.
- Bukan stablecoin, bukan leveraged token, bukan blacklist manual.
- Status simbol dapat diperdagangkan jika metadata tersedia.
- Volume 24 jam minimal `MIN_QUOTE_VOLUME_USDT_24H`.
- Kenaikan 24 jam minimal `PUMP_MIN_24H_CHANGE_PCT`.
- Volume 24 jam minimal `PUMP_VOLUME_SURGE_MULT` kali rata-rata volume harian tertutup sebelumnya.
- Volume candle konfirmasi memenuhi filter rolling sesuai parameter di atas.
- Filter korelasi BTC menolak entry jika penurunan BTC melewati `BTC_MAX_DROP_PCT` dalam `BTC_LOOKBACK_BARS`.
- Usia listing minimal `MIN_LISTING_AGE_DAYS` bila filter usia aktif.
- Spread order book maksimal `MAX_SPREAD_PCT` sebelum entry.

## Exit dan sizing

Exit default memakai ATR:

| Key | Makna |
| --- | --- |
| `USE_ATR_EXIT` | Mengaktifkan exit adaptif ATR |
| `ATR_PERIOD` | Periode ATR |
| `ATR_MULT_SL` | Jarak Stop Loss dalam ATR |
| `ATR_MULT_TP` | Jarak Take Profit dalam ATR |
| `ATR_MULT_TRAIL` | Jarak trailing dalam ATR |
| `ATR_MULT_BE_TRIGGER` | Pemicu breakeven dalam ATR |
| `ATR_MULT_BE_LOCK` | Jarak lock breakeven dalam ATR |
| `ATR_MULT_TRAIL_START` | Pemicu trailing dalam ATR |
| `SL_PCT`, `TP_PCT` | Fallback exit persen ketika ATR dimatikan |

Sizing yang tersedia:

| Key | Makna |
| --- | --- |
| `USE_RISK_PERCENT` | True memakai persentase saldo bebas |
| `RISK_PERCENT` | Persentase saldo bebas yang dipakai saat mode persen aktif |
| `POSITION_SIZE_USDT` | Nominal tetap saat mode persen mati |
| `MAX_POSITION_USDT` | Plafon nominal per posisi |
| `BALANCE_BUFFER_PCT` | Saldo yang sengaja tidak dibelanjakan |

### Proteksi exchange-side LIVE

Saat `MODE=LIVE`, `USE_STOP_LOSS=True`, `USE_TP=True`, dan `USE_NATIVE_OCO=True`,
bot memasang satu OCO SELL Binance setelah BUY benar-benar terisi. Leg atas
adalah `TAKE_PROFIT_LIMIT`, leg bawah adalah `STOP_LOSS_LIMIT`. Harga kedua leg
dibulatkan ke `tickSize` dan divalidasi terhadap bid terbaru. Buffer limit OCO
dapat diatur lewat `NATIVE_OCO_LIMIT_BUFFER_PCT`.

Alur aman proteksi:

- list client ID dan client ID kedua leg disimpan ke file state sebelum request POST;
- bila POST timeout atau statusnya tidak pasti, bot tidak mengulang POST dan tidak
  memasang proteksi kedua secara buta. Status dicari dengan query order-list;
- sebelum exit manual atau exit lokal, OCO direkonsiliasi lalu dibatalkan. SELL
  market ditahan bila cancel tidak dapat diverifikasi;
- bila salah satu leg OCO berstatus FILLED, saldo direkonsiliasi dan bot tidak
  mengirim SELL kedua;
- bila client jelas tidak mendukung OCO atau validasi lokal gagal, bot dapat
  memakai `STOP_LOSS` market native sebagai fallback. Error POST Binance yang
  statusnya UNKNOWN tidak memicu fallback;
- Take Profit, breakeven, dan trailing lokal tetap tersedia sebagai logika
  exit, tetapi OCO menjadi proteksi exchange-side utama saat konfigurasi aktif;
- kegagalan pemasangan tidak menghapus local SL. `reconciliation_required` tetap
  aktif sehingga entry baru fail-closed.

Implementasi ini tidak mengirim order ke LIVE selama selftest dan test suite.
Pengujian integrasi order hanya boleh memakai fake client atau kredensial
Binance Spot Testnet yang terpisah.

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

Asumsi dan keterbatasan penting: model memakai OHLC candle, bukan order book atau
antrian matching. Entry dan exit simulasi dianggap terisi penuh pada satu harga
adverse, sehingga partial fill, depth yang habis, rejection filter, dan urutan
tick di dalam candle belum dapat direkonstruksi. Gap pada open ditangani dengan
harga open saat level exit sudah ditembus, dan bila SL serta TP tersentuh pada
candle yang sama SL diprioritaskan secara konservatif.

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
