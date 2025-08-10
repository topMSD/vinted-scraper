# scripts/scrape_vinted.py
import os, re, csv, time, asyncio
from urllib.parse import urljoin
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# --- Secrets / config from env ---
PROFILE_URL   = os.getenv("VINTED_PROFILE_URL", "").strip()
EMAIL         = os.getenv("VINTED_EMAIL", "").strip()
PASSWORD      = os.getenv("VINTED_PASSWORD", "").strip()
OUT           = "data/feed.csv"

# Tuning af scroll
MAX_SCROLLS   = int(os.getenv("MAX_SCROLLS", "50"))          # hvor mange scroll-cyklusser
SCROLL_DELAY  = int(os.getenv("SCROLL_DELAY_MS", "1000"))    # ventetid pr. scroll (ms)

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
        await page.fill("input[name='email']", EMAIL, timeout=15000)
        await page.fill("input[name='password']", PASSWORD, timeout=15000)
        # Vinted har typisk en button[type=submit] / tekst-varianter
        await page.click(
            "button[type='submit'], button:has-text('Log ind'), button:has-text('Sign in')",
            timeout=15000
        )
        await page.wait_for_load_state("networkidle", timeout=30000)
        return True
    except Exception:
        # Hvis login fejler, fortsætter vi uden login.
        return False

async def collect_items(context, profile_url: str):
    """
    Besøger profil-siden, laver robust infinite scroll, samler alle item-links,
    og åbner hver detaljeside for at hente ekstra felter.
    """
    page = await context.new_page()
    await page.goto(profile_url, wait_until="networkidle")

    # -------- Robust infinite scroll --------
    last_height = 0
    stalls = 0
    for i in range(MAX_SCROLLS):
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(SCROLL_DELAY)
        try:
            height = await page.evaluate("() => document.body.scrollHeight")
        except Exception:
            height = last_height

        if height <= last_height:
            stalls += 1
            if stalls >= 3:  # tre gange uden vækst = stop
                break
        else:
            stalls = 0
            last_height = height

    # -------- Saml alle links til items (bredere selectors + deduplikering) --------
    candidates = await page.query_selector_all(
        "a[href*='/items/'], div a[data-testid*='item'][href*='/items/']"
    )

    links_set = set()
    for a in candidates:
        href = await a.get_attribute("href")
        if href and "/items/" in href:
            links_set.add(urljoin("https://www.vinted.dk", href))

    links = sorted(links_set)
    print(f"[collect_items] Found {len(links)} item links")

    rows = []

    # -------- Gennemgå hvert item-link --------
    for idx, full in enumerate(links, start=1):
        name = ""
        img = ""
        desc = ""
        cat = ""
        item_id = ""

        # Hent navn/billede fra kortet hvis muligt (fallback på detaljesiden)
        try:
            # Forsøg at finde korresponderende <a> på profilsiden
            rel = full.replace("https://www.vinted.dk", "")
            a = await page.query_selector(f"a[href='{rel}']")
            if a:
                for sel in ["h3", "h4", ".item-title", "[data-testid='item-title']"]:
                    el = await a.query_selector(sel)
                    if el:
                        t = (await el.inner_text()).strip()
                        if t:
                            name = t
                            break
                img_el = await a.query_selector("img")
                if img_el:
                    img = await img_el.get_attribute("src")
        except Exception:
            pass

        # Åbn detaljeside for flere data
        try:
            d = await context.new_page()
            await d.goto(full, wait_until="domcontentloaded")
            await d.wait_for_timeout(500)  # lille pause for at sikre content er klar

            # ID fra URL
            m = re.search(r"/items/(\d+)", d.url)
            if m:
                item_id = m.group(1)

            # Navn fra detaljeside (fallback)
            if not name:
                for sel in ["h1", "[data-testid='item-title']", ".Item__title", "h2"]:
                    ttl = await d.query_selector(sel)
                    if ttl:
                        t = (await ttl.inner_text()).strip()
                        if t:
                            name = t
                            break

            # Beskrivelse
            for sel in ["[data-testid='description']", ".description", ".ItemDetails__description", "[class*='description']"]:
                about = await d.query_selector(sel)
                if about:
                    t = (await about.inner_text()).strip()
                    if t:
                        desc = t
                        break

            # Kategori (heuristik – første match vinder)
            for sel in [
                "a[href*='catalog']",
                "[data-testid='item-details'] a",
                ".details a",
                ".item-details a",
                "[class*='details'] a",
            ]:
                el = await d.query_selector(sel)
                if el:
                    t = (await el.inner_text()).strip()
                    if t:
                        cat = t
                        break

            # Billede fra detaljeside (fallback hvis ingen fra kortet)
            if not img:
                img_el = await d.query_selector("img")
                if img_el:
                    img = await img_el.get_attribute("src")

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

        # lille throttle for at være venlig mod sitet
        await asyncio.sleep(0.1)

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
