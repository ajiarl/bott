# browser_engine.py
"""
Browser Engine — buka Chromium dengan persistent context dan route blocking.

Exports:
    open_browser()   — launch browser, buka URL, pasang blocking
    close_browser()  — tutup context dengan bersih
"""

import re
import logging
from typing import Tuple

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Route,
    Request,
    Playwright,
)

import config as cfg

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  BLOCK-LIST
#  Request yang cocok dibuang sebelum keluar ke jaringan.
#  Efek: halaman muat 3–8× lebih cepat, hemat 60–90% bandwidth.
# ─────────────────────────────────────────────────────────────────────────────

_BLOCKED_TYPES = {
    "image", "media", "font", "websocket",
    "eventsource", "manifest", "texttrack", "other",
}

_BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"google-analytics\.com",
        r"googletagmanager\.com",
        r"googlesyndication\.com",
        r"doubleclick\.net",
        r"facebook\.net",
        r"hotjar\.com",
        r"clarity\.ms",
        r"segment\.io",
        r"mixpanel\.com",
        r"amplitude\.com",
        r"\.(jpg|jpeg|png|gif|webp|avif|svg|ico|bmp)(\?.*)?$",
        r"\.(mp4|webm|ogg|mov|m3u8|ts)(\?.*)?$",
        r"\.(woff2?|eot|ttf|otf)(\?.*)?$",
        r"adservice\.google\.",
        r"amazon-adsystem\.com",
    ]
]


def _should_block(request: Request) -> bool:
    if request.resource_type in _BLOCKED_TYPES:
        return True
    return any(p.search(request.url) for p in _BLOCKED_PATTERNS)


async def _apply_blocking(page: Page) -> None:
    """Pasang satu intercept '**/*' — semua request melewati handler ini."""
    async def handler(route: Route, request: Request) -> None:
        if _should_block(request):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", handler)
    log.info("Route blocking aktif — gambar/video/font/tracker diblokir.")


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

async def open_browser(url: str) -> Tuple[Playwright, BrowserContext, Page]:
    """
    Luncurkan Chromium dengan persistent context (profil login tersimpan),
    pasang route blocking, navigasi ke `url`.

    Args:
        url: URL yang langsung dibuka setelah browser siap.

    Returns:
        Tuple (playwright_instance, context, page) — ketiganya harus di-close
        oleh pemanggil via close_browser().
    """
    playwright = await async_playwright().__aenter__()

    log.info("Meluncurkan browser — profil: %s", cfg.USER_DATA_DIR)

    context: BrowserContext = await playwright.chromium.launch_persistent_context(
        user_data_dir = str(cfg.USER_DATA_DIR),
        headless      = cfg.HEADLESS,
        args          = cfg.LAUNCH_ARGS,
        viewport      = {"width": 1280, "height": 800},
        locale        = "id-ID",
        timezone_id   = "Asia/Jakarta",
    )

    # Ambil tab pertama jika sudah ada, buka tab baru jika belum.
    page: Page = context.pages[0] if context.pages else await context.new_page()

    # Log console browser ke debug (tidak mengganggu output normal)
    page.on("console", lambda msg: log.debug("[browser] %s", msg.text))

    # Pasang blocking SEBELUM navigasi — tidak ada request yang lolos
    await _apply_blocking(page)

    log.info("Navigasi ke %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    log.info("Halaman dimuat: %s", await page.title())

    return playwright, context, page


async def close_browser(playwright: Playwright, context: BrowserContext) -> None:
    """Tutup context dan playwright instance dengan bersih."""
    try:
        await context.close()
    except Exception:
        pass
    try:
        await playwright.__aexit__(None, None, None)
    except Exception:
        pass
    log.info("Browser ditutup.")
