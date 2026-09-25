"""Generator data candle sintetis yang deterministik untuk tes dan selftest.

SATU sumber data sintetis dipakai bersama oleh tests/, test_watchlist.py, dan
selftest di pump_scanner_bot.py, backtest.py, serta portfolio_backtest.py.
Alasannya sederhana: kalau setiap file membuat candle sendiri, mudah sekali
satu di antaranya lupa mengisi volume, dan deteksi setup yang memakai anchored
VWAP diam-diam selalu gagal di file itu saja.

ATURAN WAJIB di modul ini: setiap candle SELALU punya volume dan quote_volume
yang konsisten, dengan quote_volume = volume x harga rata-rata candle
(rata-rata high, low, close). Nilai default Kline.volume dan
Kline.quote_volume adalah 0.0, dan VWAP dari volume nol tidak terdefinisi.

Semua angka di sini dipilih supaya skenario mudah dibaca manusia, bukan untuk
mewakili perilaku pasar sungguhan. Jangan memakainya untuk menilai strategi.
"""

from __future__ import annotations

from strategy import Kline

MS_PER_BAR = 300_000  # 5 menit


def make_candle(index: int, open_: float, high: float, low: float, close: float,
                volume: float = 1_000.0, bar_ms: int = MS_PER_BAR) -> Kline:
    """Satu candle dengan quote_volume yang konsisten terhadap volume dan harga.

    quote_volume = volume x rata-rata (high, low, close). Ini mendekati cara
    bursa menghitung quote asset volume (jumlah harga x kuantitas per trade),
    cukup untuk membuat anchored VWAP bergerak masuk akal pada data uji.
    """
    harga_rata = (high + low + close) / 3.0
    return Kline(
        open_time=index * bar_ms,
        open=float(open_),
        high=float(high),
        low=float(low),
        close=float(close),
        close_time=index * bar_ms + (bar_ms - 1),
        volume=float(volume),
        quote_volume=float(volume) * harga_rata,
    )


def sideways_base(n: int = 30, harga: float = 100.0, pivot_index: int = 20,
                  pivot_high: float = 101.0, volume: float = 1_000.0) -> list[Kline]:
    """Deret candle datar dengan SATU pivot high yang jelas.

    Pivot high inilah yang menjadi breakout_level pada skenario di bawah.
    Candle di kiri dan kanan pivot sengaja dibuat lebih rendah supaya pivot
    terdeteksi untuk SWING_PIVOT_WING_BARS sampai beberapa candle.
    """
    out = []
    for i in range(n):
        o = harga
        h = harga + 0.4
        l = harga - 0.4
        c = harga + 0.1
        if i == pivot_index:
            h = pivot_high
            c = harga + 0.5
        out.append(make_candle(i, o, h, l, c, volume))
    return out


def skenario_pullback_retest(nama: str = "lolos") -> list[Kline]:
    """Kumpulan skenario deteksi setup, semuanya memakai level pivot 101.0.

    Nama skenario:
      lolos                  breakout, pullback ke zona, close kembali di atas level
      wick_saja              sumbu menembus level tetapi close di bawahnya
      close_di_bawah_level   retest menyentuh zona tetapi close di bawah level
      close_lemah            close retest di bagian bawah range candle
      kedaluwarsa            retest datang setelah MAX_BARS_BREAKOUT_TO_RETEST
      terlalu_jauh           close retest sudah jauh di atas level (anti-kejar)
      di_bawah_vwap          close retest di atas level tetapi di bawah anchored VWAP
      vwap_jauh              anchored VWAP jauh dari level (tanpa konfluensi)
      invalidasi             ada candle yang close di bawah batas invalidasi
      datar                  seluruh candle identik (ATR nol)
      volume_nol             sama dengan "lolos" tetapi volume nol
      data_kurang            jumlah candle di bawah required_lookback_bars()
    """
    base = sideways_base()
    i = len(base)

    if nama == "data_kurang":
        return base[:10]

    if nama == "datar":
        return [make_candle(j, 100.0, 100.0, 100.0, 100.0) for j in range(40)]

    volume = 0.0 if nama == "volume_nol" else 1_000.0

    if nama == "wick_saja":
        # Sumbu menembus 101 tetapi close tetap di bawah level.
        base.append(make_candle(i, 100.2, 102.3, 100.1, 100.8, volume)); i += 1
        base.append(make_candle(i, 100.8, 101.0, 100.4, 100.6, volume)); i += 1
        base.append(make_candle(i, 100.6, 100.9, 100.3, 100.7, volume)); i += 1
        base.append(make_candle(i, 100.7, 101.0, 100.5, 100.9, volume)); i += 1
        return base

    if nama == "vwap_jauh":
        # Breakout sangat jauh di atas level, lalu harga jatuh kembali ke zona.
        # Anchored VWAP tertinggal jauh di atas level sehingga tidak konfluen.
        base.append(make_candle(i, 100.2, 106.2, 100.1, 106.0, volume * 5)); i += 1
        base.append(make_candle(i, 106.0, 106.3, 104.0, 104.2, volume * 5)); i += 1
        base.append(make_candle(i, 104.2, 104.4, 101.2, 101.6, volume)); i += 1
        base.append(make_candle(i, 101.6, 102.0, 100.9, 101.9, volume)); i += 1
        return base

    # Breakout standar: close 102.2, di atas 101 ditambah buffer.
    base.append(make_candle(i, 100.2, 102.3, 100.1, 102.2, volume)); i += 1
    base.append(make_candle(i, 102.2, 102.5, 101.8, 102.0, volume)); i += 1
    base.append(make_candle(i, 102.0, 102.2, 101.4, 101.6, volume)); i += 1

    if nama == "kedaluwarsa":
        # Harga menggantung di atas level jauh melewati batas umur setup, lalu
        # baru datang ke zona. Setup lama sudah gugur karena waktu.
        for _ in range(14):
            base.append(make_candle(i, 101.8, 102.1, 101.5, 101.9, volume)); i += 1
        base.append(make_candle(i, 101.5, 101.95, 100.9, 101.9, volume)); i += 1
        return base

    if nama == "invalidasi":
        # Candle tertutup jauh di bawah level, setup gugur sebelum retest.
        base.append(make_candle(i, 101.6, 101.7, 99.2, 99.3, volume)); i += 1
        base.append(make_candle(i, 99.3, 101.0, 99.2, 100.9, volume)); i += 1
        return base

    if nama == "terlalu_jauh":
        base.append(make_candle(i, 101.3, 103.6, 100.9, 103.5, volume)); i += 1
        return base

    if nama == "close_di_bawah_level":
        base.append(make_candle(i, 101.4, 101.45, 100.9, 100.95, volume)); i += 1
        return base

    if nama == "close_lemah":
        base.append(make_candle(i, 101.6, 101.8, 100.9, 101.02, volume)); i += 1
        return base

    if nama == "di_bawah_vwap":
        base.append(make_candle(i, 101.1, 101.35, 100.9, 101.3, volume)); i += 1
        return base

    # nama == "lolos" atau "volume_nol"
    base.append(make_candle(i, 101.3, 101.95, 100.9, 101.9, volume)); i += 1
    return base


def lanjutan_setelah_entry(klines: list[Kline], arah: str = "invalidasi",
                           jumlah: int = 6) -> list[Kline]:
    """Tambahkan candle SETELAH candle entry untuk menguji jalur exit.

    arah "invalidasi" menurunkan harga menembus batas invalidasi setup,
    arah "bertahan" menahan harga tetap di atas level breakout.
    """
    out = list(klines)
    i = len(out)
    harga = out[-1].close
    for _ in range(jumlah):
        if arah == "invalidasi":
            harga -= 0.5
            out.append(make_candle(i, harga + 0.5, harga + 0.55, harga - 0.1, harga))
        else:
            harga += 0.05
            out.append(make_candle(i, harga - 0.05, harga + 0.2, harga - 0.15, harga))
        i += 1
    return out


def seri_dengan_setup(harga: float = 1.0, bar_datar: int = 288, volume: float = 5_000_000.0,
                      ekor: str = "naik", panjang_ekor: int = 20,
                      mulai_index: int = 0) -> list[Kline]:
    """Deret panjang yang memuat SATU setup pullback retest di dekat ujungnya.

    Dipakai backtest satu simbol dan backtest portofolio supaya keduanya
    memakai skenario yang sama persis. Bagian datar di depan diperlukan
    karena run_backtest() butuh 24 jam penuh (288 candle 5 menit) sebelum
    statistik bergulir 24 jam terisi.

    ekor:
      "naik"       harga naik terus setelah entry (menguji Take Profit)
      "invalidasi" harga jatuh menembus batas invalidasi setelah entry
      "bertahan"   harga bergerak datar sedikit di atas level breakout
    """
    out: list[Kline] = []
    i = mulai_index
    for _ in range(bar_datar):
        out.append(make_candle(i, harga, harga * 1.002, harga * 0.998, harga * 1.0005, volume))
        i += 1

    blok, harga_akhir, i = blok_setup(harga, i, volume)
    out.extend(blok)

    harga_kini = harga_akhir
    for _ in range(panjang_ekor):
        if ekor == "naik":
            harga_kini *= 1.01
            out.append(make_candle(i, harga_kini / 1.01, harga_kini * 1.002, harga_kini / 1.011,
                                   harga_kini, volume))
        elif ekor == "invalidasi":
            harga_kini *= 0.99
            out.append(make_candle(i, harga_kini / 0.99, harga_kini * 1.001, harga_kini * 0.998,
                                   harga_kini, volume))
        else:
            out.append(make_candle(i, harga_kini, harga_kini * 1.001, harga_kini * 0.999,
                                   harga_kini, volume))
        i += 1
    return out


def blok_setup(harga: float, mulai_index: int, volume: float = 5_000_000.0):
    """Satu blok 16 candle: konsolidasi, pivot high, breakout, lalu retest sah.

    Return (candles, harga_penutupan_terakhir, index_berikutnya). Dipisah dari
    seri_dengan_setup supaya bisa dirangkai berkali-kali pada satu deret.
    """
    out: list[Kline] = []
    i = mulai_index
    # Struktur: 12 candle datar dengan satu pivot high di tengah.
    for j in range(12):
        o = harga
        h = harga * 1.004
        l = harga * 0.996
        c = harga * 1.001
        if j == 6:
            h = harga * 1.012
            c = harga * 1.006
        out.append(make_candle(i, o, h, l, c, volume))
        i += 1

    level = harga * 1.012
    # Candle breakout, close jelas di atas level.
    out.append(make_candle(i, harga * 1.002, level * 1.013, harga * 0.999, level * 1.012, volume)); i += 1
    # Dua candle menjauh lalu mulai turun kembali.
    out.append(make_candle(i, level * 1.012, level * 1.014, level * 1.006, level * 1.008, volume)); i += 1
    out.append(make_candle(i, level * 1.008, level * 1.010, level * 1.002, level * 1.004, volume)); i += 1
    # Candle retest: low masuk zona, close kembali di atas level.
    out.append(make_candle(i, level * 1.001, level * 1.009, level * 0.997, level * 1.008, volume)); i += 1
    return out, level * 1.008, i


def seri_banyak_setup(harga: float = 100.0, siklus: int = 10, bar_datar: int = 288,
                      volume: float = 5_000_000.0, naik: int = 6, turun: int = 20,
                      mulai_index: int = 0) -> list[Kline]:
    """Deret panjang berisi BANYAK setup pullback retest berturut-turut.

    Setiap siklus: struktur setup, lalu harga naik sebentar, lalu turun lagi
    ke sekitar harga awal siklus supaya deretnya tidak meledak secara
    eksponensial. Dipakai backtest portofolio yang butuh banyak kesempatan
    entry di sepanjang garis waktu.
    """
    out: list[Kline] = []
    i = mulai_index
    for _ in range(bar_datar):
        out.append(make_candle(i, harga, harga * 1.002, harga * 0.998, harga * 1.0005, volume))
        i += 1

    for _ in range(max(1, siklus)):
        blok, harga, i = blok_setup(harga, i, volume)
        out.extend(blok)
        for _ in range(naik):
            harga *= 1.01
            out.append(make_candle(i, harga / 1.01, harga * 1.002, harga / 1.011, harga, volume))
            i += 1
        for _ in range(turun):
            harga *= 0.995
            out.append(make_candle(i, harga / 0.995, harga * 1.001, harga * 0.997, harga, volume))
            i += 1
    return out


# ======================================================================
# Data pendukung GERBANG PUMP (naik 24 jam + volume naik)
# ======================================================================

def riwayat_harian(klines: list[Kline], hari: int = 7,
                   quote_volume_harian: float = 1_000_000.0,
                   harga: float = 1.0) -> list[Kline]:
    """Candle 1d PENUH yang seluruhnya tertutup SEBELUM deret ``klines``.

    Dipakai tes dan selftest yang memanggil backtest: gerbang pump menolak
    simbol yang belum punya tujuh candle harian penuh (fail closed), jadi
    skenario yang ingin menguji hal LAIN (exit, sizing, invalidasi) tetap
    harus menyediakan riwayat harian yang masuk akal.

    Candle dibuat mundur dari candle pertama ``klines``, satu hari per candle.
    Timestamp boleh negatif karena data sintetis memakai open_time mulai dari
    nol; yang dipakai gerbang hanya urutan waktu dan quote_volume.
    """
    ms_per_day = 86_400_000
    mulai = int(klines[0].open_time) if klines else 0
    out: list[Kline] = []
    for n in range(hari, 0, -1):
        open_time = mulai - n * ms_per_day
        out.append(Kline(
            open_time=open_time,
            open=harga, high=harga, low=harga, close=harga,
            close_time=open_time + ms_per_day - 1,
            volume=quote_volume_harian / max(harga, 1e-9),
            quote_volume=float(quote_volume_harian),
        ))
    return out


def cfg_gerbang_pump_nonaktif(config: dict) -> dict:
    """Salinan config dengan ambang gerbang pump dilonggarkan total.

    HANYA untuk pengujian yang menguji bagian lain dari pipeline (exit,
    sizing, paritas live vs backtest). Gerbang pump sendiri diuji terpisah di
    tests/test_pump_gate.py dengan ambang sungguhan. Melonggarkan ambang di
    sini membuat tes tersebut tidak ikut merah setiap kali default gerbang
    diubah, tanpa perlu mematikan gerbangnya lewat jalan belakang di kode
    produksi.
    """
    out = dict(config)
    out["PUMP_MIN_24H_CHANGE_PCT"] = -1000.0
    out["PUMP_VOLUME_SURGE_MULT"] = 0.0
    return out
