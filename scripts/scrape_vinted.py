# scripts/scrape_vinted.py
import os, re, csv, time, asyncio
from urllib.parse import urljoin
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

PROFILE_URL = os.getenv("VINTED_PROFILE_URL", "").strip()
EMAIL = os.getenv("VINTED_EMAIL", "").strip()
PASSWORD = os.getenv("VINTED_PASSWORD", "").strip()
OUT = "data/feed.csv"

def first_image(src: str) -> str:
    if not src:
        return ""
    parts = re.split(r'[,\s;|]+', src.strip())
    for p in parts:
        if p.startswith("http"):
            return p
    return parts[0] if parts else ""

async def maybe_login(page):
    """
    Login er valgfrit. Vi forsøger kun, hvis begge secrets findes.
    NB: 2FA håndteres ikke i denne basisversion (kan tilføjes senere).
    """
    if not (EMAIL and PASSWORD):
        return False
    try:
        await page.goto("https://www.vinted.dk/login", wait_until="domcontentloaded")
        # Basale selectors – kan ændre sig over tid:
        await page.fill("input[name='email']", EMAIL, timeout=15000)
        await page.fill("input[name='password']", PASSWORD, timeout=15000)
        # Tryk login
        # Vinted har typisk en button[type=submit] eller data-testid på knappen
        await page.click("button[type='submit'], button:has-text('Log ind'), button:has-text('Sign in')", timeout=15000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        return True
    except Exception:
        # Hvis login fejler, fortsætter vi uden login.
        return False

async def collect_items(context, profile_url: str):
    page = await context.new_page()
    await page.goto(profile_url, wait_until="networkidle")
    # Scroll for at loade flere items
    for _ in range(12):
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(800)

    # Få fat i kort/links til items
    cards = await page.query_selector_all("a[href*='/items/']")
    rows = []

    for a in cards:
        link = await a.get_attribute("href")
        if not link:
            continue
        full = urljoin("https://www.vinted.dk", link)

        # Titel/navn i kortet (fallback senere)
        name = ""
        for sel in ["h3", "h4", ".item-title", "[data-testid='item-title']"]:
            el = await a.query_selector(sel)
            if el:
                name = (await el.inner_text()).strip()
                if name:
                    break

        # Billede
        img = ""
        img_el = await a.query_selector("img")
        if img_el:
            img = await img_el.get_attribute("src")

        # Åbn detaljeside for mere tekst/kategori
        desc = ""
        cat = ""
        item_id = ""
        try:
            d = await context.new_page()
            await d.goto(full, wait_until="domcontentloaded")

            # ID fra URL
            m = re.search(r"/items/(\d+)", d.url)
            if m:
                item_id = m.group(1)

            # Beskrivelse
            for sel in ["[data-testid='description']", ".description", ".ItemDetails__description"]:
                about = await d.query_selector(sel)
                if about:
                    desc = (await about.inner_text()).strip()
                    if desc:
                        break

            # Kategori (heuristik – selector kan ændre sig)
            # vi prøver links/moduler der ofte indeholder katalog/kategori
            for sel in ["a[href*='catalog']", "[data-testid='item-details'] a", ".details a", ".item-details a"]:
                el = await d.query_selector(sel)
                if el:
                    cat = (await el.inner_text()).strip()
                    if cat:
                        break
        except PWTimeout:
            pass
        except Exception:
            pass
        finally:
            try:
                await d.close()
            except Exception:
                pass

        if not name:
            name = "Patch"
        if not item_id:
            item_id = f"id-{int(time.time()*1000)}"

        rows.append({
            "id": item_id,
            "name": name,
            "category": cat,
            "image_url": first_image(img),
            "description": desc,
            "link": full
        })

    await page.close()
    return rows

async def run():
    if not PROFILE_URL:
        raise SystemExit("Missing VINTED_PROFILE_URL (secret)")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(locale="da-DK")

        # Prøv login hvis muligt
        await maybe_login(await context.new_page())

        rows = await collect_items(context, PROFILE_URL)
        await browser.close()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id","name","category","image_url","description","link"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

if __name__ == "__main__":
    asyncio.run(run())
