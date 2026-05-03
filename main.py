# main.py
"""
╔══════════════════════════════════════════════════════════════╗
║            SHOPEE WAR BOT — ENTRY POINT                     ║
╚══════════════════════════════════════════════════════════════╝

Pipeline:
  ① NTP Gate        — kalibrasi & validasi jam
  ② Buka Browser    — persistent context, login tersimpan
  ③ Timing Gate     — tunggu T-0

  MODE "dynamic" (DIREKOMENDASIKAN):
     ④ Observer Click  — MutationObserver deteksi tombol Checkout (Page A)
     ⑤ API place_order — checkout/get + place_order langsung (Page B)
                         Fallback: hardware click tombol "Buat Pesanan"

  MODE "refresh":
     ④ Reload + Observer Click
     ⑤ API place_order (sama dengan dynamic)

  Kenapa hybrid dynamic + API:
    - Page A: observer deteksi disabled → CDP click → tepat T-0
    - Page B: checkout/get dipanggil SETELAH navigasi (~150-350ms setelah T-0)
              → harga flash sale sudah terupdate di server
              → place_order langsung tanpa tunggu render tombol (hemat ~1500ms)
    - Kalau API gagal → fallback hardware click (sama seperti sebelumnya)
"""

import asyncio
import sys
import logging
import datetime

import config as cfg
from ntp_sync        import calibrate_ntp, true_time, build_target_ts, async_wait_until, ClockSync
from browser_engine  import open_browser, close_browser
from observer_engine import inject_checkout_observer, start_checkout_observer, finish_checkout_observer
from api_checkout    import fire_checkout_page_b, ApiCheckoutResult

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s.%(msecs)03d  %(levelname)s  %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — NTP GATE
# ─────────────────────────────────────────────────────────────────────────────

def run_ntp_gate() -> ClockSync:
    log.info("━" * 60)
    log.info("  ① NTP GATE")
    log.info("━" * 60)

    try:
        sync = calibrate_ntp()
    except Exception as exc:
        log.critical("Kalibrasi NTP gagal: %s", exc)
        sys.exit(1)

    errors     = []
    offset_ms  = abs(sync.offset_sec) * 1_000
    std_dev_ms = sync.offset_std_dev  * 1_000
    rtt_ms     = sync.rtt_sec         * 1_000

    if offset_ms  > cfg.MAX_OFFSET_MS:
        errors.append(f"Offset {offset_ms:.1f} ms > batas {cfg.MAX_OFFSET_MS} ms")
    if std_dev_ms > cfg.MAX_STD_DEV_MS:
        errors.append(f"Std dev {std_dev_ms:.1f} ms > batas {cfg.MAX_STD_DEV_MS} ms")
    if sync.stratum > cfg.MAX_STRATUM:
        errors.append(f"Stratum {sync.stratum} > batas {cfg.MAX_STRATUM}")
    if rtt_ms > cfg.MAX_RTT_MS:
        errors.append(f"RTT {rtt_ms:.1f} ms > batas {cfg.MAX_RTT_MS} ms")

    if errors:
        log.error("NTP gate TIDAK LULUS:")
        for e in errors:
            log.error("  ✗  %s", e)
        log.critical("Bot dihentikan — perbaiki koneksi atau ubah threshold di config.py.")
        sys.exit(1)

    log.info(
        "NTP gate LULUS ✓  offset=%.3f ms | std_dev=%.3f ms | RTT=%.3f ms | stratum=%d",
        offset_ms, std_dev_ms, rtt_ms, sync.stratum,
    )
    return sync


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN ASYNC
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:

    # ── ① NTP Gate ────────────────────────────────────────────────────────
    sync = run_ntp_gate()

    target_unix = build_target_ts(
        hour        = cfg.FLASH_SALE_HOUR,
        minute      = cfg.FLASH_SALE_MINUTE,
        second      = cfg.FLASH_SALE_SECOND,
        microsecond = cfg.FLASH_SALE_USEC,
        use_utc     = True,
        sync        = sync,
    )
    target_dt = datetime.datetime.utcfromtimestamp(target_unix)

    log.info("━" * 60)
    log.info("  Target flash sale : %s UTC", target_dt.strftime("%Y-%m-%d %H:%M:%S.%f"))
    log.info("  Mode checkout     : %s",     cfg.CHECKOUT_MODE)
    log.info("  Selector Page A   : %s",     cfg.CHECKOUT_BTN_SELECTOR)
    log.info("  Selector Page B   : %s",     cfg.CONFIRM_BTN_SELECTOR)
    log.info("━" * 60)

    # ── ② Buka Browser ────────────────────────────────────────────────────
    log.info("  ② BUKA BROWSER")
    log.info("━" * 60)
    playwright, context, page = await open_browser(cfg.SHOPEE_CART_URL)

    now_str = datetime.datetime.utcfromtimestamp(true_time(sync)).strftime("%H:%M:%S.%f")
    log.info("Keranjang siap | NTP: %s UTC", now_str)

    # Pastikan tombol ada di DOM SEBELUM menunggu T-0 (untuk speed maksimal)
    try:
        await page.wait_for_selector(cfg.CHECKOUT_BTN_SELECTOR, state="attached", timeout=15_000)
        log.info("Tombol Checkout siap di DOM ✓")
    except Exception as exc:
        log.error("Tombol Checkout tidak ditemukan di DOM: %s", exc)
        return

    # ── ③ Timing Gate + ④ Observer Click (Page A) ─────────────────────────
    log.info("━" * 60)
    log.info("  ③ TIMING GATE + ④ OBSERVER CLICK (Page A)")
    log.info("━" * 60)

    try:
        if cfg.CHECKOUT_MODE == "dynamic":
            log.info("[DYNAMIC] Pre-injecting observer …")
            await start_checkout_observer(page)

            log.info("[DYNAMIC] Menunggu T-0 …")
            await async_wait_until(sync, target_unix)

            log.info("[DYNAMIC] T-0 tercapai. Menunggu sinyal observer …")
            await finish_checkout_observer(page, sync)

        elif cfg.CHECKOUT_MODE == "refresh":
            reload_ts = target_unix - (cfg.PRE_RELOAD_LEAD_MS / 1_000)
            reload_dt = datetime.datetime.utcfromtimestamp(reload_ts)
            log.info(
                "[REFRESH] Menunggu T-%d ms = %s UTC …",
                cfg.PRE_RELOAD_LEAD_MS,
                reload_dt.strftime("%H:%M:%S.%f"),
            )
            await async_wait_until(sync, reload_ts)
            log.info("[REFRESH] Reload halaman keranjang …")
            await page.reload(wait_until="domcontentloaded", timeout=15_000)
            log.info("[REFRESH] Halaman termuat ulang. Menunggu tombol Checkout di DOM …")
            try:
                await page.wait_for_selector(cfg.CHECKOUT_BTN_SELECTOR, state="attached", timeout=10_000)
            except Exception:
                pass
            log.info("[REFRESH] Menyuntikkan observer …")
            await inject_checkout_observer(page, sync)

        else:
            log.critical("CHECKOUT_MODE tidak dikenal: '%s'.", cfg.CHECKOUT_MODE)
            await close_browser(playwright, context)
            sys.exit(1)

    except RuntimeError as exc:
        log.error("Observer gagal: %s", exc)
        await close_browser(playwright, context)
        sys.exit(1)

    # ── ⑤ Page B — API place_order + fallback UI ──────────────────────────
    #
    #  Dipanggil SETELAH observer klik berhasil.
    #  Saat ini browser sudah mulai navigasi ke /checkout.
    #  checkout/get dipanggil setelah navigasi selesai (~150-350ms setelah T-0)
    #  → harga flash sale sudah valid di server → place_order langsung.
    #  Kalau checkout/get gagal → fallback ke hardware click "Buat Pesanan".
    # ─────────────────────────────────────────────────────────────────────
    log.info("━" * 60)
    log.info("  ⑤ PAGE B — API place_order + fallback UI")
    log.info("━" * 60)

    try:
        result: ApiCheckoutResult = await fire_checkout_page_b(page)
    except Exception as exc:
        log.error("Page B gagal: %s", exc)
        await close_browser(playwright, context)
        sys.exit(1)

    # ── Laporan Akhir ─────────────────────────────────────────────────────
    log.info("━" * 60)
    log.info("  LAPORAN AKHIR")
    log.info("━" * 60)
    log.info("  Status   : %s", "✓ BERHASIL" if result.success else "✗ GAGAL")
    log.info("  Method   : %s", result.method)
    log.info("  Order ID : %s", result.order_id or "(lihat response)")
    log.info("  Elapsed  : %.1f ms", result.elapsed_ms)
    if result.error_msg:
        log.error("  Error    : %s", result.error_msg)
    log.info("━" * 60)

    await asyncio.sleep(cfg.POST_CLICK_HOLD_MS / 1000)
    await close_browser(playwright, context)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(main())