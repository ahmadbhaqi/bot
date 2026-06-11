import os

from dotenv import load_dotenv

load_dotenv()

# =============================================
# 🤖 BOT CONFIGURATION
# =============================================

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN tidak ditemukan! Pastikan sudah diset di file .env")

# Daftar Telegram user ID yang punya akses admin
_raw_admin_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: list[int] = [
    int(x.strip()) for x in _raw_admin_ids.split(",") if x.strip().isdigit()
]

if not ADMIN_IDS:
    raise ValueError(
        "ADMIN_IDS tidak ditemukan! Isi minimal satu admin ID di file .env"
    )

# =============================================
# 🏪 STORE CONFIGURATION
# =============================================

STORE_NAME: str = os.getenv("STORE_NAME", "🎓 GHS Store")

# =============================================
# 💳 PAYMENT CONFIGURATION (manual fallback)
# =============================================

PAYMENT_INFO: dict = {
    "bank": os.getenv("PAYMENT_BANK", "BCA"),
    "account_number": os.getenv("PAYMENT_ACCOUNT", "1234567890"),
    "account_name": os.getenv("PAYMENT_NAME", "Nama Pemilik"),
    "qris": os.getenv("PAYMENT_QRIS", ""),
}

# =============================================
# 💸 RONZZPAY PAYMENT GATEWAY
# =============================================

RONZZPAY_API_KEY: str = os.getenv("RONZZPAY_API_KEY", "")
RONZZPAY_SANDBOX: bool = os.getenv("RONZZPAY_SANDBOX", "true").lower() in (
    "true",
    "1",
    "yes",
)

# Metode pembayaran default via RonzzPay (qris, dana, ovo, gopay)
RONZZPAY_DEFAULT_METHOD: str = os.getenv("RONZZPAY_DEFAULT_METHOD", "qris")

# Webhook server port
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")

# Apakah RonzzPay aktif (otomatis true jika api key diisi)
RONZZPAY_ENABLED: bool = bool(RONZZPAY_API_KEY)

# =============================================
# 💳 PAKASIR PAYMENT GATEWAY
# =============================================

PAKASIR_API_KEY: str = os.getenv("PAKASIR_API_KEY", "mYXyws7sc94Bs38x9MDHQdNmStaYHVyp")

# Slug proyek Pakasir (lihat di halaman detail Proyek di app.pakasir.com)
PAKASIR_PROJECT_SLUG: str = os.getenv("PAKASIR_PROJECT_SLUG", "ghs")

# Metode pembayaran default Pakasir
# Pilihan: qris, bni_va, bri_va, cimb_niaga_va, sampoerna_va,
#          bnc_va, maybank_va, permata_va, atm_bersama_va, artha_graha_va
PAKASIR_DEFAULT_METHOD: str = os.getenv("PAKASIR_DEFAULT_METHOD", "qris")

# Apakah Pakasir aktif (otomatis true jika api key & slug diisi)
PAKASIR_ENABLED: bool = bool(PAKASIR_API_KEY and PAKASIR_PROJECT_SLUG)

# URL publik webhook server (wajib untuk produksi, agar RonzzPay bisa mengirim notifikasi)
# Contoh: https://yourdomain.com atau https://xxxx.ngrok-free.app
# Kosongkan untuk mode lokal (bot tetap bisa auto-confirm via polling)
WEBHOOK_PUBLIC_URL: str = os.getenv("WEBHOOK_PUBLIC_URL", "")
