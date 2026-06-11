"""
Pakasir Payment Gateway Client
================================
Docs: https://pakasir.com/p/docs

Integrasi via API:
  POST  /api/transactioncreate/{method}   — buat transaksi
  GET   /api/transactiondetail            — cek status
  POST  /api/transactioncancel            — batalkan transaksi

Webhook diterima oleh server kita di /webhook/pakasir.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)


# Label nama metode pembayaran yang tampil ke user
PAYMENT_METHOD_LABELS: Dict[str, str] = {
    "qris": "QRIS",
    "bni_va": "BNI Virtual Account",
    "bri_va": "BRI Virtual Account",
    "cimb_niaga_va": "CIMB Niaga Virtual Account",
    "sampoerna_va": "Bank Sampoerna Virtual Account",
    "bnc_va": "BNC Virtual Account",
    "maybank_va": "Maybank Virtual Account",
    "permata_va": "Permata Virtual Account",
    "atm_bersama_va": "ATM Bersama Virtual Account",
    "artha_graha_va": "Artha Graha Virtual Account",
}


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------


@dataclass
class PakasirTransaction:
    project: str
    order_id: str
    amount: int  # harga asli produk
    fee: int  # biaya layanan Pakasir
    total_payment: int  # total yang harus dibayar user (amount + fee)
    payment_method: str  # qris, bni_va, dll.
    payment_number: str  # QR string (QRIS) atau nomor VA
    expired_at: str
    raw: Dict[str, Any]

    @property
    def is_qris(self) -> bool:
        return self.payment_method == "qris"

    @property
    def is_va(self) -> bool:
        return self.payment_method.endswith("_va")

    @property
    def method_label(self) -> str:
        return PAYMENT_METHOD_LABELS.get(
            self.payment_method, self.payment_method.upper()
        )


@dataclass
class PakasirStatus:
    order_id: str
    status: str  # "completed", "pending", dll.
    amount: int
    payment_method: str
    completed_at: Optional[str]
    raw: Dict[str, Any]


# ------------------------------------------------------------------
# Client
# ------------------------------------------------------------------


class PakasirClient:
    """Client untuk Pakasir Payment Gateway API."""

    BASE_URL = "https://app.pakasir.com/api"
    PAY_URL_BASE = "https://app.pakasir.com/pay"

    def __init__(self, api_key: str, project_slug: str, timeout: int = 30):
        self.api_key = api_key
        self.project_slug = project_slug
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})
        logger.info("Pakasir client initialized → project=%s", project_slug)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _base_payload(self, order_id: str, amount: int) -> Dict[str, Any]:
        return {
            "project": self.project_slug,
            "order_id": order_id,
            "amount": amount,
            "api_key": self.api_key,
        }

    def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            logger.error("Pakasir timeout: %s", url)
            raise PakasirError("Request timeout ke Pakasir")
        except requests.exceptions.ConnectionError as exc:
            logger.error("Pakasir connection error: %s", exc)
            raise PakasirError("Tidak bisa terhubung ke Pakasir")
        except requests.exceptions.HTTPError as exc:
            logger.error("Pakasir HTTP error: %s", exc)
            raise PakasirError(f"HTTP error Pakasir: {exc}")
        except ValueError:
            logger.error("Pakasir non-JSON response")
            raise PakasirError("Response Pakasir bukan JSON")

    def _get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            raise PakasirError("Request timeout ke Pakasir")
        except requests.exceptions.ConnectionError:
            raise PakasirError("Tidak bisa terhubung ke Pakasir")
        except requests.exceptions.HTTPError as exc:
            raise PakasirError(f"HTTP error Pakasir: {exc}")
        except ValueError:
            raise PakasirError("Response Pakasir bukan JSON")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_transaction(
        self,
        method: str,
        order_id: str,
        amount: int,
    ) -> PakasirTransaction:
        """
        Buat transaksi baru di Pakasir.

        Args:
            method   : Metode pembayaran (qris, bni_va, bri_va, dll.)
            order_id : ID pesanan internal bot
            amount   : Nominal transaksi produk (belum termasuk fee)

        Returns:
            PakasirTransaction dengan detail pembayaran.
        """
        payload = self._base_payload(order_id, amount)
        logger.debug(
            "Pakasir create_transaction: method=%s order_id=%s amount=%s",
            method,
            order_id,
            amount,
        )

        result = self._post(f"transactioncreate/{method}", payload)

        if "payment" not in result:
            msg = result.get("message", result.get("error", "Unknown error"))
            logger.warning("Pakasir create error: %s | raw=%s", msg, result)
            raise PakasirError(f"Pakasir API error: {msg}")

        data = result["payment"]
        txn = PakasirTransaction(
            project=data.get("project", self.project_slug),
            order_id=data.get("order_id", order_id),
            amount=data.get("amount", amount),
            fee=data.get("fee", 0),
            total_payment=data.get("total_payment", amount),
            payment_method=data.get("payment_method", method),
            payment_number=data.get("payment_number", ""),
            expired_at=data.get("expired_at", ""),
            raw=data,
        )
        logger.info(
            "Pakasir txn created: order_id=%s method=%s total=%s fee=%s expired=%s",
            txn.order_id,
            txn.payment_method,
            txn.total_payment,
            txn.fee,
            txn.expired_at,
        )
        return txn

    def check_transaction_status(self, order_id: str, amount: int) -> PakasirStatus:
        """
        Cek status transaksi Pakasir.

        Args:
            order_id : ID pesanan internal bot
            amount   : total_payment yang tersimpan di order (bukan harga produk)
        """
        params = {
            "project": self.project_slug,
            "amount": amount,
            "order_id": order_id,
            "api_key": self.api_key,
        }
        logger.debug("Pakasir check_status: order_id=%s amount=%s", order_id, amount)

        result = self._get("transactiondetail", params)

        if "transaction" not in result:
            msg = result.get("message", result.get("error", "Unknown error"))
            logger.warning("Pakasir status error: %s | raw=%s", msg, result)
            raise PakasirError(f"Pakasir API error: {msg}")

        data = result["transaction"]
        return PakasirStatus(
            order_id=data.get("order_id", order_id),
            status=data.get("status", "unknown"),
            amount=data.get("amount", amount),
            payment_method=data.get("payment_method", ""),
            completed_at=data.get("completed_at"),
            raw=data,
        )

    def cancel_transaction(self, order_id: str, amount: int) -> bool:
        """
        Batalkan transaksi Pakasir.
        Returns True jika berhasil.
        """
        payload = self._base_payload(order_id, amount)
        try:
            self._post("transactioncancel", payload)
            logger.info("Pakasir transaction cancelled: order_id=%s", order_id)
            return True
        except PakasirError as exc:
            logger.warning("Pakasir cancel error: %s", exc)
            return False

    def get_pay_url(
        self,
        order_id: str,
        amount: int,
        qris_only: bool = False,
        redirect_url: str = "",
    ) -> str:
        """
        Generate URL pembayaran Pakasir (integrasi via URL / fallback).

        Args:
            order_id    : ID pesanan internal bot
            amount      : Nominal transaksi
            qris_only   : True → paksa tampil QRIS saja
            redirect_url: URL tujuan setelah pembayaran berhasil
        """
        url = f"{self.PAY_URL_BASE}/{self.project_slug}/{amount}?order_id={order_id}"
        if qris_only:
            url += "&qris_only=1"
        if redirect_url:
            url += f"&redirect={redirect_url}"
        return url


# ------------------------------------------------------------------
# Exception
# ------------------------------------------------------------------


class PakasirError(Exception):
    """Raised when a Pakasir API call fails."""

    pass
