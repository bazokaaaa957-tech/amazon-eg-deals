"""WhatsApp alerts through the free CallMeBot API.

Needs two environment variables (stored as GitHub Secrets, never in code):
  WHATSAPP_PHONE     e.g. +2010xxxxxxxx
  CALLMEBOT_APIKEY   the key CallMeBot sends you on WhatsApp
"""
from __future__ import annotations

import os
import time

import requests

API_URL = "https://api.callmebot.com/whatsapp.php"


def is_configured() -> bool:
    return bool(os.environ.get("WHATSAPP_PHONE") and os.environ.get("CALLMEBOT_APIKEY"))


def send_whatsapp(text: str) -> bool:
    phone = os.environ.get("WHATSAPP_PHONE", "").strip()
    apikey = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if not phone or not apikey:
        return False
    for attempt in range(2):
        try:
            r = requests.get(
                API_URL,
                params={"phone": phone, "text": text, "apikey": apikey},
                timeout=30,
            )
            body = r.text.lower()
            if r.status_code == 200 and "invalid" not in body and "error" not in body:
                time.sleep(2)  # CallMeBot asks not to flood
                return True
            print(f"  whatsapp: CallMeBot answered HTTP {r.status_code}")
        except requests.RequestException as exc:
            print(f"  whatsapp: network error ({exc.__class__.__name__})")
        time.sleep(5)
    return False


def _money(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def deal_message(deal: dict, price_drop: bool = False) -> str:
    title = deal["title"]
    if len(title) > 110:
        title = title[:107].rstrip() + "..."
    head = "📉 *السعر نزل تاني!*" if price_drop else "🔥 *عرض جديد - أمازون مصر*"
    lines = [
        head,
        "",
        f"📦 {title}",
        f"💰 السعر: *{_money(deal['price'])} ج.م* ({deal['condition_ar']})",
        f"🏷️ الخصم: *{int(deal['discount'])}%* (بدل {_money(deal['reference_price'])} ج.م)",
    ]
    if deal.get("seller"):
        lines.append(f"🏪 البائع: {deal['seller']}")
    lines += ["", f"🛒 {deal['url']}"]
    return "\n".join(lines)
