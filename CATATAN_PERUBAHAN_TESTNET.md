# Ringkasan Perubahan: DRY_RUN diganti Mode TESTNET

Tanggal: 2026-09-23

## Inti perubahan

Mode `DRY_RUN` (simulasi lokal, tidak pernah mengirim order) dihapus total dan
diganti `MODE = "TESTNET"`, yang konsepnya sama persis dengan bot live: order
benar-benar dikirim dan dieksekusi server Binance, hanya saja di Spot Test
Network dengan dana virtual.

Sekarang bot hanya punya dua mode:

| | `MODE="TESTNET"` (default) | `MODE="LIVE"` |
|---|---|---|
| Endpoint REST | `https://testnet.binance.vision` | `https://api.binance.com` |
| Order | Sungguhan, dana virtual | Sungguhan, uang asli |
| API key | Dari testnet.binance.vision | Binance produksi |
| Dust sweep (`/sapi/*`) | Dilewati otomatis | Aktif |
| Jalur kode | Sama persis | Sama persis |

Kenapa ini lebih baik dari DRY_RUN: jalur simulasi lama melewatkan hal-hal yang
paling sering bikin masalah saat live, yaitu pembulatan `LOT_SIZE`,
`MIN_NOTIONAL`, slippage market order, dan autentikasi. Di testnet semua itu
divalidasi oleh server Binance sungguhan.

## File yang berubah

### config.py
- `"DRY_RUN"` dan `"BASE_URL"` statis dihapus.
- Ditambah `"MODE"` (default `"TESTNET"`), `"LIVE_BASE_URL"`, `"TESTNET_BASE_URL"`.
- Ditambah helper `get_mode()`, `is_testnet()`, `get_base_url()`.
- `MODE` yang salah ketik otomatis jatuh ke `TESTNET`, bukan `LIVE`. Disengaja
  supaya typo tidak pernah berujung order pakai uang asli.
- `PUMP_CONFIG["BASE_URL"]` tetap diisi otomatis agar kode lama tidak pecah.

### pump_scanner_bot.py
- Parameter `dry_run` dihapus dari `open_position()`, `close_position()`,
  `manage_exit()`, `check_manual_control()`, `try_dust_sweep()`.
- Semua cabang `if dry_run: ... return` (BUY simulasi, SELL simulasi, equity
  palsu `MAX_POSITION_USDT * 5`) dihapus. Sekarang selalu jalur order asli.
- `open_position()`: pengambilan saldo dibungkus `try/except BinanceAPIError`.
  Sebelumnya di mode live, `get_account()` yang gagal akan melempar exception
  mentah ke loop utama dan menambah hitungan error beruntun. Sekarang entry
  hanya dilewati dengan log jelas.
- `run()`: validasi API key sekarang berlaku di kedua mode (testnet juga butuh
  kredensial), plus log pembuka menyebut mode dan endpoint yang dipakai.
- Dust sweep di mode TESTNET dilewati otomatis dengan log, karena Spot Test
  Network hanya menyediakan endpoint `/api/*`.

### run.py
- Label mode di log jadi TESTNET/LIVE beserta endpoint aktif.
- Pemeriksaan API key berlaku di kedua mode, dengan pesan yang mengarahkan ke
  testnet.binance.vision saat mode TESTNET.

### dashboard.py
- Client memakai `get_base_url()`, jadi dashboard otomatis ikut ke testnet.
- Field JSON `dry_run` diganti `mode` + `testnet`.
- Regex parser log `[DRY_RUN] BUY/SELL MARKET` dihapus karena pola log itu
  sudah tidak pernah muncul lagi. Riwayat trade kini hanya dari
  `BUY FILLED` / `SELL FILLED`, yang berarti PnL di dashboard selalu berasal
  dari harga eksekusi asli, bukan taksiran.

### templates/dashboard.html
- Banner dan pill mode diganti jadi TESTNET, dengan kalimat yang menegaskan
  order sungguhan ke Test Network memakai dana virtual.
- Tag baris riwayat `SIM` diganti `TESTNET`.

### README.md dan .env.example
- Ditambah bagian "Mode TESTNET dan LIVE" beserta cara pindah mode dan
  batasan testnet.
- Semua instruksi lama yang menyuruh mengubah `DRY_RUN` diperbarui.

## Bug yang ikut diperbaiki saat audit

1. **Selftest gagal sebelum perubahan ini** (sudah ada di repo asli, bukan efek
   samping). `--selftest` memakai `PUMP_CONFIG` langsung padahal angka
   pembandingnya hardcode. Begitu `SL_PCT` di-tuning jadi 1.8, assertion "rugi
   -2% belum boleh kena Stop Loss (ambang 3.0%)" gagal. Sekarang skenario exit
   memakai `cfg_exit` dengan ambang dikunci eksplisit, jadi selftest
   deterministik dan tidak ikut rusak saat config di-tuning.
2. **`get_account()` tanpa penanganan error di `open_position()`** saat mode
   live, seperti dijelaskan di atas.
3. Selftest sekarang memakai `FakeTradeClient` (client tiruan tanpa jaringan),
   karena `close_position()` sudah tidak punya jalur simulasi dan selalu
   memanggil `get_account()` lalu `new_market_order()`.

## Hasil verifikasi

- `python3 pump_scanner_bot.py --selftest`: semua lulus.
- `python3 backtest.py --selftest`: semua lulus.
- Semua file lulus `py_compile`.
- Dashboard dijalankan, `GET /` dan `GET /api/all` balas HTTP 200, field
  `mode` terbaca `TESTNET`.
- Base URL testnet dipastikan valid: koneksi ke `testnet.binance.vision`
  berhasil terbentuk. Catatan, permintaan dari lingkungan pengujian ini dibalas
  HTTP 451 (pemblokiran regional Binance), jadi eksekusi order end-to-end belum
  bisa saya buktikan dari sini dan perlu Anda jalankan sendiri.

## Yang perlu Anda lakukan

1. Buat API key di https://testnet.binance.vision (login pakai akun GitHub).
2. Salin `.env.example` jadi `.env`, isi dengan key testnet tersebut.
3. `python run.py`, lalu buka dashboard di port 8080.
4. Saat mau live: ubah `"MODE"` jadi `"LIVE"` di `config.py` dan ganti isi
   `.env` dengan key produksi.

## Catatan penting soal testnet

Sumber: Binance Developer Docs, halaman Testnet General Info (dicek 2026-09-23).

- Endpoint `/sapi/*` tidak tersedia di Spot Test Network, hanya `/api/*`.
- Testnet di-reset sekitar sebulan sekali; saldo dan order hilang, API key tetap.
- Likuiditas dan pergerakan harga di testnet tidak sama dengan pasar asli.
  Jadi profit/loss di testnet bukan prediksi hasil live. Yang divalidasi di
  sana adalah kebenaran mekanis bot, bukan profitabilitas strategi.
- Key produksi tidak berlaku di testnet dan sebaliknya; kalau tertukar Binance
  membalas error `-2015 Invalid API-key`.

---

# Tambahan: SL/TP adaptif berbasis ATR (opsional)

Tanggal: 2026-09-23

## Ringkasan

Ditambahkan opsi Stop Loss dan Take Profit berbasis ATR dengan bentuk
**hibrida** (ATR dengan batas bawah dan batas atas persen), plus alat
perbandingan di `backtest.py`.

**Default `USE_ATR_EXITS = False`**, jadi tanpa tindakan apa pun dari Anda
perilaku bot tidak berubah sedikit pun. Ini disengaja: data yang ada tidak
cukup kuat untuk menjadikan ATR sebagai default.

Rumusnya:

```
SL% = batasi(ATR_MULTIPLIER_SL x ATR%, antara ATR_SL_MIN_PCT dan ATR_SL_MAX_PCT)
TP% = SL% x ATR_TP_RR_RATIO
```

Cakupan sesuai permintaan: **Stop Loss + Take Profit saja**. Trailing sengaja
tidak disentuh, karena data yang saya temukan justru menunjukkan ATR trailing
stop berkinerja buruk (positif hanya di 4 dari 12 uji harian).

## File yang berubah

### strategy.py
- `true_ranges()`: True Range versi Wilder, termasuk penanganan gap antar candle.
- `atr()`: ATR dengan Wilder smoothing (RMA), bukan SMA. Ini penting supaya
  angkanya cocok dengan TradingView dan dengan literatur multiplier.
- `atr_percent()`: ATR sebagai persen harga, supaya bisa dibandingkan antar
  koin yang harganya berbeda ribuan kali lipat.
- `resolve_exit_levels()`: satu fungsi penentu level exit, dipakai **bersama**
  oleh bot live dan backtest. Kalau keduanya menghitung sendiri-sendiri, hasil
  backtest bisa diam-diam tidak mewakili bot sungguhan.

### config.py
- Ditambah `USE_ATR_EXITS`, `ATR_PERIOD`, `ATR_MULTIPLIER_SL`,
  `ATR_SL_MIN_PCT`, `ATR_SL_MAX_PCT`, `ATR_TP_RR_RATIO`.

### pump_scanner_bot.py
- Level exit dihitung sekali saat entry lalu **dikunci di state**
  (`sl_pct`, `tp_pct`, `exit_source`, `atr_pct_at_entry`).
- `manage_exit()` memakai level terkunci itu, dengan fallback ke config untuk
  state versi lama.
- Peringatan saat startup kalau `CONFIRM_LOOKBACK_BARS` lebih kecil dari
  `ATR_PERIOD + 1` (ATR akan selalu gagal dan bot diam-diam pakai nilai tetap).

### backtest.py
- Mendukung ATR dengan fungsi yang sama seperti bot.
- `compare_fixed_vs_atr()` dan `print_comparison()`: jalankan backtest dua kali
  pada candle yang sama, semua parameter lain identik.
- CLI baru: `--compare-atr`, `--symbol`, `--days`, `--atr-multiplier`,
  `--atr-period`, `--atr-min`, `--atr-max`, `--atr-rr`.

### dashboard.py
- Menampilkan level yang **benar-benar berlaku** untuk posisi terbuka, bukan
  nilai config. Kalau posisi memakai SL dari ATR tapi dashboard menampilkan
  nilai config, itu menyesatkan justru saat informasinya paling dibutuhkan.

## Keputusan desain yang perlu Anda ketahui

1. **Level dikunci saat entry, tidak dihitung ulang.** ATR bergerak terus.
   Stop yang ikut bergerak turun setelah posisi dibuka berarti risiko
   per-trade membengkak diam-diam setelah Anda sudah berkomitmen. Stop hanya
   boleh mengetat lewat Breakeven/Trailing, tidak pernah melonggar.
2. **Fallback tidak pernah meninggalkan posisi tanpa stop.** Kalau ATR gagal
   dihitung, bot memakai SL_PCT/TP_PCT tetap dan menandainya sebagai
   `ATR_FALLBACK_FIXED`.
3. **Bentuk hibrida, bukan ATR murni.** ATR murni bisa menghasilkan jarak stop
   ekstrem saat volatilitas meledak. Batas min/max menahan itu.

## Bug yang ikut diperbaiki

**Selftest backtest memakai indeks sebagai timestamp, bukan waktu sungguhan.**
Akibatnya `MAX_HOLD_MINUTES` dan cooldown tidak pernah aktif, dan skenario uji
hanya menghasilkan 1 trade. Sekarang timestamp memakai langkah 5 menit asli,
dan skenario yang sama menghasilkan ratusan trade. Ini membuat uji look-ahead
bias baru benar-benar bermakna (402 trade dibandingkan, bukan 1).

## Hasil verifikasi

- `pump_scanner_bot.py --selftest`: lulus, termasuk 3 skenario baru yang
  membuktikan `manage_exit` memakai level terkunci, bukan nilai config.
- `backtest.py --selftest`: lulus, termasuk:
  - True Range menangkap gap antar candle
  - Wilder smoothing terbukti berbeda dari SMA (13,61 vs 2,00)
  - ATR menolak data kurang (None, bukan angka asal)
  - SL dibatasi lantai dan plafon dengan benar
  - Fallback aman saat ATR gagal
  - Config min/max tertukar ditangani, tidak crash
  - **Uji look-ahead bias: 402 trade dibandingkan antara data pendek dan
    panjang, semua level SL identik.** Ini membuktikan ATR di backtest tidak
    pernah memakai candle masa depan.
- Default `USE_ATR_EXITS = False` terverifikasi menghasilkan SL 1,8% / TP 4,0%
  persis seperti sebelumnya.
- Dashboard jalan, HTTP 200, field baru terbaca.

## Cara memakai

```bash
# Bandingkan dulu, JANGAN langsung aktifkan
python backtest.py --compare-atr --symbol SOLUSDT --days 30

# Coba parameter lain
python backtest.py --compare-atr --symbol SOLUSDT --days 30 \
    --atr-multiplier 1.5 --atr-min 1.0 --atr-max 6.0
```

Baca bagian "Sebaran stop ATR" di output. Kalau banyak trade kena batas atas,
`ATR_SL_MAX_PCT` terlalu ketat dan ATR praktis tidak bekerja.

Kalau hasilnya meyakinkan pada beberapa simbol dan beberapa periode, barulah
set `USE_ATR_EXITS = True` di `config.py`, lalu uji di mode TESTNET dulu.

## Yang masih belum dikerjakan

`RISK_PERCENT = 95.0` belum disentuh. Prinsip inti ATR adalah stop melebar
harus dibarengi posisi mengecil supaya risiko rupiah tetap konstan. Selama
position sizing belum mengikuti jarak stop, manfaat ATR belum penuh. Ini
kandidat perbaikan berikutnya.

---

# Tambahan: RISK_PERCENT fleksibel + perhitungan biaya

Tanggal: 2026-09-23

## Temuan utama: config lama membatalkan RISK_PERCENT Anda

`RISK_PERCENT = 95.0` dipasang bersama `MAX_POSITION_USDT = 10.0`. Karena kode
memakai `min(nominal_dari_persen, MAX_POSITION_USDT)`, plafon selalu menang:

| Saldo | Niat 95% | Nyatanya dipakai |
|---|---|---|
| 100 USDT | 95,00 | 10,00 (10% saldo) |
| 1.000 USDT | 950,00 | 10,00 (1% saldo) |
| 5.000 USDT | 4.750,00 | 10,00 (0,2% saldo) |

Jadi bot tidak pernah all-in. Makin besar saldo, makin kecil porsinya. Ini
kemungkinan besar sebabnya kalau hasil terasa tidak berdampak.

## Yang diubah

### config.py
- `MAX_POSITION_USDT` default jadi **0 = tanpa plafon**, sehingga
  `RISK_PERCENT` benar-benar terpakai berapa pun saldo.
- `BALANCE_BUFFER_PCT = 0.5` (baru): bantalan teknis agar order MARKET tidak
  ditolak `-2010 insufficient balance` karena fee dipotong dari saldo yang sama.
- `MAX_SPREAD_PCT` dari `0.5` ke `0.25`.
- `TAKER_FEE_PCT = 0.1` dan `USE_BNB_FEE_DISCOUNT = False` (baru), plus helper
  `get_taker_fee_pct()`. Binance Spot VIP0 2026 = 0,1%, dengan BNB jadi 0,075%.

### pump_scanner_bot.py
- Sizing memakai bantalan, plafon jadi opsional.
- Kalau plafon memotong ukuran posisi, bot **memperingatkan di log** lengkap
  dengan persentase sesungguhnya, agar bug lama tidak terulang diam-diam.

### backtest.py
- Fee taker diperhitungkan dua kali (beli + jual) pada setiap trade.
- Ringkasan memisahkan Return KOTOR, Hilang karena fee, dan Return BERSIH.

## Bug yang ikut diperbaiki

**Return kotor dijumlah biasa sementara return bersih di-compound.** Ini
menghasilkan hal mustahil: bersih (2018%) terlihat lebih besar dari kotor
(446%). Sekarang keduanya di-compound, jadi sebanding.

## Kenapa ini penting untuk kebiasaan all-in

Pada data uji, biaya menelan hasil kotor 7.853% menjadi bersih 2.018%, yakni
sekitar **74% hasil kotor habis oleh fee**. Ini risiko sesungguhnya dari
all-in berulang, bukan Stop Loss.

Soal SL: dengan SL 1,8%, kalah 10 kali beruntun menyisakan 83,4% saldo, dan
pada win rate 60% kejadian itu hanya muncul ~0,05 kali per 500 trade. Jadi
all-in dengan SL ketat di spot **bukan** skenario kiamat seperti di futures
ber-leverage. Yang menggerus adalah biaya per putaran terhadap seluruh modal.

## Hasil verifikasi

- Sizing diuji pada saldo 100 / 1.000 / 5.000: konsisten 94,53% di semua level
  (sebelumnya 10% / 1% / 0,2%).
- Plafon aktif diuji: benar-benar mengikat dan memicu peringatan.
- `RISK_PERCENT = 100` diuji: memakai 995 dari 1.000, menyisakan ruang fee.
- Mode nominal tetap (`USE_RISK_PERCENT = False`) tidak berubah.
- Selftest SL memverifikasi kotor -3,00%, fee 0,20%, bersih -3,20%.
- Semua selftest bot dan backtest lulus, semua file lulus compile.

## Saran

Nyalakan `USE_BNB_FEE_DISCOUNT = True` dan simpan sedikit BNB di Spot wallet.
Biaya turun dari 0,2% ke 0,15% per putaran. Pada volume trade bot ini, itu
selisih yang nyata.

---

# Tambahan: Breakeven & Trailing ikut skala ATR

Tanggal: 2026-09-23

## Masalah yang ditemukan

Implementasi ATR sebelumnya hanya mencakup SL dan TP (sesuai cakupan yang
diminta saat itu). Akibatnya pincang: SL/TP melebar mengikuti volatilitas,
tapi Breakeven dan Trailing tetap memakai angka tetap dari config.

Rasio trailing terhadap ATR jadi timpang:

| ATR koin | Trailing 0,6% setara | Akibat |
|---|---|---|
| 0,5% | 1,20x ATR | wajar |
| 1,0% | 0,60x ATR | kena noise |
| 3,0% | 0,20x ATR | kena noise parah |
| 5,0% | 0,12x ATR | hampir pasti langsung kena |

Terukur di backtest: TAKE_PROFIT hanya tercapai 2,9% dari trade, sementara
91,2% ditutup BE/Trailing di rata-rata +1,78% padahal TP dipasang 6,76%.
Risk:reward terbalik.

## Yang diubah

### config.py
Empat pengali baru: `ATR_BE_TRIGGER_MULT` (0.5), `ATR_BE_LOCK_MULT` (0.1),
`ATR_TRAILING_START_MULT` (1.0), `ATR_TRAILING_STEP_MULT` (1.5).

Trailing step 1,5x ATR dipilih karena data yang dikutip sebelumnya: stop di
bawah 1,0x ATR terpicu noise >65% dalam 3 bar pertama, di 1,5x turun ke 38%.

### strategy.py
`resolve_exit_levels()` kini mengembalikan enam level, bukan dua. Dua pengaman
ditambahkan:
- Trailing step dibatasi tidak melebihi SL (kalau lebih longgar, SL selalu
  kena duluan dan trailing cuma ilusi).
- Breakeven dipaksa terpicu sebelum Trailing.

### pump_scanner_bot.py
State menyimpan `be_trigger_pct`, `be_lock_pct`, `trail_start_pct`,
`trail_step_pct`. `manage_exit()` memakai level terkunci itu, dengan fallback
ke config untuk state versi lama.

### backtest.py
Memakai level yang sama, jadi backtest tetap mewakili perilaku bot.

### dashboard.py
Menampilkan level BE/Trailing yang benar-benar berlaku untuk posisi terbuka.

## Hasil

| | TP tercapai | Ditutup BE/Trail | Profit factor |
|---|---|---|---|
| Pincang (BE/Trail tetap) | 2,9% | 91,2% | 3,60 |
| Semua ikut ATR | 8,5% | 80,4% | 4,70 |
| Semua tetap (ATR off) | 5,9% | 78,0% | 2,48 |

Angka dari data sintetis, jadi besarannya tidak berlaku untuk pasar asli.
Yang valid adalah arah perbaikannya.

## Verifikasi

- Selftest baru: level BE/Trailing berskala ATR, trailing dibatasi <= SL,
  BE dipaksa sebelum Trailing, `manage_exit` memakai level state bukan config.
- `USE_ATR_EXITS = False` terverifikasi tetap identik dengan perilaku lama
  (SL 1,8 / TP 4,0 / BE 1,0 / Trail 1,5 / step 0,6).
- Semua selftest bot dan backtest lulus, semua file lulus compile.
- Dashboard HTTP 200, field baru terbaca.

---

# Tambahan: Parameter ATR bisa diatur dari dashboard

Tanggal: 2026-09-23

## Masalah

Form backtest di dashboard hanya punya 9 parameter dan tidak satu pun
menyentuh ATR. Jadi kalau `USE_ATR_EXITS = True` di config, nilai SL/TP yang
diketik di form tetap ditimpa oleh hitungan ATR tanpa penjelasan apa pun.
Membingungkan, dan parameter ATR sama sekali tidak bisa diuji dari UI.

## Yang diubah

### backtest.py
- `apply_overrides()` menerima `USE_ATR_EXITS` (dengan caster boolean yang juga
  memahami string `"true"`/`"false"` dari form web) plus empat pengali
  BE/Trailing.
- `validate_params()` menambah rentang wajar untuk keempat pengali, dan
  pemeriksaan baru: batas bawah SL ATR tidak boleh melebihi batas atasnya.
- Flag CLI baru: `--atr-be-trigger`, `--atr-be-lock`, `--atr-trail-start`,
  `--atr-trail-step`.

### dashboard.py
- Daftar parameter disatukan jadi satu konstanta `BT_PARAM_KEYS` supaya
  endpoint start, defaults, dan params_used tidak bisa lagi tidak sinkron.
- Endpoint start menerima `compare: true`, memanggil `compare_fixed_vs_atr()`
  dan mengembalikan hasil kedua sisi. Dalam mode banding, toggle
  `USE_ATR_EXITS` dari form sengaja diabaikan (mode ini mengatur sendiri).

### templates/dashboard.html
- Bagian baru "Exit berbasis ATR": toggle plus sembilan field parameter.
- Field Stop Loss dan Take Profit otomatis diredupkan dan dinonaktifkan saat
  ATR aktif, karena nilainya akan ditimpa.
- Tabel preview yang menghitung ulang otomatis: menunjukkan level yang
  benar-benar berlaku untuk ATR 0,5% sampai 5%, termasuk efek kedua clamp.
- Tombol "Bandingkan Tetap vs ATR" plus tabel hasil berdampingan, dengan
  penanda hijau di sisi yang unggul per baris.
- Kedua tombol saling mengunci supaya tidak bisa menjalankan dua job sekaligus.

## Verifikasi

Diuji lewat Flask test client dengan candle sintetis (Binance memblokir IP
sandbox dengan HTTP 451, murni pembatasan geografis, bukan bug kode):

1. Mode banding mengembalikan kedua sisi, hasilnya bisa diserialisasi JSON.
2. Mode ATR tunggal: `compare` bernilai null, `USE_ATR_EXITS` true.
3. Mode tetap tunggal berjalan normal.
4. Pengali BE/Trailing dari form terbukti mengubah hasil.
5. Mode banding memberi hasil identik apa pun posisi toggle.
6. Regresi: hasil mode tetap tunggal identik dengan sisi TETAP di mode banding.

Validasi input juga diuji dan menolak dengan pesan yang jelas: batas bawah SL
lebih besar dari batas atas, dan pengali di luar rentang wajar.
