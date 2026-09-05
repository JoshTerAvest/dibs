import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        
        # Desktop
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto("http://localhost:7475")
        await page.wait_for_timeout(2000) # Wait for JS to render and cycle a bit
        await page.screenshot(path="docs/dashboard-v3.png")
        
        # Mobile
        page = await browser.new_page(viewport={"width": 375, "height": 812})
        await page.goto("http://localhost:7475")
        await page.wait_for_timeout(2000)
        await page.screenshot(path="docs/dashboard-v3-mobile.png")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
