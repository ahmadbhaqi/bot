import logging
import urllib.parse
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from config import (
    PAKASIR_DEFAULT_METHOD,
    PAKASIR_ENABLED,
    PAYMENT_INFO,
    RONZZPAY_DEFAULT_METHOD,
    RONZZPAY_ENABLED,
    STORE_NAME,
)
from database import db

logger = logging.getLogger(__name__)


def _build_qr_url(data: str, size: int = 400) -> str:
    """
    Buat URL gambar QR code dari api.qrserver.com.
    Telegram akan men-download gambarnya langsung dari URL ini.
    Tidak membutuhkan library tambahan apapun.
    """
    encoded = urllib.parse.quote(data, safe="")
    return (
        f"https://api.qrserver.com/v1/create-qr-code/"
        f"?size={size}x{size}&data={encoded}&margin=10&format=png"
    )


# Payment gateway client singletons (initialized in main.py)
if TYPE_CHECKING:
    from payment.pakasir import PakasirClient as PakasirClientType
    from payment.ronzzpay import RonzzPayClient

_ronzzpay_client: "Optional[RonzzPayClient]" = None
_pakasir_client: "Optional[PakasirClientType]" = None


def set_ronzzpay_client(client):
    global _ronzzpay_client
    _ronzzpay_client = client


def set_pakasir_client(client):
    global _pakasir_client
    _pakasir_client = client


# ------------------------------------------------------------------
# Conversation states (exported so main.py can import them)
# ------------------------------------------------------------------
WAITING_PAYMENT_PROOF = 1

# Label waktu auto-confirm untuk ditampilkan ke user (harus sync dengan _POLL_INTERVAL di main.py)
_POLL_INTERVAL_LABEL = "5 detik"

# Batas waktu pembayaran yang ditampilkan ke user (harus sync dengan _MAX_POLL_DURATION di main.py)
_ORDER_EXPIRY_MINUTES = 10


def _order_expiry_str() -> str:
    """Return string waktu kedaluwarsa pesanan (sekarang + 10 menit), format HH:MM:SS."""
    from datetime import datetime, timedelta

    exp = datetime.now() + timedelta(minutes=_ORDER_EXPIRY_MINUTES)
    return exp.strftime("%H:%M:%S")


def _order_expiry_iso() -> str:
    """Return ISO string waktu kedaluwarsa pesanan dalam UTC (untuk disimpan ke DB)."""
    from datetime import datetime, timezone, timedelta

    exp = datetime.now(timezone.utc) + timedelta(minutes=_ORDER_EXPIRY_MINUTES)
    return exp.isoformat()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _escape_md_user(text: str) -> str:
    """Escape karakter khusus Telegram Markdown V1 dalam teks user-generated.

    Mencegah kegagalan parse_mode='Markdown' saat username atau nama produk
    mengandung underscore (_), asterisk (*), atau karakter Markdown lainnya.
    """
    for ch in r"\_*[]`~":
        text = text.replace(ch, f"\\{ch}")
    return text


async def _edit_or_reply_text(query, *args, **kwargs):
    """Safely edit message text. If current message is a photo (has no text),
    delete it and send a new text message. Prevents 'There is no text' BadRequest.
    """
    try:
        await query.edit_message_text(*args, **kwargs)
    except Exception as exc:
        exc_str = str(exc).lower()
        if "message is not modified" in exc_str:
            # Just ignore if we are trying to edit to the exact same text
            pass
        elif "there is no text" in exc_str:
            try:
                if query.message:
                    await query.message.delete()
            except Exception:
                pass
            if query.message:
                bot = query.get_bot()
                # args[0] adalah text (sama seperti argumen pertama edit_message_text).
                # send_message() menempatkan chat_id sebagai positional pertama, jadi
                # kita harus memetakan text ke keyword 'text' agar tidak bentrok.
                text = args[0] if args else kwargs.pop("text", "")
                send_kwargs = {k: v for k, v in kwargs.items() if k != "text"}
                await bot.send_message(
                    chat_id=query.message.chat_id, text=text, **send_kwargs
                )
        else:
            raise


def fmt_price(price: int) -> str:
    """Format integer price as Indonesian Rupiah string."""
    return "Rp " + f"{price:,}".replace(",", ".")


STATUS_EMOJI = {
    "pending_payment": "⏳",
    "payment_sent": "📤",
    "confirmed": "✅",
    "rejected": "❌",
    "cancelled": "🚫",
    "paid": "💚",
}

STATUS_LABEL = {
    "pending_payment": "Menunggu Pembayaran",
    "payment_sent": "Bukti Bayar Terkirim",
    "confirmed": "Selesai",
    "rejected": "Ditolak",
    "cancelled": "Dibatalkan",
    "paid": "Terbayar (Otomatis)",
}


def get_status_emoji(status: str) -> str:
    return STATUS_EMOJI.get(status, "❓")


def get_status_label(status: str) -> str:
    return STATUS_LABEL.get(status, status)


def _fmt_join_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d %b %Y")
    except Exception:
        return iso[:10]


# ------------------------------------------------------------------
# Keyboards
# ------------------------------------------------------------------


def kb_main_menu() -> InlineKeyboardMarkup:
    """Main menu: 3 tombol utama."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒  Order / Beli", callback_data="menu_order")],
            [
                InlineKeyboardButton("📋  Cara Order", callback_data="menu_cara_order"),
                InlineKeyboardButton("👤  Profil", callback_data="menu_profil"),
            ],
        ]
    )


def kb_order_menu() -> InlineKeyboardMarkup:
    """Sub-menu pilih produk — 1 tombol per produk (nama + harga + stok)."""
    products = db.load_products()
    rows = []

    for pid, p in products.items():
        stock = db.get_stock_count(pid)
        stock_lbl = f"✅ {stock} stok" if stock > 0 else "❌ Habis"
        extra = " ⚡" if pid == "ghs_do" else ""

        rows.append(
            [
                InlineKeyboardButton(
                    f"{p['emoji']}  {p['name']}{extra}  •  {fmt_price(p['price'])}  •  {stock_lbl}",
                    callback_data=f"product_{pid}",
                )
            ]
        )

    rows.append(
        [InlineKeyboardButton("🔙  Kembali ke Menu", callback_data="main_menu")]
    )
    return InlineKeyboardMarkup(rows)


def kb_product_detail(product_id: str, has_stock: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_stock:
        rows.append(
            [
                InlineKeyboardButton(
                    "🛒  Beli Sekarang", callback_data=f"buy_{product_id}"
                )
            ]
        )
    else:
        rows.append([InlineKeyboardButton("⚠️  Stok Habis", callback_data="no_stock")])
    rows.append(
        [InlineKeyboardButton("🔙  Kembali ke Produk", callback_data="menu_order")]
    )
    return InlineKeyboardMarkup(rows)


def kb_product_detail_qty(
    product_id: str, qty: int, stock: int
) -> InlineKeyboardMarkup:
    """Keyboard halaman detail produk dengan kontrol QTY +/- untuk bulk purchase.
    GHS DO kini mendukung bulk: tiap unit menghasilkan satu klaim DO terpisah."""
    product = db.get_product(product_id)
    unit_price = product["price"] if product else 0
    max_qty = min(stock, 10)
    rows = []

    if stock > 0:
        # Baris QTY selector: [➖]  QTY: n  [➕]
        can_dec = qty > 1
        can_inc = qty < max_qty
        rows.append(
            [
                InlineKeyboardButton(
                    "➖",
                    callback_data=f"qty_down_{product_id}_{qty}"
                    if can_dec
                    else "noop",
                ),
                InlineKeyboardButton(f"  QTY: {qty}  ", callback_data="noop"),
                InlineKeyboardButton(
                    "➕",
                    callback_data=f"qty_up_{product_id}_{qty}"
                    if can_inc
                    else "noop",
                ),
            ]
        )
        # Hitung total dengan promo volume discount jika berlaku
        promo = db.get_promo()
        effective_price = (
            promo["promo_price"]
            if promo["min_qty"] > 0
            and promo["promo_price"] > 0
            and qty >= promo["min_qty"]
            else unit_price
        )
        total = effective_price * qty
        promo_tag = " 🎁" if effective_price < unit_price else ""
        beli_label = (
            f"🛒  Beli {qty} Item — {fmt_price(total)}{promo_tag}"
            if qty > 1
            else f"🛒  Beli Sekarang — {fmt_price(total)}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    beli_label, callback_data=f"buy_{product_id}_{qty}"
                )
            ]
        )
    else:
        rows.append([InlineKeyboardButton("⚠️  Stok Habis", callback_data="no_stock")])

    rows.append(
        [InlineKeyboardButton("🔙  Kembali ke Produk", callback_data="menu_order")]
    )
    return InlineKeyboardMarkup(rows)


def kb_order_payment(order_id: str, is_auto: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not is_auto:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅  Sudah Transfer", callback_data=f"sudah_bayar_{order_id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "🔄  Cek Status Bayar", callback_data=f"check_pay_{order_id}"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "❌  Batalkan Pesanan", callback_data=f"cancel_order_{order_id}"
            )
        ]
    )
    rows.append([InlineKeyboardButton("🏠  Menu Utama", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠  Menu Utama", callback_data="main_menu")]]
    )


def kb_back_order() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒  Lihat Produk Lain", callback_data="menu_order")],
            [InlineKeyboardButton("🏠  Menu Utama", callback_data="main_menu")],
        ]
    )


# ------------------------------------------------------------------
# /start
# ------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    # Catat user untuk fitur broadcast
    user = update.effective_user
    if user:
        try:
            db.track_user(user.id, user.username or "", user.first_name or "")
        except Exception as exc:
            logger.warning("Gagal track user %s: %s", user.id, exc)
    await update.message.reply_text(
        f"🏠Menu Utama - *{STORE_NAME}*\n\nPilih menu:",
        parse_mode="Markdown",
        reply_markup=kb_main_menu(),
    )


# ------------------------------------------------------------------
# Main menu
# ------------------------------------------------------------------


async def _show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()
    await _edit_or_reply_text(query, 
        f"🏠 *Menu Utama — {STORE_NAME}*\n\nPilih menu:",
        parse_mode="Markdown",
        reply_markup=kb_main_menu(),
    )


# ------------------------------------------------------------------
# Order / Beli — sub-menu produk
# ------------------------------------------------------------------


async def _show_order_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()
    await _edit_or_reply_text(query, 
        "🛒 *Pilih Produk*\n\n"
        "Klik produk untuk melihat detail dan melakukan pembelian:",
        parse_mode="Markdown",
        reply_markup=kb_order_menu(),
    )


async def _show_product_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: str, qty: int = 1
) -> None:
    query = update.callback_query
    assert query is not None
    product = db.get_product(product_id)
    if not product:
        await query.answer("Produk tidak ditemukan!", show_alert=True)
        return

    stock = db.get_stock_count(product_id)
    stock_text = f"✅ {stock} tersedia" if stock > 0 else "❌ Stok habis"
    is_ghs_do = product_id == "ghs_do"

    extra_info = ""
    if is_ghs_do:
        extra_info = (
            "\n\n⚡ *Cara Kerja Auto Klaim:*\n"
            "Setelah pembayaran dikonfirmasi, bot akan otomatis "
            "mengklaim DigitalOcean Credit *$200* ke akun DO kamu "
            "dalam waktu ±30–60 detik.\n"
            "_Bisa beli beberapa sekaligus — tiap unit diklaim ke akun DO "
            "terpisah, satu per satu._"
        )

    # Tampilkan total jika qty > 1 (semua produk, termasuk ghs_do bulk)
    total_line = ""
    if qty > 1:
        promo = db.get_promo()
        effective_price = (
            promo["promo_price"]
            if promo["min_qty"] > 0
            and promo["promo_price"] > 0
            and qty >= promo["min_qty"]
            else product["price"]
        )
        total = effective_price * qty
        promo_tag = " 🎁" if effective_price < product["price"] else ""
        total_line = f"\n💵 *Total ({qty}x):* {fmt_price(total)}{promo_tag}"

    # Banner promo volume discount (semua produk)
    promo_line = ""
    promo = db.get_promo()
    if promo["min_qty"] > 0 and promo["promo_price"] > 0:
        promo_line = (
            f"\n\n🎁 *PROMO:* Beli ≥ *{promo['min_qty']}* akun "
            f"→ harga *{fmt_price(promo['promo_price'])}*/akun!"
        )

    text = (
        f"{product['emoji']} *{product['name']}*\n\n"
        f"{product['description']}"
        f"{extra_info}"
        f"{promo_line}\n\n"
        f"💰 *Harga Satuan:* {fmt_price(product['price'])}\n"
        f"📦 *Stok:* {stock_text}"
        f"{total_line}"
    )
    await _edit_or_reply_text(query, 
        text,
        parse_mode="Markdown",
        reply_markup=kb_product_detail_qty(product_id, qty, stock),
    )


async def _handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler untuk tombol yang dinonaktifkan (disabled buttons)."""
    query = update.callback_query
    assert query is not None
    await query.answer()


async def _handle_qty_up(
    update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str
) -> None:
    """Tambah kuantitas: payload = '{product_id}_{current_qty}'"""
    query = update.callback_query
    assert query is not None

    parts = payload.rsplit("_", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer("Format tidak valid!", show_alert=True)
        return

    product_id, qty_str = parts
    current_qty = int(qty_str)
    stock = db.get_stock_count(product_id)
    max_qty = min(stock, 10)
    new_qty = min(current_qty + 1, max_qty)

    if new_qty == current_qty:
        await query.answer(f"Maksimum {max_qty} item per order!", show_alert=True)
        return

    await query.answer()
    await _show_product_detail(update, context, product_id, new_qty)


async def _handle_qty_down(
    update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str
) -> None:
    """Kurangi kuantitas: payload = '{product_id}_{current_qty}'"""
    query = update.callback_query
    assert query is not None

    parts = payload.rsplit("_", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer("Format tidak valid!", show_alert=True)
        return

    product_id, qty_str = parts
    current_qty = int(qty_str)
    new_qty = max(current_qty - 1, 1)

    if new_qty == current_qty:
        await query.answer("Minimal 1 item!", show_alert=True)
        return

    await query.answer()
    await _show_product_detail(update, context, product_id, new_qty)


# ------------------------------------------------------------------
# Buy flow — step 1: create order & show payment info
# ------------------------------------------------------------------


async def _handle_buy(
    update: Update, context: ContextTypes.DEFAULT_TYPE, product_id_qty: str
) -> None:
    query = update.callback_query
    assert query is not None
    user = update.effective_user
    assert user is not None

    # Catat user untuk fitur broadcast
    try:
        db.track_user(user.id, user.username or "", user.first_name or "")
    except Exception as exc:
        logger.warning("Gagal track user %s: %s", user.id, exc)

    # Parse product_id dan qty dari payload
    # Format: "{product_id}_{qty}" — e.g. "ghs_fresh_1", "ghs_bekas_do_3"
    # Backward compat: "{product_id}" → qty=1
    parts = product_id_qty.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        product_id = parts[0]
        qty = int(parts[1])
    else:
        product_id = product_id_qty
        qty = 1

    product = db.get_product(product_id)
    if not product:
        await query.answer("Produk tidak ditemukan!", show_alert=True)
        return

    stock = db.get_stock_count(product_id)
    if stock <= 0:
        await query.answer("Maaf, stok sudah habis!", show_alert=True)
        return

    if qty > stock:
        await query.answer(
            f"⚠️ Stok tidak cukup!\nTersedia: {stock} item.", show_alert=True
        )
        return

    try:
        order_id = db.create_order(
            user_id=user.id,
            username=user.username or user.first_name,
            product_id=product_id,
            quantity=qty,
        )
    except Exception as exc:
        logger.error("Error creating order: %s", exc)
        await query.answer("Terjadi kesalahan, silakan coba lagi!", show_alert=True)
        return

    # Hitung total dengan mempertimbangkan promo volume discount
    total_price = db.calc_promo_total(product["price"], qty)
    promo_active = total_price < product["price"] * qty
    if promo_active:
        # Simpan total promo ke order agar auto-confirm tahu harga yang dibayar
        db.update_order(order_id, total_price=total_price)

    # Pilih gateway berdasarkan pengaturan aktif (default: pakasir)
    active_gw = db.get_setting("active_gateway", "pakasir")

    if active_gw == "pakasir" and PAKASIR_ENABLED and _pakasir_client:
        try:
            await _handle_buy_pakasir(query, order_id, product, qty, total_price)
            return
        except Exception as exc:
            logger.warning("Pakasir gagal, fallback ke manual: %s", exc)

    elif active_gw == "ronzzpay" and RONZZPAY_ENABLED and _ronzzpay_client:
        try:
            await _handle_buy_ronzzpay(query, order_id, product, qty, total_price)
            return
        except Exception as exc:
            logger.warning("RonzzPay gagal, fallback ke manual: %s", exc)

    # Fallback: manual transfer
    await _handle_buy_manual(query, order_id, product, qty, total_price)


async def _handle_buy_ronzzpay(
    query, order_id: str, product: dict, qty: int = 1, total_price: int = 0
) -> None:
    """Buat transaksi RonzzPay dan kirim QRIS/link pembayaran ke user."""
    assert _ronzzpay_client is not None
    if not total_price:
        total_price = product["price"] * qty
    code = RONZZPAY_DEFAULT_METHOD
    desc = f"Order {order_id} - {product['name']} x{qty}"

    txn = _ronzzpay_client.create_transaction(
        code=code,
        amount=total_price,
        description=desc,
    )

    logger.info(
        "RonzzPay txn created: reff_id=%s code=%s amount=%s fee=%s qr_image=%s qr_string=%s pay_url=%s",
        txn.reff_id,
        txn.code,
        txn.amount,
        txn.fee,
        bool(txn.qr_image),
        bool(txn.qr_string),
        bool(txn.pay_url),
    )

    db.update_order(
        order_id,
        payment_method="ronzzpay",
        ronzzpay_reff_id=txn.reff_id,
        ronzzpay_code=txn.code,
        ronzzpay_amount=txn.amount,
        ronzzpay_fee=txn.fee,
        ronzzpay_expired_at=_order_expiry_iso(),
    )

    fee_line = f"💸 Biaya layanan: *{fmt_price(txn.fee)}*\n" if txn.fee else ""
    method_label = (txn.payment_name or txn.code or "Otomatis").upper()
    qty_line = f"🔢 Kuantitas: *{qty}x {product['name']}*\n" if qty > 1 else ""
    unit_price = total_price // qty if qty > 0 else total_price
    promo_tag = " 🎁" if unit_price < product["price"] else ""
    price_line = (
        f"💰 Harga Satuan: *{fmt_price(unit_price)}{promo_tag}*\n"
        if qty > 1
        else f"💰 Harga: *{fmt_price(unit_price)}{promo_tag}*\n"
    )
    expiry_str = _order_expiry_str()

    base_text = (
        f"📋 *Detail Pesanan*\n\n"
        f"🆔 Order ID: `{order_id}`\n"
        f"📦 Produk: *{product['name']}*\n"
        f"{qty_line}"
        f"{price_line}"
        f"{fee_line}"
        f"💵 Total Bayar: *{fmt_price(txn.amount)}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 *Pembayaran via {method_label}*\n"
        f"⏳ Bayar sebelum: `{expiry_str}` _(10 menit)_\n\n"
        f"⚡ *Produk dikirim otomatis setelah pembayaran terdeteksi*\n"
        f"_(maksimal {_POLL_INTERVAL_LABEL}, tanpa konfirmasi admin)_"
    )

    kb = kb_order_payment(order_id, is_auto=True)

    # ── Priority 1: qr_image URL (gambar QR siap tampil) ──────────────
    if txn.qr_image:
        # Hapus pesan sebelumnya (detail produk), lalu kirim satu pesan
        # yang menggabungkan gambar QRIS + detail transaksi + tombol aksi.
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_photo(
            photo=txn.qr_image,
            caption=base_text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    # ── Priority 2: pay_url (link halaman pembayaran) ─────────────────
    if txn.pay_url:
        text = base_text + f"\n\n🔗 [Bayar Sekarang]({txn.pay_url})"
        await _edit_or_reply_text(query, text, parse_mode="Markdown", reply_markup=kb)
        return

    # ── Priority 3: qr_string (raw QRIS, salin ke aplikasi) ──────────
    if txn.qr_string:
        text = (
            base_text
            + f"\n\n📋 *QRIS String* (salin & paste ke aplikasi pembayaran):\n"
            f"`{txn.qr_string}`"
        )
        await _edit_or_reply_text(query, text, parse_mode="Markdown", reply_markup=kb)
        return

    # ── Tidak ada format yang dikenali ────────────────────────────────
    raise Exception(
        f"RonzzPay: tidak ada qr_image/pay_url/qr_string dalam response. "
        f"Raw keys: {list(txn.raw.keys())}"
    )


async def _handle_buy_pakasir(
    query, order_id: str, product: dict, qty: int = 1, total_price: int = 0
) -> None:
    """Buat transaksi Pakasir dan tampilkan instruksi pembayaran ke user."""
    assert _pakasir_client is not None
    if not total_price:
        total_price = product["price"] * qty
    method = PAKASIR_DEFAULT_METHOD

    txn = _pakasir_client.create_transaction(
        method=method,
        order_id=order_id,
        amount=total_price,
    )

    logger.info(
        "Pakasir txn: order_id=%s method=%s total=%s fee=%s payment_number_len=%d",
        txn.order_id,
        txn.payment_method,
        txn.total_payment,
        txn.fee,
        len(txn.payment_number),
    )

    # Simpan detail transaksi ke order
    # pakasir_amount  = harga yang dikirim ke Pakasir (sudah termasuk promo)
    # pakasir_expired_at = batas 10 menit dari kita (bukan dari gateway)
    db.update_order(
        order_id,
        payment_method="pakasir",
        pakasir_method=txn.payment_method,
        pakasir_amount=total_price,
        pakasir_total_payment=txn.total_payment,
        pakasir_fee=txn.fee,
        pakasir_payment_number=txn.payment_number,
        pakasir_expired_at=_order_expiry_iso(),
    )

    fee_line = f"💸 Biaya layanan: *{fmt_price(txn.fee)}*\n" if txn.fee else ""
    qty_line = f"🔢 Kuantitas: *{qty}x {product['name']}*\n" if qty > 1 else ""
    unit_price = total_price // qty if qty > 0 else total_price
    promo_tag = " 🎁" if unit_price < product["price"] else ""
    price_line = (
        f"💰 Harga Satuan: *{fmt_price(unit_price)}{promo_tag}*\n"
        if qty > 1
        else f"💰 Harga: *{fmt_price(unit_price)}{promo_tag}*\n"
    )
    expiry_str = _order_expiry_str()

    base_text = (
        f"📋 *Detail Pesanan*\n\n"
        f"🆔 Order ID: `{order_id}`\n"
        f"📦 Produk: *{product['name']}*\n"
        f"{qty_line}"
        f"{price_line}"
        f"{fee_line}"
        f"💵 Total Bayar: *{fmt_price(txn.total_payment)}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 *Pembayaran via {txn.method_label}*\n"
        f"⏳ Bayar sebelum: `{expiry_str}` _(10 menit)_\n\n"
        f"⚡ *Produk dikirim otomatis setelah pembayaran terdeteksi*\n"
        f"_(maksimal {_POLL_INTERVAL_LABEL}, tanpa konfirmasi admin)_"
    )

    kb = kb_order_payment(order_id, is_auto=True)

    if txn.is_qris and txn.payment_number:
        # Buat keyboard dengan tombol URL Pakasir
        pay_url = _pakasir_client.get_pay_url(
            order_id=order_id,
            amount=total_price,
            qris_only=True,
        )
        existing_rows = [list(row) for row in kb.inline_keyboard]
        pay_row = [InlineKeyboardButton("🔗 Buka Halaman Bayar", url=pay_url)]
        new_kb = InlineKeyboardMarkup([pay_row] + existing_rows)

        # Caption PENDEK untuk foto — Telegram limit 1024 karakter
        # base_text terlalu panjang sehingga send_photo gagal & fallback ke teks
        fee_label = f" + fee {fmt_price(txn.fee)}" if txn.fee else ""
        qty_label = f" x{qty}" if qty > 1 else ""
        short_caption = (
            f"📋 *Detail Pesanan*\n\n"
            f"🆔 `{order_id}`\n"
            f"📦 *{product['name']}*{qty_label}\n"
            f"💵 Total: *{fmt_price(txn.total_payment)}*{fee_label}\n"
            f"⏳ Bayar sebelum: `{expiry_str}` _(10 menit)_\n\n"
            f"📲 *Scan QR* di atas atau klik *Buka Halaman Bayar*."
        )

        # Simpan chat_id sebelum hapus pesan (referensi tetap valid)
        msg = query.message
        if msg is None:
            # Tidak bisa kirim foto, tampilkan teks saja
            logger.warning("QRIS: query.message is None, fallback ke teks")
            await _edit_or_reply_text(query, 
                base_text + f"\n\n📲 *Scan QRIS* atau klik *Buka Halaman Bayar*.\n"
                f"`{txn.payment_number}`",
                parse_mode="Markdown",
                reply_markup=new_kb,
            )
            return

        # Buat URL QR code — Telegram fetch langsung dari qrserver.com
        qr_url = _build_qr_url(txn.payment_number)

        # Hapus pesan lama + kirim foto QR
        try:
            await msg.delete()
        except Exception:
            pass

        try:
            await msg.chat.send_photo(
                photo=qr_url,
                caption=short_caption,
                parse_mode="Markdown",
                reply_markup=new_kb,
            )
        except Exception as send_exc:
            logger.error(
                "QRIS send_photo gagal order=%s: %s", order_id, send_exc, exc_info=True
            )
            # Fallback: kirim teks ke chat yang sama
            try:
                await msg.chat.send_message(
                    text=base_text
                    + f"\n\n📲 *Scan QRIS* atau klik *Buka Halaman Bayar*.\n"
                    f"`{txn.payment_number}`",
                    parse_mode="Markdown",
                    reply_markup=new_kb,
                )
            except Exception:
                pass
        return

    if txn.is_va and txn.payment_number:
        # Tampilkan nomor Virtual Account
        text = (
            base_text + f"\n\n🏦 *Nomor {txn.method_label}:*\n"
            f"`{txn.payment_number}`\n\n"
            f"Transfer tepat sesuai nominal di atas."
        )
        await _edit_or_reply_text(query, text, parse_mode="Markdown", reply_markup=kb)
        return

    # Fallback: gunakan URL pembayaran Pakasir
    pay_url = _pakasir_client.get_pay_url(
        order_id=order_id, amount=total_price
    )
    text = base_text + f"\n\n🔗 [Bayar Sekarang — Pakasir]({pay_url})"
    await _edit_or_reply_text(query, text, parse_mode="Markdown", reply_markup=kb)


async def _handle_buy_manual(query, order_id: str, product: dict, qty: int = 1, total_price: int = 0) -> None:
    """Tampilkan info transfer bank manual."""
    if not total_price:
        total_price = product["price"] * qty
    db.update_order(order_id, payment_method="manual")
    pi = PAYMENT_INFO
    qris_line = f"🔲 *QRIS:* `{pi['qris']}`\n" if pi.get("qris") else ""
    qty_line = f"🔢 Kuantitas: *{qty}x {product['name']}*\n" if qty > 1 else ""
    unit_price = total_price // qty if qty > 0 else total_price
    promo_tag = " 🎁" if unit_price < product["price"] else ""
    price_line = (
        f"💰 Harga Satuan: *{fmt_price(unit_price)}{promo_tag}*\n"
        if qty > 1
        else f"💰 Harga: *{fmt_price(unit_price)}{promo_tag}*\n"
    )

    text = (
        f"📋 *Detail Pesanan*\n\n"
        f"🆔 Order ID: `{order_id}`\n"
        f"📦 Produk: *{product['name']}*\n"
        f"{qty_line}"
        f"{price_line}"
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 *Info Pembayaran (Transfer Manual)*\n"
        f"🏦 Bank/Dompet: *{pi['bank']}*\n"
        f"🔢 No. Rek: `{pi['account_number']}`\n"
        f"👤 A/N: *{pi['account_name']}*\n"
        f"{qris_line}"
        f"💵 Jumlah Transfer: *{fmt_price(total_price)}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Transfer *tepat* sesuai nominal agar mudah diverifikasi.\n\n"
        f"Setelah transfer, klik *Sudah Transfer* dan kirim screenshot bukti bayar."
    )
    await _edit_or_reply_text(query,
        text, parse_mode="Markdown", reply_markup=kb_order_payment(order_id)
    )


# ------------------------------------------------------------------
# Buy flow — step 2: user klik "Sudah Transfer" → ConversationHandler
# ------------------------------------------------------------------


async def entry_sudah_bayar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    assert query.data is not None
    order_id = query.data.removeprefix("sudah_bayar_")

    order = db.get_order(order_id)
    if not order:
        await query.answer("Pesanan tidak ditemukan!", show_alert=True)
        return ConversationHandler.END
    user = update.effective_user
    assert user is not None
    if order["user_id"] != user.id:
        await query.answer("Ini bukan pesananmu!", show_alert=True)
        return ConversationHandler.END
    if order["status"] != "pending_payment":
        await query.answer("Status pesanan sudah berubah!", show_alert=True)
        return ConversationHandler.END

    assert context.user_data is not None
    context.user_data["current_order_id"] = order_id

    await _edit_or_reply_text(query, 
        f"📸 *Kirim Bukti Pembayaran*\n\n"
        f"Order ID: `{order_id}`\n\n"
        f"Silakan kirim *screenshot* bukti transfermu sekarang.\n"
        f"Admin akan memverifikasi dalam waktu singkat ⚡\n\n"
        f"Ketik /cancel untuk membatalkan.",
        parse_mode="Markdown",
    )
    return WAITING_PAYMENT_PROOF


# ------------------------------------------------------------------
# Buy flow — step 3: terima foto bukti bayar
# ------------------------------------------------------------------


async def handle_payment_proof(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    from config import ADMIN_IDS

    assert context.user_data is not None
    assert update.message is not None
    order_id: str | None = context.user_data.get("current_order_id")
    if not order_id:
        await update.message.reply_text(
            "❌ Sesi sudah berakhir. Silakan mulai dari menu.",
            reply_markup=kb_back_main(),
        )
        return ConversationHandler.END

    order = db.get_order(order_id)
    if not order:
        await update.message.reply_text("❌ Pesanan tidak ditemukan!")
        return ConversationHandler.END

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text(
            "⚠️ Tolong kirim *gambar/screenshot* bukti transfer.",
            parse_mode="Markdown",
        )
        return WAITING_PAYMENT_PROOF

    db.update_order(order_id, status="payment_sent", payment_proof_file_id=file_id)

    await update.message.reply_text(
        f"✅ *Bukti pembayaran diterima!*\n\n"
        f"Order ID: `{order_id}`\n"
        f"Status: Menunggu konfirmasi admin 🔍\n\n"
        f"Kamu akan mendapat notifikasi setelah pembayaran dikonfirmasi.\n"
        f"Estimasi: *1–5 menit* ⚡",
        parse_mode="Markdown",
        reply_markup=kb_back_main(),
    )

    # Notif semua admin
    product = db.get_product(order["product_id"])
    user = update.effective_user
    assert user is not None

    # Escape username agar karakter seperti _ tidak merusak Markdown
    raw_username = user.username or user.first_name
    safe_username = _escape_md_user(raw_username)
    safe_product_name = _escape_md_user(product['name'] if product else order['product_id'])

    admin_caption = (
        f"🔔 *PEMBAYARAN MASUK!*\n\n"
        f"👤 User: @{safe_username} (ID: `{user.id}`)\n"
        f"🆔 Order ID: `{order_id}`\n"
        f"📦 Produk: *{safe_product_name}*\n"
        f"💰 Nominal: *{fmt_price(order['price'])}*\n\n"
        f"Konfirmasi atau tolak pembayaran di bawah."
    )
    admin_caption_plain = (
        f"🔔 PEMBAYARAN MASUK!\n\n"
        f"👤 User: @{raw_username} (ID: {user.id})\n"
        f"🆔 Order ID: {order_id}\n"
        f"📦 Produk: {product['name'] if product else order['product_id']}\n"
        f"💰 Nominal: {fmt_price(order['price'])}\n\n"
        f"Konfirmasi atau tolak pembayaran di bawah."
    )
    admin_kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Konfirmasi", callback_data=f"admin_confirm_{order_id}"
                ),
                InlineKeyboardButton(
                    "❌ Tolak", callback_data=f"admin_reject_{order_id}"
                ),
            ]
        ]
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=file_id,
                caption=admin_caption,
                parse_mode="Markdown",
                reply_markup=admin_kb,
            )
        except Exception as exc:
            logger.warning(
                "Gagal notif admin %s (Markdown photo): %s — fallback plain text",
                admin_id, exc,
            )
            # Fallback 1: kirim foto tanpa Markdown
            try:
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=admin_caption_plain,
                    reply_markup=admin_kb,
                )
            except Exception as exc2:
                logger.warning(
                    "Gagal notif admin %s (plain photo): %s — fallback teks saja",
                    admin_id, exc2,
                )
                # Fallback 2: kirim teks saja tanpa foto
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=admin_caption_plain + "\n\n📷 (Foto bukti bayar gagal terkirim)",
                        reply_markup=admin_kb,
                    )
                except Exception as exc3:
                    logger.error(
                        "Notif admin %s GAGAL TOTAL: %s", admin_id, exc3
                    )

    context.user_data.pop("current_order_id", None)
    return ConversationHandler.END


async def cancel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert context.user_data is not None
    assert update.message is not None
    context.user_data.pop("current_order_id", None)
    await update.message.reply_text(
        "❌ Proses pembayaran dibatalkan.", reply_markup=kb_back_main()
    )
    return ConversationHandler.END


# ------------------------------------------------------------------
# Cara Order
# ------------------------------------------------------------------


async def _show_cara_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    text = (
        f"📋 *Cara Order di {STORE_NAME}*\n\n"
        f"*1️⃣  Pilih Produk*\n"
        f"Klik *🛒 Order / Beli* → pilih produk. "
        f"Setiap tombol langsung menampilkan nama, harga, dan stok.\n\n"
        f"*2️⃣  Lihat Detail & Beli*\n"
        f"Klik tombol produk untuk melihat deskripsi lengkap, "
        f"lalu klik *Beli Sekarang*.\n\n"
        f"*3️⃣  Pembayaran*\n"
        f"✅ *QRIS Otomatis :*\n"
        f"Bot menampilkan QRIS — scan & bayar. "
        f"Konfirmasi *otomatis* dalam ±{_POLL_INTERVAL_LABEL} setelah pembayaran terdeteksi, "
        f"tanpa perlu konfirmasi admin.\n\n"
        f"*4️⃣  Akun Terkirim*\n"
        f"Setelah pembayaran terkonfirmasi, akun langsung dikirim ke chat ini.\n\n"
        f"*5️⃣  Khusus GHS Only DO ⚡ — Klaim Kredit $200*\n"
        f"Setelah pembayaran terkonfirmasi, bot meminta login DigitalOcean kamu "
        f"(email/password atau cookies). "
        f"Bot otomatis mengklaim DO Credit *$200* ke akun kamu dalam ±30–60 detik.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"❓ Ada pertanyaan? Hubungi admin."
    )

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒  Beli Sekarang", callback_data="menu_order")],
            [InlineKeyboardButton("🏠  Menu Utama", callback_data="main_menu")],
        ]
    )
    await _edit_or_reply_text(query, text, parse_mode="Markdown", reply_markup=kb)


# ------------------------------------------------------------------
# Profil
# ------------------------------------------------------------------


async def _show_profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    user = update.effective_user
    assert user is not None
    orders = db.get_user_orders(user.id)

    total = len(orders)
    selesai = sum(1 for o in orders if o["status"] == "confirmed")
    pending = sum(
        1 for o in orders if o["status"] in ("pending_payment", "payment_sent", "paid")
    )
    dibatalkan = sum(1 for o in orders if o["status"] in ("cancelled", "rejected"))

    username_line = f"@{user.username}" if user.username else "_(tidak ada username)_"

    text = (
        f"👤 *Profil Kamu*\n\n"
        f"*Nama:* {user.first_name}"
        + (f" {user.last_name}" if user.last_name else "")
        + f"\n"
        f"*Username:* {username_line}\n"
        f"*Telegram ID:* `{user.id}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *Ringkasan Pesanan*\n\n"
        f"Total Pesanan    : {total}\n"
        f"✅ Selesai       : {selesai}\n"
        f"⏳ Diproses      : {pending}\n"
        f"❌ Dibatalkan    : {dibatalkan}\n"
    )

    # Tampilkan 5 pesanan terakhir
    if orders:
        text += "\n━━━━━━━━━━━━━━━━━━━━\n📋 *5 Pesanan Terakhir*\n\n"
        for o in orders[:5]:
            emoji = get_status_emoji(o["status"])
            label = get_status_label(o["status"])
            date = o["created_at"][:10]
            text += (
                f"{emoji} `{o['id']}` — {_escape_md_user(o['product_name'])}\n"
                f"   {fmt_price(o['price'])}  •  {date}  •  _{label}_\n\n"
            )

    rows = []
    if orders:
        rows.append(
            [
                InlineKeyboardButton(
                    "📋  Semua Pesanan", callback_data="profil_all_orders"
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠  Menu Utama", callback_data="main_menu")])

    await _edit_or_reply_text(query, 
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def _show_all_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    user = update.effective_user
    assert user is not None
    orders = db.get_user_orders(user.id)
    if not orders:
        await _edit_or_reply_text(query, 
            "📋 *Pesanan Saya*\n\nKamu belum punya pesanan.",
            parse_mode="Markdown",
            reply_markup=kb_back_main(),
        )
        return

    rows = []
    for o in orders[:15]:
        emoji = get_status_emoji(o["status"])
        rows.append(
            [
                InlineKeyboardButton(
                    f"{emoji}  {o['id']}  •  {_escape_md_user(o['product_name'])}",
                    callback_data=f"order_detail_{o['id']}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("🔙  Kembali ke Profil", callback_data="menu_profil")]
    )

    await _edit_or_reply_text(query, 
        "📋 *Semua Pesanan*\n\nKlik pesanan untuk melihat detail:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _show_order_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str
) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    user = update.effective_user
    assert user is not None
    order = db.get_order(order_id)
    if not order or order["user_id"] != user.id:
        await query.answer("Pesanan tidak ditemukan!", show_alert=True)
        return

    emoji = get_status_emoji(order["status"])
    label = get_status_label(order["status"])

    qty = order.get("quantity", 1)
    total_price = order.get("total_price", order["price"])
    qty_line = f"\n🔢 Kuantitas  : *{qty}x*" if qty > 1 else ""
    price_label = (
        f"💰 Harga Satuan: {fmt_price(order['price'])}{qty_line}\n"
        f"💵 Total      : {fmt_price(total_price)}"
    )

    text = (
        f"📦 *Detail Pesanan*\n\n"
        f"🆔 Order ID : `{order_id}`\n"
        f"📦 Produk   : *{_escape_md_user(order['product_name'])}*\n"
        f"{price_label}\n"
        f"📅 Tanggal  : {order['created_at'][:10]}\n"
        f"Status      : {emoji} *{label}*"
    )

    if order["status"] == "confirmed":
        if order["product_id"] == "ghs_do":
            # Untuk ghs_do, account_delivered berisi ringkasan status klaim DO per akun
            total = order.get("quantity", 1) or 1
            done = order.get("do_claim_index", 0) or 0
            claim_status = order.get("account_delivered")
            if claim_status and claim_status != "[TERKIRIM ✓]":
                text += f"\n\n{claim_status}"
                if done < total:
                    text += (
                        f"\n\n⏳ *{done}/{total} klaim selesai.* "
                        "Gunakan tombol klaim dari bot untuk melanjutkan akun berikutnya."
                    )
            else:
                text += (
                    "\n\n⏳ *Klaim DO Credit belum dilakukan.*\n"
                    "Gunakan tombol yang dikirim bot setelah pembayaran dikonfirmasi."
                )
        else:
            acct = order.get("account_delivered", "")
            if acct == "[TERKIRIM ✓]":
                text += (
                    "\n\n✅ *Akun sudah dikirim.*\n"
                    "_Cek pesan dari bot di chat ini untuk melihat akunmu._"
                )
            elif acct:
                # Data lama — tampilkan sebagaimana adanya
                accs = acct.split("\n")
                if len(accs) == 1:
                    text += (
                        f"\n\n✅ *Akun yang dikirim:*\n"
                        f"`{accs[0]}`\n\nSimpan baik-baik ya! 🙏"
                    )
                else:
                    acc_lines = "\n".join(
                        [f"{i + 1}. `{a}`" for i, a in enumerate(accs)]
                    )
                    text += (
                        f"\n\n✅ *{len(accs)} Akun yang dikirim:*\n"
                        f"{acc_lines}\n\nSimpan akun-akun di atas dengan aman! 🙏"
                    )

    await _edit_or_reply_text(query, 
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙  Kembali ke Pesanan", callback_data="profil_all_orders"
                    )
                ],
                [InlineKeyboardButton("🏠  Menu Utama", callback_data="main_menu")],
            ]
        ),
    )


# ------------------------------------------------------------------
# Cancel order
# ------------------------------------------------------------------


async def _handle_cancel_order(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str
) -> None:
    query = update.callback_query
    assert query is not None

    user = update.effective_user
    assert user is not None
    order = db.get_order(order_id)
    if not order or order["user_id"] != user.id:
        await query.answer("Pesanan tidak ditemukan!", show_alert=True)
        return
    if order["status"] != "pending_payment":
        await query.answer("Pesanan tidak bisa dibatalkan lagi!", show_alert=True)
        return

    db.update_order(order_id, status="cancelled")
    await _edit_or_reply_text(query, 
        f"❌ *Pesanan Dibatalkan*\n\nOrder `{order_id}` telah dibatalkan.",
        parse_mode="Markdown",
        reply_markup=kb_back_main(),
    )


# ------------------------------------------------------------------
# Cek status pembayaran RonzzPay (tombol Cek Status Bayar)
# ------------------------------------------------------------------


async def _handle_check_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str
) -> None:
    query = update.callback_query
    assert query is not None
    user = update.effective_user
    assert user is not None
    order = db.get_order(order_id)
    if not order:
        await query.answer("Pesanan tidak ditemukan!", show_alert=True)
        return
    if order["user_id"] != user.id:
        await query.answer("Ini bukan pesananmu!", show_alert=True)
        return

    # Jika sudah confirmed sebelumnya
    if order["status"] == "confirmed":
        await query.answer(
            "✅ Pesanan sudah dikonfirmasi dan akun sudah terkirim!", show_alert=True
        )
        return

    # ── Cek status RonzzPay ──────────────────────────────────────────────
    if order.get("payment_method") == "ronzzpay":
        reff_id = order.get("ronzzpay_reff_id")
        rz_client = _ronzzpay_client
        if not reff_id or not rz_client:
            await query.answer(
                "💳 Pembayaran manual — tunggu konfirmasi admin.", show_alert=True
            )
            return

        await query.answer("🔍 Memeriksa status pembayaran...", show_alert=False)

        try:
            status = rz_client.check_transaction_status(reff_id)

            if status.status == "success" and order["status"] == "pending_payment":
                from handlers.admin import auto_confirm_order

                await auto_confirm_order(order_id, context.bot)
                try:
                    await _edit_or_reply_text(query, 
                        "✅ *Pembayaran dikonfirmasi.*",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

            elif status.status == "success" and order["status"] != "pending_payment":
                await query.answer("✅ Pembayaran sudah dikonfirmasi!", show_alert=True)

            elif status.status == "pending":
                await query.answer(
                    "⏳ Belum ada pembayaran terdeteksi.\n"
                    "Bot akan otomatis konfirmasi begitu pembayaran masuk.",
                    show_alert=True,
                )

            elif status.status == "expired":
                db.update_order(order_id, status="cancelled")
                await _edit_or_reply_text(query, 
                    f"⏰ *Pembayaran Expired*\n\n"
                    f"Order ID: `{order_id}`\n\n"
                    f"Waktu pembayaran telah habis. Silakan buat pesanan baru.",
                    parse_mode="Markdown",
                    reply_markup=kb_back_main(),
                )

            else:
                await query.answer(f"Status: {status.status}", show_alert=True)

        except Exception as exc:
            logger.error("Check payment (RonzzPay) error: %s", exc)
            await query.answer(
                "⚠️ Gagal cek status. Bot tetap memeriksa secara otomatis.",
                show_alert=True,
            )
        return

    # ── Cek status Pakasir ───────────────────────────────────────────────
    if order.get("payment_method") == "pakasir":
        # Gunakan pakasir_amount (harga asli) bukan pakasir_total_payment
        # karena Pakasir API transactiondetail menggunakan amount yang sama
        # seperti yang dikirim saat create transaction.
        pak_amount = order.get("pakasir_amount") or order.get("pakasir_total_payment")
        pak_client = _pakasir_client
        if not pak_amount or not pak_client:
            await query.answer("💳 Tidak bisa cek status saat ini.", show_alert=True)
            return

        await query.answer("🔍 Memeriksa status pembayaran...", show_alert=False)

        try:
            pak_status = pak_client.check_transaction_status(
                order_id=order_id, amount=pak_amount
            )

            if (
                pak_status.status == "completed"
                and order["status"] == "pending_payment"
            ):
                from handlers.admin import auto_confirm_order

                db.update_order(order_id, pakasir_paid_at=pak_status.completed_at)
                await auto_confirm_order(order_id, context.bot)
                try:
                    await _edit_or_reply_text(query, 
                        "✅ *Pembayaran dikonfirmasi.*",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

            elif (
                pak_status.status == "completed"
                and order["status"] != "pending_payment"
            ):
                await query.answer("✅ Pembayaran sudah dikonfirmasi!", show_alert=True)

            elif pak_status.status == "pending":
                await query.answer(
                    "⏳ Belum ada pembayaran terdeteksi.\n"
                    "Bot akan otomatis konfirmasi begitu pembayaran masuk.",
                    show_alert=True,
                )

            else:
                await query.answer(f"Status: {pak_status.status}", show_alert=True)

        except Exception as exc:
            logger.error("Check payment (Pakasir) error: %s", exc)
            await query.answer(
                "⚠️ Gagal cek status. Bot tetap memeriksa secara otomatis.",
                show_alert=True,
            )
        return

    # ── Pembayaran manual ────────────────────────────────────────────────
    await query.answer(
        "💳 Pembayaran manual — tunggu konfirmasi admin.", show_alert=True
    )


# ------------------------------------------------------------------
# Master callback dispatcher (Router Pattern)
# ------------------------------------------------------------------


async def _handle_no_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer("Maaf, stok sedang habis!", show_alert=True)


async def _handle_do_claim_expired(
    update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str
) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer(
        "Sesi klaim sudah berakhir. Hubungi admin jika butuh bantuan.",
        show_alert=True,
    )


# Router untuk data yang persis sama (Exact Match)
EXACT_ROUTES = {
    "main_menu": _show_main_menu,
    "menu_order": _show_order_menu,
    "menu_cara_order": _show_cara_order,
    "menu_profil": _show_profil,
    "profil_all_orders": _show_all_orders,
    "no_stock": _handle_no_stock,
    "noop": _handle_noop,
}

# Router untuk data yang memiliki awalan/prefix (Prefix Match)
PREFIX_ROUTES = {
    "order_detail_": _show_order_detail,
    "product_": _show_product_detail,
    "buy_": _handle_buy,
    "qty_up_": _handle_qty_up,
    "qty_down_": _handle_qty_down,
    "cancel_order_": _handle_cancel_order,
    "check_pay_": _handle_check_payment,
    "do_claim_": _handle_do_claim_expired,
}


async def handle_user_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Master callback query handler using Router Pattern."""
    query = update.callback_query
    assert query is not None
    assert query.data is not None
    data: str = query.data

    # 1. Cek rute Exact Match
    if data in EXACT_ROUTES:
        handler = EXACT_ROUTES[data]
        await handler(update, context)
        return

    # 2. Cek rute Prefix Match
    for prefix, handler in PREFIX_ROUTES.items():
        if data.startswith(prefix):
            payload = data.removeprefix(prefix)
            await handler(update, context, payload)
            return

    # 3. Fallback jika tidak ada rute yang cocok
    await query.answer()
