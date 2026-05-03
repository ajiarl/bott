# tests/test_runner.py
"""
Test Runner — Shopee KW Mock Server

Menjalankan server HTTP lokal yang menyajikan cart.html dan checkout.html,
lalu menjalankan bot dengan config yang di-override otomatis.

Cara pakai:
    cd shopee_war
    python tests/test_runner.py

    # Pilih mode:
    python tests/test_runner.py --mode disabled --delay 5   (default)
    python tests/test_runner.py --mode aria     --delay 3
    python tests/test_runner.py --mode both     --delay 8

Flag:
    --mode    : 'disabled' | 'aria' | 'both'  (jenis disable tombol)
    --delay   : detik sebelum tombol aktif (default: 5)
    --port    : port HTTP server (default: 8080)
    --headless: jalankan browser headless
"""

import sys
import os
import time
import asyncio
import logging
import argparse
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Pastikan root project ada di sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent

# ─────────────────────────────────────────────────────────────────────────────
#  PARSE ARGUMEN
# ─────────────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Shopee War Bot — Local Test")
parser.add_argument("--mode",     default="disabled", choices=["disabled", "aria", "both"])
parser.add_argument("--delay",    type=int,   default=5,    help="Detik sebelum tombol aktif")
parser.add_argument("--port",     type=int,   default=8080, help="Port server lokal")
parser.add_argument("--headless", action="store_true",      help="Jalankan headless")
ARGS = parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
#  OVERRIDE CONFIG (harus dilakukan SEBELUM import modul lain)
# ─────────────────────────────────────────────────────────────────────────────

import config as cfg

BASE_URL = f"http://localhost:{ARGS.port}"

# URL lokal
cfg.SHOPEE_CART_URL      = f"{BASE_URL}/cart?mode={ARGS.mode}&delay={ARGS.delay}"
cfg.CHECKOUT_URL_PATTERN = f"**/checkout**"

# Selector sama dengan yang ada di HTML kita
cfg.CHECKOUT_BTN_SELECTOR      = "button.shopee-button-solid.shopee-button-solid--primary"
cfg.CONFIRM_BTN_SELECTOR       = "button.shopee-button-solid.shopee-button-solid--primary"
cfg.CHECKOUT_SKELETON_SELECTOR = None   # Page B kita tidak pakai skeleton selector

# ── FLASH_SALE_TIME sengaja tidak diset di sini ───────────────────────────
# Dihitung di dalam run_test() SETELAH NTP selesai, supaya tidak keburu
# kadaluarsa jika NTP makan waktu lama karena sample gagal/timeout.

cfg.CHECKOUT_MODE = "dynamic"      # cart.html selalu dynamic mode
cfg.HEADLESS      = ARGS.headless

# Longgarkan threshold NTP untuk test lokal
cfg.MAX_OFFSET_MS    = 2000.0
cfg.MAX_STD_DEV_MS   =  100.0
cfg.MAX_STRATUM      =    5
cfg.MAX_RTT_MS       =  500.0

# Timeout lebih longgar untuk mesin lokal
cfg.NAV_TIMEOUT_MS      = 20_000
cfg.SKELETON_TIMEOUT_MS =  5_000
cfg.CONFIRM_TIMEOUT_MS  = 30_000   # checkout.html delay 1500ms + buffer
cfg.OBSERVER_TIMEOUT_MS = 20_000
cfg.TOTAL_BUDGET_MS     = 90_000

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s.%(msecs)03d  %(levelname)s  %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  HTTP SERVER LOKAL
#  Melayani cart.html dan checkout.html dari folder tests/
# ─────────────────────────────────────────────────────────────────────────────

class _ShopeeKWHandler(BaseHTTPRequestHandler):
    """Handler minimal — routing /cart dan /checkout ke file HTML."""

    def log_message(self, format, *args):
        log.debug("  [server] %s", format % args)

    def do_GET(self):
        path = self.path.split("?")[0]   # buang query string untuk routing

        if path in ("/", "/cart"):
            self._serve_file(TESTS_DIR / "cart.html", "text/html")
        elif path == "/checkout":
            self._serve_file(TESTS_DIR / "checkout.html", "text/html")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def _serve_file(self, filepath: Path, content_type: str) -> None:
        try:
            data = filepath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(f"File tidak ditemukan: {filepath}".encode())


def start_server(port: int) -> HTTPServer:
    """Mulai HTTP server di thread terpisah. Kembalikan instance server."""
    server = HTTPServer(("localhost", port), _ShopeeKWHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Server lokal berjalan di http://localhost:%d", port)
    log.info("  /cart     → %s", TESTS_DIR / "cart.html")
    log.info("  /checkout → %s", TESTS_DIR / "checkout.html")
    return server


# ─────────────────────────────────────────────────────────────────────────────
#  TEST RUNNER
# ─────────────────────────────────────────────────────────────────────────────

async def run_test() -> None:
    from ntp_sync        import calibrate_ntp, true_time, build_target_ts, async_wait_until, ClockSync
    from browser_engine  import open_browser, close_browser
    from observer_engine import inject_checkout_observer
    from api_checkout    import fire_checkout_page_b

    log.info("═" * 62)
    log.info("  SHOPEE WAR BOT — LOCAL TEST")
    log.info("═" * 62)
    log.info("  Mode disable    : %s", ARGS.mode)
    log.info("  Delay aktif     : %s detik", ARGS.delay)
    log.info("═" * 62)

    # ── [1/5] NTP ─────────────────────────────────────────────────────────
    log.info("[1/5] Kalibrasi NTP …")
    try:
        sync = calibrate_ntp()
    except Exception as exc:
        log.warning("NTP gagal: %s — lanjut dengan sync dummy.", exc)
        sync = ClockSync(
            offset_sec=0.0, rtt_sec=0.01, stratum=1,
            server_ip="127.0.0.1", sample_count=1, offset_std_dev=0.0,
        )

    # ── Hitung target SETELAH NTP selesai ────────────────────────────────
    # FIX: dulu dihitung sebelum NTP di level modul → target keburu lewat
    # kalau NTP makan waktu > delay detik (sample timeout 3s × 3 gagal = +9s).
    # Sekarang: ambil waktu NTP-corrected saat ini, baru tambah buffer.
    _now_dt = datetime.datetime.utcfromtimestamp(true_time(sync))
    _target_dt = _now_dt + datetime.timedelta(seconds=ARGS.delay + 2)

    cfg.FLASH_SALE_HOUR   = _target_dt.hour
    cfg.FLASH_SALE_MINUTE = _target_dt.minute
    cfg.FLASH_SALE_SECOND = _target_dt.second
    cfg.FLASH_SALE_USEC   = _target_dt.microsecond

    target_unix = build_target_ts(
        hour        = cfg.FLASH_SALE_HOUR,
        minute      = cfg.FLASH_SALE_MINUTE,
        second      = cfg.FLASH_SALE_SECOND,
        microsecond = cfg.FLASH_SALE_USEC,
        use_utc     = True,
    )

    log.info("  Flash sale time : %s UTC  (dalam %.1f s)",
             _target_dt.strftime("%H:%M:%S"),
             target_unix - true_time(sync))
    log.info("  Cart URL        : %s", cfg.SHOPEE_CART_URL)
    log.info("[1/5] ✓ NTP siap.")

    # ── [2/5] Buka browser ────────────────────────────────────────────────
    log.info("[2/5] Buka browser → %s", cfg.SHOPEE_CART_URL)
    playwright, context, page = await open_browser(cfg.SHOPEE_CART_URL)
    log.info("[2/5] ✓ Browser siap.")

    # ── [3/5] Timing wait ─────────────────────────────────────────────────
    remaining = target_unix - true_time(sync)
    log.info("[3/5] Menunggu %.2f detik sampai T-0 …", remaining)
    delta_ms = await async_wait_until(sync, target_unix)
    log.info("[3/5] ✓ T-0 tercapai. Delta: %+.3f ms", delta_ms)

    # ── [4/5] Observer inject ─────────────────────────────────────────────
    log.info("[4/5] Menyuntikkan MutationObserver …")
    t_obs = time.perf_counter()
    try:
        obs_result = await inject_checkout_observer(page, sync)
    except Exception as exc:
        log.error("[4/5] Observer gagal: %s", exc)
        await close_browser(playwright, context)
        return

    log.info("[4/5] ✓ Observer klik berhasil  latency=%.3f ms",
             obs_result.get("elapsed_ms", 0))

    # ── [5/5] API checkout + fallback UI ────────────────────────────────────
    log.info("[5/5] Menunggu Page B — API place_order + fallback UI …")
    try:
        result = await fire_checkout_page_b(page)
    except Exception as exc:
        log.error("[5/5] Checkout gagal: %s", exc)
        await close_browser(playwright, context)
        return

    # ── Laporan Akhir ─────────────────────────────────────────────────────
    log.info("═" * 62)
    log.info("  HASIL TEST")
    log.info("═" * 62)
    log.info("  Status          : %s", "✅ PASS" if result.success else "❌ FAIL")
    log.info("  Method          : %s", result.method)
    log.info("  Elapsed         : %.1f ms", result.elapsed_ms)
    log.info("  Order ID        : %s", result.order_id or "(none)")
    if result.error_msg:
        log.info("  Error           : %s", result.error_msg)
    log.info("  Timing breakdown:")
    log.info("    NTP delta     : %+.3f ms   (target akurasi < 2 ms)", delta_ms)
    log.info("    Observer klik : %.3f ms    (target < 5 ms)",
             obs_result.get("elapsed_ms", 0))
    log.info("    Total Page B  : %.1f ms",  result.elapsed_ms)
    log.info("═" * 62)

    verdict(result.success, delta_ms,
            obs_result.get("elapsed_ms", 999),
            None)  # ApiCheckoutResult tidak punya confirm_click

    await asyncio.sleep(4)
    await close_browser(playwright, context)


def verdict(success, delta_ms, obs_ms, click_result) -> None:
    log.info("  VERDIKT PER KOMPONEN:")
    _check("NTP presisi < 5 ms",
           abs(delta_ms) < 5,
           f"{delta_ms:+.3f} ms")
    _check("Observer < 5 ms (fast-path jika 0)",
           obs_ms < 5 or obs_ms == 0,
           f"{obs_ms:.3f} ms")
    if click_result:
        _check("isTrusted = true",
               click_result.trusted is True or click_result.trusted is None,
               str(click_result.trusted))
    _check("Full flow sukses", success, "")


def _check(label: str, ok: bool, detail: str) -> None:
    icon = "  ✅" if ok else "  ❌"
    log.info("%s  %-38s %s", icon, label, detail)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = start_server(ARGS.port)
    time.sleep(0.3)

    try:
        asyncio.run(run_test())
    except KeyboardInterrupt:
        log.info("Test dihentikan manual.")
    finally:
        server.shutdown()
        log.info("Server dihentikan.")