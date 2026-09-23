# Bot Pump Scanner — Binance Spot

Bot ini **memindai SEMUA pair USDT** di Binance Spot untuk mencari koin yang
sedang naik tajam (momentum chasing), lalu masuk dengan **satu entry** dan
keluar lewat Stop Loss / Take Profit / Breakeven / Trailing / batas waktu
hold / momentum pudar. **Tidak ada averaging-down/martingale.**

> Catatan: repo ini dulu juga berisi bot grid martingale (`bot.py`) untuk satu
> pair tetap (BTCUSDT). Fitur itu sudah dihapus. Yang tersisa sekarang hanya
> bot pump scanner.

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

# 3) Uji dulu dengan DRY_RUN. Ubah "DRY_RUN": False menjadi True di config.py,
#    lalu jalankan -- bot memakai data pasar ASLI dan menjalankan logika ASLI,
#    tapi TIDAK mengirim order sungguhan
python pump_scanner_bot.py

# 4) Setelah yakin, kembalikan DRY_RUN menjadi False di config.py, lalu jalankan lagi
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
- Kalau `DRY_RUN=False` tapi API key belum di-set, `run.py` menolak
  jalan dari awal (sama seperti proteksi yang sudah ada di
  `pump_scanner_bot.py`).

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
4. Kalau `DRY_RUN=True`, penjualan ini juga simulasi (tidak ada order
   sungguhan ke Binance), sama seperti seluruh mekanisme SL/TP lain di
   proyek ini. Kalau `DRY_RUN=False`, ini order SELL MARKET sungguhan.
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
- Di mode `DRY_RUN=True`, fitur ini **tidak pernah** memanggil API
  sungguhan (hanya simulasi/log), konsisten dengan seluruh mekanisme SL/TP/
  manual close lainnya di proyek ini.
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
