import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from config import ADMIN_IDS, PAKASIR_ENABLED, STORE_NAME
from database import db
from handlers.user import fmt_price, get_status_emoji, get_status_label

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Helpers — Markdown escaping & robust admin notification
# ------------------------------------------------------------------


def _escape_md(text: str) -> str:
    """Escape karakter khusus Telegram Markdown V1 agar tidak merusak parsing.

    Karakter yang di-escape: _ * [ ] ` ~
    Ini mencegah kegagalan parse_mode='Markdown' saat username/akun
    mengandung underscore atau karakter spesial lainnya.
    """
    for ch in r"\_*[]`~":
        text = text.replace(ch, f"\\{ch}")
    return text


async def _notify_admins(bot, text_md: str, text_plain: str | None = None) -> None:
    """Kirim notifikasi ke SEMUA admin dengan fallback tanpa Markdown.

    Alur:
      1. Coba kirim dengan parse_mode='Markdown'.
      2. Jika gagal (BadRequest / karakter ilegal), kirim ulang tanpa
         parse_mode (plain text) — agar notifikasi tetap sampai.
      3. Log setiap kegagalan agar bisa ditelusuri.
    """
    if text_plain is None:
        # Buat versi plain text: strip Markdown formatting chars
        import re
        text_plain = re.sub(r"[`*_~\[\]]", "", text_md)

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text_md,
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.warning(
                "Notif admin %s gagal (Markdown): %s — fallback ke plain text",
                admin_id,
                exc,
            )
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=text_plain,
                )
            except Exception as exc2:
                logger.error(
                    "Notif admin %s GAGAL TOTAL (plain text juga gagal): %s",
                    admin_id,
                    exc2,
                )


# ------------------------------------------------------------------
# Conversation states (exported so main.py can import them)
# ------------------------------------------------------------------
WAITING_STOCK_INPUT = 100
WAITING_PRICE_INPUT = 101
WAITING_PROMO_INPUT = 102
WAITING_BROADCAST_INPUT = 103
WAITING_EDU_INPUT = 104


# ------------------------------------------------------------------
# Auth helper
# ------------------------------------------------------------------


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ------------------------------------------------------------------
# Keyboards
# ------------------------------------------------------------------


def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📦 Kelola Stok", callback_data="admin_stock")],
            [
                InlineKeyboardButton(
                    "🎓 Apply GitHub Edu", callback_data="admin_edu"
                )
            ],
            [
                InlineKeyboardButton(
                    "🎁 Tambah Promo", callback_data="admin_promo"
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 Broadcast", callback_data="admin_broadcast"
                )
            ],
            [
                InlineKeyboardButton(
                    "📜 Semua Pesanan", callback_data="admin_all_orders"
                )
            ],
            [
                InlineKeyboardButton(
                    "⚙️ Pengaturan Gateway", callback_data="admin_settings"
                )
            ],
        ]
    )


def kb_admin_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Admin Menu", callback_data="admin_menu")]]
    )


# ------------------------------------------------------------------
# Account delivery formatting
# ------------------------------------------------------------------

_ACCOUNT_PASSWORD = ".ganteng123"
_CARD_DIVIDER = "=" * 30


def _parse_account(account: str) -> tuple[str, str, str]:
    """Parse string akun stok menjadi (username, secret, password).

    Format baru (utama):
        username:secretcode               → password default .ganteng123
        username:secretcode:password      → password kustom
    Pemisah '|' juga didukung:
        username | secretcode | password
    """
    sep = "|" if "|" in account else ":"
    parts = [p.strip() for p in account.split(sep)]
    username = parts[0] if len(parts) > 0 else ""
    secret = parts[1] if len(parts) > 1 else "-"
    password = parts[2] if len(parts) > 2 and parts[2] else _ACCOUNT_PASSWORD
    return username, secret, password


def _fmt_account_card(account: str, include_password: bool = True) -> str:
    """Format string akun menjadi kartu USERNAME / SECRET / PASSWORD.

    Format akun: username:secretcode[:password]
    Password opsional — bila kosong dipakai default .ganteng123.
    """
    username, secret, password = _parse_account(account)

    lines = [_CARD_DIVIDER, f"USERNAME: {username}", f"SECRET  : {secret}"]
    if include_password:
        lines.append(f"PASSWORD: {password}")
    lines.append(_CARD_DIVIDER)
    return "\n".join(lines)


def _fmt_delivery_text(
    accounts: list,
    product_name: str,
    order_id: str,
    qty: int = 0,
    partial: bool = False,
    promo_price: int = 0,
) -> str:
    """Buat teks pengiriman akun untuk dikirim ke user."""
    delivered = len(accounts)
    partial_note = (
        f"\n\n⚠️ _Catatan: {delivered} dari {qty} item terkirim karena stok terbatas._"
        if partial
        else ""
    )
    promo_note = (
        f"\n\n🎁 _Harga promo berlaku: {fmt_price(promo_price)}/akun._"
        if promo_price > 0
        else ""
    )

    if delivered == 1:
        card = _fmt_account_card(accounts[0], include_password=True)
        return (
            f"✅ *Pembayaran Diterima*\n\n"
            f"📦 *{product_name}*  |  `{order_id}`\n\n"
            f"```\n{card}\n```"
            f"{partial_note}\n\n"
            f"Simpan akun di atas dengan aman. 🙏"
        )
    else:
        # Tiap kartu menyertakan password masing-masing (default .ganteng123)
        cards = "\n".join(
            _fmt_account_card(a, include_password=True) for a in accounts
        )
        return (
            f"✅ *Pembayaran Diterima*\n\n"
            f"📦 *{product_name} x{delivered}*  |  `{order_id}`\n\n"
            f"```\n{cards}\n```"
            f"{promo_note}"
            f"{partial_note}\n\n"
            f"Simpan akun-akun di atas dengan aman. 🙏"
        )


# Label gateway yang tampil di UI (RonzzPay dihapus — default Pakasir)
_GATEWAY_LABELS = {
    "pakasir": "Pakasir 💳",
    "manual": "Manual Transfer 🏦",
}


def kb_settings_menu(active: str) -> InlineKeyboardMarkup:
    """Keyboard menu pengaturan gateway dengan indikator yang sedang aktif."""
    rows = []
    for gw, label in _GATEWAY_LABELS.items():
        if gw == active:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"✅ {label} (aktif)", callback_data=f"admin_gw_set_{gw}"
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"🔄 Ganti ke {label}", callback_data=f"admin_gw_set_{gw}"
                    )
                ]
            )
    rows.append([InlineKeyboardButton("🔙 Kembali", callback_data="admin_menu")])
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------
# /admin command
# ------------------------------------------------------------------


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.effective_user is not None
    assert update.message is not None
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Akses ditolak!")
        return
    await update.message.reply_text(
        f"⚙️ *Admin Panel — {STORE_NAME}*\n\nHalo, {update.effective_user.first_name}!",
        parse_mode="Markdown",
        reply_markup=kb_admin_menu(),
    )


# ------------------------------------------------------------------
# Stock management
# ------------------------------------------------------------------


async def _show_admin_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    products = db.load_products()
    text = "📦 *Kelola Stok Produk*\n\n"
    rows = []

    for pid, p in products.items():
        stock = db.get_stock_count(pid)
        shared_with = p.get("shared_stock_with")
        shared_label = ""
        if shared_with:
            shared_name = products.get(shared_with, {}).get("name", shared_with)
            shared_label = f" _(bersama {shared_name})_"

        text += f"{p['emoji']} *{p['name']}*: `{stock}` stok{shared_label}\n"

        if shared_with:
            # Produk stok bersama: tombol tambah stok diarahkan ke produk sumber
            shared_name = products.get(shared_with, {}).get("name", shared_with)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"➕ Tambah Stok → {shared_name} ({stock})",
                        callback_data=f"admin_add_stock_{shared_with}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"➕ Tambah Stok: {p['name']} ({stock})",
                        callback_data=f"admin_add_stock_{pid}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    f"💲 Ubah Harga: {p['name']} ({fmt_price(p['price'])})",
                    callback_data=f"admin_set_price_{pid}",
                )
            ]
        )

    rows.append([InlineKeyboardButton("🔙 Kembali", callback_data="admin_menu")])
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


# ------------------------------------------------------------------
# Add stock — ConversationHandler entry
# ------------------------------------------------------------------


async def entry_add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: admin clicked 'Tambah Stok <product>'."""
    query = update.callback_query
    assert query is not None
    assert query.data is not None
    assert update.effective_user is not None
    assert context.user_data is not None
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Akses ditolak!", show_alert=True)
        return ConversationHandler.END

    product_id = query.data.removeprefix("admin_add_stock_")
    product = db.get_product(product_id)
    if not product:
        await query.answer("Produk tidak ditemukan!", show_alert=True)
        return ConversationHandler.END

    context.user_data["add_stock_product_id"] = product_id

    await query.edit_message_text(
        f"➕ *Tambah Stok — {product['name']}*\n\n"
        f"Kirim daftar akun, satu per baris.\n\n"
        f"*Format (pilih salah satu):*\n"
        f"`username:secret`\n"
        f"`username:secret:Lokasi Alamat`\n\n"
        f"Contoh:\n"
        f"`AxelDanisa:T7ASCMHDA3DSSAZR`\n"
        f"`AxelDanisa:T7ASCMHDA3DSSAZR:Jl. Kenanga No. 5`\n\n"
        f"Kirim /cancel untuk membatalkan.",
        parse_mode="Markdown",
    )
    return WAITING_STOCK_INPUT


async def handle_stock_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.effective_user is not None
    assert update.message is not None
    assert context.user_data is not None
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    product_id: str | None = context.user_data.get("add_stock_product_id")
    if not product_id:
        await update.message.reply_text("❌ Sesi expired. Coba lagi dari /admin.")
        return ConversationHandler.END

    assert update.message.text is not None
    lines = [line.strip() for line in update.message.text.splitlines() if line.strip()]
    if not lines:
        await update.message.reply_text("⚠️ Tidak ada data yang valid. Coba lagi.")
        return WAITING_STOCK_INPUT

    added = db.add_stock_accounts(product_id, lines)
    product = db.get_product(product_id)
    total_stock = db.get_stock_count(product_id)

    context.user_data.pop("add_stock_product_id", None)

    await update.message.reply_text(
        f"✅ *{added} akun berhasil ditambahkan!*\n\n"
        f"Produk: *{product['name'] if product else product_id}*\n"
        f"Total stok sekarang: *{total_stock}*",
        parse_mode="Markdown",
        reply_markup=kb_admin_back(),
    )
    return ConversationHandler.END


# ------------------------------------------------------------------
# Set price — ConversationHandler entry
# ------------------------------------------------------------------


async def entry_set_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: admin clicked 'Ubah Harga <product>'."""
    query = update.callback_query
    assert query is not None
    assert query.data is not None
    assert update.effective_user is not None
    assert context.user_data is not None
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Akses ditolak!", show_alert=True)
        return ConversationHandler.END

    product_id = query.data.removeprefix("admin_set_price_")
    product = db.get_product(product_id)
    if not product:
        await query.answer("Produk tidak ditemukan!", show_alert=True)
        return ConversationHandler.END

    context.user_data["set_price_product_id"] = product_id

    await query.edit_message_text(
        f"💲 *Ubah Harga — {product['name']}*\n\n"
        f"Harga saat ini: *{fmt_price(product['price'])}*\n\n"
        f"Kirim harga baru (angka saja, tanpa titik/koma):\n"
        f"Contoh: `75000`\n\n"
        f"Kirim /cancel untuk membatalkan.",
        parse_mode="Markdown",
    )
    return WAITING_PRICE_INPUT


async def handle_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.effective_user is not None
    assert update.message is not None
    assert context.user_data is not None
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    product_id: str | None = context.user_data.get("set_price_product_id")
    if not product_id:
        await update.message.reply_text("❌ Sesi expired. Coba lagi dari /admin.")
        return ConversationHandler.END

    assert update.message.text is not None
    raw = update.message.text.strip().replace(".", "").replace(",", "")
    if not raw.isdigit():
        await update.message.reply_text(
            "⚠️ Input tidak valid. Kirim angka saja (contoh: `75000`).",
            parse_mode="Markdown",
        )
        return WAITING_PRICE_INPUT

    new_price = int(raw)
    db.update_product_price(product_id, new_price)
    product = db.get_product(product_id)
    context.user_data.pop("set_price_product_id", None)

    await update.message.reply_text(
        f"✅ *Harga berhasil diubah!*\n\n"
        f"Produk: *{product['name'] if product else product_id}*\n"
        f"Harga baru: *{fmt_price(new_price)}*",
        parse_mode="Markdown",
        reply_markup=kb_admin_back(),
    )
    return ConversationHandler.END


async def cancel_admin_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    assert update.message is not None
    assert context.user_data is not None
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Aksi dibatalkan.",
        reply_markup=kb_admin_back(),
    )
    return ConversationHandler.END


# ------------------------------------------------------------------
# Promo — ConversationHandler (beli X akun → harga Y per akun)
# ------------------------------------------------------------------


async def entry_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: admin klik 'Atur Promo' → minta jumlah minimum pembelian."""
    query = update.callback_query
    assert query is not None
    assert update.effective_user is not None
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Akses ditolak!", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    promo = db.get_promo()
    current = (
        f"Promo aktif: beli ≥*{promo['min_qty']}* akun → *{fmt_price(promo['promo_price'])}*/akun\n\n"
        if promo["min_qty"] > 0
        else "Belum ada promo aktif.\n\n"
    )

    await query.edit_message_text(
        f"🎁 *Atur Promo — Volume Discount*\n\n"
        f"{current}"
        f"Langkah 1/2: Berapa *jumlah minimum* akun yang harus dibeli "
        f"untuk mendapatkan harga promo?\n\n"
        f"Kirim angka saja. Contoh: `3`\n\n"
        f"Kirim /cancel untuk membatalkan.",
        parse_mode="Markdown",
    )
    return WAITING_PROMO_INPUT


async def handle_promo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Terima min_qty (langkah 1), lalu minta promo_price (langkah 2)."""
    assert update.effective_user is not None
    assert update.message is not None
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    assert update.message.text is not None

    # Langkah 2: sudah punya min_qty, sekarang terima promo_price
    if context.user_data.get("promo_min_qty"):
        raw = update.message.text.strip().replace(".", "").replace(",", "")
        if not raw.isdigit() or int(raw) < 1:
            await update.message.reply_text(
                "⚠️ Input tidak valid. Kirim harga dalam Rupiah (angka saja, contoh: `15000`).",
                parse_mode="Markdown",
            )
            return WAITING_PROMO_INPUT

        promo_price = int(raw)
        min_qty = int(context.user_data.pop("promo_min_qty"))
        db.set_promo(min_qty, promo_price)

        await update.message.reply_text(
            f"✅ *Promo berhasil disimpan!*\n\n"
            f"🎁 Beli ≥ *{min_qty}* akun → harga *{fmt_price(promo_price)}*/akun\n\n"
            f"_Contoh: beli {min_qty} akun = total {fmt_price(promo_price * min_qty)} "
            f"(hemat {fmt_price(0)} dibanding harga normal)._",
            parse_mode="Markdown",
            reply_markup=kb_admin_back(),
        )
        return ConversationHandler.END

    # Langkah 1: terima min_qty
    raw = update.message.text.strip().replace(".", "").replace(",", "")
    if not raw.isdigit() or int(raw) < 1:
        await update.message.reply_text(
            "⚠️ Input tidak valid. Kirim angka minimal `1` (contoh: `3`).",
            parse_mode="Markdown",
        )
        return WAITING_PROMO_INPUT

    min_qty = int(raw)
    context.user_data["promo_min_qty"] = min_qty

    await update.message.reply_text(
        f"Langkah 2/2: Berapa *harga per akun* saat promo berlaku "
        f"(beli ≥ {min_qty} akun)?\n\n"
        f"Kirim harga dalam Rupiah (angka saja). Contoh: `15000`\n\n"
        f"Kirim /cancel untuk membatalkan.",
        parse_mode="Markdown",
    )
    return WAITING_PROMO_INPUT


async def handle_promo_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nonaktifkan promo (callback langsung, bukan conversation)."""
    query = update.callback_query
    assert query is not None
    assert update.effective_user is not None
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Akses ditolak!", show_alert=True)
        return

    db.set_promo(0, 0)
    await query.answer("Promo dinonaktifkan.")
    await query.edit_message_text(
        "🎁 *Tambah Promo*\n\nStatus promo saat ini: ❌ *Nonaktif*\n\n"
        "Klik tombol di bawah untuk mengatur promo volume discount.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✏️ Atur Promo", callback_data="admin_promo_set"
                    )
                ],
                [InlineKeyboardButton("🔙 Kembali", callback_data="admin_menu")],
            ]
        ),
    )


# ------------------------------------------------------------------
# Broadcast — ConversationHandler (kirim pesan ke semua user)
# ------------------------------------------------------------------


async def entry_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: admin klik 'Broadcast' → minta isi pesan."""
    query = update.callback_query
    assert query is not None
    assert update.effective_user is not None
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Akses ditolak!", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    total = db.get_user_count()

    if total == 0:
        await query.edit_message_text(
            "📢 *Broadcast*\n\n"
            "Belum ada user yang tercatat. Broadcast tersedia setelah ada "
            "user yang memakai bot.",
            parse_mode="Markdown",
            reply_markup=kb_admin_back(),
        )
        return ConversationHandler.END

    await query.edit_message_text(
        f"📢 *Broadcast ke Semua User*\n\n"
        f"Pesan akan dikirim ke *{total}* user yang pernah memakai bot.\n\n"
        f"Kirim isi pesan broadcast sekarang (boleh memakai format Markdown).\n\n"
        f"Kirim /cancel untuk membatalkan.",
        parse_mode="Markdown",
    )
    return WAITING_BROADCAST_INPUT


async def handle_broadcast_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Terima isi pesan broadcast lalu kirim ke semua user yang tercatat."""
    assert update.effective_user is not None
    assert update.message is not None
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text or ""
    if not text.strip():
        await update.message.reply_text(
            "⚠️ Pesan kosong. Kirim teks broadcast, atau /cancel untuk batal."
        )
        return WAITING_BROADCAST_INPUT

    user_ids = db.get_all_user_ids()
    admin_id = update.effective_user.id

    status_msg = await update.message.reply_text(
        f"📤 Mengirim broadcast ke {len(user_ids)} user...",
    )

    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode="Markdown",
            )
            sent += 1
        except Exception:
            # Fallback tanpa Markdown bila parsing gagal
            try:
                await context.bot.send_message(chat_id=uid, text=text)
                sent += 1
            except Exception as exc:
                failed += 1
                logger.warning("Broadcast gagal ke %s: %s", uid, exc)
        # Hindari rate limit Telegram (~30 pesan/detik)
        await asyncio.sleep(0.05)

    try:
        await status_msg.edit_text(
            f"✅ *Broadcast selesai!*\n\n"
            f"📬 Terkirim: *{sent}*\n"
            f"⚠️ Gagal: *{failed}*\n"
            f"👥 Total user: *{len(user_ids)}*",
            parse_mode="Markdown",
            reply_markup=kb_admin_back(),
        )
    except Exception:
        await update.message.reply_text(
            f"✅ Broadcast selesai! Terkirim: {sent}, Gagal: {failed}",
            reply_markup=kb_admin_back(),
        )

    logger.info(
        "Broadcast oleh admin %s: %d terkirim, %d gagal", admin_id, sent, failed
    )
    return ConversationHandler.END


# ------------------------------------------------------------------
# GitHub Edu Apply — ConversationHandler
# ------------------------------------------------------------------


def _parse_edu_accounts(text: str) -> list[tuple[str, str, str]]:
    """Parse input akun edu (satu per baris) → list (username, secret, password).

    Format: username:secret[:password]  (pemisah ':' atau '|', password opsional).
    """
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sep = "|" if "|" in line else ":"
        parts = [p.strip() for p in line.split(sep)]
        username = parts[0] if len(parts) > 0 else ""
        secret = parts[1] if len(parts) > 1 else ""
        password = parts[2] if len(parts) > 2 and parts[2] else _ACCOUNT_PASSWORD
        if username and secret:
            out.append((username, secret, password))
    return out


async def entry_edu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: admin klik 'Apply GitHub Edu' → minta daftar akun."""
    query = update.callback_query
    assert query is not None
    assert update.effective_user is not None
    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Akses ditolak!", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_text(
        "🎓 *Apply GitHub Education*\n\n"
        "Kirim akun GitHub yang akan di-apply, *satu akun per baris*.\n\n"
        "Format tiap baris:\n"
        "`username:secretcode`\n"
        "`username:secretcode:password`\n\n"
        "_secretcode = TOTP 2FA secret. Password opsional "
        "(default `.ganteng123`)._\n\n"
        "Bisa banyak akun sekaligus (diproses berurutan).\n\n"
        "Ketik /cancel untuk membatalkan.",
        parse_mode="Markdown",
    )
    return WAITING_EDU_INPUT


async def handle_edu_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Terima daftar akun, jalankan apply, tampilkan log dalam 1 pesan (ditimpa)."""
    assert update.effective_user is not None
    assert update.message is not None
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    accounts = _parse_edu_accounts(update.message.text or "")
    if not accounts:
        await update.message.reply_text(
            "❌ Format tidak valid. Kirim minimal `username:secretcode` "
            "(satu akun per baris).\n\nCoba lagi atau /cancel.",
            parse_mode="Markdown",
        )
        return WAITING_EDU_INPUT

    total = len(accounts)
    status_msg = await update.message.reply_text(
        f"🎓 *Apply GitHub Edu* — {total} akun\n\n⏳ Memulai...",
        parse_mode="Markdown",
    )

    summary = []
    for idx, (username, secret, password) in enumerate(accounts, start=1):
        result = await _run_edu_apply_single(
            context, status_msg, username, secret, password, idx, total
        )
        st = (result or {}).get("status", "failed")
        edu = (result or {}).get("edu_status") or "-"
        icon = "✅" if st == "success" else "❌"
        summary.append(f"{icon} `{_escape_md(username)}` — {st} (edu: {edu})")

    # Ringkasan akhir (timpa pesan log)
    try:
        await status_msg.edit_text(
            f"🎓 *Apply GitHub Edu Selesai* — {total} akun\n\n"
            + "\n".join(summary),
            parse_mode="Markdown",
            reply_markup=kb_admin_back(),
        )
    except Exception:
        await update.message.reply_text(
            "🎓 Apply selesai.\n" + "\n".join(s.replace("`", "") for s in summary),
            reply_markup=kb_admin_back(),
        )
    return ConversationHandler.END


async def _run_edu_apply_single(
    context, status_msg, username, secret, password, idx, total
) -> dict:
    """Jalankan apply 1 akun di thread terpisah; update 1 pesan log (throttled)."""
    import asyncio as _asyncio

    loop = _asyncio.get_running_loop()
    log_lines: list[str] = []
    header = f"🎓 *Apply GitHub Edu* ({idx}/{total})\n👤 `{_escape_md(username)}`\n"
    last_edit = {"t": 0.0}

    def _sink(line: str) -> None:
        # Dipanggil dari thread worker — jadwalkan update pesan secara throttled.
        text = (line or "").strip()
        if not text:
            return
        log_lines.append(text)

        async def _do_edit():
            import time as _t

            now = _t.monotonic()
            # Throttle: edit maksimal tiap ~1.5 detik agar tidak kena rate limit
            if now - last_edit["t"] < 1.5:
                return
            last_edit["t"] = now
            tail = "\n".join(log_lines[-12:])  # 12 baris terakhir saja
            body = f"{header}\n```\n{tail[-3500:]}\n```"
            try:
                await status_msg.edit_text(body, parse_mode="Markdown")
            except Exception:
                pass

        try:
            _asyncio.run_coroutine_threadsafe(_do_edit(), loop)
        except Exception:
            pass

    def _worker() -> dict:
        from automation.edu_apply import apply_account_for_bot

        return apply_account_for_bot(
            username=username,
            secret_key=secret,
            password=password,
            proxy="",
            headless=True,
            log_sink=_sink,
        )

    result = await _asyncio.to_thread(_worker)

    # Update final untuk akun ini (pasti tampil, tanpa throttle)
    tail = "\n".join(log_lines[-12:])
    st = (result or {}).get("status", "failed")
    msg = (result or {}).get("message", "")
    icon = "✅" if st == "success" else "❌"
    try:
        await status_msg.edit_text(
            f"{header}\n{icon} *{st}* — {_escape_md(msg)}\n\n```\n{tail[-3000:]}\n```",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    return result or {}


# ------------------------------------------------------------------
# Order management
# ------------------------------------------------------------------


async def _show_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan status promo aktif (dipanggil dari callback non-conversation)."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    promo = db.get_promo()
    min_qty = promo["min_qty"]
    promo_price = promo["promo_price"]
    status = (
        f"🎁 *Aktif* — beli ≥`{min_qty}` akun → *{fmt_price(promo_price)}*/akun"
        if min_qty > 0
        else "❌ *Nonaktif*"
    )

    await query.edit_message_text(
        f"🎁 *Tambah Promo*\n\n"
        f"Status promo saat ini: {status}\n\n"
        f"Klik tombol di bawah untuk mengatur promo volume discount.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✏️ Atur Promo", callback_data="admin_promo_set"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🗑 Nonaktifkan Promo", callback_data="admin_promo_off"
                    )
                ],
                [InlineKeyboardButton("🔙 Kembali", callback_data="admin_menu")],
            ]
        ),
    )


async def _show_admin_order_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str
) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    order = db.get_order(order_id)
    if not order:
        await query.answer("Pesanan tidak ditemukan!", show_alert=True)
        return

    emoji = get_status_emoji(order["status"])
    label = get_status_label(order["status"])

    text = (
        f"📦 *Detail Pesanan*\n\n"
        f"🆔 Order ID: `{order_id}`\n"
        f"👤 User: @{order['username']} (ID: `{order['user_id']}`)\n"
        f"📦 Produk: *{_escape_md(order['product_name'])}*\n"
        f"💰 Harga: {fmt_price(order['price'])}\n"
        f"📅 Dibuat: {order['created_at'][:16].replace('T', ' ')}\n"
        f"Status: {emoji} *{label}*"
    )

    rows = []
    if order["status"] == "payment_sent":
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Konfirmasi", callback_data=f"admin_confirm_{order_id}"
                ),
                InlineKeyboardButton(
                    "❌ Tolak", callback_data=f"admin_reject_{order_id}"
                ),
            ]
        )
    rows.append(
        [InlineKeyboardButton("🔙 Admin Menu", callback_data="admin_menu")]
    )

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def _confirm_order(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str
) -> None:
    query = update.callback_query
    assert query is not None
    assert update.effective_user is not None

    order = db.get_order(order_id)
    if not order:
        await query.answer("Pesanan tidak ditemukan!", show_alert=True)
        return
    if order["status"] not in ("payment_sent", "pending_payment"):
        await query.answer("Pesanan sudah diproses!", show_alert=True)
        return

    qty = order.get("quantity", 1) or 1

    if order["product_id"] == "ghs_do":
        # GHS DO bulk: ambil sebanyak qty akun GHS
        accounts = db.take_stock_accounts(order["product_id"], qty)
        if not accounts:
            await query.answer(
                "⚠️ Stok habis! Tambah stok dulu sebelum konfirmasi.", show_alert=True
            )
            return

        # Simpan semua akun internal; klaim dimulai dari index 0
        db.update_order(
            order_id,
            status="confirmed",
            ghs_account_used="\n".join(accounts),
            quantity=len(accounts),
            do_claim_index=0,
        )

        try:
            from handlers.do_claim import send_do_claim_prompt

            await send_do_claim_prompt(context.bot, order["user_id"], order_id)
        except Exception as exc:
            logger.error("Failed to notify user %s: %s", order["user_id"], exc)

        confirmed_text = (
            f"✅ *DIKONFIRMASI*\n\n"
            f"Order ID: `{order_id}`\n"
            f"User: @{order['username']}\n"
            f"Produk: {_escape_md(order['product_name'])}\n"
            f"Akun GHS (internal, {len(accounts)}x):\n"
            + "\n".join(f"`{a}`" for a in accounts)
            + f"\n\nDikonfirmasi oleh: {update.effective_user.first_name}"
        )
    else:
        account = db.take_stock_account(order["product_id"])
        if not account:
            await query.answer(
                "⚠️ Stok habis! Tambah stok dulu sebelum konfirmasi.", show_alert=True
            )
            return
        # Produk biasa: kirim akun dengan format kartu, lalu hapus dari record
        delivery_succeeded = False
        try:
            msg = _fmt_delivery_text([account], order["product_name"], order_id)
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=msg,
                parse_mode="Markdown",
            )
            delivery_succeeded = True
        except Exception as exc:
            logger.error("Failed to notify user %s: %s", order["user_id"], exc)

        db.update_order(
            order_id,
            status="confirmed",
            account_delivered="[TERKIRIM ✓]" if delivery_succeeded else account,
        )

        confirmed_text = (
            f"✅ *DIKONFIRMASI*\n\n"
            f"Order ID: `{order_id}`\n"
            f"User: @{order['username']}\n"
            f"Produk: {_escape_md(order['product_name'])}\n"
            f"Akun terkirim: `{account}` ✓\n\n"
            f"Dikonfirmasi oleh: {update.effective_user.first_name}"
        )
    try:
        await query.edit_message_caption(caption=confirmed_text, parse_mode="Markdown")
    except Exception:
        try:
            await query.edit_message_text(confirmed_text, parse_mode="Markdown")
        except Exception as exc:
            logger.error("Could not edit admin message: %s", exc)

    await query.answer("✅ Pesanan dikonfirmasi & akun terkirim!")


async def _reject_order(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str
) -> None:
    query = update.callback_query
    assert query is not None
    assert update.effective_user is not None

    order = db.get_order(order_id)
    if not order:
        await query.answer("Pesanan tidak ditemukan!", show_alert=True)
        return
    if order["status"] not in ("payment_sent", "pending_payment"):
        await query.answer("Pesanan sudah diproses!", show_alert=True)
        return

    db.update_order(order_id, status="rejected")

    # Notify user
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"❌ *Pembayaran Ditolak*\n\n"
                f"Order ID: `{order_id}`\n\n"
                f"Maaf, bukti pembayaranmu tidak valid atau tidak sesuai nominal.\n"
                f"Hubungi admin jika ada pertanyaan."
            ),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.error("Failed to notify user %s: %s", order["user_id"], exc)

    rejected_text = (
        f"❌ *DITOLAK*\n\n"
        f"Order ID: `{order_id}`\n"
        f"User: @{order['username']}\n\n"
        f"Ditolak oleh: {update.effective_user.first_name}"
    )
    try:
        await query.edit_message_caption(caption=rejected_text, parse_mode="Markdown")
    except Exception:
        try:
            await query.edit_message_text(rejected_text, parse_mode="Markdown")
        except Exception as exc:
            logger.error("Could not edit admin message: %s", exc)

    await query.answer("❌ Pesanan ditolak.")


# ------------------------------------------------------------------
# Auto-confirm (called by webhook / manual check)
# ------------------------------------------------------------------


async def auto_confirm_order(order_id: str, bot) -> bool:
    """
    Automatically confirm an order and deliver the account(s).
    Support bulk purchase: qty > 1 → deliver multiple accounts.
    GHS DO selalu qty=1 (satu DO account per transaksi).
    Returns True if successfully confirmed.
    """
    # ── Guard atomik — cegah double-delivery ────────────────────────
    # try_lock_order_for_confirm membaca + mengubah status ke 'processing'
    # dalam satu operasi terkunci. Jika dua caller tiba bersamaan,
    # hanya satu yang mendapat True; yang lain langsung return False.
    if not db.try_lock_order_for_confirm(order_id):
        logger.info(
            "auto_confirm: order %s dilewati (sudah diproses/dikunci)", order_id
        )
        return False

    order = db.get_order(order_id)
    if not order:
        logger.warning("auto_confirm: order %s not found setelah lock", order_id)
        return False

    qty = order.get("quantity", 1)
    is_ghs_do = order["product_id"] == "ghs_do"

    # Promo volume discount: catat harga promo yang berlaku saat konfirmasi.
    # GHS DO juga ikut promo (bisa bulk), namun tidak menampilkan kartu akun.
    promo = db.get_promo()
    promo_price = (
        promo["promo_price"]
        if promo["min_qty"] > 0 and qty >= promo["min_qty"]
        else 0
    )

    # ── Ambil akun dari stok ──────────────────────────────────────────
    if qty == 1:
        account = db.take_stock_account(order["product_id"])
        accounts = [account] if account else []
    else:
        accounts = db.take_stock_accounts(order["product_id"], qty)

    if not accounts:
        logger.error(
            "auto_confirm: no stock for product %s (order %s)",
            order["product_id"],
            order_id,
        )
        # Notify admins
        stock_msg_md = (
            f"⚠️ *STOK HABIS — AUTO-CONFIRM GAGAL*\n\n"
            f"Order `{order_id}` sudah dibayar "
            f"tapi stok *{_escape_md(order['product_name'])}* kosong!\n\n"
            f"Segera tambah stok dan konfirmasi manual."
        )
        await _notify_admins(bot, stock_msg_md)
        db.update_order(order_id, status="paid")
        db.unlock_order(order_id)
        return False

    # ── Jika hanya sebagian stok tersedia (bulk kurang) ──────────────
    delivered_qty = len(accounts)
    if delivered_qty < qty:
        logger.warning(
            "auto_confirm: partial stock — requested %d, got %d (order %s)",
            qty,
            delivered_qty,
            order_id,
        )

    # ── Kirim notifikasi ke user & simpan ke order ───────────────────
    if is_ghs_do:
        # GHS DO: simpan SEMUA akun GHS internal (mendukung bulk), mulai dari klaim ke-0.
        # `do_claim_index` melacak berapa akun yang sudah diklaim user.
        db.update_order(
            order_id,
            status="confirmed",
            ghs_account_used="\n".join(accounts),
            quantity=delivered_qty,
            do_claim_index=0,
        )
        try:
            from handlers.do_claim import send_do_claim_prompt

            await send_do_claim_prompt(bot, order["user_id"], order_id)
        except Exception as exc:
            logger.error(
                "auto_confirm: failed to notify user %s: %s", order["user_id"], exc
            )
    else:
        # Produk biasa: kirim akun dengan format kartu, hapus dari record
        delivery_succeeded = False
        try:
            msg = _fmt_delivery_text(
                accounts,
                order["product_name"],
                order_id,
                qty=qty,
                partial=delivered_qty < qty,
                promo_price=promo_price,
            )
            await bot.send_message(
                chat_id=order["user_id"],
                text=msg,
                parse_mode="Markdown",
            )
            delivery_succeeded = True
        except Exception as exc:
            logger.error(
                "auto_confirm: failed to notify user %s: %s", order["user_id"], exc
            )

        account_stored = "[TERKIRIM ✓]" if delivery_succeeded else "\n".join(accounts)
        db.update_order(
            order_id,
            status="confirmed",
            account_delivered=account_stored,
            quantity=delivered_qty,
        )

    # ── Notify admins ─────────────────────────────────────────────────
    safe_user = _escape_md(order['username'])
    safe_product = _escape_md(order['product_name'])

    if is_ghs_do:
        acct_info = f"Akun GHS (internal): `{accounts[0]}`"
        acct_info_plain = f"Akun GHS (internal): {accounts[0]}"
    elif delivered_qty == 1:
        acct_info = f"Akun: `{accounts[0]}`"
        acct_info_plain = f"Akun: {accounts[0]}"
    else:
        acct_info = f"Akun ({delivered_qty}x):\n" + "\n".join(
            [f"`{a}`" for a in accounts]
        )
        acct_info_plain = f"Akun ({delivered_qty}x):\n" + "\n".join(accounts)

    qty_info = f"\nQty: {delivered_qty}x" if delivered_qty > 1 else ""

    admin_md = (
        f"✅ *Terkonfirmasi Otomatis*\n\n"
        f"Order: `{order_id}`\n"
        f"User: @{safe_user}\n"
        f"Produk: {safe_product}{qty_info}\n"
        f"{acct_info}"
    )
    admin_plain = (
        f"✅ Terkonfirmasi Otomatis\n\n"
        f"Order: {order_id}\n"
        f"User: @{order['username']}\n"
        f"Produk: {_escape_md(order['product_name'])}{qty_info}\n"
        f"{acct_info_plain}"
    )

    await _notify_admins(bot, admin_md, admin_plain)

    logger.info(
        "auto_confirm: order %s confirmed — %d account(s) delivered",
        order_id,
        delivered_qty,
    )
    return True


# ------------------------------------------------------------------
# Settings — ganti payment gateway
# ------------------------------------------------------------------


async def _show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan halaman pengaturan payment gateway."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    active = db.get_setting("active_gateway", "pakasir")
    active_label = _GATEWAY_LABELS.get(active, active)

    pakasir_status = (
        "✅ Terkonfigurasi"
        if PAKASIR_ENABLED
        else "❌ Belum dikonfigurasi (PAKASIR_PROJECT_SLUG kosong)"
    )

    text = (
        f"⚙️ *Pengaturan Payment Gateway*\n\n"
        f"Gateway aktif saat ini: *{active_label}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 *Pakasir:* {pakasir_status}\n"
        f"🏦 *Manual Transfer:* ✅ Selalu tersedia\n\n"
        f"Pilih gateway yang ingin diaktifkan:"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=kb_settings_menu(active)
    )


async def _handle_switch_gateway(
    update: Update, context: ContextTypes.DEFAULT_TYPE, gateway: str
) -> None:
    """Simpan pilihan gateway baru dan konfirmasi ke admin."""
    query = update.callback_query
    assert query is not None
    assert update.effective_user is not None

    if gateway not in _GATEWAY_LABELS:
        await query.answer("Gateway tidak dikenal!", show_alert=True)
        return

    # Validasi gateway tersedia sebelum mengaktifkan
    if gateway == "pakasir" and not PAKASIR_ENABLED:
        await query.answer(
            "⚠️ Pakasir belum dikonfigurasi!\n"
            "Isi PAKASIR_PROJECT_SLUG di file .env terlebih dahulu.",
            show_alert=True,
        )
        return

    db.set_setting("active_gateway", gateway)
    label = _GATEWAY_LABELS[gateway]

    logger.info("Admin %s mengganti gateway ke '%s'", update.effective_user.id, gateway)
    await query.answer(f"✅ Gateway diubah ke {label}!", show_alert=False)

    # Refresh halaman pengaturan
    active = db.get_setting("active_gateway", "pakasir")
    active_label = _GATEWAY_LABELS.get(active, active)

    pakasir_status = (
        "✅ Terkonfigurasi"
        if PAKASIR_ENABLED
        else "❌ Belum dikonfigurasi (PAKASIR_PROJECT_SLUG kosong)"
    )

    text = (
        f"⚙️ *Pengaturan Payment Gateway*\n\n"
        f"Gateway aktif saat ini: *{active_label}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 *Pakasir:* {pakasir_status}\n"
        f"🏦 *Manual Transfer:* ✅ Selalu tersedia\n\n"
        f"Pilih gateway yang ingin diaktifkan:"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=kb_settings_menu(active)
    )


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------


async def _show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    stats = db.get_stats()
    products = db.load_products()

    stock_lines = ""
    for pid, count in stats["stock"].items():
        p = products.get(pid, {})
        stock_lines += f"{p.get('emoji', '📦')} {p.get('name', pid)}: *{count}* stok\n"

    # Active gateway info
    active_gw = db.get_setting("active_gateway", "pakasir")
    active_gw_label = _GATEWAY_LABELS.get(active_gw, active_gw)

    text = (
        f"📊 *Statistik Toko*\n\n"
        f"⚙️ *Gateway Aktif:* {active_gw_label}\n"
        f"📦 *Stok Saat Ini:*\n{stock_lines}\n"
        f"📋 *Ringkasan Pesanan:*\n"
        f"Total       : *{stats['total_orders']}*\n"
        f"✅ Selesai   : *{stats['confirmed']}*\n"
        f"💚 Auto-paid : *{stats.get('paid_auto', 0)}*\n"
        f"📤 Menunggu  : *{stats['payment_sent']}*\n"
        f"⏳ Pending   : *{stats['pending']}*\n"
        f"❌ Ditolak   : *{stats['rejected']}*\n"
        f"🚫 Dibatalkan: *{stats['cancelled']}*\n\n"
        f"💰 *Total Pendapatan:* {fmt_price(stats['total_revenue'])}"
    )
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 Kembali ke Pesanan", callback_data="admin_all_orders"
                    )
                ],
                [InlineKeyboardButton("🏠 Admin Menu", callback_data="admin_menu")],
            ]
        ),
    )


# ------------------------------------------------------------------
# All orders
# ------------------------------------------------------------------


async def _show_all_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    orders = db.get_all_orders()
    if not orders:
        await query.edit_message_text(
            "📜 *Semua Pesanan*\n\nBelum ada pesanan.",
            parse_mode="Markdown",
            reply_markup=_kb_orders_nav(),
        )
        return

    lines = []
    for o in orders[:25]:
        emoji = get_status_emoji(o["status"])
        # Escape username & nama produk agar tidak merusak parsing Markdown
        uname = _escape_md(o["username"])
        pname = _escape_md(o["product_name"])
        lines.append(f"{emoji} `{o['id']}` @{uname} · {pname}")

    suffix = f"\n\n_...dan {len(orders) - 25} pesanan lagi_" if len(orders) > 25 else ""
    text = "📜 *Semua Pesanan*\n\n" + "\n".join(lines) + suffix

    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=_kb_orders_nav()
    )


def _kb_orders_nav() -> InlineKeyboardMarkup:
    """Navigasi di halaman Semua Pesanan: lihat Statistik atau kembali ke menu."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Statistik", callback_data="admin_stats")],
            [InlineKeyboardButton("🔙 Admin Menu", callback_data="admin_menu")],
        ]
    )


# ------------------------------------------------------------------
# Master admin callback dispatcher
# ------------------------------------------------------------------


async def handle_admin_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    assert query is not None
    assert query.data is not None
    assert update.effective_user is not None

    if not is_admin(update.effective_user.id):
        await query.answer("⛔ Akses ditolak!", show_alert=True)
        return

    data: str = query.data

    if data == "admin_menu":
        await query.answer()
        await query.edit_message_text(
            f"⚙️ *Admin Panel — {STORE_NAME}*",
            parse_mode="Markdown",
            reply_markup=kb_admin_menu(),
        )

    elif data == "admin_stock":
        await _show_admin_stock(update, context)

    elif data == "admin_promo":
        await _show_promo(update, context)

    elif data == "admin_promo_off":
        await handle_promo_off(update, context)

    elif data.startswith("admin_view_order_"):
        await _show_admin_order_detail(
            update, context, data.removeprefix("admin_view_order_")
        )

    elif data.startswith("admin_confirm_"):
        await _confirm_order(update, context, data.removeprefix("admin_confirm_"))

    elif data.startswith("admin_reject_"):
        await _reject_order(update, context, data.removeprefix("admin_reject_"))

    elif data == "admin_stats":
        await _show_stats(update, context)

    elif data == "admin_all_orders":
        await _show_all_orders(update, context)

    elif data == "admin_settings":
        await _show_settings(update, context)

    elif data.startswith("admin_gw_set_"):
        await _handle_switch_gateway(
            update, context, data.removeprefix("admin_gw_set_")
        )

    else:
        await query.answer("Perintah tidak dikenali.", show_alert=True)
