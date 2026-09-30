"""Penjaga integritas antara JavaScript dashboard dan DOM-nya.

Latar belakang. Pada 1 Oktober 2026 dashboard melempar galat runtime
"Cannot set properties of null (setting 'textContent')" setiap kali sebuah
backtest selesai. Penyebabnya sederhana tetapi tidak terlihat oleh test mana
pun yang ada: commit 0b6ca1d menghapus elemen

    <span id="btTradeCount"></span>

dari HTML, tetapi baris JavaScript yang memakainya ikut tertinggal hidup:

    document.getElementById('btTradeCount').textContent = '';

getElementById mengembalikan null, penulisan .textContent ke null melempar
TypeError, dan karena galat itu terjadi di tengah fungsi render, SELURUH panel
hasil batal digambar. Pengguna hanya melihat kotak merah tanpa penjelasan.

Kelas bug ini tidak akan pernah tertangkap oleh pytest biasa, karena kodenya
tidak dieksekusi Python. Karena itu berkas ini membaca template sebagai teks
lalu memastikan setiap id yang dirujuk JavaScript benar benar ada di HTML.

Test di sini sengaja tidak memerlukan Node. Yang diperiksa adalah kecocokan
rujukan, bukan jalannya program.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"


@pytest.fixture(scope="module")
def html() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _id_tersedia(s: str) -> set[str]:
    """Kumpulkan id yang tersedia saat runtime.

    Termasuk id statis di markup dan id yang dipasang dinamis lewat
    element.id = '...' di JavaScript.
    """
    statis = set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', s))
    dinamis = set(re.findall(r"\.id\s*=\s*['\"]([^'\"]+)['\"]", s))
    return statis | dinamis


def _id_dirujuk(s: str) -> dict[str, int]:
    """Petakan id yang dipakai getElementById ke nomor baris pertamanya."""
    hasil: dict[str, int] = {}
    for m in re.finditer(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", s):
        hasil.setdefault(m.group(1), s[: m.start()].count("\n") + 1)
    return hasil


def test_setiap_getelementbyid_punya_elemen(html):
    """Tidak boleh ada getElementById yang menunjuk elemen tidak ada.

    Inilah test yang seharusnya sudah ada sejak awal. Ia menangkap galat
    btTradeCount secara langsung.
    """
    ada = _id_tersedia(html)
    dirujuk = _id_dirujuk(html)
    menggantung = {k: v for k, v in dirujuk.items() if k not in ada}
    assert not menggantung, (
        "getElementById menunjuk id yang tidak ada di HTML. Setiap rujukan "
        "menggantung akan melempar TypeError dan membatalkan render panel: "
        + ", ".join(f"{k!r} (baris {v})" for k, v in sorted(menggantung.items()))
    )


def test_id_tidak_terduplikasi(html):
    """id ganda membuat getElementById memilih yang mana saja, diam diam."""
    semua = re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', html)
    ganda = {x for x in semua if semua.count(x) > 1}
    assert not ganda, f"id dipakai lebih dari sekali: {sorted(ganda)}"


def test_panel_hasil_backtest_lengkap(html):
    """Panel hasil backtest wajib punya wadah untuk setiap bagian hasil.

    Tanpa ini, mesin backtest yang sudah dipulihkan tetap tidak punya tempat
    menampilkan trade yang dihasilkannya.
    """
    wajib = {
        "btTradeBody": "tabel riwayat trade",
        "btTradeCount": "penghitung jumlah trade",
        "btSymbolBox": "kontribusi per simbol",
        "btSkipBox": "daftar sinyal terlewat",
        "btSkipCount": "penghitung sinyal terlewat",
        "btChartWrap": "kurva return kumulatif",
        "btInfoBox": "info dan parameter dipakai",
        "btLimitList": "daftar keterbatasan",
    }
    ada = _id_tersedia(html)
    kurang = {k: v for k, v in wajib.items() if k not in ada}
    assert not kurang, f"wadah hasil backtest hilang: {kurang}"


def test_tabel_trade_memakai_tbody_bukan_div(html):
    """btTradeBody harus tbody, karena JS mengisinya dengan baris <tr>.

    Saat dilucuti, elemen ini sempat diganti menjadi <div class="posbox">
    sementara JS tetap menulis '<tr>...' ke dalamnya. Browser membuang tag
    tabel yang tidak punya induk sah, jadi isinya tidak pernah tampil benar.
    """
    assert re.search(r'<tbody\s+id="btTradeBody"', html), (
        "btTradeBody bukan <tbody>. JS menulis <tr> ke elemen ini, sehingga "
        "harus berada di dalam struktur tabel yang sah."
    )


def test_kolom_tabel_trade_cocok_dengan_colspan(html):
    """Jumlah <th> harus sama dengan colspan baris kosongnya.

    Kalau tidak cocok, baris "tidak ada trade" akan tampak rusak lebarnya.
    """
    # Ambil posisi tbody-nya dulu, lalu mundur ke <thead> TERDEKAT sebelumnya.
    # Mencari maju dengan .*? akan menyeberang ke tabel lain di halaman yang
    # sama dan menghasilkan hitungan kolom yang keliru.
    posisi_tbody = html.find('<tbody id="btTradeBody">')
    assert posisi_tbody != -1, "tbody tabel trade tidak ditemukan"

    sebelum = html[:posisi_tbody]
    awal_thead = sebelum.rfind("<thead>")
    assert awal_thead != -1, "header tabel trade tidak ditemukan"
    blok_thead = sebelum[awal_thead:]
    jumlah_kolom = len(re.findall(r"<th[\s>]", blok_thead))

    # colspan baris kosong ditulis di JS. Halaman ini punya beberapa tabel yang
    # sama sama memakai variabel bernama tb, jadi pencarian dibatasi ke blok
    # setelah btTradeCount, yaitu badan fungsi render hasil backtest.
    awal_render = html.find("getElementById('btTradeCount')")
    assert awal_render != -1, "blok render tabel trade tidak ditemukan"
    baris_kosong = re.search(
        r"tb\.innerHTML\s*=\s*'<tr><td colspan=\"(\d+)\"", html[awal_render:]
    )
    assert baris_kosong, "baris kosong tabel trade tidak ditemukan"
    assert int(baris_kosong.group(1)) == jumlah_kolom, (
        f"colspan={baris_kosong.group(1)} tetapi tabel punya {jumlah_kolom} kolom"
    )


def test_fungsi_render_yang_dipanggil_memang_terdefinisi(html):
    """Setiap renderX() yang dipanggil harus punya definisinya.

    renderSymbolBreakdown dan renderSkipped sempat ikut terhapus bersama
    panelnya, dan pemanggilannya akan melempar ReferenceError.
    """
    terdefinisi = set(re.findall(r"function\s+(render[A-Za-z0-9_]*)\s*\(", html))
    terdefinisi |= set(
        re.findall(r"(?:const|let|var)\s+(render[A-Za-z0-9_]*)\s*=", html)
    )
    dipanggil = set(re.findall(r"\b(render[A-Za-z0-9_]*)\s*\(", html))
    hilang = dipanggil - terdefinisi
    assert not hilang, f"fungsi render dipanggil tetapi tidak terdefinisi: {sorted(hilang)}"


def test_tidak_ada_lagi_teks_data_only(html):
    """Sisa kalimat dari masa backtest dilucuti tidak boleh tertinggal.

    Kalimat seperti "backtest bersifat data-only" menyesatkan sekarang, karena
    mesinnya benar benar mensimulasikan trade.
    """
    menyesatkan = [
        "data-only",
        "tidak membuat trade atau order baru",
        "hanya memuat dan memvalidasi data historis",
    ]
    ketemu = [f for f in menyesatkan if f in html]
    assert not ketemu, f"teks peninggalan masa backtest dilucuti masih ada: {ketemu}"
