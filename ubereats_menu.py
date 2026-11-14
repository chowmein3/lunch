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
from importlib.util import find_spec
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def as_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return as_text(value.get("text") or value.get("title"))
    return ""


def slugify(value):
    return re.sub(r"[^a-z0-9]+", "-", as_text(value).lower()).strip("-") or "item"


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
    stores = [find_store(data) for data in captured]
    scripts = page.eval_on_selector_all("script", "els => els.map(e => e.textContent || '')")
    for text in scripts:
        if "catalogSectionsMap" in text:
            data = decode_embedded(text)
            store = find_store(data) if data is not None else None
            if store:
                stores.append(store)
    stores = [store for store in stores if store]
    return max(stores, key=catalog_item_count, default=None)


def catalog_item_count(store):
    sections = store.get("catalogSectionsMap")
    if not isinstance(sections, dict):
        return 0
    count = 0
    for entries in sections.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            standard = payload.get("standardItemsPayload")
            if isinstance(standard, dict) and isinstance(standard.get("catalogItems"), list):
                count += len(standard["catalogItems"])
    return count


def parse_price(item):
    cents = item.get("price")
    if isinstance(cents, (int, float)) and not isinstance(cents, bool):
        return round(cents / 100, 2)

    text = as_text(item.get("priceTagline"))
    match = re.search(r"(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?", text)
    if not match:
        return None
    return round(float(match.group(0).replace(",", "")), 2)


def extract_items(store):
    """Turn catalogSectionsMap into a flat list of items."""
    if not isinstance(store, dict):
        return []
    sections = store.get("catalogSectionsMap")
    if not isinstance(sections, dict):
        return []

    entries = []
    for section in sections.values():
        if isinstance(section, list):
            entries.extend(entry for entry in section if isinstance(entry, dict))
        elif isinstance(section, dict):
            entries.append(section)
    # Real menu categories are VERTICAL_GRID; horizontal ones repeat "featured" items.
    entries.sort(key=lambda entry: 0 if entry.get("type") == "VERTICAL_GRID" else 1)

    items, seen = [], set()
    for entry in entries:
        entry_payload = entry.get("payload")
        if not isinstance(entry_payload, dict):
            continue
        payload = entry_payload.get("standardItemsPayload")
        if not isinstance(payload, dict):
            continue
        category = as_text(payload.get("title")) or "Menu"
        catalog_items = payload.get("catalogItems")
        if not isinstance(catalog_items, list):
            continue

        for it in catalog_items:
            if not isinstance(it, dict):
                continue
            uuid = as_text(it.get("uuid"))
            name = as_text(it.get("title"))
            description = as_text(it.get("itemDescription")).replace("\n", " ").strip()
            image_url = as_text(it.get("imageUrl"))
            uid = uuid or (category, name, description, image_url)
            if uid in seen:
                continue
            seen.add(uid)
            items.append({
                "uuid": uuid,
                "category": category,
                "name": name,
                "description": description,
                "price": parse_price(it),
                "image_url": image_url,
                "sold_out": bool(it.get("isSoldOut")),
            })
    return items


def download_images(items, out_dir):
    try:
        import requests
    except ImportError as exc:
        raise SystemExit("Requests is required. Install it with `pip install requests`.") from exc

    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = UA
    ok = 0
    for i, it in enumerate(items, 1):
        url = it["image_url"]
        it["image_file"] = ""
        if not url:
            continue
        try:
            if not url.startswith(("https://", "http://")):
                raise ValueError("unsupported image URL")
            r = s.get(url, timeout=20)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
            name = f"{i:03d}-{slugify(it['name'])[:50]}{ext}"
            (img_dir / name).write_bytes(r.content)
            it["image_file"] = f"images/{name}"
            ok += 1
        except (requests.RequestException, OSError, ValueError) as e:
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
            desc = as_text(it.get("description")).replace("|", "/")
            name = as_text(it.get("name")).replace("|", "/")
            price = it.get("price")
            price_text = f"{price:.2f}" if isinstance(price, (int, float)) else ""
            lines.append(f"{name} | {price_text} | {desc}".rstrip(" |"))
    (out_dir / "menu_for_app.txt").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Scrape an Uber Eats store menu.")
    ap.add_argument("url")
    ap.add_argument("--out", default="ubereats_out")
    ap.add_argument("--headless", action="store_true", help="hide the browser window")
    ap.add_argument("--no-images", action="store_true")
    args = ap.parse_args()

    if not args.no_images and find_spec("requests") is None:
        sys.exit("Requests is required. Install it with `python3 -m pip install requests`.")

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        sys.exit(f"Playwright is required. Install it with `python3 -m pip install playwright` "
                 f"and `python3 -m playwright install chromium`. ({exc})")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        store = None
        try:
            ctx = browser.new_context(user_agent=UA, locale="en-US",
                                      viewport={"width": 1280, "height": 900})
            page = ctx.new_page()

            def on_response(resp):
                if "getStoreV1" in resp.url:
                    try:
                        captured.append(resp.json())
                    except (PlaywrightError, ValueError):
                        print(f"Could not read store response: {resp.url}", file=sys.stderr)

            page.on("response", on_response)
            print(f"Opening {args.url}")
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeoutError:
                print("Page load timed out; checking whether menu data was received.", file=sys.stderr)

            best_count = 0
            stable_rounds = 0
            for _ in range(30):
                page.wait_for_timeout(1000)
                candidate = get_store(page, captured)
                count = catalog_item_count(candidate) if candidate else 0
                if candidate and count >= best_count:
                    if count == best_count:
                        stable_rounds += 1
                    else:
                        best_count = count
                        stable_rounds = 0
                    store = candidate
                if store and stable_rounds >= 3:
                    break
                page.mouse.wheel(0, 1200)
        finally:
            browser.close()

    if not store:
        sys.exit("Couldn't find menu data. Uber Eats may have blocked the browser or "
                 "changed its page format. Try without --headless, or open the page "
                 "manually first to clear any check.")

    items = extract_items(store)
    store_name = as_text(store.get("title")) or as_text(store.get("name")) or "Restaurant"
    print(f"{store_name}: {len(items)} items")
    if not items:
        sys.exit("Found the store but no items. The data layout may have changed.")

    if not args.no_images:
        download_images(items, out_dir)
    write_outputs(store_name, items, out_dir)
    print(f"Done. See {out_dir.resolve()}")


if __name__ == "__main__":
    main()