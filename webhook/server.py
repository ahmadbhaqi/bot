"""
Webhook Server (RonzzPay & Pakasir)
=====================================
Lightweight aiohttp server yang menerima callback notifikasi pembayaran.

Endpoints:
  POST /webhook/ronzzpay  — notifikasi dari RonzzPay
  POST /webhook/pakasir   — notifikasi dari Pakasir
  GET  /health            — health check

RonzzPay events:
  transaction.success  → auto-confirm order & kirim akun ke user
  withdraw.success     → log only

Pakasir events:
  status = "completed" → auto-confirm order & kirim akun ke user
"""

import json
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

# Will be set from main.py after bot application is built
_bot_app = None
_on_payment_success = None  # RonzzPay callback
_on_pakasir_payment_success = None  # Pakasir callback


def set_bot_app(bot_app):
    """Store reference to the Telegram bot Application for sending messages."""
    global _bot_app
    _bot_app = bot_app


def set_payment_callback(callback):
    """
    Register the callback to invoke when a RonzzPay payment succeeds.

    Signature: async def callback(reff_id: str, data: dict, bot) -> None
    """
    global _on_payment_success
    _on_payment_success = callback


def set_pakasir_payment_callback(callback):
    """
    Register the callback to invoke when a Pakasir payment succeeds.

    Signature: async def callback(order_id: str, data: dict, bot) -> None
    """
    global _on_pakasir_payment_success
    _on_pakasir_payment_success = callback


# ------------------------------------------------------------------
# Webhook endpoint handler
# ------------------------------------------------------------------


async def handle_ronzzpay_webhook(request: web.Request) -> web.Response:
    """
    Handle incoming POST /webhook/ronzzpay from RonzzPay.

    Expected JSON body:
    {
        "event": "transaction.success",
        "data": {
            "reff_id": "...",
            "description": "...",
            "amount": 50000,
            "status": "success",
            ...
        }
    }
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Webhook: invalid JSON body — %s", exc)
        return web.Response(status=400, text="Invalid JSON")

    event = body.get("event", "")
    data = body.get("data", {})
    reff_id = data.get("reff_id", "unknown")

    logger.info(
        "Webhook received: event=%s  reff_id=%s  amount=%s  status=%s",
        event,
        reff_id,
        data.get("amount"),
        data.get("status"),
    )

    if event == "transaction.success":
        if _on_payment_success and _bot_app:
            try:
                bot = _bot_app.bot
                await _on_payment_success(reff_id, data, bot)
            except Exception as exc:
                logger.error("Webhook callback error for reff_id=%s: %s", reff_id, exc)
        else:
            logger.warning(
                "Webhook: transaction.success received but no callback registered "
                "(reff_id=%s)",
                reff_id,
            )

    elif event == "withdraw.success":
        logger.info(
            "Webhook: withdraw.success — reff_id=%s amount=%s",
            reff_id,
            data.get("amount"),
        )

    else:
        logger.info("Webhook: unhandled event '%s'", event)

    # Always respond 200 OK so RonzzPay doesn't retry
    return web.Response(status=200, text="OK")


# ------------------------------------------------------------------
# Pakasir webhook endpoint handler
# ------------------------------------------------------------------


async def handle_pakasir_webhook(request: web.Request) -> web.Response:
    """
    Handle incoming POST /webhook/pakasir dari Pakasir.

    Expected JSON body:
    {
        "amount": 22000,
        "order_id": "ABCD1234",
        "project": "yourslug",
        "status": "completed",
        "payment_method": "qris",
        "completed_at": "2024-09-10T08:07:02.819+07:00"
    }
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Pakasir Webhook: invalid JSON body — %s", exc)
        return web.Response(status=400, text="Invalid JSON")

    order_id = body.get("order_id", "unknown")
    status = body.get("status", "")
    amount = body.get("amount")

    logger.info(
        "Pakasir Webhook received: order_id=%s  status=%s  amount=%s  method=%s",
        order_id,
        status,
        amount,
        body.get("payment_method"),
    )

    if status == "completed":
        if _on_pakasir_payment_success and _bot_app:
            try:
                bot = _bot_app.bot
                await _on_pakasir_payment_success(order_id, body, bot)
            except Exception as exc:
                logger.error(
                    "Pakasir Webhook callback error for order_id=%s: %s", order_id, exc
                )
        else:
            logger.warning(
                "Pakasir Webhook: 'completed' received but no callback registered "
                "(order_id=%s)",
                order_id,
            )
    else:
        logger.info(
            "Pakasir Webhook: unhandled status '%s' for order_id=%s", status, order_id
        )

    # Selalu respons 200 OK agar Pakasir tidak retry
    return web.Response(status=200, text="OK")


async def handle_health(request: web.Request) -> web.Response:
    """Simple health check endpoint."""
    return web.Response(status=200, text="OK")


# ------------------------------------------------------------------
# Server lifecycle
# ------------------------------------------------------------------


def create_webhook_app() -> web.Application:
    """Create and configure the aiohttp web application."""
    app = web.Application()
    app.router.add_post("/webhook/ronzzpay", handle_ronzzpay_webhook)
    app.router.add_post("/webhook/pakasir", handle_pakasir_webhook)
    app.router.add_get("/health", handle_health)
    return app


async def start_webhook_server(
    host: str = "0.0.0.0", port: int = 8080
) -> web.AppRunner:
    """
    Start the webhook server.
    Returns the runner so it can be cleaned up later.
    """
    app = create_webhook_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Webhook server started on http://%s:%d", host, port)
    return runner


async def stop_webhook_server(runner: web.AppRunner) -> None:
    """Cleanly shut down the webhook server."""
    if runner:
        await runner.cleanup()
        logger.info("Webhook server stopped.")
