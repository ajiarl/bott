# api_checkout.py
"""
API Checkout — dipakai di Page B setelah observer klik di Page A.

Flow hybrid:
  Page A: dynamic observer → CDP click Checkout (tepat T-0)
             ↓ navigasi ~150ms
  Page B: checkout/get (retry 5x) → place_order     ← hemat ~1500ms
             ↓ gagal semua
          fallback: hardware click "Buat Pesanan"

Kenapa ini berbeda dari percobaan API sebelumnya:
  - Sebelumnya: checkout/get dipanggil tepat T-0 → Shopee masih update harga → ERROR
  - Sekarang: dipanggil SETELAH navigasi ke /checkout (~150-350ms setelah T-0)
              → server Shopee sudah selesai update harga flash sale → berhasil
"""

import asyncio
import json
import logging
import time
import copy
from dataclasses import dataclass, field

from playwright.async_api import Page

from hardware_click import hardware_click
import config as cfg

log = logging.getLogger(__name__)

_CHECKOUT_GET_RETRIES  = 5      # retry checkout/get maksimal 5x
_CHECKOUT_GET_INTERVAL = 0.100  # 100ms antar retry


@dataclass
class ApiCheckoutResult:
    success      : bool
    order_id     : str | None
    error_msg    : str | None
    elapsed_ms   : float
    method       : str = "unknown"   # "api" atau "ui"
    raw_response : dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN ENTRY — dipanggil dari main.py setelah observer klik Page A
# ─────────────────────────────────────────────────────────────────────────────

async def fire_checkout_page_b(page: Page) -> ApiCheckoutResult:
    """
    Dipanggil SETELAH inject_checkout_observer() sukses.
    Browser sudah mulai navigasi ke /checkout.

    Strategi:
      1. Tunggu URL berubah ke /checkout
      2. Langsung checkout/get dengan retry — harga sudah flash sale
      3. Kalau berhasil → place_order (~250ms total dari T-0)
      4. Kalau gagal semua → fallback hardware click "Buat Pesanan"
    """
    t_start = time.perf_counter()

    # ── Tunggu navigasi ke /checkout ──────────────────────────────────────
    log.info("[PAGE-B] Menunggu navigasi ke checkout …")
    try:
        await page.wait_for_url("**/checkout**", timeout=5_000)
        t_nav = (time.perf_counter() - t_start) * 1000
        log.info("[PAGE-B] ✓ Navigasi selesai: T+%.0f ms", t_nav)
    except Exception:
        log.warning("[PAGE-B] Navigasi timeout — tetap coba checkout/get …")

    # ── Retry checkout/get ────────────────────────────────────────────────
    # NOTE: _CHECKOUT_GET_RETRIES hanya berlaku untuk error transient
    # (network timeout, server 500). Error permanent Shopee (misal
    # "informasi produk diperbarui") langsung break → fallback UI.
    checkout_data = None
    for attempt in range(1, _CHECKOUT_GET_RETRIES + 1):
        t_attempt = (time.perf_counter() - t_start) * 1000
        log.info("[PAGE-B] checkout/get attempt %d/%d  (T+%.0f ms)",
                 attempt, _CHECKOUT_GET_RETRIES, t_attempt)

        try:
            result_get = await page.evaluate(_CHECKOUT_GET_JS)
        except Exception as exc:
            log.warning("[PAGE-B] attempt %d evaluate error: %s", attempt, exc)
            await asyncio.sleep(_CHECKOUT_GET_INTERVAL)
            continue

        raw          = result_get.get("data", {})
        checkout_data = raw.get("data") or raw
        session_id   = checkout_data.get("checkout_session_id")

        if session_id:
            t_got = (time.perf_counter() - t_start) * 1000
            log.info("[PAGE-B] ✓ session_id OK  attempt=%d  T+%.0f ms",
                     attempt, t_got)
            break

        err = raw.get("error_msg") or raw.get("error") or ""
        err_str = str(err)[:120]
        log.warning("[PAGE-B] attempt %d gagal: %s", attempt, err_str)
        checkout_data = None

        # Error permanent → skip retry, langsung fallback
        if "informasi produk" in err_str.lower() or "diperbarui" in err_str.lower():
            log.info("[PAGE-B] Error permanent — skip retry, langsung fallback UI")
            break

        if attempt < _CHECKOUT_GET_RETRIES:
            await asyncio.sleep(_CHECKOUT_GET_INTERVAL)

    # ── API berhasil → place_order ────────────────────────────────────────
    if checkout_data:
        log.info("[PAGE-B] Menembak place_order via API …")
        return await _fire_place_order(page, checkout_data, t_start)

    # ── Fallback UI ────────────────────────────────────────────────────────
    log.warning("[PAGE-B] checkout/get gagal %d attempt — fallback UI …",
                _CHECKOUT_GET_RETRIES)
    return await _fallback_hardware_click(page, t_start)


# ─────────────────────────────────────────────────────────────────────────────
#  FIRE PLACE_ORDER
# ─────────────────────────────────────────────────────────────────────────────

async def _fire_place_order(
    page         : Page,
    checkout_data: dict,
    t_start      : float,
) -> ApiCheckoutResult:

    payload     = _build_place_order_payload(checkout_data)
    payload_str = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))

    try:
        result = await page.evaluate(_PLACE_ORDER_JS, payload_str)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        log.error("[PAGE-B] place_order evaluate error: %s", exc)
        return await _fallback_hardware_click(page, t_start)

    elapsed_ms = (time.perf_counter() - t_start) * 1000
    js_ms      = result.get("elapsed", 0)

    if not result.get("ok"):
        log.error("[PAGE-B] place_order fetch error: %s", result.get("error"))
        return await _fallback_hardware_click(page, t_start)

    data = result.get("data", {})
    log.info("[PAGE-B] place_order HTTP=%s | JS=%.0f ms | Total=%.0f ms",
             result.get("status"), js_ms, elapsed_ms)
    log.info("[PAGE-B] Response: %s", json.dumps(data)[:500])

    order_id = None
    if "checkout_order_list" in data:
        orders = data["checkout_order_list"]
        if orders:
            order_id = str(orders[0].get("orderid") or orders[0].get("order_id") or "")
    elif "ordersn"  in data: order_id = str(data["ordersn"])
    elif "order_id" in data: order_id = str(data["order_id"])

    error_msg = data.get("error_msg") or data.get("message") or ""
    success   = (
        data.get("error") == 0
        or order_id is not None
        or bool(data.get("redirect_url"))
        or "payment" in str(data).lower()
    )

    if not success:
        log.warning("[PAGE-B] place_order response tidak sukses: %s — fallback UI", error_msg)
        return await _fallback_hardware_click(page, t_start)

    return ApiCheckoutResult(
        success      = True,
        order_id     = order_id,
        error_msg    = None,
        elapsed_ms   = elapsed_ms,
        method       = "api",
        raw_response = data,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  FALLBACK — hardware click "Buat Pesanan"
# ─────────────────────────────────────────────────────────────────────────────

async def _dismiss_popups(page: Page) -> None:
    """Tutup popup/overlay Shopee yang mungkin menutupi tombol."""
    selectors = [
        ".shopee-popup__close-btn",
        ".shopee-modal__close",
        ".stardust-popup__close-btn",
        "button.shopee-button-solid--primary:has-text('OK')",
        "button.shopee-button-solid--primary:has-text('Mengerti')",
        "button:has-text('Kembali')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible():
                await btn.click(force=True, timeout=1_000)
                log.info("[PAGE-B] Popup ditutup via: %s", sel)
                await asyncio.sleep(0.3)
                return
        except Exception:
            continue


async def _fallback_hardware_click(page: Page, t_start: float) -> ApiCheckoutResult:
    """
    Fallback: tunggu tombol "Buat Pesanan" visible lalu klik via CDP.
    Dismiss popup dulu, lalu klik dengan retry + verifikasi.
    """
    log.info("[PAGE-B] Fallback: tunggu tombol 'Buat Pesanan' …")

    # ── Dismiss popup yang mungkin muncul ─────────────────────────────
    await _dismiss_popups(page)

    # ── Cari tombol "Buat Pesanan" ────────────────────────────────────
    confirm_loc = page.locator(
        "button.stardust-button.stardust-button--primary.stardust-button--large",
        has_text="Buat Pesanan",
    )

    try:
        await confirm_loc.wait_for(state="visible", timeout=10_000)
    except Exception:
        confirm_loc = page.locator(
            "button.stardust-button.stardust-button--primary",
            has_text="Buat Pesanan",
        )
        try:
            await confirm_loc.wait_for(state="visible", timeout=5_000)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t_start) * 1000
            # Screenshot dihapus untuk speed

            return ApiCheckoutResult(
                success=False, order_id=None,
                error_msg=f"Tombol tidak muncul: {exc}",
                elapsed_ms=elapsed_ms, method="ui",
            )

    # ── Log info tombol ───────────────────────────────────────────────
    t_visible = (time.perf_counter() - t_start) * 1000
    log.info("[PAGE-B] Tombol muncul T+%.0f ms", t_visible)

    # ── Klik dengan retry (max 3x) ────────────────────────────────────
    url_before = page.url
    max_retries = 3

    def _is_navigated() -> bool:
        """Cek apakah halaman sudah pindah dari checkout."""
        u = page.url
        return u != url_before or "payment" in u or "order" in u or "success" in u

    for attempt in range(1, max_retries + 1):
        # Cek: mungkin klik sebelumnya sudah navigasi (redirect lambat)
        if _is_navigated():
            elapsed_ms = (time.perf_counter() - t_start) * 1000
            log.info("[PAGE-B] ✓ Sudah navigasi ke: %s  T+%.0f ms", page.url, elapsed_ms)
            return ApiCheckoutResult(
                success=True, order_id=None,
                error_msg=None, elapsed_ms=elapsed_ms, method="ui",
            )

        log.info("[PAGE-B] Klik percobaan %d/%d …", attempt, max_retries)

        try:
            click_result = await hardware_click(
                page,
                confirm_loc,
                scroll_into_view = True,
                verify_trusted   = False,
            )
            log.info(
                "[PAGE-B] Klik terkirim (%.0f, %.0f) — polling URL …",
                click_result.x, click_result.y,
            )
        except Exception as exc:
            # Klik gagal bisa karena page sudah navigasi
            if _is_navigated():
                elapsed_ms = (time.perf_counter() - t_start) * 1000
                log.info("[PAGE-B] ✓ Page navigasi saat klik: %s  T+%.0f ms", page.url, elapsed_ms)
                return ApiCheckoutResult(
                    success=True, order_id=None,
                    error_msg=None, elapsed_ms=elapsed_ms, method="ui",
                )
            log.warning("[PAGE-B] Klik gagal: %s", exc)
            if attempt < max_retries:
                await asyncio.sleep(0.5)
                continue
            elapsed_ms = (time.perf_counter() - t_start) * 1000
            return ApiCheckoutResult(
                success=False, order_id=None,
                error_msg=str(exc), elapsed_ms=elapsed_ms, method="ui",
            )

        # ── Polling URL: cek setiap 300ms selama 2.4 detik ──────────────
        for _poll in range(8):
            await asyncio.sleep(0.3)
            if _is_navigated():
                elapsed_ms = (time.perf_counter() - t_start) * 1000
                log.info("[PAGE-B] ✓ URL berubah → %s  T+%.0f ms", page.url, elapsed_ms)
                return ApiCheckoutResult(
                    success=True, order_id=None,
                    error_msg=None, elapsed_ms=elapsed_ms, method="ui",
                )

        log.warning("[PAGE-B] URL tidak berubah 2.5s — retry …")
        await _dismiss_popups(page)

    # Semua retry habis — cek terakhir
    if _is_navigated():
        elapsed_ms = (time.perf_counter() - t_start) * 1000
        log.info("[PAGE-B] ✓ URL akhirnya berubah: %s  T+%.0f ms", page.url, elapsed_ms)
        return ApiCheckoutResult(
            success=True, order_id=None,
            error_msg=None, elapsed_ms=elapsed_ms, method="ui",
        )

    elapsed_ms = (time.perf_counter() - t_start) * 1000
    log.warning("[PAGE-B] Semua %d klik tidak mengubah URL", max_retries)
    return ApiCheckoutResult(
        success=False, order_id=None,
        error_msg="Klik tidak mengubah URL setelah retry",
        elapsed_ms=elapsed_ms, method="ui",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_place_order_payload(checkout_data: dict) -> dict:
    payload = copy.deepcopy(checkout_data)
    payload["timestamp"]       = int(time.time())
    payload["captcha_id"]      = ""
    payload["captcha_version"] = 1
    payload["client_id"]       = payload.get("client_id", 0)
    payload["cart_type"]       = payload.get("cart_type", 0)
    payload["checkout_scope"]  = payload.get("checkout_scope", 0)
    for key in ["banners", "buy_one_more", "notifications", "first_load_info",
                "tms_trackers", "__raw", "disabled_checkout_info",
                "payment_channel_info"]:
        payload.pop(key, None)
    return payload


# ─────────────────────────────────────────────────────────────────────────────
#  JS STRINGS
# ─────────────────────────────────────────────────────────────────────────────

_CHECKOUT_GET_JS = """
    async () => {
        const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
        const resp = await fetch('https://shopee.co.id/api/v4/checkout/get', {
            method: 'POST',
            headers: {
                'content-type': 'application/json',
                'x-requested-with': 'XMLHttpRequest',
                'x-csrftoken': csrf,
                'x-api-source': 'rweb',
                'x-shopee-language': 'id',
                'referer': 'https://shopee.co.id/checkout',
            },
            body: JSON.stringify({
                timestamp: Math.floor(Date.now() / 1000),
                cart_type: 0,
                client_id: 0,
                device_info: {
                    timezone_offset_in_minutes: 420,
                    device_sz_fingerprint: '',
                    device_id: '',
                    device_fingerprint: '',
                    tongdun_blackbox: '',
                    buyer_payment_info: {},
                },
            }),
            credentials: 'include',
        });
        const data = await resp.json();
        return { status: resp.status, data };
    }
"""

_PLACE_ORDER_JS = """
    async (payloadStr) => {
        const t0   = performance.now();
        const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
        const payload = JSON.parse(payloadStr);
        payload.timestamp = Math.floor(Date.now() / 1000);
        try {
            const resp = await fetch('https://shopee.co.id/api/v4/checkout/place_order', {
                method: 'POST',
                headers: {
                    'content-type': 'application/json',
                    'x-requested-with': 'XMLHttpRequest',
                    'x-csrftoken': csrf,
                    'x-api-source': 'pc',
                    'x-shopee-language': 'id',
                    'referer': 'https://shopee.co.id/checkout',
                },
                body: JSON.stringify(payload),
                credentials: 'include',
            });
            const data = await resp.json();
            return { ok: true, status: resp.status, data, elapsed: performance.now() - t0 };
        } catch(e) {
            return { ok: false, error: e.message };
        }
    }
"""


# ─────────────────────────────────────────────────────────────────────────────
#  BACKWARD COMPATIBILITY
# ─────────────────────────────────────────────────────────────────────────────

async def preload_checkout_page(page: Page) -> dict:
    return {}

async def fire_place_order(page: Page, payload: dict) -> ApiCheckoutResult:
    return await fire_checkout_page_b(page)

async def fire_api_checkout(page: Page) -> ApiCheckoutResult:
    return await fire_checkout_page_b(page)