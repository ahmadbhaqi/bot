import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Data classes for typed responses
# ------------------------------------------------------------------


@dataclass
class RonzzPayProfile:
    username: str
    balance: int
    role: str
    raw: Dict[str, Any]


@dataclass
class RonzzPayTransaction:
    reff_id: str
    description: str
    method: str
    code: str
    type: str
    amount: int  # total yang dibayar user (sudah termasuk fee)
    fee: int
    net_amount: int  # jumlah yang diterima merchant (get_amount di API)
    # Field QR / link pembayaran — minimal satu pasti ada
    qr_string: Optional[str]  # raw QRIS string (untuk disalin atau generate QR)
    qr_image: Optional[str]  # URL gambar QR code siap tampil
    pay_url: Optional[str]  # URL halaman pembayaran (jika bukan QRIS)
    # Info tambahan
    payment_name: Optional[str]  # nama metode pembayaran, misal "RonzzPay"
    instructions: Optional[str]  # instruksi pembayaran dalam bahasa manusia
    status: str
    expired_at: Optional[str]
    raw: Dict[str, Any]


@dataclass
class RonzzPayStatus:
    reff_id: str
    status: str
    amount: int
    raw: Dict[str, Any]


# ------------------------------------------------------------------
# Main client
# ------------------------------------------------------------------


class RonzzPayClient:
    """Client for the RonzzPay Payment Gateway API."""

    PRODUCTION_BASE = "https://pg.ronzzyt.id/api"
    SANDBOX_BASE = "https://pg.ronzzyt.id/sandbox"

    def __init__(self, api_key: str, sandbox: bool = False, timeout: int = 30):
        self.api_key = api_key
        self.sandbox = sandbox
        self.base_url = self.SANDBOX_BASE if sandbox else self.PRODUCTION_BASE
        self.timeout = timeout
        self._session = requests.Session()

        env_label = "SANDBOX" if sandbox else "PRODUCTION"
        logger.info("RonzzPay client initialized [%s] → %s", env_label, self.base_url)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, endpoint: str, **kwargs) -> Dict[str, Any]:
        """
        Send a POST request to RonzzPay.
        Always injects api_key into the form body.
        Returns the parsed JSON response or raises on error.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        data = {"api_key": self.api_key, **kwargs}

        logger.debug(
            "RonzzPay POST %s  data=%s",
            url,
            {k: v for k, v in data.items() if k != "api_key"},
        )

        try:
            resp = self._session.post(url, data=data, timeout=self.timeout)
            resp.raise_for_status()
            result = resp.json()
        except requests.exceptions.Timeout:
            logger.error("RonzzPay request timed out: %s", url)
            raise RonzzPayError("Request timeout ke RonzzPay")
        except requests.exceptions.ConnectionError as exc:
            logger.error("RonzzPay connection error: %s", exc)
            raise RonzzPayError("Tidak bisa terhubung ke RonzzPay")
        except requests.exceptions.HTTPError as exc:
            logger.error("RonzzPay HTTP error: %s", exc)
            raise RonzzPayError(f"HTTP error: {exc}")
        except ValueError:
            logger.error("RonzzPay returned non-JSON response")
            raise RonzzPayError("Response RonzzPay bukan JSON")

        if not result.get("status"):
            msg = result.get("message", "Unknown error")
            logger.warning("RonzzPay API error: %s", msg)
            raise RonzzPayError(msg)

        return result

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self) -> RonzzPayProfile:
        """Dapatkan info profil dan saldo akun."""
        result = self._post("profile")
        data = result.get("data", {})
        return RonzzPayProfile(
            username=data.get("username", ""),
            balance=data.get("balance", 0),
            role=data.get("role", ""),
            raw=data,
        )

    # ------------------------------------------------------------------
    # Transaction (pembayaran masuk)
    # ------------------------------------------------------------------

    def create_transaction(
        self,
        code: str,
        amount: int,
        description: str = "",
    ) -> RonzzPayTransaction:
        kwargs = {"code": code, "amount": amount}
        if description:
            kwargs["description"] = description[:255]

        result = self._post("transaction/create", **kwargs)
        data = result.get("data", {})

        return RonzzPayTransaction(
            reff_id=data.get("reff_id", ""),
            description=data.get("description", ""),
            method=data.get("method", ""),
            code=data.get("code", ""),
            type=data.get("type", ""),
            amount=data.get("amount", 0),
            fee=data.get("fee", 0),
            # API RonzzPay mengembalikan "get_amount" bukan "net_amount"
            net_amount=data.get("get_amount") or data.get("net_amount", 0),
            # QR fields — API sandbox/production bisa berbeda key
            qr_string=data.get("qr_string") or None,
            qr_image=data.get("qr_image") or None,
            pay_url=data.get("pay_url") or None,
            # Info tambahan
            payment_name=data.get("payment_name") or None,
            instructions=data.get("instructions") or None,
            status=data.get("status", ""),
            expired_at=data.get("expired_at"),
            raw=data,
        )

    def check_transaction_status(self, reff_id: str) -> RonzzPayStatus:
        """Cek status transaksi berdasarkan reff_id."""
        result = self._post("transaction/status", reff_id=reff_id)
        data = result.get("data", {})
        return RonzzPayStatus(
            reff_id=data.get("reff_id", reff_id),
            status=data.get("status", "unknown"),
            amount=data.get("amount", 0),
            raw=data,
        )

    def list_transactions(self) -> List[Dict[str, Any]]:
        """Dapatkan riwayat semua transaksi."""
        result = self._post("transaction/list")
        return result.get("data", [])

    def get_payment_methods(self) -> list:
        """Dapatkan daftar metode pembayaran yang tersedia beserta kode dan fee-nya."""
        result = self._post("transaction/methods")
        return result.get("data", [])

    def set_webhook_url(self, url: str) -> bool:
        """
        Daftarkan URL webhook ke RonzzPay agar notifikasi transaction.success
        dikirim otomatis ke server kita.
        Kembalikan True jika berhasil.
        """
        try:
            self._post("webhook/set", url=url)
            return True
        except Exception as exc:
            logger.warning("set_webhook_url failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Withdraw (pencairan dana)
    # ------------------------------------------------------------------

    def create_withdraw(
        self,
        amount: int,
        method: str,
        code: str,
        account_number: str,
        account_name: str,
    ) -> Dict[str, Any]:
        result = self._post(
            "withdraw/create",
            amount=amount,
            method=method,
            code=code,
            account_number=account_number,
            account_name=account_name,
        )
        return result.get("data", {})

    def check_withdraw_status(self, reff_id: str) -> RonzzPayStatus:
        """Cek status pencairan berdasarkan reff_id."""
        result = self._post("withdraw/status", reff_id=reff_id)
        data = result.get("data", {})
        return RonzzPayStatus(
            reff_id=data.get("reff_id", reff_id),
            status=data.get("status", "unknown"),
            amount=data.get("amount", 0),
            raw=data,
        )

    def list_withdrawals(self) -> List[Dict[str, Any]]:
        """Dapatkan riwayat semua withdraw."""
        result = self._post("withdraw/list")
        return result.get("data", [])


# ------------------------------------------------------------------
# Exception
# ------------------------------------------------------------------


class RonzzPayError(Exception):
    """Raised when a RonzzPay API call fails."""

    pass
