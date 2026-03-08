# SMC Signal Bot 🤖

Bot trading signal berbasis **Smart Money Concepts (SMC)** yang scan pasar crypto 24/7 dan mengirim alert ke Telegram ketika ada setup trading yang valid.

> ⚠️ **DISCLAIMER**: Bot ini adalah **tools analisa**, bukan financial advice. Selalu gunakan risk management dan lakukan analisa mandiri sebelum trading. Pengembang tidak bertanggung jawab atas kerugian trading.

---

## Fitur

- **Scan otomatis** BTC/USDT, ETH/USDT, SOL/USDT setiap 15 menit
- **Deteksi SMC** lengkap: Order Blocks, Fair Value Gaps, Liquidity Pools, Market Structure
- **Multi-timeframe analysis**: Daily + 4H untuk bias, 1H + 15m untuk konfirmasi entry
- **Confluence scoring** 1-5 bintang — hanya alert jika score ≥ 3
- **Risk management**: validasi Risk/Reward minimum 1:2
- **Alert Telegram** dengan detail lengkap: entry zone, SL, TP1, TP2, RR, alasan
- **Market update** setiap 4 jam: trend semua pair + key levels

---

## Prerequisites

- Python **3.10** atau lebih baru
- Akun Telegram (untuk membuat bot)
- Koneksi internet (data diambil dari Bybit public API — tidak perlu API key)

---

## Cara Buat Telegram Bot

### Langkah 1: Buat Bot Baru
1. Buka aplikasi Telegram
2. Cari **@BotFather** di search bar
3. Klik Start / kirim `/start`
4. Kirim perintah `/newbot`
5. Masukkan nama bot (contoh: `SMC Signal Bot`)
6. Masukkan username bot — harus diakhiri `bot` (contoh: `smc_signal_bot`)
7. BotFather akan memberikan **API Token** — simpan token ini!

### Langkah 2: Buat Channel/Group
1. Buat channel atau group baru di Telegram
2. Tambahkan bot kamu sebagai **admin** di channel/group tersebut
3. Kirim satu pesan di channel/group (agar bot bisa detect)

### Langkah 3: Dapatkan Chat ID
**Cara 1 — Pakai @userinfobot:**
1. Cari `@userinfobot` di Telegram
2. Kirim `/start` — bot akan balas dengan ID kamu
3. Untuk channel/group: forward pesan dari channel ke `@userinfobot`

**Cara 2 — Pakai API Telegram:**
1. Buka browser, masukkan URL:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
2. Kirim pesan ke channel/group kamu
3. Refresh halaman — cari `"chat": {"id": -XXXXXXXXX}` dalam JSON response
4. Angka tersebut adalah Chat ID kamu (biasanya negatif untuk group/channel)

---

## Instalasi

### 1. Clone / Download Project
```bash
# Jika pakai git
git clone <repo-url>
cd smc-signal-bot

# Atau extract ZIP dan masuk ke folder smc-signal-bot
cd smc-signal-bot
```

### 2. Buat Virtual Environment (Direkomendasikan)
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux / macOS
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Setup File .env
```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Buka file `.env` dengan text editor dan isi nilainya:

```env
# Wajib diisi:
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_CHAT_ID=-1001234567890

# Opsional (sudah ada default):
TRADING_PAIRS=BTC/USDT,ETH/USDT,SOL/USDT
SCAN_INTERVAL_MINUTES=15
MARKET_UPDATE_HOURS=4
DEFAULT_CAPITAL=1000000
RISK_PER_TRADE_PERCENT=1
MAX_LEVERAGE=3
MIN_RR_RATIO=2
MIN_CONFIDENCE_SCORE=3
TIMEZONE=Asia/Jakarta
```

---

## Penjelasan Konfigurasi

| Variable | Default | Keterangan |
|----------|---------|-----------|
| `TELEGRAM_BOT_TOKEN` | — | Token dari @BotFather (wajib) |
| `TELEGRAM_CHAT_ID` | — | ID channel/group tujuan alert (wajib) |
| `TRADING_PAIRS` | BTC,ETH,SOL | Pair yang di-scan (pisah koma) |
| `SCAN_INTERVAL_MINUTES` | 15 | Frekuensi scan dalam menit |
| `MARKET_UPDATE_HOURS` | 4 | Interval market update dalam jam |
| `DEFAULT_CAPITAL` | 1000000 | Modal dalam USDT (untuk hitung position size) |
| `RISK_PER_TRADE_PERCENT` | 1 | Persentase risiko per trade |
| `MAX_LEVERAGE` | 3 | Maksimum leverage |
| `MIN_RR_RATIO` | 2 | Minimum Risk/Reward ratio (1:2) |
| `MIN_CONFIDENCE_SCORE` | 3 | Minimum confluence score (1-5) |
| `TIMEZONE` | Asia/Jakarta | Timezone untuk tampilan waktu |

---

## Menjalankan Bot

```bash
# Pastikan virtual environment aktif
python main.py
```

Bot akan:
1. Memvalidasi konfigurasi .env
2. Mengirim pesan startup ke Telegram
3. Langsung menjalankan scan pertama
4. Menjalankan scan setiap 15 menit (atau sesuai config)
5. Mengirim market update setiap 4 jam

**Untuk stop bot:** tekan `Ctrl+C`

---

## Cara Membaca Signal Alert

Contoh alert yang dikirim ke Telegram:

```
🔴 SHORT SETUP - SOL/USDT
━━━━━━━━━━━━━━━━━━━━
📊 Timeframe: 4H → 1H konfirmasi
📈 Trend HTF: 1D: Bearish | 4H: Bearish

🎯 ENTRY ZONE: $87.00 - $88.00
🛑 STOP LOSS: $89.50
✅ TP1: $83.00 (partial close 50%)
✅ TP2: $78.00 (let it run)

📐 Risk/Reward: 1:2.6
💪 Confidence: ⭐⭐⭐⭐ (4/5)

📋 ALASAN:
• Trend Bearish di Daily & 4H
• Harga di Premium Zone (72% dari range)
• Harga masuk ke Bearish Order Block 4H
• FVG overlap dengan Order Block (high confluence)

⚠️ KONFIRMASI DULU:
Tunggu CHoCH bearish di 15m sebelum entry.
Jangan entry tanpa konfirmasi!
```

### Penjelasan Setiap Bagian:

| Bagian | Penjelasan |
|--------|-----------|
| 🔴/🟢 SHORT/LONG | Arah trade — merah=short, hijau=long |
| Timeframe | TF analisa utama dan TF konfirmasi |
| Trend HTF | Trend di Daily dan 4H (Higher Timeframe) |
| ENTRY ZONE | Range harga ideal untuk entry |
| STOP LOSS | Level invalidasi setup |
| TP1 | Target pertama — close 50% posisi di sini |
| TP2 | Target akhir — sisa posisi dibiarkan jalan |
| Risk/Reward | Perbandingan risiko vs reward (min 1:2) |
| Confidence | Skor confluence 1-5 bintang |
| ALASAN | Faktor SMC yang mendukung setup |
| KONFIRMASI | Tunggu CHoCH di 15m sebelum entry! |

### Cara Entry yang Benar:
1. Terima alert signal dari bot
2. Buka chart di exchange kamu (timeframe 15m)
3. **Tunggu CHoCH** (Change of Character) terkonfirmasi di 15m
4. Entry setelah konfirmasi, bukan langsung saat alert masuk
5. Set SL dan TP sesuai yang tertera di alert
6. Close 50% posisi di TP1, biarkan sisa jalan ke TP2

---

## Menjalankan Unit Tests

```bash
# Install test dependencies (sudah ada di requirements.txt)
pip install pytest pytest-asyncio

# Jalankan semua tests
pytest tests/ -v

# Jalankan test spesifik
pytest tests/test_market_structure.py -v
```

---

## Tips Kustomisasi

- **Tambah pair baru**: edit `TRADING_PAIRS` di `.env`
- **Lebih banyak signal**: kurangi `MIN_CONFIDENCE_SCORE` ke 2 (lebih agresif)
- **Lebih selektif**: naikkan `MIN_CONFIDENCE_SCORE` ke 4 atau 5
- **Scan lebih sering**: kurangi `SCAN_INTERVAL_MINUTES` ke 5
- **Ubah risk per trade**: edit `RISK_PER_TRADE_PERCENT`

---

## Struktur Project

```
smc-signal-bot/
├── .env.example          # Template konfigurasi
├── .gitignore
├── requirements.txt
├── README.md
├── main.py               # Entry point — jalankan ini
├── config/
│   └── settings.py       # Load .env dan konstanta
├── core/
│   ├── data_fetcher.py   # Fetch data Bybit via ccxt
│   ├── market_structure.py  # Deteksi BOS, CHoCH, trend
│   ├── order_blocks.py   # Deteksi Order Block
│   ├── fvg.py            # Deteksi Fair Value Gap
│   ├── liquidity.py      # Deteksi EQH/EQL, Weak High/Low
│   ├── premium_discount.py  # Zona premium/discount
│   ├── entry_engine.py   # Otak utama — scoring & setup
│   └── risk_manager.py   # Hitung RR dan position size
├── alerts/
│   └── telegram_alert.py # Format & kirim alert Telegram
├── utils/
│   └── logger.py         # Logging ke console dan file
├── logs/                 # Log files (dibuat otomatis)
└── tests/                # Unit tests
```

---

## Troubleshooting

**Bot tidak mengirim pesan:**
- Periksa `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` di `.env`
- Pastikan bot sudah di-invite ke channel/group sebagai admin
- Cek file `logs/smc_bot.log` untuk error

**Error saat install:**
- Pastikan Python 3.10+: `python --version`
- Coba upgrade pip: `pip install --upgrade pip`
- Di Windows, mungkin perlu: `pip install --upgrade setuptools wheel`

**Terlalu banyak/sedikit signal:**
- Sesuaikan `MIN_CONFIDENCE_SCORE` di `.env`
- Sesuaikan `MIN_RR_RATIO` (default 2 = minimum RR 1:2)

**Error `ccxt` / data tidak dapat diambil:**
- Periksa koneksi internet
- Bybit mungkin sedang maintenance — cek status.bybit.com
- Cek log untuk detail error: `logs/smc_bot.log`
