"""Amazon Egypt used-deals tracker.

One run = scan a slice of the "Used" catalogue on amazon.eg, verify the
promising products on their offers page, update web/data/deals.json for the
dashboard, and send a WhatsApp alert for every deal not alerted before.

Usage:
  python scraper/tracker.py                 normal run (what GitHub Actions does)
  python scraper/tracker.py --dry-run       scan and print, write nothing, send nothing
  python scraper/tracker.py --no-notify     write files but do not send WhatsApp
  python scraper/tracker.py --min-discount 40 --max-price 500 --pages 5
  python scraper/tracker.py --test-whatsapp send one test message and exit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from amazon import AmazonClient, Blocked, product_url  # noqa: E402
import notify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "scraper" / "config.json"
STATE_PATH = ROOT / "data" / "state.json"
DEALS_PATH = ROOT / "web" / "data" / "deals.json"

# Price bands (EGP). Amazon shows at most ~7 pages per search, so the catalogue
# is split into narrow bands and the tracker walks through them run after run.
BAND_EDGES = [0, 30, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500, 600, 700, 850,
              1000, 1250, 1500, 2000, 2500, 3000, 4000, 5000, 7500, 10000]
MAX_PAGES_PER_BAND = 20
MAX_QUEUE = 300
MAX_RUNTIME_SECONDS = 480
BLOCKED_ALERT_AFTER = 6  # consecutive blocked runs (~3 hours) before warning on WhatsApp

KIND_AR = {
    "used": "مستعمل",
    "open box": "علبة مفتوحة",
    "renewed": "مُجدَّد",
    "refurbished": "مُجدَّد",
}
GRADE_AR = {
    "like new": "كالجديد",
    "very good": "جيد جداً",
    "good": "جيد",
    "acceptable": "مقبول",
}


def condition_ar(condition: str) -> str:
    """'Open Box - Very Good' -> 'علبة مفتوحة - جيد جداً'."""
    kind, _, grade = (part.strip() for part in condition.partition("-"))
    kind_ar = KIND_AR.get(kind.lower())
    if not kind_ar:
        return condition
    grade_ar = GRADE_AR.get(grade.lower())
    return f"{kind_ar} - {grade_ar}" if grade_ar else kind_ar


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def build_bands(min_price: float, max_price: float) -> list[tuple[float, float]]:
    edges = [min_price] + [e for e in BAND_EDGES if min_price < e < max_price] + [max_price]
    return list(zip(edges, edges[1:]))


def reference_price(new_price, list_price, basis: str):
    """The 'before' price the discount is measured against."""
    if basis == "new":
        return new_price
    if basis == "list":
        return list_price
    values = [v for v in (new_price, list_price) if v]
    return max(values) if values else None


def prefilter(card: dict, cfg: dict):
    """Cheap check on a search card. Returns the preliminary discount or None."""
    prices = [p for p in (card.get("main_price"), card.get("secondary_price")) if p]
    if not prices:
        return None
    best = min(prices)
    ref = reference_price(card.get("main_price"), card.get("list_price"), cfg["discount_basis"])
    if not ref or best >= cfg["max_price"]:
        return None
    discount = (1 - best / ref) * 100
    return discount if discount >= cfg["min_discount"] else None


def band_cap(cfg: dict) -> float:
    return max(cfg["max_price"], cfg["min_price"] + 1)


def evaluate(asin: str, info: dict, offers: list, cfg: dict, now: datetime):
    """Strict check on the real offers. Returns a deal dict or None."""
    used = [o for o in offers if o.is_used and o.price]
    if not used:
        return None
    best = min(used, key=lambda o: o.price)
    new_prices = [o.price for o in offers if not o.is_used and o.price]
    new_price = min(new_prices) if new_prices else None
    ref = reference_price(new_price, info.get("list_price"), cfg["discount_basis"])
    if not ref or ref <= best.price:
        return None
    discount = (1 - best.price / ref) * 100
    if best.price >= cfg["max_price"] or best.price < cfg["min_price"] or discount < cfg["min_discount"]:
        return None
    return {
        "asin": asin,
        "title": info.get("title") or asin,
        "image": info.get("image"),
        "url": product_url(asin),
        "price": best.price,
        "condition": best.condition,
        "condition_ar": condition_ar(best.condition),
        "seller": best.seller,
        "new_price": new_price,
        "list_price": info.get("list_price"),
        "reference_price": ref,
        "discount": round(discount, 1),
        "first_seen": info.get("first_seen") or iso(now),
        "last_checked": iso(now),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Amazon Egypt used-deals tracker")
    p.add_argument("--dry-run", action="store_true", help="scan and print only")
    p.add_argument("--no-notify", action="store_true", help="do not send WhatsApp")
    p.add_argument("--min-discount", type=float)
    p.add_argument("--max-price", type=float)
    p.add_argument("--pages", type=int, help="search pages to scan in this run")
    p.add_argument("--test-whatsapp", action="store_true", help="send a test message and exit")
    return p.parse_args()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parse_args()

    if args.test_whatsapp:
        if not notify.is_configured():
            print("WHATSAPP_PHONE / CALLMEBOT_APIKEY are not set.")
            return 1
        ok = notify.send_whatsapp("✅ تم ربط واتساب بنجاح مع متتبع عروض أمازون مصر.")
        print("test message sent" if ok else "test message FAILED")
        return 0 if ok else 1

    cfg = load_json(CONFIG_PATH, {})
    cfg.setdefault("max_price", 1000)
    cfg.setdefault("min_price", 0)
    cfg.setdefault("min_discount", 70)
    cfg.setdefault("discount_basis", "highest")
    cfg.setdefault("pages_per_run", 20)
    cfg.setdefault("max_verifications_per_run", 40)
    cfg.setdefault("max_rechecks_per_run", 25)
    cfg.setdefault("max_whatsapp_per_run", 8)
    cfg.setdefault("renotify_price_drop_percent", 10)
    cfg.setdefault("forget_after_days", 30)
    cfg.setdefault("request_delay_seconds", [1.5, 3.0])
    if args.min_discount is not None:
        cfg["min_discount"] = args.min_discount
    if args.max_price is not None:
        cfg["max_price"] = args.max_price
    if args.pages is not None:
        cfg["pages_per_run"] = args.pages

    started = time.monotonic()
    now = now_utc()
    state = load_json(STATE_PATH, {})
    state.setdefault("cursor", {"band": 0, "page": 1})
    state.setdefault("queue", [])
    state.setdefault("notified", {})
    state.setdefault("sweeps_completed", 0)
    state.setdefault("blocked_streak", 0)
    data = load_json(DEALS_PATH, {})
    active: dict[str, dict] = {d["asin"]: d for d in data.get("deals", []) if d.get("asin")}
    previous_meta = data.get("meta", {})

    bands = build_bands(cfg["min_price"], band_cap(cfg))
    cursor = state["cursor"]
    if cursor.get("band", 0) >= len(bands):
        cursor.update(band=0, page=1)

    client = AmazonClient(delay=tuple(cfg["request_delay_seconds"]))
    stats = {"pages": 0, "products": 0, "candidates": 0, "verified": 0, "new_deals": 0, "expired": 0, "alerts": 0}
    queue: dict[str, dict] = {item["asin"]: item for item in state["queue"] if item.get("asin")}
    checked: set[str] = set()
    status, block_reason = "ok", None

    def out_of_time() -> bool:
        return time.monotonic() - started > MAX_RUNTIME_SECONDS

    try:
        # 1) Walk the used catalogue from where the previous run stopped.
        while stats["pages"] < cfg["pages_per_run"] and not out_of_time():
            low, high = bands[cursor["band"]]
            cards, has_next = client.search_used(low, high, cursor["page"])
            stats["pages"] += 1
            stats["products"] += len(cards)
            hits = 0
            for card in cards:
                prelim = prefilter(card, cfg)
                if prelim is None:
                    continue
                hits += 1
                card["prelim_discount"] = round(prelim, 1)
                queue[card["asin"]] = card
            print(f"band {low:g}-{high:g} EGP, page {cursor['page']}: {len(cards)} products, {hits} candidates")
            if cards and has_next and cursor["page"] < MAX_PAGES_PER_BAND:
                cursor["page"] += 1
            else:
                cursor["page"] = 1
                cursor["band"] += 1
                if cursor["band"] >= len(bands):
                    cursor["band"] = 0
                    state["sweeps_completed"] += 1
                    state["last_sweep_completed_at"] = iso(now_utc())
        stats["candidates"] = len(queue)

        # 2) Verify candidates on their real offers page (best first).
        todo = sorted(queue.values(), key=lambda c: c.get("prelim_discount", 0), reverse=True)
        for card in todo[: cfg["max_verifications_per_run"]]:
            if out_of_time():
                break
            asin = card["asin"]
            offers = client.offers(asin)
            queue.pop(asin, None)
            checked.add(asin)
            stats["verified"] += 1
            info = dict(card)
            if asin in active:
                info["first_seen"] = active[asin].get("first_seen")
            deal = evaluate(asin, info, offers, cfg, now_utc())
            if deal:
                if asin not in active:
                    stats["new_deals"] += 1
                active[asin] = deal
                print(f"  DEAL {asin}: {deal['price']:g} EGP, -{deal['discount']:g}% ({deal['condition']}) {deal['title'][:60]}")
            elif asin in active:
                del active[asin]
                stats["expired"] += 1

        # 3) Re-check deals already on the dashboard so sold-out ones disappear.
        stale = sorted((d for a, d in active.items() if a not in checked), key=lambda d: d.get("last_checked", ""))
        for old in stale[: cfg["max_rechecks_per_run"]]:
            if out_of_time():
                break
            asin = old["asin"]
            offers = client.offers(asin)
            deal = evaluate(asin, old, offers, cfg, now_utc())
            if deal:
                active[asin] = deal
            else:
                del active[asin]
                stats["expired"] += 1
                print(f"  expired {asin}")
    except Blocked as exc:
        status, block_reason = "blocked", str(exc)
        print(f"::warning::Amazon blocked this run ({exc}). Will try again on the next schedule.")

    state["queue"] = sorted(queue.values(), key=lambda c: c.get("prelim_discount", 0), reverse=True)[:MAX_QUEUE]
    deals = sorted(active.values(), key=lambda d: d["discount"], reverse=True)

    # 4) WhatsApp alerts - each deal once; again only if its price drops further.
    notified: dict[str, dict] = state["notified"]
    can_notify = not args.dry_run and not args.no_notify and notify.is_configured()
    if not args.dry_run and not args.no_notify and not notify.is_configured():
        print("WhatsApp secrets are not set - alerts skipped (deals are still saved).")
    drop = cfg["renotify_price_drop_percent"] / 100
    for deal in deals:
        asin = deal["asin"]
        previous = notified.get(asin)
        price_drop = bool(previous) and deal["price"] <= previous["price"] * (1 - drop)
        if previous:
            previous["last_active"] = iso(now)
            if not price_drop:
                continue
        if not can_notify or stats["alerts"] >= cfg["max_whatsapp_per_run"]:
            continue  # stays un-notified, will be sent on a later run
        if notify.send_whatsapp(notify.deal_message(deal, price_drop=price_drop)):
            notified[asin] = {"price": deal["price"], "at": iso(now), "last_active": iso(now)}
            stats["alerts"] += 1

    cutoff = now - timedelta(days=cfg["forget_after_days"])
    for asin in [a for a, n in notified.items() if a not in active]:
        seen = parse_iso(notified[asin].get("last_active") or notified[asin].get("at"))
        if not seen or seen < cutoff:
            del notified[asin]

    if status == "blocked":
        state["blocked_streak"] += 1
        if state["blocked_streak"] == BLOCKED_ALERT_AFTER and can_notify:
            notify.send_whatsapp(
                "⚠️ متتبع عروض أمازون مصر: أمازون بيرفض الطلبات من حوالي 3 ساعات. "
                "النظام هيفضل يحاول تلقائياً - راجع ملف README لو المشكلة استمرت."
            )
    else:
        state["blocked_streak"] = 0

    meta = {
        "updated_at": iso(now_utc()),
        "status": status,
        "block_reason": block_reason,
        "last_success_at": iso(now_utc()) if status == "ok" else previous_meta.get("last_success_at"),
        "filters": {
            "max_price": cfg["max_price"],
            "min_price": cfg["min_price"],
            "min_discount": cfg["min_discount"],
            "discount_basis": cfg["discount_basis"],
        },
        "run": stats,
        "sweep": {
            "band": cursor["band"] + 1,
            "bands": len(bands),
            "completed": state["sweeps_completed"],
            "last_completed_at": state.get("last_sweep_completed_at"),
        },
    }

    summary = (
        f"status={status} pages={stats['pages']} products={stats['products']} "
        f"candidates={stats['candidates']} verified={stats['verified']} "
        f"active_deals={len(deals)} new={stats['new_deals']} expired={stats['expired']} "
        f"alerts={stats['alerts']} requests={client.requests_made} "
        f"time={time.monotonic() - started:.0f}s"
    )
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Amazon EG tracker\n\n`{summary}`\n")

    if args.dry_run:
        print("dry run - nothing written.")
        return 0
    save_json(DEALS_PATH, {"meta": meta, "deals": deals})
    save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
