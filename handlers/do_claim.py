"""
do_claim.py — Conversation handler untuk alur klaim kredit DigitalOcean $200
via GitHub Education (GitHub Student Developer Pack).

Dipanggil setelah admin mengkonfirmasi pesanan 'ghs_do'.
Admin mengirim tombol "Klaim DO Credit" ke user → handler ini aktif.

=== ALUR ===
  entry_do_claim      — Tampilkan info + pilihan metode login DO
  handle_choose_method:
    • "do_method_email"   → minta email DO → state DO_WAITING_EMAIL
    • "do_method_cookies" → minta cookies DO → state DO_WAITING_COOKIES
    • "do_skip"           → tampilkan panduan klaim manual → END
  handle_do_email     — Terima email, minta password → state DO_WAITING_PASS
  handle_do_password  — Terima password, jalankan automation email+pass
  handle_do_cookies   — Terima cookies JSON, jalankan automation via cookies

=== YANG DIOTOMASI (alur OAuth asli) ===
  1. Login GitHub sebagai akun GHS (+2FA TOTP).
  2. Buka education.github.com/pack, skip survei onboarding bila muncul.
  3. Klik offer DigitalOcean (Get access by connecting your GitHub account).
  4. DigitalOcean minta login → isi kredensial buyer (email+pass) + 2FA DO.
  5. "Authenticate with GitHub" → "Authorize digitalocean".
  6. Verifikasi "Happy Coding!" / "GitHub Student Pack Applied" = sukses.
  Setiap proses memakai browser + context baru (cookie bersih).

States:
    DO_WAITING_EMAIL  (201) — menunggu input email DO buyer
    DO_WAITING_PASS   (202) — menunggu input password DO buyer
    DO_WAITING_TOTP   (204) — menunggu TOTP secret 2FA DO (atau 'skip')
    DO_WAITING_COOKIES(203) — menunggu input cookies DO dari user

Format akun GHS di products.json (field 'account_delivered'):
    "email@gmail.com:Password123"              — tanpa 2FA
    "email@gmail.com:Password123:TOTP_SECRET"  — dengan TOTP 2FA
"""

import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from database import db

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# States — di-export ke main.py
# ------------------------------------------------------------------

DO_WAITING_EMAIL = 201  # Menunggu user mengetik email DO
DO_WAITING_PASS = 202  # Menunggu user mengetik password DO
DO_WAITING_COOKIES = 203  # Menunggu user mengirim cookies DO dalam format JSON
DO_WAITING_TOTP = 204  # Menunggu user mengetik TOTP secret 2FA DO (opsional)
DO_CHOOSE_METHOD = 205  # Menunggu user memilih metode login DO (email/cookies)
DO_WAITING_GHS = 206  # Menunggu user (buyer) mengetik akun GHS miliknya


# ------------------------------------------------------------------
# Helpers — multi-akun (bulk ghs_do)
# ------------------------------------------------------------------


def _get_ghs_accounts(order: dict) -> list[str]:
    """Ambil daftar akun GHS pada sebuah order ghs_do.

    `ghs_account_used` bisa berisi satu akun atau beberapa akun yang
    dipisah baris baru (untuk pembelian bulk).
    """
    raw = order.get("ghs_account_used") or ""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def build_ghs_source_keyboard(order_id: str) -> InlineKeyboardMarkup:
    """Keyboard pilihan SUMBER akun GHS: dari stok seller atau input buyer."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🏪 GHS dari Seller",
                    callback_data=f"do_src_seller_{order_id}",
                ),
                InlineKeyboardButton(
                    "👤 GHS dari Buyer",
                    callback_data=f"do_src_buyer_{order_id}",
                ),
            ]
        ]
    )


def build_do_buyer_start_keyboard(order_id: str) -> InlineKeyboardMarkup:
    """Keyboard untuk produk ghs_do_buyer: satu tombol mulai klaim.

    Produk ini SELALU memakai GHS milik buyer, jadi tidak perlu memilih sumber.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚀 Mulai Klaim DO",
                    callback_data=f"do_src_buyer_{order_id}",
                ),
            ]
        ]
    )


def build_do_method_keyboard() -> InlineKeyboardMarkup:
    """Keyboard pilihan metode login DigitalOcean (email/cookies)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📧 Email & Password", callback_data="do_method_email"
                ),
                InlineKeyboardButton("🍪 Cookies", callback_data="do_method_cookies"),
            ]
        ]
    )


async def send_do_claim_prompt(bot, chat_id: int, order_id: str) -> None:
    """Kirim pesan ajakan klaim DO untuk akun berikutnya yang belum diklaim."""
    order = db.get_order(order_id)
    if not order:
        return

    total = order.get("quantity", 1) or 1
    index = order.get("do_claim_index", 0) or 0

    if index >= total:
        return

    progress = f" *({index + 1} dari {total})*" if total > 1 else ""
    bulk_note = (
        f"\n\n📦 Pesanan ini berisi *{total}* klaim DO. "
        f"Kamu akan diminta login untuk setiap akun satu per satu."
        if total > 1
        else ""
    )

    is_buyer_product = order["product_id"] == "ghs_do_buyer"

    try:
        if is_buyer_product:
            # ghs_do_buyer: SELALU pakai GHS buyer → satu tombol mulai klaim.
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ *Pembayaran Diterima*\n\n"
                    f"*{order['product_name']}*  |  `{order_id}`\n\n"
                    f"Klaim DO Credit{progress}.\n"
                    f"Kredit $200 hanya berlaku untuk akun DigitalOcean yang belum pernah "
                    f"mendapat kredit sebelumnya dan sudah memiliki metode pembayaran terdaftar."
                    f"{bulk_note}\n\n"
                    f"👤 Produk ini menggunakan *GHS (GitHub Education) milikmu sendiri*.\n"
                    f"Tekan tombol di bawah untuk mulai."
                ),
                parse_mode="Markdown",
                reply_markup=build_do_buyer_start_keyboard(order_id),
            )
        else:
            # ghs_do: tampilkan pilihan seller / buyer
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ *Pembayaran Diterima*\n\n"
                    f"*{order['product_name']}*  |  `{order_id}`\n\n"
                    f"Klaim DO Credit{progress}.\n"
                    f"Kredit $200 hanya berlaku untuk akun DigitalOcean yang belum pernah "
                    f"mendapat kredit sebelumnya dan sudah memiliki metode pembayaran terdaftar."
                    f"{bulk_note}\n\n"
                    f"Pilih *sumber akun GHS* yang akan dipakai untuk klaim:\n"
                    f"• 🏪 *GHS dari Seller* — pakai akun stok kami\n"
                    f"• 👤 *GHS dari Buyer* — pakai akun GitHub Education milikmu sendiri"
                ),
                parse_mode="Markdown",
                reply_markup=build_ghs_source_keyboard(order_id),
            )
    except Exception as exc:
        logger.error("send_do_claim_prompt gagal untuk order %s: %s", order_id, exc)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _parse_ghs(account: str) -> tuple[str, str, str]:
    """
    Parse string akun GHS menjadi (email, password, totp_secret).

    Format yang didukung:
        email:password
        email:password:totp_secret
    """
    parts = account.split(":", 2)
    email = parts[0].strip() if len(parts) > 0 else ""
    passwd = parts[1].strip() if len(parts) > 1 else ""
    totp = parts[2].strip() if len(parts) > 2 else ""
    return email, passwd, totp


def _parse_do_account(line: str) -> tuple[str, str, str]:
    """
    Parse satu baris akun buyer DigitalOcean menjadi (email, password, totp_secret).

    Format yang didukung (pemisah ':' atau '|', spasi di sekitar pemisah diabaikan):
        email:password
        email:password:totp_secret
        email | password
        email | password | totp_secret

    TOTP secret bersifat opsional. Kembalikan ("", "", "") bila tidak valid.
    """
    raw = (line or "").strip()
    if not raw:
        return "", "", ""

    # Tentukan pemisah: gunakan '|' bila ada, selain itu ':'
    if "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
    else:
        parts = [p.strip() for p in raw.split(":")]

    email = parts[0] if len(parts) > 0 else ""
    passwd = parts[1] if len(parts) > 1 else ""
    # Sisa bagian (bila ada) digabung kembali sebagai TOTP secret.
    # TOTP base32 tidak mengandung ':' atau '|', jadi aman.
    totp = parts[2].replace(" ", "") if len(parts) > 2 else ""
    return email, passwd, totp


def _parse_do_accounts_block(text: str) -> list[tuple[str, str, str]]:
    """
    Parse beberapa baris akun buyer DO (satu akun per baris) untuk pembelian bulk.

    Kembalikan list of (email, password, totp_secret) untuk baris yang valid
    (minimal punya email + password).
    """
    result = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        email, passwd, totp = _parse_do_account(line)
        if email and passwd:
            result.append((email, passwd, totp))
    return result


def _consume_seller_ghs(order_id: str, ghs_email: str) -> None:
    """Hapus satu akun GHS seller dari order setelah berhasil dipakai.

    Pencocokan dilakukan berdasarkan email (robust terhadap perbedaan format
    pemisah ':' / '|'). Dengan menghapus akun yang sudah terpakai, percobaan
    klaim berikutnya tidak akan memakai ulang akun yang sudah ditebus, dan
    akun yang gagal tetap berada di antrian untuk dicoba lagi.
    """
    if not order_id or not ghs_email:
        return
    order = db.get_order(order_id)
    if not order:
        return
    lines = [
        line for line in (order.get("ghs_account_used") or "").splitlines() if line.strip()
    ]
    target = ghs_email.strip().lower()
    for i, line in enumerate(lines):
        email, _, _ = _parse_ghs(line)
        if email.strip().lower() == target:
            del lines[i]
            db.update_order(order_id, ghs_account_used="\n".join(lines))
            return


def _clear_do_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hapus semua data sensitif klaim DO dari user_data."""
    for key in (
        "do_ghs_email",
        "do_ghs_password",
        "do_ghs_totp",
        "do_ghs_source",
        "do_claim_order_id",
        "do_claim_total",
        "do_method",
        "do_email",
        "do_password",
        "do_totp",
        "do_cookies",
        "do_accounts_queue",
        "do_ghs_queue",
        "do_suppress_next_prompt",
    ):
        context.user_data.pop(key, None)


def _is_valid_email(email: str) -> bool:
    """Validasi format email sederhana menggunakan regex."""
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()))


# ------------------------------------------------------------------
# Entry point — pilih SUMBER akun GHS (seller / buyer)
# ------------------------------------------------------------------


async def entry_do_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry ConversationHandler.
    callback_data: do_src_seller_{order_id} atau do_src_buyer_{order_id}
    """
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("do_src_seller_"):
        source = "seller"
        order_id = data.removeprefix("do_src_seller_")
    else:
        source = "buyer"
        order_id = data.removeprefix("do_src_buyer_")

    order = db.get_order(order_id)
    if not order or order["user_id"] != update.effective_user.id:
        await query.answer("Pesanan tidak valid!", show_alert=True)
        return ConversationHandler.END
    if order["product_id"] not in ("ghs_do", "ghs_do_buyer"):
        await query.answer("Produk tidak mendukung fitur ini.", show_alert=True)
        return ConversationHandler.END

    total = order.get("quantity", 1) or 1
    done = order.get("do_claim_index", 0) or 0
    remaining = max(total - done, 1)
    context.user_data["do_claim_order_id"] = order_id
    context.user_data["do_claim_total"] = total
    context.user_data["do_ghs_source"] = source

    # Hapus keyboard sumber agar tidak diklik ganda
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Untuk produk ghs_do_buyer, sumber GHS HARUS dari buyer
    if order["product_id"] == "ghs_do_buyer":
        source = "buyer"
        context.user_data["do_ghs_source"] = "buyer"

    if source == "seller":
        # Ambil akun GHS dari stok seller (yang sudah disimpan di order).
        accounts = _get_ghs_accounts(order)
        if not accounts:
            await query.answer("Akun GHS belum tersedia. Hubungi admin.", show_alert=True)
            return ConversationHandler.END

        # Bangun antrian GHS seller dari DEPAN daftar yang tersisa (dibatasi sisa
        # klaim). Akun GHS yang sudah berhasil ditebus telah dihapus dari order
        # (lihat _consume_seller_ghs), jadi front-of-list selalu akun yang belum
        # terpakai. Tiap GHS dipasangkan 1:1 dengan akun DO.
        ghs_queue: list[tuple[str, str, str]] = []
        for raw_account in accounts[:remaining]:
            ghs_email, ghs_pass, ghs_totp = _parse_ghs(raw_account)
            if ghs_email and ghs_pass:
                ghs_queue.append((ghs_email, ghs_pass, ghs_totp))

        if not ghs_queue:
            await query.answer("Format akun GHS tidak valid. Hubungi admin.", show_alert=True)
            return ConversationHandler.END

        context.user_data["do_ghs_queue"] = ghs_queue
        # Set GHS pertama untuk jalur cookies (satu klaim per pesan).
        context.user_data["do_ghs_email"] = ghs_queue[0][0]
        context.user_data["do_ghs_password"] = ghs_queue[0][1]
        context.user_data["do_ghs_totp"] = ghs_queue[0][2]

        await update.effective_chat.send_message(
            "🏪 *GHS dari Seller dipilih.*\n\n"
            "Sekarang pilih cara login ke akun *DigitalOcean* tujuan:",
            parse_mode="Markdown",
            reply_markup=build_do_method_keyboard(),
        )
        return DO_CHOOSE_METHOD

    # source == "buyer" → minta akun GHS milik buyer (bisa banyak untuk bulk).
    if remaining > 1:
        prompt = (
            f"👤 *GHS dari Buyer dipilih.* (sisa *{remaining}* klaim)\n\n"
            f"Kirim *{remaining} akun GitHub Education* milikmu, *satu akun per baris*.\n"
            f"Tiap akun GHS akan dipakai untuk *satu* klaim DO.\n\n"
            f"Format tiap baris (pilih salah satu):\n"
            f"`email:password`\n"
            f"`email:password:TOTP_SECRET`\n"
            f"`email | password | TOTP_SECRET`\n\n"
            f"_TOTP secret hanya jika akun GitHub-mu pakai 2FA._\n"
            f"⚠️ Pesan akan dihapus otomatis demi keamanan.\n\n"
            f"Ketik /cancel untuk membatalkan."
        )
    else:
        prompt = (
            "👤 *GHS dari Buyer dipilih.*\n\n"
            "Kirim akun *GitHub Education* milikmu yang akan dipakai klaim.\n\n"
            "Format (pilih salah satu):\n"
            "`email:password`\n"
            "`email:password:TOTP_SECRET`\n"
            "`email | password | TOTP_SECRET`\n\n"
            "_TOTP secret hanya jika akun GitHub-mu pakai 2FA._\n"
            "⚠️ Pesan akan dihapus otomatis demi keamanan.\n\n"
            "Ketik /cancel untuk membatalkan."
        )
    await update.effective_chat.send_message(prompt, parse_mode="Markdown")
    return DO_WAITING_GHS


# ------------------------------------------------------------------
# State DO_WAITING_GHS — buyer mengetik akun GHS miliknya
# ------------------------------------------------------------------


async def handle_do_ghs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Terima akun GHS milik buyer (satu atau beberapa untuk bulk),
    lalu lanjut ke pemilihan metode login DO."""
    msg = update.message
    raw = (msg.text or "").strip()

    # Hapus pesan demi keamanan (berisi kredensial GitHub)
    try:
        await msg.delete()
    except Exception:
        pass

    # Parse semua baris akun GHS (mendukung ':' atau '|', satu akun per baris).
    ghs_accounts = _parse_do_accounts_block(raw)
    if not ghs_accounts:
        await update.effective_chat.send_message(
            "❌ Format akun GHS tidak valid.\n\n"
            "Gunakan: `email:password` atau `email:password:TOTP_SECRET` "
            "(satu akun per baris).\n"
            "Coba kirim ulang, atau ketik /cancel untuk membatalkan.",
            parse_mode="Markdown",
        )
        return DO_WAITING_GHS

    # Validasi format email pada tiap akun.
    invalid = [e for e, _, _ in ghs_accounts if not _is_valid_email(e)]
    if invalid:
        await update.effective_chat.send_message(
            "❌ Ada email GHS yang formatnya tidak valid:\n"
            + "\n".join(f"• `{e}`" for e in invalid[:5])
            + "\n\nPerbaiki dan kirim ulang, atau ketik /cancel.",
            parse_mode="Markdown",
        )
        return DO_WAITING_GHS

    # Batasi jumlah GHS sesuai sisa klaim pada order.
    order_id = context.user_data.get("do_claim_order_id", "")
    total = context.user_data.get("do_claim_total", 1) or 1
    order = db.get_order(order_id) if order_id else None
    done = (order.get("do_claim_index", 0) if order else 0) or 0
    remaining = max(total - done, 1)

    if len(ghs_accounts) > remaining:
        ghs_accounts = ghs_accounts[:remaining]
        await update.effective_chat.send_message(
            f"ℹ️ Kamu mengirim lebih banyak akun GHS dari yang dibutuhkan. "
            f"Hanya *{remaining}* akun pertama yang akan dipakai.",
            parse_mode="Markdown",
        )

    context.user_data["do_ghs_queue"] = ghs_accounts
    # Set GHS pertama untuk jalur cookies (satu klaim per pesan).
    context.user_data["do_ghs_email"] = ghs_accounts[0][0]
    context.user_data["do_ghs_password"] = ghs_accounts[0][1]
    context.user_data["do_ghs_totp"] = ghs_accounts[0][2]

    count_note = (
        f" ({len(ghs_accounts)} akun)" if len(ghs_accounts) > 1 else ""
    )
    await update.effective_chat.send_message(
        f"✅ Akun GHS diterima{count_note}.\n\n"
        "Sekarang pilih cara login ke akun *DigitalOcean* tujuan:",
        parse_mode="Markdown",
        reply_markup=build_do_method_keyboard(),
    )
    return DO_CHOOSE_METHOD


# ------------------------------------------------------------------
# State DO_CHOOSE_METHOD — pilih metode login DO (email/cookies)
# ------------------------------------------------------------------


async def handle_choose_do_method(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Setelah akun GHS siap, user memilih metode login DigitalOcean."""
    query = update.callback_query
    await query.answer()

    method = "email" if query.data == "do_method_email" else "cookies"
    context.user_data["do_method"] = method

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    total = context.user_data.get("do_claim_total", 1) or 1
    # Jumlah akun DO yang diminta = jumlah GHS yang sudah disiapkan (1:1).
    ghs_queue = context.user_data.get("do_ghs_queue", [])
    count = len(ghs_queue) if ghs_queue else total

    if method == "email":
        if count > 1:
            prompt = (
                f"📝 *Input Akun DigitalOcean (Bulk x{count})*\n\n"
                f"Kirim *{count} akun* DigitalOcean tujuan, *satu akun per baris*.\n"
                f"Tiap akun DO dipasangkan dengan satu akun GHS secara berurutan.\n\n"
                f"Format tiap baris:\n"
                f"`email:password`\n"
                f"`email:password:TOTP_SECRET`\n"
                f"`email | password | TOTP_SECRET`\n\n"
                f"_TOTP secret hanya jika akun pakai 2FA._\n"
                f"Ketik /cancel untuk membatalkan."
            )
        else:
            prompt = (
                f"📝 *Input Akun DigitalOcean*\n\n"
                f"Kirim akun DigitalOcean tujuan dalam *satu baris*.\n\n"
                f"Format:\n"
                f"`email:password`\n"
                f"`email:password:TOTP_SECRET`\n"
                f"`email | password | TOTP_SECRET`\n\n"
                f"_TOTP secret hanya jika akun pakai 2FA._\n"
                f"⚠️ Pesan akan dihapus otomatis demi keamanan.\n"
                f"Ketik /cancel untuk membatalkan."
            )
        await update.effective_chat.send_message(prompt, parse_mode="Markdown")
        return DO_WAITING_EMAIL
    else:
        await update.effective_chat.send_message(
            "Export cookies dari browser:\n\n"
            "1. Install ekstensi Cookie-Editor di Chrome atau Firefox\n"
            "2. Login ke cloud.digitalocean.com\n"
            "3. Klik ikon Cookie-Editor → Export → Export as JSON\n"
            "4. Salin semua teks JSON dan kirim di sini\n\n"
            "Ketik /cancel untuk membatalkan.",
            disable_web_page_preview=True,
        )
        return DO_WAITING_COOKIES


# ------------------------------------------------------------------
# State DO_WAITING_EMAIL
# ------------------------------------------------------------------


async def handle_do_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handler untuk state DO_WAITING_EMAIL.

    Menerima akun DigitalOcean dalam format gabungan satu baris
    (email:password[:totp] atau dipisah '|'). Mendukung banyak akun
    (satu per baris) untuk pembelian bulk.
    """
    msg = update.message
    raw_text = (msg.text or "").strip()

    # Hapus pesan demi keamanan (berisi password)
    try:
        await msg.delete()
    except Exception:
        pass

    accounts = _parse_do_accounts_block(raw_text)
    if not accounts:
        await update.effective_chat.send_message(
            "❌ Format akun tidak valid.\n\n"
            "Gunakan salah satu format berikut (satu akun per baris):\n"
            "`email:password`\n"
            "`email:password:TOTP_SECRET`\n"
            "`email | password | TOTP_SECRET`\n\n"
            "Coba kirim ulang, atau ketik /cancel untuk membatalkan.",
            parse_mode="Markdown",
        )
        return DO_WAITING_EMAIL

    # Validasi email pada tiap akun
    invalid = [e for e, _, _ in accounts if not _is_valid_email(e)]
    if invalid:
        await update.effective_chat.send_message(
            "❌ Ada email yang formatnya tidak valid:\n"
            + "\n".join(f"• `{e}`" for e in invalid[:5])
            + "\n\nPerbaiki dan kirim ulang, atau ketik /cancel untuk membatalkan.",
            parse_mode="Markdown",
        )
        return DO_WAITING_EMAIL

    # Berapa akun yang masih dibutuhkan (sisa klaim pada order ini)
    order_id = context.user_data.get("do_claim_order_id", "")
    total = context.user_data.get("do_claim_total", 1) or 1
    order = db.get_order(order_id) if order_id else None
    done = (order.get("do_claim_index", 0) if order else 0) or 0
    remaining = max(total - done, 1)

    # Antrian GHS yang sudah disiapkan (seller dari stok / buyer dari input).
    ghs_queue = list(context.user_data.get("do_ghs_queue", []))
    if not ghs_queue:
        # Fallback (kompatibilitas): pakai GHS tunggal yang tersimpan.
        single = (
            context.user_data.get("do_ghs_email", ""),
            context.user_data.get("do_ghs_password", ""),
            context.user_data.get("do_ghs_totp", ""),
        )
        if single[0] and single[1]:
            ghs_queue = [single]

    if not ghs_queue:
        await update.effective_chat.send_message(
            "❌ Akun GHS tidak tersedia. Mulai ulang klaim dari tombol, atau /cancel.",
        )
        return ConversationHandler.END

    # Batas akun yang diproses = min(sisa klaim, jumlah DO dikirim, jumlah GHS).
    limit = min(remaining, len(accounts), len(ghs_queue))

    if len(accounts) > limit:
        await update.effective_chat.send_message(
            f"ℹ️ Kamu mengirim lebih banyak akun dari yang bisa diproses. "
            f"Hanya *{limit}* akun pertama yang akan diproses.",
            parse_mode="Markdown",
        )

    do_accounts = accounts[:limit]
    ghs_accounts = ghs_queue[:limit]

    # Simpan antrian akun yang akan diproses berurutan (DO + GHS dipasangkan 1:1).
    context.user_data["do_accounts_queue"] = do_accounts
    context.user_data["do_ghs_queue"] = ghs_accounts

    # Proses semua akun di antrian satu per satu
    await _process_email_queue(update, context)

    _clear_do_data(context)
    return ConversationHandler.END


async def _process_email_queue(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Jalankan automation untuk setiap akun DO di antrian (mendukung bulk).

    Selama antrian diproses, prompt tombol klaim berikutnya ditahan agar tidak
    spam. Setelah antrian habis, baru tampilkan prompt lanjutan / pesan selesai.
    """
    queue = context.user_data.get("do_accounts_queue", [])
    ghs_queue = context.user_data.get("do_ghs_queue", [])
    order_id = context.user_data.get("do_claim_order_id", "")
    total = context.user_data.get("do_claim_total", 1) or 1
    chat = update.effective_chat

    # Tahan prompt per-iterasi
    context.user_data["do_suppress_next_prompt"] = True
    # Pasangkan tiap akun DO dengan satu akun GHS (1:1). Setiap akun GHS hanya
    # bisa menebus penawaran DigitalOcean satu kali, jadi pemasangan ini wajib.
    for (do_email, do_password, do_totp), (ghs_email, ghs_password, ghs_totp) in zip(
        queue, ghs_queue
    ):
        context.user_data["do_email"] = do_email
        context.user_data["do_password"] = do_password
        context.user_data["do_totp"] = do_totp
        context.user_data["do_ghs_email"] = ghs_email
        context.user_data["do_ghs_password"] = ghs_password
        context.user_data["do_ghs_totp"] = ghs_totp
        await _run_automation(update, context)
    context.user_data["do_suppress_next_prompt"] = False

    # Setelah antrian habis, cek apakah masih ada sisa klaim
    if not order_id:
        return
    refreshed = db.get_order(order_id)
    if not refreshed:
        return
    done = refreshed.get("do_claim_index", 0) or 0
    if done < total:
        # Masih ada akun yang belum diklaim (mis. sebagian gagal / dikirim sebagian)
        await chat.send_message(
            f"ℹ️ *{done}/{total} klaim selesai.* "
            f"Kirim akun DigitalOcean berikutnya lewat tombol di bawah.",
            parse_mode="Markdown",
        )
        await send_do_claim_prompt(context.bot, chat.id, order_id)
    elif total > 1:
        await chat.send_message(
            f"🎉 *Semua {total} klaim DO Credit selesai!*\n\n"
            f"Terima kasih sudah berbelanja. 🙏",
            parse_mode="Markdown",
        )


# ------------------------------------------------------------------
# State DO_WAITING_PASS / DO_WAITING_TOTP (legacy — dipertahankan untuk
# kompatibilitas, namun alur email kini memakai input satu baris di
# DO_WAITING_EMAIL sehingga handler ini jarang terpakai)
# ------------------------------------------------------------------


async def handle_do_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Legacy: terima password DO terpisah lalu lanjut ke 2FA.

    Dipertahankan agar state lama tidak menyebabkan error bila masih aktif.
    """
    msg = update.message
    password = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass

    if len(password) < 6:
        await update.effective_chat.send_message(
            "❌ Password terlalu pendek (minimal 6 karakter).\n\n"
            "Kirim ulang password kamu, atau ketik /cancel untuk membatalkan.",
        )
        return DO_WAITING_PASS

    context.user_data["do_password"] = password
    await update.effective_chat.send_message(
        "🔐 *Autentikasi Dua Faktor DigitalOcean*\n\n"
        "Jika akun DigitalOcean kamu memakai 2FA, kirim *TOTP secret*-nya.\n"
        "Jika tidak memakai 2FA, ketik *skip*.\n\n"
        "Ketik /cancel untuk membatalkan.",
        parse_mode="Markdown",
    )
    return DO_WAITING_TOTP


async def handle_do_totp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Legacy: terima TOTP secret 2FA DO (atau 'skip'), lalu jalankan automation."""
    msg = update.message
    raw = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass

    if raw.lower() in ("skip", "lewati", "-", "no", "tidak"):
        context.user_data["do_totp"] = ""
    else:
        context.user_data["do_totp"] = raw.replace(" ", "")

    await _run_automation(update, context)
    _clear_do_data(context)
    return ConversationHandler.END


# ------------------------------------------------------------------
# State DO_WAITING_COOKIES
# ------------------------------------------------------------------


async def handle_do_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handler untuk state DO_WAITING_COOKIES.
    Menerima cookies JSON, hapus pesan (keamanan), validasi, lalu jalankan automation.
    """
    msg = update.message
    raw_text = (msg.text or "").strip()

    # Hapus pesan yang berisi cookies secepatnya demi keamanan
    try:
        await msg.delete()
    except Exception:
        pass  # Mungkin tidak punya izin menghapus pesan

    # Validasi: harus JSON yang valid
    try:
        cookies = json.loads(raw_text)
    except json.JSONDecodeError:
        await update.effective_chat.send_message(
            "❌ Format cookies tidak valid. Pastikan kamu mengirim JSON yang benar.\n\n"
            "Gunakan fitur *Export as JSON* di Cookie-Editor, "
            "lalu salin seluruh isinya dan kirim di sini.\n\n"
            "Coba kirim ulang, atau ketik /cancel untuk membatalkan.",
            parse_mode="Markdown",
        )
        return DO_WAITING_COOKIES

    # Validasi: harus berupa list (array JSON)
    if not isinstance(cookies, list):
        await update.effective_chat.send_message(
            "❌ Format cookies salah. Cookies harus berupa *array JSON* (diawali `[`).\n\n"
            "Pastikan kamu menggunakan fitur *Export as JSON* di Cookie-Editor.\n\n"
            "Coba kirim ulang, atau ketik /cancel untuk membatalkan.",
            parse_mode="Markdown",
        )
        return DO_WAITING_COOKIES

    # Validasi: tidak boleh kosong
    if len(cookies) == 0:
        await update.effective_chat.send_message(
            "❌ Cookies yang dikirim kosong (array tidak berisi item apa pun).\n\n"
            "Pastikan kamu sudah login ke DigitalOcean sebelum export cookies.\n\n"
            "Coba kirim ulang, atau ketik /cancel untuk membatalkan.",
        )
        return DO_WAITING_COOKIES

    # Simpan cookies ke user_data
    context.user_data["do_cookies"] = cookies

    # Jalankan automation
    await _run_automation(update, context)

    # Bersihkan semua data sensitif
    _clear_do_data(context)
    return ConversationHandler.END


# ------------------------------------------------------------------
# Internal: jalankan automation dan kirim hasil ke user
# ------------------------------------------------------------------


async def _run_automation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ambil credentials dari user_data, panggil fungsi automation yang sesuai
    (email atau cookies), lalu kirim hasilnya ke user.
    """
    chat = update.effective_chat
    method = context.user_data.get("do_method", "email")

    ghs_email = context.user_data.get("do_ghs_email", "")
    ghs_password = context.user_data.get("do_ghs_password", "")
    ghs_totp = context.user_data.get("do_ghs_totp", "")

    # Kirim pesan status awal
    status_msg = await chat.send_message(
        "⚙️ *Memproses Klaim DO Credit...*\n\n"
        "🔐 Login ke DigitalOcean...\n"
        "⏳ Mohon tunggu ±30–60 detik\n\n"
        "_Jangan tutup chat selama proses berlangsung._",
        parse_mode="Markdown",
    )

    try:
        if method == "cookies":
            do_cookies = context.user_data.get("do_cookies", [])
            from automation.do_claimer import claim_do_credit_with_cookies

            result = await claim_do_credit_with_cookies(
                do_cookies=do_cookies,
                ghs_email=ghs_email,
                ghs_password=ghs_password,
                ghs_totp_secret=ghs_totp,
            )
        else:
            # method == "email"
            do_email = context.user_data.get("do_email", "")
            do_password = context.user_data.get("do_password", "")
            do_totp = context.user_data.get("do_totp", "")
            from automation.do_claimer import claim_do_credit_with_email

            result = await claim_do_credit_with_email(
                do_email=do_email,
                do_password=do_password,
                ghs_email=ghs_email,
                ghs_password=ghs_password,
                ghs_totp_secret=ghs_totp,
                do_totp_secret=do_totp,
            )

    except Exception as exc:
        import traceback
        logger.exception("[DO Claim] Error tak terduga saat menjalankan automation")
        tb = traceback.format_exc()
        err_text = f"❌ *Error*\n\n```\n{tb[-3000:]}\n```"
        try:
            await status_msg.edit_text(err_text, parse_mode="Markdown")
        except Exception:
            await chat.send_message(err_text, parse_mode="Markdown")
        return

    # --- Jika berhasil: pindahkan akun GHS ke stok ghs_bekas_do ---
    # HANYA untuk GHS dari seller. GHS milik buyer tidak masuk stok kami.
    ghs_source = context.user_data.get("do_ghs_source", "seller")
    if result.success and ghs_source == "seller":
        # Susun kembali string akun GHS sesuai format stok
        if ghs_totp:
            ghs_account_str = f"{ghs_email}:{ghs_password}:{ghs_totp}"
        else:
            ghs_account_str = f"{ghs_email}:{ghs_password}"

        try:
            added = db.add_stock_accounts("ghs_bekas_do", [ghs_account_str])
            if added:
                logger.info(
                    "[DO Claim] Akun GHS '%s' dipindahkan ke stok ghs_bekas_do.",
                    ghs_email,
                )
            else:
                logger.warning(
                    "[DO Claim] Gagal memindahkan GHS '%s' ke ghs_bekas_do "
                    "(produk tidak ditemukan?).",
                    ghs_email,
                )
        except Exception as exc_stock:
            # Jangan sampai error stok membatalkan laporan sukses ke user
            logger.error("[DO Claim] Error saat pindah GHS ke bekas: %s", exc_stock)

        # Tandai akun GHS seller ini sudah terpakai pada order, supaya tidak
        # dipakai ulang oleh percobaan klaim berikutnya (mencegah index drift
        # saat sebagian klaim gagal di tengah batch bulk).
        try:
            _consume_seller_ghs(
                context.user_data.get("do_claim_order_id", ""), ghs_email
            )
        except Exception as exc_consume:
            logger.error(
                "[DO Claim] Error saat menandai GHS seller terpakai: %s", exc_consume
            )

    # --- Update pesan status dengan hasil automation ---
    order_id_claim = context.user_data.get("do_claim_order_id", "")
    total = context.user_data.get("do_claim_total", 1) or 1

    # Tentukan progress saat ini (index akun yang barusan diproses)
    order_now = db.get_order(order_id_claim) if order_id_claim else None
    current_index = (order_now.get("do_claim_index", 0) if order_now else 0) or 0
    progress = f" ({current_index + 1}/{total})" if total > 1 else ""

    if result.success:
        result_text = f"✅ *Klaim DO Credit Berhasil!*{progress}\n\n" + result.message
    else:
        result_text = f"❌ *Klaim DO Credit Gagal*{progress}\n\n" + result.message

    # --- Catat progress klaim & status ke order ---
    if order_id_claim and order_now is not None:
        # Akumulasi ringkasan hasil per akun di account_delivered
        prev_summary = order_now.get("account_delivered") or ""
        if prev_summary in ("[TERKIRIM ✓]", None):
            prev_summary = ""
        line = (
            f"✅ Akun {current_index + 1}: DO Credit $200 diklaim"
            if result.success
            else f"⚠️ Akun {current_index + 1}: klaim belum berhasil"
        )
        new_summary = (prev_summary + "\n" + line).strip() if prev_summary else line

        # Index hanya maju bila klaim sukses; jika gagal, user bisa coba ulang akun yang sama
        new_index = current_index + 1 if result.success else current_index
        db.update_order(
            order_id_claim,
            account_delivered=new_summary,
            do_claim_index=new_index,
        )

    try:
        await status_msg.edit_text(
            result_text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception:
        # Jika edit gagal (pesan terlalu panjang, dll), kirim pesan baru
        try:
            await chat.send_message(
                result_text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:
            # Fallback tanpa Markdown jika masih gagal (mungkin ada karakter khusus)
            await chat.send_message(result.message)

    # --- Kirim screenshot jika tersedia ---
    if result.screenshot:
        caption = (
            "📸 Screenshot billing DigitalOcean — kredit $200 aktif ✅"
            if result.success
            else "📸 Screenshot halaman saat proses berlangsung (informasi debugging)"
        )
        try:
            await chat.send_photo(photo=result.screenshot, caption=caption)
        except Exception as exc:
            logger.warning("[DO Claim] Tidak bisa mengirim screenshot: %s", exc)

    # --- Jika masih ada akun tersisa untuk diklaim (bulk), kirim prompt berikutnya ---
    # Saat memproses antrian email (bulk sekali kirim), jangan kirim prompt tombol
    # di tiap iterasi — biarkan _process_email_queue yang menanganinya di akhir.
    suppress_next_prompt = context.user_data.get("do_suppress_next_prompt", False)
    if order_id_claim and result.success and not suppress_next_prompt:
        refreshed = db.get_order(order_id_claim)
        if refreshed:
            next_index = refreshed.get("do_claim_index", 0) or 0
            if next_index < total:
                await send_do_claim_prompt(
                    context.bot, chat.id, order_id_claim
                )
            else:
                if total > 1:
                    await chat.send_message(
                        f"🎉 *Semua {total} klaim DO Credit selesai!*\n\n"
                        f"Terima kasih sudah berbelanja. 🙏",
                        parse_mode="Markdown",
                    )


# ------------------------------------------------------------------
# Cancel fallback — dipanggil saat user ketik /cancel
# ------------------------------------------------------------------


async def cancel_do_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handler /cancel — batalkan proses klaim DO dan bersihkan semua data."""
    _clear_do_data(context)
    await update.message.reply_text(
        "❌ Proses klaim DO Credit dibatalkan.\n\n"
        "Kamu masih bisa klaim DO Credit secara manual kapan saja "
        "menggunakan akun GHS yang sudah dikirimkan.\n\n"
        "Ketik /start untuk kembali ke menu utama."
    )
    return ConversationHandler.END
