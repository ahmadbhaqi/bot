"""
================================================================================
  GITHUB EDU AUTO-APPLY - PLAYWRIGHT EDITION (BHAQI CORE v16)
================================================================================
Rewrite penuh dari apply_edu_github.py + bulk_apply_edu.py ke Playwright.

Perubahan utama dibanding versi Selenium/AdsPower:
  - TANPA AdsPower. Pakai Chromium bawaan Playwright (browser unggulan).
  - Setiap akun mendapat BROWSER BARU dengan IDENTITAS BERBEDA:
    user-agent, viewport, timezone, locale, hardwareConcurrency, deviceMemory,
    WebGL vendor/renderer di-randomize + context bersih (cookies/storage kosong)
    -> tiap sesi terasa seperti browser yang benar-benar baru.
  - Satu file menggabungkan mode SATUAN (single) dan BANYAK (bulk).
    Saat dijalankan, program menanyakan mode terlebih dahulu.

Semua fungsi inti dipertahankan:
  - TOTP/2FA native (RFC 6238) + sinkronisasi NTP (cache)
  - Parsing profil GitHub (avatar, location, display name)
  - Render ID card via cardgenerator.html + capture isolated viewport
  - Spoof kamera WebRTC (Logitech C922) dengan hand-tremor + CMOS noise
  - Pengisian form edu, deteksi "not on campus", retry
  - Klasifikasi status edu (verified / pending / declined)
  - Monitoring status dengan refresh berkala

Format accounts.json:
  [
    { "username": "githubuser", "secret_key": "TOTP_SECRET" },
    ...
  ]
"""

import base64
import hashlib
import hmac
import json
import os
import random
import re
import string
import struct
import sys
import time
import traceback
from datetime import datetime

import requests

# Patchright = drop-in Playwright yang sudah ter-patch anti-deteksi (mirip UC).
# Pakai patchright bila tersedia; fallback ke playwright biasa.
try:
    from patchright.sync_api import sync_playwright
    _USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright
    _USING_PATCHRIGHT = False

# ==============================================================================
# INTEGRASI BOT — log sink (1 pesan, ditimpa berulang)
# ==============================================================================
# Bot Telegram memasang callback di sini untuk menerima setiap baris log.
# Bila terpasang, `print` di modul ini diarahkan ke callback (selain stdout),
# sehingga proses apply bisa ditampilkan sebagai SATU pesan yang di-edit, bukan
# banyak pesan. Callback menerima satu argumen string (baris log).
_LOG_SINK = None

_real_print = print


def set_log_sink(callback):
    """Pasang/lepas callback log. callback(line:str) atau None untuk melepas."""
    global _LOG_SINK
    _LOG_SINK = callback


def print(*args, **kwargs):  # noqa: A001 — sengaja override print modul ini
    """Override print: tetap tulis ke stdout, lalu teruskan ke log sink bot."""
    try:
        _real_print(*args, **kwargs)
    except Exception:
        pass
    if _LOG_SINK is not None:
        try:
            sep = kwargs.get("sep", " ")
            line = sep.join(str(a) for a in args)
            _LOG_SINK(line)
        except Exception:
            pass


# ==============================================================================
# KONFIGURASI
# ==============================================================================
CONSTANT_PASSWORD = ".ganteng123"
ACCOUNTS_FILE = "accounts.json"
LOGS_DIR = "logs"
CARD_HTML = "cardgenerator.html"

# Proxy WAJIB dipakai untuk rotasi IP (host:port:user:pass).
# Tiap akun bisa pakai IP berbeda lewat rotasi session (DataImpulse) atau
# IP_ROTATION_URL. Isi sesuai langganan proxy kamu.
CONSTANT_PROXY = "niceproxy.io:17521:bhaqi_r5sZ-country-ID-st-west_java-ssid-74yLn182lh:Ganteng123"
IP_ROTATION_URL = ""  # opsional: URL API untuk trigger rotasi IP modem/proxy

# Target geolokasi IP proxy. Sebelum memproses akun, script memverifikasi
# bahwa IP proxy benar-benar berada di region ini. Kalau tidak cocok
# (mis. nyasar ke provinsi lain), proxy dirotasi & dicek ulang.
TARGET_REGION_KEYWORDS = ["west java", "jawa barat", "jabar"]  # cocokkan salah satu
TARGET_COUNTRY = "ID"            # ISO country code yang diharuskan (kosongkan untuk skip)
GEO_CHECK_MAX_ROTATE = 8         # maksimal rotasi proxy saat mencari IP yang cocok
GEO_CHECK_ENABLED = True         # set False untuk lewati pengecekan geo
GEO_RECHECK_DELAY = 4            # detik jeda antar cek (beri waktu proxy ganti IP)

HEADLESS = True  # True = tanpa GUI (lebih cepat, tapi sulit dipantau)

# Path ke Chrome/Chromium di OS. Chromium bawaan Playwright kadang tidak
# terinstall di hosting; arahkan ke Google Chrome sistem. Bisa di-override via
# env var CHROME_PATH atau PLAYWRIGHT_CHROME_PATH.
CHROME_EXECUTABLE_PATH = (
    os.environ.get("CHROME_PATH")
    or os.environ.get("PLAYWRIGHT_CHROME_PATH")
    or "/usr/bin/google-chrome-stable"
)


def _resolve_chrome_path():
    """Kembalikan path executable Chrome bila ada di sistem, selain itu None
    (None = pakai Chromium bawaan Playwright)."""
    candidates = [
        CHROME_EXECUTABLE_PATH,
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def safe_goto(page, url, timeout=60000, retries=3, wait_until="domcontentloaded"):
    """page.goto dengan retry saat renderer crash / timeout.

    - "Page crashed" / timeout  → renderer bisa pulih, coba ulang.
    - "Target closed"           → browser/context sudah mati total; retry sia-sia,
                                   langsung lempar error agar caller bisa hentikan.
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return page.goto(url, timeout=timeout, wait_until=wait_until)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Browser/context sudah tertutup → tidak ada gunanya retry
            if "target closed" in msg or "has been closed" in msg:
                raise
            if "crashed" in msg or "timeout" in msg:
                print(f"   [WARN] goto gagal (cuba {attempt}/{retries}): {str(e)[:80]}")
                time.sleep(3)
                # Reset renderer: arahkan ke about:blank dulu agar proses render
                # yang crash dibuang & diganti yang baru sebelum coba URL asli.
                try:
                    page.goto("about:blank", timeout=15000)
                    time.sleep(1)
                except Exception:
                    # Halaman benar-benar tidak responsif → kemungkinan browser mati
                    raise last_err
                continue
            raise
    if last_err:
        raise last_err


# ==============================================================================
# PERILAKU MANUSIAWI (anti-deteksi otomasi / kurangi risiko suspend)
# ==============================================================================
def human_pause(a=0.6, b=1.8):
    """Jeda acak singkat meniru jeda berpikir manusia."""
    time.sleep(random.uniform(a, b))


def human_type(page, selector, text, click_first=True):
    """Ketik teks karakter-per-karakter dengan delay acak (seperti manusia).

    Jauh lebih aman daripada page.fill() yang mengisi instan — GitHub mendeteksi
    input instan sebagai sinyal bot.
    """
    try:
        if click_first:
            page.click(selector, timeout=8000)
            time.sleep(random.uniform(0.2, 0.5))
    except Exception:
        pass
    # Bersihkan field dulu
    try:
        page.fill(selector, "")
    except Exception:
        pass
    for ch in text:
        try:
            page.type(selector, ch, delay=random.uniform(60, 160))
        except Exception:
            # Fallback: isi sisanya sekaligus bila per-char gagal
            try:
                page.fill(selector, text)
            except Exception:
                pass
            return
        # Sesekali jeda lebih lama seperti orang berhenti mengetik
        if random.random() < 0.08:
            time.sleep(random.uniform(0.3, 0.8))


def human_mouse_wiggle(page):
    """Gerakkan mouse acak sedikit + scroll ringan agar terlihat alami."""
    try:
        for _ in range(random.randint(2, 4)):
            x = random.randint(100, WINDOW_WIDTH - 200)
            y = random.randint(150, WINDOW_HEIGHT - 200)
            page.mouse.move(x, y, steps=random.randint(5, 15))
            time.sleep(random.uniform(0.1, 0.4))
        if random.random() < 0.6:
            page.mouse.wheel(0, random.randint(100, 400))
            time.sleep(random.uniform(0.3, 0.7))
    except Exception:
        pass


# Ukuran jendela browser (jangan full-screen). Geolokasi yang dikirim ke GitHub
# saat klik "Share Location" - default sekitar Bandung, Jawa Barat.
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800
GEO_LATITUDE = -6.9175    # Bandung, Jawa Barat
GEO_LONGITUDE = 107.6191
GEO_ACCURACY = 50         # meter

# Bulk: jeda antar akun (detik)
DELAY_MIN_SEC = 3
DELAY_MAX_SEC = 8

# Monitoring status edu per akun (bisa di-override via env)
# GitHub kadang butuh waktu lama me-review; default tunggu hingga 30 menit.
EDU_WAIT_TIMEOUT_SEC = int(os.environ.get("EDU_WAIT_TIMEOUT_SEC", "1800"))  # 30 menit
EDU_REFRESH_INTERVAL_SEC = int(os.environ.get("EDU_REFRESH_INTERVAL_SEC", "15"))

MAX_ACCOUNTS = 30
MAX_NOT_ON_CAMPUS_RETRY = 3     # batas retry saat form "not on campus" muncul
# Berapa kali rotasi IP + apply ulang saat kena "not on campus" (per akun)
MAX_IP_ROTATE_ON_NOC = int(os.environ.get("MAX_IP_ROTATE_ON_NOC", "3"))


# ==============================================================================
# TOTP / NTP  (murni Python - identik dengan versi lama)
# ==============================================================================
def clean_totp_secret(secret):
    """Bersihkan & beri padding Base32 yang benar untuk TOTP."""
    clean_secret = re.sub(r"[^A-Z2-7]", "", secret.upper().replace(" ", ""))
    pad_len = len(clean_secret) % 8
    if pad_len != 0:
        clean_secret += "=" * (8 - pad_len)
    return clean_secret


_NTP_OFFSET_CACHE = None  # cache offset agar sync NTP hanya sekali per sesi


def get_ntp_time():
    """Ambil waktu akurat dari NTP. Offset di-cache (network call sekali saja)."""
    global _NTP_OFFSET_CACHE
    if _NTP_OFFSET_CACHE is not None:
        return time.time() + _NTP_OFFSET_CACHE
    for host in ("time.google.com", "pool.ntp.org"):
        try:
            import ntplib

            client = ntplib.NTPClient()
            response = client.request(host, version=3, timeout=5)
            _NTP_OFFSET_CACHE = response.tx_time - time.time()
            print(f"   NTP sync ({host}). Offset: {_NTP_OFFSET_CACHE:.2f}s (cached)")
            return response.tx_time
        except Exception:
            continue
    print("   NTP gagal, pakai waktu lokal.")
    _NTP_OFFSET_CACHE = 0.0
    return time.time()


def generate_totp(secret, time_step=30, digits=6):
    """Generate kode TOTP 6 digit (RFC 6238) native, HMAC-SHA1 + waktu NTP."""
    key = base64.b32decode(secret, casefold=True)
    current_time = get_ntp_time()
    time_counter = int(current_time) // time_step
    time_bytes = struct.pack(">Q", time_counter)
    hmac_hash = hmac.new(key, time_bytes, hashlib.sha1).digest()
    offset = hmac_hash[-1] & 0x0F
    truncated = struct.unpack(">I", hmac_hash[offset : offset + 4])[0]
    truncated &= 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


# ==============================================================================
# PROXY HELPERS  (dipertahankan)
# ==============================================================================
def parse_proxy(proxy_str):
    """Parse host:port:user:pass -> dict {host, port, user, pwd} atau None."""
    if not proxy_str:
        return None
    parts = proxy_str.rsplit(":", 3)
    if len(parts) != 4:
        print("   [ERROR] Format proxy salah! Gunakan host:port:user:pass")
        return None
    host, port, user, pwd = parts
    return {"host": host, "port": port, "user": user, "pwd": pwd}


def proxy_to_playwright(proxy_str):
    """Konversi host:port:user:pass -> dict proxy Playwright, atau None."""
    info = parse_proxy(proxy_str)
    if not info:
        return None
    return {
        "server": f"http://{info['host']}:{info['port']}",
        "username": info["user"],
        "password": info["pwd"],
    }


def rotate_dataimpulse_proxy(proxy_str: str) -> str:
    """Rotasi session id DataImpulse agar dapat IP baru."""
    if not proxy_str or "dataimpulse.com" not in proxy_str:
        return proxy_str
    parts = proxy_str.split(":")
    if len(parts) != 4:
        return proxy_str
    host, port, user, pwd = parts
    random_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    if "sessid." in user:
        user = re.sub(r"sessid\.[^;]+", f"sessid.{random_id}", user)
    else:
        user = f"{user};sessid.{random_id}"
    return f"{host}:{port}:{user}:{pwd}"


def trigger_ip_rotation_url(url: str):
    if not url:
        return False
    print(f"   Menghubungi URL rotasi IP: {url}")
    try:
        resp = requests.get(url, timeout=15)
        print(f"   Respon rotasi IP: {resp.status_code} - {resp.text[:100].strip()}")
        return True
    except Exception as e:
        print(f"   Gagal menghubungi URL rotasi IP: {e}")
        return False


# ==============================================================================
# GEO-IP DETECTION (pastikan IP proxy benar-benar di West Java / Jawa Barat)
# ==============================================================================
def _requests_proxies(proxy_str):
    """Bangun dict proxies untuk library requests dari host:port:user:pass."""
    info = parse_proxy(proxy_str)
    if not info:
        return None
    # Username proxy bisa mengandung spasi (mis. '...-st-West Java') -> quote.
    from urllib.parse import quote

    user = quote(info["user"], safe="")
    pwd = quote(info["pwd"], safe="")
    url = f"http://{user}:{pwd}@{info['host']}:{info['port']}"
    return {"http": url, "https": url}


def get_proxy_geo(proxy_str) -> dict:
    """
    Query geolokasi IP LEWAT proxy itu sendiri (bukan IP PC).

    PENTING untuk proxy rotating: tiap pemanggilan WAJIB membuka koneksi TCP
    BARU. Kalau requests memakai ulang koneksi (keep-alive), proxy mengembalikan
    exit IP yang SAMA -> seolah IP tidak berubah. Maka di sini kita:
      - pakai requests.Session baru tiap panggilan
      - set header 'Connection: close'
      - matikan keep-alive di adapter
    Coba beberapa penyedia geo-IP agar tahan kalau salah satu down.
    Returns dict: { ip, country, region, city, source }
    """
    proxies = _requests_proxies(proxy_str) if proxy_str else None

    providers = [
        ("ipwho.is", "https://ipwho.is/",
         lambda j: {
             "ip": j.get("ip", ""),
             "country": j.get("country_code", "") or j.get("country", ""),
             "region": j.get("region", "") or j.get("region_code", ""),
             "city": j.get("city", ""),
         }),
        ("ip-api.com", "http://ip-api.com/json/?fields=status,country,countryCode,regionName,region,city,query",
         lambda j: {
             "ip": j.get("query", ""),
             "country": j.get("countryCode", ""),
             "region": j.get("regionName", "") or j.get("region", ""),
             "city": j.get("city", ""),
         }),
        ("ipapi.co", "https://ipapi.co/json/",
         lambda j: {
             "ip": j.get("ip", ""),
             "country": j.get("country_code", "") or j.get("country", ""),
             "region": j.get("region", ""),
             "city": j.get("city", ""),
         }),
    ]

    headers = {
        "Connection": "close",
        "Cache-Control": "no-cache",
        "User-Agent": "Mozilla/5.0 geo-check",
    }

    for name, url, mapper in providers:
        sess = requests.Session()
        # Matikan reuse koneksi: setiap request -> TCP handshake baru -> IP baru
        try:
            sess.headers.update(headers)
            sess.keep_alive = False
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=1, pool_maxsize=1, max_retries=0
            )
            sess.mount("http://", adapter)
            sess.mount("https://", adapter)
            resp = sess.get(url, proxies=proxies, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if isinstance(data, dict) and data.get("status") == "fail":
                continue
            geo = mapper(data)
            geo["source"] = name
            if geo.get("ip") or geo.get("region") or geo.get("country"):
                return geo
        except Exception:
            continue
        finally:
            try:
                sess.close()
            except Exception:
                pass

    return {"ip": "", "country": "", "region": "", "city": "", "source": ""}


def is_region_match(geo: dict) -> bool:
    """Cek apakah hasil geo sesuai target region (Jawa Barat) & negara (ID)."""
    if not geo:
        return False
    region = (geo.get("region", "") or "").lower()
    city = (geo.get("city", "") or "").lower()
    country = (geo.get("country", "") or "").upper()

    # Negara harus cocok kalau TARGET_COUNTRY diset
    if TARGET_COUNTRY and country and country != TARGET_COUNTRY.upper():
        return False

    haystack = f"{region} {city}"
    # Kota-kota besar Jawa Barat sebagai sinyal tambahan (kadang region kosong,
    # tapi city menunjukkan Bandung/Bekasi/Depok/Bogor dll).
    jabar_cities = [
        "bandung", "bekasi", "bogor", "depok", "cimahi", "cirebon", "sukabumi",
        "tasikmalaya", "garut", "karawang", "purwakarta", "subang", "cianjur",
        "kuningan", "indramayu", "majalengka", "sumedang", "banjar",
    ]
    if any(kw in haystack for kw in TARGET_REGION_KEYWORDS):
        return True
    if any(c in city for c in jabar_cities):
        return True
    return False


def ensure_proxy_in_region(proxy_str):
    """
    Pastikan IP proxy berada di region target (Jawa Barat). Untuk proxy rotating,
    cukup cek ulang beberapa kali karena setiap koneksi baru sudah dapat IP berbeda
    secara otomatis — tidak perlu rotasi manual.

    Returns (proxy_final, ok: bool, geo: dict).
      - proxy_final : string proxy (tidak berubah untuk rotating proxy)
      - ok          : True kalau IP akhirnya cocok region
      - geo         : info geolokasi terakhir yang terdeteksi
    """
    if not GEO_CHECK_ENABLED:
        return proxy_str, True, {}
    if not proxy_str:
        print("   [GEO] Proxy kosong - lewati pengecekan region.")
        return proxy_str, True, {}

    last_geo = {}
    seen_ips = []
    for attempt in range(1, GEO_CHECK_MAX_ROTATE + 1):
        geo = get_proxy_geo(proxy_str)
        last_geo = geo
        loc = f"{geo.get('city', '?')}, {geo.get('region', '?')}, {geo.get('country', '?')}"
        ip  = geo.get("ip", "?")
        src = geo.get("source", "-")

        if is_region_match(geo):
            print(f"   [GEO] OK (cek #{attempt}) IP {ip} -> {loc} (via {src})")
            return proxy_str, True, geo

        # Deteksi kalau IP tidak berubah -> proxy bukan rotating / butuh trigger
        if ip and ip != "?":
            seen_ips.append(ip)

        print(
            f"   [GEO] TIDAK COCOK (cek #{attempt}/{GEO_CHECK_MAX_ROTATE}) "
            f"IP {ip} -> {loc} (via {src})"
        )

        if attempt < GEO_CHECK_MAX_ROTATE:
            # Trigger rotasi eksternal kalau dikonfigurasi (modem/API)
            if IP_ROTATION_URL:
                trigger_ip_rotation_url(IP_ROTATION_URL)

            # Peringatan kalau 3 cek terakhir dapat IP sama -> rotating tidak jalan
            if len(seen_ips) >= 3 and len(set(seen_ips[-3:])) == 1:
                print(
                    "   [GEO] PERINGATAN: IP tidak berubah 3x berturut-turut. "
                    "Pastikan endpoint proxy memang tipe ROTATING (bukan sticky), "
                    "atau set IP_ROTATION_URL."
                )

            print("   [GEO] Tunggu IP baru (koneksi baru)...")
            time.sleep(GEO_RECHECK_DELAY)

    print(f"   [GEO] GAGAL menemukan IP Jawa Barat setelah {GEO_CHECK_MAX_ROTATE} percobaan.")
    return proxy_str, False, last_geo


# ==============================================================================
# IDENTITAS BROWSER UNIK PER AKUN
# ==============================================================================
# Setiap akun dapat profil fingerprint berbeda supaya GitHub melihat tiap sesi
# sebagai perangkat/browser baru. Semua nilai dipilih acak dari pool realistis.

_CHROME_MAJORS = [122, 123, 124, 125, 126, 127, 128, 129, 130]
_WIN_VERSIONS = ["10.0", "10.0"]  # Windows NT 10.0 (Win10/11 sama-sama "10.0")

_VIEWPORTS = [
    (1920, 1080), (1600, 900), (1536, 864), (1440, 900),
    (1366, 768), (1680, 1050), (1280, 720), (2560, 1440),
]
_TIMEZONES = [
    "Asia/Jakarta", "Asia/Makassar", "Asia/Pontianak", "Asia/Jayapura",
]
_LOCALES = ["en-US", "en-GB", "id-ID"]
_HW_CONCURRENCY = [4, 6, 8, 12, 16]
_DEVICE_MEMORY = [4, 8, 8, 16]
_WEBGL_VENDORS = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]


def build_identity() -> dict:
    """Bangun satu set identitas browser acak (fingerprint baru)."""
    major = random.choice(_CHROME_MAJORS)
    win = random.choice(_WIN_VERSIONS)
    ua = (
        f"Mozilla/5.0 (Windows NT {win}; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )
    vw, vh = random.choice(_VIEWPORTS)
    vendor, renderer = random.choice(_WEBGL_VENDORS)
    return {
        "user_agent": ua,
        "screen": {"width": vw, "height": vh},
        "timezone": random.choice(_TIMEZONES),
        "locale": random.choice(_LOCALES),
        "hardware_concurrency": random.choice(_HW_CONCURRENCY),
        "device_memory": random.choice(_DEVICE_MEMORY),
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
    }


def _identity_init_script(idn: dict) -> str:
    """
    JS yang di-inject SEBELUM halaman load untuk menyamarkan properti fingerprint.
    Hanya menyentuh properti yang benar-benar relevan:
      - navigator.webdriver      → critical untuk hindari deteksi otomasi
      - hardwareConcurrency / deviceMemory / platform → variasi antar sesi
      - WebGL vendor & renderer  → beda per "mesin" virtual
    Canvas noise TIDAK di-inject: GitHub edu tidak melakukan canvas fingerprinting,
    dan injeksi tersebut men-mutasi pixel canvas secara in-place (bisa korupsi
    gambar ID card).
    """
    return f"""
(() => {{
  const HW = {idn['hardware_concurrency']};
  const MEM = {idn['device_memory']};
  const GL_VENDOR = {json.dumps(idn['webgl_vendor'])};
  const GL_RENDERER = {json.dumps(idn['webgl_renderer'])};

  // Critical: sembunyikan tanda otomasi
  try {{ Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }}); }} catch(e){{}}

  // Variasi hardware per sesi
  try {{ Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => HW }}); }} catch(e){{}}
  try {{ Object.defineProperty(navigator, 'deviceMemory', {{ get: () => MEM }}); }} catch(e){{}}
  try {{ Object.defineProperty(navigator, 'platform', {{ get: () => 'Win32' }}); }} catch(e){{}}

  // WebGL vendor & renderer
  try {{
    const patch = (proto) => {{
      const gp = proto.getParameter;
      proto.getParameter = function(p) {{
        if (p === 37445) return GL_VENDOR;
        if (p === 37446) return GL_RENDERER;
        return gp.call(this, p);
      }};
    }};
    if (window.WebGLRenderingContext) patch(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
  }} catch(e){{}}
}})();
"""


# ==============================================================================
# EDUCATION BENEFITS STATUS CHECKER
# ==============================================================================
def _extract_rejection_reason(raw_text: str, low: str = None) -> str:
    """Ekstrak ALASAN penolakan dari teks halaman benefits.

    GitHub menampilkan alasan spesifik saat aplikasi ditolak (mis. masalah
    billing/pembayaran, dokumen tidak jelas, nama tidak cocok, akun terlalu baru,
    dll). Fungsi ini mencocokkan pola alasan umum + mengambil kalimat di sekitar
    kata 'declined/rejected' agar admin tahu apa yang perlu diperbaiki.
    """
    if low is None:
        low = re.sub(r"\s+", " ", (raw_text or "").lower()).strip()
    if not low:
        return ""

    # Pola alasan umum → pesan ringkas Bahasa Indonesia
    reason_patterns = [
        (("billing", "payment", "verified payment", "add a payment",
          "payment method", "credit card"),
         "Masalah BILLING/pembayaran — akun GitHub perlu metode pembayaran "
         "terverifikasi atau ada masalah tagihan."),
        (("could not read", "couldn't read", "image is blurry", "blurry",
          "not legible", "illegible", "unable to read", "document is not clear",
          "couldn't verify the document", "image quality"),
         "Dokumen/ID card tidak terbaca jelas (blur / kualitas gambar rendah)."),
        (("name does not match", "name doesn't match", "name on your",
          "names do not match", "doesn't match the name"),
         "Nama di ID card tidak cocok dengan nama akun GitHub."),
        (("not affiliated", "could not confirm your affiliation",
          "academic affiliation", "not a student", "enrollment"),
         "GitHub tidak bisa mengonfirmasi status pelajar/afiliasi akademik."),
        (("expired", "out of date", "not current", "date on"),
         "Tanggal pada dokumen kedaluwarsa / tidak berlaku saat ini."),
        (("too new", "account was created", "recently created",
          "account age"),
         "Akun GitHub terlalu baru — tunggu beberapa hari sebelum apply."),
        (("suspicious", "automated", "unusual activity", "fraud", "abuse",
          "violat"),
         "Aktivitas dianggap mencurigakan/otomatis oleh GitHub (risiko suspend)."),
        (("not on campus", "far from campus", "location"),
         "Lokasi IP tidak cocok dengan kampus (perlu rotasi IP / proxy region tepat)."),
        (("vpn", "proxy"),
         "GitHub mendeteksi VPN/proxy."),
    ]

    matched = []
    for keys, msg in reason_patterns:
        if any(k in low for k in keys):
            matched.append(msg)

    # Ambil juga kalimat asli di sekitar 'declined'/'rejected'/'because'/'reason'
    snippet = ""
    norm = re.sub(r"\s+", " ", (raw_text or "")).strip()
    for anchor in ("because", "reason", "declined", "rejected", "unable to",
                   "could not", "couldn't"):
        idx = norm.lower().find(anchor)
        if idx != -1:
            snippet = norm[idx: idx + 220].strip()
            break

    parts = []
    if matched:
        # Hilangkan duplikat sambil jaga urutan
        seen = set()
        for m in matched:
            if m not in seen:
                seen.add(m)
                parts.append("• " + m)
    if snippet:
        parts.append(f"📄 Pesan GitHub: \"{snippet}\"")

    if not parts:
        return "Alasan tidak terdeteksi otomatis — cek detail di halaman benefits."
    return "\n".join(parts)


def _classify_edu_status(page_text: str) -> dict:
    """
    Klasifikasi teks halaman benefits -> verified / pending / declined / unknown.
    Whitespace dinormalisasi dulu agar frasa multi-baris tetap cocok.

    Hanya sinyal yang BENAR-BENAR unik untuk setiap status yang dipakai.
    Sinyal ambigu (100%/done, congratulations sendiri, dll) TIDAK dipakai
    agar tidak false positive.
    """
    raw = page_text or ""
    out = {"status": "unknown", "message": "", "raw_text": raw[:800]}
    low = re.sub(r"\s+", " ", raw.lower()).strip()
    if not low:
        out["message"] = "halaman kosong / belum ter-render"
        return out

    # --- 1. PENDING / DECLINED dicek PALING DULU (prioritas tertinggi) ---
    # Halaman benefits menampilkan teks marketing ("Student Developer Pack",
    # "GitHub Global Campus", "Get Copilot for free") WALAU status masih pending.
    # Jadi penanda pending/declined harus menang lebih dulu agar tidak
    # false-positive "verified".
    pending_signals = [
        "pending review",
        "currently pending review",
        "pending application",
        "current pending application",
        "under review",
        "in review",
        "we are reviewing",
        "we'll let you know",
        "we will let you know",
        "thanks for applying",
        "we have received your application",
        "your application has been received",
        "application is being reviewed",
        "verification pending",
        "is pending",
    ]
    if any(kw in low for kw in pending_signals):
        out["status"] = "pending"
        out["message"] = "masih pending review"
        return out

    declined_signals = [
        "declined", "rejected", "denied", "not eligible",
        "we couldn't verify", "we could not verify",
        "could not be verified", "application was rejected",
        "unable to verify", "couldn't verify", "not verified",
    ]
    if any(kw in low for kw in declined_signals):
        out["status"] = "declined"
        out["message"] = "ditolak GitHub"
        out["reason"] = _extract_rejection_reason(raw, low)
        return out

    # --- 2. VERIFIED ---
    # Sampai di sini PASTI tidak ada penanda pending/declined (sudah return di
    # atas). Jadi sinyal "benefit aktif" di bawah AMAN dipakai — frasa seperti
    # "student developer pack" / "global campus" / "get copilot" hanya muncul
    # sebagai konten benefit yang sudah aktif bila tidak ada teks pending.
    verified_strong = [
        # Badge eksplisit
        "verified (benefits available)",
        "verified, benefits available",
        "verified benefits available",
        "benefits available",
        # Pernyataan kepemilikan
        "you're a verified student",
        "you are a verified student",
        "you've been verified",
        "you have been verified",
        "your account has been verified",
        "your academic affiliation has been verified",
        "academic affiliation approved",
        # Konten/benefit yang hanya tampil setelah approved
        "student developer pack",
        "github global campus",
        "global campus",
        "redeem copilot",
        "get copilot for free",
        "coupon applied",
    ]
    if any(sig in low for sig in verified_strong):
        out["status"] = "verified"
        out["message"] = "akun terverifikasi - student benefits aktif"
        return out

    # --- 3. COMBO verified (cadangan, bila frasa terpecah baris) ---
    verified_combos = [
        ("congratulations", "your academic benefits", "are now available"),
        ("your campus", "developer pack"),
    ]
    if any(all(p in low for p in combo) for combo in verified_combos):
        out["status"] = "verified"
        out["message"] = "akun terverifikasi - student benefits aktif"
        return out

    out["status"] = "unknown"
    out["message"] = "tidak bisa parse status (cek raw_text di edu_status_debug.txt)"
    return out


# JS untuk membaca teks menembus Shadow DOM (badge status GitHub pakai web-component)
_JS_DEEP_TEXT = r"""
() => {
    const out = [];
    try { if (document.body && document.body.innerText) out.push(document.body.innerText); } catch(e){}
    const walk = (root) => {
        let nodes;
        try { nodes = root.querySelectorAll('*'); } catch(e){ return; }
        for (const el of nodes) {
            if (el.shadowRoot) {
                try { const t = el.shadowRoot.textContent; if (t) out.push(t); } catch(e){}
                walk(el.shadowRoot);
            }
        }
    };
    try { walk(document); } catch(e){}
    try {
        document.querySelectorAll('[aria-label],img[alt],[title]').forEach(e => {
            const a = e.getAttribute('aria-label') || e.getAttribute('alt') || e.getAttribute('title');
            if (a) out.push(a);
        });
    } catch(e){}
    return out.join('\n');
}
"""


def _read_benefits_text(page) -> str:
    """Ambil teks halaman benefits (deep text + shadow DOM). Retry sampai kata kunci muncul."""
    decisive = (
        "benefits available", "pending", "review", "declined", "rejected",
        "verified", "developer pack", "not eligible", "coupon applied",
        "student developer pack", "global campus", "expires", "academic",
    )
    last = ""
    for _ in range(5):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        txt = ""
        try:
            txt = (page.evaluate(_JS_DEEP_TEXT) or "").strip()
        except Exception:
            pass
        if not txt:
            try:
                txt = (page.inner_text("body") or "").strip()
            except Exception:
                pass
        last = txt
        if last and any(k in last.lower() for k in decisive):
            return last
        time.sleep(1.0)

    # Diagnostik: dump teks + HTML mentah
    try:
        _dir = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(_dir, "edu_status_debug.txt"), "w", encoding="utf-8") as f:
            f.write("URL: " + (page.url or "") + "\n\n=== DEEP TEXT ===\n" + (last or "<KOSONG>"))
        with open(os.path.join(_dir, "edu_status_debug.html"), "w", encoding="utf-8") as f:
            f.write(page.content() or "")
        print("   [DEBUG] Teks status tak dikenali -> dump ke edu_status_debug.txt/.html")
    except Exception:
        pass
    return last


def check_education_status(page) -> dict:
    """Navigate ke benefits + klasifikasi. Returns {status, message, raw_text}."""
    out = {"status": "unknown", "message": "", "raw_text": ""}
    try:
        safe_goto(page, "https://github.com/settings/education/benefits", timeout=60000)
    except Exception as e:
        out["message"] = f"navigasi gagal: {e}"
        return out
    txt = _read_benefits_text(page)
    cls = _classify_edu_status(txt)
    out.update(cls)
    return out


def monitor_education_status_with_refresh(
    page, username, total_timeout=EDU_WAIT_TIMEOUT_SEC,
    refresh_interval=EDU_REFRESH_INTERVAL_SEC,
) -> dict:
    """
    Refresh halaman benefits tiap N detik sampai status final (verified/declined)
    atau timeout. Berhenti pada transisi pertama ke verified/declined.
    """
    print(f"   Monitor status edu (refresh {refresh_interval}s, timeout {total_timeout}s)...")
    deadline = time.time() + total_timeout
    icon = {"verified": "[OK]", "pending": "[..]", "declined": "[X]", "unknown": "[?]"}

    try:
        safe_goto(page, "https://github.com/settings/education/benefits", timeout=60000)
    except Exception as e:
        return {"status": "unknown", "message": f"navigasi awal gagal: {e}", "raw_text": ""}

    cls = _classify_edu_status(_read_benefits_text(page))
    last = cls["status"]
    print(f"   {icon.get(last, '[?]')} status awal: {last.upper()} - {cls['message']}")
    if last in ("verified", "declined"):
        return cls

    while time.time() < deadline:
        sisa = int(deadline - time.time())
        wait_s = min(refresh_interval, sisa)
        if wait_s <= 0:
            break
        time.sleep(wait_s)
        try:
            safe_goto(page, "https://github.com/settings/education/benefits", timeout=60000)
            txt = _read_benefits_text(page)
        except Exception as e:
            print(f"   refresh gagal: {str(e)[:60]}")
            continue
        cls = _classify_edu_status(txt)
        if cls["status"] != last:
            print(f"   {icon.get(cls['status'], '[?]')} status berubah: "
                  f"{last.upper()} -> {cls['status'].upper()} ({cls['message']})")
            last = cls["status"]
        if cls["status"] in ("verified", "declined"):
            return cls

    print(f"   Monitor selesai tanpa transisi final (status terakhir: {last})")
    if cls.get("status") == "pending":
        cls["message"] = (
            "masih pending review setelah monitoring — GitHub belum selesai "
            "memverifikasi. Cek lagi nanti di halaman benefits."
        )
    return cls


# ==============================================================================
# DATA SEKOLAH & REGION
# ==============================================================================
SCHOOLS = [
    "Depok State Senior High School 5", "Depok State Junior High School 4",
    "Depok State Senior High School 12", "Depok State Junior High School 17",
    "State Vocational School 3 Depok", "Public Senior High School 9 Depok",
    "State Junior High School 10 Depok", "State Senior High School 6 Depok",
    "SMK Negeri 1 Depok", "SMA Negeri 8 Depok", "SMA Negeri 4 Depok",
    "SMA Negeri 1 Depok", "SMA Negeri 7 Depok", "SMA Negeri 3 Depok",
    "SMA Negeri 13 Depok", "SMK Negeri 2 Kota Depok",
    "Bekasi State Junior High School 24", "Bekasi State Junior High School 1",
    "Bekasi State Senior High School 13", "Bekasi State Senior High School 8",
    "Bekasi State Vocational School 1", "Bekasi State Vocational School 4",
    "Bekasi State Vocational School 10", "Bekasi State Junior High School 26",
    "Bekasi State Junior High School 4", "Bekasi State Vocational High School 5",
    "Bekasi State Junior High School 35", "Bekasi State Senior High School 1",
    "Bekasi State Junior High School 21", "Junior High School 31 Bekasi City",
    "State Vocational High School 3 Bekasi City",
    "State Vocational High School 2 Bekasi City",
    "Bogor State Senior High School 10", "Bogor State Junior High School 6",
    "Bogor State Vocational High School 2", "Bogor State Islamic High School 1",
    "Bogor State Islamic Senior High School 1", "Senior High School 7 Bogor",
    "State Senior High School 9 Bogor",
]

def derive_student_name(username: str, first_name: str = "") -> str:
    """Derivasi nama untuk ID card dari username CamelCase atau first_name."""
    first_name = first_name.strip().upper() if first_name else ""
    if first_name and " " in first_name:
        return first_name
    _clean = re.sub(r"[0-9]+", "", username)
    _clean = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", _clean)
    name = _clean.replace(".", " ").replace("_", " ").strip().upper()
    if first_name and first_name in name and " " not in name:
        name = name.replace(first_name, first_name + " ", 1).strip()
    return name or "STUDENT"


# ==============================================================================
# WEBRTC CAMERA HIJACK (Logitech C922 + hand-tremor + CMOS noise)
# ==============================================================================
def build_camera_hijack_js(img_data_url: str) -> str:
    """Bangun script spoof getUserMedia yang menyajikan ID card sebagai video kamera."""
    return f"""
(function() {{
    if (window.__BHAQI_HIJACK_INSTALLED) return;
    window.__BHAQI_HIJACK_INSTALLED = true;

    const CAMERA_LABEL = 'Webcam Logitech C922 Pro HD';
    const DEVICE_ID    = 'c922pro0001logitech0001bhaqi';
    const GROUP_ID     = 'c922pro0001group0001bhaqi';
    const _dataURL     = '{img_data_url}';

    if (typeof window.__bhaqi_cachedStream === 'undefined') {{
        window.__bhaqi_cachedStream = null;
    }}

    function spoofTrack(track) {{
        Object.defineProperty(track, 'label', {{ get: function() {{ return CAMERA_LABEL; }}, configurable: true }});
        Object.defineProperty(track, 'readyState', {{ get: function() {{ return 'live'; }}, configurable: true }});
        track.stop = function() {{ console.log('[BHAQI] track.stop() dicegah'); }};
        track.getSettings = function() {{
            var w = window.__bhaqi_streamW || 600;
            var h = window.__bhaqi_streamH || 380;
            return {{ deviceId: DEVICE_ID, groupId: GROUP_ID, width: w, height: h, frameRate: 30, aspectRatio: parseFloat((w/h).toFixed(4)), facingMode: 'user', resizeMode: 'none' }};
        }};
        track.getCapabilities = function() {{
            return {{ deviceId: DEVICE_ID, groupId: GROUP_ID, width: {{min:1,max:1920}}, height: {{min:1,max:1080}}, frameRate: {{min:1,max:60}}, aspectRatio: {{min:0.01,max:100}}, facingMode: ['user'], resizeMode: ['none','crop-and-scale'] }};
        }};
        track.getConstraints = function() {{ return {{ video: true }}; }};
        return track;
    }}

    function buildFakeStream() {{
        return new Promise(function(resolve, reject) {{
            if (window.__bhaqi_cachedStream && window.__bhaqi_cachedStream.active) {{
                return resolve(window.__bhaqi_cachedStream);
            }}
            var img = new Image();
            img.onload = function() {{
                var iw = img.naturalWidth  || 600;
                var ih = img.naturalHeight || 380;
                iw = (iw % 2 === 0) ? iw : iw + 1;
                ih = (ih % 2 === 0) ? ih : ih + 1;
                var canvas = document.createElement('canvas');
                canvas.width = iw; canvas.height = ih;
                var ctx = canvas.getContext('2d');
                var _t0 = Date.now();
                function drawFrame() {{
                    // Hanya hand-tremor (translate ringan). Per-pixel CMOS noise
                    // via getImageData/putImageData DIHAPUS karena sangat berat di
                    // software rendering (server headless) dan bikin renderer crash.
                    var t = Date.now() - _t0;
                    var jx = Math.sin(t/870)*1.6 + Math.sin(t/230)*0.4 + (Math.random()-0.5)*0.7;
                    var jy = Math.cos(t/1130)*1.3 + Math.cos(t/310)*0.35 + (Math.random()-0.5)*0.5;
                    ctx.clearRect(0, 0, iw, ih);
                    ctx.drawImage(img, jx|0, jy|0, iw, ih);
                }}
                drawFrame();
                // 10fps cukup untuk terlihat hidup tapi jauh lebih ringan
                var _t = setInterval(drawFrame, 100);
                window.__bhaqi_drawTimer = _t;
                window.__bhaqi_streamW = iw;
                window.__bhaqi_streamH = ih;
                var stream = canvas.captureStream(15);
                stream.getVideoTracks().forEach(spoofTrack);
                window.__bhaqi_cachedStream = stream;
                console.log('[BHAQI] Stream OK ' + iw + 'x' + ih);
                resolve(stream);
            }};
            img.onerror = function() {{ reject(new Error('Gagal load gambar ID card')); }};
            img.src = _dataURL;
        }});
    }}

    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {{
        var _origEnum = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
        navigator.mediaDevices.enumerateDevices = function() {{
            return _origEnum().then(function(devices) {{
                var hasVideo = devices.some(function(d) {{ return d.kind === 'videoinput'; }});
                if (!hasVideo) {{
                    devices.push({{ deviceId: DEVICE_ID, groupId: GROUP_ID, kind: 'videoinput', label: CAMERA_LABEL,
                        toJSON: function() {{ return {{ deviceId: DEVICE_ID, groupId: GROUP_ID, kind: 'videoinput', label: CAMERA_LABEL }}; }} }});
                }}
                return devices;
            }});
        }};
    }}

    if (navigator.mediaDevices) {{
        var _origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getUserMedia = function(constraints) {{
            if (constraints && constraints.video) {{
                return buildFakeStream();
            }}
            return _origGUM ? _origGUM(constraints) : Promise.reject('no camera');
        }};
    }}

    var _origTrackStop = MediaStreamTrack.prototype.stop;
    MediaStreamTrack.prototype.stop = function() {{
        if (this.label === CAMERA_LABEL) {{ return; }}
        return _origTrackStop.call(this);
    }};
    console.log('[BHAQI_CORE] Fake camera + stream guard terpasang.');
}})();
"""


# ==============================================================================
# ID CARD RENDER & CAPTURE (cardgenerator.html)
# ==============================================================================
def render_id_card(page, cdp, school_name, student_name, user_address, avatar_url):
    """
    Buka cardgenerator.html, isi data, lalu capture sebagai PNG data URL via
    isolated viewport (CDP). Returns data URL string atau None.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(script_dir, CARD_HTML)
    if not os.path.exists(html_path):
        raise FileNotFoundError(f"{CARD_HTML} tidak ditemukan: {html_path}")

    file_url = "file:///" + html_path.replace("\\", "/")
    page.goto(file_url, timeout=60000)
    page.wait_for_selector("#idCard", timeout=8000)

    # Isi field + trigger 'input' agar preview update
    page.evaluate(
        """([school, name, addr]) => {
            const s = document.getElementById('inputSchool');
            s.value = school; s.dispatchEvent(new Event('input'));
            const n = document.getElementById('inputName');
            n.value = name; n.dispatchEvent(new Event('input'));
            const a = document.getElementById('inputAddress');
            a.value = addr; a.dispatchEvent(new Event('input'));
        }""",
        [school_name, student_name, user_address],
    )

    # Set avatar (tunggu load) jika ada
    if avatar_url:
        try:
            page.evaluate(
                """(url) => new Promise((resolve) => {
                    const img = document.getElementById('renderPhoto');
                    img.setAttribute('crossOrigin', 'anonymous');
                    img.onload = () => resolve();
                    img.onerror = () => resolve();
                    img.src = url;
                })""",
                avatar_url,
            )
        except Exception:
            pass

    # Re-roll student id & DOB
    try:
        page.evaluate("document.getElementById('btnGenerateSys').click();")
        # Tunggu barcode/DOB di-re-roll (element presence berubah)
        page.wait_for_function(
            "() => document.getElementById('idCard') !== null", timeout=5000
        )
    except Exception:
        pass

    # Tunggu SEMUA aset render selesai agar kartu tidak "berantakan":
    #   - fonts (Inter / Times / Libre Barcode) lewat document.fonts.ready
    #   - semua <img> (logo sekolah, barcode, foto) selesai load
    try:
        page.evaluate(
            """() => new Promise((resolve) => {
                const done = () => {
                    const imgs = Array.from(document.images || []);
                    const pending = imgs.filter(im => !im.complete || im.naturalWidth === 0);
                    let waitImgs = Promise.resolve();
                    if (pending.length) {
                        waitImgs = Promise.all(pending.map(im => new Promise(r => {
                            im.onload = r; im.onerror = r;
                            setTimeout(r, 4000); // jangan menggantung
                        })));
                    }
                    const waitFonts = (document.fonts && document.fonts.ready)
                        ? document.fonts.ready : Promise.resolve();
                    Promise.all([waitImgs, waitFonts]).then(resolve);
                };
                done();
            })"""
        )
    except Exception:
        pass
    # Beri waktu ekstra untuk layout settle (barcode SVG + font metrics)
    time.sleep(1.2)

    img_data_url = None
    try:
        print("   Capture ID card (isolated viewport)...")
        # Klon #idCard ke body bersih
        page.evaluate("""() => {
            var card = document.getElementById('idCard');
            var clone = card.cloneNode(true);
            clone.style.transform = 'none';
            clone.style.transformOrigin = '';
            clone.style.margin = '0';
            clone.style.borderRadius = '0';
            document.body.style.cssText = 'margin:0;padding:0;background:transparent;overflow:hidden;';
            document.body.innerHTML = '';
            document.body.appendChild(clone);
        }""")
        # Viewport = ukuran fisik kartu, 2x scale
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": 600, "height": 380, "deviceScaleFactor": 2, "mobile": False,
        })
        time.sleep(0.5)
        shot = cdp.send("Page.captureScreenshot", {
            "format": "png", "captureBeyondViewport": False,
        })
        img_data_url = "data:image/png;base64," + shot["data"]
        print(f"   Captured 1200x760 @2x ({len(shot['data'])} chars)")
    except Exception as e_iso:
        print(f"   Isolated viewport gagal: {e_iso}. Fallback html2canvas...")
        try:
            img_data_url = page.evaluate("""() => new Promise((cb) => {
                var el = document.querySelector('body > .id-card-container') ||
                         document.getElementById('idCard') ||
                         document.body.firstElementChild;
                if (!el || typeof html2canvas === 'undefined') { cb('error_no_el'); return; }
                html2canvas(el, { scale: 2, useCORS: true, backgroundColor: null, logging: false })
                    .then(c => cb(c.toDataURL('image/png')))
                    .catch(e => cb('error_' + e.message));
            })""")
            if not img_data_url or "error" in str(img_data_url):
                raise Exception(str(img_data_url))
            print("   Captured via html2canvas (fallback)")
        except Exception as e_h2c:
            print(f"   Semua metode capture gagal: {e_h2c}")
            img_data_url = None
    finally:
        try:
            cdp.send("Emulation.clearDeviceMetricsOverride", {})
        except Exception:
            pass

    return img_data_url


# ==============================================================================
# DETEKSI "NOT ON CAMPUS"
# ==============================================================================
_NOT_ON_CAMPUS_JS = """
() => {
    var body = document.body;
    if (!body) return false;
    var bodyText = (body.innerText || body.textContent || '').toLowerCase();
    var TEXT_KEYWORDS = [
        'far from campus', 'so far from campus', 'far away from campus',
        'you are so far', 'why are you not on campus',
        'please upload an image to help us understand',
        'not on campus', 'why you are not'
    ];
    for (var i = 0; i < TEXT_KEYWORDS.length; i++) {
        if (bodyText.indexOf(TEXT_KEYWORDS[i]) !== -1) return true;
    }
    var radios = document.querySelectorAll('input[type="radio"]');
    for (var j = 0; j < radios.length; j++) {
        var parent = radios[j].closest('label, div, span');
        if (parent) {
            var labelText = (parent.innerText || '').toLowerCase();
            if (labelText.indexOf('distance learning') !== -1 ||
                labelText.indexOf('not yet started') !== -1 ||
                labelText.indexOf('using a vpn') !== -1 ||
                labelText.indexOf('remote') !== -1) { return true; }
        }
    }
    var alerts = document.querySelectorAll('.flash-error, .flash-warn, [role="alert"]');
    for (var k = 0; k < alerts.length; k++) {
        var alertText = (alerts[k].innerText || '').toLowerCase();
        if (alertText.indexOf('far') !== -1 && alertText.indexOf('campus') !== -1) return true;
        if (alertText.indexOf('cannot be reviewed') !== -1) return true;
    }
    return false;
}
"""

_FORM_STILL_EXISTS_JS = """
() => {
    var body = document.body.innerText || document.body.textContent || '';
    return body.indexOf('What is a valid proof of education?') !== -1 ||
           document.querySelector('button[class*="WebcamUpload-module__Button"]') !== null;
}
"""


# ==============================================================================
# MAIN ENGINE (PLAYWRIGHT)
# ==============================================================================
def run_account(
    username, secret_key, first_name="", proxy_input="",
    password=None, interactive=True,
):
    """
    Proses satu akun penuh dengan ROTASI IP saat 'not on campus'.

    Bila form GitHub menolak dengan 'not on campus' (lokasi IP dianggap jauh dari
    kampus), browser ditutup, IP proxy dirotasi (DataImpulse session / IP_ROTATION_URL),
    region diverifikasi ulang (Jawa Barat), lalu apply diulang dengan IP baru.
    Maksimal MAX_IP_ROTATE_ON_NOC kali.

    Returns dict: {status, message, edu_status, edu_message}
    """
    current_proxy = proxy_input
    last_result = {
        "status": "failed", "message": "", "edu_status": None, "edu_message": ""
    }

    for noc_attempt in range(MAX_IP_ROTATE_ON_NOC + 1):
        if noc_attempt > 0:
            print(
                f"\n=== ROTASI IP #{noc_attempt}/{MAX_IP_ROTATE_ON_NOC} "
                f"(akibat 'not on campus') ==="
            )
            # Rotasi session DataImpulse → IP baru
            if current_proxy and "dataimpulse.com" in current_proxy:
                current_proxy = rotate_dataimpulse_proxy(current_proxy)
                print("   Session proxy DataImpulse dirotasi.")
            # Trigger rotasi eksternal bila dikonfigurasi (modem/API)
            if IP_ROTATION_URL:
                trigger_ip_rotation_url(IP_ROTATION_URL)
                time.sleep(5)
            # Pastikan IP baru benar-benar di Jawa Barat sebelum apply ulang
            if current_proxy and GEO_CHECK_ENABLED:
                current_proxy, geo_ok, geo = ensure_proxy_in_region(current_proxy)
                if geo_ok:
                    print(
                        f"   IP baru OK: {geo.get('city','?')}, {geo.get('region','?')}"
                    )
                else:
                    print(
                        f"   [WARN] IP baru bukan Jawa Barat "
                        f"({geo.get('region','?')}/{geo.get('city','?')}), tetap dicoba."
                    )

        result = _run_account_once(
            username, secret_key, first_name, current_proxy,
            password, interactive,
        )
        last_result = result

        # Hanya ulang (rotasi IP) kalau alasannya 'not on campus'
        if result.get("status") == "not_on_campus":
            # Tanpa proxy, rotasi IP tidak mungkin → langsung gagal
            if not current_proxy:
                print("   'Not on campus' tapi tanpa proxy — tidak bisa rotasi IP.")
                result["status"] = "failed"
                result["message"] = (
                    "Ditolak: not on campus. Tidak ada proxy untuk rotasi IP "
                    "(set EDU_PROXY / CONSTANT_PROXY)."
                )
                return result
            if noc_attempt < MAX_IP_ROTATE_ON_NOC:
                print("   'Not on campus' → akan rotasi IP & apply ulang...")
                continue
            print("   Batas rotasi IP 'not on campus' tercapai.")
            result["status"] = "failed"
            result["message"] = "Ditolak: not on campus (rotasi IP habis)"
            return result

        # Status lain (success / failed / dll) → selesai
        return result

    return last_result


def _run_account_once(
    username, secret_key, first_name="", proxy_input="",
    password=None, interactive=True,
):
    """
    Satu kali percobaan apply (1 browser baru): login -> 2FA -> parse profil ->
    render ID card -> isi form edu -> spoof kamera -> submit -> deteksi not-on-campus.

    Returns dict: {status, message, edu_status, edu_message}.
      status == "not_on_campus" → caller (run_account) akan rotasi IP & ulang.
    """
    result = {"status": "failed", "message": "", "edu_status": None, "edu_message": ""}
    use_password = password if password else CONSTANT_PASSWORD

    school_name = random.choice(SCHOOLS)
    student_name = derive_student_name(username, first_name)

    print(f"\n=== Parameter terpilih ===")
    print(f"  Sekolah     : {school_name}")
    print(f"  Nama IDCard : {student_name}")

    identity = build_identity()
    scr = identity["screen"]
    print(f"  Identitas   : {identity['user_agent'][40:75]}… | "
          f"{scr['width']}x{scr['height']} | "
          f"{identity['timezone']} | {identity['locale']} | "
          f"{identity['hardware_concurrency']}core/{identity['device_memory']}GB")

    pw = None
    browser = None
    context = None
    page = None

    try:
        pw = sync_playwright().start()
        if _USING_PATCHRIGHT:
            print("   🛡️ Engine: Patchright (anti-deteksi aktif)")
        else:
            print("   Engine: Playwright biasa (patchright tidak terpasang)")

        # Flag dasar. Catatan: '--disable-blink-features=AutomationControlled'
        # SENGAJA TIDAK dipakai saat patchright aktif karena flag itu justru
        # menjadi 'tell' bot; patchright menangani penyamaran sendiri.
        base_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            # /dev/shm sering hanya 64MB di container → paksa pakai /tmp
            "--disable-dev-shm-usage",
            # GPU: software rendering stabil di server headless.
            "--disable-gpu",
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--in-process-gpu",
            "--disable-software-rasterizer",
            # Kurangi beban background yang tidak perlu
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=Translate,IsolateOrigins,site-per-process",
            "--js-flags=--max-old-space-size=512",
            f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
            "--window-position=40,30",
        ]
        if not _USING_PATCHRIGHT:
            # Playwright biasa: butuh flag ini untuk menyembunyikan otomasi
            base_args.insert(0, "--disable-blink-features=AutomationControlled")
            base_args.append("--disable-extensions")

        launch_args = {"headless": HEADLESS, "args": base_args}
        # Pakai Chrome sistem bila Chromium bawaan Playwright tidak tersedia
        chrome_path = _resolve_chrome_path()
        if chrome_path:
            launch_args["executable_path"] = chrome_path
            print(f"   Memakai Chrome sistem: {chrome_path}")
        pw_proxy = proxy_to_playwright(proxy_input) if proxy_input else None
        if pw_proxy:
            launch_args["proxy"] = pw_proxy

        browser = pw.chromium.launch(**launch_args)

        # Context BARU = cookies/storage kosong + identitas unik.
        # viewport=None -> ikut ukuran window (tidak full-screen).
        context = browser.new_context(
            user_agent=identity["user_agent"],
            no_viewport=True,
            screen=identity["screen"],
            locale=identity["locale"],
            timezone_id=identity["timezone"],
            permissions=["camera", "microphone", "geolocation"],
            geolocation={
                "latitude": GEO_LATITUDE,
                "longitude": GEO_LONGITUDE,
                "accuracy": GEO_ACCURACY,
            },
            is_mobile=False,
            has_touch=False,
        )
        # Inject spoof fingerprint sebelum halaman apa pun load
        context.add_init_script(_identity_init_script(identity))
        # Grant izin kamera/mic/geo untuk github.com (geolocation perlu agar
        # 'Share Location' tidak menggantung menunggu izin/koordinat)
        try:
            context.grant_permissions(
                ["camera", "microphone", "geolocation"],
                origin="https://github.com",
            )
        except Exception:
            pass

        page = context.new_page()
        page.set_default_timeout(20000)
        cdp = context.new_cdp_session(page)

        _do_login_and_apply(
            page, cdp, context, username, use_password, secret_key,
            school_name, student_name, interactive, result,
        )

        # Mode bulk: setelah submit sukses, monitor status edu SELAGI browser
        # masih hidup (di mode satuan, monitoring dilakukan oleh menu manual).
        if not interactive and result.get("status") == "success":
            try:
                print("\nMonitoring status edu (menunggu approved/final)...")
                edu = monitor_education_status_with_refresh(page, username)
                result["edu_status"] = edu.get("status")
                result["edu_message"] = edu.get("message", "")
                # Bila ditolak, sertakan alasan detail agar admin tahu perbaikannya
                if edu.get("reason"):
                    result["edu_reason"] = edu.get("reason")
                    print(f"  ALASAN DITOLAK:\n{edu.get('reason')}")
            except Exception as e:
                print(f"  Gagal monitor status edu: {e}")

        return result

    except Exception as e:
        print(f"\n[ERROR] Runtime: {e}")
        traceback.print_exc()
        result["message"] = str(e)
        return result
    finally:
        # Mode interactive: monitoring status sebentar lalu tutup.
        # Mode bulk: caller (run_bulk) yang memutuskan; di sini kita tetap
        # tutup browser karena run_bulk sudah selesai memakai page-nya.
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass


def _is_2fa_page(url):
    return any(p in url for p in ["sessions/two-factor", "sessions/verified-device", "/login"])


def _do_login_and_apply(
    page, cdp, context, username, use_password, secret_key,
    school_name, student_name, interactive, result,
):
    """Inti proses: login -> 2FA -> parse -> ID card -> form edu -> submit."""

    # ---- PHASE 1: LOGIN ----
    print("\nPHASE 1: LOGIN GITHUB...")
    safe_goto(page, "https://github.com/login", timeout=60000)
    human_pause(1.0, 2.2)  # jeda seperti orang membaca halaman login
    human_mouse_wiggle(page)
    human_type(page, "#login_field", username)
    human_pause(0.5, 1.2)
    human_type(page, "#password", use_password)
    human_pause(0.4, 1.0)

    # Pre-generate OTP (NTP sync bisa lambat) sebelum submit
    clean_secret = clean_totp_secret(secret_key)
    pre_otp = None
    try:
        print("  Pre-sync NTP & generate OTP...")
        pre_otp = generate_totp(clean_secret)
        print(f"  OTP siap: {pre_otp}")
    except Exception as e:
        print(f"  Pre-generate OTP gagal: {e}")

    page.press("#password", "Enter")

    # ---- PHASE 2: 2FA ----
    print("\nPHASE 2: CHECKING 2FA...")
    try:
        page.wait_for_selector('input[name="otp"], input#app_totp', timeout=10000)
        if not _is_2fa_page(page.url):
            print(f"  Login OK (2FA di-skip - trusted): {page.url}")
        else:
            print("  2FA challenge terdeteksi, memasukkan OTP...")
            attempt = 0
            otp_code = pre_otp
            while attempt < 5:
                if not _is_2fa_page(page.url):
                    print(f"  Berhasil login! URL: {page.url}")
                    break
                if otp_code is None:
                    try:
                        otp_code = generate_totp(clean_secret)
                    except Exception as e:
                        print(f"  Gagal generate OTP: {e}")
                        break
                attempt += 1
                print(f"  Percobaan #{attempt} - Kode: {otp_code}")
                try:
                    field = page.wait_for_selector(
                        'input[name="otp"], input#app_totp', timeout=5000
                    )
                    human_pause(0.4, 1.0)
                    field.fill("")
                    # Ketik OTP digit-per-digit dengan jeda manusiawi
                    for d in otp_code:
                        field.type(d, delay=random.uniform(80, 180))
                    human_pause(0.3, 0.7)
                    field.press("Enter")
                except Exception as e:
                    print(f"  OTP field tidak tersedia: {e}")
                    if not _is_2fa_page(page.url):
                        break
                    time.sleep(2)
                    continue
                print("  Menunggu respon GitHub...")
                try:
                    page.wait_for_function(
                        """(u) => location.href !== u || !(
                            location.href.includes('sessions/two-factor') ||
                            location.href.includes('sessions/verified-device') ||
                            location.href.includes('/login'))""",
                        arg=page.url, timeout=10000,
                    )
                except Exception:
                    pass
                if not _is_2fa_page(page.url):
                    print(f"  Berhasil login! URL: {page.url}")
                    break
                print("  Kode ditolak. Tunggu 15 detik re-generate...")
                time.sleep(15)
                otp_code = None
    except Exception:
        print("  Tidak ada 2FA challenge / langsung masuk.")

    if "trusted-devices" in page.url:
        # Langsung ke profil — tidak perlu mampir ke dashboard dulu
        pass

    # Parse profil sekaligus "warm-up" alami (kunjungi halaman settings)
    avatar_url = None
    user_address = "JL. GITHUB CAMPUS NO. 1"
    try:
        safe_goto(page, "https://github.com/settings/profile", timeout=60000)
        page.wait_for_selector("#user_profile_location", timeout=10000)
        profile_data = page.evaluate("""() => {
            var data = {avatar: null, location: null, display_name: null};
            var avatarImg = document.querySelector('.avatar-user');
            if (avatarImg && avatarImg.src && !avatarImg.src.includes('identicon') && !avatarImg.src.includes('github-logo')) {
                data.avatar = avatarImg.src;
            }
            var locInput = document.getElementById('user_profile_location');
            if (locInput && locInput.value.trim() !== '') data.location = locInput.value.trim();
            var nameInput = document.getElementById('user_display_name');
            if (nameInput && nameInput.value.trim() !== '') data.display_name = nameInput.value.trim();
            return data;
        }""")
        if profile_data.get("avatar"):
            avatar_url = profile_data["avatar"]
            print(f"  Avatar: {avatar_url}")
        else:
            print("  Avatar tidak ditemukan (fallback siluet).")
        if profile_data.get("location"):
            user_address = profile_data["location"]
            print(f"  Location: {user_address}")
        else:
            print(f"  Location kosong (fallback: {user_address}).")
        if profile_data.get("display_name"):
            student_name = profile_data["display_name"].upper()
            print(f"  Display Name -> Nama IDCard: {student_name}")
    except Exception as e:
        print(f"  Gagal parse profil: {e}")

    # ---- RENDER ID CARD ----
    print("\nPHASE 3: RENDER ID CARD...")
    img_data_url = None
    try:
        img_data_url = render_id_card(page, cdp, school_name, student_name, user_address, avatar_url)
    except Exception as e:
        print(f"  Gagal render ID card: {e}")

    hijack_js = None
    if img_data_url:
        hijack_js = build_camera_hijack_js(img_data_url)
        # Pasang di SETIAP dokumen baru (mirip addScriptToEvaluateOnNewDocument)
        context.add_init_script(hijack_js)
        print("  Camera spoof (Logitech C922) siap.")
    else:
        print("  Camera spoof dilewati (capture gagal).")

    # ---- PHASE 4: FORM EDU + KAMERA + SUBMIT ----
    submitted_ok = _fill_edu_form_loop(
        page, school_name, hijack_js, result, interactive
    )
    return submitted_ok


def _interact_camera_button(page, button_name):
    """Cari & klik tombol kamera (Start Camera / Take picture) dengan beberapa fallback."""
    xpath = (
        f'//button[contains(@class, "WebcamUpload-module__Button")] | '
        f'//button[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
        f'"abcdefghijklmnopqrstuvwxyz"), "{button_name.lower()}")]'
    )
    try:
        btn = page.wait_for_selector(f"xpath={xpath}", timeout=5000)
        if not btn:
            return False
        btn.scroll_into_view_if_needed()
        time.sleep(1)
        try:
            btn.click(timeout=3000)
        except Exception:
            btn.evaluate("el => el.click()")
        return True
    except Exception:
        return False


def _select_proof_type(page):
    """Pilih proof type '1. Dated school ID' dari dropdown."""
    try:
        select_btn = page.wait_for_selector(
            'xpath=//span[contains(text(), "Select...")]/ancestor::button', timeout=8000
        )
        select_btn.scroll_into_view_if_needed()
        select_btn.evaluate("el => el.click()")
        option_one = page.wait_for_selector(
            'xpath=//span[contains(text(), "1. Dated school ID")]', timeout=8000
        )
        option_one.evaluate("el => el.click()")
        print("  Proof Type -> '1. Dated school ID'")
        return True
    except Exception as e:
        print(f"  Gagal pilih proof type: {e}")
        return False


def _fill_edu_form_loop(page, school_name, hijack_js, result, interactive):
    """Loop pengisian form edu + retry saat 'not on campus'."""
    application_attempt = 1

    while application_attempt <= MAX_NOT_ON_CAMPUS_RETRY + 1:
        print(f"\nPHASE 3: Isi formulir aplikasi (percobaan {application_attempt})...")
        # Bersihkan stream/timer kamera dari percobaan sebelumnya agar tidak
        # menumpuk dan membebani renderer (penyebab "Page crashed" di retry).
        try:
            page.evaluate(
                """() => {
                    try { if (window.__bhaqi_drawTimer) clearInterval(window.__bhaqi_drawTimer); } catch(e){}
                    try {
                        if (window.__bhaqi_cachedStream) {
                            window.__bhaqi_cachedStream.getTracks().forEach(t => { try { t.stop(); } catch(e){} });
                        }
                    } catch(e){}
                    window.__bhaqi_cachedStream = null;
                    window.__bhaqi_drawTimer = null;
                }"""
            )
        except Exception:
            pass
        safe_goto(page, "https://github.com/settings/education/benefits", timeout=60000)
        human_pause(1.2, 2.5)
        human_mouse_wiggle(page)

        # Bypass popup
        try:
            popup = page.wait_for_selector("#dialog-show-education-benefits-dialog", timeout=8000)
            popup.evaluate("el => el.click()")
        except Exception:
            pass

        # Pilih student radio
        try:
            radio = page.wait_for_selector('input[value="student"]', timeout=8000)
            human_pause(0.5, 1.2)
            radio.evaluate("el => el.click()")
        except Exception:
            pass

        # Share Location
        print("  Meminta Share Location...")
        human_pause(0.6, 1.4)
        shared = False
        try:
            share = page.wait_for_selector(
                'xpath=//button[contains(normalize-space(.), "Share Location")]', timeout=10000
            )
            share.scroll_into_view_if_needed()
            try:
                share.click(timeout=3000)
            except Exception:
                share.evaluate("el => el.click()")
            print("  'Share Location' diklik. Menunggu koordinat diproses...")
            # Tunggu form lanjut ke stage berikut: tombol Share Location hilang
            # ATAU field sekolah muncul. Geolocation context sudah di-set, jadi
            # getCurrentPosition langsung sukses (tidak menggantung).
            try:
                page.wait_for_function(
                    """() => {
                        var sf = document.getElementById('js-school-name-search');
                        if (sf) return true;
                        var btns = Array.from(document.querySelectorAll('button'));
                        var still = btns.some(b => (b.innerText||'').includes('Share Location'));
                        return !still;
                    }""",
                    timeout=15000,
                )
                shared = True
                print("  Lokasi diterima, lanjut ke pemilihan sekolah.")
            except Exception:
                print("  WARNING: stage lokasi belum berubah setelah Share Location.")
        except Exception:
            # Tombol tidak ada bisa berarti lokasi sudah granted sebelumnya
            # ATAU memang sudah di stage sekolah. Cek field sekolah.
            if page.query_selector("#js-school-name-search"):
                shared = True
                print("  Share Location tidak diperlukan (sudah di stage sekolah).")
            else:
                print("  Tombol Share Location tidak terdeteksi.")

        # Pilih sekolah - tunggu field benar-benar ada dulu (gate stage 1)
        print(f"  Selecting School: {school_name}")
        try:
            page.wait_for_selector("#js-school-name-search", timeout=15000)
            human_pause(0.6, 1.3)
            page.evaluate(
                """([sel, val]) => {
                    var el = document.querySelector(sel);
                    var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, ''); el.dispatchEvent(new Event('input', {bubbles:true}));
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }""",
                ["#js-school-name-search", school_name],
            )
            print("  Menunggu dropdown sekolah...")
            try:
                page.wait_for_selector(
                    "ul[role='listbox'] li, .ActionList-item, .autocomplete-item, [role='option']",
                    timeout=12000,
                )
                human_pause(0.5, 1.1)  # jeda seperti orang melihat daftar
                items = page.query_selector_all(
                    "ul[role='listbox'] li, .ActionList-item, .autocomplete-item, [role='option']"
                )
                matched = False
                for item in items:
                    txt = (item.inner_text() or "").strip()
                    if txt and school_name.lower() in txt.lower():
                        item.evaluate("el => el.click()")
                        print(f"  Pilih sekolah: {txt}")
                        matched = True
                        break
                if not matched:
                    page.press("#js-school-name-search", "ArrowDown")
                    page.press("#js-school-name-search", "Enter")
                    print("  Fallback: opsi pertama dipilih.")
            except Exception:
                page.press("#js-school-name-search", "ArrowDown")
                page.press("#js-school-name-search", "Enter")
                print("  Fallback Enter standar.")
        except Exception as e:
            print(f"  Gagal input sekolah: {e}")

        # Continue
        try:
            cont = page.wait_for_selector('xpath=//button[contains(., "Continue")]', timeout=10000)
            cont.evaluate("el => el.click()")
            print("  Clicked Continue!")
        except Exception:
            pass

        # GATE STAGE 2: pastikan benar-benar pindah dari stage sekolah ke stage
        # proof. Tanpa ini, kalau stage 1 gagal (mis. lokasi/sekolah tak terisi),
        # kode salah mengira sudah submit. Tunggu indikator stage proof muncul.
        print("  Verifikasi transisi ke stage bukti (proof)...")
        in_proof_stage = False
        try:
            page.wait_for_function(
                """() => {
                    var body = (document.body.innerText || '').toLowerCase();
                    if (body.includes('what is a valid proof of education')) return true;
                    if (body.includes('proof of education')) return true;
                    if (document.querySelector('button[class*="WebcamUpload-module__Button"]')) return true;
                    var spans = Array.from(document.querySelectorAll('span,button'));
                    if (spans.some(s => (s.innerText||'').includes('Select...'))) return true;
                    return false;
                }""",
                timeout=15000,
            )
            in_proof_stage = True
            print("  Sudah di stage bukti.")
        except Exception:
            print("  WARNING: belum masuk stage bukti (stage 1 mungkin gagal).")

        if not in_proof_stage:
            # Stage 1 belum lewat. Jangan teruskan ke kamera/submit (mencegah
            # false 'submitted'). Ulangi dari awal form kalau masih ada jatah.
            if application_attempt <= MAX_NOT_ON_CAMPUS_RETRY:
                print(f"  Stage 1 gagal, ulang form (percobaan {application_attempt + 1})...")
                application_attempt += 1
                continue
            else:
                print("  Stage 1 gagal terus, batas retry tercapai.")
                result["status"] = "failed"
                result["message"] = "Stuck di stage 1 (lokasi/sekolah tidak lolos)"
                return False

        # Proof type
        print("  Memilih Proof Type...")
        _select_proof_type(page)

        # Kamera
        print(f"\nPHASE 4: START CAMERA & UPLOAD (percobaan {application_attempt})...")
        print("  Klik 'Start Camera'...")
        if _interact_camera_button(page, "start camera"):
            print("  'Start Camera' diklik, tunggu stream WebRTC (5s)...")
            time.sleep(5)
            print("  Klik 'Take picture'...")
            if _interact_camera_button(page, "take picture") or _interact_camera_button(page, "take photo"):
                print("  'Take picture' diklik, tunggu snapshot (4s)...")
                time.sleep(4)
        else:
            print("  Tombol kamera tidak ditemukan, lanjut ke proof sub-loop...")

        # Proof sub-loop
        not_on_campus = _proof_subloop(page, hijack_js)

        # Double-check not-on-campus jika belum ketahuan
        if not not_on_campus:
            print("  Cek respons GitHub (deteksi 'not on campus')...")
            time.sleep(3)
            try:
                not_on_campus = bool(page.evaluate(_NOT_ON_CAMPUS_JS))
            except Exception:
                not_on_campus = False

        if not_on_campus:
            print("\n  DITOLAK: Form 'Not on campus' terdeteksi.")
            # Tidak retry di sesi yang sama — IP harus dirotasi dulu. Beri sinyal
            # ke run_account agar menutup browser, ganti IP, lalu apply ulang.
            result["status"] = "not_on_campus"
            result["message"] = "Ditolak: not on campus (perlu rotasi IP)"
            return False
        else:
            print("  Tidak ada penolakan 'not on campus'. Proses berjalan!")
            result["status"] = "success"
            result["message"] = "Apply berhasil disubmit"

            if not interactive:
                return True

            # Mode interactive: menu manual
            print("\nFINAL PHASE: MANUAL CONTROL")
            print("  1. Apply ulang")
            print("  2. Keluar (tutup browser)")
            while True:
                cmd = input("Command (1/2) > ").strip()
                if cmd == "1":
                    application_attempt += 1
                    break
                elif cmd == "2":
                    return True
                else:
                    print("  Pilihan tidak valid.")
            continue

    return result.get("status") == "success"


def _proof_subloop(page, hijack_js):
    """Submit Continue & cek apakah bukti terupload. Return True jika 'not on campus'."""
    proof_attempt = 0
    while True:
        proof_attempt += 1
        print(f"\n  [Proof Sub-Loop #{proof_attempt}] Submit (Continue)...")
        try:
            final_continue = page.wait_for_selector(
                "#js-developer-pack-application-submit-button", timeout=10000
            )
            final_continue.evaluate("el => el.click()")
            print("  'Continue' utama diklik!")
        except Exception as e:
            print(f"  Gagal klik 'Continue': {e}")

        print("  Menunggu transisi halaman (5s)...")
        time.sleep(5)

        # Prioritas: cek not-on-campus dulu
        try:
            if page.evaluate(_NOT_ON_CAMPUS_JS):
                print("  Form 'Not on campus' terdeteksi di sub-loop!")
                return True
        except Exception:
            pass

        # Cek form proof masih ada?
        try:
            form_exists = bool(page.evaluate(_FORM_STILL_EXISTS_JS))
        except Exception:
            form_exists = False

        if not form_exists:
            print("  Form proof hilang, verifikasi ulang (3s)...")
            time.sleep(3)
            try:
                if page.evaluate(_NOT_ON_CAMPUS_JS):
                    print("  'Not on campus' terdeteksi setelah verifikasi ulang!")
                    return True
            except Exception:
                pass
            print("  Form bukti sudah tidak ada - aplikasi maju ke tahap berikutnya!")
            return False

        print("  Form proof/kamera masih ada. Mengulang proses kamera...")

        # Re-inject WebRTC
        if hijack_js:
            try:
                page.evaluate("""() => {
                    if (window.__bhaqi_drawTimer) { clearInterval(window.__bhaqi_drawTimer); window.__bhaqi_drawTimer = null; }
                    window.__BHAQI_HIJACK_INSTALLED = false;
                    window.__bhaqi_cachedStream = null;
                }""")
                page.evaluate(hijack_js)
                print("  WebRTC hijack di-reinject.")
            except Exception as e:
                print(f"  Gagal reinject WebRTC: {e}")

        # Pilih ulang proof type
        _select_proof_type(page)

        # Ulangi capture kamera
        print("  Mengupload kembali gambar kamera...")
        if _interact_camera_button(page, "start camera"):
            time.sleep(5)
            _interact_camera_button(page, "take picture")
            time.sleep(4)
        elif _interact_camera_button(page, "camera") or _interact_camera_button(page, "picture"):
            time.sleep(3)

        # Batas aman sub-loop
        if proof_attempt >= 4:
            print("  Sub-loop proof mencapai batas, keluar untuk evaluasi.")
            return False


# ==============================================================================
# accounts.json HELPERS
# ==============================================================================
def load_accounts() -> list:
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"File '{ACCOUNTS_FILE}' tidak ditemukan!")
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Gagal membaca '{ACCOUNTS_FILE}': {e}")
        return []


def save_accounts(accounts: list):
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Gagal menyimpan '{ACCOUNTS_FILE}': {e}")


def _derive_name_preview(acc: dict) -> str:
    username = acc.get("username", "").strip()
    if not username:
        return "(kosong)"
    _clean = re.sub(r"[0-9]+", "", username)
    _clean = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", _clean)
    return _clean.replace(".", " ").replace("_", " ").strip().upper() or "STUDENT"


def make_log_path(username: str, index: int, total: int) -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = username.replace(".", "_").replace("-", "_").replace(" ", "_")
    return os.path.join(LOGS_DIR, f"bulk_{index:02d}of{total:02d}_{safe}_{ts}.log")


class TeeOutput:
    """Tulis stdout ke terminal sekaligus ke file log."""
    def __init__(self, log_path: str):
        self._terminal = sys.stdout
        self._log_file = open(log_path, "w", encoding="utf-8")

    def write(self, msg):
        self._terminal.write(msg)
        try:
            self._log_file.write(msg)
        except Exception:
            pass

    def flush(self):
        self._terminal.flush()
        try:
            self._log_file.flush()
        except Exception:
            pass

    def restore(self):
        sys.stdout = self._terminal
        try:
            self._log_file.close()
        except Exception:
            pass


def print_accounts_table(accounts: list):
    cw = [4, 18, 20, 8]
    H = ["#", "Username", "Nama (estimasi)", "Secret"]

    def row_line(left="├", mid="┼", right="┤"):
        return left + mid.join("─" * (w + 2) for w in cw) + right

    def fmt_row(*cells):
        return "│" + "│".join(f" {str(c):<{cw[i]}} " for i, c in enumerate(cells)) + "│"

    print("Daftar Akun (accounts.json):")
    print(row_line("┌", "┬", "┐"))
    print(fmt_row(*H))
    print(row_line("├", "┼", "┤"))
    for idx, acc in enumerate(accounts, 1):
        uname = acc.get("username", "").strip() or "(kosong)"
        nama = _derive_name_preview(acc)
        has_sk = "ada" if acc.get("secret_key", "").strip() else "-"
        print(fmt_row(idx, uname[: cw[1]], nama[: cw[2]], has_sk))
    print(row_line("└", "┴", "┘"))
    print(f"  Proxy   : {CONSTANT_PROXY or '(BELUM DISET - isi CONSTANT_PROXY!)'}")
    print(f"  Password: {CONSTANT_PASSWORD}\n")


def prompt_account_selection(accounts: list) -> list:
    while True:
        raw = input(
            f"Pilih akun (maks {MAX_ACCOUNTS}). Koma -> 1,3,5 | Semua -> all | Keluar -> q\n> "
        ).strip().lower()
        if raw == "q":
            return []
        if raw == "all":
            selected = list(range(len(accounts)))
        else:
            try:
                selected = [int(x.strip()) - 1 for x in raw.split(",") if x.strip()]
            except ValueError:
                print("Format tidak valid.\n")
                continue
        invalid = [i + 1 for i in selected if i < 0 or i >= len(accounts)]
        if invalid:
            print(f"Nomor tidak valid: {invalid}.\n")
            continue
        seen, unique = set(), []
        for i in selected:
            if i not in seen:
                seen.add(i)
                unique.append(i)
        if len(unique) > MAX_ACCOUNTS:
            print(f"Maksimal {MAX_ACCOUNTS} akun.\n")
            continue
        if not unique:
            print("Tidak ada akun dipilih.\n")
            continue
        return unique


def prompt_missing_data(accounts: list, selected_idx: list) -> list:
    needs = [
        i for i in selected_idx
        if not accounts[i].get("username", "").strip()
        or not accounts[i].get("secret_key", "").strip()
    ]
    if not needs:
        return accounts
    print("\nLengkapi data akun yang kosong:")
    changed = False
    for i in needs:
        acc = accounts[i]
        print(f"\n[Akun #{i + 1}]")
        if not acc.get("username", "").strip():
            val = input("  GitHub Username       : ").strip()
            if val:
                acc["username"] = val
                changed = True
            else:
                print("  Dilewati (username kosong).")
                continue
        if not acc.get("secret_key", "").strip():
            val = input("  2FA Secret Key (TOTP) : ").strip()
            if val:
                acc["secret_key"] = val
                changed = True
            else:
                print("  Dilewati (secret key kosong).")
                acc["username"] = acc.get("username", "")
                continue
        accounts[i] = acc
    if changed:
        save_accounts(accounts)
        print("\n  accounts.json diperbarui.")
    print()
    return accounts


# ==============================================================================
# MODE SATUAN (single)
# ==============================================================================
def run_single():
    print("\n" + "=" * 55)
    print("GITHUB EDU AUTO-APPLY - PLAYWRIGHT (MODE SATUAN)")
    print("=" * 55)
    username = input("GitHub Username       : ").strip()
    secret_key = input("2FA Secret Key (TOTP) : ").strip()
    first_name = input("Nama Depan (opsional) : ").strip().upper()
    proxy_input = input(
        f"Proxy (host:port:user:pass) [Enter = {CONSTANT_PROXY or 'tanpa proxy'}]: "
    ).strip()
    if not proxy_input:
        proxy_input = CONSTANT_PROXY

    if not username or not secret_key:
        print("Username & secret key wajib diisi.")
        return

    # Pastikan IP proxy di Jawa Barat sebelum mulai
    proxy_input, geo_ok, geo = ensure_proxy_in_region(proxy_input)
    if not geo_ok:
        cont = input(
            "IP proxy BUKAN Jawa Barat "
            f"({geo.get('region','?')}/{geo.get('city','?')}). Lanjut juga? (y/n): "
        ).strip().lower()
        if cont != "y":
            print("Dibatalkan.")
            return

    result = run_account(
        username, secret_key, first_name, proxy_input, interactive=True
    )
    print(f"\nHasil: {result.get('status', 'failed').upper()} - {result.get('message', '')}")
    input("\nTekan Enter untuk keluar...")


# ==============================================================================
# MODE BANYAK (bulk)
# ==============================================================================
def run_bulk():
    print("\n" + "=" * 60)
    print("  GITHUB EDU BULK APPLY - PLAYWRIGHT (MODE BANYAK)")
    print("  Sequential: 1 browser identitas-baru per akun")
    print("=" * 60)
    print(f"  Jeda antar akun   : {DELAY_MIN_SEC}-{DELAY_MAX_SEC}s")
    print(f"  Tunggu status edu : sampai final / maks {EDU_WAIT_TIMEOUT_SEC // 60} menit")
    print("=" * 60 + "\n")

    accounts = load_accounts()
    if not accounts:
        input("Tekan Enter untuk keluar...")
        return

    print_accounts_table(accounts)
    selected_idx = prompt_account_selection(accounts)
    if not selected_idx:
        print("Keluar.")
        return

    accounts = prompt_missing_data(accounts, selected_idx)

    ready_idx = [
        i for i in selected_idx
        if accounts[i].get("username", "").strip()
        and accounts[i].get("secret_key", "").strip()
    ]
    skipped_idx = [i for i in selected_idx if i not in ready_idx]
    if not ready_idx:
        print("Tidak ada akun siap diproses.")
        input("Tekan Enter untuk keluar...")
        return
    if skipped_idx:
        print(f"Akun #{[i + 1 for i in skipped_idx]} dilewati (data tidak lengkap).\n")

    print(f"Memulai -> {len(ready_idx)} akun...\n")

    results = []
    total = len(ready_idx)

    for batch_no, acc_idx in enumerate(ready_idx, 1):
        acc = accounts[acc_idx]
        username = acc["username"].strip()
        secret_key = acc.get("secret_key", "")

        # Rotasi proxy/IP per akun (kalau dikonfigurasi)
        proxy = CONSTANT_PROXY
        if CONSTANT_PROXY and "dataimpulse.com" in CONSTANT_PROXY:
            proxy = rotate_dataimpulse_proxy(CONSTANT_PROXY)
        if IP_ROTATION_URL:
            trigger_ip_rotation_url(IP_ROTATION_URL)
            time.sleep(5)

        # Pastikan IP proxy benar-benar di Jawa Barat sebelum proses akun.
        proxy, geo_ok, geo = ensure_proxy_in_region(proxy)
        if not geo_ok:
            print(f"  [GEO] Lewati akun {username}: IP proxy bukan Jawa Barat.")
            results.append({
                "no": batch_no, "username": username, "status": "skipped_geo",
                "message": f"IP proxy tidak di Jawa Barat ({geo.get('region','?')}/{geo.get('city','?')})",
                "log": "", "duration": 0, "edu_status": None, "edu_message": "",
            })
            continue

        log_path = make_log_path(username, batch_no, total)
        div = "=" * 60
        print(
            f"\n{div}\n  [{batch_no}/{total}] {username}\n"
            f"  Proxy : {proxy or '(langsung)'}\n  Log   : {log_path}\n"
            f"  Waktu : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{div}"
        )

        tee = TeeOutput(log_path)
        sys.stdout = tee
        start_time = time.time()
        row = {
            "no": batch_no, "username": username, "status": "failed",
            "message": "", "log": log_path, "duration": 0,
            "edu_status": None, "edu_message": "",
        }
        try:
            result = run_account(
                username=username, secret_key=secret_key,
                proxy_input=proxy, password=CONSTANT_PASSWORD, interactive=False,
            )
            row["status"] = result.get("status", "failed")
            row["message"] = result.get("message", "")
            row["edu_status"] = result.get("edu_status")
            row["edu_message"] = result.get("edu_message", "")
        except Exception as exc:
            row["status"] = "error"
            row["message"] = str(exc)
            traceback.print_exc()
        finally:
            row["duration"] = round(time.time() - start_time, 1)
            tee.restore()

        results.append(row)
        icon = "[OK]" if row["status"] == "success" else "[X]"
        print(f"  {icon} [{batch_no}/{total}] {username} -> "
              f"{row['status'].upper()} ({row['duration']}s)")
        if row.get("edu_status"):
            print(f"  Status edu: {row['edu_status'].upper()} - {row.get('edu_message','')}")

        if batch_no < total:
            delay = random.uniform(DELAY_MIN_SEC, DELAY_MAX_SEC)
            print(f"\n  Jeda {delay:.0f}s sebelum akun berikutnya...")
            time.sleep(delay)

    # Ringkasan
    ok_n = sum(1 for r in results if r["status"] == "success")
    err_n = sum(1 for r in results if r["status"] != "success")
    edu_stats = {"verified": 0, "pending": 0, "declined": 0, "unknown": 0}
    for r in results:
        es = r.get("edu_status")
        if es in edu_stats:
            edu_stats[es] += 1

    print("\n" + "=" * 60)
    print("  RINGKASAN BULK APPLY")
    print("=" * 60)
    print(f"  Berhasil : {ok_n}")
    print(f"  Gagal    : {err_n}")
    print(f"  Dilewati : {len(skipped_idx)}")
    if any(edu_stats.values()):
        print("-" * 60)
        print(f"  Status Edu - verified: {edu_stats['verified']}  "
              f"pending: {edu_stats['pending']}  declined: {edu_stats['declined']}  "
              f"unknown: {edu_stats['unknown']}")
    print("-" * 60)
    for r in results:
        icon = "[OK]" if r["status"] == "success" else "[X]"
        es = r.get("edu_status", "")
        es_tag = f" [{es}]" if es else ""
        print(f"  {r['no']:<3} {icon} {r['status']:<8} {r['duration']:>6}s  {r['username']}{es_tag}")
    print()

    summary_path = os.path.join(
        LOGS_DIR, f"bulk_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(summary_path, "w", encoding="utf-8") as sf:
            sf.write(f"BULK APPLY SUMMARY - {datetime.now()}\n{'=' * 55}\n")
            sf.write(f"Berhasil : {ok_n}\nGagal : {err_n}\nDilewati : {len(skipped_idx)}\n")
            if any(edu_stats.values()):
                sf.write(f"Edu : verified={edu_stats['verified']}, pending={edu_stats['pending']}, "
                         f"declined={edu_stats['declined']}, unknown={edu_stats['unknown']}\n")
            sf.write("\n")
            for r in results:
                sf.write(f"[{r['no']}] {r['username']} -> {r['status']} ({r['duration']}s)\n")
                if r["message"]:
                    sf.write(f"    Pesan : {r['message']}\n")
                if r.get("edu_status"):
                    sf.write(f"    Edu   : {r['edu_status']} - {r.get('edu_message','')}\n")
                sf.write(f"    Log   : {r['log']}\n")
        print(f"  Summary: {summary_path}")
    except Exception as e:
        print(f"  Gagal simpan summary: {e}")

    print("=" * 60)
    input("\nTekan Enter untuk keluar...")


# ==============================================================================
# ENTRY POINT - PILIH MODE
# ==============================================================================
def apply_account_for_bot(
    username: str,
    secret_key: str,
    password: str = "",
    proxy: str = "",
    headless: bool = True,
    log_sink=None,
) -> dict:
    """Entry point untuk dipanggil dari bot Telegram (1 akun, non-interaktif).

    - Mengarahkan semua log ke `log_sink` (callback(line:str)) bila diberikan.
    - Proxy: bila tidak diberikan, pakai env EDU_PROXY → CONSTANT_PROXY.
      Tanpa proxy, GitHub akan melihat IP server (lokasi tidak cocok dengan
      GPS sekolah Indonesia) → hampir pasti "not on campus".
    - Geo-check dijalankan bila proxy ada (memastikan IP di Jawa Barat).
    - Mengembalikan dict hasil: {status, message, edu_status, edu_message}.
    """
    global HEADLESS, GEO_CHECK_ENABLED

    HEADLESS = headless
    if log_sink is not None:
        set_log_sink(log_sink)

    # Urutan prioritas proxy: argumen eksplisit → env EDU_PROXY → CONSTANT_PROXY
    proxy_input = proxy or os.environ.get("EDU_PROXY", "") or CONSTANT_PROXY

    if not proxy_input:
        print(
            "   [WARN] TANPA PROXY: GitHub akan melihat IP server. Lokasi GPS "
            "sekolah Indonesia tidak akan cocok dengan IP → kemungkinan besar "
            "'not on campus'. Set EDU_PROXY atau CONSTANT_PROXY."
        )

    GEO_CHECK_ENABLED = bool(proxy_input)
    if proxy_input and GEO_CHECK_ENABLED:
        print("   Memverifikasi region IP proxy (target: Jawa Barat)...")
        proxy_input, geo_ok, geo = ensure_proxy_in_region(proxy_input)
        if not geo_ok:
            print(
                f"   [WARN] IP proxy bukan Jawa Barat "
                f"({geo.get('region', '?')}/{geo.get('city', '?')}). "
                f"Tetap lanjut, tapi risiko 'not on campus' naik."
            )

    try:
        return run_account(
            username=username,
            secret_key=secret_key,
            first_name="",
            proxy_input=proxy_input,
            password=password or CONSTANT_PASSWORD,
            interactive=False,
        )
    finally:
        if log_sink is not None:
            set_log_sink(None)


def main():
    global HEADLESS
    print("\n" + "=" * 60)
    print("  GITHUB EDU AUTO-APPLY  |  PLAYWRIGHT EDITION (BHAQI v16)")
    print("=" * 60)
    print("-" * 60)
    print("  Pilih mode:")
    print("    1. Satuan (single)  - input 1 akun manual")
    print("    2. Banyak (bulk)    - proses banyak akun dari accounts.json")
    print("=" * 60)
    while True:
        choice = input("Mode (1/2) [Enter = 2]: ").strip()
        if choice == "" or choice == "2":
            run_bulk()
            return
        if choice == "1":
            run_single()
            return
        print("Pilihan tidak valid. Ketik 1 atau 2.")


if __name__ == "__main__":
    main()