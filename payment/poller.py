"""
Background payment poller untuk transaksi RonzzPay.

Cara kerja:
  - Setelah transaksi RonzzPay dibuat, `start_poll()` dipanggil untuk
    memulai task asyncio yang mengecek status setiap POLL_INTERVAL detik.
  - Jika status SUCCESS  → callback `on_success(order_id)` dipanggil.
  - Jika status EXPIRED  → callback `on_expire(order_id)` dipanggil.
  - Setelah MAX_WAIT detik tanpa respons → `on_expire` juga dipanggil.
  - Jika webhook RonzzPay tiba lebih dulu, `cancel_poll(reff_id)` dipanggil
    untuk membatalkan task sehingga tidak terjadi double-confirm.

Catatan keamanan:
  - `auto_confirm_order` sudah idempotent (cek status sebelum eksekusi),
    sehingga kalau keduanya (webhook + poller) terpicu bersamaan tetap aman.
"""

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# ── Konfigurasi ──────────────────────────────────────────────────────────────
POLL_INTERVAL: int = 15  # detik antara setiap cek status
MAX_WAIT: int = 900  # 15 menit — waktu maksimum sebelum dianggap expired

# reff_id → asyncio.Task yang sedang berjalan
_active: dict[str, asyncio.Task] = {}


# ── Internal loop ─────────────────────────────────────────────────────────────


async def _poll_loop(
    reff_id: str,
    order_id: str,
    check_fn: Callable,  # sync: client.check_transaction_status
    on_success: Callable[[str], Awaitable[None]],
    on_expire: Callable[[str], Awaitable[None]],
) -> None:
    elapsed = 0

    while elapsed < MAX_WAIT:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        try:
            # check_fn adalah fungsi sinkron (requests), jalankan di thread
            result = await asyncio.to_thread(check_fn, reff_id)

            logger.debug(
                "Poller [%s] order=%s status=%s elapsed=%ds",
                reff_id,
                order_id,
                result.status,
                elapsed,
            )

            if result.status == "success":
                logger.info(
                    "Poller: pembayaran terkonfirmasi reff_id=%s order=%s",
                    reff_id,
                    order_id,
                )
                await on_success(order_id)
                return

            if result.status in ("expired", "failed", "cancelled"):
                logger.info(
                    "Poller: order %s selesai dengan status=%s",
                    order_id,
                    result.status,
                )
                await on_expire(order_id)
                return

            # status masih "pending" → lanjut polling

        except asyncio.CancelledError:
            logger.info(
                "Poller dibatalkan untuk order=%s reff_id=%s", order_id, reff_id
            )
            raise

        except Exception as exc:
            # Jangan hentikan loop hanya karena network error sementara
            logger.warning("Poller error order=%s: %s", order_id, exc)

    # Waktu habis tanpa konfirmasi
    logger.warning(
        "Poller timeout (%ds) untuk order=%s reff_id=%s",
        MAX_WAIT,
        order_id,
        reff_id,
    )
    await on_expire(order_id)


async def _run_poll(
    reff_id: str,
    order_id: str,
    check_fn: Callable,
    on_success: Callable[[str], Awaitable[None]],
    on_expire: Callable[[str], Awaitable[None]],
) -> None:
    """Wrapper agar _active dibersihkan meski task dibatalkan / exception."""
    try:
        await _poll_loop(reff_id, order_id, check_fn, on_success, on_expire)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.exception("Poller fatal error order=%s: %s", order_id, exc)
    finally:
        _active.pop(reff_id, None)


# ── Public API ────────────────────────────────────────────────────────────────


def start_poll(
    reff_id: str,
    order_id: str,
    check_fn: Callable,
    on_success: Callable[[str], Awaitable[None]],
    on_expire: Callable[[str], Awaitable[None]],
) -> None:
    """
    Mulai polling background untuk satu transaksi RonzzPay.

    Args:
        reff_id   : Reference ID transaksi dari RonzzPay
        order_id  : ID pesanan internal bot
        check_fn  : Fungsi sinkron untuk cek status (client.check_transaction_status)
        on_success: Coroutine async yang dipanggil saat pembayaran berhasil
        on_expire : Coroutine async yang dipanggil saat expired / timeout
    """
    if reff_id in _active:
        logger.debug("Poller sudah aktif untuk reff_id=%s, skip.", reff_id)
        return

    task = asyncio.create_task(
        _run_poll(reff_id, order_id, check_fn, on_success, on_expire),
        name=f"poll-{order_id}",
    )
    _active[reff_id] = task
    logger.info("Poller dimulai: order=%s reff_id=%s", order_id, reff_id)


def cancel_poll(reff_id: str) -> None:
    """
    Batalkan task polling yang sedang aktif.
    Dipanggil saat webhook RonzzPay sudah menangani event lebih dulu,
    atau saat user membatalkan pesanan.
    """
    task = _active.pop(reff_id, None)
    if task and not task.done():
        task.cancel()
        logger.info("Poller dibatalkan (webhook/cancel): reff_id=%s", reff_id)


def active_count() -> int:
    """Jumlah task polling yang sedang berjalan (untuk debugging)."""
    return len(_active)
