# Audit Fitur Bot: binance-trade

Tanggal audit: 2026-09-23
Repo: SHA24-Cysec/binance-trade (commit `f3b3c40 Upload`)
Metode: pembacaan seluruh kode sumber + menjalankan kedua `--selftest` (dua-duanya LULUS).

Repo ini berisi **dua bot terpisah** yang berbagi pustaka yang sama:

1. `bot.py` -> Bot Grid Martingale (satu pair tetap, default BTCUSDT)
2. `pump_scanner_bot.py` -> Bot Pump Scanner (rotasi seluruh pair USDT, satu koin pada satu waktu)

Keduanya berbagi: `binance_client.py`, `strategy.py`, `state.py`, `market_scanner.py`, `config.py`.

---

## 1. Peta File

| File | Peran |
|---|---|
| `config.py` | Dua blok konfigurasi: `CONFIG` (grid) dan `PUMP_CONFIG` (pump scanner) |
| `binance_client.py` | Klien REST Binance Spot buatan sendiri + parser filter simbol |
| `strategy.py` | Indikator: EMA, ATR Wilder, SuperTrend, sinyal tren, jarak grid |
| `market_scanner.py` | Filter, ranking, dan konfirmasi momentum untuk mode pump |
| `state.py` | Simpan/muat state ke JSON (tahan restart), penulisan atomik |
| `bot.py` | Loop utama bot grid + selftest |
| `pump_scanner_bot.py` | Loop utama bot pump + selftest |
| `requirements.txt` | Hanya `requests` |

---

## 2. Fitur Infrastruktur (dipakai kedua bot)

### Klien REST Binance (`binance_client.py`)
- Signing HMAC SHA256 dengan payload di-percent-encode dulu. Diverifikasi cocok byte-per-byte dengan contoh resmi dokumentasi Binance (selftest LULUS).
- Sinkronisasi waktu server (`sync_time`) + koreksi offset timestamp, plus re-sync otomatis saat error `-1021`.
- Retry otomatis dengan exponential backoff (maks 3 percobaan), URL + signature dibangun ulang tiap percobaan supaya timestamp selalu segar.
- Endpoint publik: server time, exchangeInfo, ticker 24h semua pair, klines, book ticker, harga.
- Endpoint ber-signature: info akun, market order (BUY/SELL).
- Hanya mendukung order **MARKET** (tidak ada LIMIT).
- Parser filter simbol (`SymbolFilters`): membaca LOT_SIZE, MARKET_LOT_SIZE, MIN_NOTIONAL/NOTIONAL, PRICE_FILTER. Menangani kasus stepSize = 0 (dianggap "abaikan", bukan "tanpa pembulatan") dan mengambil aturan paling ketat. Pembulatan quantity ROUND_DOWN sesuai step size.
- `build_filters_cache`: bangun cache filter untuk semua simbol sekaligus (dipakai mode pump).

### State persisten (`state.py`)
- Simpan state ke JSON dengan penulisan atomik (tulis ke file `.tmp` lalu rename).
- Muat state dengan merge ke default, tahan file rusak/hilang.
- Basket, level BE/trailing, tracking equity harian, dan cooldown semua ikut tersimpan sehingga selamat dari restart/crash VPS.

### Indikator strategi (`strategy.py`)
- EMA (default period 200).
- ATR dengan smoothing Wilder (sama seperti iATR di MT5).
- SuperTrend (replikasi logika finalUpper/finalLower/trend dari EA MQ5 asli).
- Sinyal tren: bullish jika SuperTrend naik DAN close di atas EMA200; bearish kebalikannya; 0 jika data kurang.
- Opsi pakai bar yang sudah close (`USE_CLOSED_BAR_SIGNAL`) untuk hindari sinyal dari candle berjalan.
- Jarak grid adaptif berbasis ATR (dalam persen harga), dibatasi min/maks.

---

## 3. Fitur Bot Grid Martingale (`bot.py`)

### Entry
- Entry awal BUY MARKET saat sinyal bullish (SuperTrend + EMA200) dan belum ada posisi.
- **Grid martingale**: menambah layer BUY saat harga turun >= jarak grid dari layer terakhir; nominal tiap layer = layer sebelumnya x `LOT_MULTIPLIER` (default 1.3).
- Jarak grid adaptif dari ATR, dibatasi `GRID_MIN_PCT` (0.6%) sampai `GRID_MAX_PCT` (3.0%), atau nilai tetap jika ATR dimatikan.
- Maks layer: `MAX_GRID_LAYERS` (default 5).
- Opsi hanya tambah layer bila tren masih valid (`ONLY_ADD_IF_TREND_VALID`).
- **BUY-only**: tidak ada short (batasan Binance Spot). Saat tren berbalik turun, bot tidak buka posisi apa pun dan tidak jual paksa.

### Exit (dikelola sendiri oleh bot, karena Spot tidak punya SL bursa)
- **Take Profit basket**: tutup semua bila profit basket >= `BASKET_TP_PCT` (1.2%).
- **Breakeven**: setelah profit >= `BE_TRIGGER_PCT` (0.7%), kunci stop di atas harga rata-rata (`BE_LOCK_PCT` 0.1%).
- **Trailing stop**: setelah profit >= `TRAILING_START_PCT` (1.5%), stop mengikuti harga naik dengan jarak `TRAILING_STEP_PCT` (0.4%).

### Kontrol risiko
- Ukuran order: nominal USDT tetap (`INITIAL_ORDER_USDT`) atau persen saldo (`RISK_PERCENT`).
- Batas per order (`MAX_ORDER_USDT`) dan batas total eksposur seluruh grid (`MAX_TOTAL_EXPOSURE_USDT`).
- Stop drawdown dari puncak equity (`USE_EQUITY_STOP`, default MATI) + cooldown jam.
- Stop rugi harian / target profit harian (`USE_DAILY_STOP`, default MATI).
- Opsi tutup semua posisi saat kena limit (`CLOSE_ALL_AT_LIMIT`).
- Filter spread bid-ask maksimum (`MAX_SPREAD_PCT` 0.15%).
- Cooldown setelah close, jeda minimum antar order, sinkronisasi waktu berkala.

### Operasional
- Mode DRY_RUN (simulasi tanpa order sungguhan).
- Logging ke konsol + file berputar (RotatingFileHandler, 5 MB x 5).
- Heartbeat berkala (default tiap 5 menit).
- Shutdown rapi via sinyal SIGINT/SIGTERM.
- Auto-stop setelah 10 error beruntun.
- `--selftest`: uji pembulatan qty, signature HMAC, EMA/ATR/SuperTrend, dan avg price basket (LULUS).

---

## 4. Fitur Bot Pump Scanner (`pump_scanner_bot.py`)

### Seleksi kandidat (`market_scanner.py`)
- Ambil ticker 24 jam SEMUA pair dalam satu request, tiap `MARKET_SCAN_INTERVAL_SECONDS` (5 menit).
- Filter: naik >= `MIN_PUMP_PCT_24H` (8%), volume 24h >= `MIN_QUOTE_VOLUME_USDT_24H` (2 juta USDT), bukan token leverage (UP/DOWN/BULL/BEAR), bukan stablecoin.
- Ranking berdasarkan % kenaikan tertinggi.
- Konfirmasi momentum candle 5m untuk top-N kandidat: rata-rata 3 close terakhir > 3 close sebelumnya DAN candle terakhir bukan reversal/topping (posisi close di dalam range).
- **Filter VWAP** (`USE_VWAP_FILTER`, ditambahkan setelah audit awal ini, default AKTIF) --
  kandidat yang lolos konfirmasi momentum masih dicek terhadap VWAP BERGULIR jangka pendek
  (window = `CONFIRM_LOOKBACK_BARS`, candle yang sama dipakai konfirmasi momentum, BUKAN
  VWAP 24 jam/sesi). VWAP = `total(quote_volume)/total(volume)` pada window itu. Kandidat
  DITOLAK kalau harga masih di bawah VWAP (tekanan beli belum dominan) atau sudah lebih dari
  `VWAP_MAX_EXTENSION_PCT` (5%) di atas VWAP (harga sudah kepanasan/ekstrem, risiko besar beli
  di puncak lokal). Sebelum fitur ini ada, satu-satunya penyaring "kualitas" leg pump hanya
  `confirm_momentum()` (arah + bentuk candle), tanpa ukuran seberapa jauh harga sudah
  menyimpang dari rata-rata transaksi riil terbaru -- filter VWAP menutup celah ini.

### Entry
- Beli SATU koin pada satu waktu (tanpa martingale/averaging-down).
- Cek spread sebelum masuk (`MAX_SPREAD_PCT` 0.5%, lebih longgar dari mode grid).

### Exit
- **Stop Loss** (`SL_PCT` 3%, ditambahkan setelah audit awal ini) -- batas kerugian
  maksimum dari harga entry, dicek PALING AWAL sebelum kondisi exit lainnya. Sebelum
  fitur ini ada, bot TIDAK punya batas bawah kerugian sama sekali: kalau harga
  langsung turun sejak entry dan tidak pernah naik ke `BE_TRIGGER_PCT`, posisi akan
  terus dipegang sampai `MAX_HOLD_MINUTES` habis, rugi berapa pun. Stop Loss menutup
  celah ini.
- Take Profit (`TP_PCT` 6%).
- Breakeven (trigger 3%, kunci 0.3%).
- Trailing stop (mulai 4%, step 1.5%).
- Batas waktu hold maksimum (`MAX_HOLD_MINUTES` 240 menit) supaya tidak nyangkut di pump yang sudah mati.
- **Momentum fade exit**: keluar dini bila koin sudah jatuh keluar dari top-30 gainer.

### Kontrol risiko & operasional
- Ukuran posisi: persen saldo (`RISK_PERCENT`) atau nominal tetap, dibatasi `MAX_POSITION_USDT`.
- Stop drawdown & stop harian (konsep sama dengan mode grid, keduanya default MATI).
- File state & log terpisah (`pump_bot_state.json`, `pump_bot.log`) supaya bisa jalan bareng mode grid.
- Refresh cache filter tiap 6 jam.
- DRY_RUN, heartbeat, shutdown rapi, auto-stop 10 error beruntun, `--selftest` (LULUS).

---

## 5. Status Verifikasi

- `python bot.py --selftest` -> LULUS semua.
- `python pump_scanner_bot.py --selftest` -> LULUS semua.
- Selftest hanya menguji logika murni, tanpa jaringan. Perilaku terhadap order live (fill nyata, slippage, saldo) tidak bisa diuji tanpa koneksi Binance.

---

## 6. Temuan Penting (harap diperhatikan)

Ini bukan bagian dari "daftar fitur", tapi muncul saat audit dan berdampak pada keamanan:

1. **`DRY_RUN` default = `False` di kedua config**, padahal komentar di README dan header file menyebut defaultnya `True`. Artinya menjalankan `python bot.py` tanpa mengubah apa pun akan LANGSUNG kirim order sungguhan. Log (`pump_bot.log`) mengonfirmasi bot ini sudah pernah jalan live pada 2026-09-13 (contoh: beli LSKUSDT saat +232% dalam 24 jam, tutup via Take Profit). Pertimbangkan mengembalikan default ke `True` agar aman.

2. **`PUMP_CONFIG` pakai `USE_RISK_PERCENT = True` dengan `RISK_PERCENT = 95.0`**, artinya tiap entry memakai 95% saldo USDT free. Sangat agresif untuk strategi momentum-chasing di altcoin. Batas `MAX_POSITION_USDT = 10.0` menahan nominalnya, tapi kombinasi ini perlu Anda sadari.

3. **Sifat strategi**: grid martingale menambah posisi saat harga melawan, berisiko rugi besar saat tren turun panjang. Mode pump bersifat reaktif (masuk setelah harga sudah naik), rawan beli di puncak. Ini karakter strategi, bukan bug.

4. **Bot harus jalan 24/7** di VPS karena breakeven/trailing/TP dieksekusi oleh bot sendiri, bukan oleh bursa. Jika bot mati, level-level itu tidak tereksekusi.

5. Order selalu MARKET, jadi rentan slippage terutama di altcoin ilikuid. Filter volume dan spread membantu tapi tidak menjamin.

Tidak ditemukan bug yang membuat program crash atau error saat selftest. Semua modul konsisten dan saling terhubung dengan benar.
