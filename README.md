# Bot Grid Martingale — Binance Spot (BTCUSDT)

Adaptasi dari `Gold_Grid_Martingale_Pro.mq5` (MetaTrader 5) ke Python + Binance Spot API resmi.

## Isi paket

| File | Fungsi |
|---|---|
| `config.py` | Semua parameter strategi, risiko, dan kredensial |
| `binance_client.py` | Klien REST Binance Spot (dibuat manual, signature sudah diverifikasi cocok dengan contoh resmi Binance) |
| `market_scanner.py` | Filter & ranking koin "pump" + konfirmasi momentum (mode 2) |
| `pump_scanner_bot.py` | Program mode 2: scan pasar, rotasi 1 koin (`--selftest` tersedia juga) |
| `strategy.py` | SuperTrend + EMA + perhitungan jarak grid (replikasi logika MQ5) |
| `state.py` | Penyimpanan state basket ke file JSON (tahan restart) |
| `bot.py` | Program utama + mode `--selftest` |
| `requirements.txt` | Dependency Python |

## Perbedaan penting dari EA MT5 asli (WAJIB DIPAHAMI)

1. **Hanya BUY, tidak ada short.** Binance Spot tidak mendukung short-sell.
   Saat sinyal tren berbalik turun, bot **tidak** membuka posisi apa pun dan
   **tidak** memaksa jual — basket yang sudah ada tetap dikelola lewat Take
   Profit / Breakeven / Trailing seperti biasa.
2. **Satuan diganti dari "points" ke persentase harga.** XAUUSD dan BTCUSDT
   punya karakter volatilitas yang sangat berbeda, jadi jarak grid, TP,
   breakeven, dan trailing semuanya dihitung sebagai persentase dari harga
   rata-rata basket, bukan poin harga absolut.
3. **"Lot" diganti nominal USDT** per entry (`INITIAL_ORDER_USDT` atau
   `RISK_PERCENT` dari saldo).
4. **Tidak ada Stop Loss di level bursa.** Karena ini akun Spot (tanpa
   leverage/likuidasi), breakeven dan trailing dipantau dan dieksekusi oleh
   bot itu sendiri secara terus-menerus. **Bot harus berjalan 24/7** di VPS
   agar level-level ini benar-benar tereksekusi.
5. Semua nilai default parameter adalah **titik awal yang wajar, bukan hasil
   backtest** untuk BTCUSDT. Silakan sesuaikan setelah mengamati perilaku
   bot di mode `DRY_RUN`.

## Cara menjalankan

```bash
pip install -r requirements.txt

# 1) Audit logika inti tanpa koneksi apa pun (wajib dijalankan dulu)
python bot.py --selftest

# 2) Set kredensial API (jangan taruh langsung di file config.py)
export BINANCE_API_KEY="isi_api_key_anda"
export BINANCE_API_SECRET="isi_api_secret_anda"

# 3) Jalankan dengan DRY_RUN=True (default di config.py) -- bot memakai
#    data pasar ASLI dan menjalankan logika ASLI, tapi TIDAK mengirim order
python bot.py

# 4) Setelah yakin, ubah DRY_RUN menjadi False di config.py, lalu jalankan lagi
python bot.py
```

### Menjalankan 24/7 di VPS

Gunakan `systemd`, `tmux`, atau `screen` supaya proses tidak mati saat sesi
SSH terputus. Contoh sederhana dengan `tmux`:

```bash
tmux new -s gridbot
python bot.py
# Ctrl+B lalu D untuk detach; sesi tetap berjalan di background
```

## Cara kerja singkat

1. Setiap candle **M5 yang baru saja close**, bot menghitung SuperTrend
   (berbasis ATR Wilder) dan EMA200 — persis konsep di EA asli.
2. Jika sinyal bullish (trend naik **dan** harga di atas EMA200) dan tidak
   ada posisi terbuka → bot membuka **BUY MARKET** awal.
3. Selama posisi berjalan, jika harga turun dari entry terakhir sejauh
   `grid_step_pct` (dihitung dari ATR, dibatasi `GRID_MIN_PCT`–`GRID_MAX_PCT`)
   **dan** tren masih bullish (jika `ONLY_ADD_IF_TREND_VALID=True`) → bot
   menambah layer BUY baru dengan nominal `qty_sebelumnya × LOT_MULTIPLIER`
   (martingale), sampai maksimum `MAX_GRID_LAYERS`.
4. Setiap 15 detik (`LOOP_INTERVAL_SECONDS`), bot mengecek basket:
   - **Take Profit**: tutup semua jika profit basket ≥ `BASKET_TP_PCT`.
   - **Breakeven**: begitu profit ≥ `BE_TRIGGER_PCT`, kunci stop di atas
     harga rata-rata (`BE_LOCK_PCT`), tutup jika harga turun ke level itu.
   - **Trailing**: begitu profit ≥ `TRAILING_START_PCT`, trailing stop
     mengikuti harga naik dengan jarak `TRAILING_STEP_PCT`, tutup jika
     tersentuh.
5. Kontrol risiko: batas drawdown dari puncak equity
   (`MAX_DRAWDOWN_PERCENT`), batas rugi harian, target profit harian, batas
   total eksposur (`MAX_TOTAL_EXPOSURE_USDT`) — semua bisa memaksa jeda
   entry baru atau (opsional) menutup semua posisi.

## Yang sudah diaudit sebelum diserahkan

- `python bot.py --selftest` menguji: pembulatan quantity sesuai filter
  `LOT_SIZE`, signature HMAC (dicocokkan byte-per-byte dengan contoh resmi
  di dokumentasi Binance), perhitungan EMA/ATR/SuperTrend dengan data
  sintetis, dan perhitungan rata-rata harga basket.
- Simulasi integrasi penuh (entry awal → penambahan grid layer → exit lewat
  Take Profit) dijalankan secara lokal tanpa jaringan dan berhasil tanpa
  error, dengan rasio martingale antar-layer sesuai `LOT_MULTIPLIER`.
- Parsing `exchangeInfo` sengaja ditulis manual (bukan pakai SDK resmi
  `binance-sdk-spot` versi terbaru) karena versi SDK tersebut per audit
  ternyata gagal mem-parsing daftar `filters` (LOT_SIZE/NOTIONAL) dari JSON
  asli — jadi lebih aman memakai parsing JSON mentah langsung dari endpoint
  resmi `GET /api/v3/exchangeInfo`.
- Signature request sudah mengikuti aturan terbaru Binance (berlaku sejak
  15 Januari 2026): payload di-percent-encode dulu sebelum dihitung HMAC.

**Yang belum bisa diaudit oleh saya**: perilaku terhadap order sungguhan di
akun live Anda (fill price, slippage nyata, saldo nyata, dsb), karena
sandbox saya tidak memiliki akses jaringan ke `api.binance.com`. Ini alasan
utama kenapa `DRY_RUN=True` adalah default dan kenapa Anda WAJIB menjalankan
bot dalam mode itu dulu sebelum mengaktifkan order sungguhan.

## Peringatan risiko (harap dibaca)

Strategi **grid martingale menambah ukuran posisi saat harga bergerak
melawan Anda**. Ini bisa terlihat "menang terus" dalam kondisi pasar
sideways/berayun, tapi berisiko kerugian besar dan cepat saat terjadi tren
turun yang panjang dan dalam — situasi yang cukup umum terjadi di BTC.
Modal awal yang Anda sebutkan (< 500 USDT) sudah saya pertimbangkan dalam
nilai default (`INITIAL_ORDER_USDT=15`, `MAX_TOTAL_EXPOSURE_USDT=150`),
tapi Anda tetap harus:

- Menjalankan mode `DRY_RUN` cukup lama untuk melihat perilaku bot di
  kondisi pasar nyata sebelum memakai uang sungguhan.
- Hanya memakai dana yang benar-benar siap Anda rugikan sepenuhnya.
- Memantau bot secara berkala meskipun ia berjalan otomatis 24/7.

Ini bukan nasihat keuangan. Anda bertanggung jawab penuh atas keputusan dan
risiko trading Anda sendiri.

---

## Mode Pump Scanner (`pump_scanner_bot.py`)

Mode kedua, terpisah dari bot grid martingale di atas. Alih-alih trading di
satu pair tetap (BTCUSDT), bot ini **memindai SEMUA pair USDT** di Binance
Spot untuk mencari koin yang sedang naik tajam.

### Cara kerja

1. Setiap 5 menit (`MARKET_SCAN_INTERVAL_SECONDS`), bot mengambil data 24
   jam SEMUA pair sekaligus (`GET /api/v3/ticker/24hr` tanpa parameter
   symbol -- satu request untuk seluruh pasar).
2. Filter: naik ≥ `MIN_PUMP_PCT_24H` dalam 24 jam, volume 24 jam ≥
   `MIN_QUOTE_VOLUME_USDT_24H` (hindari koin ilikuid), bukan token leverage
   (xxxUP/DOWN/BULL/BEAR), bukan pair stablecoin-ke-stablecoin.
3. Dari hasil yang lolos, diambil top-N (`TOP_N_CANDIDATES_TO_CONFIRM`)
   untuk dicek candle 5 menit terakhirnya: apakah momentum jangka pendek
   masih naik dan candle terakhir bukan reversal/topping. Kandidat pertama
   yang lolos konfirmasi ini yang dibeli.
4. Bot hanya memegang **1 koin dalam satu waktu** (sesuai pilihan Anda).
   Tidak ada averaging-down/martingale di mode ini -- satu kali entry per
   rotasi, karena averaging-down pada koin yang sedang "gagal pump" (dan
   berpotensi dump tajam, terutama altcoin kecil) jauh lebih berisiko
   dibanding averaging-down pada tren naik yang sudah stabil seperti di
   mode grid.
5. Keluar dari posisi lewat salah satu dari: Take Profit, Breakeven,
   Trailing Stop, batas waktu hold maksimum (`MAX_HOLD_MINUTES` --
   mencegah "nyangkut" di pump yang sudah mati), atau momentum pudar
   (koin sudah tidak masuk top-N gainer lagi saat scan berikutnya).

### PERINGATAN KHUSUS mode ini

- **Ini reaktif, bukan prediktif.** Bot masuk SETELAH harga sudah naik
  signifikan. Ada risiko nyata membeli di puncak / menjelang koreksi.
- **Altcoin kecil rawan manipulasi** (pump-and-dump yang memang disengaja,
  wash trading untuk mengelabui filter volume, dsb). Filter volume minimum
  membantu tapi tidak menjamin keamanan penuh.
- **Spread & slippage** di altcoin jauh lebih besar dari BTCUSDT --
  `MAX_SPREAD_PCT` default di mode ini sengaja dilonggarkan (0.5%)
  dibanding mode grid (0.15%), tapi tetap perhatikan baik-baik.
- Jalankan `python pump_scanner_bot.py --selftest` dulu, lalu `DRY_RUN=True`
  cukup lama untuk melihat koin apa saja yang biasanya terpilih, sebelum
  memakai uang sungguhan.

### Menjalankan mode ini

```bash
python pump_scanner_bot.py --selftest
python pump_scanner_bot.py
```

State dan log mode ini terpisah dari mode grid (`pump_bot_state.json`,
`pump_bot.log`), jadi kedua mode bisa dijalankan bersamaan di dua jendela
terminal berbeda tanpa saling tabrakan file -- **tapi tetap perhatikan**
kalau keduanya dijalankan bersamaan, total modal yang terpakai adalah
gabungan dari kedua mode, jadi sesuaikan `MAX_TOTAL_EXPOSURE_USDT` (mode
grid) dan `MAX_POSITION_USDT` (mode pump scanner) supaya tidak melebihi
modal total Anda.
