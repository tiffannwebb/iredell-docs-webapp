import asyncio
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Optional, Dict

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from playwright.async_api import async_playwright, Page


IREDell_MAPGEO_URL = "https://iredellcountync.mapgeo.io/datasets/properties"
DEFAULT_TIMEOUT_MS = 45_000

app = FastAPI(title="Iredell Property Docs Downloader")


@dataclass
class PropertyLinks:
    address: str
    pin: Optional[str] = None
    prc_url: Optional[str] = None
    tax_bills_url: Optional[str] = None
    deed_url: Optional[str] = None
    deed_book_page: Optional[str] = None


def _safe_filename(s: str) -> str:
    s = re.sub(r"[^\w\s\-\.]", "", s).strip()
    s = re.sub(r"\s+", "_", s)
    return s[:120] if len(s) > 120 else s


async def _wait_for_mapgeo_ready(page: Page) -> None:
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_selector('input[placeholder*="Quick Search"]', timeout=DEFAULT_TIMEOUT_MS)


async def _search_address_on_mapgeo(page: Page, address: str) -> None:
    # Wait for the quick search input
    search = page.locator('input[placeholder*="Quick Search"]')
    await search.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)

    # Clear + type + press Enter
    await search.click()
    await search.fill("")
    await search.type(address, delay=25)
    await search.press("Enter")

    # Give MapGeo time to populate results / update UI
    await page.wait_for_timeout(1500)



async def _open_first_result_details(page: Page) -> None:
    """
    More robust MapGeo result-opening logic:
    - Prefer clicking the first *address-like link* on the page after search
    - If that fails, try clicking the first item in a list-like panel
    - Finally, verify we got the details panel by waiting for "PIN"
    """
    # Sometimes MapGeo auto-opens details; check first
    try:
        await page.wait_for_selector("text=PIN", timeout=6_000)
        return
    except:
        pass

    # Try clicking the first address-looking link (most reliable clickable target)
    address_link = page.locator("a").filter(
        has_text=re.compile(r"^\s*\d+\s+.+", re.I)
    ).first

    try:
        await address_link.wait_for(state="visible", timeout=12_000)
        await address_link.click(timeout=12_000)
    except:
        # Fallback: click first row-like element that contains an address pattern
        row = page.locator("div").filter(
            has_text=re.compile(r"\b\d+\s+\w+", re.I)
        ).first
        await row.wait_for(state="visible", timeout=12_000)
        await row.click(timeout=12_000)

    # After clicking a result, wait for details panel
    await page.wait_for_selector("text=PIN", timeout=DEFAULT_TIMEOUT_MS)




async def _extract_property_links_from_details(page: Page, address: str) -> PropertyLinks:
    links = PropertyLinks(address=address)

    body_text = await page.locator("body").inner_text()

    # Try to capture PIN value next to PIN label
    pin_match = re.search(r"\bPIN\b\s*([\d\.]+)", body_text, flags=re.I)
    if pin_match:
        links.pin = pin_match.group(1).strip()

    async def href_for_link_text(pattern: str) -> Optional[str]:
        loc = page.locator("a").filter(has_text=re.compile(pattern, re.I)).first
        try:
            if await loc.is_visible(timeout=2_500):
                return await loc.get_attribute("href")
        except:
            return None
        return None

    links.prc_url = await href_for_link_text(r"Property\s+Record\s+Card")
    links.tax_bills_url = await href_for_link_text(r"Tax\s+Bills")

    # Deed is often a link with text like "2972 / 328"
    try:
        deed_anchor = page.locator("a").filter(has_text=re.compile(r"^\s*\d+\s*/\s*\d+\s*$")).first
        if await deed_anchor.is_visible(timeout=2_500):
            links.deed_book_page = (await deed_anchor.inner_text()).strip()
            links.deed_url = await deed_anchor.get_attribute("href")
    except:
        pass

    return links


async def _download_via_browser_download(page: Page, url: str) -> Optional[bytes]:
    """
    Try to trigger an actual browser download and return the file bytes.
    """
    try:
        async with page.expect_download(timeout=20_000) as dl_info:
            await page.goto(url, wait_until="domcontentloaded")
        download = await dl_info.value
        path = await download.path()
        if path:
            return path.read_bytes()
    except:
        return None
    return None


async def _print_page_to_pdf(page: Page, url: str, landscape: bool = True) -> bytes:
    await page.goto(url, wait_until="networkidle")
    await page.wait_for_timeout(750)
    pdf_bytes = await page.pdf(
        format="Letter",
        landscape=landscape,
        print_background=True,
        margin={"top": "0.25in", "bottom": "0.25in", "left": "0.25in", "right": "0.25in"},
    )
    return pdf_bytes


async def _try_get_latest_tax_bill_url(page: Page, tax_bills_url: str) -> str:
    await page.goto(tax_bills_url, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")

    # If there is a Search button, click it to populate results
    for btn_text in ["Search", "SEARCH"]:
        btn = page.locator(f'input[type="submit"][value="{btn_text}"], button:has-text("{btn_text}")').first
        try:
            if await btn.is_visible(timeout=2_000):
                await btn.click()
                await page.wait_for_timeout(800)
                break
        except:
            pass

    # Try pick most recent year row with a link
    rows = page.locator("tr")
    n = await rows.count()
    year_re = re.compile(r"\b(20\d{2})\b")

    best_year = -1
    best_href = None

    for i in range(n):
        r = rows.nth(i)
        try:
            txt = (await r.inner_text()).strip()
        except:
            continue
        m = year_re.search(txt)
        if not m:
            continue
        year = int(m.group(1))
        if year < best_year:
            continue

        a = r.locator("a").first
        try:
            href = await a.get_attribute("href")
        except:
            href = None

        if href:
            best_year = year
            best_href = href

    if best_href:
        if best_href.startswith("http"):
            return best_href
        if best_href.startswith("/"):
            origin = re.match(r"^(https?://[^/]+)", page.url)
            if origin:
                return origin.group(1) + best_href

    # Fall back: print whatever page we ended up on
    return page.url


async def fetch_docs_as_zip(address: str) -> bytes:
    """
    Returns ZIP bytes containing available PDFs.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        # 1) Open MapGeo and search
        await page.goto(IREDell_MAPGEO_URL, wait_until="domcontentloaded")
        await _wait_for_mapgeo_ready(page)
        await _search_address_on_mapgeo(page, address)
        await _open_first_result_details(page)

        # 2) Extract links
        links = await _extract_property_links_from_details(page, address)

        files: Dict[str, bytes] = {}

        # 3) Deed
        if links.deed_url:
            deed_bytes = await _download_via_browser_download(page, links.deed_url)
            if deed_bytes is None:
                # Sometimes deed opens in viewer; print it
                try:
                    deed_bytes = await _print_page_to_pdf(page, links.deed_url, landscape=False)
                except:
                    deed_bytes = None
            if deed_bytes:
                files["deed.pdf"] = deed_bytes

        # 4) Property record card (print to PDF)
        if links.prc_url:
            try:
                files["property_record_card.pdf"] = await _print_page_to_pdf(page, links.prc_url, landscape=True)
            except:
                pass

        # 5) Tax bill (latest)
        if links.tax_bills_url:
            try:
                bill_url = await _try_get_latest_tax_bill_url(page, links.tax_bills_url)
                files["tax_bill.pdf"] = await _print_page_to_pdf(page, bill_url, landscape=False)
            except:
                pass

        await context.close()
        await browser.close()

    if not files:
        raise HTTPException(status_code=404, detail="No documents could be retrieved for that address.")

    # Make ZIP in memory
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return zip_buf.getvalue()


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html>
      <head><title>Iredell Property Docs</title></head>
      <body style="font-family: sans-serif; max-width: 720px; margin: 40px auto;">
        <h2>Iredell Property Docs Downloader</h2>
        <p>Enter an address and download a ZIP with the available PDFs (deed, tax bill, property record card).</p>
        <form method="post" action="/download">
          <label>Property address</label><br/>
          <input name="address" style="width: 100%; padding: 10px; font-size: 16px;" placeholder="133 Manorly Ln, Mooresville, NC" />
          <br/><br/>
          <button type="submit" style="padding: 10px 14px; font-size: 16px;">Download ZIP</button>
        </form>
        <p style="color:#666; margin-top: 16px;">
          Note: This uses browser automation (Playwright). First request can take a bit.
        </p>
      </body>
    </html>
    """


@app.post("/download")
async def download(address: str = Form(...)):
    address = address.strip()
    if len(address) < 5:
        raise HTTPException(status_code=400, detail="Please provide a valid address.")

    zip_bytes = await fetch_docs_as_zip(address)
    fn = _safe_filename(address) + "__iredell_docs.zip"
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


