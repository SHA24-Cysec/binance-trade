"""
Klien REST Binance Spot minimal, dibuat manual (bukan pakai SDK pihak ketiga).

Kenapa tidak pakai SDK resmi `binance-sdk-spot`?
- Keputusan ini dibuat berdasarkan temuan saat klien ini pertama ditulis
  (bug parse filter simbol oneOf di exchangeInfo). CATATAN AUDIT 2026-09-24:
  klaim tersebut TIDAK berhasil diverifikasi ulang dari sumber publik
  (SDK resmi ada dan aktif, versi 11.2.0). Statusnya: PERLU VERIFIKASI
  DOKUMENTASI. Apa pun hasilnya, klien manual ini tetap aman dipakai: ia
  memakai endpoint resmi yang didokumentasikan di developers.binance.com dan
  sudah diuji cocok dengan contoh signature resmi Binance.

Aturan signature (berlaku sejak 2026-01-15): payload harus di-percent-encode
dulu sebelum dihitung HMAC SHA256, atau request ditolak dengan -1022
INVALID_SIGNATURE. Fungsi `_build_query` di bawah sudah menerapkan ini.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import time
import urllib.parse
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

import requests

from rate_limiter import RateLimitBlockedError, SharedRequestWeightLimiter

logger = logging.getLogger("binance_client")


class BinanceAPIError(Exception):
    def __init__(self, status_code: int, code: Optional[int], msg: str):
        self.status_code = status_code
        self.code = code
        self.msg = msg
        super().__init__(f"HTTP {status_code} | code={code} | {msg}")


class SignedEndpointBlockedError(RuntimeError):
    """Dilempar bila kode mencoba mengirim request BERTANDA TANGAN dari klien
    yang dibuat dengan allow_signed=False.

    Ini pengaman keras mode PAPER: lapisan data pasar PAPER memakai klien
    keyless (allow_signed=False), sehingga upaya apa pun untuk menembak
    endpoint order/akun bertanda tangan -- bahkan bila API key kebetulan
    terisi di environment -- gagal keras alih-alih diam-diam menghubungi
    Binance dengan uang/akun asli.
    """


class BinanceRateLimitError(BinanceAPIError):
    """HTTP 429 (batas rate terlampaui) atau 418 (IP diblokir otomatis).

    Dipisahkan dari error biasa karena penanganannya berbeda secara
    fundamental: error biasa boleh dicoba ulang cepat, sedangkan ini WAJIB
    ditunggu sesuai header Retry-After. Dokumen resmi Binance menyebut ban
    IP "scale in duration for repeat offenders, from 2 minutes to 3 days",
    jadi mencoba ulang terlalu cepat justru memperburuk keadaan.
    """

    def __init__(self, status_code: int, code: Optional[int], msg: str,
                 retry_after: Optional[int] = None):
        super().__init__(status_code, code, msg)
        self.retry_after = retry_after


def _build_query(params: dict) -> str:
    """Percent-encode key & value lalu gabungkan jadi query string.
    Urutan dict di Python 3.7+ terjaga (insertion order), jadi urutan
    parameter yang kita masukkan akan konsisten dipakai untuk signing
    maupun pengiriman request (harus sama persis)."""
    items = []
    for k, v in params.items():
        if v is None:
            continue
        items.append(
            f"{urllib.parse.quote_plus(str(k))}={urllib.parse.quote_plus(str(v))}"
        )
    return "&".join(items)


def _fmt_num(value) -> str:
    """Format angka untuk parameter API Binance tanpa notasi ilmiah.

    str(float) di Python beralih ke notasi ilmiah untuk nilai < 1e-4
    (mis. str(0.0000833) -> '8.33e-05'), dan Binance menolak parameter
    quantity dalam bentuk itu (error -1100/-1013). Lewat Decimal + format
    'f' hasilnya selalu bentuk desimal biasa, berapa pun kecil nilainya.
    Nilai non-finite (NaN/inf) ditolak keras supaya bug harga/qty di hulu
    tidak pernah terkirim sebagai order.
    """
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"nilai non-finite tidak boleh dikirim ke API: {value!r}")
    if isinstance(value, Decimal):
        return format(value, "f")
    return format(Decimal(str(value)), "f")


class BinanceSpotClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str, timeout: float = 10.0,
                 allow_signed: bool = True, rate_limit_state_file: str | None = None,
                 rate_limit_limit: int = 6000, rate_limit_safety_margin: int = 100):
        """allow_signed=False membuat klien ini MENOLAK setiap request
        bertanda tangan (melempar SignedEndpointBlockedError). Dipakai lapisan
        data pasar mode PAPER agar tidak mungkin menyentuh endpoint order/akun.
        Header X-MBX-APIKEY sengaja TIDAK dipasang saat allow_signed=False
        supaya request publik benar-benar keyless."""
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.allow_signed = allow_signed
        self.session = requests.Session()
        if allow_signed and self.api_key:
            self.session.headers.update({"X-MBX-APIKEY": self.api_key})
        self._time_offset_ms = 0
        self._rate_limiter = SharedRequestWeightLimiter(
            rate_limit_state_file,
            limit=rate_limit_limit,
            safety_margin=rate_limit_safety_margin,
        )

        # Pelacakan kuota rate limit. Diisi dari header respons Binance.
        self.used_weight_1m = 0      # weight terpakai pada menit berjalan
        self.used_weight_ts = 0.0    # kapan angka di atas terakhir dibaca
        self.blocked_until = 0.0     # kapan IP boleh dipakai lagi setelah 429/418

    # ---------------------------------------------------------------
    # Infrastruktur dasar
    # ---------------------------------------------------------------
    def sync_time(self) -> None:
        """Samakan jam lokal dengan server Binance supaya timestamp request
        tidak pernah meleset (penting karena request signed punya recvWindow)."""
        server_time = self.get_server_time()
        local_time = int(time.time() * 1000)
        self._time_offset_ms = server_time - local_time
        logger.info("Sinkronisasi waktu server selesai. Offset = %d ms", self._time_offset_ms)

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    @staticmethod
    def _estimate_request_weight(path: str, params: dict | None = None) -> int:
        """Perkiraan konservatif REQUEST_WEIGHT sebelum request dikirim."""
        params = params or {}
        if path == "/api/v3/ticker/24hr" and not params.get("symbol"):
            return 80
        if path == "/api/v3/exchangeInfo":
            return 20 if not params.get("symbol") else 1
        if path == "/api/v3/klines":
            return 2
        if path == "/api/v3/depth":
            limit = int(params.get("limit", 100) or 100)
            return 5 if limit <= 100 else 25 if limit <= 500 else 50 if limit <= 1000 else 250
        if path == "/api/v3/ticker/bookTicker":
            return 4 if not params.get("symbol") else 2
        if path in ("/api/v3/ticker/price", "/api/v3/time"):
            return 2
        return 1

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        signed: bool = False,
        max_retries: int = 3,
    ) -> Any:
        # Pengaman keras mode PAPER: klien keyless tidak boleh mengirim request
        # bertanda tangan, apa pun isi environment. Gagal keras di sini, JAUH
        # sebelum menyentuh jaringan.
        if signed and not self.allow_signed:
            raise SignedEndpointBlockedError(
                f"Request bertanda tangan ke {method} {path} diblokir: klien ini "
                "dibuat dengan allow_signed=False (mode data pasar publik/PAPER)."
            )
        base_params = dict(params or {})

        def build_url() -> str:
            """Dibangun ulang di SETIAP percobaan (bukan sekali di luar loop),
            supaya timestamp & signature selalu segar. Sebelumnya ini dibangun
            sekali di luar loop retry -- bug: kalau percobaan pertama gagal
            karena -1021 lalu di-resync, percobaan berikutnya tetap memakai
            timestamp basi yang sama sehingga -1021 terus berulang."""
            local_params = dict(base_params)
            if signed:
                local_params["timestamp"] = self._timestamp()
                local_params.setdefault("recvWindow", 5000)
                q = _build_query(local_params)
                signature = hmac.new(
                    self.api_secret.encode("utf-8"), q.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                q = f"{q}&signature={signature}"
            else:
                q = _build_query(local_params)
            u = f"{self.base_url}{path}"
            return f"{u}?{q}" if q else u

        request_weight = self._estimate_request_weight(path, base_params)
        last_exc = None
        skip_shared_block = False
        for attempt in range(1, max_retries + 1):
            try:
                try:
                    if skip_shared_block:
                        # Retry internal sudah menunggu Retry-After. Tetap
                        # reservasi bobot request kedua, tetapi abaikan blok
                        # yang dibuat oleh respons 429 yang baru saja ditunggu.
                        self._rate_limiter.record_retry_after_server_wait(request_weight)
                    else:
                        self._rate_limiter.reserve(request_weight)
                    skip_shared_block = False
                except RateLimitBlockedError as exc:
                    raise BinanceRateLimitError(
                        429, None,
                        "shared rate limiter masih memblokir request sebelum dikirim",
                        retry_after=int(exc.retry_after),
                    ) from exc
                url = build_url()
                resp = self.session.request(method, url, timeout=self.timeout)

                # Binance mengirim sisa pemakaian kuota di SETIAP respons.
                # Dicatat supaya pemanggil yang rakus (mis. penyegar
                # watchlist otomatis) bisa mengerem sendiri SEBELUM kena 429.
                self._record_used_weight(resp.headers)

                if resp.status_code >= 400:
                    try:
                        body = resp.json()
                        code = body.get("code")
                        msg = body.get("msg", resp.text)
                    except ValueError:
                        code = None
                        msg = resp.text

                    # 429 = melanggar batas rate. 418 = IP sudah di-ban otomatis
                    # karena terus mengirim setelah kena 429.
                    #
                    # PENTING: ini TIDAK boleh diperlakukan seperti error biasa.
                    # Dokumen resmi Binance menyatakan ban IP "scale in duration
                    # for repeat offenders, from 2 minutes to 3 days", dan
                    # kewajiban klien adalah mundur, bukan mencoba lagi cepat.
                    # Backoff lama (maks 10 detik) justru mempercepat eskalasi.
                    # Sumber: developers.binance.com, General REST API
                    # Information / LIMITS (dicek 2026-09-24).
                    if resp.status_code in (429, 418):
                        retry_after = self._parse_retry_after(resp.headers)
                        self._note_rate_limited(resp.status_code, retry_after)
                        raise BinanceRateLimitError(
                            resp.status_code, code, msg, retry_after=retry_after
                        )

                    raise BinanceAPIError(resp.status_code, code, msg)
                if resp.text == "":
                    return {}
                return resp.json()
            except (requests.exceptions.RequestException, BinanceAPIError) as exc:
                last_exc = exc

                # Kena 429/418: hormati Retry-After dari server, jangan pakai
                # backoff tebakan sendiri yang bisa jauh lebih pendek.
                if isinstance(exc, BinanceRateLimitError):
                    # Lantai 5 detik diterapkan DI SINI (bukan di parser, yang
                    # sengaja dibiarkan setia pada header asli karena diuji
                    # terpisah). Ada laporan Binance pernah mengirim
                    # Retry-After bernilai 0/1; retry secepat itu justru bisa
                    # mempercepat eskalasi ke ban IP (HTTP 418) yang
                    # eskalatif 2 menit sampai 3 hari menurut dokumen resmi.
                    wait = max(5.0, float(exc.retry_after)) if exc.retry_after else min(60 * attempt, 180)
                    logger.error(
                        "Kena batas rate Binance (HTTP %s) pada %s %s. "
                        "Mundur %ds sesuai instruksi server (percobaan %d/%d).",
                        exc.status_code, method, path, wait, attempt, max_retries,
                    )
                    # HTTP 418 berarti IP sudah diblokir; mencoba lagi dalam
                    # proses yang sama hanya memperpanjang hukuman.
                    if exc.status_code == 418:
                        logger.error(
                            "HTTP 418: IP ini sedang diblokir Binance sampai %ds "
                            "ke depan. Permintaan dihentikan, tidak dicoba ulang.", wait,
                        )
                        raise
                    # Tidur hanya DI ANTARA percobaan (temuan S-10): kalau ini
                    # percobaan TERAKHIR (mis. jalur kritis posisi dengan
                    # max_retries=1), jangan tertahan sampai ratusan detik di
                    # sini -- lempar saja dan biarkan pemanggil (loop bot)
                    # yang mengatur jadwal coba-ulang berikutnya.
                    if attempt < max_retries:
                        time.sleep(wait)
                        # Percobaan berikutnya mengikuti Retry-After response
                        # ini. Ia tetap masuk ledger shared, tetapi block yang
                        # baru saja ditunggu tidak boleh mencegah retry itu.
                        skip_shared_block = True
                        continue
                    raise

                # -1021 = timestamp di luar recvWindow -> re-sync lalu retry
                if isinstance(exc, BinanceAPIError) and exc.code == -1021:
                    logger.warning("Timestamp meleset, sinkronisasi ulang jam server...")
                    self.sync_time()
                wait = min(2 ** attempt, 10)
                logger.warning(
                    "Request %s %s gagal (percobaan %d/%d): %s. Tunggu %ds.",
                    method, path, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
        raise last_exc

    # ---------------------------------------------------------------
    # Pelacakan kuota rate limit (dipakai penyegar watchlist otomatis)
    # ---------------------------------------------------------------
    def _record_used_weight(self, headers) -> None:
        """Simpan nilai header x-mbx-used-weight-1m kalau ada.

        Header ini dikirim Binance pada setiap respons dan berisi total
        weight yang sudah terpakai IP ini dalam menit berjalan. Batasnya
        6000/menit (dibaca dari exchangeInfo, dicek 2026-09-24).
        """
        try:
            raw = headers.get("x-mbx-used-weight-1m") or headers.get("X-MBX-USED-WEIGHT-1M")
            if raw is not None:
                self.used_weight_1m = int(raw)
                self.used_weight_ts = time.time()
                self._rate_limiter.observe_server_weight(self.used_weight_1m)
        except (TypeError, ValueError):
            pass

    @staticmethod
    def _parse_retry_after(headers) -> Optional[int]:
        try:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            if raw is not None:
                return max(1, int(float(raw)))
        except (TypeError, ValueError):
            pass
        return None

    def _note_rate_limited(self, status_code: int, retry_after: Optional[int]) -> None:
        """Catat kapan IP boleh dipakai lagi, supaya pemanggil lain ikut diam."""
        wait = retry_after if retry_after else (300 if status_code == 418 else 60)
        self.blocked_until = max(getattr(self, "blocked_until", 0.0), time.time() + wait)
        self._rate_limiter.block(wait)

    def is_rate_limited(self) -> bool:
        """True kalau IP sedang dalam masa tunggu akibat 429/418."""
        return time.time() < getattr(self, "blocked_until", 0.0)

    def weight_headroom(self, limit: int = 6000) -> float:
        """Perkiraan sisa kuota weight menit ini, sebagai pecahan 0..1.

        Dipakai penyegar watchlist untuk mengerem sendiri. Kalau header
        belum pernah terbaca, dianggap penuh (1.0) supaya tidak menghambat
        operasi normal bot secara tidak perlu.
        """
        ts = getattr(self, "used_weight_ts", 0.0)
        if not ts or time.time() - ts > 60:
            return 1.0
        used = getattr(self, "used_weight_1m", 0)
        return max(0.0, 1.0 - used / float(limit or 6000))

    # ---------------------------------------------------------------
    # Public endpoints (tidak butuh API key)
    # ---------------------------------------------------------------
    def get_server_time(self) -> int:
        data = self._request("GET", "/api/v3/time")
        return int(data["serverTime"])

    def get_exchange_info(self, symbol: Optional[str] = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/api/v3/exchangeInfo", params)

    def get_ticker_24hr_all(self) -> list:
        """Ambil statistik 24 jam untuk SEMUA pair sekaligus dalam satu
        request (weight lumayan besar, ~80 -- jangan dipanggil terlalu
        sering; cukup tiap beberapa menit untuk scanning pasar)."""
        return self._request("GET", "/api/v3/ticker/24hr", {})

    def get_klines(self, symbol: str, interval: str, limit: int = 500,
                    start_time_ms: Optional[int] = None, end_time_ms: Optional[int] = None) -> list:
        """start_time_ms/end_time_ms opsional -- dipakai fitur backtest untuk
        mengambil rentang historis tertentu lewat paging (endpoint ini
        maksimal mengembalikan 1000 candle per panggilan)."""
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        return self._request("GET", "/api/v3/klines", params)

    def get_book_ticker(self, symbol: str, max_retries: int = 3) -> dict:
        return self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol},
                             max_retries=max_retries)

    def get_depth(self, symbol: str, limit: int = 100, max_retries: int = 3) -> dict:
        """Order book (kedalaman) satu simbol -- GET /api/v3/depth.

        Dipakai simulasi PAPER untuk "berjalan" melalui level order book saat
        mengisi market order (bid untuk SELL, ask untuk BUY) sehingga harga
        rata-rata isi & slippage realistis. Juga dipakai sebagai snapshot awal
        untuk menyemai order book lokal berbasis WebSocket depth stream.

        Bobot request tergantung limit (dokumentasi Binance, dicek 2026-09-24):
        limit 1-100 -> weight 5; 101-500 -> 25; 501-1000 -> 50; 1001-5000 -> 250.
        Nilai limit yang diterima Binance: 5,10,20,50,100,500,1000,5000.
        Respons: {"lastUpdateId": int, "bids": [[price, qty], ...],
                  "asks": [[price, qty], ...]} (harga & qty berupa string).
        """
        return self._request("GET", "/api/v3/depth",
                             {"symbol": symbol, "limit": limit}, max_retries=max_retries)

    def get_book_ticker_all(self) -> list:
        """bookTicker untuk SEMUA simbol dalam satu request (dipakai modul
        penyegar watchlist). Dibungkus metode publik supaya modul lain tidak
        perlu memanggil _request (API privat) secara langsung."""
        return self._request("GET", "/api/v3/ticker/bookTicker")

    def get_price(self, symbol: str, max_retries: int = 3) -> float:
        """Ambil harga terakhir satu simbol.

        max_retries diteruskan ke _request. Jalur KRITIS posisi (manage_exit
        di loop utama) memanggil ini dengan max_retries=1 (perbaikan audit,
        temuan S-10): kegagalan cepat lebih baik daripada tertahan sampai
        180 detik di dalam retry panjang, karena selama tertahan SL/TP/BE/
        Trailing sama sekali tidak dievaluasi. Loop utama sudah mencoba lagi
        sendiri tiap LOOP_INTERVAL_SECONDS.
        """
        data = self._request("GET", "/api/v3/ticker/price", {"symbol": symbol},
                             max_retries=max_retries)
        return float(data["price"])

    # ---------------------------------------------------------------
    # Signed endpoints (butuh API key + secret)
    # ---------------------------------------------------------------
    def get_account(self) -> dict:
        return self._request("GET", "/api/v3/account", signed=True)

    def new_market_order(self, symbol: str, side: str, quantity: Optional[float] = None,
                          quote_order_qty: Optional[float] = None,
                          new_client_order_id: Optional[str] = None) -> dict:
        params = {"symbol": symbol, "side": side, "type": "MARKET"}
        # _fmt_num WAJIB di sini: str(float) berubah jadi notasi ilmiah untuk
        # nilai < 1e-4 (mis. '8.33e-05') dan Binance menolak quantity dalam
        # bentuk itu (-1100/-1013). Pada order SELL ini bisa menahan posisi
        # tanpa proteksi. AUDIT 2026-09-24 (temuan T-03).
        if quantity is not None:
            params["quantity"] = _fmt_num(quantity)
        if quote_order_qty is not None:
            params["quoteOrderQty"] = _fmt_num(quote_order_qty)
        if new_client_order_id:
            params["newClientOrderId"] = str(new_client_order_id)
        # POST order bersifat non-idempotent dari sudut matching engine.
        # Setelah timeout/HTTP 5xx, status eksekusi bisa UNKNOWN: retry buta
        # dapat membuat order kedua jika order pertama sudah FILLED. Pemanggil
        # wajib melakukan query berdasarkan clientOrderId, jadi hanya satu
        # percobaan dikirim dari lapisan REST ini.
        return self._request("POST", "/api/v3/order", params, signed=True,
                             max_retries=1)

    def new_stop_loss_order(self, symbol: str, quantity: float,
                            stop_price: float,
                            new_client_order_id: str | None = None) -> dict:
        """Pasang STOP_LOSS market SELL native Binance.

        Endpoint ini adalah lapisan proteksi independen dari loop Python.
        POST tidak diulang otomatis karena status jaringan dapat UNKNOWN.
        """
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP_LOSS",
            "quantity": _fmt_num(quantity),
            "stopPrice": _fmt_num(stop_price),
        }
        if new_client_order_id:
            params["newClientOrderId"] = str(new_client_order_id)
        return self._request("POST", "/api/v3/order", params, signed=True,
                             max_retries=1)

    def new_oco_sell_order(self, symbol: str, quantity: float,
                           above_price: float, above_stop_price: float,
                           below_price: float, below_stop_price: float,
                           list_client_order_id: str,
                           above_client_order_id: str,
                           below_client_order_id: str) -> dict:
        """Pasang OCO SELL TAKE_PROFIT_LIMIT + STOP_LOSS_LIMIT.

        Endpoint order-list bersifat non-idempotent. Intent semua ID wajib
        sudah disimpan oleh pemanggil sebelum method ini dipanggil.
        """
        params = {
            "symbol": symbol,
            "side": "SELL",
            "quantity": _fmt_num(quantity),
            "listClientOrderId": str(list_client_order_id),
            "aboveType": "TAKE_PROFIT_LIMIT",
            "aboveClientOrderId": str(above_client_order_id),
            "abovePrice": _fmt_num(above_price),
            "aboveStopPrice": _fmt_num(above_stop_price),
            "aboveTimeInForce": "GTC",
            "belowType": "STOP_LOSS_LIMIT",
            "belowClientOrderId": str(below_client_order_id),
            "belowPrice": _fmt_num(below_price),
            "belowStopPrice": _fmt_num(below_stop_price),
            "belowTimeInForce": "GTC",
            "newOrderRespType": "FULL",
        }
        return self._request("POST", "/api/v3/orderList/oco", params,
                             signed=True, max_retries=1)

    def get_order_list(self, order_list_id: int | None = None,
                       list_client_order_id: str | None = None) -> dict:
        params = {}
        if order_list_id is not None:
            params["orderListId"] = order_list_id
        if list_client_order_id is not None:
            params["origClientOrderId"] = str(list_client_order_id)
        return self._request("GET", "/api/v3/orderList", params, signed=True)

    def cancel_order_list(self, symbol: str, order_list_id: int | None = None,
                          list_client_order_id: str | None = None) -> dict:
        params = {"symbol": symbol}
        if order_list_id is not None:
            params["orderListId"] = order_list_id
        if list_client_order_id is not None:
            params["listClientOrderId"] = str(list_client_order_id)
        return self._request("DELETE", "/api/v3/orderList", params,
                             signed=True, max_retries=1)

    def get_dust_convertible(self, account_type: str = "SPOT") -> dict:
        """POST /sapi/v1/asset/dust-btc -- daftar aset "dust" (saldo kecil)
        yang SAAT INI diakui Binance sebagai layak dikonversi ke BNB, beserta
        estimasi hasil konversinya. Endpoint ini HANYA MEMBACA, tidak pernah
        mengeksekusi apa pun -- dipakai untuk memvalidasi dulu sebelum
        benar-benar memanggil convert_dust().
        Referensi resmi: developers.binance.com/docs/wallet/asset/assets-can-convert-bnb
        (dicek 2026-09-23)."""
        # Endpoint POST tidak diulang otomatis setelah status jaringan tidak pasti.
        return self._request("POST", "/sapi/v1/asset/dust-btc",
                             {"accountType": account_type}, signed=True,
                             max_retries=1)

    def convert_dust(self, assets: list, account_type: str = "SPOT") -> dict:
        """POST /sapi/v1/asset/dust -- konversi aset "dust" (saldo kecil) ke
        BNB. HANYA memproses asset yang disebutkan eksplisit di parameter
        `assets` -- TIDAK PERNAH "menyapu semua aset kecil di akun" secara
        implisit, supaya pemanggil (try_dust_sweep di pump_scanner_bot.py)
        yang mengontrol persis apa yang boleh disentuh.
        Format parameter `asset` di Binance adalah string dipisah koma
        (mis. "BTC,ETH"), BUKAN parameter berulang -- ini terkonfirmasi dari
        beberapa laporan pengguna komunitas python-binance yang membuktikan
        format array/berulang menghasilkan error -1022 (signature tidak
        valid), sedangkan format string dipisah koma berhasil.
        Binance sendiri MEMBATASI FREKUENSI endpoint ini per akun. Angka
        persisnya TIDAK ada di dokumentasi resmi yang bisa diverifikasi
        (laporan komunitas menyebut sekitar tiap 24 jam sekali -- status:
        PERLU VERIFIKASI DOKUMENTASI); yang jelas error dari batas tersebut
        harus ditangani oleh pemanggil sebagai hal wajar, bukan bug.
        Referensi resmi: developers.binance.com/docs/wallet/asset/dust-transfer
        (dicek 2026-09-23)."""
        params = {"asset": ",".join(assets), "accountType": account_type}
        # Konversi dapat mengubah saldo. Status unknown harus direkonsiliasi
        # oleh pemanggil, bukan diulang dengan request POST baru.
        return self._request("POST", "/sapi/v1/asset/dust", params, signed=True,
                             max_retries=1)


# ---------------------------------------------------------------------
# Util filter simbol (LOT_SIZE, NOTIONAL/MIN_NOTIONAL, PRICE_FILTER)
# ---------------------------------------------------------------------
class SymbolFilters:
    def __init__(self, step_size: Decimal, min_qty: Decimal, min_notional: Decimal,
                 tick_size: Decimal, max_qty: Decimal = Decimal("0"),
                 max_notional: Decimal = Decimal("0"),
                 quote_order_qty_market_allowed: bool = True):
        self.step_size = step_size
        self.min_qty = min_qty
        self.min_notional = min_notional
        self.tick_size = tick_size
        self.max_qty = max_qty
        self.max_notional = max_notional
        self.quote_order_qty_market_allowed = quote_order_qty_market_allowed

    @classmethod
    def from_symbol_data(cls, sym_data: dict) -> "SymbolFilters":
        """Parsing filter dari SATU entry symbol di exchangeInfo['symbols'].
        Dipakai oleh build_filters_cache() untuk membangun cache banyak
        simbol sekaligus tanpa scan ulang list symbols setiap kali."""
        symbol = sym_data.get("symbol", "?")

        # PENTING: untuk simbol likuid seperti BTCUSDT, Binance sekarang
        # mengirim MARKET_LOT_SIZE dengan stepSize/minQty = "0.00000000",
        # yang artinya "tidak ada batasan tambahan di luar LOT_SIZE" --
        # BUKAN berarti steps-nya benar-benar nol. Kalau nilai 0 ini asal
        # ditimpakan begitu saja, pembulatan quantity jadi rusak (tidak
        # dibulatkan sama sekali) dan order akan ditolak bursa karena
        # presisi quantity tidak sesuai LOT_SIZE. Jadi nilai 0 pada filter
        # manapun harus dianggap "abaikan", bukan "pakai nilai ini".
        lot_step = Decimal("0")
        lot_min = Decimal("0")
        market_lot_step = Decimal("0")
        market_lot_min = Decimal("0")
        lot_max_values = []
        market_lot_max_values = []
        min_notional_values = []
        max_notional_values = []
        tick_size = Decimal("0.01")

        for f in sym_data.get("filters", []):
            ftype = f.get("filterType")
            if ftype == "LOT_SIZE":
                lot_step = Decimal(f["stepSize"])
                lot_min = Decimal(f["minQty"])
                if Decimal(f.get("maxQty", "0")) > 0:
                    lot_max_values.append(Decimal(f["maxQty"]))
            elif ftype == "MARKET_LOT_SIZE":
                market_lot_step = Decimal(f["stepSize"])
                market_lot_min = Decimal(f["minQty"])
                if Decimal(f.get("maxQty", "0")) > 0:
                    market_lot_max_values.append(Decimal(f["maxQty"]))
            elif ftype == "MIN_NOTIONAL":
                if f.get("applyToMarket", True):
                    min_notional_values.append(Decimal(f["minNotional"]))
            elif ftype == "NOTIONAL":
                if f.get("applyMinToMarket", True):
                    min_notional_values.append(Decimal(f["minNotional"]))
                if f.get("applyMaxToMarket", False) and Decimal(f.get("maxNotional", "0")) > 0:
                    max_notional_values.append(Decimal(f["maxNotional"]))
            elif ftype == "PRICE_FILTER":
                tick_size = Decimal(f["tickSize"])

        # Bot ini SELALU memakai order MARKET, jadi idealnya patuhi
        # MARKET_LOT_SIZE -- tapi hanya kalau nilainya benar-benar > 0.
        step_size = market_lot_step if market_lot_step > 0 else lot_step
        min_qty = market_lot_min if market_lot_min > 0 else lot_min
        # LOT_SIZE tetap wajib dipatuhi juga (dua-duanya berlaku bersamaan
        # di sisi bursa), jadi ambil yang paling ketat di antara keduanya.
        if lot_step > 0:
            step_size = max(step_size, lot_step)
        min_qty = max(min_qty, lot_min)
        max_qty_values = lot_max_values + market_lot_max_values
        max_qty = min(max_qty_values) if max_qty_values else Decimal("0")
        min_notional = max(min_notional_values) if min_notional_values else Decimal("0")
        max_notional = min(max_notional_values) if max_notional_values else Decimal("0")

        if step_size <= 0:
            # Jaga-jaga kalau suatu saat kedua filter sama-sama 0 -- jangan
            # sampai bot mengira "tidak perlu pembulatan" padahal itu keliru.
            step_size = Decimal("0.00000001")
            logger.warning(
                "LOT_SIZE dan MARKET_LOT_SIZE sama-sama 0 untuk %s -- "
                "memakai fallback stepSize=%s. Mohon cek manual di Binance.",
                symbol, step_size,
            )

        return cls(
            step_size=step_size,
            min_qty=min_qty,
            min_notional=min_notional,
            tick_size=tick_size,
            max_qty=max_qty,
            max_notional=max_notional,
            quote_order_qty_market_allowed=bool(
                sym_data.get("quoteOrderQtyMarketAllowed", True)
            ),
        )

    def round_qty(self, qty: float) -> float:
        q = Decimal(str(qty))
        if self.step_size <= 0:
            return float(q)
        steps = (q / self.step_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = steps * self.step_size
        return float(rounded)

    def round_price(self, price: float, *, rounding=ROUND_DOWN) -> float:
        p = Decimal(str(price))
        if self.tick_size <= 0:
            return float(p)
        steps = (p / self.tick_size).to_integral_value(rounding=rounding)
        return float(steps * self.tick_size)


def build_trading_symbols(exchange_info: dict) -> set:
    """Himpunan simbol yang statusnya TRADING dan mengizinkan perdagangan SPOT.

    Dipakai scanner untuk membentuk semesta kandidat. Ticker 24 jam tetap
    mengirim baris untuk simbol berstatus HALT atau BREAK, dan order ke simbol
    seperti itu pasti ditolak bursa, jadi lebih baik dibuang sejak awal.
    Field status dan isSpotTradingAllowed berasal dari GET /api/v3/exchangeInfo
    (dicek 2026-09-25 di developers.binance.com, katalog Spot REST API).
    """
    out = set()
    for sym_data in exchange_info.get("symbols", []):
        symbol = sym_data.get("symbol")
        if not symbol:
            continue
        if str(sym_data.get("status", "")).upper() != "TRADING":
            continue
        if sym_data.get("isSpotTradingAllowed") is False:
            continue
        out.add(symbol)
    return out


def build_filters_cache(exchange_info: dict) -> dict:
    """Bangun cache {symbol: SymbolFilters} untuk SEMUA simbol dari satu
    respons exchangeInfo (dipakai mode pump scanner yang butuh filter utk
    banyak koin berbeda, bukan cuma satu simbol tetap)."""
    cache = {}
    for sym_data in exchange_info.get("symbols", []):
        symbol = sym_data.get("symbol")
        if not symbol:
            continue
        try:
            cache[symbol] = SymbolFilters.from_symbol_data(sym_data)
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("Lewati parsing filter untuk %s: %s", symbol, exc)
    return cache
