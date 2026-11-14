#!/usr/bin/env python3
"""
Pull the menu (items, prices, descriptions, photos) from an Uber Eats store page.

Setup:
    python3 -m pip install playwright requests
    python3 -m playwright install chromium

Usage:
    python ubereats_menu.py "https://www.ubereats.com/store/<name>/<id>"
    python ubereats_menu.py "<url>" --out chipotle --headless --no-images

Outputs (in --out, default ./ubereats_out):
    menu.json          full structured data
    menu.csv           spreadsheet-friendly
    menu_for_app.txt   paste into the Organizer "edit by hand" box of the team order app
    images/            one photo per item (unless --no-images)

Notes:
- Uber Eats has no public API and uses bot detection. This uses a real browser
  (Chromium via Playwright), which is the most reliable approach. It runs visible by
  default because headless is more likely to get blocked. Try --headless if you like.
- The page data format is unofficial and can change. If it stops working, the
  fix is almost always in extract_items().
- Keep it to personal or internal use. Check Uber Eats' terms, and remember the
  photos belong to the restaurants.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "item"


def find_store(obj):
    """Walk any JSON and return the dict that holds 'catalogSectionsMap'."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if isinstance(cur.get("catalogSectionsMap"), dict):
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def decode_embedded(txt):
    """Uber embeds store data in a <script> as an escaped JSON string. Try a few decodings."""
    txt = txt.strip()
    candidates = []
    try:
        s = json.loads('"' + txt + '"')
        candidates += [s.replace("%5C", "\\"), s]
    except Exception:
        pass
    candidates.append(txt)
    for c in candidates:
        try:
            return json.loads(c)
        except Exception:
            continue
    return None


def get_store(page, captured):
    # 1) API responses the page made while loading
    for data in captured:
        store = find_store(data)
        if store:
            return store
    # 2) Data embedded in the page's own <script> tags
    scripts = page.eval_on_selector_all("script", "els => els.map(e => e.textContent || '')")
    for text in scripts:
        if "catalogSectionsMap" in text:
            data = decode_embedded(text)
            store = find_store(data) if data else None
            if store:
                return store
    return None


def extract_items(store):
    """Turn catalogSectionsMap into a flat list of items."""
    entries = []
    for section in store.get("catalogSectionsMap", {}).values():
        entries.extend(section or [])
    # Real menu categories are VERTICAL_GRID; horizontal ones repeat "featured" items.
    entries.sort(key=lambda e: 0 if e.get("type") == "VERTICAL_GRID" else 1)

    items, seen = [], set()
    for entry in entries:
        payload = (entry.get("payload") or {}).get("standardItemsPayload") or {}
        category = ((payload.get("title") or {}).get("text")) or "Menu"
        for it in payload.get("catalogItems") or []:
            uid = it.get("uuid") or f"{category}-{it.get('title')}"
            if uid in seen:
                continue
            seen.add(uid)
            cents = it.get("price")
            if cents is None:
                m = re.search(r"([\d]+(?:\.\d+)?)", (it.get("priceTagline") or {}).get("text") or "")
                price = float(m.group(1)) if m else 0.0
            else:
                price = cents / 100
            items.append({
                "uuid": it.get("uuid"),
                "category": category,
                "name": it.get("title") or "",
                "description": (it.get("itemDescription") or "").replace("\n", " "),
                "price": round(price, 2),
                "image_url": it.get("imageUrl") or "",
                "sold_out": bool(it.get("isSoldOut")),
            })
    return items


def download_images(items, out_dir):
    img_dir = out_dir / "images"
    img_dir.mkdir(exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = UA
    ok = 0
    for i, it in enumerate(items, 1):
        url = it["image_url"]
        it["image_file"] = ""
        if not url:
            continue
        try:
            r = s.get(url, timeout=20)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
            name = f"{i:03d}-{slugify(it['name'])[:50]}{ext}"
            (img_dir / name).write_bytes(r.content)
            it["image_file"] = f"images/{name}"
            ok += 1
        except Exception as e:
            print(f"  image failed for {it['name']}: {e}", file=sys.stderr)
    print(f"Downloaded {ok}/{len(items)} images")


def write_outputs(store_name, items, out_dir):
    (out_dir / "menu.json").write_text(
        json.dumps({"store": store_name, "items": items}, indent=2, ensure_ascii=False), encoding="utf-8")

    with open(out_dir / "menu.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["category", "name", "price", "description",
                                          "image_url", "image_file", "sold_out", "uuid"],
                           extrasaction="ignore")
        w.writeheader()
        for it in items:
            w.writerow({**it, "image_file": it.get("image_file", "")})

    # Format the team-order app's menu box understands
    lines, cats = [], []
    for it in items:
        if it["category"] not in cats:
            cats.append(it["category"])
    for c in cats:
        lines.append(f"# {c}")
        for it in (i for i in items if i["category"] == c):
            desc = it["description"].replace("|", "/")
            lines.append(f"{it['name'].replace('|', '/')} | {it['price']:.2f} | {desc}".rstrip(" |"))
    (out_dir / "menu_for_app.txt").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Scrape an Uber Eats store menu.")
    ap.add_argument("url")
    ap.add_argument("--out", default="ubereats_out")
    ap.add_argument("--headless", action="store_true", help="hide the browser window")
    ap.add_argument("--no-images", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        def on_response(resp):
            if "getStoreV1" in resp.url:
                try:
                    captured.append(resp.json())
                except Exception:
                    pass

        page.on("response", on_response)
        print(f"Opening {args.url}")
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)

        store = None
        for _ in range(30):  # up to ~30s, in case of a bot check or slow load
            page.wait_for_timeout(1000)
            store = get_store(page, captured)
            if store:
                break
            page.mouse.wheel(0, 1200)  # nudge lazy loading
        browser.close()

    if not store:
        sys.exit("Couldn't find menu data. Uber Eats may have blocked the browser or "
                 "changed its page format. Try without --headless, or open the page "
                 "manually first to clear any check.")

    items = extract_items(store)
    store_name = store.get("title") or "Restaurant"
    print(f"{store_name}: {len(items)} items")
    if not items:
        sys.exit("Found the store but no items. The data layout may have changed.")

    if not args.no_images:
        download_images(items, out_dir)
    write_outputs(store_name, items, out_dir)
    print(f"Done. See {out_dir.resolve()}")


if __name__ == "__main__":
    main()