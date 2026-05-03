# transition.py
"""
⚠️  DEPRECATED — file ini TIDAK dipakai di pipeline saat ini.
    Pipeline sekarang: main.py → api_checkout.fire_checkout_page_b()
    yang sudah punya fallback UI sendiri (_fallback_hardware_click).

    File ini disimpan sebagai referensi / backup.

─────────────────────────────────────────────────────────────────

Transition — navigasi Page A → Page B + hardware click tombol konfirmasi.

Pipeline:
  Phase 1  expect_navigation (armed sebelum klik)   → tangkap redirect
  Phase 2  wait_for_url                             → konfirmasi URL final
  Phase 3  wait_for_selector skeleton (opsional)    → deteksi blank page
  Phase 4  locator has_text "Buat Pesanan"           → tombol muncul
  Phase 5  hardware_click dengan retry               → klik isTrusted=true

Semua timeout dikelola _Budget — tidak ada timeout yang diam-diam
menghabiskan jatah waktu total.

Exports:
    TransitionResult
    wait_for_page_b_and_confirm()
"""

import asyncio
import time
import logging
from dataclasses import dataclass, field

from playwright.async_api import Page

from hardware_click import hardware_click, ClickResult
import config as cfg

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  DATA
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TransitionResult:
    success           : bool
    final_url         : str          = ""
    ms_url_change     : float        = 0.0
    ms_skeleton       : float        = 0.0
    ms_confirm_visible: float        = 0.0
    confirm_click     : ClickResult | None = None
    notes             : list[str]    = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
#  BUDGET TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class _Budget:
    """
    Pelacak waktu total yang tersisa.
    Mencegah timeout individual diam-diam menghabiskan budget global.
    """
    def __init__(self, total_ms: float):
        self._start    = time.perf_counter()
        self._total_ms = total_ms

    def remaining(self, cap_ms: float | None = None) -> float:
        elapsed = (time.perf_counter() - self._start) * 1_000
        left    = max(0.0, self._total_ms - elapsed)
        return min(left, cap_ms) if cap_ms is not None else left


def _ms(t: float) -> float:
    return (time.perf_counter() - t) * 1_000


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

async def wait_for_page_b_and_confirm(page: Page) -> TransitionResult:
    """
    Tunggu halaman checkout (Page B) termuat setelah klik MutationObserver,
    lalu hardware-click tombol konfirmasi "Buat Pesanan".

    Dipanggil SETELAH inject_checkout_observer() sukses — saat itu browser
    sudah mulai redirect ke halaman checkout.

    Returns:
        TransitionResult dengan timing breakdown lengkap.

    Raises:
        RuntimeError untuk kegagalan yang tidak bisa di-recover.
    """
    result = TransitionResult(success=False)
    budget = _Budget(cfg.TOTAL_BUDGET_MS)

    # ── Phase 1: Deteksi navigasi ─────────────────────────────────────────
    log.info("[Phase 1] Menunggu perubahan URL ke checkout …")
    t1 = time.perf_counter()

    try:
        await page.wait_for_url(
            cfg.CHECKOUT_URL_PATTERN,
            wait_until = "commit",
            timeout    = budget.remaining(cfg.NAV_TIMEOUT_MS),
        )
    except Exception as exc:
        raise RuntimeError(
            f"[Phase 1] URL checkout tidak terdeteksi. "
            f"URL sekarang: {page.url}  Error: {exc}"
        ) from exc

    result.ms_url_change = _ms(t1)
    result.final_url     = page.url
    log.info("[Phase 1] ✓ Navigasi ke %s  (%.1f ms)", result.final_url, result.ms_url_change)

    # ── Phase 2: Tunggu load event (dokumen stabil) ───────────────────────
    log.info("[Phase 2] Menunggu load event …")
    try:
        await page.wait_for_url(
            cfg.CHECKOUT_URL_PATTERN,
            wait_until = "load",
            timeout    = budget.remaining(cfg.NAV_TIMEOUT_MS),
        )
    except Exception as exc:
        result.notes.append(f"Phase 2 timeout (non-fatal): {exc}")
        log.warning("[Phase 2] load event timeout — lanjut (mungkin SPA).")

    log.info("[Phase 2] ✓ URL confirmed: %s", page.url)

    # ── Phase 3: Skeleton check (opsional) ───────────────────────────────
    if cfg.CHECKOUT_SKELETON_SELECTOR:
        log.info("[Phase 3] Menunggu skeleton: %s", cfg.CHECKOUT_SKELETON_SELECTOR)
        t3 = time.perf_counter()
        try:
            await page.wait_for_selector(
                cfg.CHECKOUT_SKELETON_SELECTOR,
                state   = "visible",
                timeout = budget.remaining(cfg.SKELETON_TIMEOUT_MS),
            )
            result.ms_skeleton = _ms(t3)
            log.info("[Phase 3] ✓ Skeleton terlihat (%.1f ms)", result.ms_skeleton)
        except Exception as exc:
            raise RuntimeError(
                f"[Phase 3] Skeleton '{cfg.CHECKOUT_SKELETON_SELECTOR}' tidak muncul. "
                f"Page B mungkin blank. Error: {exc}"
            ) from exc
    else:
        log.info("[Phase 3] Skeleton check dilewati (CHECKOUT_SKELETON_SELECTOR = None).")

    # ── Phase 4: Tunggu tombol "Buat Pesanan" ────────────────────────────
    #
    #  Pakai locator has_text supaya tidak salah klik tombol lain
    #  yang kebetulan punya class sama (misal tombol voucher, dll).
    #  Class hash obfuscated (LtH6tW) sengaja TIDAK dipakai — bisa
    #  berubah kapan saja setelah Shopee deploy ulang.
    # ─────────────────────────────────────────────────────────────────────
    log.info("[Phase 4] Menunggu tombol 'Buat Pesanan' …")
    t4 = time.perf_counter()

    confirm_loc = page.locator(
        "button.stardust-button.stardust-button--primary.stardust-button--large",
        has_text="Buat Pesanan",
    )

    try:
        await confirm_loc.wait_for(
            state   = "visible",
            timeout = budget.remaining(cfg.CONFIRM_TIMEOUT_MS),
        )
    except Exception as exc:
        # Fallback: coba selector lebih longgar kalau class --large berubah
        log.warning(
            "[Phase 4] Selector ketat tidak ketemu (%s) — coba fallback …", exc
        )
        confirm_loc = page.locator(
            "button.stardust-button.stardust-button--primary",
            has_text="Buat Pesanan",
        )
        try:
            await confirm_loc.wait_for(
                state   = "visible",
                timeout = budget.remaining(cfg.CONFIRM_TIMEOUT_MS),
            )
        except Exception as exc2:
            raise RuntimeError(
                f"[Phase 4] Tombol 'Buat Pesanan' tidak muncul. "
                f"Sisa budget: {budget.remaining():.0f} ms. Error: {exc2}"
            ) from exc2

    result.ms_confirm_visible = _ms(t4)
    log.info("[Phase 4] ✓ Tombol 'Buat Pesanan' muncul (%.1f ms)", result.ms_confirm_visible)

    # ── Phase 5: Hardware click + retry ──────────────────────────────────
    last_exc: Exception | None = None
    max_attempts = cfg.CONFIRM_CLICK_RETRIES + 1

    for attempt in range(1, max_attempts + 1):
        try:
            log.info("[Phase 5] Hardware click — percobaan %d/%d …", attempt, max_attempts)

            # Pakai locator (bukan string selector) supaya tepat sasaran
            result.confirm_click = await hardware_click(
                page,
                confirm_loc,          # <── locator has_text "Buat Pesanan"
                verify_trusted    = True,
                scroll_into_view  = True,
                stability_wait_ms = cfg.STABILITY_WAIT_MS,
            )

            log.info(
                "[Phase 5] ✓ Klik berhasil  (%.0f, %.0f)  isTrusted=%s  %.1f ms",
                result.confirm_click.x,
                result.confirm_click.y,
                result.confirm_click.trusted,
                result.confirm_click.elapsed_ms,
            )
            result.success = True
            break

        except Exception as exc:
            last_exc = exc
            log.warning("[Phase 5] Percobaan %d gagal: %s", attempt, exc)

            if attempt < max_attempts:
                await asyncio.sleep(cfg.RETRY_DELAY_MS / 1_000)
                # Re-tunggu locator — mungkin terjadi re-render
                try:
                    await confirm_loc.wait_for(
                        state   = "visible",
                        timeout = budget.remaining(3_000),
                    )
                except Exception:
                    pass

    if not result.success:
        raise RuntimeError(
            f"[Phase 5] Semua {max_attempts} percobaan klik gagal. "
            f"Error terakhir: {last_exc}"
        )

    return result