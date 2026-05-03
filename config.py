# config.py
"""
╔══════════════════════════════════════════════════════╗
║         SHOPEE WAR BOT — PENGATURAN UTAMA            ║
║  Edit file ini sebelum menjalankan main.py           ║
╚══════════════════════════════════════════════════════╝
"""
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════════
#  1. WAKTU FLASH SALE (UTC)
#     UTC = WIB - 7 jam  |  UTC = WITA - 8 jam  |  UTC = WIT - 9 jam
#     flash sale WIB 12:00 → HOUR=5, MINUTE=0, SECOND=0
#     flash sale WIB 18:00 → HOUR=11, MINUTE=0, SECOND=0
#     flash sale WIB 20:00 → HOUR=13, MINUTE=0, SECOND=0
# ══════════════════════════════════════════════════════════════════════════════
FLASH_SALE_HOUR   = 20      # ← TEST: 20:55 UTC = 03:55 WIB
FLASH_SALE_MINUTE = 55
FLASH_SALE_SECOND = 0
FLASH_SALE_USEC   : int = 0

# ══════════════════════════════════════════════════════════════════════════════
#  2. STRATEGI CHECKOUT
#     "dynamic" → tunggu T-0, observer deteksi tombol aktif (DIREKOMENDASIKAN)
#     "refresh"  → reload halaman 2 detik sebelum T-0 (fallback)
# ══════════════════════════════════════════════════════════════════════════════
CHECKOUT_MODE      : str = "dynamic"
PRE_RELOAD_LEAD_MS : int = 2000   # hanya dipakai jika CHECKOUT_MODE = "refresh"

# ══════════════════════════════════════════════════════════════════════════════
#  3. URL
# ══════════════════════════════════════════════════════════════════════════════
SHOPEE_CART_URL      = "https://shopee.co.id/cart"
CHECKOUT_URL_PATTERN = "**/checkout**"

# ══════════════════════════════════════════════════════════════════════════════
#  4. SELECTOR
# ══════════════════════════════════════════════════════════════════════════════
CHECKOUT_BTN_SELECTOR      = "button.shopee-button-solid.shopee-button-solid--primary"
CHECKOUT_SKELETON_SELECTOR : str | None = None
CONFIRM_BTN_SELECTOR       = "button.stardust-button.stardust-button--primary"

# ══════════════════════════════════════════════════════════════════════════════
#  5. BROWSER
# ══════════════════════════════════════════════════════════════════════════════
USER_DATA_DIR = Path("./browser_profile")
HEADLESS      = False

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-gpu",
]

# ══════════════════════════════════════════════════════════════════════════════
#  6. NTP
# ══════════════════════════════════════════════════════════════════════════════
NTP_HOST         = "pool.ntp.org"
NTP_SAMPLE_COUNT = 8
NTP_TIMEOUT_SEC  = 3.0

MAX_OFFSET_MS  = 2000.0
MAX_STD_DEV_MS =   50.0
MAX_STRATUM    =    3
MAX_RTT_MS     =  300.0

# ══════════════════════════════════════════════════════════════════════════════
#  7. TIMEOUT (milidetik)
# ══════════════════════════════════════════════════════════════════════════════
NAV_TIMEOUT_MS      = 15_000
SKELETON_TIMEOUT_MS =  5_000
CONFIRM_TIMEOUT_MS  = 25_000
OBSERVER_TIMEOUT_MS = 30_000
TOTAL_BUDGET_MS     = 60_000

# ══════════════════════════════════════════════════════════════════════════════
#  8. RETRY
# ══════════════════════════════════════════════════════════════════════════════
CONFIRM_CLICK_RETRIES = 2
RETRY_DELAY_MS        = 800.0
POST_CLICK_HOLD_MS    = 15000   # tahan browser 15s — biar bisa lihat hasil klik

# ══════════════════════════════════════════════════════════════════════════════
#  9. MOUSE (hardware click)
#     [OPTIMASI] Nilai diturunkan untuk mempercepat klik konfirmasi:
#     MOUSE_MOVE_STEPS    : 8  → 3   (hemat ~40ms)
#     MOUSE_MOVE_DELAY_MS : 8  → 2   (hemat ~48ms)
#     STABILITY_WAIT_MS   : 60 → 20  (hemat ~40ms)
#     Total penghematan   : ~130ms → target klik < 200ms
# ══════════════════════════════════════════════════════════════════════════════
MOUSE_MOVE_STEPS    = 3     # [OPTIMASI] sebelumnya 8
MOUSE_MOVE_DELAY_MS = 2.0   # [OPTIMASI] sebelumnya 8.0 ms
STABILITY_WAIT_MS   = 5.0   # [OPTIMASI] sebelumnya 20.0 ms — hanya kena kalau scroll

# ── verify isTrusted setelah klik konfirmasi ──────────────────────────────
# True  → tunggu event listener resolve (adds ~200-300ms)
# False → skip verifikasi, langsung return setelah klik (DIREKOMENDASIKAN)
VERIFY_TRUSTED      = False

# ══════════════════════════════════════════════════════════════════════════════
#  10. TWO-PHASE WAITER
# ══════════════════════════════════════════════════════════════════════════════
COARSE_SLEEP_SEC   = 0.5
FINE_THRESHOLD_SEC = 0.05