# Bot Pump Scanner — Binance Spot

Bot ini **memindai SEMUA pair USDT** di Binance Spot untuk mencari koin yang
sedang naik tajam (momentum chasing), lalu masuk dengan **satu entry** dan
keluar lewat Stop Loss / Take Profit / Breakeven / Trailing / batas waktu
hold / momentum pudar. **Tidak ada averaging-down/martingale.**

> Catatan: repo ini dulu juga berisi bot grid martingale (`bot.py`) untuk satu
> pair tetap (BTCUSDT). Fitur itu sudah dihapus. Yang tersisa sekarang hanya
> bot pump scanner.


## Mode TESTNET dan LIVE

Bot ini punya dua mode, diatur lewat `"MODE"` di `config.py`:

| | `MODE="TESTNET"` (default) | `MODE="LIVE"` |
|---|---|---|
| Endpoint REST | `https://testnet.binance.vision` | `https://api.binance.com` |
| Order | Sungguhan, dana virtual | Sungguhan, uang asli |
| API key | Dibuat di testnet.binance.vision | Binance produksi |
| Dust sweep (`/sapi/*`) | Dilewati otomatis (tidak tersedia di testnet) | Aktif |
| Jalur kode | Sama persis | Sama persis |

Poin pentingnya: **tidak ada lagi jalur simulasi lokal**. Mode DRY_RUN yang
dulu ada sudah dihapus, karena jalur simulasi itu melewatkan hal-hal yang
justru paling sering bikin masalah di live, seperti pembulatan `LOT_SIZE`,
`MIN_NOTIONAL`, slippage market order, dan error autentikasi. Di TESTNET
semua itu benar-benar diuji oleh server Binance.

Cara pindah mode:

1. Buka `config.py`, ubah `"MODE"` jadi `"TESTNET"` atau `"LIVE"`.
2. Ganti isi `.env` dengan pasangan key yang sesuai. Kedua mode membaca
   variabel yang sama (`BINANCE_API_KEY` / `BINANCE_API_SECRET`), dan key
   produksi TIDAK berlaku di testnet begitu pula sebaliknya (akan muncul
   error `-2015 Invalid API-key`).
3. Jalankan ulang bot.

Hal yang perlu diketahui soal Spot Test Network (sumber: Binance Developer
Docs, Testnet General Info, dicek 2026-09-23):

- Saldo virtual diberikan otomatis saat mendaftar, tidak bisa
  ditarik/disetor.
- Testnet **di-reset sekitar sebulan sekali**, semua order dan saldo hilang.
  API key tetap dipertahankan saat reset.
- Likuiditas dan pergerakan harga di testnet **tidak sama** dengan pasar
  asli, jadi profit/loss di sana bukan prediksi hasil live. Yang divalidasi
  di testnet adalah kebenaran mekanis bot (order terkirim, terisi, exit
  jalan), bukan profitabilitas strategi.
- Nilai kalau salah ketik: `MODE` yang tidak dikenali otomatis dianggap
  `TESTNET`, bukan `LIVE`. Ini disengaja supaya typo tidak berujung order
  pakai uang asli.



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

### Mengubah parameter: ada 3 tempat

| Tempat | Cakupan | Sifat |
|---|---|---|
| **Dashboard** (form Backtest) | Semua parameter exit + semua parameter ATR | Sementara, sekali jalan |
| **CLI** `backtest.py` | Sama, lewat flag | Sementara, sekali jalan |
| **`config.py`** | Semua | Permanen, dipakai bot live |

Dashboard dan CLI **tidak pernah menulis ke `config.py`**. Kalau sudah ketemu
angka yang bagus, pindahkan sendiri ke `config.py`.

Nilai default form dashboard dibaca langsung dari `config.py`, jadi form selalu
mencerminkan konfigurasi bot yang sedang berlaku.

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

`MAX_HOLD_MINUTES` tetap berbasis waktu, tidak ikut ATR.

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
filter `MIN_PUMP_PCT_24H = 13.0` secara definisi hanya memilih koin yang
volatilitasnya sedang meledak, dan besarnya berbeda jauh antar koin. Stop 1,8%
bisa berarti 3x ATR di satu koin tapi hanya 0,8x ATR di koin lain.

Namun ada dua hal yang melawan:

1. **Timeframe.** ATR jauh lebih baik di timeframe harian; PF jatuh dari 1,72
   ke 0,96 di timeframe per jam karena noise. Bot ini pakai 5 menit dengan
   `MAX_HOLD_MINUTES = 45`, jadi ada di ujung yang kurang menguntungkan ATR.
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

Makin besar saldo, makin kecil persentase sesungguhnya. Sekarang defaultnya
`MAX_POSITION_USDT = 0` yang berarti **tanpa plafon**, sehingga `RISK_PERCENT`
benar-benar terpakai. Kalau Anda mengisinya dengan angka di atas 0 dan plafon
itu memotong ukuran posisi, bot akan **memperingatkan di log** lengkap dengan
persentase sesungguhnya, supaya tidak terulang diam-diam.

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
| `config.py` | Semua parameter strategi, risiko, dan kredensial (`PUMP_CONFIG`) |
| `binance_client.py` | Klien REST Binance Spot (dibuat manual, signature sudah diverifikasi cocok dengan contoh resmi Binance) |
| `market_scanner.py` | Filter & ranking koin "pump" + konfirmasi momentum + filter VWAP |
| `pump_scanner_bot.py` | Program utama: scan pasar, rotasi 1 koin (`--selftest` tersedia juga) |
| `strategy.py` | Struktur candle (`Kline`) + parser klines |
| `state.py` | Penyimpanan state posisi ke file JSON (tahan restart) |
| `run.py` | Peluncur gabungan: jalankan bot + dashboard sekaligus dalam satu perintah |
| `dashboard.py` | Dashboard web untuk memantau bot + tombol "Jual Sekarang" manual (Flask) |
| `templates/dashboard.html` | Tampilan dashboard (self-contained, tanpa CDN) |
| `backtest.py` | Mesin backtest parameter (dipanggil dashboard, bisa juga `python backtest.py` untuk selftest) |
| `requirements.txt` | Dependency Python |

## Cara kerja

1. Setiap 5 menit (`MARKET_SCAN_INTERVAL_SECONDS`), bot mengambil data 24
   jam SEMUA pair sekaligus (`GET /api/v3/ticker/24hr` tanpa parameter
   symbol -- satu request untuk seluruh pasar).
2. Filter: naik ≥ `MIN_PUMP_PCT_24H` dalam 24 jam, volume 24 jam ≥
   `MIN_QUOTE_VOLUME_USDT_24H` (hindari koin ilikuid), bukan token leverage
   (xxxUP/DOWN/BULL/BEAR), bukan pair stablecoin-ke-stablecoin.
3. Dari hasil yang lolos, diambil top-N (`TOP_N_CANDIDATES_TO_CONFIRM`)
   untuk dicek candle 5 menit terakhirnya: apakah momentum jangka pendek
   masih naik dan candle terakhir bukan reversal/topping, DAN (kalau
   `USE_VWAP_FILTER` aktif) apakah harga saat ini masih wajar dibanding
   VWAP bergulir jangka pendek -- lihat bagian "Filter VWAP" di bawah.
   Kandidat pertama yang lolos SEMUA konfirmasi ini yang dibeli.
4. Bot hanya memegang **1 koin dalam satu waktu**. Tidak ada
   averaging-down/martingale -- satu kali entry per rotasi, karena
   averaging-down pada koin yang sedang "gagal pump" (dan berpotensi dump
   tajam, terutama altcoin kecil) sangat berisiko.
5. Keluar dari posisi lewat salah satu dari: **Stop Loss** (`SL_PCT`,
   default 3% -- batas kerugian maksimum dari harga entry, dicek PALING
   AWAL sebelum kondisi lain), Take Profit, Breakeven, Trailing Stop,
   batas waktu hold maksimum (`MAX_HOLD_MINUTES` -- mencegah "nyangkut" di
   pump yang sudah mati), atau momentum pudar (koin sudah tidak masuk
   top-N gainer lagi saat scan berikutnya).

## Filter VWAP (`USE_VWAP_FILTER`, `VWAP_MAX_EXTENSION_PCT`)

Filter tambahan pada tahap konfirmasi entry (langkah 3 di atas), aktif
secara default (`USE_VWAP_FILTER: True`).

- **VWAP yang dipakai adalah VWAP BERGULIR jangka pendek**, bukan VWAP
  sesi/harian seperti di bursa saham. Binance Spot buka 24/7 tanpa jam
  reset sesi, jadi VWAP dihitung dari window candle konfirmasi yang sama
  dipakai `confirm_momentum()` (`CONFIRM_LOOKBACK_BARS`, default 20 candle
  5 menit = 100 menit terakhir). Ini sengaja BUKAN VWAP 24 jam bergulir
  karena filter pump 24 jam (`MIN_PUMP_PCT_24H`) sudah membuat semua
  kandidat otomatis jauh di atas VWAP 24 jam-nya -- window pendek jauh
  lebih diskriminatif karena mengukur leg pump yang SEDANG terjadi, bukan
  tercampur histori sebelum pump dimulai.
- Rumus: `VWAP = total(quote_volume) / total(volume)` pada window candle
  tersebut (rata-rata harga tertimbang volume transaksi riil).
- Kandidat **DITOLAK** kalau harga saat ini masih di BAWAH VWAP jangka
  pendek (indikasi tekanan beli di leg ini belum benar-benar dominan).
- Kandidat **DITOLAK** kalau harga saat ini sudah lebih dari
  `VWAP_MAX_EXTENSION_PCT` (default 5%) DI ATAS VWAP jangka pendek
  (indikasi harga sudah terlalu "kepanasan"/ekstrem, risiko besar membeli
  di puncak lokal yang segera terkoreksi).
- Kandidat **LOLOS** hanya kalau harga berada di rentang
  `[VWAP, VWAP × (1 + VWAP_MAX_EXTENSION_PCT/100)]`.
- Bisa dimatikan (`USE_VWAP_FILTER: False`) kalau ingin kembali ke perilaku
  sebelum fitur ini ada (hanya mengandalkan `confirm_momentum()`).

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

# 1) Audit logika inti tanpa koneksi apa pun (wajib dijalankan dulu)
python pump_scanner_bot.py --selftest

# 2) Set kredensial API lewat file .env (jangan taruh langsung di file config.py)
#    Salin .env.example jadi .env, lalu isi API key/secret Anda di dalamnya.
#    Linux/macOS: cp .env.example .env      |      Windows: copy .env.example .env
#    Detail lengkap dan opsi lain ada di komentar paling atas config.py

# 3) Uji dulu di TESTNET. Di config.py pastikan "MODE": "TESTNET" (ini default),
#    lalu isi .env dengan API key dari https://testnet.binance.vision.
#    Bot mengirim order SUNGGUHAN, tapi ke Spot Test Network memakai dana
#    virtual -- alur kodenya sama persis dengan mode LIVE.
python pump_scanner_bot.py

# 4) Setelah yakin, ubah "MODE": "LIVE" di config.py, ganti isi .env dengan API
#    key produksi Binance Anda, lalu jalankan lagi (mulai dari modal kecil)
python pump_scanner_bot.py
```

### Menjalankan bot + dashboard sekaligus (satu perintah)

Kalau Anda mau bot DAN dashboard jalan bersamaan tanpa buka dua terminal
terpisah, pakai `run.py`:

```bash
python run.py
# Dashboard otomatis tersedia di http://localhost:8080
# Ctrl+C sekali akan mematikan KEDUANYA sekaligus
```

Cara kerjanya:
- Log bot (BUY/SELL/scan/error) dan log akses dashboard tercampur tampil di
  satu layar terminal yang sama, dengan urutan waktu apa adanya.
- **Kebijakan "semua atau tidak sama sekali":** kalau salah satu proses
  (bot ATAU dashboard) berhenti/crash karena sebab apa pun, proses yang
  satunya otomatis ikut dihentikan, lalu `run.py` keluar. Ini supaya Anda
  tidak salah kira dashboard masih "hidup" memantau bot padahal botnya
  sudah lama mati, atau sebaliknya.
- Ganti port dashboard: `DASHBOARD_PORT=9000 python run.py`
- Kalau API key belum di-set, `run.py` menolak jalan dari awal (sama seperti
  proteksi yang sudah ada di `pump_scanner_bot.py`). Ini berlaku di kedua
  mode, karena TESTNET pun mengirim order sungguhan ke Spot Test Network.

Menjalankan keduanya terpisah (dua terminal) masih tetap bisa kalau Anda
lebih suka begitu:
```bash
python pump_scanner_bot.py     # terminal 1
python dashboard.py            # terminal 2
```

### Menjalankan 24/7 di VPS

Gunakan `systemd`, `tmux`, atau `screen` supaya proses tidak mati saat sesi
SSH terputus. Contoh sederhana dengan `tmux` (memakai `run.py` supaya bot +
dashboard jalan bersamaan dalam satu sesi):

```bash
tmux new -s pumpbot
python run.py
# Ctrl+B lalu D untuk detach; sesi tetap berjalan di background
```

State dan log tersimpan di `pump_bot_state.json` dan `pump_bot.log`, sehingga
posisi yang sedang berjalan dan level BE/trailing selamat dari restart/crash.

## Dashboard pemantauan (web)

Dashboard web untuk memantau bot: posisi terbuka + PnL live,
equity & saldo, riwayat trade, win rate, kurva PnL kumulatif, log aktivitas,
dan parameter strategi. Dashboard **hampir sepenuhnya read-only** -- satu-satunya
perintah yang bisa dikirim ke bot adalah tombol **"Jual Sekarang (Manual)"**
untuk menutup paksa posisi yang sedang terbuka (lihat bagian "Jual Sekarang"
di bawah). Di luar itu dashboard hanya membaca `pump_bot_state.json`,
`pump_bot.log`, dan (opsional) data live Binance (harga real-time + saldo
bila API key tersedia).

```bash
pip install -r requirements.txt
python dashboard.py
# buka http://localhost:8080  (atau http://IP_VPS_ANDA:8080)
```

- Port bisa diganti: `DASHBOARD_PORT=9000 python dashboard.py`
- Refresh otomatis tiap 5 detik.
- Kalau Binance tak terjangkau atau API key kosong, dashboard tetap jalan
  dengan data dari file (harga/saldo live ditampilkan sebagai "—").
- **Keamanan:** dashboard menampilkan saldo & aktivitas trading Anda, dan
  bisa mengirim 1 jenis perintah (jual paksa posisi terbuka). Kalau di-expose
  ke internet (bukan hanya localhost), lindungi dengan firewall / reverse
  proxy + autentikasi -- siapa pun yang bisa mengakses dashboard bisa menutup
  posisi Anda kapan saja. Bisa juga dijalankan di VPS yang sama dengan bot
  lalu diakses lewat SSH tunnel: `ssh -L 8080:localhost:8080 user@ip_vps`.

### Jual Sekarang (tutup posisi manual dari dashboard)

Kalau ada posisi terbuka, panel "Posisi Saat Ini" menampilkan tombol merah
**"Jual Sekarang (Manual)"**. Ini untuk situasi Anda ingin keluar dari posisi
kapan saja tanpa harus membuka app/web Binance, di luar logika otomatis
Stop Loss/Take Profit/Breakeven/Trailing yang sudah berjalan.

Cara kerja:

1. Klik tombol -> muncul modal konfirmasi (menyebutkan simbol yang akan
   dijual) supaya tidak kepencet tidak sengaja.
2. Setelah dikonfirmasi, dashboard menulis perintah ke file terpisah
   (`pump_bot_control.json`, sengaja dipisah dari `pump_bot_state.json`
   supaya tidak tabrakan tulis dengan proses bot yang berjalan).
3. Proses bot (`pump_scanner_bot.py`) membaca file ini di **awal setiap
   iterasi loop utamanya**, jadi perintah diproses dalam maksimal
   `LOOP_INTERVAL_SECONDS` (default 15 detik) -- bukan seketika, karena bot
   dan dashboard adalah dua proses terpisah yang cuma bisa saling bicara
   lewat file.
4. Penjualan ini SELALU order SELL MARKET sungguhan lewat jalur
   `close_position()` yang sama dengan SL/TP. Bedanya cuma tujuannya: di
   `MODE="TESTNET"` order dikirim ke Spot Test Network (dana virtual), di
   `MODE="LIVE"` ke Binance produksi (uang asli).
5. Dashboard menampilkan status "terkirim, menunggu diproses" lalu
   otomatis polling sampai posisi hilang dari state (maksimal ~2 menit),
   dan alasan trade di riwayat akan tertulis `MANUAL_CLOSE_DASHBOARD`.
6. Perintah kadaluarsa (lebih dari 2 menit belum diproses, misalnya bot
   sempat mati) otomatis diabaikan oleh bot demi keamanan -- tidak akan
   ada penjualan "nyasar" dari klik lama yang terlupakan.
7. Kalau proses bot tampaknya tidak berjalan (dideteksi dari kapan
   terakhir `pump_bot_state.json` diperbarui), dashboard menampilkan
   peringatan bahwa tombol ini tidak akan diproses sampai bot dijalankan
   lagi -- supaya Anda tahu harus mengecek `run.py`/proses bot dulu.

## Dust sweep ke BNB (`USE_DUST_SWEEP`)

Aktif secara default (`USE_DUST_SWEEP: True`). Setelah SEBUAH posisi ditutup
(oleh Stop Loss, Take Profit, Breakeven, Trailing, Momentum Fade, ATAU tombol
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
- Di mode `TESTNET`, fitur ini **otomatis dilewati** (hanya dicatat di log).
  Alasannya bukan pilihan desain: Binance Spot Test Network memang hanya
  menyediakan endpoint `/api/*`, sedangkan dust convert memakai
  `/sapi/v1/asset/dust` yang tidak tersedia di sana
  (sumber: developers.binance.com, Testnet General Info, dicek 2026-09-23).
  Di mode `LIVE` fitur ini tetap berjalan penuh.
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
Maks Hold, Min Pump 24h, Maks Ekstensi VWAP), lalu klik **Jalankan
Backtest**. Prosesnya:

1. Dashboard mengambil candle 5 menit historis simbol tsb langsung dari
   Binance (butuh koneksi internet keluar dari server dashboard).
2. Untuk tiap candle, dihitung ulang persentase kenaikan & volume 24 jam
   bergulir, lalu diuji dengan filter `MIN_PUMP_PCT_24H`, fungsi konfirmasi
   momentum, dan filter VWAP (`check_vwap_extension()`) **yang sama persis**
   dengan `market_scanner.py`.
3. Kalau lolos, posisi "dibuka", lalu dievaluasi tiap candil berikutnya
   dengan logika exit **yang sama persis** dengan `manage_exit()` di
   `pump_scanner_bot.py` (Stop Loss / Take Profit / Breakeven / Trailing /
   Maks Hold).

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
  Trailing, baru Max Hold -- supaya hasil backtest tidak melebih-lebihkan
  profit saat kondisinya ambigu.
- **`MOMENTUM_FADE_EXIT` tidak disimulasikan** (butuh data ranking seluruh
  pasar per candle, bukan cuma satu simbol).
- **Filter VWAP** memakai VWAP BERGULIR jangka pendek (window
  `CONFIRM_LOOKBACK_BARS`, sama dengan candle konfirmasi momentum), BUKAN
  VWAP sesi/harian -- lihat bagian "Filter VWAP" di atas untuk detail.
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
