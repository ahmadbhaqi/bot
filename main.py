import asyncio
import logging
import re
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import (
    BOT_TOKEN,
    PAKASIR_API_KEY,
    PAKASIR_DEFAULT_METHOD,
    PAKASIR_ENABLED,
    PAKASIR_PROJECT_SLUG,
    RONZZPAY_API_KEY,
    RONZZPAY_ENABLED,
    RONZZPAY_SANDBOX,
    WEBHOOK_HOST,
    WEBHOOK_PORT,
    WEBHOOK_PUBLIC_URL,
)
from database import db as db_module
from handlers.admin import (
    WAITING_BROADCAST_INPUT,
    WAITING_EDU_INPUT,
    WAITING_NEW_PRODUCT,
    WAITING_PRICE_INPUT,
    WAITING_PROMO_INPUT,
    WAITING_STOCK_INPUT,
    admin_command,
    auto_confirm_order,
    cancel_admin_action,
    entry_add_stock,
    entry_broadcast,
    entry_edu,
    entry_new_product,
    entry_promo,
    entry_set_price,
    handle_admin_callback,
    handle_broadcast_input,
    handle_edu_input,
    handle_new_product,
    handle_price_input,
    handle_promo_input,
    handle_stock_input,
)
from handlers.do_claim import (
    DO_CHOOSE_METHOD,
    DO_WAITING_COOKIES,
    DO_WAITING_EMAIL,
    DO_WAITING_GHS,
    DO_WAITING_PASS,
    DO_WAITING_TOTP,
    cancel_do_claim,
    entry_do_claim,
    handle_choose_do_method,
    handle_do_cookies,
    handle_do_email,
    handle_do_ghs,
    handle_do_password,
    handle_do_totp,
)
from handlers.user import (
    WAITING_PAYMENT_PROOF,
    cancel_payment,
    entry_sudah_bayar,
    handle_payment_proof,
    handle_user_callback,
    set_pakasir_client,
    set_ronzzpay_client,
    start_command,
)

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Webhook callbacks
# ------------------------------------------------------------------


async def on_ronzzpay_payment_success(reff_id: str, data: dict, bot) -> None:
    """Called by webhook server when RonzzPay sends transaction.success."""
    from database import db

    order = db.get_order_by_reff_id(reff_id)
    if not order:
        logger.warning("Webhook: no order found for reff_id=%s", reff_id)
        return

    logger.info(
        "Webhook: RonzzPay payment success for order %s (reff_id=%s, amount=%s)",
        order["id"],
        reff_id,
        data.get("amount"),
    )

    # Update payment timestamp
    db.update_order(
        order["id"],
        ronzzpay_paid_at=data.get("paid_at"),
    )

    # Auto-confirm and deliver account
    await auto_confirm_order(order["id"], bot)


async def on_pakasir_payment_success(order_id: str, data: dict, bot) -> None:
    """
    Called by webhook server when Pakasir sends status=completed.
    Pakasir mengirim order_id kita langsung di body webhook.
    """
    from database import db

    order = db.get_order(order_id)
    if not order:
        logger.warning("Pakasir Webhook: no order found for order_id=%s", order_id)
        return

    # Validasi amount jika tersedia (keamanan tambahan)
    # Webhook Pakasir mengirim amount = harga asli (bukan total+fee)
    webhook_amount = data.get("amount")
    stored_amount = order.get("pakasir_amount") or order.get("pakasir_total_payment")
    if webhook_amount and stored_amount and int(webhook_amount) != int(stored_amount):
        logger.warning(
            "Pakasir Webhook: amount mismatch order=%s webhook=%s stored=%s — proses tetap dilanjutkan",
            order_id,
            webhook_amount,
            stored_amount,
        )

    logger.info(
        "Pakasir Webhook: payment completed for order %s (amount=%s method=%s)",
        order_id,
        webhook_amount,
        data.get("payment_method"),
    )

    # Update timestamp pembayaran
    db.update_order(
        order_id,
        pakasir_paid_at=data.get("completed_at"),
    )

    # Auto-confirm dan kirim akun ke user
    await auto_confirm_order(order_id, bot)


# ------------------------------------------------------------------
# Diagnostics & error handling
# ------------------------------------------------------------------


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Simple health-check command to confirm the bot receives updates."""
    user = update.effective_user
    chat = update.effective_chat
    logger.info(
        "Received /ping from user_id=%s chat_id=%s",
        user.id if user else "-",
        chat.id if chat else "-",
    )

    if update.effective_message:
        await update.effective_message.reply_text(
            "pong ✅ Bot aktif dan menerima update."
        )


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every incoming update so silent polling issues are visible."""
    user = update.effective_user
    chat = update.effective_chat
    logger.info(
        "Update received: update_id=%s user_id=%s chat_id=%s",
        update.update_id,
        user.id if user else "-",
        chat.id if chat else "-",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — tampilkan traceback lengkap ke user untuk debugging."""
    import traceback

    logger.exception(
        "Unhandled exception while processing update=%s", update, exc_info=context.error
    )

    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"⚠️ *Error*\n\n```\n{tb[-3000:]}\n```",
                parse_mode="Markdown",
            )
        except Exception:
            # Fallback tanpa Markdown bila teks mengandung karakter khusus
            try:
                await update.effective_message.reply_text(
                    f"⚠️ Error:\n\n{tb[-3000:]}"
                )
            except Exception:
                logger.exception("Failed to send error notification to user")


# ------------------------------------------------------------------
# Background payment polling loop (auto-confirm tanpa admin)
# ------------------------------------------------------------------

# ── Konfigurasi polling ────────────────────────────────────────────
_POLL_INTERVAL = 5        # sweep utama setiap 5 detik
_FAST_POLL_INTERVAL = 3   # fast-poll per order: cek setiap 3 detik
_FAST_POLL_DURATION = 600 # fast-poll aktif selama 10 menit sejak order dibuat
_MAX_POLL_DURATION = 600  # batas maksimum polling = 10 menit

# Registry fast-poll tasks: order_id → asyncio.Task
_fast_poll_tasks: dict[str, asyncio.Task] = {}


# ── Helper: check satu order RonzzPay ─────────────────────────────


def _is_order_expired(order: dict) -> bool:
    """True bila order sudah melewati batas waktu pembayaran.

    Batas waktu = 10 menit sejak order dibuat (_MAX_POLL_DURATION).
    Field expiry dari gateway (pakasir_expired_at / ronzzpay_expired_at) dipakai
    sebagai batas awal bila lebih pendek dari 10 menit; jika lebih panjang,
    tetap dipotong ke 10 menit agar tidak ada order yang menggantung terlalu lama.
    """
    from datetime import timezone

    now_utc = datetime.now(timezone.utc)

    # Hitung batas 10 menit dari created_at (batas keras kita)
    created = _parse_dt_utc(str(order.get("created_at", "")))
    hard_deadline = (
        created + timedelta(seconds=_MAX_POLL_DURATION) if created else None
    )

    # Cek batas gateway (ambil yang lebih awal antara gateway dan batas keras kita)
    raw_exp = order.get("pakasir_expired_at") or order.get("ronzzpay_expired_at")
    if raw_exp:
        gw_exp = _parse_dt_utc(str(raw_exp))
        if gw_exp is not None:
            # Pakai yang lebih awal: gateway expiry atau batas keras 10 menit
            effective = min(gw_exp, hard_deadline) if hard_deadline else gw_exp
            return now_utc > effective + timedelta(seconds=60)

    # Fallback: hanya pakai batas keras
    if hard_deadline is not None:
        return now_utc > hard_deadline

    return False


def _parse_dt_utc(value: str):
    """Parse string ISO datetime ke datetime UTC-aware. Return None jika gagal.

    Menangani:
      - Suffix 'Z' (UTC)
      - Presisi nanodetik (dipotong ke mikrodetik agar fromisoformat bisa parse)
      - Datetime naive (diasumsikan UTC)
    """
    from datetime import timezone

    if not value:
        return None

    v = value.strip()
    # Ganti 'Z' dengan offset +00:00 agar dikenali fromisoformat
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"

    # Potong fraksi detik berlebih (nanodetik → mikrodetik, maks 6 digit)
    m = re.match(r"(.*\.\d{6})\d*(.*)$", v)
    if m:
        v = m.group(1) + m.group(2)

    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _check_one_ronzzpay(order: dict, bot, ronzzpay_client) -> None:
    """Cek status satu order RonzzPay. Confirm/cancel sesuai hasil."""
    from handlers.admin import auto_confirm_order
    from payment.ronzzpay import RonzzPayError

    order_id = order["id"]
    reff_id = order.get("ronzzpay_reff_id")
    if not reff_id:
        return

    try:
        status = await asyncio.to_thread(
            ronzzpay_client.check_transaction_status, reff_id
        )
        if status.status == "success":
            logger.info("💚 RonzzPay: sukses — order %s", order_id)
            db_module.update_order(
                order_id, ronzzpay_paid_at=datetime.now().isoformat()
            )
            await auto_confirm_order(order_id, bot)

        elif status.status == "expired":
            logger.info("⏰ RonzzPay: expired — order %s", order_id)
            db_module.update_order(order_id, status="cancelled")
            try:
                await bot.send_message(
                    chat_id=order["user_id"],
                    text=(
                        f"⏰ *Waktu Pembayaran Habis*\n\n"
                        f"Order ID: `{order_id}`\n\n"
                        f"Pembayaran sudah kedaluwarsa.\n"
                        f"Silakan buat pesanan baru dari menu."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    except RonzzPayError as exc:
        logger.warning("RonzzPay API error order %s: %s", order_id, exc)
    except Exception as exc:
        logger.error("RonzzPay check error order %s: %s", order_id, exc, exc_info=True)


# ── Helper: check satu order Pakasir ──────────────────────────────


async def _check_one_pakasir(order: dict, bot, pakasir_client) -> None:
    """Cek status satu order Pakasir. Confirm jika completed, expire jika kedaluwarsa."""
    from handlers.admin import auto_confirm_order
    from payment.pakasir import PakasirError

    order_id = order["id"]
    pak_amount = order.get("pakasir_amount") or order.get("pakasir_total_payment")
    if not pak_amount:
        return

    # ── Tandai expired bila sudah lewat batas waktu pembayaran ──────────
    # Tanpa ini, order pending yang tidak dibayar akan terus di-poll selamanya
    # dan memicu rate limit (HTTP 429) dari Pakasir walau tidak ada aktivitas.
    if _is_order_expired(order):
        logger.info("⏰ Pakasir: expired (timeout) — order %s", order_id)
        db_module.update_order(order_id, status="cancelled")
        try:
            await bot.send_message(
                chat_id=order["user_id"],
                text=(
                    f"⏰ *Waktu Pembayaran Habis*\n\n"
                    f"Order ID: `{order_id}`\n\n"
                    f"Pembayaran sudah kedaluwarsa.\n"
                    f"Silakan buat pesanan baru dari menu."
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    try:
        pak_status = await asyncio.to_thread(
            pakasir_client.check_transaction_status, order_id, pak_amount
        )
        if pak_status.status == "completed":
            logger.info("💚 Pakasir: completed — order %s", order_id)
            db_module.update_order(
                order_id,
                pakasir_paid_at=pak_status.completed_at or datetime.now().isoformat(),
            )
            await auto_confirm_order(order_id, bot)

    except PakasirError as exc:
        logger.warning("Pakasir API error order %s: %s", order_id, exc)
    except Exception as exc:
        logger.error("Pakasir check error order %s: %s", order_id, exc, exc_info=True)


# ── Fast-poll: per-order task yang dimulai saat transaksi dibuat ────────


async def _fast_poll_order(order_id: str, bot, ronzzpay_client, pakasir_client) -> None:
    """
    Fast-poll satu order segera setelah transaksi dibuat.
    Cek setiap _FAST_POLL_INTERVAL detik selama _FAST_POLL_DURATION detik,
    lalu berhenti (main loop mengambil alih).
    Ini memberi respons ~3 detik setelah user selesai bayar.
    """
    elapsed = 0
    try:
        while elapsed < _FAST_POLL_DURATION:
            await asyncio.sleep(_FAST_POLL_INTERVAL)
            elapsed += _FAST_POLL_INTERVAL

            # Cek apakah order masih pending (mungkin sudah dikonfirm oleh sweep utama)
            order = db_module.get_order(order_id)
            if not order or order["status"] not in ("pending_payment", "paid"):
                logger.debug("Fast-poll %s: order sudah selesai, stop.", order_id)
                return

            method = order.get("payment_method")
            if method == "ronzzpay" and ronzzpay_client:
                await _check_one_ronzzpay(order, bot, ronzzpay_client)
            elif method == "pakasir" and pakasir_client:
                await _check_one_pakasir(order, bot, pakasir_client)
            else:
                return  # manual order, tidak perlu di-poll

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Fast-poll error order %s: %s", order_id, exc, exc_info=True)
    finally:
        _fast_poll_tasks.pop(order_id, None)


def start_fast_poll(order_id: str, bot, ronzzpay_client, pakasir_client) -> None:
    """
    Mulai fast-poll task untuk order yang baru dibuat.
    Dipanggil dari payment_polling_loop saat mendeteksi order baru,
    atau bisa dipanggil langsung setelah create_order.
    """
    if order_id in _fast_poll_tasks:
        return
    task = asyncio.create_task(
        _fast_poll_order(order_id, bot, ronzzpay_client, pakasir_client),
        name=f"fast-poll-{order_id}",
    )
    _fast_poll_tasks[order_id] = task
    logger.debug("Fast-poll dimulai: order %s", order_id)


# ── Main sweep loop ─────────────────────────────────────────────────


async def payment_polling_loop(bot, ronzzpay_client, pakasir_client) -> None:
    """
    Sweep utama: setiap _POLL_INTERVAL detik, cek SEMUA pending orders secara paralel.
    Juga mendeteksi order baru dan menjalankan fast-poll task per-order (3 detik interval).

    Arsitektur dual-layer:
      ┌─ fast_poll_task (per order, 3s) ────→ konfirmasi dalam ~3 detik setelah bayar
      └─ sweep loop (5s, semua order paralel) → fallback & cleanup
    """
    gateway_list = []
    if ronzzpay_client:
        gateway_list.append("RonzzPay")
    if pakasir_client:
        gateway_list.append("Pakasir")

    logger.info(
        "🔄 Payment polling started | sweep=%ds fast=%ds | gateway: %s",
        _POLL_INTERVAL,
        _FAST_POLL_INTERVAL,
        ", ".join(gateway_list) if gateway_list else "none",
    )

    # Set order_id yang sudah punya fast-poll task
    known_orders: set[str] = set()

    while True:
        try:
            await asyncio.sleep(_POLL_INTERVAL)

            # Kumpulkan semua coroutine check untuk di-gather sekaligus
            check_coros = []

            # ── RonzzPay ───────────────────────────────────────────────
            if ronzzpay_client:
                rz_orders = db_module.get_pending_ronzzpay_orders()
                for order in rz_orders:
                    oid = order["id"]
                    check_coros.append(_check_one_ronzzpay(order, bot, ronzzpay_client))
                    # Jalankan fast-poll untuk order baru
                    if oid not in known_orders:
                        known_orders.add(oid)
                        start_fast_poll(oid, bot, ronzzpay_client, pakasir_client)

            # ── Pakasir ────────────────────────────────────────────────
            if pakasir_client:
                pk_orders = db_module.get_pending_pakasir_orders()
                for order in pk_orders:
                    oid = order["id"]
                    check_coros.append(_check_one_pakasir(order, bot, pakasir_client))
                    if oid not in known_orders:
                        known_orders.add(oid)
                        start_fast_poll(oid, bot, ronzzpay_client, pakasir_client)

            # ── Jalankan semua cek secara paralel ────────────────────────
            if check_coros:
                logger.debug(
                    "🔄 Sweep: cek %d order secara paralel...", len(check_coros)
                )
                results = await asyncio.gather(*check_coros, return_exceptions=True)
                for i, res in enumerate(results):
                    if isinstance(res, Exception):
                        logger.error("Sweep error coroutine[%d]: %s", i, res)

            # Bersihkan known_orders dari order yang sudah selesai
            all_pending_ids = {
                o["id"] for o in db_module.get_pending_ronzzpay_orders()
            } | {o["id"] for o in db_module.get_pending_pakasir_orders()}
            known_orders &= all_pending_ids

        except asyncio.CancelledError:
            logger.info("🛑 Payment polling loop dihentikan.")
            # Cancel semua fast-poll tasks
            for task in list(_fast_poll_tasks.values()):
                task.cancel()
            raise
        except Exception as exc:
            logger.error(
                "Payment polling loop crash: %s — melanjutkan...", exc, exc_info=True
            )


# ------------------------------------------------------------------
# Application builder
# ------------------------------------------------------------------


def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_error_handler(error_handler)
    app.add_handler(TypeHandler(Update, log_update), group=-1)

    # ------------------------------------------------------------------
    # ConversationHandler — user payment proof
    # Flow: user clicks "Sudah Transfer" → sends screenshot
    # ------------------------------------------------------------------
    payment_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_sudah_bayar, pattern=r"^sudah_bayar_"),
        ],
        states={
            WAITING_PAYMENT_PROOF: [
                MessageHandler(
                    filters.PHOTO | filters.Document.ALL,
                    handle_payment_proof,
                ),
                CommandHandler("cancel", cancel_payment),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_payment)],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # ------------------------------------------------------------------
    # ConversationHandler — admin add stock
    # Flow: admin clicks "Tambah Stok" → sends account lines
    # ------------------------------------------------------------------
    add_stock_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_add_stock, pattern=r"^admin_add_stock_"),
        ],
        states={
            WAITING_STOCK_INPUT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_stock_input,
                ),
                CommandHandler("cancel", cancel_admin_action),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # ------------------------------------------------------------------
    # ConversationHandler — admin set price
    # Flow: admin clicks "Ubah Harga" → sends new price integer
    # ------------------------------------------------------------------
    set_price_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_set_price, pattern=r"^admin_set_price_"),
        ],
        states={
            WAITING_PRICE_INPUT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_price_input,
                ),
                CommandHandler("cancel", cancel_admin_action),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # ------------------------------------------------------------------
    # ConversationHandler — promo per-produk (beli X → harga Y)
    # Flow: admin klik "Terapkan Promo" di produk → min_qty → harga
    # ------------------------------------------------------------------
    promo_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_promo, pattern=r"^admin_promo_set_"),
        ],
        states={
            WAITING_PROMO_INPUT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_promo_input,
                ),
                CommandHandler("cancel", cancel_admin_action),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # ------------------------------------------------------------------
    # ConversationHandler — tambah produk baru
    # Flow: admin klik "Tambah Produk" → kirim "id | nama | emoji | harga"
    # ------------------------------------------------------------------
    new_product_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_new_product, pattern=r"^admin_prod_new$"),
        ],
        states={
            WAITING_NEW_PRODUCT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_new_product,
                ),
                CommandHandler("cancel", cancel_admin_action),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # ------------------------------------------------------------------
    # ConversationHandler — admin broadcast ke semua user
    # Flow: admin klik "Broadcast" → kirim isi pesan → kirim ke semua user
    # ------------------------------------------------------------------
    broadcast_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_broadcast, pattern=r"^admin_broadcast$"),
        ],
        states={
            WAITING_BROADCAST_INPUT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_broadcast_input,
                ),
                CommandHandler("cancel", cancel_admin_action),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # ------------------------------------------------------------------
    # ConversationHandler — admin apply GitHub Edu
    # Flow: admin klik "Apply GitHub Edu" → kirim daftar akun → proses
    # ------------------------------------------------------------------
    edu_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_edu, pattern=r"^admin_edu$"),
        ],
        states={
            WAITING_EDU_INPUT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_edu_input,
                ),
                CommandHandler("cancel", cancel_admin_action),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin_action)],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # ------------------------------------------------------------------
    # ConversationHandler — DO credit auto-claim (GHS Only DO)
    # Flow: user klik "Klaim DO Credit"
    #       → pilih metode login DO (email/cookies/skip)
    #       → bot login ke DO pembeli + GitHub GHS → apply promo code
    #       → kirim screenshot bukti kredit $200 aktif ke user
    # ------------------------------------------------------------------
    do_claim_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                entry_do_claim,
                pattern=r"^do_src_seller_|^do_src_buyer_",
            ),
        ],
        states={
            DO_WAITING_GHS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_do_ghs),
                CommandHandler("cancel", cancel_do_claim),
            ],
            DO_CHOOSE_METHOD: [
                CallbackQueryHandler(
                    handle_choose_do_method,
                    pattern=r"^do_method_email$|^do_method_cookies$",
                ),
                CommandHandler("cancel", cancel_do_claim),
            ],
            DO_WAITING_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_do_email),
                CommandHandler("cancel", cancel_do_claim),
            ],
            DO_WAITING_PASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_do_password),
                CommandHandler("cancel", cancel_do_claim),
            ],
            DO_WAITING_TOTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_do_totp),
                CommandHandler("cancel", cancel_do_claim),
            ],
            DO_WAITING_COOKIES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_do_cookies),
                CommandHandler("cancel", cancel_do_claim),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_do_claim)],
        per_user=True,
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # ------------------------------------------------------------------
    # Register handlers — ORDER MATTERS:
    #   1. Commands
    #   2. ConversationHandlers (intercept before generic callbacks)
    #   3. Admin callbacks  (pattern-matched, before catch-all)
    #   4. User callbacks   (catch-all)
    # ------------------------------------------------------------------

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("admin", admin_command))

    # Conversations (must sit above generic CallbackQueryHandlers)
    app.add_handler(payment_conv)
    app.add_handler(add_stock_conv)
    app.add_handler(set_price_conv)
    app.add_handler(promo_conv)
    app.add_handler(new_product_conv)
    app.add_handler(broadcast_conv)
    app.add_handler(edu_conv)
    app.add_handler(do_claim_conv)

    # Admin callbacks  (pattern: admin_*)
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^admin_"))

    # User callbacks  (catch-all — must be last)
    app.add_handler(CallbackQueryHandler(handle_user_callback))

    return app


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------


async def main_async() -> None:
    """Async main: starts bot + webhook server + background payment polling."""
    logger.info("Starting bot...")
    app = build_application()

    ronzzpay_client = None
    pakasir_client = None
    webhook_runner = None
    polling_task = None

    # ── Inisialisasi RonzzPay ────────────────────────────────────
    if RONZZPAY_ENABLED:
        from payment.ronzzpay import RonzzPayClient

        ronzzpay_client = RonzzPayClient(
            api_key=RONZZPAY_API_KEY, sandbox=RONZZPAY_SANDBOX
        )
        set_ronzzpay_client(ronzzpay_client)
        env = "SANDBOX" if RONZZPAY_SANDBOX else "PRODUCTION"
        logger.info("💸 RonzzPay enabled [%s]", env)
    else:
        logger.info("💸 RonzzPay disabled")

    # ── Inisialisasi Pakasir ────────────────────────────────────
    if PAKASIR_ENABLED:
        from payment.pakasir import PakasirClient

        pakasir_client = PakasirClient(
            api_key=PAKASIR_API_KEY,
            project_slug=PAKASIR_PROJECT_SLUG,
        )
        set_pakasir_client(pakasir_client)
        logger.info(
            "💳 Pakasir enabled | project=%s | default_method=%s",
            PAKASIR_PROJECT_SLUG,
            PAKASIR_DEFAULT_METHOD,
        )
    else:
        logger.info(
            "💳 Pakasir disabled — set PAKASIR_PROJECT_SLUG di .env untuk mengaktifkan"
        )

    # ── Mulai webhook server jika ada gateway yang aktif ─────────────────
    if RONZZPAY_ENABLED or PAKASIR_ENABLED:
        from webhook.server import (
            set_bot_app,
            set_pakasir_payment_callback,
            set_payment_callback,
            start_webhook_server,
        )

        set_bot_app(app)
        set_payment_callback(on_ronzzpay_payment_success)
        set_pakasir_payment_callback(on_pakasir_payment_success)

        try:
            webhook_runner = await start_webhook_server(
                host=WEBHOOK_HOST, port=WEBHOOK_PORT
            )
            logger.info(
                "🌐 Webhook server aktif di http://%s:%d", WEBHOOK_HOST, WEBHOOK_PORT
            )
            logger.info("🌐 Endpoints: /webhook/ronzzpay  /webhook/pakasir  /health")

            # Daftarkan webhook URL ke RonzzPay jika dikonfigurasi
            if WEBHOOK_PUBLIC_URL and RONZZPAY_ENABLED and ronzzpay_client:
                full_url = f"{WEBHOOK_PUBLIC_URL.rstrip('/')}/webhook/ronzzpay"
                ok = await asyncio.to_thread(ronzzpay_client.set_webhook_url, full_url)
                if ok:
                    logger.info("🔗 Webhook RonzzPay terdaftar: %s", full_url)
                else:
                    logger.warning("⚠️ Gagal mendaftarkan webhook URL ke RonzzPay.")

            # Info URL webhook Pakasir (didaftarkan manual di dashboard)
            if WEBHOOK_PUBLIC_URL and PAKASIR_ENABLED:
                pakasir_url = f"{WEBHOOK_PUBLIC_URL.rstrip('/')}/webhook/pakasir"
                logger.info(
                    "💳 Pakasir webhook URL: %s"
                    " — daftarkan di dashboard app.pakasir.com",
                    pakasir_url,
                )
            elif not WEBHOOK_PUBLIC_URL:
                logger.info(
                    "ℹ️  WEBHOOK_PUBLIC_URL belum diset — webhook nonaktif, polling aktif sebagai fallback."
                )

        except OSError as exc:
            webhook_runner = None
            logger.warning(
                "⚠️ Webhook server gagal start (port %d): %s. Polling tetap aktif.",
                WEBHOOK_PORT,
                exc,
            )
        except Exception as exc:
            webhook_runner = None
            logger.exception("⚠️ Webhook server gagal start: %s", exc)
    else:
        logger.info("💳 Semua payment gateway disabled — hanya pembayaran manual")

    # Run bot polling
    try:
        await app.initialize()

        me = await app.bot.get_me()
        logger.info("🤖 Connected as @%s (id=%s)", me.username, me.id)

        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("🧹 Telegram webhook dihapus; mode polling aktif")

        await app.start()
        if app.updater is None:
            raise RuntimeError(
                "Application updater tidak tersedia. "
                "Pastikan Application dibuat dengan updater aktif."
            )
        await app.updater.start_polling(drop_pending_updates=False)
        logger.info("✅ Bot is running! Kirim /ping atau /start ke @%s", me.username)

        # Mulai background payment polling jika ada gateway yang aktif
        if ronzzpay_client is not None or pakasir_client is not None:
            polling_task = asyncio.create_task(
                payment_polling_loop(app.bot, ronzzpay_client, pakasir_client),
                name="payment_polling",
            )
            logger.info(
                "🔄 Background payment polling aktif (auto-confirm setiap %ds)",
                _POLL_INTERVAL,
            )

        # Keep running until interrupted
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass

    finally:
        # Cleanup polling task
        if polling_task and not polling_task.done():
            polling_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                pass

        if app.updater is not None:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if webhook_runner:
            from webhook.server import stop_webhook_server

            await stop_webhook_server(webhook_runner)


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
