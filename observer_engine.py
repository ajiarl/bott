# observer_engine.py
"""
Observer Engine — injeksi MutationObserver ke tombol Checkout Shopee (Page A).

Cara kerja:
    1. Auto-centang semua item di keranjang (HANYA mode refresh — setelah reload
       centang hilang, perlu dicentang ulang. Mode dynamic skip ini.)
    2. JS diinjeksi ke V8 heap untuk DETEKSI tombol aktif secepat mungkin (sub-ms).
    3. Begitu tombol aktif terdeteksi, Python langsung klik via CDP
       (Chrome DevTools Protocol) — menghasilkan isTrusted=true di sisi halaman.

Exports:
    inject_checkout_observer()   — suntik observer + klik CDP
"""

import asyncio
import logging
import datetime

from playwright.async_api import Page

from ntp_sync import ClockSync, true_time
from hardware_click import hardware_click
import config as cfg

log = logging.getLogger(__name__)

CHECKOUT_BTN_SELECTOR = "button.shopee-button-solid.shopee-button-solid--primary"
_CHECKBOX_SELECTOR    = "input.stardust-checkbox__input"


_SETUP_OBSERVER_JS = """
() => {
    const TARGET_SELECTOR = 'button.shopee-button-solid.shopee-button-solid--primary';
    const TIMEOUT_MS      = {timeout_ms};

    window.__shopee_war_promise = new Promise((resolve, reject) => {
        const target = document.querySelector(TARGET_SELECTOR);
        if (!target) {
            return reject('Tombol tidak ditemukan: "' + TARGET_SELECTOR + '"');
        }

        const isDisabled = (el) =>
            el.disabled === true || el.getAttribute('aria-disabled') === 'true';

        // Fast-path
        if (!isDisabled(target)) {
            return resolve({
                elapsed_ms: 0,
                mechanism:  'fast-path',
                note:       'Tombol sudah aktif saat setup.'
            });
        }

        const t0 = performance.now();
        const observer = new MutationObserver((mutations) => {
            for (const m of mutations) {
                if (m.type !== 'attributes') continue;
                const attr = m.attributeName;
                if (attr !== 'disabled' && attr !== 'aria-disabled') continue;
                if (isDisabled(target)) continue;

                observer.disconnect();
                clearTimeout(safetyTimer);
                resolve({
                    elapsed_ms: parseFloat((performance.now() - t0).toFixed(3)),
                    mechanism:  attr,
                    note:       'MutationObserver (' + attr + ') fired.'
                });
            }
        });

        observer.observe(target, {
            attributes: true,
            attributeFilter: ['disabled', 'aria-disabled'],
        });

        const safetyTimer = setTimeout(() => {
            observer.disconnect();
            reject('Timeout ' + TIMEOUT_MS + ' ms: tombol tidak pernah aktif.');
        }, TIMEOUT_MS);
    });
    
    return true; // Setup selesai instan
}
"""

_AWAIT_OBSERVER_JS = """
() => window.__shopee_war_promise
"""


async def _auto_check_items(page: Page) -> None:
    """Centang semua item keranjang. Hanya dipanggil di mode refresh."""
    try:
        await page.wait_for_selector(
            _CHECKBOX_SELECTOR,
            state   = "attached",
            timeout = 5_000,
        )
        await asyncio.sleep(0.5)

        all_checkboxes = page.locator(_CHECKBOX_SELECTOR)
        count = await all_checkboxes.count()

        if count == 0:
            log.warning("Auto-centang: tidak ada checkbox ditemukan — skip.")
            return

        log.info("Auto-centang: ditemukan %d checkbox.", count)

        first_cb   = all_checkboxes.first
        is_checked = await first_cb.is_checked()

        if not is_checked:
            await first_cb.click(force=True)
            await asyncio.sleep(0.3)
            log.info("Auto-centang: klik 'Pilih Semua' ✓")
        else:
            log.info("Auto-centang: sudah tercentang semua ✓")

    except Exception as exc:
        log.warning("Auto-centang gagal (non-fatal): %s — lanjut.", exc)


async def start_checkout_observer(page: Page) -> bool:
    """
    Suntik MutationObserver ke V8 heap tapi jangan ditunggu hasilnya dulu.
    Script setup ini akan selesai instan setelah memasang observer di window.
    """
    js = _SETUP_OBSERVER_JS.replace("{timeout_ms}", str(cfg.OBSERVER_TIMEOUT_MS))
    return await page.evaluate(js)


async def finish_checkout_observer(page: Page, sync: ClockSync) -> dict:
    """
    Awaits the observer promise stored on the window object.
    """
    try:
        result = await page.evaluate(_AWAIT_OBSERVER_JS)
    except Exception as exc:
        raise RuntimeError(f"Observer await failed: {exc}") from exc

    try:
        await hardware_click(
            page,
            CHECKOUT_BTN_SELECTOR,
            scroll_into_view = False,
            verify_trusted   = False,
        )
    except Exception as exc:
        raise RuntimeError(f"Hardware click gagal: {exc}") from exc

    result["clicked"] = True
    t_fire = datetime.datetime.utcfromtimestamp(true_time(sync)).strftime("%H:%M:%S.%f")

    log.info("━" * 56)
    log.info("  ✓  KLIK PAGE A TERKIRIM  (isTrusted=true via CDP)")
    log.info("  NTP time   : %s UTC", t_fire)
    log.info("  Clicked    : %s",     result.get("clicked"))
    log.info("  Latency JS : %s ms",  result.get("elapsed_ms"))
    log.info("  Mechanism  : %s",     result.get("mechanism"))
    log.info("  Note       : %s",     result.get("note"))
    log.info("━" * 56)

    return result


async def inject_checkout_observer(page: Page, sync: ClockSync) -> dict:
    """
    Fungsi legacy/convenience: pre-centang, suntik, dan tunggu.
    """
    if cfg.CHECKOUT_MODE == "refresh":
        log.info("Auto-centang item keranjang …")
        await _auto_check_items(page)

    log.info("MutationObserver diinjeksi — menunggu tombol aktif …")
    handle = await start_checkout_observer(page)
    return await finish_checkout_observer(handle, page, sync)