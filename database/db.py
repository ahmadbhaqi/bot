import json
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

# Paths
_BASE = os.path.dirname(__file__)
PRODUCTS_FILE = os.path.join(_BASE, "..", "data", "products.json")
ORDERS_FILE = os.path.join(_BASE, "..", "data", "orders.json")
SETTINGS_FILE = os.path.join(_BASE, "..", "data", "settings.json")

_lock = threading.Lock()

# =============================================================
# SETTINGS
# =============================================================


def _load_settings() -> Dict[str, Any]:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_settings(data: Dict[str, Any]) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_setting(key: str, default: Any = None) -> Any:
    """Ambil nilai pengaturan dari settings.json."""
    return _load_settings().get(key, default)


def set_setting(key: str, value: Any) -> None:
    """Simpan nilai pengaturan ke settings.json."""
    with _lock:
        settings = _load_settings()
        settings[key] = value
        _save_settings(settings)


# =============================================================
# PRODUCTS
# =============================================================


def load_products() -> Dict[str, Any]:
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_products(data: Dict[str, Any]) -> None:
    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_product(product_id: str) -> Optional[Dict[str, Any]]:
    return load_products().get(product_id)


def is_service_product(product_id: str) -> bool:
    """True bila produk adalah JASA (tidak butuh stok, selalu tersedia).

    Produk jasa ditandai dengan field `"is_service": true` di products.json.
    Contoh: ghs_do (jasa klaim DO pakai GHS seller) dan
            ghs_do_buyer (jasa klaim DO pakai GHS buyer sendiri).
    """
    p = get_product(product_id)
    return bool(p and p.get("is_service"))


def _resolve_stock_id(product_id: str, products: Dict[str, Any]) -> str:
    """Kembalikan product_id sumber stok yang sebenarnya.
    Jika produk punya field 'shared_stock_with', redirect ke produk tersebut."""
    p = products.get(product_id, {})
    shared = p.get("shared_stock_with")
    if shared and shared in products:
        return shared
    return product_id


def get_stock_count(product_id: str) -> int:
    products = load_products()
    target_id = _resolve_stock_id(product_id, products)
    target = products.get(target_id)
    if not target:
        return 0
    return len(target.get("accounts", []))


def add_stock_accounts(product_id: str, accounts: List[str]) -> int:
    """Append accounts to product stock. Returns number added.
    Jika produk punya shared_stock_with, akun ditambahkan ke produk sumber."""
    with _lock:
        products = load_products()
        if product_id not in products:
            return 0
        target_id = _resolve_stock_id(product_id, products)
        products[target_id].setdefault("accounts", []).extend(accounts)
        _save_products(products)
        return len(accounts)


def take_stock_account(product_id: str) -> Optional[str]:
    """Pop one account from stock (FIFO). Returns account string or None.
    Jika produk punya shared_stock_with, ambil dari produk sumber."""
    with _lock:
        products = load_products()
        if product_id not in products:
            return None
        target_id = _resolve_stock_id(product_id, products)
        accounts: list = products[target_id].get("accounts", [])
        if not accounts:
            return None
        account = accounts.pop(0)
        products[target_id]["accounts"] = accounts
        _save_products(products)
        return account


def take_stock_accounts(product_id: str, count: int) -> List[str]:
    """Pop beberapa akun sekaligus dari stok (FIFO, atomic).
    Kembalikan list akun. Jika stok kurang dari count, ambil sebanyak yang tersedia.
    Jika produk punya shared_stock_with, ambil dari produk sumber."""
    with _lock:
        products = load_products()
        if product_id not in products:
            return []
        target_id = _resolve_stock_id(product_id, products)
        accounts: list = products[target_id].get("accounts", [])
        if not accounts:
            return []
        taken = accounts[:count]
        products[target_id]["accounts"] = accounts[count:]
        _save_products(products)
        return taken


def update_product_price(product_id: str, new_price: int) -> bool:
    with _lock:
        products = load_products()
        if product_id not in products:
            return False
        products[product_id]["price"] = new_price
        _save_products(products)
        return True


def create_product(
    product_id: str,
    name: str,
    emoji: str,
    price: int,
    description: str = "",
) -> bool:
    """Buat produk baru. Return False bila product_id sudah ada."""
    with _lock:
        products = load_products()
        if product_id in products:
            return False
        products[product_id] = {
            "id": product_id,
            "name": name,
            "emoji": emoji or "📦",
            "description": description or name,
            "price": int(price),
            "accounts": [],
        }
        _save_products(products)
        return True


def delete_product(product_id: str) -> bool:
    """Hapus produk dari katalog. Return False bila tidak ditemukan."""
    with _lock:
        products = load_products()
        if product_id not in products:
            return False
        del products[product_id]
        _save_products(products)
        return True


def take_all_stock(product_id: str) -> List[str]:
    """Ambil SEMUA akun dari stok produk lalu kosongkan. Kembalikan list akun.
    Untuk fitur 'ambil stok lalu hapus' (export + clear)."""
    with _lock:
        products = load_products()
        if product_id not in products:
            return []
        target_id = _resolve_stock_id(product_id, products)
        accounts: list = list(products[target_id].get("accounts", []))
        products[target_id]["accounts"] = []
        _save_products(products)
        return accounts


# =============================================================
# ORDERS
# =============================================================


def load_orders() -> Dict[str, Any]:
    try:
        with open(ORDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_orders(data: Dict[str, Any]) -> None:
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def create_order(
    user_id: int, username: str, product_id: str, quantity: int = 1
) -> str:
    """Create a new pending order. Returns the order_id."""
    product = get_product(product_id)
    if not product:
        raise ValueError(f"Product '{product_id}' not found")

    order_id = str(uuid.uuid4())[:8].upper()
    now = datetime.now().isoformat()

    order: Dict[str, Any] = {
        "id": order_id,
        "user_id": user_id,
        "username": username or "unknown",
        "product_id": product_id,
        "product_name": product["name"],
        "price": product["price"],
        "quantity": quantity,
        "total_price": product["price"] * quantity,
        # Status values:
        #   pending_payment  -> order created, waiting for user to pay
        #   payment_sent     -> user sent proof, waiting for admin to confirm
        #   confirmed        -> admin confirmed, account delivered to user
        #   rejected         -> admin rejected
        #   cancelled        -> user/admin cancelled
        #   paid             -> RonzzPay confirmed payment (auto)
        "status": "pending_payment",
        "account_delivered": None,
        "ghs_account_used": None,  # Internal: kredensial GHS (email:pass:totp), TIDAK ditampilkan ke user
        "do_claim_index": 0,  # Untuk ghs_do bulk: berapa akun yang sudah diklaim user
        "payment_proof_file_id": None,
        # RonzzPay-specific fields
        "payment_method": None,  # 'ronzzpay' or 'manual'
        "ronzzpay_reff_id": None,  # RonzzPay reference ID
        "ronzzpay_code": None,  # Payment method code (qris, dana, etc.)
        "ronzzpay_amount": None,  # Total amount including fee
        "ronzzpay_fee": None,  # RonzzPay fee
        "ronzzpay_expired_at": None,  # When the payment link expires
        "ronzzpay_paid_at": None,  # When payment was confirmed
        "created_at": now,
        "updated_at": now,
    }

    with _lock:
        orders = load_orders()
        orders[order_id] = order
        _save_orders(orders)

    return order_id


def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    return load_orders().get(order_id)


def create_topup_order(user_id: int, username: str, amount: int) -> str:
    """Buat order TOP-UP saldo (pakai gateway pembayaran). Return order_id.

    Order top-up tidak terkait produk; field `is_topup=True` & `topup_amount`
    dipakai auto_confirm_order untuk mengkreditkan saldo, bukan mengirim akun.
    """
    order_id = "TOP" + str(uuid.uuid4())[:5].upper()
    now = datetime.now().isoformat()
    order: Dict[str, Any] = {
        "id": order_id,
        "user_id": user_id,
        "username": username or "unknown",
        "product_id": "__topup__",
        "product_name": f"Top Up Saldo {amount}",
        "price": int(amount),
        "quantity": 1,
        "total_price": int(amount),
        "status": "pending_payment",
        "is_topup": True,
        "topup_amount": int(amount),
        "account_delivered": None,
        "ghs_account_used": None,
        "do_claim_index": 0,
        "payment_proof_file_id": None,
        "payment_method": None,
        "created_at": now,
        "updated_at": now,
    }
    with _lock:
        orders = load_orders()
        orders[order_id] = order
        _save_orders(orders)
    return order_id


def create_coupon_order(
    user_id: int, username: str, amount: int, job: Dict[str, Any]
) -> str:
    """Buat order JASA KLAIM KUPON DO yang dibayar via gateway (QRIS).

    `job` berisi data untuk menjalankan automation setelah pembayaran lunas
    (metode login, kredensial DO, daftar promo code). auto_confirm_order akan
    mendeteksi `is_coupon_service=True` lalu menjalankan klaim.
    """
    order_id = "CPN" + str(uuid.uuid4())[:5].upper()
    now = datetime.now().isoformat()
    count = len(job.get("promo_codes", []) or [])
    order: Dict[str, Any] = {
        "id": order_id,
        "user_id": user_id,
        "username": username or "unknown",
        "product_id": "__coupon__",
        "product_name": f"Jasa Klaim Kupon DO x{count}",
        "price": int(amount),
        "quantity": count or 1,
        "total_price": int(amount),
        "status": "pending_payment",
        "is_coupon_service": True,
        "coupon_job": job,
        "account_delivered": None,
        "ghs_account_used": None,
        "do_claim_index": 0,
        "payment_proof_file_id": None,
        "payment_method": None,
        "created_at": now,
        "updated_at": now,
    }
    with _lock:
        orders = load_orders()
        orders[order_id] = order
        _save_orders(orders)
    return order_id


def try_lock_order_for_confirm(order_id: str) -> bool:
    """
    Guard atomik untuk mencegah double-delivery.
    Menggunakan os.mkdir untuk atomic cross-process lock.
    """
    lock_dir = os.path.join(_BASE, "..", "data", f"{order_id}.lock")
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        return False

    with _lock:
        orders = load_orders()
        if order_id not in orders:
            try:
                os.rmdir(lock_dir)
            except OSError:
                pass
            return False
        status = orders[order_id].get("status")
        if status not in ("pending_payment", "paid"):
            try:
                os.rmdir(lock_dir)
            except OSError:
                pass
            return False
        # Tandai sebagai 'processing' agar tidak ada caller lain yang lolos
        orders[order_id]["status"] = "processing"
        orders[order_id]["updated_at"] = datetime.now().isoformat()
        _save_orders(orders)
        return True


def unlock_order(order_id: str) -> None:
    """Membuka lock agar bisa diproses ulang."""
    lock_dir = os.path.join(_BASE, "..", "data", f"{order_id}.lock")
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass


def update_order(order_id: str, **kwargs) -> bool:
    with _lock:
        orders = load_orders()
        if order_id not in orders:
            return False
        orders[order_id].update(kwargs)
        orders[order_id]["updated_at"] = datetime.now().isoformat()
        _save_orders(orders)
        return True


def get_order_by_reff_id(reff_id: str) -> Optional[Dict[str, Any]]:
    """Find an order by its RonzzPay reference ID."""
    orders = load_orders()
    for order in orders.values():
        if order.get("ronzzpay_reff_id") == reff_id:
            return order
    return None


def get_user_orders(user_id: int) -> List[Dict[str, Any]]:
    orders = load_orders()
    result = [o for o in orders.values() if o["user_id"] == user_id]
    return sorted(result, key=lambda x: x["created_at"], reverse=True)


def get_orders_by_status(status: str) -> List[Dict[str, Any]]:
    orders = load_orders()
    result = [o for o in orders.values() if o["status"] == status]
    return sorted(result, key=lambda x: x["created_at"])


def get_all_orders() -> List[Dict[str, Any]]:
    orders = load_orders()
    return sorted(orders.values(), key=lambda x: x["created_at"], reverse=True)


def get_stats() -> Dict[str, Any]:
    orders = load_orders()
    all_orders = list(orders.values())

    confirmed = [o for o in all_orders if o["status"] == "confirmed"]
    pending = [o for o in all_orders if o["status"] == "pending_payment"]
    sent = [o for o in all_orders if o["status"] == "payment_sent"]
    rejected = [o for o in all_orders if o["status"] == "rejected"]
    cancelled = [o for o in all_orders if o["status"] == "cancelled"]
    paid_auto = [o for o in all_orders if o["status"] == "paid"]

    products = load_products()
    stock_info = {pid: len(p.get("accounts", [])) for pid, p in products.items()}

    # Count RonzzPay vs manual payments
    ronzzpay_orders = [o for o in confirmed if o.get("payment_method") == "ronzzpay"]
    manual_orders = [o for o in confirmed if o.get("payment_method") != "ronzzpay"]

    return {
        "total_orders": len(all_orders),
        "confirmed": len(confirmed),
        "pending": len(pending),
        "payment_sent": len(sent),
        "rejected": len(rejected),
        "cancelled": len(cancelled),
        "paid_auto": len(paid_auto),
        "total_revenue": sum(o.get("total_price", o["price"]) for o in confirmed),
        "ronzzpay_revenue": sum(
            o.get("total_price", o["price"]) for o in ronzzpay_orders
        ),
        "manual_revenue": sum(o.get("total_price", o["price"]) for o in manual_orders),
        "stock": stock_info,
    }


def get_pending_ronzzpay_orders() -> List[Dict[str, Any]]:
    """Ambil semua order dengan status pending_payment dan payment_method=ronzzpay.
    Digunakan oleh background polling loop untuk auto-confirm pembayaran."""
    orders = load_orders()
    result = [
        o
        for o in orders.values()
        if o.get("status") == "pending_payment"
        and o.get("payment_method") == "ronzzpay"
        and o.get("ronzzpay_reff_id")
    ]
    return sorted(result, key=lambda x: x["created_at"])


def get_pending_pakasir_orders() -> List[Dict[str, Any]]:
    """Ambil semua order dengan status pending_payment dan payment_method=pakasir.
    Digunakan oleh background polling loop untuk auto-confirm pembayaran Pakasir."""
    orders = load_orders()
    result = [
        o
        for o in orders.values()
        if o.get("status") == "pending_payment"
        and o.get("payment_method") == "pakasir"
        # pakasir_amount (baru) atau pakasir_total_payment (lama) harus ada
        and (o.get("pakasir_amount") or o.get("pakasir_total_payment"))
    ]
    return sorted(result, key=lambda x: x["created_at"])


def get_order_by_pakasir_order_id(order_id: str) -> Optional[Dict[str, Any]]:
    """Cari order berdasarkan ID pesanan internal (digunakan webhook Pakasir)."""
    return load_orders().get(order_id)


# =============================================================
# PROMO  (per-produk: beli X akun → harga Y per akun)
# =============================================================


def get_promo(product_id: str) -> Dict[str, Any]:
    """Ambil konfigurasi promo untuk SATU produk.

    Promo disimpan di dalam produk (field 'promo').
    Return dict: {"min_qty": int, "promo_price": int}
        min_qty == 0 → promo nonaktif untuk produk ini.
    """
    product = get_product(product_id) or {}
    promo = product.get("promo", {})
    if not isinstance(promo, dict):
        promo = {}
    return {
        "min_qty": int(promo.get("min_qty", 0) or 0),
        "promo_price": int(promo.get("promo_price", 0) or 0),
    }


def set_promo(product_id: str, min_qty: int, promo_price: int) -> bool:
    """Set promo untuk SATU produk. min_qty=0 menonaktifkan promo.
    Return False bila produk tidak ditemukan."""
    with _lock:
        products = load_products()
        if product_id not in products:
            return False
        products[product_id]["promo"] = {
            "min_qty": int(min_qty),
            "promo_price": int(promo_price),
        }
        _save_products(products)
        return True


def calc_promo_total(product_id: str, product_price: int, quantity: int) -> int:
    """Hitung total harga dengan promo volume discount produk ini.

    Jika promo produk aktif dan quantity >= min_qty → pakai promo_price per unit.
    Jika tidak → pakai product_price normal. Kembalikan total harga.
    """
    promo = get_promo(product_id)
    min_qty = promo["min_qty"]
    promo_price = promo["promo_price"]
    if min_qty > 0 and promo_price > 0 and quantity >= min_qty:
        return promo_price * quantity
    return product_price * quantity


# =============================================================
# USERS  (untuk broadcast — semua user yang pernah memakai bot)
# =============================================================

USERS_FILE = os.path.join(_BASE, "..", "data", "users.json")


def _load_users() -> Dict[str, Any]:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_users(data: Dict[str, Any]) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def track_user(user_id: int, username: str = "", first_name: str = "") -> None:
    """Catat user yang berinteraksi dengan bot (untuk fitur broadcast).

    Idempotent: memperbarui username/first_name & last_seen jika sudah ada.
    PENTING: jangan menimpa field 'balance' yang sudah ada.
    """
    if not user_id:
        return
    with _lock:
        users = _load_users()
        key = str(user_id)
        now = datetime.now().isoformat()
        existing = users.get(key, {})
        users[key] = {
            "user_id": user_id,
            "username": username or existing.get("username", ""),
            "first_name": first_name or existing.get("first_name", ""),
            "first_seen": existing.get("first_seen", now),
            "last_seen": now,
            # Jaga saldo agar tidak hilang saat tracking diperbarui
            "balance": int(existing.get("balance", 0)),
        }
        _save_users(users)


# =============================================================
# SALDO LOCAL (balance per user, disimpan di users.json)
# =============================================================


def get_balance(user_id: int) -> int:
    """Ambil saldo lokal user (Rupiah). Default 0."""
    users = _load_users()
    u = users.get(str(user_id))
    if not u:
        return 0
    return int(u.get("balance", 0) or 0)


def add_balance(user_id: int, amount: int, username: str = "") -> int:
    """Tambah (atau kurangi bila negatif) saldo user. Kembalikan saldo baru.

    Membuat entri user bila belum ada.
    """
    with _lock:
        users = _load_users()
        key = str(user_id)
        now = datetime.now().isoformat()
        u = users.get(key, {})
        new_balance = int(u.get("balance", 0) or 0) + int(amount)
        if new_balance < 0:
            new_balance = 0
        users[key] = {
            "user_id": user_id,
            "username": username or u.get("username", ""),
            "first_name": u.get("first_name", ""),
            "first_seen": u.get("first_seen", now),
            "last_seen": u.get("last_seen", now),
            "balance": new_balance,
        }
        _save_users(users)
        return new_balance


def deduct_balance(user_id: int, amount: int) -> bool:
    """Kurangi saldo bila cukup. Return True bila berhasil, False bila saldo kurang.
    Operasi atomik (lock) untuk mencegah saldo minus akibat race condition.
    """
    amount = int(amount)
    if amount <= 0:
        return True
    with _lock:
        users = _load_users()
        key = str(user_id)
        u = users.get(key)
        if not u:
            return False
        current = int(u.get("balance", 0) or 0)
        if current < amount:
            return False
        u["balance"] = current - amount
        u["last_seen"] = datetime.now().isoformat()
        users[key] = u
        _save_users(users)
        return True


def set_balance(user_id: int, amount: int, username: str = "") -> int:
    """Set saldo user ke nilai tertentu (untuk admin). Kembalikan saldo baru."""
    with _lock:
        users = _load_users()
        key = str(user_id)
        now = datetime.now().isoformat()
        u = users.get(key, {})
        users[key] = {
            "user_id": user_id,
            "username": username or u.get("username", ""),
            "first_name": u.get("first_name", ""),
            "first_seen": u.get("first_seen", now),
            "last_seen": u.get("last_seen", now),
            "balance": max(0, int(amount)),
        }
        _save_users(users)
        return users[key]["balance"]


def find_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Cari user berdasarkan username (tanpa '@', case-insensitive)."""
    uname = (username or "").lstrip("@").strip().lower()
    if not uname:
        return None
    for u in _load_users().values():
        if (u.get("username") or "").lower() == uname:
            return u
    return None


def get_all_user_ids() -> List[int]:
    """Kembalikan daftar semua user_id yang pernah memakai bot.

    Menggabungkan dua sumber:
      1. users.json  — user yang memakai bot setelah fitur tracking ditambahkan
      2. orders.json — semua user yang pernah membuat pesanan (termasuk user lama)
    """
    from_users = {u["user_id"] for u in _load_users().values() if u.get("user_id")}
    from_orders = {
        o["user_id"]
        for o in load_orders().values()
        if o.get("user_id")
    }
    combined = from_users | from_orders
    return list(combined)


def get_user_count() -> int:
    """Jumlah user unik yang pernah memakai bot (gabungan users.json + orders.json)."""
    return len(get_all_user_ids())
