# scripts/scrape_vinted.py
import os, re, csv, time, asyncio
from urllib.parse import urljoin
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ---------------------------
# Konfiguration via miljøvariabler
# ---------------------------
PROFILE_URL        = os.getenv("VINTED_PROFILE_URL", "").strip()
EMAIL              = os.getenv("VINTED_EMAIL", "").strip()
PASSWORD           = os.getenv("VINTED_PASSWORD", "").strip()
OUT                = os.getenv("OUT_CSV", "data/feed.csv")

# Pagination + lazy-scroll
MAX_PAGES          = int(os.getenv("MAX_PAGES", "30"))          # antal sider at forsøge (skrues op hvis mange varer)
SCROLLS_PER_PAGE   = int(os.getenv("SCROLLS_PER_PAGE", "5"))    # små scrolls pr. side for at trigge lazy-load
SCROLL_DELAY_MS    = int(os.getenv("SCROLL_DELAY_MS", "900"))   # ms mellem scrolls
MAX_ITEMS          = int(os.getenv("MAX_ITEMS", "200"))         # hårdt loft, hvis du vil stoppe tidligere

# Browser
HEADLESS           = (os.getenv("HEADLESS", "1") == "1")        # 1=headless, 0=vis browser

# ---------------------------
# Hjælpere
# ---------------------------
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
    Login er valgfrit. Forsøger kun hvis EMAIL & PASSWORD er sat.
    NB: 2FA håndteres ikke her (kan tilføjes senere).
    """
    if not (EMAIL and PASSWORD):
        return False
    try:
        await page.goto("https://www.vinted.dk/login", wait_until="domcontentloaded")
        await page.fill("input[name='email']", EMAIL, timeout=15000)
        await page.fill("input[name='password']", PASSWORD, timeout=15000)
        await page.click(
            "button[type='submit'], button:has-text('Log ind'), button:has-text('Sign in')",
            timeout=15000
        )
        await page.wait_for_load_state("networkidle", timeout=30000)
        print("[login] OK")
        return True
    except Exception as e:
        print(f"[login] skipped/failed: {e}")
        return False

# ---------------------------
# Hoved-scraper: pagination + let scroll pr. side
# ---------------------------
async def collect_items(context, profile_url: str):
    """
    Henter alle item-links via pagination (?page=1..N).
    På hver side laves en lille scroll for at sikre lazy-loadede elementer.
    Dedup'er links på tværs af sider.
    """
    page = await context.new_page()
    all_links = set()

    for page_num in range(1, MAX_PAGES + 1):
        sep = "&" if "?" in profile_url else "?"
        url = f"{profile_url}{sep}page={page_num}"

        try:
            await page.goto(url, wait_until="networkidle")
        except Exception as e:
            print(f"[page] goto failed p={page_num}: {e}")
            continue

        # Lille lazy-scroll pr. side
        last_height = 0
        for _ in range(SCROLLS_PER_PAGE):
            await page.mouse.wheel(0, 1600)
            await page.wait_for_timeout(SCROLL_DELAY_MS)
            try:
                height = await page.evaluate("() => document.body.scrollHeight")
                if height == last_height:
                    break
                last_height = height
            except Exception:
                break

        # Saml links
        candidates = await page.query_selector_all(
            "a[href*='/items/'], div a[data-testid*='item'][href*='/items/']"
        )
        before = len(all_links)
        for a in candidates:
            href = await a.get_attribute("href")
            if href and "/items/" in href:
                all_links.add(urljoin("https://www.vinted.dk", href))
        after = len(all_links)
        gained = after - before
        print(f"[collect_items] page={page_num} added={gained} total={after}")

        # Stop hvis ingen nye links eller vi har ramt MAX_ITEMS
        if gained == 0 or len(all_links) >= MAX_ITEMS:
            break

    await page.close()

    links = sorted(all_links)
    if len(links) > MAX_ITEMS:
        links = links[:MAX_ITEMS]
    print(f"[collect_items] TOTAL links collected: {len(links)}")

    rows = []

    # Detaljeside-scrape for hver vare
    for i, full in enumerate(links, start=1):
        name = ""
        img  = ""
        desc = ""
        cat  = ""
        item_id = ""

        try:
            d = await context.new_page()
            await d.goto(full, wait_until="domcontentloaded")
            await d.wait_for_timeout(500)

            # ID
            m = re.search(r"/items/(\d+)", d.url)
            if m:
                item_id = m.group(1)

            # Titel
            for sel in ["h1", "[data-testid='item-title']", ".Item__title", "h2"]:
                el = await d.query_selector(sel)
                if el:
                    t = (await el.inner_text()).strip()
                    if t:
                        name = t
                        break

            # Beskrivelse
            for sel in ["[data-testid='description']", ".description", ".ItemDetails__description", "[class*='description']"]:
                el = await d.query_selector(sel)
                if el:
                    t = (await el.inner_text()).strip()
                    if t:
                        desc = t
                        break

            # Kategori (heuristik)
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

            # Billede
            img_el = await d.query_selector("img")
            if img_el:
                img = await img_el.get_attribute("src")

        except PWTimeout:
            pass
        except Exception as e:
            print(f"[detail] err: {e}")
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

        # venlig throttle
        await asyncio.sleep(0.05)

    return rows

# ---------------------------
# Entrypoint
# ---------------------------
async def run():
    if not PROFILE_URL:
        raise SystemExit("Missing VINTED_PROFILE_URL (secret)")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(locale="da-DK")

        # Login (valgfrit)
        await maybe_login(await context.new_page())

        rows = await collect_items(context, PROFILE_URL)

        await browser.close()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "name", "category", "image_url", "description", "link"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"[write] {OUT} written with {len(rows)} rows")

if __name__ == "__main__":
    asyncio.run(run())
