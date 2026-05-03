# hardware_click.py
"""
Hardware Click — klik level CDP via page.mouse, menghasilkan isTrusted=true.

Berbeda dari element.click() (JS synthetic → isTrusted=false), page.mouse.click()
dirouting melalui CDP Input.dispatchMouseEvent yang diperlakukan browser
identik dengan input hardware fisik.

Exports:
    ClickResult          — hasil klik dengan koordinat & timing
    hardware_click()     — fungsi utama
"""

import asyncio
import random
import time
import logging
from dataclasses import dataclass
from typing import Literal

from playwright.async_api import Page, Locator, ElementHandle

import config as cfg

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  DATA
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ClickResult:
    success    : bool
    x          : float
    y          : float
    trusted    : bool | None   # None = verifikasi dilewati (navigasi sudah terjadi)
    elapsed_ms : float
    note       : str


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

async def hardware_click(
    page   : Page,
    target : Locator | ElementHandle | str,
    *,
    button           : Literal["left", "right", "middle"] = "left",
    click_count      : int   = 1,
    scroll_into_view : bool  = True,
    stability_wait_ms: float = cfg.STABILITY_WAIT_MS,
    move_steps       : int   = cfg.MOUSE_MOVE_STEPS,
    move_delay_ms    : float = cfg.MOUSE_MOVE_DELAY_MS,
    use_cdp_fallback : bool  = True,
    verify_trusted   : bool  = cfg.VERIFY_TRUSTED,
    verify_timeout_ms: float = 800,
) -> ClickResult:
    """
    Cari koordinat layar elemen, gerakkan mouse secara natural, lalu
    klik via CDP Input.dispatchMouseEvent → event.isTrusted === true.

    OPTIMASI vs versi sebelumnya:
    - stability_wait_ms default di config diturunkan: 60ms → 20ms
    - move_steps default di config diturunkan: 8 → 3
    - move_delay_ms default di config diturunkan: 8ms → 2ms
    - scroll_into_view skip jika elemen sudah visible (hemat ~50ms)
    - verify_timeout_ms diturunkan: 1500ms → 800ms
    Efek total: klik dari ~499ms → target <200ms
    """
    t_start = time.perf_counter()

    # ── 1. Resolve target ────────────────────────────────────────────────
    handle = await _resolve_handle(page, target)

    # ── 2. Scroll into view ──────────────────────────────────────────────
    # [FIX] is_visible() hanya cek CSS, bukan apakah elemen dalam viewport.
    # Elemen bisa is_visible=True tapi y=1319 (di luar layar) → klik meleset.
    # Solusi: cek bounding_box dulu, scroll hanya kalau di luar viewport.
    # stability_wait tetap di-skip kalau sudah dalam viewport → hemat ~20ms.
    if scroll_into_view:
        bbox_pre = await handle.bounding_box()
        vp        = page.viewport_size or {"width": 1280, "height": 800}
        in_viewport = (
            bbox_pre is not None
            and bbox_pre["y"] >= 0
            and (bbox_pre["y"] + bbox_pre["height"]) <= vp["height"]
        )
        if not in_viewport:
            await handle.scroll_into_view_if_needed(timeout=5_000)
            await asyncio.sleep(stability_wait_ms / 1_000)
        # Kalau sudah dalam viewport → skip sleep, langsung ke bounding box

    # ── 3. Bounding box → koordinat klik ─────────────────────────────────
    bbox = await handle.bounding_box()
    if bbox is None:
        raise RuntimeError(
            "bounding_box() = None — elemen tidak terlihat atau dimensi nol. "
            "Periksa CSS visibility/display."
        )

    # Titik tengah + jitter sub-piksel kecil (hindari heuristik exact-centre)
    cx = bbox["x"] + bbox["width"]  / 2 + random.uniform(-2, 2)
    cy = bbox["y"] + bbox["height"] / 2 + random.uniform(-2, 2)

    # ── 4. Pasang listener isTrusted SEBELUM klik ─────────────────────────
    # [OPTIMASI] verify_trusted=False → skip inject listener sepenuhnya.
    # Listener inject + resolve makan ~200-300ms — skip ini hemat signifikan.
    trusted_future: asyncio.Future | None = None
    if verify_trusted:
        trusted_future = await _inject_trusted_listener(page, handle, verify_timeout_ms)

    # ── 5. Gerakkan mouse secara natural (arc Bézier) ─────────────────────
    # [OPTIMASI] steps=3, delay=2ms → total gerakan ~6ms vs sebelumnya ~64ms
    # Masih menghasilkan isTrusted=true karena tetap pakai CDP mouseMoved.
    await _move_mouse_naturally(page, cx, cy, steps=move_steps, delay_ms=move_delay_ms)

    # ── 6. Hardware click via CDP ─────────────────────────────────────────
    try:
        await page.mouse.click(cx, cy, button=button, click_count=click_count)
    except Exception as exc:
        if not use_cdp_fallback:
            raise
        log.warning("page.mouse.click gagal (%s) — fallback ke raw CDP.", exc)
        await _cdp_dispatch_click(page, cx, cy, button=button)

    elapsed_ms = (time.perf_counter() - t_start) * 1000

    # ── 7. Verifikasi isTrusted ───────────────────────────────────────────
    # [OPTIMASI] timeout diturunkan 1500ms → 800ms.
    # Kalau Shopee navigasi dalam 800ms → trusted=None (anggap sukses).
    # Kalau tidak navigasi dalam 800ms → ada masalah, perlu retry.
    trusted: bool | None = None
    if verify_trusted and trusted_future is not None:
        try:
            trusted = await asyncio.wait_for(
                trusted_future, timeout=verify_timeout_ms / 1_000
            )
            if not trusted:
                raise RuntimeError(
                    "isTrusted=false — klik masih synthetic. "
                    "Periksa apakah ada addEventListener wrapper atau shadow DOM boundary."
                )
        except asyncio.TimeoutError:
            trusted = None

    return ClickResult(
        success    = True,
        x          = cx,
        y          = cy,
        trusted    = trusted,
        elapsed_ms = round(elapsed_ms, 3),
        note       = (
            f"CDP click ({cx:.1f}, {cy:.1f})  "
            f"isTrusted={'verified' if trusted is True else 'skipped/navigated'}"
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  INTERNALS
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_handle(
    page  : Page,
    target: Locator | ElementHandle | str,
) -> ElementHandle:
    """Normalkan tiga tipe target ke ElementHandle."""
    if isinstance(target, str):
        handle = await page.locator(target).first.element_handle(timeout=10_000)
        if handle is None:
            raise RuntimeError(f"Selector '{target}' tidak menemukan elemen.")
        return handle

    if isinstance(target, ElementHandle):
        return target

    handle = await target.element_handle(timeout=10_000)
    if handle is None:
        raise RuntimeError("Locator tidak menemukan elemen.")
    return handle


async def _move_mouse_naturally(
    page    : Page,
    dest_x  : float,
    dest_y  : float,
    steps   : int   = 3,      # [OPTIMASI] 8 → 3: hemat ~40ms
    delay_ms: float = 2.0,    # [OPTIMASI] 8ms → 2ms: hemat ~48ms
) -> None:
    """
    Gerakkan mouse dari posisi saat ini ke (dest_x, dest_y) melalui
    arc kuadratik Bézier dengan titik kontrol yang dirandomisasi.

    steps=3, delay=2ms → total ~6ms (vs sebelumnya steps=8, delay=8ms = ~64ms)
    Masih menghasilkan isTrusted=true — CDP mouseMoved tetap dikirim.
    Anti-fingerprint tetap terjaga karena arc Bézier masih acak setiap run.
    """
    try:
        curr_x: float = page.mouse._x  # type: ignore[attr-defined]
        curr_y: float = page.mouse._y  # type: ignore[attr-defined]
    except AttributeError:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        curr_x = vp["width"]  / 2
        curr_y = vp["height"] / 2

    mid_x  = (curr_x + dest_x) / 2
    mid_y  = (curr_y + dest_y) / 2
    ctrl_x = mid_x + random.uniform(-40, 40)
    ctrl_y = mid_y + random.uniform(-30, 30)

    for i in range(1, steps + 1):
        t  = i / steps
        bx = (1 - t)**2 * curr_x + 2 * (1 - t) * t * ctrl_x + t**2 * dest_x
        by = (1 - t)**2 * curr_y + 2 * (1 - t) * t * ctrl_y + t**2 * dest_y
        await page.mouse.move(bx, by)
        await asyncio.sleep(delay_ms / 1_000)


async def _inject_trusted_listener(
    page      : Page,
    handle    : ElementHandle,
    timeout_ms: float,
) -> asyncio.Future:
    """
    Pasang listener JS one-shot yang meneruskan nilai event.isTrusted
    ke Python melalui page.expose_function.
    """
    loop   = asyncio.get_event_loop()
    future : asyncio.Future = loop.create_future()

    cb_name = "__shopee_war_trusted_cb__"

    def on_trusted(val: bool) -> None:
        if not future.done():
            future.set_result(val)

    try:
        await page.expose_function(cb_name, on_trusted)
    except Exception:
        pass

    await handle.evaluate(f"""
        (el) => {{
            el.addEventListener('click', (e) => {{
                if (window['{cb_name}']) window['{cb_name}'](e.isTrusted);
            }}, {{ once: true }});
        }}
    """)

    return future


async def _cdp_dispatch_click(
    page  : Page,
    x     : float,
    y     : float,
    button: str = "left",
) -> None:
    """
    Raw CDP fallback: kirim mousePressed + mouseReleased langsung.
    """
    cdp = await page.context.new_cdp_session(page)
    base = dict(type="mousePressed", x=x, y=y,
                button=button, clickCount=1, modifiers=0)
    await cdp.send("Input.dispatchMouseEvent", base)
    await asyncio.sleep(0.04)
    await cdp.send("Input.dispatchMouseEvent", {**base, "type": "mouseReleased"})
    await cdp.detach()