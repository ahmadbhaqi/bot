"""
do_claim_standalone.py — Standalone Auto Klaim GitHub Student Pack → DigitalOcean $200
=======================================================================================
Wrapper CLI tipis di atas automation/do_claimer.py. Bisa dijalankan langsung
dari terminal TANPA bot Telegram. Logika otomasi (alur OAuth asli) sepenuhnya
berada di automation/do_claimer.py agar tidak ada duplikasi.

CARA PAKAI:
    python do_claim_standalone.py                  # mode interaktif
    python do_claim_standalone.py --help           # lihat opsi CLI

ALUR (sesuai screenshot asli):
    Login GitHub GHS (+2FA) → buka education.github.com/pack → skip survei →
    klik offer DigitalOcean → login DO buyer (+2FA) → Authenticate with GitHub →
    Authorize digitalocean → "Happy Coding!" = sukses.

METODE LOGIN DO:
    1. Email & Password (+ TOTP 2FA opsional)
    2. Cookies JSON (dari ekstensi Cookie-Editor)

FORMAT AKUN GHS:
    email@gmail.com:Password123                    — tanpa 2FA
    email@gmail.com:Password123:TOTP_SECRET        — dengan TOTP 2FA

REQUIREMENTS:
    pip install playwright
    playwright install chromium
"""

import argparse
import asyncio
import getpass
import json
import logging
import re
import sys
from datetime import datetime

from automation.do_claimer import (
    ClaimResult,
    claim_do_credit_with_cookies,
    claim_do_credit_with_email,
)

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)


# ------------------------------------------------------------------
# Warna terminal (ANSI)
# ------------------------------------------------------------------


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


if _supports_color():
    C_GREEN = "\033[92m"
    C_YELLOW = "\033[93m"
    C_RED = "\033[91m"
    C_CYAN = "\033[96m"
    C_BOLD = "\033[1m"
    C_RESET = "\033[0m"
else:
    C_GREEN = C_YELLOW = C_RED = C_CYAN = C_BOLD = C_RESET = ""


def ok(msg: str) -> str:
    return f"{C_GREEN}✅ {msg}{C_RESET}"


def warn(msg: str) -> str:
    return f"{C_YELLOW}⚠️  {msg}{C_RESET}"


def err(msg: str) -> str:
    return f"{C_RED}❌ {msg}{C_RESET}"


def info(msg: str) -> str:
    return f"{C_CYAN}ℹ  {msg}{C_RESET}"


# ------------------------------------------------------------------
# Screenshot saver
# ------------------------------------------------------------------


def _save_screenshot(data: bytes, prefix: str = "result") -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{prefix}_{ts}.png"
    with open(name, "wb") as f:
        f.write(data)
    return name


# ------------------------------------------------------------------
# Input helpers
# ------------------------------------------------------------------


def _prompt(label: str, secret: bool = False, allow_empty: bool = False) -> str:
    while True:
        if secret:
            val = getpass.getpass(f"  {label}: ")
        else:
            val = input(f"  {label}: ").strip()
        if val or allow_empty:
            return val
        print(warn("Input tidak boleh kosong."))


def _prompt_ghs() -> tuple:
    """Return (ghs_email, ghs_password, ghs_totp)."""
    print()
    print(f"{C_BOLD}Akun GHS{C_RESET} (dari stok penjual)")
    raw = _prompt("GHS (email:password atau email:password:TOTP_SECRET)")
    parts = raw.split(":", 2)
    ghs_email = parts[0].strip()
    ghs_password = parts[1].strip() if len(parts) > 1 else ""
    ghs_totp = parts[2].strip() if len(parts) > 2 else ""
    if not ghs_password:
        ghs_password = _prompt("Password GHS", secret=True)
    return ghs_email, ghs_password, ghs_totp


def _split_ghs(raw: str) -> tuple:
    parts = raw.split(":", 2)
    return (
        parts[0].strip(),
        parts[1].strip() if len(parts) > 1 else "",
        parts[2].strip() if len(parts) > 2 else "",
    )


# ------------------------------------------------------------------
# Tampilkan hasil
# ------------------------------------------------------------------


def _print_result(result: ClaimResult) -> None:
    print()
    print("=" * 50)
    print(ok("BERHASIL") if result.success else err("GAGAL"))
    print()
    print(re.sub(r"[*_`]", "", result.message))
    print()
    if result.screenshot:
        fname = _save_screenshot(
            result.screenshot, prefix="success" if result.success else "failed"
        )
        print(info(f"Screenshot disimpan: {fname}"))
    print("=" * 50)
    print()


# ------------------------------------------------------------------
# Mode interaktif
# ------------------------------------------------------------------


async def _interactive() -> None:
    print()
    print(f"{C_BOLD}{C_CYAN}{'=' * 52}{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}   GHS → DigitalOcean $200 Auto Claimer (Standalone){C_RESET}")
    print(f"{C_BOLD}{C_CYAN}{'=' * 52}{C_RESET}")
    print()

    print(f"{C_BOLD}Metode login DigitalOcean (akun buyer):{C_RESET}")
    print("  1. Email & Password (+ TOTP 2FA opsional)")
    print("  2. Cookies JSON (dari ekstensi Cookie-Editor)")
    print()

    while True:
        choice = input("  Pilih [1/2]: ").strip()
        if choice in ("1", "2"):
            break
        print(warn("Pilih 1 atau 2."))

    if choice == "1":
        print()
        print(f"{C_BOLD}Akun DigitalOcean buyer{C_RESET}")
        do_email = _prompt("Email DO")
        do_password = _prompt("Password DO", secret=True)
        do_totp = _prompt(
            "TOTP secret 2FA DO (Enter jika tidak pakai 2FA)",
            secret=True,
            allow_empty=True,
        ).replace(" ", "")
        ghs_email, ghs_password, ghs_totp = _prompt_ghs()

        print()
        print(info("Memulai proses klaim... (bisa memakan 60–120 detik)"))
        result = await claim_do_credit_with_email(
            do_email=do_email,
            do_password=do_password,
            ghs_email=ghs_email,
            ghs_password=ghs_password,
            ghs_totp_secret=ghs_totp,
            do_totp_secret=do_totp,
        )
    else:
        print()
        print(f"{C_BOLD}Cookies DigitalOcean{C_RESET}")
        print("  1. Login ke cloud.digitalocean.com di browser")
        print("  2. Klik ikon Cookie-Editor → Export → Export as JSON")
        print("  3. Paste semua teks JSON di bawah, lalu Enter dua kali")
        print()

        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)

        raw_cookies = "\n".join(lines).strip()
        try:
            do_cookies = json.loads(raw_cookies)
            if not isinstance(do_cookies, list):
                raise ValueError("Bukan array JSON")
        except Exception as exc:
            print(err(f"Format cookies tidak valid: {exc}"))
            sys.exit(1)

        ghs_email, ghs_password, ghs_totp = _prompt_ghs()

        print()
        print(info("Memulai proses klaim... (bisa memakan 60–120 detik)"))
        result = await claim_do_credit_with_cookies(
            do_cookies=do_cookies,
            ghs_email=ghs_email,
            ghs_password=ghs_password,
            ghs_totp_secret=ghs_totp,
        )

    _print_result(result)
    sys.exit(0 if result.success else 1)


# ------------------------------------------------------------------
# Mode CLI (argparse)
# ------------------------------------------------------------------


async def _cli(args: argparse.Namespace) -> None:
    if args.method == "email":
        if not all([args.do_email, args.do_password, args.ghs]):
            print(
                err("--do-email, --do-password, dan --ghs wajib untuk metode email.")
            )
            sys.exit(1)
        ghs_email, ghs_password, ghs_totp = _split_ghs(args.ghs)
        print(info("Memulai klaim via email..."))
        result = await claim_do_credit_with_email(
            do_email=args.do_email,
            do_password=args.do_password,
            ghs_email=ghs_email,
            ghs_password=ghs_password,
            ghs_totp_secret=ghs_totp,
            do_totp_secret=(args.do_totp or ""),
        )
    else:
        if not args.cookies_file:
            print(err("--cookies-file wajib untuk metode cookies."))
            sys.exit(1)
        if not args.ghs:
            print(err("--ghs wajib diisi."))
            sys.exit(1)
        try:
            with open(args.cookies_file, "r", encoding="utf-8") as f:
                do_cookies = json.load(f)
        except Exception as exc:
            print(err(f"Gagal membaca file cookies: {exc}"))
            sys.exit(1)
        ghs_email, ghs_password, ghs_totp = _split_ghs(args.ghs)
        print(info("Memulai klaim via cookies..."))
        result = await claim_do_credit_with_cookies(
            do_cookies=do_cookies,
            ghs_email=ghs_email,
            ghs_password=ghs_password,
            ghs_totp_secret=ghs_totp,
        )

    _print_result(result)
    sys.exit(0 if result.success else 1)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="do_claim_standalone.py",
        description="Auto Klaim GitHub Student Pack → DigitalOcean $200 (OAuth flow)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
CONTOH:
  # Mode interaktif (direkomendasikan):
  python do_claim_standalone.py

  # Mode email via CLI (DO buyer pakai 2FA):
  python do_claim_standalone.py --method email \\
      --do-email buyer@gmail.com --do-password p4ssw0rd --do-totp DOTOTPSECRET \\
      --ghs ghs@gmail.com:GhsPass123:GHSTOTPSECRET

  # Mode cookies via CLI:
  python do_claim_standalone.py --method cookies \\
      --cookies-file cookies_do.json \\
      --ghs ghs@gmail.com:GhsPass123
        """,
    )
    parser.add_argument(
        "--method", choices=["email", "cookies"], help="Metode login DO"
    )
    parser.add_argument("--do-email", help="Email akun DigitalOcean buyer")
    parser.add_argument("--do-password", help="Password akun DigitalOcean buyer")
    parser.add_argument("--do-totp", help="TOTP secret 2FA DigitalOcean (opsional)")
    parser.add_argument(
        "--ghs", help="Akun GHS: email:password atau email:password:TOTP_SECRET"
    )
    parser.add_argument(
        "--cookies-file", help="Path file JSON cookies (metode cookies)"
    )

    args = parser.parse_args()

    if not any(vars(args).values()):
        asyncio.run(_interactive())
    else:
        asyncio.run(_cli(args))


if __name__ == "__main__":
    main()
