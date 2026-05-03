# login_setup.py
# Jalankan SEKALI untuk menyimpan sesi login ke browser_profile
# Setelah ini, main.py tidak perlu login lagi.

import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            "./browser_profile",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://shopee.co.id/buyer/login")

        print("\n" + "="*50)
        print("  Login manual di browser yang terbuka.")
        print("  Setelah berhasil masuk ke halaman utama,")
        print("  kembali ke sini dan tekan Enter.")
        print("="*50 + "\n")
        input("  Tekan Enter setelah login selesai...")

        await ctx.close()
        print("\nSesi tersimpan di browser_profile/  ✓")
        print("Sekarang jalankan: python main.py")

asyncio.run(main())
