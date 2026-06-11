"""
coupon_claim.py — Jasa Klaim Kupon DigitalOcean (DI LUAR katalog produk).

JASA OTOMASI (bukan produk katalog). Mendukung BULK (banyak akun DO sekaligus).

Alur (BAYAR DULU):
  1. User klik "Mulai Klaim Kupon" → pilih JUMLAH akun DO (bulk).
  2. Pilih metode pembayaran (SALDO / QRIS). Total = harga × jumlah akun.
       • Saldo → potong saldo, lanjut input akun DO.
       • QRIS  → buat order + tampilkan QRIS. Setelah lunas, pesan QRIS dihapus
                 dan bot mengirim tombol "Lanjutkan" untuk input akun.
  3. Setelah lunas:
       • qty == 1 → bisa pilih metode login (email / cookies).
       • qty > 1  → input akun email:password[:totp], SATU akun per baris.
  4. Bot menerapkan promo (fixed) ke tiap akun, verifikasi, lalu buang sesi.
     Tiap akun yang GAGAL direfund ke saldo (harga per akun).

Kode promo selalu FIXED = "ReferreeNew2209".

States:
    COUPON_WAITING_QTY   (306) — pilih jumlah akun (bulk)
    COUPON_CHOOSE_PAYMENT(305) — pilih metode bayar (saldo / qris)
    COUPON_CHOOSE_METHOD (301) — pilih metode login DO (email / cookies) [qty==1]
    COUPON_WAITING_EMAIL (302) — input akun DO (1 baris, atau N baris untuk bulk)
    COUPON_WAITING_COOKIES(303) — input cookies DO (JSON) [qty==1]
"""

import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from config import PAKASIR_ENABLED
from database import db

logger = logging.getLogger(__name__)

# States
COUPON_CHOOSE_METHOD = 301
COUPON_WAITING_EMAIL = 302
COUPON_WAITING_COOKIES = 303
COUPON_CHOOSE_PAYMENT = 305
COUPON_WAITING_QTY = 306

# Harga default per akun (Rupiah) bila belum di-set admin
_DEFAULT_PRICE = 1000
# Kode promo yang selalu digunakan (fixed)
FIXED_PROMO_CODE = "ReferreeNew2209"
# Batas jumlah akun per transaksi bulk
_MAX_QTY = 20


def get_service_price() -> int:
    """Harga jasa klaim kupon DO PER AKUN (dari settings, default _DEFAULT_PRICE)."""
    try:
        return int(db.get_setting("coupon_service_price", _DEFAULT_PRICE) or _DEFAULT_PRICE)
    except Exception:
        return _DEFAULT_PRICE


def _fmt_price(price: int) -> str:
    return "Rp " + f"{price:,}".replace(",", ".")


def _mask_email(email: str) -> str:
    """Samarkan email untuk ditampilkan di ringkasan (a***@domain)."""
    try:
        local, domain = email.split("@", 1)
        if len(local) <= 2:
            masked = local[0] + "*"
        else:
            masked = local[0] + "***" + local[-1]
        return f"{masked}@{domain}"
    except Exception:
        return email


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        "coupon_method",
        "coupon_qty",
        "coupon_accounts",
        "coupon_do_cookies",
        "coupon_total",
        "coupon_price_each",
        "coupon_paid",
        "coupon_charged",
        "coupon_order_id",
    ):
        context.user_data.pop(key, None)


def _parse_do_account(line: str):
    """Parse akun DO 'email:password[:totp]' atau dipisah '|'. Return (email, pass, totp)."""
    raw = (line or "").strip()
    if not raw:
        return "", "", ""
    parts = [p.strip() for p in (raw.split("|") if "|" in raw else raw.split(":"))]
    email = parts[0] if len(parts) > 0 else ""
    passwd = parts[1] if len(parts) > 1 else ""
    totp = parts[2].replace(" ", "") if len(parts) > 2 else ""
    return email, passwd, totp


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (email or "").strip()))


def _parse_accounts_block(text: str) -> list:
    """Parse beberapa baris akun DO (bulk). Return list (email, pass, totp) valid."""
    result = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        email, passwd, totp = _parse_do_account(line)
        if _is_valid_email(email) and passwd:
            result.append((email, passwd, totp))
    return result


# ------------------------------------------------------------------
# Helper pengiriman pesan — fallback Markdown→plain & pemotongan otomatis
# ------------------------------------------------------------------

_TG_LIMIT = 4096  # batas panjang pesan Telegram


def _strip_md(text: str) -> str:
    """Buang karakter format Markdown agar aman dikirim sebagai plain text."""
    return text.replace("*", "").replace("`", "").replace("_", "")


def _split_chunks(text: str, limit: int = _TG_LIMIT) -> list:
    """Pecah teks panjang menjadi potongan ≤ limit, memutus di batas baris."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        # Baris tunggal lebih panjang dari limit → potong paksa.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


async def _safe_send(send, text: str, **kwargs):
    """Kirim pesan dengan fallback otomatis ke plain text bila Markdown gagal.

    Memotong teks panjang menjadi beberapa pesan (batas Telegram 4096 char).
    Mengembalikan objek Message terakhir (atau None bila semua gagal).
    """
    last = None
    for chunk in _split_chunks(text):
        try:
            last = await send(text=chunk, **kwargs)
        except Exception:
            plain_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
            try:
                last = await send(text=_strip_md(chunk), **plain_kwargs)
            except Exception:
                pass
    return last


def kb_coupon_method() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📧 Email & Password", callback_data="coupon_m_email"),
                InlineKeyboardButton("🍪 Cookies", callback_data="coupon_m_cookies"),
            ]
        ]
    )


def kb_coupon_qty(qty: int, price_each: int) -> InlineKeyboardMarkup:
    """Stepper kuantitas interaktif (mirip pembelian produk): -5 -1 [qty] +1 +5.

    Tombol step yang akan melewati batas (1.._MAX_QTY) di-nonaktifkan (jadi noop)
    agar qty selalu berada di rentang valid.
    """
    can_dec1 = qty > 1
    can_dec5 = qty > 1  # -5 di-clamp ke 1, tetap berguna selama qty > 1
    can_inc1 = qty < _MAX_QTY
    can_inc5 = qty < _MAX_QTY  # +5 di-clamp ke _MAX_QTY

    step_row = [
        InlineKeyboardButton("➖5", callback_data="coupon_qty_dec5" if can_dec5 else "coupon_noop"),
        InlineKeyboardButton("➖", callback_data="coupon_qty_dec1" if can_dec1 else "coupon_noop"),
        InlineKeyboardButton(f"  {qty}  ", callback_data="coupon_noop"),
        InlineKeyboardButton("➕", callback_data="coupon_qty_inc1" if can_inc1 else "coupon_noop"),
        InlineKeyboardButton("➕5", callback_data="coupon_qty_inc5" if can_inc5 else "coupon_noop"),
    ]
    total = price_each * qty
    confirm_row = [
        InlineKeyboardButton(
            f"✅ Lanjut Bayar — {_fmt_price(total)}",
            callback_data="coupon_qty_confirm",
        )
    ]
    cancel_row = [InlineKeyboardButton("❌ Batalkan", callback_data="coupon_qty_cancel")]
    return InlineKeyboardMarkup([step_row, confirm_row, cancel_row])


def _qty_text(qty: int, price_each: int) -> str:
    total = price_each * qty
    return (
        f"🔢 *Jumlah Akun DigitalOcean*\n\n"
        f"Atur jumlah akun yang ingin diklaim (1–{_MAX_QTY}):\n\n"
        f"📦 Jumlah: *{qty}* akun\n"
        f"💵 Harga: {_fmt_price(price_each)} × {qty} = *{_fmt_price(total)}*"
    )


# ------------------------------------------------------------------
# Info (callback_data='coupon_info' dari menu utama) — bukan bagian conv
# ------------------------------------------------------------------


async def show_coupon_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan info jasa + tombol mulai."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    user = update.effective_user
    balance = db.get_balance(user.id) if user else 0
    price = get_service_price()

    text = (
        "🎟️ *Jasa Klaim Kupon DigitalOcean*\n\n"
        "Bot otomatis menerapkan promo credit ke akun DO milikmu:\n"
        "✅ Auto-login DigitalOcean (mendukung 2FA)\n"
        "✅ Apply kode promo di halaman billing\n"
        "✅ Verifikasi kredit ter-apply\n"
        "✅ *Bulk* — banyak akun DO sekaligus\n\n"
        f"💵 *Biaya:* {_fmt_price(price)} / akun\n"
        f"💰 *Saldo kamu:* {_fmt_price(balance)}\n\n"
        "💳 Bayar pakai *Saldo* atau *QRIS* (bayar dulu, lalu input akun DO).\n"
        "_Pastikan akun DO sudah punya metode pembayaran terdaftar._"
    )

    rows = [
        [InlineKeyboardButton("🚀 Mulai Klaim Kupon", callback_data="coupon_start")],
        [InlineKeyboardButton("💰 Top Up Saldo", callback_data="menu_topup")],
        [InlineKeyboardButton("🏠 Menu Utama", callback_data="main_menu")],
    ]

    try:
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
        )
    except Exception:
        await update.effective_chat.send_message(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
        )


# ------------------------------------------------------------------
# Entry conversation (callback_data='coupon_start') — pilih jumlah dulu
# ------------------------------------------------------------------


async def entry_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    price = get_service_price()
    context.user_data["coupon_qty"] = 1
    context.user_data["coupon_price_each"] = price
    await update.effective_chat.send_message(
        _qty_text(1, price),
        parse_mode="Markdown",
        reply_markup=kb_coupon_qty(1, price),
    )
    return COUPON_WAITING_QTY


async def handle_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data
    price_each = context.user_data.get("coupon_price_each") or get_service_price()
    context.user_data["coupon_price_each"] = price_each
    qty = int(context.user_data.get("coupon_qty", 1) or 1)

    # Tombol noop (step di luar batas) — cukup acknowledge
    if data == "coupon_noop":
        await query.answer()
        return COUPON_WAITING_QTY

    if data == "coupon_qty_cancel":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        _clear(context)
        await update.effective_chat.send_message("❌ Transaksi dibatalkan.")
        return ConversationHandler.END

    if data == "coupon_qty_confirm":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        context.user_data["coupon_qty"] = qty
        context.user_data["coupon_total"] = price_each * qty
        return await _show_payment_choice(update, context)

    # Step +/-
    delta_map = {
        "coupon_qty_inc1": 1,
        "coupon_qty_dec1": -1,
        "coupon_qty_inc5": 5,
        "coupon_qty_dec5": -5,
    }
    delta = delta_map.get(data, 0)
    new_qty = max(1, min(_MAX_QTY, qty + delta))

    if new_qty == qty:
        # Tidak ada perubahan (sudah di batas)
        await query.answer(f"Jumlah min 1, maks {_MAX_QTY}.", show_alert=False)
        return COUPON_WAITING_QTY

    await query.answer()
    context.user_data["coupon_qty"] = new_qty
    try:
        await query.edit_message_text(
            _qty_text(new_qty, price_each),
            parse_mode="Markdown",
            reply_markup=kb_coupon_qty(new_qty, price_each),
        )
    except Exception:
        # Bila edit gagal (mis. pesan sama), kirim ulang
        await update.effective_chat.send_message(
            _qty_text(new_qty, price_each),
            parse_mode="Markdown",
            reply_markup=kb_coupon_qty(new_qty, price_each),
        )
    return COUPON_WAITING_QTY


async def _show_payment_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    qty = context.user_data.get("coupon_qty", 1)
    price_each = context.user_data.get("coupon_price_each", get_service_price())
    total = price_each * qty
    balance = db.get_balance(user.id)
    context.user_data["coupon_total"] = total

    text = (
        f"🧾 *Pembayaran Jasa Klaim Kupon DO*\n\n"
        f"🔢 Jumlah akun: *{qty}*\n"
        f"💵 Biaya: {_fmt_price(price_each)} × {qty} = *{_fmt_price(total)}*\n"
        f"💰 Saldo kamu: *{_fmt_price(balance)}*\n\n"
        f"Pilih metode pembayaran. Setelah lunas, kamu akan diminta "
        f"login akun DigitalOcean."
    )

    rows = []
    if balance >= total:
        rows.append(
            [InlineKeyboardButton(f"💰 Bayar pakai Saldo ({_fmt_price(total)})",
                                  callback_data="coupon_pay_saldo")]
        )
    else:
        rows.append(
            [InlineKeyboardButton("💰 Saldo kurang — Top Up dulu",
                                  callback_data="coupon_pay_saldo")]
        )
    if PAKASIR_ENABLED:
        rows.append(
            [InlineKeyboardButton("📲 Bayar via QRIS", callback_data="coupon_pay_qris")]
        )
    rows.append([InlineKeyboardButton("❌ Batalkan", callback_data="coupon_pay_cancel")])

    await update.effective_chat.send_message(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )
    return COUPON_CHOOSE_PAYMENT


async def handle_payment_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if choice == "coupon_pay_cancel":
        _clear(context)
        await update.effective_chat.send_message("❌ Transaksi dibatalkan.")
        return ConversationHandler.END

    if choice == "coupon_pay_qris":
        return await _pay_with_qris(update, context)

    # coupon_pay_saldo
    return await _pay_with_saldo(update, context)


# ------------------------------------------------------------------
# Bayar dengan SALDO → lanjut input akun DO
# ------------------------------------------------------------------


async def _pay_with_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    chat = update.effective_chat
    total = context.user_data.get("coupon_total", get_service_price())
    balance = db.get_balance(user.id)

    if balance < total:
        kurang = total - balance
        await chat.send_message(
            f"⚠️ *Saldo tidak cukup.*\n\n"
            f"💰 Saldo kamu: *{_fmt_price(balance)}*\n"
            f"💵 Dibutuhkan: *{_fmt_price(total)}*\n"
            f"➕ Kurang: *{_fmt_price(kurang)}*\n\n"
            f"Top up saldo dulu, atau bayar via QRIS.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("💰 Top Up Saldo Sekarang", callback_data="menu_topup")],
                    [InlineKeyboardButton("📲 Bayar via QRIS", callback_data="coupon_pay_qris")],
                ]
            ),
        )
        return COUPON_CHOOSE_PAYMENT

    if not db.deduct_balance(user.id, total):
        await chat.send_message("⚠️ Gagal memotong saldo. Coba lagi.")
        return COUPON_CHOOSE_PAYMENT

    context.user_data["coupon_charged"] = total
    context.user_data["coupon_paid"] = True

    # Gabung konfirmasi pembayaran + permintaan kredensial jadi SATU pesan.
    return await _ask_credentials(
        update,
        context,
        prefix=f"✅ *Pembayaran saldo berhasil* ({_fmt_price(total)}).\n\n",
    )


# ------------------------------------------------------------------
# Bayar dengan QRIS → buat order, tampilkan QRIS, akhiri conv.
# ------------------------------------------------------------------


async def _pay_with_qris(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from handlers.user import _handle_buy_pakasir, _pakasir_client

    user = update.effective_user
    chat = update.effective_chat
    qty = context.user_data.get("coupon_qty", 1)
    price_each = context.user_data.get("coupon_price_each", get_service_price())
    total = context.user_data.get("coupon_total", price_each * qty)

    if not (PAKASIR_ENABLED and _pakasir_client):
        await chat.send_message("⚠️ QRIS sedang tidak tersedia. Coba bayar pakai saldo.")
        return COUPON_CHOOSE_PAYMENT

    job = {
        "promo_code": FIXED_PROMO_CODE,
        "price_each": price_each,
        "qty": qty,
        "awaiting_credentials": True,
    }
    order_id = db.create_coupon_order(
        user.id, user.username or user.first_name, total, job
    )

    pseudo_product = {"name": f"Jasa Klaim Kupon DO x{qty}", "price": total}

    class _QueryShim:
        def __init__(self, message, bot):
            self.message = message
            self._bot = bot
            self.from_user = user

        async def answer(self, *a, **k):
            pass

        def get_bot(self):
            return self._bot

        async def edit_message_text(self, *a, **k):
            raise Exception("no editable message")

    sent = await chat.send_message("⏳ Membuat QRIS pembayaran...")
    shim = _QueryShim(sent, context.bot)
    try:
        await _handle_buy_pakasir(shim, order_id, pseudo_product, 1, total)
    except Exception as exc:
        logger.error("Coupon QRIS gagal order %s: %s", order_id, exc)
        await chat.send_message("⚠️ Gagal membuat QRIS. Coba lagi atau bayar pakai saldo.")
        return COUPON_CHOOSE_PAYMENT

    await chat.send_message(
        "📲 Setelah pembayaran QRIS terdeteksi, bot akan mengirim tombol "
        "*Lanjutkan* untuk input akun DigitalOcean kamu.",
        parse_mode="Markdown",
    )
    _clear(context)
    return ConversationHandler.END


# ------------------------------------------------------------------
# Resume setelah QRIS lunas (callback 'coupon_resume_<order_id>')
# ------------------------------------------------------------------


async def entry_coupon_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    order_id = query.data.removeprefix("coupon_resume_")

    order = db.get_order(order_id)
    user = update.effective_user
    if not order or order.get("user_id") != user.id or not order.get("is_coupon_service"):
        await query.answer("Pesanan tidak valid.", show_alert=True)
        return ConversationHandler.END
    if order.get("status") != "confirmed":
        await query.answer("Pembayaran belum lunas.", show_alert=True)
        return ConversationHandler.END
    if order.get("coupon_job", {}).get("done"):
        await query.answer("Pesanan ini sudah diproses.", show_alert=True)
        return ConversationHandler.END

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    job = order.get("coupon_job") or {}
    context.user_data["coupon_order_id"] = order_id
    context.user_data["coupon_paid"] = True
    context.user_data["coupon_qty"] = int(job.get("qty", 1) or 1)
    context.user_data["coupon_price_each"] = int(job.get("price_each", get_service_price()))
    context.user_data["coupon_total"] = order.get("total_price", get_service_price())

    return await _ask_credentials(update, context, prefix="✅ *Pembayaran lunas.*\n\n")


# ------------------------------------------------------------------
# Minta kredensial DO (setelah lunas)
#   qty == 1 → tawarkan metode login (email / cookies)
#   qty  > 1 → langsung minta N akun email (satu per baris)
# ------------------------------------------------------------------


async def _ask_credentials(
    update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str = ""
) -> int:
    qty = context.user_data.get("coupon_qty", 1)
    chat = update.effective_chat

    if qty > 1:
        context.user_data["coupon_method"] = "email"
        await chat.send_message(
            f"{prefix}"
            f"📝 *Input {qty} Akun DigitalOcean* (bulk)\n\n"
            f"Kirim *{qty} akun*, *satu akun per baris*:\n"
            f"`email:password`\n"
            f"`email:password:TOTP_SECRET`\n\n"
            f"_TOTP secret hanya jika akun pakai 2FA._\n"
            f"⚠️ Pesan dihapus otomatis demi keamanan.\n"
            f"Ketik /cancel untuk membatalkan.",
            parse_mode="Markdown",
        )
        return COUPON_WAITING_EMAIL

    # qty == 1 → pilih metode login
    await chat.send_message(
        f"{prefix}Pilih cara login ke akun *DigitalOcean* kamu:",
        parse_mode="Markdown",
        reply_markup=kb_coupon_method(),
    )
    return COUPON_CHOOSE_METHOD


async def handle_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    method = "email" if query.data == "coupon_m_email" else "cookies"
    context.user_data["coupon_method"] = method
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if method == "email":
        await update.effective_chat.send_message(
            "📝 *Input Akun DigitalOcean*\n\n"
            "Kirim akun DO dalam satu baris:\n"
            "`email:password`\n"
            "`email:password:TOTP_SECRET`\n\n"
            "_TOTP secret hanya jika akun pakai 2FA._\n"
            "⚠️ Pesan dihapus otomatis demi keamanan.\n"
            "Ketik /cancel untuk membatalkan.",
            parse_mode="Markdown",
        )
        return COUPON_WAITING_EMAIL
    else:
        await update.effective_chat.send_message(
            "Export cookies dari browser:\n\n"
            "1. Install ekstensi Cookie-Editor\n"
            "2. Login ke cloud.digitalocean.com\n"
            "3. Cookie-Editor → Export → Export as JSON\n"
            "4. Salin semua teks JSON dan kirim di sini\n\n"
            "Ketik /cancel untuk membatalkan.",
            disable_web_page_preview=True,
        )
        return COUPON_WAITING_COOKIES


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    raw = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass

    qty = context.user_data.get("coupon_qty", 1)
    accounts = _parse_accounts_block(raw)

    if not accounts:
        await update.effective_chat.send_message(
            "❌ Tidak ada akun valid terbaca. Gunakan `email:password` "
            "(satu akun per baris).\nCoba lagi, atau /cancel.",
            parse_mode="Markdown",
        )
        return COUPON_WAITING_EMAIL

    if len(accounts) < qty:
        await update.effective_chat.send_message(
            f"⚠️ Kamu membayar untuk *{qty}* akun, tapi hanya *{len(accounts)}* "
            f"akun valid terbaca.\nKirim total *{qty}* akun (satu per baris), atau /cancel.",
            parse_mode="Markdown",
        )
        return COUPON_WAITING_EMAIL

    if len(accounts) > qty:
        accounts = accounts[:qty]
        await update.effective_chat.send_message(
            f"ℹ️ Kamu mengirim lebih dari {qty} akun. "
            f"Hanya {qty} akun pertama yang diproses.",
        )

    context.user_data["coupon_method"] = "email"
    context.user_data["coupon_accounts"] = accounts
    await _finish_claim(update, context)
    return ConversationHandler.END


async def handle_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    raw = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass

    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError:
        await update.effective_chat.send_message(
            "❌ Format cookies tidak valid (harus JSON). Gunakan *Export as JSON* "
            "di Cookie-Editor.\nCoba lagi, atau /cancel.",
            parse_mode="Markdown",
        )
        return COUPON_WAITING_COOKIES

    if not isinstance(cookies, list) or not cookies:
        await update.effective_chat.send_message(
            "❌ Cookies harus berupa array JSON yang tidak kosong.\nCoba lagi, atau /cancel.",
        )
        return COUPON_WAITING_COOKIES

    context.user_data["coupon_method"] = "cookies"
    context.user_data["coupon_do_cookies"] = cookies
    await _finish_claim(update, context)
    return ConversationHandler.END


# ------------------------------------------------------------------
# Jalankan klaim (kredensial sudah ada, pembayaran sudah lunas)
# ------------------------------------------------------------------


async def _finish_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    method = context.user_data.get("coupon_method", "email")
    price_each = context.user_data.get("coupon_price_each", get_service_price())
    qty = context.user_data.get("coupon_qty", 1)

    job = {
        "method": method,
        "accounts": context.user_data.get("coupon_accounts", []),
        "do_cookies": context.user_data.get("coupon_do_cookies", []),
        "promo_code": FIXED_PROMO_CODE,
        "price_each": price_each,
        "qty": qty,
    }

    # Tandai order QRIS (bila ada) sudah diproses agar tidak dobel
    order_id = context.user_data.get("coupon_order_id")
    if order_id:
        order = db.get_order(order_id)
        if order:
            cj = dict(order.get("coupon_job") or {})
            cj["done"] = True
            db.update_order(order_id, coupon_job=cj)

    await _run_coupon_job(chat, user, job, context.bot)
    _clear(context)


# ------------------------------------------------------------------
# Dipanggil auto_confirm_order setelah QRIS lunas → kirim tombol Lanjutkan
# ------------------------------------------------------------------


async def send_coupon_resume_prompt(order: dict, bot) -> None:
    """Setelah QRIS lunas, minta user input akun DO via tombol Lanjutkan."""
    order_id = order["id"]
    qty = (order.get("coupon_job") or {}).get("qty", 1)
    try:
        await bot.send_message(
            chat_id=order["user_id"],
            text=(
                "✅ *Pembayaran QRIS lunas!*\n\n"
                f"Lanjutkan klaim kupon untuk *{qty} akun*: tekan tombol di bawah "
                "untuk input akun DigitalOcean kamu."
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➡️ Lanjutkan Klaim", callback_data=f"coupon_resume_{order_id}")]]
            ),
        )
    except Exception as exc:
        logger.error("send_coupon_resume_prompt gagal order %s: %s", order_id, exc)


# ------------------------------------------------------------------
# Eksekusi automation klaim (bulk)
# ------------------------------------------------------------------


async def _run_coupon_job(chat, user, job, bot) -> None:
    await _run_coupon_job_core(
        send=lambda **kw: chat.send_message(**kw),
        send_photo=lambda **kw: chat.send_photo(**kw),
        user=user,
        job=job,
    )


async def _run_coupon_job_core(send, send_photo, user, job) -> None:
    """Jalankan automation klaim ke 1..N akun DO. Tiap akun gagal direfund.

    job:
      method      : "email" | "cookies"
      accounts    : list (email, pass, totp)  [untuk email/bulk]
      do_cookies  : list cookie               [untuk cookies, qty==1]
      promo_code  : kode promo (fixed)
      price_each  : harga per akun
      qty         : jumlah akun
    """
    method = job.get("method", "email")
    promo_code = job.get("promo_code", FIXED_PROMO_CODE)
    price_each = int(job.get("price_each", get_service_price()))
    accounts = job.get("accounts", []) or []
    qty = int(job.get("qty", max(1, len(accounts))) or 1)

    await _safe_send(
        send,
        "⚙️ *Memproses Klaim Kupon...*\n\n"
        f"🔐 Login DigitalOcean & apply promo ke *{qty} akun*...\n"
        "⏳ Mohon tunggu, jangan tutup chat.",
        parse_mode="Markdown",
    )

    # Label tiap hasil (email tersamarkan / "Akun cookies")
    labels = []
    try:
        if method == "cookies":
            from automation.do_claimer import claim_coupon_with_cookies

            r = await claim_coupon_with_cookies(
                do_cookies=job.get("do_cookies", []),
                promo_code=promo_code,
            )
            results = [r]
            labels = ["Akun (cookies)"]
        else:
            from automation.do_claimer import claim_coupon_bulk_emails

            results = await claim_coupon_bulk_emails(
                accounts=accounts,
                promo_code=promo_code,
            )
            labels = [_mask_email(e) for (e, _p, _t) in accounts]
    except Exception:
        import traceback

        logger.exception("[Coupon Claim] Error tak terduga saat run job")
        refund = price_each * qty
        db.add_balance(user.id, refund, getattr(user, "username", "") or "")
        tb = traceback.format_exc()
        await _safe_send(
            send,
            f"❌ *Error* (refund {_fmt_price(refund)} ke saldo)\n\n"
            f"```\n{tb[-2500:]}\n```",
            parse_mode="Markdown",
        )
        return

    success = sum(1 for r in results if r.success)
    failed = len(results) - success
    refund = price_each * failed
    if refund > 0:
        db.add_balance(user.id, refund, getattr(user, "username", "") or "")

    # Satu pesan ringkasan: header + status per akun + detail + footer.
    # (sebelumnya: 1 ringkasan + 1 pesan detail per akun → kini digabung)
    sections = [f"🎟️ *Hasil Klaim Kupon ({len(results)} akun)*\n"]
    for lbl, r in zip(labels, results):
        icon = "✅" if r.success else "❌"
        sections.append(f"{icon} *{lbl}*\n{r.message}")

    footer = f"✅ Berhasil: *{success}*  |  ❌ Gagal: *{failed}*"
    if refund > 0:
        footer += f"\n💰 Refund {failed} akun gagal: *{_fmt_price(refund)}* (ke saldo)"
    footer += f"\n💳 Saldo sekarang: *{_fmt_price(db.get_balance(user.id))}*"
    sections.append(footer)

    await _safe_send(
        send,
        "\n\n".join(sections),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    # Screenshot tetap dikirim terpisah (tidak bisa digabung ke teks).
    for lbl, r in zip(labels, results):
        if not r.screenshot:
            continue
        try:
            await send_photo(
                photo=r.screenshot,
                caption=(f"📸 {lbl} — Billing DO" if r.success else f"📸 {lbl} — Debug"),
            )
        except Exception as exc:
            logger.warning("[Coupon Claim] gagal kirim screenshot: %s", exc)


async def cancel_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Batalkan. Refund bila saldo sudah dipotong (jalur saldo, belum diproses)."""
    charged = context.user_data.get("coupon_charged", 0)
    user = update.effective_user
    if charged and user:
        db.add_balance(user.id, charged, user.username or "")
    _clear(context)
    await update.message.reply_text(
        "❌ Proses klaim kupon dibatalkan.\n"
        + (f"💰 Saldo {_fmt_price(charged)} dikembalikan.\n" if charged else "")
        + "Ketik /start untuk kembali ke menu utama."
    )
    return ConversationHandler.END
