"""Amazon.eg client: fetches and parses search pages and offer listings.

Only talks to https://www.amazon.eg. No CAPTCHA solving, no proxy tricks:
if Amazon answers with a robot check the client raises `Blocked` and the
run stops politely until the next schedule.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

BASE = "https://www.amazon.eg"
LANG = "en_AE"  # English pages => stable strings to parse ("Used - Very Good")
USED_CONDITION = "p_n_condition-type:28071524031"  # the "Used" filter on amazon.eg

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}

BLOCK_MARKERS = (
    "validateCaptcha",
    "api-services-support@amazon.com",
    "Enter the characters you see below",
    "opfcaptcha",
)

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
MONEY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
EGP_RE = re.compile(r"EGP\s*(\d[\d,]*(?:\.\d+)?)")


class Blocked(Exception):
    """Amazon refused to serve the page (robot check / throttling)."""


@dataclass
class Offer:
    condition: str
    price: float | None
    seller: str | None

    @property
    def is_used(self) -> bool:
        # Anything that is not plain "New" counts: Used - Like New / Very Good /
        # Good / Acceptable, Renewed, Refurbished, Open Box.
        return bool(self.condition) and not self.condition.lower().startswith("new")


def parse_money(text: str | None) -> float | None:
    if not text:
        return None
    text = text.replace("\xa0", " ")
    m = EGP_RE.search(text) or MONEY_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def looks_blocked(html: str) -> bool:
    return any(marker in html for marker in BLOCK_MARKERS)


def parse_search(html: str) -> tuple[list[dict], bool]:
    """Returns (cards, has_next_page)."""
    soup = BeautifulSoup(html, "html.parser")
    cards: list[dict] = []
    for el in soup.select('div[data-component-type="s-search-result"]'):
        asin = (el.get("data-asin") or "").strip()
        if not ASIN_RE.match(asin):
            continue
        h2 = el.select_one("h2")
        title = h2.get_text(" ", strip=True) if h2 else ""
        scope = el.select_one('[data-cy="price-recipe"]') or el
        main_el = scope.select_one("span.a-price:not(.a-text-price) span.a-offscreen")
        list_el = scope.select_one("span.a-price.a-text-price span.a-offscreen")
        sec_el = el.select_one('[data-cy="secondary-offer-recipe"]')
        img = el.select_one("img.s-image")
        cards.append(
            {
                "asin": asin,
                "title": title,
                "image": img.get("src") if img else None,
                "main_price": parse_money(main_el.get_text()) if main_el else None,
                "list_price": parse_money(list_el.get_text()) if list_el else None,
                "secondary_price": parse_money(sec_el.get_text(" ")) if sec_el else None,
            }
        )
    nxt = soup.select_one("a.s-pagination-next")
    has_next = bool(nxt) and "s-pagination-disabled" not in (nxt.get("class") or [])
    return cards, has_next


def parse_offers(html: str) -> list[Offer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: list[Offer] = []
    for el in soup.select("#aod-pinned-offer, #aod-offer"):
        head = el.select_one("#aod-offer-heading")
        condition = re.sub(r"\s+", " ", head.get_text(" ", strip=True)) if head else ""
        price = None
        price_el = el.select_one('[id^="aod-price-"]')
        if price_el:
            label = price_el.select_one(".aok-offscreen")
            price = parse_money(label.get_text()) if label else None
            if price is None:
                whole = price_el.select_one(".a-price-whole")
                frac = price_el.select_one(".a-price-fraction")
                if whole:
                    price = parse_money(
                        whole.get_text(strip=True).rstrip(".") + "." + (frac.get_text(strip=True) if frac else "00")
                    )
        seller = None
        sold_by = el.select_one("#aod-offer-soldBy")
        if sold_by:
            link = sold_by.select_one("a")
            if link:
                seller = link.get_text(" ", strip=True)
            else:
                text = re.sub(r"\s+", " ", sold_by.get_text(" ", strip=True))
                seller = re.sub(r"^Sold by\s*", "", text).split("Seller rating")[0].strip() or None
        offers.append(Offer(condition=condition, price=price, seller=seller))
    return offers


class AmazonClient:
    def __init__(self, delay: tuple[float, float] = (1.5, 3.0), timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self.timeout = timeout
        self.requests_made = 0

    def _get(self, path: str, params: dict) -> str:
        last_reason = "unknown"
        for attempt in range(3):
            if self.requests_made:
                time.sleep(random.uniform(*self.delay))
            if attempt:
                time.sleep(8 * attempt)  # back off before retrying
            self.requests_made += 1
            try:
                r = self.session.get(BASE + path, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_reason = f"network error: {exc.__class__.__name__}"
                continue
            if r.status_code == 200 and not looks_blocked(r.text):
                return r.text
            if r.status_code == 404:
                return ""
            last_reason = "robot check" if looks_blocked(r.text) else f"HTTP {r.status_code}"
        raise Blocked(last_reason)

    def search_used(self, low: int, high: int, page: int) -> tuple[list[dict], bool]:
        """Used-condition products whose cheapest offer is between low..high EGP."""
        html = self._get(
            "/s",
            {
                # p_36 is in piasters and filters on the cheapest offer price
                "rh": f"{USED_CONDITION},p_36:{int(low * 100)}-{int(high * 100)}",
                "page": page,
                "language": LANG,
            },
        )
        if html and "s-main-slot" not in html and "s-search-result" not in html:
            raise Blocked("unexpected search page layout")
        return parse_search(html)

    def offers(self, asin: str) -> list[Offer]:
        """All offers (new + used) of one product, cheapest first."""
        html = self._get(
            "/gp/product/ajax/aodAjaxMain/",
            {"asin": asin, "pc": "dp", "experienceId": "aodAjaxMain", "language": LANG},
        )
        return parse_offers(html)


def product_url(asin: str) -> str:
    # ?aod=1 opens the product page with the "all offers" panel (used offers) visible
    return f"{BASE}/dp/{asin}?aod=1"
