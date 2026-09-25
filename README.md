# Bot Pump Scanner — Binance Spot

Bot ini **memindai SEMUA pair USDT** di Binance Spot untuk mencari setup
**pullback dan retest**: harga menembus sebuah swing high, lalu kembali
menguji level yang baru ditembus itu dan ditolak naik lagi. Bot masuk dengan
**satu entry** (long only, spot) dan keluar lewat Stop Loss / Take Profit /
Breakeven / Trailing / pembatalan setup.
**Tidak ada averaging-down/martingale.**

Strategi lama (mengejar top gainer 24 jam dengan konfirmasi momentum, plus
exit `MOMENTUM_FADE`) sudah **dihapus total**, bukan dinonaktifkan.

> Catatan: repo ini dulu juga berisi bot grid martingale (`bot.py`) untuk satu
> pair tetap (BTCUSDT). Fitur itu sudah dihapus. Yang tersisa sekarang hanya
> bot pump scanner.


## Mode PAPER dan LIVE

Bot ini punya dua mode. Mode runtime dipilih lewat tab **Kontrol** dan disimpan
atomik di `pump_bot_runtime.json`. Default tetap PAPER:

| | `MODE="PAPER"` (default) | `MODE="LIVE"` |
|---|---|---|
| Eksekusi order | **Disimulasikan lokal** (tidak ada order sungguhan) | Sungguhan, **uang asli** |
| Saldo | Virtual, dari `PAPER_INITIAL_BALANCES` (default 10.000 USDT) | Saldo Spot asli akun Anda |
| Data pasar | **ASLI** dari Binance produksi publik (REST + WebSocket) | Sama, ASLI dari produksi |
| API key | **Tidak perlu** (semua data publik, tanpa tanda tangan) | Wajib, key produksi di `.env` |
| Fee | Disimulasikan (`TAKER_FEE_PCT` + diskon BNB) | Dipotong Binance sungguhan |
| Dust sweep (`/sapi/*`) | Dilewati (endpoint bertanda tangan, dilarang di PAPER) | Aktif |
| Jalur LOGIKA strategi | **Sama persis** (lewat antarmuka `ExchangeClient`) | Sama persis |
| File state posisi | `pump_bot_state_paper.json` | `pump_bot_state_live.json` |
| File state akun simulasi | `pump_paper_account_paper.json` | (tidak dipakai) |
| File log | `pump_bot_paper.log` | `pump_bot_live.log` |
| File kontrol jual manual | `pump_bot_control_paper.json` | `pump_bot_control_live.json` |
| File perintah Stop | `pump_bot_control_paper.stop.json` | `pump_bot_control_live.stop.json` |
| File settings override | `pump_bot_settings_paper.json` | `pump_bot_settings_live.json` |
| File lock/lifecycle | `pump_bot_lock_paper.json` / `pump_bot_process_paper.json` | `pump_bot_lock_live.json` / `pump_bot_process_live.json` |

Perbedaan PAPER vs LIVE **hanya** ada di lapisan eksekusi order dan sumber
saldo. Semua logika strategi (scan, deteksi setup, sizing, SL/TP/BE/Trailing,
invalidasi setup) identik. Data pasar juga identik: keduanya menarik harga, order
book, kline, dan `exchangeInfo` dari Binance **produksi publik**.

### Arsitektur singkat

- `ExchangeClient` (antarmuka) -> `PaperClient` (simulasi) / `LiveClient` (asli).
  Bot hanya bicara ke antarmuka, tidak tahu mode mana yang aktif.
- `MarketDataProvider` (dipakai bersama) = WebSocket (primer, real-time) +
  REST publik keyless (untuk `exchangeInfo`, kline historis, snapshot depth,
  dan fallback saat WS basi/putus). REST di sini **allow_signed=False** sehingga
  mustahil menyentuh endpoint bertanda tangan.
- `PaperMatchingEngine` + `PaperStore` = mesin eksekusi simulasi + penyimpanan
  saldo/order/riwayat virtual (JSON atomik).

### Kelebihan mode PAPER

- Meniru realita yang biasa dilewatkan simulator lama (mode DRY_RUN yang sudah
  dihapus): pembulatan `LOT_SIZE`/`MARKET_LOT_SIZE`, `MIN_NOTIONAL`, slippage
  market order (dihitung dengan **berjalan melalui kedalaman order book asli**),
  partial fill, fee yang dipotong dari aset yang benar, dan bentuk respons +
  kode error yang identik dengan Binance (mis. `-1013`, `-2010`).
- Tidak perlu API key dan tidak ada risiko uang asli.
- Data pasar tetap nyata, jadi perilaku bot terhadap kondisi pasar sungguhan
  ikut teruji.
- State persisten: setelah restart, saldo virtual, posisi, dan order terbuka
  tetap ada.

### Batasan mode PAPER (jujur)

Hasil PAPER **bukan jaminan** hasil LIVE. Yang **tidak** dimodelkan:

- **Latensi jaringan** antara bot dan Binance.
- **Posisi antrean** Anda di dalam order book (untuk limit order).
- **Dampak pasar (market impact)** dari order Anda sendiri.
- **Perbedaan likuiditas** dan pergerakan harga saat order "dalam perjalanan".
- **Sisa market order** yang tak terisi karena kedalaman order book habis
  diperlakukan sebagai `EXPIRED` (penyederhanaan yang realistis).
- **Diskon BNB** dimodelkan sebagai **tarif fee lebih rendah** (0,075%) namun
  tetap dipotong dari aset yang diperdagangkan, bukan dari saldo BNB terpisah.
  Ini disengaja agar perilaku "fee mengurangi qty yang diterima" tetap terjaga
  sehingga qty jual tidak pernah melebihi saldo.
- Order book untuk mengisi order diambil dari **snapshot depth REST yang segar**
  saat order dikirim (bukan order book lokal dari depth-diff WebSocket), karena
  snapshot segar lebih setia untuk simulasi fill.

### Cara reset state PAPER

Gunakan **Kontrol > Reset Akun PAPER**. Reset hanya diizinkan saat mode aktif
PAPER dan bot sudah `STOPPED` atau `CRASHED`. Masukkan saldo awal positif,
ketik `RESET`, tinjau konfirmasi, lalu lanjutkan.

Dashboard memindahkan file akun dan state menjadi
`*.bak-<timestamp>` sebelum menyimpan saldo awal baru. File lama tidak dihapus.
Jika pengarsipan atau penyimpanan settings gagal, reset dibatalkan dan file
dikembalikan sebisa mungkin.

Saat Start berikutnya, jika file akun tidak ada, bot membuat saldo awal dari
`PAPER_INITIAL_BALANCES`. Jika file state rusak, bot membuat cadangan
`*.corrupt-<timestamp>` dan memberi tahu di log. File rusak tidak ditimpa
diam-diam.

### Pengaman mode

- `MODE` yang **tidak dikenal / kosong / typo** membuat bot **berhenti** dengan
  pesan jelas (`InvalidModeError`), TIDAK pernah jatuh diam-diam ke LIVE.
- Default `MODE` adalah `"PAPER"` (aman).
- Di PAPER, **tidak ada satu pun** request bertanda tangan yang dikirim, bahkan
  jika `BINANCE_API_KEY` kebetulan terisi di environment. Ada guard yang melempar
  `SignedEndpointBlockedError` bila ada kode yang mencoba.

### Peringatan

**Hasil PAPER bukan jaminan hasil LIVE.** PAPER menguji kebenaran mekanis dan
reaksi bot terhadap data pasar nyata, bukan menjamin profitabilitas dengan uang
asli. Slippage nyata, antrean order book, dan likuiditas riil bisa berbeda.



## Stop Loss & Take Profit: tetap atau berbasis ATR

Sejak versi ini bot mendukung dua cara menentukan jarak exit, diatur lewat
`USE_ATR_EXITS` di `config.py`. **Defaultnya `False`**, artinya perilaku lama
(SL_PCT/TP_PCT tetap) tidak berubah sama sekali kalau Anda tidak menyentuhnya.

### Cara kerja mode ATR

```
SL% = batasi(ATR_MULTIPLIER_SL x ATR%, antara ATR_SL_MIN_PCT dan ATR_SL_MAX_PCT)
TP% = SL% x ATR_TP_RR_RATIO
```

ATR (Average True Range, Wilder 1978) mengukur volatilitas koin. Level exit
dihitung **sekali saat entry lalu dikunci di state**. Stop tidak pernah
dilonggarkan setelah posisi dibuka, hanya bisa mengetat lewat Breakeven dan
Trailing. Kalau ATR gagal dihitung (candle kurang), bot otomatis jatuh ke
SL_PCT/TP_PCT tetap, jadi posisi tidak pernah dibiarkan tanpa stop.

### Mengubah parameter

| Tempat | Cakupan | Sifat |
|---|---|---|
| **Dashboard, tab Backtest** | Parameter uji exit dan ATR | Sementara, sekali jalan |
| **CLI** `backtest.py` | Sama, lewat flag | Sementara, sekali jalan |
| **Dashboard, tab Kontrol** | Schema lengkap strategi, risiko, sistem, dan watchlist | Persisten, terpisah PAPER/LIVE |
| **`config.py`** | Nilai bawaan immutable | Default aplikasi |

Tab Backtest dan CLI tidak menyimpan settings. Tab Kontrol menulis hanya file
override per mode, bukan mengubah source `config.py`. Nilai efektif adalah
default `config.py` yang digabung dengan override mode aktif. Tombol default
per field atau grup menghapus perbedaan terhadap default tersebut.

**Tombol Bandingkan Tetap vs ATR** menjalankan dua backtest pada candle yang
sama persis lalu menampilkan tabelnya berdampingan. Setara dengan:

```bash
python backtest.py --compare-atr --symbol SOLUSDT --days 30
```

Flag CLI untuk parameter ATR:

```bash
python backtest.py --symbol SOLUSDT --days 30 \
  --atr-multiplier 2.0 --atr-period 14 --atr-min 1.2 --atr-max 4.0 --atr-rr 2.0 \
  --atr-be-trigger 0.5 --atr-be-lock 0.1 \
  --atr-trail-start 1.0 --atr-trail-step 1.5
```

Saat toggle ATR menyala di dashboard, field Stop Loss dan Take Profit otomatis
diredupkan karena nilainya akan ditimpa hasil hitungan ATR. Di bawah form
muncul tabel preview berisi level yang benar-benar akan berlaku.

### Semua komponen exit ikut skala ATR

Saat `USE_ATR_EXITS = True`, **keempat** komponen dihitung dari ATR, bukan
hanya SL/TP:

| Komponen | Rumus | Contoh (ATR 2%) |
|---|---|---|
| Stop Loss | `clamp(2.0x ATR, 1.2%, 4.0%)` | 4,00% |
| Take Profit | `SL x 2.0` | 8,00% |
| Breakeven trigger | `0.5x ATR` | 1,00% |
| Breakeven lock | `0.1x ATR` | 0,20% |
| Trailing start | `1.0x ATR` | 2,00% |
| Trailing step | `1.5x ATR` | 3,00% |

Tidak ada lagi batas waktu hold: parameter `MAX_HOLD_MINUTES` sudah **dihapus
total** dari repo ini. Posisi hanya ditutup oleh harga dan struktur, bukan
oleh jam dinding.

**Kenapa BE/Trailing wajib ikut.** Kalau hanya SL/TP yang adaptif sementara
BE/Trailing memakai angka tetap, hasilnya pincang. Pada koin ATR 3%:

```
SL            -> 4,0%  (lebar, ikut ATR)
TP            -> 8,0%  (lebar, ikut ATR)
Trailing step -> 0,6% tetap = hanya 0,20x ATR
```

Pullback normal yang masih jauh di dalam 1x ATR langsung menyentuh trailing.
Posisi tertutup di sekitar +0,9% padahal TP 8% belum tersentuh, sehingga
**risk:reward terbalik**: risiko 4%, imbalan 0,9%.

Terukur di backtest data sintetis:

| | TP tercapai | Ditutup BE/Trail | Profit factor |
|---|---|---|---|
| Pincang (BE/Trail tetap) | 2,9% | 91,2% | 3,60 |
| **Semua ikut ATR** | **8,5%** | **80,4%** | **4,70** |
| Semua tetap (ATR off) | 5,9% | 78,0% | 2,48 |

Dua pengaman otomatis di kode:

1. **Trailing step dibatasi tidak melebihi SL.** Kalau lebih longgar, SL
   selalu kena duluan dan trailing cuma ilusi.
2. **Breakeven dipaksa terpicu sebelum Trailing.** Kalau terbalik, urutan
   proteksinya kacau.

Angka di tabel berasal dari data sintetis, jadi besarannya tidak berlaku untuk
pasar asli. Yang valid adalah arah masalahnya, dan itu struktural.

### Mana yang lebih bagus menurut data

Jawaban jujurnya: **bukti yang ada terbelah, dan tidak ada yang diuji pada
strategi Anda.**

Mendukung ATR:

- Backtest 9.433 trade lintas 6 pasar menemukan multiplier 2.0x ATR memberi
  profit factor terbaik; BTCUSD harian mencapai PF 1,72 dengan drawdown
  maksimum 4,6% (quant-signals.com).
- Uji 595+ simbol periode 2018-2025 melaporkan stop ter-adjust volatilitas
  memangkas stop-out prematur 34% dibanding stop dolar tetap; stop di bawah
  1,0x ATR terpicu noise >65% dalam 3 bar pertama, di 1,5x ATR turun ke 38%
  (volatilitybox.com).

Menentang ATR:

- Pengujian Kevin Davey lintas banyak strategi dan pasar: ATR menang hanya
  66% kasus, sepertiganya stop tetap lebih baik. Bahkan kasus "tanpa stop
  sama sekali" sering mengalahkan keduanya (kjtradingsystems.com).
- Backtest crypto 15 menit menemukan fixed -3,5% mengungguli ATR 2x pada win
  rate dan profit factor, dengan ATR menghasilkan 15-20% lebih banyak
  variance per trade (trendrider.net).
- ATR trailing stop berkinerja buruk: positif hanya di 4 dari 12 uji harian
  (quant-signals.com). Karena itu fitur ini **tidak** menyentuh trailing.

Bukti akademik (kualitas lebih kuat, karena bukan milik vendor):

- Barroso & Santa-Clara (2015), *Momentum Has Its Moments*: momentum yang
  di-scale terhadap volatilitas realisasi kira-kira menggandakan Sharpe ratio,
  terutama karena eksposur mengecil sebelum periode crash.
- *Cryptocurrency momentum has (not) its moments* (Financial Markets and
  Portfolio Management, 2025): momentum crypto mengalami crash parah sampai
  satu koin tunggal bisa membatalkan return portofolio. Volatility management
  membantu meredam, **tapi tidak mengubah tail risk**.

### Kenapa fitur ini relevan khusus untuk bot ini

Konsensus semua sumber sepakat pada satu titik: ATR paling unggul ketika
memperdagangkan **banyak instrumen dengan volatilitas berbeda-beda**. Bot ini
persis kasus itu. `SL_PCT = 1.8` dipakai sama rata untuk semua koin, padahal
semesta kandidat mencakup koin dengan volatilitas yang berbeda jauh satu sama
lain. Stop 1,8%
bisa berarti 3x ATR di satu koin tapi hanya 0,8x ATR di koin lain.

Namun ada dua hal yang melawan:

1. **Timeframe.** ATR jauh lebih baik di timeframe harian; PF jatuh dari 1,72
   ke 0,96 di timeframe per jam karena noise. Bot ini pakai candle 5 menit,
   jadi ada di ujung yang kurang menguntungkan ATR.
2. **`RISK_PERCENT = 95.0`.** Prinsip inti ATR adalah stop melebar harus
   dibarengi posisi mengecil supaya risiko rupiah konstan. Fitur ini **belum**
   menyentuh position sizing, jadi logika itu masih belum lengkap.

### Buktikan sendiri, jangan percaya angka di atas

Semua angka di atas berasal dari pasar, timeframe, dan aturan entry yang
berbeda dari milik Anda. Satu-satunya data yang valid untuk strategi Anda
adalah backtest Anda sendiri:

```bash
# Bandingkan tetap vs ATR pada data historis yang sama persis
python backtest.py --compare-atr --symbol SOLUSDT --days 30

# Coba multiplier dan batas lain
python backtest.py --compare-atr --symbol SOLUSDT --days 30 \
    --atr-multiplier 1.5 --atr-min 1.0 --atr-max 6.0 --atr-rr 2.0
```

Perintah itu menjalankan backtest dua kali pada candle yang sama, dengan semua
parameter lain identik, sehingga selisihnya murni berasal dari metode exit.
Outputnya juga menampilkan sebaran stop ATR: kalau banyak trade "kena batas
atas", berarti `ATR_SL_MAX_PCT` Anda terlalu ketat dan ATR praktis tidak
bekerja.

Uji beberapa simbol dan beberapa periode. Kalau jumlah trade di bawah 30,
selisih sebesar apa pun kemungkinan besar kebetulan, dan alat ini akan
memperingatkan Anda soal itu.



## Ukuran posisi & biaya trading

### `MAX_POSITION_USDT` sekarang opsional

Kode menghitung ukuran posisi begini:

```
nominal = saldo_free x (1 - BALANCE_BUFFER_PCT%) x RISK_PERCENT%
kalau MAX_POSITION_USDT > 0: nominal = min(nominal, MAX_POSITION_USDT)
```

**Peringatan soal config lama.** Sebelumnya `RISK_PERCENT = 95.0` dipasang
bersama `MAX_POSITION_USDT = 10.0`. Karena plafon selalu menang, persentase
itu praktis tidak pernah terpakai:

| Saldo | Niat 95% | Yang benar-benar dipakai |
|---|---|---|
| 100 USDT | 95,00 | 10,00 (10% saldo) |
| 1.000 USDT | 950,00 | 10,00 (1% saldo) |
| 5.000 USDT | 4.750,00 | 10,00 (0,2% saldo) |

Makin besar saldo, makin kecil persentase sesungguhnya. Default saat ini adalah
`MAX_POSITION_USDT = 100`. Nilai `0` berarti tanpa plafon dan hanya diizinkan
untuk PAPER. Settings LIVE mewajibkan angka lebih besar dari nol agar uang asli
tidak dapat dijalankan tanpa plafon eksplisit. Jika plafon memotong hasil
`RISK_PERCENT`, bot memperingatkan di log lengkap dengan persentase efektifnya.

### `BALANCE_BUFFER_PCT`

Bantalan kecil (default 0,5%) yang tidak ikut dibelanjakan. Ini alasan teknis,
bukan filosofi risiko: order MARKET diisi pada harga yang bergerak dan fee
taker dipotong dari saldo yang sama, jadi mencoba membelanjakan 100% saldo
persis sering ditolak bursa dengan error `-2010 insufficient balance`. Dengan
`RISK_PERCENT = 95` bantalan ini praktis tidak terasa; ia baru penting kalau
Anda menaikkan `RISK_PERCENT` mendekati 100.

### Soal kebiasaan all-in

All-in di spot manual berbeda dari all-in di bot. Manual, Anda memilih momen
dan bisa membatalkan niat. Bot mengulanginya otomatis tanpa jeda penilaian.
Yang berubah bukan besar risiko per trade, tapi berapa kali risiko itu diambil.

Dengan SL 1,8% dan seluruh saldo diputar tiap trade:

| SL beruntun | Saldo tersisa |
|---|---|
| 10 kali | 83,4% |
| 30 kali | 58,0% |
| 50 kali | 40,3% |

Perlu dicatat jujur: pada win rate 60%, kalah 10 kali beruntun hanya muncul
sekitar 0,05 kali per 500 trade. Jadi **all-in dengan SL ketat di spot bukan
skenario kiamat** seperti all-in di futures ber-leverage. Bahaya sebenarnya
ada di biaya.

### Biaya trading (`TAKER_FEE_PCT`, `USE_BNB_FEE_DISCOUNT`)

Binance Spot VIP0 per 2026 mengenakan 0,1% untuk maker maupun taker, dan
membayar fee dengan BNB memberi diskon 25% sehingga menjadi 0,075%
(dicek 2026-09-23). Bot selalu memakai order MARKET, jadi yang berlaku
adalah taker, dan dibayar **dua kali** (beli dan jual).

`backtest.py` sekarang memperhitungkan ini, dan memisahkan hasilnya:

```
Return KOTOR (%)      7853.84      <- tanpa biaya
Hilang krn fee (%)    5834.90      <- dampak compounding biaya
Return BERSIH (%)     2018.94      <- yang benar-benar Anda terima
```

Pada contoh di atas biaya menelan sekitar 74% hasil kotor. Inilah alasan
sesungguhnya kenapa all-in berulang berbahaya: bukan karena SL, tapi karena
setiap putaran mengenakan biaya pada **seluruh modal**, dan efeknya menumpuk.

Dua cara menekannya:

1. Nyalakan `USE_BNB_FEE_DISCOUNT = True` (dan simpan sedikit BNB di Spot
   wallet). Biaya turun dari 0,2% jadi 0,15% per putaran.
2. `MAX_SPREAD_PCT` sudah diturunkan dari `0.5` ke `0.25`. Spread adalah
   biaya nyata yang dibayar setiap masuk lewat order MARKET, dan 0,5%
   memakan 12,5% dari target TP 4%.


## Isi paket

| File | Fungsi |
|---|---|
| `config.py` | Default immutable dan penggabungan runtime/settings per mode |
| `settings_schema.py` | Schema lengkap, validasi, diff, override, dan audit |
| `runtime_control.py` | Lock, lifecycle, Start/Stop/Restart, dan pemilik child bot |
| `procctl.py` | Abstraksi sinyal, process group, dan Windows Job Object |
| `atomic_io.py` | Write/replace atomik dengan retry Windows |
| `credential_store.py` | Penyimpanan `.env` dan verifikasi izin/ACL |
| `binance_client.py` | Klien REST Binance Spot |
| `market_scanner.py` | Saringan semesta, deteksi setup pullback retest, dan pemilihan kandidat terbaik |
| `pump_scanner_bot.py` | Program bot (`--selftest` tersedia) |
| `strategy.py` | Struktur candle (`Kline`), parser klines, ATR, anchored VWAP, dan ukuran jendela bersama |
| `synthetic_data.py` | Generator candle sintetis yang dipakai bersama selftest, backtest, dan tes |
| `state.py` | State posisi dan kanal kontrol terpisah |
| `run.py` | Dashboard utama sekaligus auto-start bot terkelola |
| `dashboard.py` | API pemantauan dan kontrol lokal berbasis Flask |
| `templates/dashboard.html` | UI responsif self-contained tanpa CDN |
| `backtest.py` | Mesin backtest parameter (dipanggil dashboard, bisa juga `python backtest.py` untuk selftest) |
| `requirements.txt` | Dependency Python |

## Cara kerja

1. Setiap 5 menit (`MARKET_SCAN_INTERVAL_SECONDS`), bot mengambil data 24
   jam SEMUA pair sekaligus (`GET /api/v3/ticker/24hr` tanpa parameter
   symbol -- satu request untuk seluruh pasar).
2. Saringan semesta: volume 24 jam >= `MIN_QUOTE_VOLUME_USDT_24H` (hindari
   koin ilikuid), status simbol TRADING di `exchangeInfo`, bukan token
   leverage (xxxUP/DOWN/BULL/BEAR), bukan pair stablecoin-ke-stablecoin,
   bukan simbol di `EXTRA_EXCLUDE_SYMBOLS`, dan umur listing minimal
   `MIN_LISTING_AGE_DAYS`.
3. **Gerbang pump (WAJIB).** Simbol hanya lanjut ke tahap berikutnya kalau
   KEDUA syarat ini terpenuhi:
   - kenaikan harga 24 jam (`priceChangePercent` dari ticker yang sudah
     diambil di langkah 1, tanpa request tambahan) **>= `PUMP_MIN_24H_CHANGE_PCT`**
     (default 10%); dan
   - volume sedang naik, yaitu `quoteVolume` 24 jam berjalan
     **>= `PUMP_VOLUME_SURGE_MULT` x rata-rata volume kuotasi 7 hari penuh
     sebelumnya** (default 1,5x). Rata-rata dihitung dari candle `1d` yang
     sudah tertutup, dan candle harian itu **hanya diminta untuk simbol yang
     sudah lolos syarat kenaikan**, jadi beban rate limit tetap kecil
     (bobot IP 2 per simbol).

   Koin yang sedang turun 24 jam, volumenya tidak naik, belum punya 7 candle
   harian penuh (baru listing), atau candle hariannya gagal diambil,
   **ditolak** (fail closed). Kalau tidak ada satu pun yang lolos, log menulis
   baris eksplisit "0 kandidat lolos gerbang pump" tiap siklus scan, jadi
   kondisi sepi terlihat jelas di `pump_bot_paper.log` / `pump_bot_live.log`.
   Gerbang ini dipakai jalur live, backtest satu simbol, backtest portofolio,
   dan penyegar watchlist lewat satu fungsi bersama
   (`market_scanner.is_pumping_today()` / `evaluate_pump_gate()`). Di jalur
   backtest, rata-rata 7 hari selalu dihitung dari candle harian yang sudah
   tertutup **pada titik waktu yang sedang diuji**, bukan data hari ini, jadi
   tidak ada look-ahead.
4. Dari hasil yang lolos, diambil top-N paling likuid
   (`TOP_N_CANDIDATES_TO_CONFIRM`, batas ini murni demi rate limit) untuk
   dicek candle 5 menitnya dengan `detect_pullback_retest()`:
   swing high pivot -> breakout close di atas level plus
   `BREAKOUT_BUFFER_ATR_MULT` x ATR -> anchored VWAP dari candle breakout ->
   pullback ke zona `RETEST_ZONE_ATR_MULT` x ATR dengan konfluensi VWAP dan
   close di atas VWAP -> konfirmasi retest di candle terakhir yang sudah
   tertutup (`MIN_CLOSE_POSITION_IN_RANGE`) -> cek invalidasi
   (`INVALIDATION_ATR_MULT`, `MAX_BARS_BREAKOUT_TO_RETEST`,
   `MAX_RETEST_TOUCHES`) -> anti-kejar (`MAX_EXTENSION_ATR_MULT`).
   Dari semua kandidat yang setupnya sah, yang dibeli adalah yang **paling
   rapat** terhadap level breakout relatif ATR, dengan volume kuotasi sebagai
   pemutus seri.
5. Bot hanya memegang **1 koin dalam satu waktu**. Tidak ada
   averaging-down/martingale -- satu kali entry per rotasi, karena
   averaging-down pada koin yang sedang "gagal pump" (dan berpotensi dump
   tajam, terutama altcoin kecil) sangat berisiko.
6. Keluar dari posisi lewat salah satu dari: **Stop Loss** (`SL_PCT`,
   default 3% -- batas kerugian maksimum dari harga entry, dicek PALING
   AWAL sebelum kondisi lain), Take Profit, Breakeven, Trailing Stop,
   atau **setup batal** (`SETUP_INVALIDATION_EXIT`:
   candle tertutup jatuh di bawah batas invalidasi yang dikunci saat entry,
   yaitu level breakout dikurangi `INVALIDATION_ATR_MULT` x ATR).

   **Tidak ada exit berbasis batas waktu.** `MAX_HOLD_MINUTES` beserta alasan
   keluar `MAX_HOLD_TIME` sudah dihapus total dari config, tab Setelan, panel
   backtest, bot live, dan kedua mesin backtest. Posisi yang setupnya masih
   utuh tidak akan ditutup hanya karena sudah lama dipegang. Konsekuensinya
   perlu disadari: satu posisi bisa dipegang tanpa batas waktu sampai Stop
   Loss, Take Profit, Breakeven, Trailing Stop, atau batas invalidasi setup
   tersentuh, jadi pastikan salah satu dari exit itu memang aktif.

### Parameter setup pullback retest

Semua parameter di bawah bisa diubah dari tab Setelan (divalidasi
`settings_schema.py`) dan dipakai bersama oleh bot live, backtest satu simbol,
backtest portofolio, dan penyegar watchlist.

| Parameter | Default | Arti |
|---|---|---|
| `SWING_LOOKBACK_BARS` | 12 | Berapa candle terakhir yang dicari swing high-nya |
| `SWING_PIVOT_WING_BARS` | 2 | Jumlah candle di kiri dan kanan yang harus lebih rendah agar sebuah high disebut pivot. Sayap kanan wajib sudah tertutup, jadi tidak ada look-ahead |
| `BREAKOUT_BUFFER_ATR_MULT` | 0.10 | Jarak minimum di atas level agar sebuah close dianggap breakout, dalam satuan ATR |
| `VWAP_MIN_BARS_AFTER_ANCHOR` | 2 | Minimum candle setelah candle breakout sebelum anchored VWAP dianggap bermakna |
| `RETEST_ZONE_ATR_MULT` | 0.5 | Setengah lebar zona retest di sekitar level, dalam satuan ATR |
| `RETEST_VWAP_CONFLUENCE_ATR_MULT` | 1.0 | Jarak maksimum anchored VWAP dari level agar konfluensi dianggap ada |
| `MIN_CLOSE_POSITION_IN_RANGE` | 0.35 | Posisi minimum close terhadap range candle retest, 0 berarti di dasar dan 1 di puncak |
| `MAX_BARS_BREAKOUT_TO_RETEST` | 12 | Batas umur setup, dihitung dari candle breakout |
| `MAX_RETEST_TOUCHES` | 1 | Berapa kali harga boleh mengunjungi zona sebelum setup dianggap lelah |
| `INVALIDATION_ATR_MULT` | 1.0 | Jarak di bawah level yang membatalkan setup dan memicu exit `SETUP_INVALIDATED` |
| `MAX_EXTENSION_ATR_MULT` | 1.5 | Anti-kejar: jarak maksimum close di atas level agar entry masih diizinkan |
| `SETUP_INVALIDATION_EXIT` | True | Aktifkan exit saat candle tertutup menembus batas invalidasi |
| `CONFIRM_LOOKBACK_BARS` | 48 | Panjang jendela candle untuk satu keputusan entry |
| `PUMP_MIN_24H_CHANGE_PCT` | 10.0 | Gerbang pump syarat 1: kenaikan harga 24 jam minimum (persen) agar sebuah simbol boleh menjadi kandidat. Koin yang turun 24 jam gugur di sini |
| `PUMP_VOLUME_SURGE_MULT` | 1.5 | Gerbang pump syarat 2: volume kuotasi 24 jam berjalan minimal sekian kali rata-rata volume kuotasi 7 hari penuh sebelumnya (candle `1d` yang sudah tertutup) |

Nilai default di atas adalah **titik awal yang belum tervalidasi**, bukan
rekomendasi. Jalankan backtest sendiri sebelum memakainya.

`strategy.required_lookback_bars(config)` menghitung jumlah candle minimum
(`SWING_LOOKBACK_BARS + 2 x SWING_PIVOT_WING_BARS + MAX_BARS_BREAKOUT_TO_RETEST`,
dan minimal `ATR_PERIOD + 1`). Angka itulah satu-satunya sumber kebenaran:
dipakai validasi setelan, bot live, kedua backtest, dashboard, dan watchlist,
menggantikan angka hard-code yang dulu tersebar.

## PERINGATAN (harap dibaca)

- **Ini reaktif, bukan prediktif.** Bot masuk SETELAH harga sudah naik
  signifikan. Ada risiko nyata membeli di puncak / menjelang koreksi.
- **Altcoin kecil rawan manipulasi** (pump-and-dump yang memang disengaja,
  wash trading untuk mengelabui filter volume, dsb). Filter volume minimum
  membantu tapi tidak menjamin keamanan penuh.
- **Spread & slippage** di altcoin bisa besar -- `MAX_SPREAD_PCT` default
  0.5%, tetap perhatikan baik-baik.
- **Stop Loss, Breakeven, dan Trailing SEMUANYA dipantau & dieksekusi oleh
  bot itu sendiri, BUKAN order stop di level bursa Binance.** Jadi **bot
  harus berjalan 24/7** di VPS -- kalau bot mati (crash, VPS restart,
  koneksi putus, dsb), level-level ini TIDAK akan tereksekusi otomatis oleh
  Binance dan posisi terbuka bisa terus merugi tanpa batas selama bot
  offline.
- Hanya pakai dana yang benar-benar siap Anda rugikan sepenuhnya. Ini bukan
  nasihat keuangan.

## Cara menjalankan

```bash
pip install -r requirements.txt

# Audit logika inti tanpa koneksi dan tanpa order
python pump_scanner_bot.py --selftest

# Jalankan dashboard sekaligus auto-start bot
python run.py
# Buka http://localhost:8080
```

`python run.py` menjalankan dashboard sebagai proses utama. Bot menjadi child
process yang dimiliki dashboard. Menekan Stop atau Restart pada tab **Kontrol**
tidak mematikan dashboard. Jika bot crash, dashboard tetap hidup dan menampilkan
status `CRASHED`, kode keluar, serta tombol Start.

`Ctrl+C` pada terminal `run.py` menghentikan child bot secara graceful lalu
menutup dashboard. Stop normal memakai file perintah khusus per mode. Jika bot
tidak merespons sampai timeout, fallback-nya adalah process-group `SIGTERM` di
Linux atau `CTRL_BREAK_EVENT` di Windows, kemudian force-kill sebagai langkah
terakhir.

Pilihan standalone tetap didukung:

```bash
python dashboard.py          # dashboard hidup, bot awalnya STOPPED
python pump_scanner_bot.py   # bot saja, mengambil lock mode yang sama
```

Hanya satu bot boleh berjalan untuk setiap mode. Lock O_EXCL dan identitas
proses mencegah Start ganda dari dashboard atau terminal lain.

### Konfigurasi awal melalui tab Kontrol

1. Mulai di PAPER. Mode default tetap PAPER.
2. Buka **Kontrol > Parameter Strategi dan Risiko**. Settings PAPER dan LIVE
   disimpan terpisah. Simpan settings yang valid, tinjau diff, lalu konfirmasi.
3. Untuk kredensial, gunakan panel **Kredensial Binance**. Secret bersifat
   write-only. Kolom kosong berarti tidak diubah.
4. Jalankan **Uji Koneksi Read-only**. Uji ini memakai endpoint account signed
   dan tidak mengirim order.
5. Sebelum menuju LIVE, isi `MAX_POSITION_USDT` LIVE dengan angka lebih besar
   dari nol. Periksa seluruh checklist, ketik `LIVE`, lalu konfirmasi lagi.

Settings tersimpan baru berlaku setelah restart. Jika bot sedang berjalan,
dashboard otomatis melakukan restart setelah commit. Jika ada posisi terbuka,
Anda wajib memilih **Jual dulu** atau **Pertahankan posisi**. Mempertahankan
posisi berarti SL/TP yang dikelola bot tidak aktif selama bot berhenti.

Kredensial disimpan atomik ke `.env`. Di Linux izinnya dipaksa dan diverifikasi
sebagai `0600`. Di Windows dashboard memakai `icacls` dan menampilkan peringatan
jujur bila ACL tidak dapat diverifikasi. Nilai `.env` dashboard mengalahkan
nilai environment proses yang lama.

### Port, bind, dan akses jarak jauh

- Port: Linux `DASHBOARD_PORT=9000 python run.py`.
- Port PowerShell: `$env:DASHBOARD_PORT='9000'; python run.py`.
- Bind default selalu `127.0.0.1` dan dashboard tidak memiliki login.
- Jika `DASHBOARD_HOST` sengaja diubah ke alamat non-loopback, semua endpoint
  tulis dinonaktifkan. UI hanya read-only.
- Untuk VPS, biarkan bind loopback dan gunakan tunnel SSH:
  `ssh -L 8080:localhost:8080 user@ip_vps`.
- Jangan mengekspos dashboard langsung ke internet.

### Menjalankan 24/7

Gunakan service manager seperti `systemd`, Task Scheduler, `tmux`, atau
`screen`. Contoh Linux sederhana:

```bash
tmux new -s pumpbot
python run.py
# Ctrl+B lalu D untuk detach
```

State dan log berada di `pump_bot_state_<mode>.json` dan
`pump_bot_<mode>.log`. Lifecycle dan lock juga dipisah per mode. Posisi dan
level BE/trailing tetap tersimpan setelah restart atau crash.

## Dashboard pemantauan dan kontrol (web)

Dashboard menampilkan posisi dan PnL, equity, saldo, riwayat trade, kurva PnL,
log, watchlist, dan backtest. Tab **Kontrol** menyediakan:

- Start, Stop, dan Restart dengan status `STARTING`, `RUNNING`, `STOPPING`,
  `STOPPED`, atau `CRASHED`.
- PID aktif, uptime, PID terakhir, dan kode keluar.
- Settings lengkap per mode, editor watchlist, default per field/grup, diff,
  validasi server, peringatan risiko LIVE, dan riwayat audit.
- Perpindahan PAPER/LIVE dengan checklist dan konfirmasi ketik `LIVE`.
- Penyimpanan serta pengujian kredensial tanpa menampilkan secret.
- Reset akun PAPER yang mengarsipkan akun dan state lama lebih dulu.

Semua request tulis dilindungi token acak per proses, pemeriksaan Host,
Origin/Referer, alamat loopback, rate limit, dan cooldown tindakan berbahaya.
Tidak ada CORS yang dibuka.

### Jual Sekarang (tutup posisi manual dari dashboard)

Kalau ada posisi terbuka, panel "Posisi Saat Ini" menampilkan tombol merah
**"Jual Sekarang (Manual)"**. Ini untuk situasi Anda ingin keluar dari posisi
kapan saja tanpa harus membuka app/web Binance, di luar logika otomatis
Stop Loss/Take Profit/Breakeven/Trailing yang sudah berjalan.

Cara kerja:

1. Klik tombol -> muncul modal konfirmasi (menyebutkan simbol yang akan
   dijual) supaya tidak kepencet tidak sengaja.
2. Setelah dikonfirmasi, dashboard menulis perintah ke file terpisah
   (`pump_bot_control_<mode>.json`, sengaja dipisah dari file state
   supaya tidak tabrakan tulis dengan proses bot yang berjalan).
3. Proses bot (`pump_scanner_bot.py`) membaca file ini di **awal setiap
   iterasi loop utamanya**, jadi perintah diproses dalam maksimal
   `LOOP_INTERVAL_SECONDS` (default 15 detik) -- bukan seketika, karena bot
   dan dashboard adalah dua proses terpisah yang cuma bisa saling bicara
   lewat file.
4. Penjualan ini SELALU order SELL MARKET sungguhan lewat jalur
   `close_position()` yang sama dengan SL/TP. Bedanya cuma tujuannya: di
   `MODE="PAPER"` order disimulasikan lokal (saldo virtual), di
   `MODE="LIVE"` dikirim ke Binance produksi (uang asli).
5. Dashboard menampilkan status "terkirim, menunggu diproses" lalu
   otomatis polling sampai posisi hilang dari state (maksimal ~2 menit),
   dan alasan trade di riwayat akan tertulis `MANUAL_CLOSE_DASHBOARD`.
6. Perintah kadaluarsa (lebih dari 2 menit belum diproses, misalnya bot
   sempat mati) otomatis diabaikan oleh bot demi keamanan -- tidak akan
   ada penjualan "nyasar" dari klik lama yang terlupakan.
7. Kalau proses bot tampaknya tidak berjalan (dideteksi dari kapan
   terakhir file state mode aktif diperbarui), dashboard menampilkan
   peringatan bahwa tombol ini tidak akan diproses sampai bot dijalankan
   lagi -- supaya Anda tahu harus mengecek `run.py`/proses bot dulu.

### Watchlist Pantau (`WATCHLIST`, `WATCHLIST_ENABLED`)

Panel di tab Ikhtisar berisi daftar koin pilihan beserta harga, perubahan 24
jam, volume, posisi harga dalam rentang harian, dan status koin itu terhadap
gerbang awal scanner.

**Panel ini TIDAK mengubah perilaku bot sama sekali.** Bot tetap memindai
SELURUH pair USDT persis seperti sebelumnya. Tidak ada satu baris pun di
`market_scanner.py`, `pump_scanner_bot.py`, `portfolio_backtest.py`, atau
`backtest.py` yang membaca daftar ini (ada pengujian otomatis yang menjaga
janji tersebut, lihat `test_watchlist.py`). Menambah atau menghapus simbol di
`WATCHLIST` tidak mengubah koin apa yang dibeli bot, tidak mengubah ranking
kandidat, dan tidak mengubah hasil backtest.

Arti kolom Status:

| Status | Arti |
|---|---|
| **LIKUID** | Volume 24 jam >= `MIN_QUOTE_VOLUME_USDT_24H`, jadi koin ini bisa masuk semesta kandidat |
| **TIPIS** | Volume 24 jam di bawah ambang, bot mengabaikannya |
| **TIDAK ADA DATA** | Simbol tidak ditemukan di ticker Binance (salah ketik, atau pair sudah delisting) |

**Status LIKUID bukan berarti bot pasti membeli.** Panel ini hanya memeriksa
gerbang volume 24 jam. Bot masih menjalankan konfirmasi candle 5 menit, cek spread,
cooldown, dan hanya mengambil SATU kandidat terbaik dari seluruh pasar.
Konfirmasi candle sengaja tidak dihitung di panel ini, karena itu berarti
mengunduh candle per simbol setiap refresh dan memakan jatah rate-limit IP
yang sama dengan yang dipakai bot untuk mengirim order.

Beban jaringannya kecil: panel memakai SATU panggilan `ticker/24hr` untuk
seluruh pasar (bukan satu per simbol) dengan cache 20 detik, jadi paling
banyak 3 request per menit berapa pun panjang daftarnya.

Matikan panel dengan `WATCHLIST_ENABLED: False`. Kalau Binance tidak
terjangkau, panel tetap tampil memakai data terakhir yang berhasil diambil,
bukan mematikan dashboard.

#### Dari mana daftar bawaannya berasal

Daftar 26 simbol bawaan disusun 2026-09-24 dari data pasar Binance Spot yang
sesungguhnya, bukan dari daftar "koin populer":

1. 3.710 simbol `exchangeInfo` + `ticker/24hr` + `bookTicker` ditarik dari
   endpoint data publik resmi Binance.
2. 487 pair USDT lolos aturan struktural bot (status TRADING, spot diizinkan,
   bukan stablecoin, bukan leveraged token).
3. 182 pair lolos `MIN_QUOTE_VOLUME_USDT_24H`, lalu ditarik candle 1 jam
   selama 120 hari untuk mengukur frekuensi pump.
4. 110 pair shortlist ditarik candle 5 menit selama 45 hari (12.960 candle per
   simbol, sama dengan `CONFIRM_INTERVAL` bot).
5. Pada tiap candle itu dijalankan `confirm_entry()` ASLI dari
   `market_scanner.py` dan `atr_percent()` asli dari `strategy.py`. Jadi angka
   "berapa kali koin ini memicu sinyal" adalah hasil menjalankan logika
   keputusan bot itu sendiri, bukan perkiraan. Total 1.391 sinyal terukur.

Skor 0-100 menimbang empat hal: frekuensi sinyal nyata (35), likuiditas dan
konsistensinya (25), spread terhadap `MAX_SPREAD_PCT` (20), dan kecocokan ATR
5 menit dengan rentang `ATR_SL_MIN_PCT`..`ATR_SL_MAX_PCT` (20).

Tier dibagi berdasarkan **uptime likuiditas**, yaitu berapa persen waktu
volume 24 jam koin itu berada di atas ambang bot:

| Tier | Uptime | Sifat |
|---|---|---|
| **INTI** | >= 90% | Sinyal paling mungkin benar-benar bisa dieksekusi |
| **AKTIF** | 60-90% | Aktif berkala, ada periode diabaikan bot |
| **SPEKULATIF** | < 60% | Sinyal paling sering, tapi likuiditas putus-putus |

Koin SPEKULATIF punya volume median DI BAWAH `MIN_QUOTE_VOLUME_USDT_24H`,
artinya di hari biasa bot memang tidak menyentuhnya; mereka hanya lolos saat
sedang ramai. Risiko slippage pada order MARKET di sana nyata.

Yang sengaja dibuang: 9 saham tokenisasi Binance (bStocks seperti `MSTRB`,
`CRCLB`, `SOXLB`). Terdeteksi dari data, bukan dari nama: porsi volume akhir
pekan mereka hanya 4-14%, sementara median crypto 24/7 adalah 25,6%. Harganya
ditambatkan ke bursa saham AS yang tutup akhir pekan, sehingga asumsi pasar
24/7 milik bot ini tidak berlaku.

#### Penyegaran daftar otomatis (`WATCHLIST_AUTO_REFRESH`)

Aktif secara default. Dashboard menyusun ULANG daftar simbol secara berkala
(default tiap 6 jam) dari data Binance terbaru memakai metodologi yang sama.
Hasilnya ditulis ke `watchlist_auto_<mode>.json` dan **tidak pernah menimpa
`config.py`** -- daftar manual tetap utuh sebagai cadangan kalau penyegaran
gagal, belum sempat berjalan, atau dimatikan.

Panel menampilkan sumber daftar yang sedang dipakai, kapan terakhir
disegarkan, dan jadwal berikutnya.

**Soal beban ke Binance.** Batas resmi adalah 6.000 request weight per menit
dan dihitung **per IP, bukan per API key** (sumber: developers.binance.com,
General REST API Information / LIMITS, dicek 2026-09-24). Artinya dashboard
dan bot berbagi jatah yang sama persis. Karena itu ruang lingkupnya
dikecilkan dari metodologi penuh:

| | Metodologi penuh | Penyegaran berkala |
|---|---|---|
| Simbol dinilai | 110 | 60 |
| Riwayat candle 5m | 45 hari | 14 hari |
| Weight per siklus | 2.944 | 684 |
| Beban (disebar 15 menit) | 3,3% anggaran | **0,76% anggaran** |

Diukur langsung saat pengujian: 14 simbol menghabiskan 224 weight dan
menyisakan 96,6% kuota menit itu (dibaca dari header `x-mbx-used-weight-1m`
milik Binance).

**Tiga rem keamanan yang TIDAK bisa dimatikan lewat config**, karena
menyangkut keselamatan posisi Anda:

1. **Penyegaran dilewati selama bot memegang posisi terbuka.** Itu saat
   paling kritis, ketika bot harus bisa mengirim order jual kapan saja.
   Kalau file state tidak terbaca, bot dianggap punya posisi (sikap aman).
2. **Berhenti sendiri kalau sisa kuota menipis**, dibaca dari header
   Binance, sebelum kena 429 bukan sesudah.
3. **Berhenti total kalau kena 429/418**, tidak mencoba ulang.

Parameter yang bisa diatur:

| Kunci | Bawaan | Arti |
|---|---|---|
| `WATCHLIST_AUTO_REFRESH` | `True` | `False` = daftar statis dari config saja |
| `WATCHLIST_AUTO_INTERVAL_HOURS` | `6` | jarak antar penyegaran |
| `WATCHLIST_AUTO_MAX_SYMBOLS` | `60` | kandidat teratas yang dinilai |
| `WATCHLIST_AUTO_DAYS` | `14` | panjang riwayat candle |
| `WATCHLIST_AUTO_KEEP` | `26` | jumlah simbol di daftar akhir |
| `WATCHLIST_AUTO_MAX_WEIGHT` | `900` | plafon keras weight per siklus |
| `WATCHLIST_AUTO_PACE_SECONDS` | `2.0` | jeda antar panggilan |
| `WATCHLIST_AUTO_MIN_HEADROOM` | `0.5` | berhenti kalau sisa kuota < 50% |

Sebagai bagian dari fitur ini, penanganan rate limit di `binance_client.py`
juga diperbaiki: HTTP 429 dan 418 kini punya kelas error sendiri
(`BinanceRateLimitError`), menghormati header `Retry-After`, dan HTTP 418
tidak pernah dicoba ulang. Sebelumnya keduanya diperlakukan seperti error
biasa dan dicoba lagi setelah 2-10 detik, yang justru perilaku pemicu ban IP
bertingkat (Binance menyebutnya "scale in duration for repeat offenders,
from 2 minutes to 3 days"). Perbaikan ini menguntungkan bot juga, bukan
hanya watchlist.

**Batas kejujuran data ini:** frekuensi sinyal TIDAK sama dengan
profitabilitas. Yang diukur adalah seberapa sering koin memicu kondisi masuk
bot, bukan seberapa sering trade-nya berakhir untung. Pasar juga berputar;
koin yang aktif hari ini bisa sepi dalam dua bulan. Tinjau ulang daftarnya
secara berkala.

Audit fitur ini tanpa jaringan:

```bash
python test_watchlist.py
```

## Dust sweep ke BNB (`USE_DUST_SWEEP`)

Aktif secara default (`USE_DUST_SWEEP: True`). Setelah SEBUAH posisi ditutup
(oleh Stop Loss, Take Profit, Breakeven, Trailing, Setup Invalidated, ATAU tombol
"Jual Sekarang" manual), bot mengecek apakah masih ada **sisa saldo kecil**
dari koin yang baru saja dijual -- biasanya muncul karena pembulatan quantity
ke `LOT_SIZE` bursa, atau sisa yang terlalu kecil untuk dijual lewat order
biasa (di bawah `MIN_NOTIONAL`). Kalau Binance mengakuinya sebagai aset
"dust" yang layak dikonversi, bot otomatis mengonversinya ke BNB lewat
endpoint resmi `POST /sapi/v1/asset/dust` (referensi:
[developers.binance.com/docs/wallet/asset/dust-transfer](https://developers.binance.com/docs/wallet/asset/dust-transfer),
dicek 2026-09-23).

**Batasan & proteksi keras (bukan sekadar default, tapi memang tidak bisa
dimatikan lewat config):**

- **Hanya menyentuh base asset dari koin yang BARU SAJA ditutup posisinya**
  -- tidak pernah "menyapu semua saldo kecil di akun" secara serampangan.
  Ambang "dust" versi Binance sendiri adalah saldo bernilai di bawah
  ~0,001 BTC (bisa setara 100+ USD tergantung harga BTC saat itu), yang
  jauh lebih besar dari ukuran posisi trading bot ini -- kalau fitur ini
  boleh menyapu aset apa saja, ada risiko nyata **modal USDT (atau BNB)
  Anda ikut ter-convert**. Karena itu quote asset (USDT) dan BNB itu
  sendiri dikecualikan secara eksplisit di kode, apa pun isi config.
- Di mode `PAPER`, fitur ini **otomatis dilewati** (hanya dicatat di log).
  Alasannya: konversi dust memakai `/sapi/v1/asset/dust` yang **bertanda
  tangan**, sedangkan PAPER dilarang keras mengirim request bertanda tangan
  apa pun (ada guard yang melempar `SignedEndpointBlockedError`). Konversi
  dust bukan bagian dari simulasi eksekusi. Di mode `LIVE` fitur ini tetap
  berjalan penuh.
- Binance sendiri **membatasi frekuensi** endpoint dust-nya per akun
  (dilaporkan komunitas sekitar tiap 6-24 jam sekali, bukan dibatasi oleh
  kode ini). Kalau bot mencoba dust sweep tapi kena batas ini, itu dianggap
  hal wajar -- dicatat di log lalu dicoba lagi di kesempatan berikutnya,
  bukan dianggap error yang menghentikan bot.
- Endpoint ini dulu bisa mengonversi ke BTC/ETH/USDT juga, tapi fitur ini
  SELALU mengonversi ke BNB saja (sesuai permintaan awal fitur ini), tidak
  ada opsi target asset lain.
- Tidak butuh permission API key tambahan -- permission "Enable Spot &
  Margin Trading" yang sudah wajib untuk fitur jual (SL/TP/manual close)
  juga sudah cukup untuk dust sweep ini.
- Bisa dimatikan sepenuhnya lewat `USE_DUST_SWEEP: False` di `config.py`
  kalau tidak diinginkan.

### Backtest parameter (dari dalam dashboard)

Di bagian bawah dashboard ada panel **"Backtest Parameter"**. Isi satu simbol
(mis. `SOLUSDT`), rentang hari (bebas, tidak dibatasi -- lihat catatan di
bawah), dan parameter yang mau diuji (Stop Loss, TP%, Breakeven, Trailing,
jarak invalidasi, batas anti-kejar), lalu klik **Jalankan Backtest**. Prosesnya:

1. Dashboard mengambil candle 5 menit historis simbol tsb langsung dari
   Binance (butuh koneksi internet keluar dari server dashboard).
2. Untuk tiap candle, dihitung ulang kenaikan dan volume 24 jam bergulir
   sebagai gerbang likuiditas dan **gerbang pump** (`PUMP_MIN_24H_CHANGE_PCT`
   dan `PUMP_VOLUME_SURGE_MULT`, rata-rata 7 hari diambil dari candle `1d`
   yang sudah tertutup pada bar tersebut), lalu jendela candle diuji dengan `detect_pullback_retest()`
   **yang sama persis** dengan `market_scanner.py`.
3. Kalau lolos, posisi "dibuka", lalu dievaluasi tiap candil berikutnya
   dengan logika exit **yang sama persis** dengan `manage_exit()` di
   `pump_scanner_bot.py` (Stop Loss / Take Profit / Breakeven / Trailing /
   pembatalan setup).

Selama proses berjalan, dashboard menampilkan **panel loading**: spinner,
progress bar (dengan efek shimmer, bukan cuma diam), tahap yang sedang
berjalan ("mengambil data historis..." / "menjalankan simulasi..."), waktu
yang sudah berjalan, dan **estimasi sisa waktu** yang dihitung dari
kecepatan progres nyata sejauh itu (bukan angka tebakan tetap) -- jadi
estimasinya makin akurat semakin lama backtest berjalan. Backtest biasanya
memakan waktu belasan detik sampai beberapa menit tergantung rentang hari
yang diminta (semakin panjang rentang, semakin banyak candle yang perlu
diunduh dengan paging dari Binance).

**Keterbatasan penting (WAJIB dibaca sebelum percaya hasilnya):**

- **Tidak mensimulasikan persaingan antar-simbol.** Bot asli memindai
  SELURUH pasar dan cuma mengambil satu kandidat terbaik. Backtest ini
  menjawab "kalau bot kebetulan memantau simbol ini dan lolos filter,
  bagaimana hasilnya" -- bukan "seberapa sering bot akan benar-benar
  memilih simbol ini". Jangan menganggap hasil ini sebagai proyeksi
  return bot yang sesungguhnya.
- **Granularitas candle 5 menit**, bukan tick real-time seperti bot asli
  (loop 15 detik). Kalau level Stop Loss, TP, dan stop (BE/Trailing)
  sama-sama tersentuh dalam satu candle yang sama, urutan sebenarnya tidak
  diketahui. Backtest memakai urutan tetap dan SENGAJA KONSERVATIF:
  STOP_LOSS diperiksa paling dulu, baru TAKE_PROFIT, baru BE, baru
  Trailing, baru Max Hold, baru SETUP_INVALIDATED -- urutan yang sama dengan
  bot live, supaya hasil backtest tidak melebih-lebihkan profit saat
  kondisinya ambigu.
- **`SETUP_INVALIDATION_EXIT` SUDAH disimulasikan**, karena exit itu hanya
  butuh candle simbol yang sedang dipegang. Ini berbeda dari exit
  `MOMENTUM_FADE` lama yang butuh peringkat seluruh pasar per candle dan
  karena itu tidak pernah bisa diuji di backtest satu simbol.
- Filter volume, spread maksimum, dan ukuran posisi TIDAK bisa diubah dari
  form ini (dipertahankan dari `config.py`).
- Return total dihitung **compounding** (reinvest 100% tiap trade, sesuai
  `RISK_PERCENT`), TANPA memperhitungkan fee trading atau slippage.
- **Rentang hari tidak dibatasi.** Isi berapa saja (mis. 365, 1000 hari).
  Batas alaminya hanya sejak kapan simbol itu mulai listing/tersedia
  datanya di Binance -- kalau rentang yang diminta lebih panjang dari
  histori yang ada, backtest otomatis memakai data yang tersedia (bukan
  error), lalu menandainya di kolom "Rentang data" pada hasil. Rentang
  yang sangat panjang wajar memakan waktu proses lebih lama karena data
  diambil dengan paging (maksimal 1000 candle per panggilan ke Binance).

Backtest berjalan di background thread dan **tidak pernah menyentuh state
atau posisi bot yang sedang berjalan** -- murni membaca data historis publik
Binance dan mensimulasikan di memori.

Audit logika backtest tanpa jaringan (data sintetis):
```bash
python backtest.py
```

## Menjalankan tes (PAPER)

Unit test mesin simulasi, penyimpanan, dan pengaman mode/guard ada di folder
`tests/` dan **tidak menghubungi jaringan** (data pasar di-inject sebagai data
palsu, jadi deterministik).

```bash
pip install -r requirements.txt   # termasuk pytest
pytest -q
```

Selftest logika strategi lama (juga tanpa jaringan) tetap tersedia:

```bash
python pump_scanner_bot.py --selftest
```

Skenario yang tercakup di `tests/`:

- Penolakan order akibat `LOT_SIZE` dan `MIN_NOTIONAL`.
- Market order berjalan melewati beberapa level order book (harga rata-rata +
  slippage).
- Partial fill saat kedalaman kurang (sisa `EXPIRED`).
- Limit order tidak terisi lalu timeout (dana terkunci dikembalikan).
- Gap harga melewati stop-loss (isi pada harga pasca-gap, bukan `stopPrice`).
- Fee terpotong dari aset yang benar (BUY: base, SELL: quote) + diskon BNB;
  qty jual tidak pernah melebihi saldo.
- Restart di tengah posisi terbuka (state termuat kembali).
- File state korup (dibuat cadangan, tidak ditimpa diam-diam) + migrasi skema.
- Guard menolak endpoint bertanda tangan di PAPER (walau API key terisi).
- `MODE` tidak valid membuat bot berhenti (tidak jatuh ke LIVE) dan idempotensi
  `clientOrderId`.
- Schema 82 parameter lengkap, merge override per mode, validasi relasi, dan
  deteksi pelonggaran risiko LIVE.
- Lock O_EXCL, lock stale, lifecycle, Start/Restart/Stop PAPER di Linux, dan
  pemisahan perintah Stop dari `CLOSE_POSITION`.
- Host, Origin, token admin, bind non-loopback read-only, rate limit, dan
  respons kredensial tanpa secret.
- Perpindahan mode dua arah saat posisi terbuka, reset PAPER, arsip state,
  CRLF/LF `.env`, mode 0600 POSIX, serta test Windows dengan `skipif`.

## Contoh konfigurasi PAPER

Default sumber berada di `config.py`, sedangkan perubahan pengguna sebaiknya
dilakukan melalui tab Kontrol:

```python
"MODE": "PAPER",
"PAPER_INITIAL_BALANCES": {"USDT": 10000.0},
"TAKER_FEE_PCT": 0.1,
"MAKER_FEE_PCT": 0.1,
"USE_BNB_FEE_DISCOUNT": True,     # 0,1% -> 0,075%
"USE_WEBSOCKET": True,            # WS primer + REST fallback (hybrid)
"WS_BASE_URL": "wss://stream.binance.com:9443",
"MAX_MARKET_DATA_AGE_SECONDS": 10.0,
"PAPER_DEPTH_LIMIT": 100,
```

`.env` boleh kosong di PAPER.

## Checklist migrasi

### Dari TESTNET ke PAPER

1. Tarik kode baru, lalu `pip install -r requirements.txt`
   (menambah `websocket-client` dan `pytest`).
2. Jalankan `python dashboard.py`, buka tab Kontrol, dan pastikan mode PAPER.
3. Hapus atau arsipkan file lama `*_testnet.*` karena tidak lagi dipakai.
4. API key tidak diperlukan untuk PAPER. `.env` boleh kosong.
5. Jalankan `pytest -q` lalu `python run.py`. Periksa mode PAPER pada header.
6. Verifikasi saldo virtual muncul di dashboard, default 10.000 USDT.

### Sebelum pindah ke LIVE

1. Simpan API key/secret produksi melalui panel Kredensial. Aktifkan izin Spot
   Trading saja dan jangan aktifkan Withdrawals.
2. Tekan **Uji Koneksi Read-only** dan pastikan berhasil.
3. Pilih settings LIVE. Periksa `RISK_PERCENT`, isi `MAX_POSITION_USDT` dengan
   nilai positif, dan periksa Stop Loss, equity stop, daily stop, serta
   `CLOSE_ALL_AT_LIMIT`.
4. Pastikan bot STOPPED dan tidak ada posisi di mode aktif.
5. Buka checklist mode, baca ringkasan risiko, ketik `LIVE`, lalu konfirmasi.
6. Mulai dari modal kecil dan pantau rotasi trade pertama.
7. Hasil PAPER bukan jaminan hasil LIVE. Slippage, antrean order book, dan
   likuiditas nyata dapat berbeda.

## Checklist manual lintas OS

### Linux

- [ ] Jalankan `python run.py`, pastikan dashboard hidup dan bot auto-start.
- [ ] Stop bot dari UI, pastikan dashboard tetap hidup dan status menjadi
  `STOPPED` tanpa fallback signal.
- [ ] Start lalu Restart, pastikan PID berubah dan tidak ada dua lock mode sama.
- [ ] Dengan posisi PAPER, uji kedua pilihan Stop: `SELL_FIRST` dan `KEEP_OPEN`.
- [ ] Ubah settings PAPER, tinjau diff, commit, dan pastikan restart otomatis.
- [ ] Coba bind non-loopback pada jaringan uji. Pastikan UI menunjukkan
  read-only dan semua POST ditolak.
- [ ] Simpan kredensial dummy pada salinan repo, lalu periksa `.env` mode 0600.
- [ ] Reset PAPER dan pastikan dua arsip `.bak-<timestamp>` terbentuk.
- [ ] Hentikan terminal dengan Ctrl+C dan pastikan tidak ada child bot tertinggal.

### Windows 11

- [ ] Jalankan dari PowerShell dengan `python run.py`. Pastikan console UTF-8
  tidak membuat proses gagal.
- [ ] Ulangi Start, Stop, Restart, status, PID, dan dashboard tetap hidup.
- [ ] Paksa bot tidak merespons file Stop pada salinan uji. Pastikan fallback
  `CTRL_BREAK_EVENT` bekerja sebelum force-kill.
- [ ] Periksa Task Manager setelah Stop dan setelah menutup dashboard. Pastikan
  tidak ada child Python tertinggal.
- [ ] Simpan `.env` dummy dan pastikan panel melaporkan ACL `icacls` terverifikasi,
  atau menampilkan warning jujur jika verifikasi gagal.
- [ ] Uji CRLF `.env`, path yang mengandung spasi, reset PAPER, settings per mode,
  Host/Origin/token, dan bind non-loopback read-only.
- [ ] Jalankan `pytest -q`. Test khusus Linux harus `skipped`, bukan gagal.

Jangan menjalankan checklist dengan mode LIVE atau API key produksi. Gunakan
PAPER, salinan repo, dan kredensial dummy kecuali saat uji account read-only
yang memang Anda setujui.

## Dokumentasi resmi yang menjadi acuan

- Flask: konfigurasi `TRUSTED_HOSTS` dan keamanan web
  <https://flask.palletsprojects.com/en/stable/config/>
  <https://flask.palletsprojects.com/en/stable/web-security/>
- Werkzeug: validasi host dan `ProxyFix`
  <https://werkzeug.palletsprojects.com/en/stable/wsgi/>
  <https://werkzeug.palletsprojects.com/en/stable/middleware/proxy_fix/>
- Python: subprocess dan sinyal Windows
  <https://docs.python.org/3/library/subprocess.html>
- python-dotenv: format `.env` dan precedence `override=True`
  <https://bbc2.github.io/python-dotenv/>
  <https://bbc2.github.io/python-dotenv/reference/>
- Binance Spot API: endpoint account signed read-only dan kode error
  <https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints>
  <https://developers.binance.com/docs/binance-spot-api-docs/errors>
