# 🎓 GHS Store Bot

Bot Telegram untuk menjual produk **GitHub Education (GHS)** dengan dua pilihan produk dan sistem manajemen pesanan otomatis.

---

## 📦 Fitur

### Untuk Pembeli
- 🛒 Lihat katalog produk dengan stok real-time
- 📋 Detail produk lengkap (deskripsi, harga, stok)
- 💳 Pembayaran otomatis via **QRIS / E-Wallet** (RonzzPay) atau manual transfer bank
- 📂 Riwayat pesanan personal
- ✅ Pengiriman akun otomatis setelah pembayaran dikonfirmasi

### Untuk Admin
- 📦 Kelola stok produk (tambah akun, ubah harga)
- 🔔 Notifikasi real-time setiap ada pembayaran masuk
- ✅ Konfirmasi / ❌ tolak pembayaran langsung dari chat
- 📜 Lihat semua pesanan
- 📊 Statistik toko (pendapatan, stok, saldo RonzzPay)
- 👥 Multi-admin support

### 💸 Payment Gateway (RonzzPay)
- 🔲 Auto-generate QRIS untuk setiap pembelian
- ⚡ Konfirmasi pembayaran otomatis via webhook
- 🏦 Mendukung DANA, OVO, GOPAY, dan lainnya
- 🧪 Mode sandbox untuk testing tanpa uang asli
- 📱 Fallback ke transfer bank manual jika RonzzPay nonaktif

---

## 🛒 Produk

| Produk | Deskripsi | Harga Default |
|--------|-----------|---------------|
| 🎓 **GHS Only DO** | GitHub Education + DigitalOcean Credit $200 (belum terpakai) | Rp 75.000 |
| ♻️ **GHS Bekas DO** | GitHub Education, DO Credit sudah dipakai, harga lebih murah | Rp 35.000 |

> Harga dapat diubah sewaktu-waktu melalui admin panel.

---

## 🚀 Instalasi

### 1. Clone / Download Project

```bash
cd github/bot
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Buat File `.env`

Salin file contoh lalu isi dengan data kamu:

```bash
cp .env.example .env
```

Edit file `.env`:

```env
BOT_TOKEN=token_dari_botfather
ADMIN_IDS=123456789

STORE_NAME=🎓 GHS Store

# Pembayaran manual (fallback)
PAYMENT_BANK=Dana
PAYMENT_ACCOUNT=088740404712
PAYMENT_NAME=Ahmad Hasan Baihaqi

# RonzzPay (opsional - kosongkan untuk nonaktifkan)
RONZZPAY_API_KEY=your_api_key_here
RONZZPAY_SANDBOX=true
RONZZPAY_DEFAULT_METHOD=qris
WEBHOOK_PORT=8080
```

### 4. Jalankan Bot

```bash
python main.py
```

---

## ⚙️ Konfigurasi `.env`

| Variable | Wajib | Keterangan |
|----------|-------|------------|
| `BOT_TOKEN` | ✅ | Token bot dari [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | ✅ | User ID Telegram admin, pisah koma jika lebih dari satu |
| `STORE_NAME` | ❌ | Nama toko yang tampil di bot (default: `🎓 GHS Store`) |
| `PAYMENT_BANK` | ❌ | Nama bank tujuan transfer (fallback manual) |
| `PAYMENT_ACCOUNT` | ❌ | Nomor rekening tujuan |
| `PAYMENT_NAME` | ❌ | Nama pemilik rekening |
| `PAYMENT_QRIS` | ❌ | Kode/link QRIS manual (opsional) |
| `RONZZPAY_API_KEY` | ❌ | API Key RonzzPay (kosongkan untuk nonaktifkan) |
| `RONZZPAY_SANDBOX` | ❌ | Mode sandbox (default: `true`) |
| `RONZZPAY_DEFAULT_METHOD` | ❌ | Metode default: `qris`, `dana`, `ovo`, `gopay` |
| `WEBHOOK_PORT` | ❌ | Port webhook server (default: `8080`) |
| `WEBHOOK_HOST` | ❌ | Host webhook (default: `0.0.0.0`) |

> **Cara cek User ID Telegram:** Chat dengan [@userinfobot](https://t.me/userinfobot)

---

## 📁 Struktur Project

```
bot/
├── main.py                  # Entry point bot + webhook
├── config.py                # Konfigurasi dari .env
├── requirements.txt         # Dependensi Python
├── .env                     # Environment variables (buat sendiri)
├── .env.example             # Template .env
├── README.md
│
├── handlers/
│   ├── __init__.py
│   ├── user.py              # Handler user (beli, pesanan, info)
│   ├── admin.py             # Handler admin (stok, konfirmasi, statistik)
│   └── do_claim.py          # Auto-claim DigitalOcean credit
│
├── payment/
│   ├── __init__.py
│   └── ronzzpay.py          # RonzzPay API client
│
├── webhook/
│   ├── __init__.py
│   └── server.py            # Webhook HTTP server (aiohttp)
│
├── database/
│   ├── __init__.py
│   └── db.py                # Operasi baca/tulis JSON
│
└── data/
    ├── products.json         # Data produk & stok akun
    └── orders.json           # Data semua pesanan
```

---

## 🔄 Alur Pembelian

### Dengan RonzzPay (Otomatis) ⚡

```
User /start → Pilih Produk → Beli Sekarang
    │
    ▼
Bot generate QRIS via RonzzPay API
    │
    ▼
User scan & bayar QRIS
    │
    ▼
RonzzPay webhook → transaction.success
    │
    ▼
Bot auto-confirm → Akun dikirim otomatis ✅
```

### Manual (Fallback)

```
User /start → Pilih Produk → Beli Sekarang
    │
    ▼
Info transfer bank (nomor rekening)
    │
    ▼
User transfer & kirim screenshot
    │
    ▼
Admin konfirmasi → Akun dikirim ✅
```

---

## 🎓 Cara Klaim DigitalOcean Credit $200

> **Khusus produk "GHS Only DO"** — membutuhkan akun DigitalOcean BARU

Credit $200 DigitalOcean dari GitHub Education **hanya berlaku untuk akun DO yang baru dibuat**.
Bot mengotomasi bagian yang susah (login GitHub Education dan mendapatkan link), sisanya kamu selesaikan sendiri.

### 🤖 Bagian yang Diotomasi Bot:
1. Login ke GitHub sebagai akun GHS yang kamu beli
2. Navigasi ke GitHub Education Pack → klik offer DigitalOcean
3. Ikuti redirect ke halaman signup DigitalOcean dengan credit aktif
4. Kirim link signup khusus tersebut ke kamu beserta instruksi

### 👤 Bagian yang Kamu Lakukan Sendiri:
1. Buka link yang diberikan bot di browser
2. Daftar akun DigitalOcean **BARU** (gunakan email & password pilihanmu sendiri)
3. Tambahkan metode pembayaran (kartu kredit/debit/PayPal)
   - **Wajib untuk verifikasi akun** — kamu tidak akan ditagih selama credit $200 masih ada
4. Credit $200 otomatis aktif ✅

### ⚠️ Yang Perlu Diketahui:
- Credit hanya untuk akun DO **baru** (bukan akun yang sudah pernah dibuat sebelumnya)
- Credit berlaku **1 tahun** sejak akun dibuat
- Bot tidak menyimpan data pembayaran kamu (otomasi berhenti sebelum step pembayaran)
- Jika otomasi gagal (GitHub detect bot, CAPTCHA, dll.), tersedia panduan klaim manual step-by-step

### Diagram Alur DO Claim

```
Klik "Klaim DO Credit"
        │
        ▼
Bot login GitHub sebagai akun GHS
        │
        ▼
Navigasi ke education.github.com/pack → klik offer DO
        │
        ▼
GitHub Education verifikasi status student
        │
        ▼
Redirect ke halaman signup DigitalOcean
        │
        ▼
Bot kirim link + screenshot ke kamu
        │
        ▼
Kamu daftar akun DO baru di link itu (manual)
        │
        ▼
Tambah metode pembayaran (manual)
        │
        ▼
Credit $200 aktif ✅
```

### Batasan Teknis

| Yang Diotomasi | Yang Tidak Diotomasi |
|---|---|
| Login GitHub Education (+ 2FA TOTP) | Pendaftaran akun DO (email/password) |
| Navigasi ke offer DigitalOcean | Penambahan metode pembayaran |
| Mendapatkan link signup dengan credit | Verifikasi email akun DO baru |
| Screenshot hasil halaman DO | Penyelesaian KYC/verifikasi tambahan |

---

## 📊 Status Pesanan

| Status | Keterangan |
|--------|------------|
| ⏳ `pending_payment` | Pesanan dibuat, menunggu user transfer |
| 📤 `payment_sent` | User sudah kirim bukti, menunggu konfirmasi admin |
| 💚 `paid` | Dibayar otomatis via RonzzPay, menunggu stok |
| ✅ `confirmed` | Akun sudah dikirim ke user |
| ❌ `rejected` | Admin tolak pembayaran |
| 🚫 `cancelled` | Pesanan dibatalkan oleh user |

---

## 📦 Cara Tambah Stok

1. Ketik `/admin` di Telegram
2. Klik **📦 Kelola Stok**
3. Klik **➕ Tambah Stok** pada produk yang ingin diisi
4. Kirim daftar akun, satu per baris:

```
email@gmail.com:Password123
email2@gmail.com:Password456
user3@outlook.com:Password789
```

Bot akan otomatis menyimpan dan memperbarui jumlah stok.

---

## 💲 Cara Ubah Harga

1. Ketik `/admin`
2. Klik **📦 Kelola Stok**
3. Klik **💲 Ubah Harga** pada produk yang ingin diubah
4. Kirim angka harga baru (contoh: `50000`)

---

## 🔒 Keamanan

- File `data/products.json` berisi **akun yang belum terjual** — jaga kerahasiaannya!
- File `.env` berisi token bot & info pembayaran — **jangan di-commit ke git!**
- Tambahkan ke `.gitignore`:
  ```
  .env
  data/orders.json
  data/products.json
  ```

---

## 📝 Dependensi

```
python-telegram-bot==20.7   # Framework bot Telegram (async)
python-dotenv==1.0.0        # Load konfigurasi dari .env
requests>=2.31.0            # HTTP client (RonzzPay API)
aiohttp>=3.9.0              # Async HTTP server (webhook)
playwright==1.58.0          # Browser automation (DO claim)
```

Membutuhkan **Python 3.10+**.

---

## 🔜 Roadmap

- [x] Integrasi payment gateway (RonzzPay)
- [ ] Broadcast pesan ke semua user
- [ ] Sistem voucher / diskon
- [ ] Export laporan pesanan ke CSV
- [ ] Database SQLite/PostgreSQL untuk skala lebih besar

---

## 📄 Lisensi

MIT License — bebas digunakan dan dimodifikasi.
