"""
do_claimer.py — Automation klaim GitHub Student Pack → DigitalOcean $200 Credit.

=== ALUR ASLI (OAuth, sesuai screenshot) ===
  1. Login GitHub sebagai akun GHS (penjual) di github.com/login + 2FA (TOTP).
  2. Buka https://education.github.com/pack.
  3. Jika muncul survei "Welcome to GitHub Education" → skip / buka ulang URL pack.
  4. Cari kartu offer DigitalOcean → klik link
     "Get access by connecting your GitHub account on DigitalOcean"
     (href: /pack/redeem/digitalocean-student, biasanya buka tab baru).
  5. DigitalOcean meminta login → isi kredensial akun BUYER (email+password)
     lalu 2FA DigitalOcean (TOTP) bila diminta.
  6. Halaman "Authenticate with GitHub" → klik tombolnya.
  7. GitHub menampilkan "Authorize DigitalOcean Education" → klik
     "Authorize digitalocean" (tombol aktif setelah jeda anti-clickjacking).
  8. Redirect balik ke DigitalOcean → muncul "GitHub Student Pack Applied"
     dan teks "Happy Coding!" = voucher berhasil diklaim.

=== KEAMANAN / ISOLASI ===
  Setiap pemanggilan membuka instance browser + context BARU (cookie bersih),
  dan context di-clear di awal untuk memastikan tidak ada sesi lama yang bocor.

=== FORMAT AKUN GHS ===
  "email@gmail.com:Password123"              — tanpa 2FA
  "email@gmail.com:Password123:TOTP_SECRET"  — dengan TOTP 2FA

=== AKUN BUYER DIGITALOCEAN ===
  do_email + do_password (+ do_totp_secret bila akun pakai 2FA)
  ATAU cookies hasil export (Cookie-Editor JSON).
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _import_async_playwright():
    """Ambil async_playwright dari patchright (anti-deteksi) bila ada,
    selain itu fallback ke playwright biasa. Return (fn, using_patchright)."""
    try:
        from patchright.async_api import async_playwright
        return async_playwright, True
    except ImportError:
        try:
            from playwright.async_api import async_playwright
            return async_playwright, False
        except ImportError:
            return None, False


# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

TIMEOUT = 30_000  # ms — general wait timeout
NAV_TIMEOUT = 60_000  # ms — page navigation timeout

GITHUB_LOGIN_URL = "https://github.com/login"
GH_EDUCATION_PACK_URL = "https://education.github.com/pack"

DO_DASHBOARD_URL = "https://cloud.digitalocean.com/projects"
DO_LOGIN_URL = "https://cloud.digitalocean.com/login"
DO_BILLING_URL = "https://cloud.digitalocean.com/account/billing"

# ------------------------------------------------------------------
# Selectors — GitHub login (github.com/login)  [verified live]
# ------------------------------------------------------------------

GH_USERNAME_SELECTORS = ["#login_field", 'input[name="login"]']
GH_PASSWORD_SELECTORS = [
    "#password",
    'input[name="password"]',
    'input[type="password"]',
]
GH_SUBMIT_SELECTORS = [
    'input[name="commit"]',
    'input[type="submit"][value="Sign in"]',
    'button[type="submit"]',
]

# GitHub 2FA — kolom kode TOTP
GH_OTP_SELECTORS = [
    "#app_totp",
    'input[name="app_otp"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[placeholder*="code" i]',
]

# ------------------------------------------------------------------
# Selectors — GitHub Education survey ("Welcome to GitHub Education!")
# ------------------------------------------------------------------

GH_SURVEY_MARKERS = [
    'button.onboarding__skip-question',
    'text=Welcome to GitHub Education',
    'text=Skip this question',
]
GH_SURVEY_SKIP_SELECTORS = [
    'button.onboarding__skip-question',
    'button:has-text("Skip this question")',
]

# ------------------------------------------------------------------
# Selectors — Offer DigitalOcean di halaman pack  [dari screenshot]
# ------------------------------------------------------------------

GH_DO_OFFER_SELECTORS = [
    'a[href="/pack/redeem/digitalocean-student"]',
    'a[href*="/pack/redeem/digitalocean"]',
    'a[aria-label*="DigitalOcean" i]',
    'a[href*="redeem"][href*="digitalocean"]',
]

# ------------------------------------------------------------------
# Selectors — GitHub OAuth authorize  [dari screenshot]
# ------------------------------------------------------------------

GH_AUTHORIZE_SELECTORS = [
    'button[name="authorize"][value="1"]',
    'button.js-oauth-authorize-btn',
    'button[data-octo-click="oauth_application_authorization"]',
    'button:has-text("Authorize")',
]

# ------------------------------------------------------------------
# Selectors — DigitalOcean login (cloud.digitalocean.com/login) [verified]
# ------------------------------------------------------------------

DO_EMAIL_SELECTORS = [
    "#email",
    '[data-testid="email-input"]',
    'input[name="email"]',
    'input[type="email"]',
]
DO_PASSWORD_SELECTORS = [
    "#password",
    '[data-testid="password-input"]',
    'input[type="password"]',
]
DO_LOGIN_SUBMIT_SELECTORS = [
    'button[data-tracking-id="registration--loginForm-login-button"]',
    'button[type="submit"]',
    'button:has-text("Log In")',
]

# DigitalOcean 2FA  [dari screenshot]
DO_OTP_SELECTORS = [
    "#code",
    '[data-testid="code-input"]',
    'input[autocomplete="one-time-code"]',
    'input[placeholder*="6-digit" i]',
    'input[placeholder*="code" i]',
]
DO_OTP_SUBMIT_SELECTORS = [
    'button:has-text("Verify Code")',
    'button[type="submit"]',
]

# DigitalOcean cookie-consent (TrustArc)
DO_CONSENT_SELECTORS = [
    "#truste-consent-button",
    'button:has-text("Agree & Proceed")',
]

# DigitalOcean "Authenticate with GitHub"  [dari screenshot]
DO_AUTH_GITHUB_SELECTORS = [
    'button:has-text("Authenticate with GitHub")',
    'a:has-text("Authenticate with GitHub")',
    '[data-testid*="oauth"]',
    'button[type="submit"]',
]

# DigitalOcean "Got it" pada modal sukses
DO_GOT_IT_SELECTORS = [
    'button:has-text("Got it")',
    'button:has-text("Got It")',
]

# ------------------------------------------------------------------
# Selectors — DigitalOcean billing / promo code  [dari screenshot]
# ------------------------------------------------------------------

DO_PROMO_INPUT_SELECTORS = [
    "#promoCode",
    'input[name="promoCode"]',
    'input[placeholder*="promo code" i]',
    'input[placeholder*="Add new promo" i]',
    'input[aria-label*="promo" i]',
]
DO_PROMO_SUBMIT_SELECTORS = [
    'button:has-text("Add Payment Method and Apply Code")',
    'button:has-text("Apply Code")',
    'button:has-text("Apply")',
    'button[type="submit"].Button__ButtonElt',
    'button[type="submit"]',
]

# ------------------------------------------------------------------
# Kata kunci verifikasi
# ------------------------------------------------------------------

DO_SUCCESS_KEYWORDS = [
    "happy coding",
    "github student pack applied",
    "has been credited",
    "good for 1 year",
    "$200 that is good",
]

GH_OFFER_NEGATIVE = [
    "already claimed",
    "already redeemed",
    "no longer available",
    "not eligible",
    "not a student",
]


# ------------------------------------------------------------------
# Result dataclass
# ------------------------------------------------------------------


@dataclass
class ClaimResult:
    success: bool
    message: str
    screenshot: Optional[bytes] = field(default=None, repr=False)


# ------------------------------------------------------------------
# TOTP generator (RFC 6238) — dipakai untuk 2FA GitHub & DigitalOcean
# ------------------------------------------------------------------


def _clean_b32(secret: str) -> str:
    """Bersihkan karakter non-Base32 dari TOTP secret."""
    return re.sub(r"[^A-Z2-7]", "", secret.upper())


def _gen_totp(secret: str) -> str:
    """Generate kode TOTP 6-digit saat ini dari Base32 secret."""
    secret = _clean_b32(secret)
    pad = len(secret) % 8
    if pad:
        secret += "=" * (8 - pad)
    key = base64.b32decode(secret)
    t = int(time.time()) // 30
    msg = struct.pack(">Q", t)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o : o + 4])[0] & 0x7FFF_FFFF) % 1_000_000
    return f"{code:06d}"


# ------------------------------------------------------------------
# Low-level Playwright helpers
# ------------------------------------------------------------------


async def _safe_screenshot(page) -> Optional[bytes]:
    """Ambil screenshot halaman saat ini. Return None jika gagal."""
    try:
        return await page.screenshot(full_page=False)
    except Exception:
        return None


async def _try_click(page, selectors: list, timeout: int = 5_000) -> bool:
    """Klik elemen pertama yang visible dari daftar selector."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            return True
        except Exception:
            continue
    return False


async def _try_fill(page, selectors: list, value: str, timeout: int = 5_000) -> bool:
    """Isi input pertama yang visible dari daftar selector."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.fill(value)
            return True
        except Exception:
            continue
    return False


async def _any_visible(page, selectors: list, timeout: int = 3_000) -> bool:
    """True jika salah satu selector visible dalam timeout."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _page_text(page) -> str:
    """Ambil teks halaman (lowercase) untuk pencarian kata kunci."""
    try:
        return (await page.content()).lower()
    except Exception:
        return ""


def _browser_args() -> list:
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]
    # Flag ini menjadi 'tell' bot; hanya dipakai bila patchright TIDAK aktif.
    _, using_patch = _import_async_playwright()
    if not using_patch:
        args.append("--disable-blink-features=AutomationControlled")
    return args


def _resolve_chrome_path():
    """Path executable Chrome sistem bila ada (None = pakai Chromium bawaan PW).

    Chromium bawaan Playwright kadang tidak terinstall di hosting; gunakan
    Google Chrome sistem. Override via env CHROME_PATH / PLAYWRIGHT_CHROME_PATH.
    """
    import os as _os

    candidates = [
        _os.environ.get("CHROME_PATH"),
        _os.environ.get("PLAYWRIGHT_CHROME_PATH"),
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for path in candidates:
        if path and _os.path.exists(path):
            return path
    return None

def _launch_kwargs() -> dict:
    """Argumen launch chromium, termasuk executable_path Chrome sistem bila ada.

    Mode headless dikontrol via env var DO_CLAIMER_HEADLESS:
      - "false" / "0" / "no"  → headed (tampilkan jendela browser)
      - selain itu (default)  → headless
    """
    import os as _os
    _headless_env = _os.environ.get("DO_CLAIMER_HEADLESS", "true").strip().lower()
    headless = _headless_env not in ("false", "0", "no")
    kwargs = {"headless": headless, "args": _browser_args()}
    chrome_path = _resolve_chrome_path()
    if chrome_path:
        kwargs["executable_path"] = chrome_path
        logger.info("[DO Claimer] Memakai Chrome sistem: %s", chrome_path)
    logger.info("[DO Claimer] headless=%s (DO_CLAIMER_HEADLESS=%s)", headless, _headless_env)
    return kwargs


def _fix_playwright_permissions() -> bool:
    """Perbaiki izin eksekusi binari Playwright (node + chromium headless shell).

    Di sebagian hosting (mis. panel Pterodactyl/Pelican), file driver Playwright
    kehilangan bit executable sehingga muncul '[Errno 13] Permission denied'.
    Fungsi ini mencoba chmod +x pada file-file driver. Return True bila ada
    perubahan yang dilakukan.
    """
    import glob
    import os
    import stat

    try:
        import playwright
    except ImportError:
        return False

    pw_root = os.path.dirname(playwright.__file__)
    candidates = []

    # Binari node driver
    candidates += glob.glob(os.path.join(pw_root, "driver", "node"))
    candidates += glob.glob(
        os.path.join(pw_root, "driver", "**", "node"), recursive=True
    )

    # Browser shell (chromium / headless shell) di cache lokal
    home = os.path.expanduser("~")
    for base in (
        os.path.join(home, ".cache", "ms-playwright"),
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
    ):
        if base and os.path.isdir(base):
            for name in ("chrome", "headless_shell", "chrome_crashpad_handler"):
                candidates += glob.glob(
                    os.path.join(base, "**", name), recursive=True
                )

    fixed = False
    for path in candidates:
        try:
            if os.path.isfile(path):
                st = os.stat(path)
                os.chmod(
                    path,
                    st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
                )
                fixed = True
        except Exception as exc:
            logger.warning("[DO Claimer] Gagal chmod %s: %s", path, exc)

    if fixed:
        logger.info("[DO Claimer] Izin eksekusi binari Playwright diperbaiki.")
    return fixed


async def _launch_browser(pw):
    """Luncurkan chromium; bila kena Permission denied, coba perbaiki izin lalu retry."""
    try:
        return await pw.chromium.launch(**_launch_kwargs())
    except Exception as exc:
        msg = str(exc).lower()
        if "permission denied" in msg or "errno 13" in msg:
            logger.warning(
                "[DO Claimer] Permission denied saat launch browser, mencoba perbaiki izin..."
            )
            if _fix_playwright_permissions():
                return await pw.chromium.launch(**_launch_kwargs())
            raise PermissionError(
                "Browser automation tidak bisa dijalankan: izin eksekusi binari "
                "Playwright ditolak server (Errno 13). Hosting perlu mengizinkan "
                "eksekusi file di ~/.cache/ms-playwright & folder driver Playwright, "
                "atau jalankan ulang: playwright install chromium."
            ) from exc
        raise


def _common_context_options() -> dict:
    return {
        "viewport": {"width": 1280, "height": 800},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "locale": "en-US",
    }


async def _new_clean_context(browser):
    """
    Buat context baru yang dijamin bersih (tanpa cookie/sesi lama).

    Setiap klaim memakai context terisolasi; clear_cookies dipanggil eksplisit
    sebagai jaring pengaman tambahan.
    """
    context = await browser.new_context(**_common_context_options())
    try:
        await context.clear_cookies()
    except Exception:
        pass
    context.set_default_timeout(TIMEOUT)
    context.set_default_navigation_timeout(NAV_TIMEOUT)
    return context


# ------------------------------------------------------------------
# Cookie normalizer (Cookie-Editor JSON → Playwright)
# ------------------------------------------------------------------


def _normalise_cookies(cookies: list) -> list:
    """Normalisasi cookie format Cookie-Editor agar diterima Playwright."""
    SAME_SITE_MAP = {
        "none": "None",
        "lax": "Lax",
        "strict": "Strict",
        "no_restriction": "None",
        "unspecified": "Lax",
    }
    UNWANTED_KEYS = {"hostOnly", "session", "storeId", "id"}

    normalized = []
    for cookie in cookies:
        c: dict = {}
        for k, v in cookie.items():
            if k in UNWANTED_KEYS:
                continue
            if k == "sameSite":
                c["sameSite"] = SAME_SITE_MAP.get(str(v).lower(), "Lax")
            elif k == "expirationDate":
                c["expires"] = float(v)
            else:
                c[k] = v
        if "sameSite" not in c:
            c["sameSite"] = "Lax"
        normalized.append(c)
    return normalized


# ------------------------------------------------------------------
# Helper: submit kode 2FA GitHub bila halaman OTP muncul
# ------------------------------------------------------------------


async def _maybe_handle_github_2fa(
    page, totp_secret: str
) -> Optional[ClaimResult]:
    """
    Deteksi & tangani halaman 2FA GitHub.

    Return:
        None                    — bukan halaman 2FA, atau 2FA sukses ditangani
        ClaimResult(False, ...) — 2FA dibutuhkan tapi gagal (secret kosong, dll)
    """
    url_now = page.url.lower()
    is_2fa = any(
        kw in url_now
        for kw in ("two-factor", "sessions/two-factor", "/two_factor", "totp")
    )
    # Beberapa halaman 2FA tidak ubah URL — cek juga keberadaan field OTP
    if not is_2fa:
        is_2fa = await _any_visible(page, GH_OTP_SELECTORS, timeout=2_000)

    if not is_2fa:
        return None

    logger.info("[DO Claimer] Halaman 2FA GitHub terdeteksi: %s", page.url)

    if not totp_secret:
        return ClaimResult(
            False,
            "Akun GHS memerlukan 2FA tetapi TOTP secret tidak tersedia.\n"
            "Pastikan format akun: email:password:TOTP_SECRET",
            await _safe_screenshot(page),
        )

    totp_code = _gen_totp(totp_secret)
    if not await _try_fill(page, GH_OTP_SELECTORS, totp_code, timeout=8_000):
        return ClaimResult(
            False,
            "Field kode OTP GitHub tidak ditemukan di halaman 2FA.",
            await _safe_screenshot(page),
        )

    # GitHub TOTP biasanya auto-submit; klik tombol sebagai cadangan
    await _try_click(
        page,
        ['button[type="submit"]', 'input[type="submit"]'],
        timeout=3_000,
    )
    await asyncio.sleep(3)
    return None


# ------------------------------------------------------------------
# GitHub GHS login (langkah 1)
# ------------------------------------------------------------------


async def _login_github(
    page, email: str, password: str, totp_secret: str
) -> Optional[ClaimResult]:
    """
    Login ke github.com sebagai akun GHS (penjual), termasuk 2FA bila ada.

    Return None jika sukses, ClaimResult(False, ...) jika gagal.
    """
    try:
        await page.goto(GITHUB_LOGIN_URL, timeout=NAV_TIMEOUT)
        await asyncio.sleep(2)
    except Exception as exc:
        return ClaimResult(False, f"Tidak bisa membuka halaman login GitHub: {exc}")

    if not await _try_fill(page, GH_USERNAME_SELECTORS, email):
        return ClaimResult(
            False,
            "Field username/email GitHub tidak ditemukan (mungkin ada CAPTCHA).",
            await _safe_screenshot(page),
        )
    if not await _try_fill(page, GH_PASSWORD_SELECTORS, password):
        return ClaimResult(
            False,
            "Field password GitHub tidak ditemukan.",
            await _safe_screenshot(page),
        )
    if not await _try_click(page, GH_SUBMIT_SELECTORS):
        return ClaimResult(
            False,
            "Tombol Sign In GitHub tidak ditemukan.",
            await _safe_screenshot(page),
        )

    await asyncio.sleep(4)

    # Tangani 2FA bila muncul
    twofa_err = await _maybe_handle_github_2fa(page, totp_secret)
    if twofa_err:
        return twofa_err

    # Cek pesan error login GitHub
    try:
        err_loc = page.locator(
            "#js-flash-container .flash-error, "
            "[class*='flash-error'], "
            "#js-flash-container [role='alert']"
        ).first
        if await err_loc.is_visible(timeout=2_000):
            err_text = (await err_loc.text_content() or "").strip()
            if err_text:
                return ClaimResult(
                    False,
                    f"Login GitHub gagal: {err_text[:200]}",
                    await _safe_screenshot(page),
                )
    except Exception:
        pass

    current_url = page.url.lower()
    if "github.com/login" in current_url or "github.com/session" in current_url:
        return ClaimResult(
            False,
            "Login GitHub gagal. Email atau password akun GHS salah.",
            await _safe_screenshot(page),
        )

    logger.info("[DO Claimer] Login GitHub GHS sukses. URL: %s", page.url)
    return None


# ------------------------------------------------------------------
# Skip survei "Welcome to GitHub Education!" (langkah 3)
# ------------------------------------------------------------------


async def _skip_education_survey(page) -> None:
    """
    Jika halaman menampilkan survei onboarding GitHub Education,
    klik "Skip this question" berulang lalu buka ulang halaman pack.

    Aman dipanggil walau survei tidak muncul (no-op).
    """
    if not await _any_visible(page, GH_SURVEY_MARKERS, timeout=3_000):
        return

    logger.info("[DO Claimer] Survei GitHub Education terdeteksi, men-skip...")

    # Survei punya beberapa pertanyaan — klik skip beberapa kali
    for _ in range(4):
        clicked = await _try_click(page, GH_SURVEY_SKIP_SELECTORS, timeout=4_000)
        if not clicked:
            break
        await asyncio.sleep(2)

    # Buka ulang halaman pack sesuai instruksi alur asli
    try:
        await page.goto(GH_EDUCATION_PACK_URL, timeout=NAV_TIMEOUT)
        await asyncio.sleep(3)
    except Exception:
        pass


# ------------------------------------------------------------------
# Buka pack & klik offer DigitalOcean (langkah 2 & 4)
# ------------------------------------------------------------------


async def _open_do_offer(page, context) -> Tuple[Optional[object], Optional[str]]:
    """
    Buka GitHub Education Pack, skip survei bila ada, lalu klik link offer
    DigitalOcean. Offer biasanya membuka tab baru.

    Return:
        (do_page, None)        — sukses; Page object yang berada di DigitalOcean
        (None, error_message)  — gagal
    """
    try:
        await page.goto(GH_EDUCATION_PACK_URL, timeout=NAV_TIMEOUT)
        await asyncio.sleep(3)
    except Exception as exc:
        return None, f"Tidak bisa membuka GitHub Education Pack: {exc}"

    # Skip survei onboarding bila muncul (akan buka ulang pack)
    await _skip_education_survey(page)

    # Deteksi pesan ketidaklayakan akun di halaman pack
    page_txt = await _page_text(page)
    for kw in GH_OFFER_NEGATIVE:
        if kw in page_txt:
            return None, (
                f"Akun GHS bermasalah untuk klaim DigitalOcean (terdeteksi: '{kw}').\n"
                "Akun mungkin sudah pernah klaim, belum terverifikasi student, "
                "atau offer tidak tersedia."
            )

    # Pasang listener tab baru sebelum klik
    new_page_holder: list = []
    context.on("page", lambda p: new_page_holder.append(p))

    # Scroll agar kartu offer ter-render, lalu klik link DigitalOcean
    try:
        loc = None
        for sel in GH_DO_OFFER_SELECTORS:
            cand = page.locator(sel).first
            try:
                await cand.wait_for(state="attached", timeout=4_000)
                loc = cand
                break
            except Exception:
                continue

        if loc is None:
            return None, (
                "Link offer DigitalOcean tidak ditemukan di halaman pack.\n"
                "Tampilan halaman mungkin berubah atau akun tidak punya akses offer ini."
            )

        try:
            await loc.scroll_into_view_if_needed(timeout=4_000)
        except Exception:
            pass
        await loc.click()
    except Exception as exc:
        return None, f"Gagal mengklik offer DigitalOcean: {exc}"

    await asyncio.sleep(5)

    # Cek tab baru (offer pakai target="_blank")
    do_page = None
    if new_page_holder:
        cand = new_page_holder[-1]
        try:
            await cand.wait_for_load_state("domcontentloaded", timeout=15_000)
            await asyncio.sleep(2)
        except Exception:
            pass
        if "digitalocean.com" in cand.url:
            do_page = cand

    # Atau redirect di tab yang sama
    if do_page is None and "digitalocean.com" in page.url:
        do_page = page

    # Kadang butuh waktu lebih untuk navigasi
    if do_page is None:
        await asyncio.sleep(4)
        if new_page_holder and "digitalocean.com" in new_page_holder[-1].url:
            do_page = new_page_holder[-1]
        elif "digitalocean.com" in page.url:
            do_page = page

    if do_page is None:
        return None, (
            "Setelah klik offer, tidak diarahkan ke DigitalOcean.\n"
            "Kemungkinan GitHub mendeteksi otomasi (CAPTCHA) atau offer berubah."
        )

    logger.info("[DO Claimer] Tiba di DigitalOcean: %s", do_page.url)
    return do_page, None


# ------------------------------------------------------------------
# DigitalOcean: dismiss cookie consent
# ------------------------------------------------------------------


async def _dismiss_do_consent(page) -> None:
    """Klik 'Agree & Proceed' pada banner cookie DO bila muncul (no-op jika tidak)."""
    try:
        await _try_click(page, DO_CONSENT_SELECTORS, timeout=3_000)
    except Exception:
        pass


# ------------------------------------------------------------------
# DigitalOcean: login buyer + 2FA (langkah 5)
# ------------------------------------------------------------------


async def _login_do_if_needed(
    page, do_email: str, do_password: str, do_totp: str
) -> Optional[ClaimResult]:
    """
    Jika halaman DigitalOcean meminta login, isi kredensial buyer + 2FA.

    Aman bila sesi DO sudah aktif (mis. via cookies) — akan langsung return None.

    Return None jika sukses / tidak perlu login, ClaimResult(False,...) jika gagal.
    """
    await _dismiss_do_consent(page)

    url = page.url.lower()
    needs_login = "login" in url or await _any_visible(
        page, DO_EMAIL_SELECTORS, timeout=3_000
    )

    if not needs_login:
        return None  # sudah login / langsung ke halaman authenticate

    if not (do_email and do_password):
        return ClaimResult(
            False,
            "DigitalOcean meminta login tetapi kredensial buyer tidak tersedia.\n"
            "Gunakan metode email+password, atau cookies yang masih valid.",
            await _safe_screenshot(page),
        )

    logger.info("[DO Claimer] DigitalOcean meminta login buyer: %s", do_email)

    if not await _try_fill(page, DO_EMAIL_SELECTORS, do_email):
        return ClaimResult(
            False,
            "Field email DigitalOcean tidak ditemukan.",
            await _safe_screenshot(page),
        )
    if not await _try_fill(page, DO_PASSWORD_SELECTORS, do_password):
        return ClaimResult(
            False,
            "Field password DigitalOcean tidak ditemukan.",
            await _safe_screenshot(page),
        )
    if not await _try_click(page, DO_LOGIN_SUBMIT_SELECTORS):
        return ClaimResult(
            False,
            "Tombol Log In DigitalOcean tidak ditemukan.",
            await _safe_screenshot(page),
        )

    await asyncio.sleep(4)

    # 2FA DigitalOcean
    twofa_err = await _maybe_handle_do_2fa(page, do_totp)
    if twofa_err:
        return twofa_err

    # Cek apakah masih di halaman login (kredensial salah)
    if "login" in page.url.lower() and await _any_visible(
        page, DO_EMAIL_SELECTORS, timeout=2_000
    ):
        return ClaimResult(
            False,
            "Login DigitalOcean gagal. Email/password buyer salah, "
            "atau butuh verifikasi tambahan.",
            await _safe_screenshot(page),
        )

    logger.info("[DO Claimer] Login DigitalOcean buyer sukses. URL: %s", page.url)
    return None


async def _maybe_handle_do_2fa(page, do_totp: str) -> Optional[ClaimResult]:
    """
    Deteksi & tangani halaman 2FA DigitalOcean ("Two-factor authentication").

    Return None jika bukan 2FA / sukses, ClaimResult(False,...) jika gagal.
    """
    url = page.url.lower()
    is_2fa = any(kw in url for kw in ("two-factor", "two_factor", "/2fa", "mfa"))
    if not is_2fa:
        is_2fa = await _any_visible(page, DO_OTP_SELECTORS, timeout=2_000)

    if not is_2fa:
        return None

    logger.info("[DO Claimer] Halaman 2FA DigitalOcean terdeteksi.")

    if not do_totp:
        return ClaimResult(
            False,
            "Akun DigitalOcean buyer memerlukan 2FA tetapi TOTP secret tidak ada.\n"
            "Sertakan TOTP secret DO, atau gunakan metode cookies.",
            await _safe_screenshot(page),
        )

    code = _gen_totp(do_totp)
    if not await _try_fill(page, DO_OTP_SELECTORS, code, timeout=8_000):
        return ClaimResult(
            False,
            "Field kode 2FA DigitalOcean tidak ditemukan.",
            await _safe_screenshot(page),
        )

    await _try_click(page, DO_OTP_SUBMIT_SELECTORS, timeout=4_000)
    await asyncio.sleep(4)
    return None


# ------------------------------------------------------------------
# Langkah 6 & 7: Authenticate with GitHub + Authorize DigitalOcean
# ------------------------------------------------------------------


async def _complete_oauth_authorize(page, context) -> Optional[ClaimResult]:
    """
    Tangani halaman:
      6. DigitalOcean "Authenticate with GitHub" → klik
      7. GitHub "Authorize DigitalOcean Education" → klik "Authorize digitalocean"

    Return None jika lolos (atau langkah tidak diperlukan), error bila gagal.
    """
    # --- Langkah 6: "Authenticate with GitHub" (di domain DO) ---
    page_txt = await _page_text(page)
    if "authenticate with github" in page_txt:
        logger.info("[DO Claimer] Halaman 'Authenticate with GitHub' terdeteksi.")
        await _try_click(page, DO_AUTH_GITHUB_SELECTORS, timeout=8_000)
        await asyncio.sleep(4)

    # --- Langkah 7: GitHub OAuth authorize ---
    # Tunggu hingga halaman authorize muncul (atau langsung di-skip GitHub).
    for _ in range(3):
        url = page.url.lower()
        page_txt = await _page_text(page)
        on_authorize = (
            "github.com/login/oauth/authorize" in url
            or "authorize digitalocean" in page_txt
            or "wants to access" in page_txt
        )
        if on_authorize:
            logger.info("[DO Claimer] Halaman Authorize GitHub terdeteksi.")
            # Tombol authorize bisa disabled beberapa detik (anti-clickjacking)
            await _wait_authorize_enabled(page)
            clicked = await _try_click(page, GH_AUTHORIZE_SELECTORS, timeout=10_000)
            if not clicked:
                return ClaimResult(
                    False,
                    "Tombol 'Authorize digitalocean' tidak bisa diklik.",
                    await _safe_screenshot(page),
                )
            await asyncio.sleep(5)
            break
        await asyncio.sleep(3)

    return None


async def _wait_authorize_enabled(page, max_wait: int = 12) -> None:
    """Tunggu tombol Authorize GitHub tidak lagi disabled (anti-clickjacking)."""
    for _ in range(max_wait):
        try:
            btn = page.locator(GH_AUTHORIZE_SELECTORS[0]).first
            if await btn.count() > 0:
                disabled = await btn.get_attribute("disabled")
                if disabled is None:
                    return
        except Exception:
            pass
        await asyncio.sleep(1)


# ------------------------------------------------------------------
# Langkah 8: Verifikasi sukses ("Happy Coding!")
# ------------------------------------------------------------------


async def _verify_success(page) -> ClaimResult:
    """
    Setelah authorize, tunggu redirect ke DigitalOcean dan deteksi
    "GitHub Student Pack Applied" / "Happy Coding!".
    """
    # Beri waktu redirect & render modal
    for _ in range(6):
        txt = await _page_text(page)
        if any(kw in txt for kw in DO_SUCCESS_KEYWORDS):
            screenshot = await _safe_screenshot(page)
            # Tutup modal sukses bila ada (opsional)
            await _try_click(page, DO_GOT_IT_SELECTORS, timeout=3_000)
            return ClaimResult(
                True,
                "✅ *GitHub Student Pack Applied!*\n\n"
                "💰 Akun DigitalOcean kamu sudah dikreditkan $200 "
                "(berlaku 1 tahun).\n"
                "🎉 *Happy Coding!*\n\n"
                "Cek saldo di: https://cloud.digitalocean.com/account/billing",
                screenshot,
            )
        await asyncio.sleep(3)

    # Tidak ketemu kata kunci sukses
    txt = await _page_text(page)
    for kw in GH_OFFER_NEGATIVE:
        if kw in txt:
            return ClaimResult(
                False,
                f"Klaim tidak berhasil (terdeteksi: '{kw}'). "
                "Akun GHS mungkin sudah pernah klaim DigitalOcean.",
                await _safe_screenshot(page),
            )

    return ClaimResult(
        False,
        "⚠️ Tidak menemukan konfirmasi 'Happy Coding!' setelah proses authorize.\n"
        "Proses mungkin terhenti di langkah verifikasi GitHub/DigitalOcean.\n"
        "Cek manual di: https://cloud.digitalocean.com/account/billing",
        await _safe_screenshot(page),
    )


# ------------------------------------------------------------------
# Orkestrasi alur klaim (setelah GitHub GHS sudah login)
# ------------------------------------------------------------------


async def _inner_claim(
    page,
    context,
    ghs_email: str,
    ghs_password: str,
    ghs_totp: str,
    do_email: str = "",
    do_password: str = "",
    do_totp: str = "",
) -> ClaimResult:
    """
    Alur inti OAuth (langkah 1–8):
      1. Login GitHub GHS (+2FA)
      2. Buka pack → skip survei → klik offer DigitalOcean
      3. DO login buyer (+2FA) bila diminta
      4. Authenticate with GitHub → Authorize digitalocean
      5. Verifikasi "Happy Coding!"
    """
    # Langkah 1
    logger.info("[DO Claimer] Login GitHub sebagai GHS: %s", ghs_email)
    gh_err = await _login_github(page, ghs_email, ghs_password, ghs_totp)
    if gh_err:
        return gh_err

    # Langkah 2 & 4 (buka pack + klik offer)
    logger.info("[DO Claimer] Membuka offer DigitalOcean di pack...")
    do_page, nav_err = await _open_do_offer(page, context)
    if nav_err:
        return ClaimResult(False, nav_err, await _safe_screenshot(page))
    if do_page is None:
        return ClaimResult(
            False, "Halaman DigitalOcean tidak tersedia.", await _safe_screenshot(page)
        )

    # Langkah 5 (login DO buyer + 2FA bila diminta)
    do_login_err = await _login_do_if_needed(do_page, do_email, do_password, do_totp)
    if do_login_err:
        return do_login_err

    # Langkah 6 & 7 (authenticate + authorize)
    oauth_err = await _complete_oauth_authorize(do_page, context)
    if oauth_err:
        return oauth_err

    # Langkah 8 (verifikasi sukses)
    return await _verify_success(do_page)


# ------------------------------------------------------------------
# Public API — dipanggil dari handlers/do_claim.py
# ------------------------------------------------------------------


async def claim_do_credit_with_email(
    do_email: str,
    do_password: str,
    ghs_email: str,
    ghs_password: str,
    ghs_totp_secret: str = "",
    do_totp_secret: str = "",
) -> ClaimResult:
    """
    Klaim GitHub Student Pack → DigitalOcean $200 dengan login DO via
    email + password (opsional + TOTP 2FA DigitalOcean).

    Browser & context dibuat baru (cookie bersih) untuk setiap pemanggilan.
    """
    async_playwright, _using_patch = _import_async_playwright()
    if async_playwright is None:
        return ClaimResult(
            False,
            "Library 'playwright/patchright' tidak terinstall di server.\n"
            "Jalankan: pip install patchright && patchright install chromium",
        )

    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
        context = await _new_clean_context(browser)
        page = await context.new_page()
        try:
            logger.info(
                "[DO Claimer] Mulai klaim (email). DO buyer: %s | GHS: %s",
                do_email,
                ghs_email,
            )
            return await _inner_claim(
                page,
                context,
                ghs_email,
                ghs_password,
                ghs_totp_secret,
                do_email=do_email,
                do_password=do_password,
                do_totp=do_totp_secret,
            )
        except Exception as exc:
            logger.exception("[DO Claimer] Error tak terduga (email)")
            return ClaimResult(
                False, f"❌ Error tak terduga: {exc}", await _safe_screenshot(page)
            )
        finally:
            await context.close()
            await browser.close()


async def claim_do_credit_with_cookies(
    do_cookies: list,
    ghs_email: str,
    ghs_password: str,
    ghs_totp_secret: str = "",
) -> ClaimResult:
    """
    Klaim GitHub Student Pack → DigitalOcean $200 dengan sesi DO buyer dari
    cookies (Cookie-Editor JSON). Berguna bila akun DO buyer memakai 2FA.

    Browser & context dibuat baru; cookies buyer diinjeksi sebagai sesi DO.
    """
    async_playwright, _using_patch = _import_async_playwright()
    if async_playwright is None:
        return ClaimResult(
            False,
            "Library 'playwright/patchright' tidak terinstall di server.\n"
            "Jalankan: pip install patchright && patchright install chromium",
        )

    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
        context = await _new_clean_context(browser)
        page = await context.new_page()
        try:
            logger.info(
                "[DO Claimer] Mulai klaim (cookies, %d cookie). GHS: %s",
                len(do_cookies),
                ghs_email,
            )
            normalized = _normalise_cookies(do_cookies)
            await context.add_cookies(normalized)
            logger.info("[DO Claimer] %d cookie DO diinjeksi.", len(normalized))

            return await _inner_claim(
                page,
                context,
                ghs_email,
                ghs_password,
                ghs_totp_secret,
                # tanpa do_email/do_password — sesi DO via cookies
            )
        except Exception as exc:
            logger.exception("[DO Claimer] Error tak terduga (cookies)")
            return ClaimResult(
                False, f"❌ Error tak terduga: {exc}", await _safe_screenshot(page)
            )
        finally:
            await context.close()
            await browser.close()


# ==================================================================
# JASA KLAIM KUPON DIGITALOCEAN
# ------------------------------------------------------------------
# Alur (sesuai spesifikasi):
#   1. Auto-login DigitalOcean (WAJIB dukung 2FA TOTP).
#   2. Buka halaman account billing.
#   3. Cari elemen promo code, isi kode, lalu submit.
#   4. FALLBACK bila submit gagal: setelah input kode → Tab → Enter
#      (promo otomatis ter-apply).
#   5. VERIFIKASI: cek tabel promo code; bila sudah ADA DUA kupon
#      ter-apply, klaim dianggap berhasil.
#   6. Sukses → langsung tutup/buang sesi.
# ==================================================================


async def _count_promo_rows(page) -> int:
    """Hitung jumlah baris kupon/promo yang ter-apply di tabel billing.

    Tabel "Promos and Credits" punya kolom Description/Expiration/Initial/
    Amount Remaining. Setiap kupon ter-apply = satu baris <tr> di tbody.
    Beberapa selector dicoba agar robust terhadap perubahan markup.
    """
    candidates = [
        "table tbody tr",
        '[data-testid*="promo" i] tbody tr',
        '[class*="promo" i] tbody tr',
        '[class*="credit" i] tbody tr',
    ]
    best = 0
    for sel in candidates:
        try:
            n = await page.locator(sel).count()
            if n > best:
                best = n
        except Exception:
            continue
    return best


async def _login_do_direct(
    page, do_email: str, do_password: str, do_totp: str
) -> Optional[ClaimResult]:
    """Login langsung ke cloud.digitalocean.com/login (bukan via OAuth GitHub).

    Mendukung 2FA TOTP. Return None bila sukses, ClaimResult(False,...) bila gagal.
    """
    try:
        await page.goto(DO_LOGIN_URL, timeout=NAV_TIMEOUT)
        await asyncio.sleep(2)
    except Exception as exc:
        return ClaimResult(False, f"Tidak bisa membuka halaman login DigitalOcean: {exc}")

    await _dismiss_do_consent(page)

    # Bila cookie sesi sudah aktif, DO akan redirect keluar dari /login
    if "login" not in page.url.lower() and not await _any_visible(
        page, DO_EMAIL_SELECTORS, timeout=3_000
    ):
        logger.info("[DO Coupon] Sesi DO sudah aktif (tanpa login ulang).")
        return None

    if not (do_email and do_password):
        return ClaimResult(
            False,
            "Kredensial DigitalOcean tidak tersedia untuk login.",
            await _safe_screenshot(page),
        )

    logger.info("[DO Coupon] Login DigitalOcean: %s", do_email)

    if not await _try_fill(page, DO_EMAIL_SELECTORS, do_email):
        return ClaimResult(
            False,
            "Field email DigitalOcean tidak ditemukan (mungkin ada CAPTCHA).",
            await _safe_screenshot(page),
        )
    if not await _try_fill(page, DO_PASSWORD_SELECTORS, do_password):
        return ClaimResult(
            False,
            "Field password DigitalOcean tidak ditemukan.",
            await _safe_screenshot(page),
        )
    if not await _try_click(page, DO_LOGIN_SUBMIT_SELECTORS):
        return ClaimResult(
            False,
            "Tombol Log In DigitalOcean tidak ditemukan.",
            await _safe_screenshot(page),
        )

    await asyncio.sleep(4)

    # 2FA DigitalOcean (TOTP)
    twofa_err = await _maybe_handle_do_2fa(page, do_totp)
    if twofa_err:
        return twofa_err

    # Masih di halaman login → kredensial salah
    if "login" in page.url.lower() and await _any_visible(
        page, DO_EMAIL_SELECTORS, timeout=2_000
    ):
        # Cek pesan error eksplisit
        try:
            err = page.locator('[class*="error" i], [role="alert"]').first
            if await err.is_visible(timeout=2_000):
                msg = (await err.text_content() or "").strip()
                if msg:
                    return ClaimResult(
                        False,
                        f"Login DigitalOcean gagal: {msg[:200]}",
                        await _safe_screenshot(page),
                    )
        except Exception:
            pass
        return ClaimResult(
            False,
            "Login DigitalOcean gagal. Email/password salah atau butuh verifikasi tambahan.",
            await _safe_screenshot(page),
        )

    logger.info("[DO Coupon] Login DigitalOcean sukses. URL: %s", page.url)
    return None


async def _apply_coupon(page, promo_code: str) -> ClaimResult:
    """Buka billing, isi promo code, submit (dengan fallback Tab+Enter),
    lalu verifikasi via tabel promo (≥2 kupon = sukses).
    """
    # --- Buka halaman billing ---
    try:
        await page.goto(DO_BILLING_URL, timeout=NAV_TIMEOUT)
        await asyncio.sleep(3)
    except Exception as exc:
        return ClaimResult(
            False, f"Tidak bisa membuka halaman billing DigitalOcean: {exc}",
            await _safe_screenshot(page),
        )

    await _dismiss_do_consent(page)

    # Hitung jumlah kupon SEBELUM apply (baseline)
    before = await _count_promo_rows(page)
    logger.info("[DO Coupon] Jumlah baris promo sebelum apply: %d", before)

    # --- Cari & isi field promo code ---
    promo_loc = None
    for sel in DO_PROMO_INPUT_SELECTORS:
        cand = page.locator(sel).first
        try:
            await cand.wait_for(state="visible", timeout=6_000)
            promo_loc = cand
            break
        except Exception:
            continue

    if promo_loc is None:
        return ClaimResult(
            False,
            "Field promo code tidak ditemukan di halaman billing.\n"
            "Tampilan DigitalOcean mungkin berubah, atau akun belum punya "
            "metode pembayaran terdaftar.",
            await _safe_screenshot(page),
        )

    try:
        await promo_loc.click()
        await promo_loc.fill("")
        await promo_loc.fill(promo_code)
        await asyncio.sleep(1)
    except Exception as exc:
        return ClaimResult(
            False, f"Gagal mengisi promo code: {exc}", await _safe_screenshot(page)
        )

    logger.info("[DO Coupon] Promo code diisi: %s", promo_code)

    # --- Submit: coba tombol dulu, lalu fallback Tab+Enter ---
    submitted = await _try_click(page, DO_PROMO_SUBMIT_SELECTORS, timeout=5_000)
    if submitted:
        logger.info("[DO Coupon] Tombol submit promo diklik.")
    else:
        # FALLBACK: Tab lalu Enter — promo otomatis ter-apply
        logger.info("[DO Coupon] Tombol submit tidak ada, fallback Tab+Enter.")
        try:
            await promo_loc.press("Tab")
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
        except Exception:
            try:
                await promo_loc.press("Enter")
            except Exception:
                pass

    await asyncio.sleep(5)

    # --- Verifikasi: cek tabel promo (≥2 kupon ter-apply = sukses) ---
    # Reload halaman billing agar tabel menampilkan kupon terbaru.
    try:
        await page.goto(DO_BILLING_URL, timeout=NAV_TIMEOUT)
        await asyncio.sleep(3)
        await _dismiss_do_consent(page)
    except Exception:
        pass

    after = 0
    # Beri beberapa kesempatan; tabel kadang lambat render.
    for _ in range(4):
        after = await _count_promo_rows(page)
        if after >= 2 or after > before:
            break
        await asyncio.sleep(3)

    logger.info(
        "[DO Coupon] Jumlah baris promo setelah apply: %d (sebelum: %d)",
        after, before,
    )

    screenshot = await _safe_screenshot(page)
    page_txt = await _page_text(page)

    # Deteksi pesan error eksplisit dari DigitalOcean
    promo_errors = [
        "invalid promo code",
        "promo code is invalid",
        "code is not valid",
        "expired",
        "already been used",
        "cannot be applied",
    ]
    matched_err = next((kw for kw in promo_errors if kw in page_txt), None)

    # Sukses bila sudah ada ≥2 kupon ter-apply ATAU jumlah baris bertambah
    if after >= 2 or (before > 0 and after > before):
        return ClaimResult(
            True,
            "✅ *Kupon DigitalOcean Berhasil Diklaim!*\n\n"
            f"💰 Promo `{promo_code}` ter-apply ke akun.\n"
            f"📊 Total kupon/kredit aktif: *{after}*\n\n"
            "Cek di: https://cloud.digitalocean.com/account/billing",
            screenshot,
        )

    if matched_err:
        return ClaimResult(
            False,
            f"Kupon gagal diklaim (terdeteksi: '{matched_err}').\n"
            f"Kode: `{promo_code}`",
            screenshot,
        )

    return ClaimResult(
        False,
        "⚠️ Tidak dapat memverifikasi kupon ter-apply.\n"
        f"Jumlah kupon terdeteksi: {after} (dibutuhkan ≥2).\n"
        f"Kode: `{promo_code}`\n"
        "Cek manual di: https://cloud.digitalocean.com/account/billing",
        screenshot,
    )


async def claim_coupon_with_email(
    do_email: str,
    do_password: str,
    promo_code: str,
    do_totp_secret: str = "",
) -> ClaimResult:
    """Jasa klaim kupon DigitalOcean via login email+password (+2FA TOTP).

    Browser & context dibuat baru (cookie bersih); sesi langsung dibuang
    setelah selesai.
    """
    async_playwright, _using_patch = _import_async_playwright()
    if async_playwright is None:
        return ClaimResult(
            False,
            "Library 'playwright/patchright' tidak terinstall di server.\n"
            "Jalankan: pip install patchright && patchright install chromium",
        )

    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
        context = await _new_clean_context(browser)
        page = await context.new_page()
        try:
            logger.info("[DO Coupon] Mulai klaim kupon. DO: %s", do_email)
            login_err = await _login_do_direct(
                page, do_email, do_password, do_totp_secret
            )
            if login_err:
                return login_err
            return await _apply_coupon(page, promo_code)
        except Exception as exc:
            logger.exception("[DO Coupon] Error tak terduga (email)")
            return ClaimResult(
                False, f"❌ Error tak terduga: {exc}", await _safe_screenshot(page)
            )
        finally:
            # Buang sesi segera setelah selesai
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


async def claim_coupon_with_cookies(
    do_cookies: list,
    promo_code: str,
) -> ClaimResult:
    """Jasa klaim kupon DigitalOcean memakai sesi dari cookies (Cookie-Editor JSON).

    Berguna bila akun DO memakai 2FA dan user lebih nyaman kirim cookies.
    """
    async_playwright, _using_patch = _import_async_playwright()
    if async_playwright is None:
        return ClaimResult(
            False,
            "Library 'playwright/patchright' tidak terinstall di server.\n"
            "Jalankan: pip install patchright && patchright install chromium",
        )

    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
        context = await _new_clean_context(browser)
        page = await context.new_page()
        try:
            logger.info("[DO Coupon] Mulai klaim kupon (cookies, %d cookie).", len(do_cookies))
            normalized = _normalise_cookies(do_cookies)
            await context.add_cookies(normalized)
            return await _apply_coupon(page, promo_code)
        except Exception as exc:
            logger.exception("[DO Coupon] Error tak terduga (cookies)")
            return ClaimResult(
                False, f"❌ Error tak terduga: {exc}", await _safe_screenshot(page)
            )
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


# ==================================================================
# BULK: klaim beberapa promo code dalam SATU sesi login
# ------------------------------------------------------------------
# Login DigitalOcean sekali, lalu apply tiap kode promo berurutan.
# Mengembalikan list[ClaimResult] (satu hasil per kode).
# ==================================================================


async def _apply_coupons_multi(page, promo_codes: list) -> list:
    """Apply beberapa promo code dalam satu sesi. Return list[ClaimResult]."""
    results = []
    for idx, code in enumerate(promo_codes, start=1):
        logger.info("[DO Coupon] Bulk apply %d/%d: %s", idx, len(promo_codes), code)
        try:
            r = await _apply_coupon(page, code)
        except Exception as exc:
            logger.exception("[DO Coupon] Bulk apply error untuk %s", code)
            r = ClaimResult(False, f"Error saat apply `{code}`: {exc}", None)
        results.append(r)
        await asyncio.sleep(2)
    return results


async def claim_coupons_with_email(
    do_email: str,
    do_password: str,
    promo_codes: list,
    do_totp_secret: str = "",
) -> list:
    """Bulk klaim kupon via login email+password (+2FA). Return list[ClaimResult].

    Bila login gagal, kembalikan satu ClaimResult(False) untuk SETIAP kode
    agar caller bisa menghitung jumlah gagal dengan benar.
    """
    async_playwright, _ = _import_async_playwright()
    if async_playwright is None:
        err = ClaimResult(
            False,
            "Library 'playwright/patchright' tidak terinstall di server.",
        )
        return [err for _ in promo_codes]

    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
        context = await _new_clean_context(browser)
        page = await context.new_page()
        try:
            login_err = await _login_do_direct(
                page, do_email, do_password, do_totp_secret
            )
            if login_err:
                return [login_err for _ in promo_codes]
            return await _apply_coupons_multi(page, promo_codes)
        except Exception as exc:
            logger.exception("[DO Coupon] Bulk error (email)")
            shot = await _safe_screenshot(page)
            return [
                ClaimResult(False, f"❌ Error tak terduga: {exc}", shot)
                for _ in promo_codes
            ]
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


async def claim_coupons_with_cookies(
    do_cookies: list,
    promo_codes: list,
) -> list:
    """Bulk klaim kupon via cookies. Return list[ClaimResult]."""
    async_playwright, _ = _import_async_playwright()
    if async_playwright is None:
        err = ClaimResult(
            False,
            "Library 'playwright/patchright' tidak terinstall di server.",
        )
        return [err for _ in promo_codes]

    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
        context = await _new_clean_context(browser)
        page = await context.new_page()
        try:
            normalized = _normalise_cookies(do_cookies)
            await context.add_cookies(normalized)
            return await _apply_coupons_multi(page, promo_codes)
        except Exception as exc:
            logger.exception("[DO Coupon] Bulk error (cookies)")
            shot = await _safe_screenshot(page)
            return [
                ClaimResult(False, f"❌ Error tak terduga: {exc}", shot)
                for _ in promo_codes
            ]
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


# ==================================================================
# BULK: klaim SATU promo code ke BANYAK akun DigitalOcean
# ------------------------------------------------------------------
# Tiap akun memakai browser+context BARU (sesi bersih, terisolasi).
# Mengembalikan list[ClaimResult] (satu hasil per akun, urut sesuai input).
# ==================================================================


async def claim_coupon_bulk_emails(
    accounts: list,
    promo_code: str,
) -> list:
    """Bulk klaim promo ke banyak akun DO via email+password (+2FA).

    Args:
        accounts: list of (email, password, totp_secret)
        promo_code: kode promo yang sama untuk semua akun
    Return: list[ClaimResult] sejajar dengan `accounts`.
    """
    async_playwright, _ = _import_async_playwright()
    if async_playwright is None:
        err = ClaimResult(
            False, "Library 'playwright/patchright' tidak terinstall di server."
        )
        return [err for _ in accounts]

    results = []
    async with async_playwright() as pw:
        for idx, (email, password, totp) in enumerate(accounts, start=1):
            logger.info("[DO Coupon] Bulk akun %d/%d: %s", idx, len(accounts), email)
            browser = None
            context = None
            page = None
            try:
                browser = await _launch_browser(pw)
                context = await _new_clean_context(browser)
                page = await context.new_page()
                login_err = await _login_do_direct(page, email, password, totp)
                if login_err:
                    results.append(login_err)
                else:
                    results.append(await _apply_coupon(page, promo_code))
            except Exception as exc:
                logger.exception("[DO Coupon] Bulk error akun %s", email)
                shot = await _safe_screenshot(page) if page else None
                results.append(ClaimResult(False, f"❌ Error: {exc}", shot))
            finally:
                try:
                    if context:
                        await context.close()
                except Exception:
                    pass
                try:
                    if browser:
                        await browser.close()
                except Exception:
                    pass
            await asyncio.sleep(2)
    return results
