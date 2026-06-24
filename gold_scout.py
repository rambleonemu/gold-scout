#!/usr/bin/env python3
"""
gold_scout.py  --  the engine

Fetches the live gold price, pulls eBay gold listings, throws out anything that
is not solid gold (plated, filled, or set with gems/stones), keeps only listings
that cost LESS PER GRAM than today's price, scores each one, and writes
results.json for the dashboard to display. Also writes CSVs you can open in Excel.

Setup (one time)
  1. eBay developer account at developer.ebay.com -> create a production keyset
  2. Copy the App ID (Client ID) and Cert ID (Secret)
  3. pip install requests
  4. Set your keys, then run:
       export EBAY_CLIENT_ID=...   EBAY_CLIENT_SECRET=...
       python3 gold_scout.py
  Put it on a cron line to refresh automatically. Run from a US IP (your home
  server over Tailscale) so listings and shipping match a US buyer.

The gold price is pulled live from gold-api.com (free, no key). "Today's price"
per gram = spot / 31.1035 x karat purity, which is what a Gold Price page shows.
"""

import os, re, csv, time, json, base64, smtplib, requests
from datetime import datetime, timezone
from email.message import EmailMessage

CLIENT_ID     = os.environ.get("EBAY_CLIENT_ID", "PASTE_APP_ID")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "PASTE_CERT_ID")

CONFIG = {
    "payout_pct":     1.00,   # your buyer pays the full listed gold price (100% of melt)
    "trap_under_pct": 0.50,   # more than this far under price = likely fake/misweighed -> hidden
    "tax_pct":        0.0,    # sales tax you pay buying on eBay
    "max_price":      2000,
    "queries": [
        # every kind of gold item, per karat. "grams" biases toward listings
        # that state a weight, which we need to price them.
        "10k gold scrap grams", "10k gold chain grams", "10k gold ring grams",
        "10k gold pendant grams", "10k gold bracelet grams", "10k gold necklace grams",
        "10k gold earrings grams",
        "14k gold scrap grams", "14k gold chain grams", "14k gold ring grams",
        "14k gold pendant grams", "14k gold bracelet grams", "14k gold necklace grams",
        "14k gold earrings grams",
        "18k gold scrap grams", "18k gold chain grams", "18k gold ring grams",
        "18k gold pendant grams", "18k gold bracelet grams", "18k gold necklace grams",
        "18k gold earrings grams",
        "22k gold scrap grams", "22k gold chain grams", "22k gold bracelet grams",
        "24k gold scrap grams", "solid gold scrap lot grams",
    ],
    "results_per_query": 75,
    "json_out":  "results.json",
    "deals_csv": "gold_candidates.csv",
    "traps_csv": "gold_traps.csv",
}

# optional email-to-self; leave EMAIL_TO empty to skip
EMAIL = {
    "to": os.environ.get("EMAIL_TO", ""), "from": os.environ.get("EMAIL_FROM", ""),
    "smtp_host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
    "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
    "smtp_user": os.environ.get("SMTP_USER", ""),
    "smtp_pass": os.environ.get("SMTP_PASS", ""),  # use an app password, never your real one
    "min_score": 60,                               # email deals scoring at/above this
}

# instant phone push via ntfy.sh (free, no account). Subscribe to your topic in
# the ntfy app, then set NTFY_TOPIC. Leave empty to skip.
ALERT = {
    "ntfy_topic": os.environ.get("NTFY_TOPIC", ""),
    "min_score":  int(os.environ.get("ALERT_MIN_SCORE", "70")),
    "seen_file":  "seen_ids.json",
}

PURITY = {10: 10/24, 14: 14/24, 18: 18/24, 22: 22/24, 24: 0.999}
TROY = 31.1035

# Not solid gold: plated, filled, bonded, tone, vermeil, over silver.
NOT_SOLID = re.compile(
    r"\b(gold[\s-]?filled|gold[\s-]?plat(e|ed|ing)|g\.?f\.?\b|g\.?p\.?\b|"
    r"gold[\s-]?tone|gold[\s-]?color|plated|rolled\s?gold|vermeil|overlay|"
    r"hge|electroplate|bonded|over\s?sterling|over\s?silver|costume|fashion)\b", re.I)
# Has gems/stones (these add weight and cannot be scrapped). "Diamond cut" is a
# finish, not a stone, so it is removed before this test.
HAS_STONE = re.compile(
    r"\b(diamond|gemstone|gem|stone|stones|cz|cubic\s?zirconia|sapphire|ruby|"
    r"emerald|pearl|opal|topaz|amethyst|garnet|turquoise|jade|onyx|moissanite|"
    r"rhinestone|crystal|birthstone|set\s?with)\b", re.I)
# Contains a non-gold metal. Mixed pieces (gold + silver/platinum/steel) wreck
# the per-gram math because part of the weight is not gold.
NON_GOLD = re.compile(
    r"\b(silver|sterling|925|platinum|palladium|titanium|stainless|steel|"
    r"brass|copper|pewter|tungsten|bronze|nickel)\b", re.I)
KARAT_RE = re.compile(r"\b(10|14|18|22|24)\s?k(?:t|arat)?\b", re.I)
GRAM_RE  = re.compile(r"(\d+(?:\.\d+)?)\s?(?:g\b|gr\b|gram|grams)", re.I)


def live_spot_per_oz():
    r = requests.get("https://api.gold-api.com/price/XAU", timeout=20)
    r.raise_for_status()
    return float(r.json()["price"])


def is_solid_no_stones(title):
    if NOT_SOLID.search(title):
        return False
    if NON_GOLD.search(title):          # silver, platinum, steel, etc. -> mixed metal
        return False
    cleaned = re.sub(r"diamond[\s-]?cut", "", title, flags=re.I)  # finish, not a stone
    # drop negations so "no stones" / "without gems" are not read as having them
    cleaned = re.sub(r"\b(no|without|free\s?of|minus)\s+(stone|stones|gem|gems|"
                     r"gemstone|gemstones|diamond|diamonds)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(stone|gem|diamond)[\s-]?free\b", "", cleaned, flags=re.I)
    if HAS_STONE.search(cleaned):
        return False
    return True


def deal_score(under_by, payout_pct, trap_under):
    """0-100. 0 at break-even (buying = refiner payout), 100 just below the trap line."""
    be = 1 - payout_pct
    if trap_under <= be:
        return 0
    s = (under_by - be) / (trap_under - be) * 100
    return max(0, min(100, round(s)))


def get_token():
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = requests.post("https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def search(token, query, limit):
    out, offset = [], 0
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    while offset < limit:
        page = min(50, limit - offset)
        r = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers,
            params={"q": query,
                    "filter": "buyingOptions:{FIXED_PRICE},conditions:{USED|UNSPECIFIED}",
                    "sort": "price", "limit": page, "offset": offset}, timeout=30)
        if r.status_code != 200:
            print(f"  ! {query!r} -> HTTP {r.status_code}: {r.text[:120]}"); break
        items = r.json().get("itemSummaries", []) or []
        out.extend(items)
        if len(items) < page:
            break
        offset += page; time.sleep(0.2)
    return out


def evaluate(item, spot24, cfg):
    title = item.get("title", "")
    if not is_solid_no_stones(title):
        return None
    km, gm = KARAT_RE.search(title), GRAM_RE.search(title)
    if not km or not gm:
        return None
    karat = int(km.group(1)); grams = float(gm.group(1))
    price = float((item.get("price") or {}).get("value", 0) or 0)
    if price <= 0 or grams <= 0 or price > cfg["max_price"]:
        return None

    ship = 0.0
    for opt in item.get("shippingOptions", []) or []:
        c = (opt.get("shippingCost") or {}).get("value")
        if c is not None:
            ship = float(c); break

    page_per_g = PURITY[karat] * spot24
    cost = (price + ship) * (1 + cfg["tax_pct"])
    all_in_g = cost / grams
    if all_in_g >= page_per_g:                 # not cheaper per gram than today's price
        return None

    under_by = (page_per_g - all_in_g) / page_per_g
    is_trap = under_by >= cfg["trap_under_pct"]
    payout = page_per_g * grams * cfg["payout_pct"]
    return {
        "score": deal_score(under_by, cfg["payout_pct"], cfg["trap_under_pct"]),
        "under_pct": round(under_by * 100, 1),
        "trap": is_trap,
        "id": item.get("itemId", ""),
        "karat": f"{karat}K", "grams": grams, "price": round(price, 2), "ship": round(ship, 2),
        "all_in_per_g": round(all_in_g, 2), "page_per_g": round(page_per_g, 2),
        "profit": round(payout - cost, 2),
        "title": title[:110], "url": item.get("itemWebUrl", ""),
    }


def load_seen(path):
    try:
        with open(path) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(path, seen):
    try:
        with open(path, "w") as f:
            json.dump(sorted(seen), f)
    except Exception:
        pass


def send_alerts(deals):
    """Push only NEW listings scoring high enough, so you are not pinged twice."""
    topic = ALERT["ntfy_topic"]
    if not topic:
        return
    seen = load_seen(ALERT["seen_file"])
    fresh = [d for d in deals
             if d["score"] >= ALERT["min_score"] and (d["id"] or d["url"]) not in seen]
    for d in fresh:
        try:
            requests.post(
                f"https://ntfy.sh/{topic}",
                data=(f"{d['karat']} {d['grams']}g  ${d['price']:.0f}  "
                      f"{d['under_pct']}% under  est ${d['profit']:.0f}\n{d['title']}").encode("utf-8"),
                headers={"Title": f"Gold deal · score {d['score']}",
                         "Priority": "high", "Tags": "moneybag", "Click": d["url"]},
                timeout=15)
            seen.add(d["id"] or d["url"])
        except Exception as e:
            print(f"  ! alert failed: {e}")
    if fresh:
        print(f"alerted {len(fresh)} new deal(s) via ntfy")
    save_seen(ALERT["seen_file"], seen)


def send_email(deals, prices):
    if not EMAIL["to"] or not EMAIL["smtp_user"]:
        return
    keep = [d for d in deals if d["score"] >= EMAIL["min_score"]]
    if not keep:
        return
    lines = [f"{len(keep)} solid-gold deals scoring {EMAIL['min_score']}+:", ""]
    for d in keep[:40]:
        lines.append(f"[{d['score']:3}] {d['under_pct']}% under  {d['karat']} {d['grams']}g  "
                     f"${d['price']:.0f}  profit ${d['profit']:.0f}\n  {d['title']}\n  {d['url']}")
    msg = EmailMessage()
    msg["Subject"] = f"Gold Scout: {len(keep)} deals"
    msg["From"], msg["To"] = EMAIL["from"] or EMAIL["smtp_user"], EMAIL["to"]
    msg.set_content("\n".join(lines))
    with smtplib.SMTP_SSL(EMAIL["smtp_host"], EMAIL["smtp_port"]) as s:
        s.login(EMAIL["smtp_user"], EMAIL["smtp_pass"]); s.send_message(msg)
    print(f"emailed {len(keep)} deals to {EMAIL['to']}")


def main():
    if "PASTE_" in CLIENT_ID or "PASTE_" in CLIENT_SECRET:
        print("Add your eBay CLIENT_ID and CLIENT_SECRET first (see header)."); return

    spot_oz = live_spot_per_oz()
    spot24 = spot_oz / TROY
    prices = {f"{k}K": round(PURITY[k] * spot24, 2) for k in PURITY}
    print(f"Live gold: ${spot_oz:.2f}/oz  ->  24K ${spot24:.2f}/g")

    token = get_token()
    rows, seen = [], set()
    for q in CONFIG["queries"]:
        print(f"Searching: {q}")
        for item in search(token, q, CONFIG["results_per_query"]):
            iid = item.get("itemId")
            if iid in seen:
                continue
            seen.add(iid)
            row = evaluate(item, spot24, CONFIG)
            if row:
                rows.append(row)

    deals = sorted([r for r in rows if not r["trap"]], key=lambda r: r["score"], reverse=True)
    traps = sorted([r for r in rows if r["trap"]], key=lambda r: r["under_pct"], reverse=True)

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "spot_per_oz": round(spot_oz, 2),
        "prices_per_gram": prices,
        "payout_pct": CONFIG["payout_pct"],
        "deals": deals,
        "traps_count": len(traps),
    }
    with open(CONFIG["json_out"], "w") as f:
        json.dump(payload, f, indent=2)

    cols = ["score","under_pct","karat","grams","price","ship","all_in_per_g",
            "page_per_g","profit","title","url"]
    for path, data in [(CONFIG["deals_csv"], deals), (CONFIG["traps_csv"], traps)]:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader(); w.writerows(data)

    print(f"\n{len(deals)} solid-gold deals under price -> {CONFIG['json_out']} / {CONFIG['deals_csv']}")
    print(f"{len(traps)} hidden as too-good-to-be-true -> {CONFIG['traps_csv']}")
    for d in deals[:10]:
        print(f"  [{d['score']:3}] {d['under_pct']:4}% under  {d['karat']} {d['grams']}g  "
              f"${d['price']:.0f}  profit ${d['profit']:.0f}  {d['title'][:60]}")

    send_alerts(deals)
    send_email(deals, prices)


if __name__ == "__main__":
    main()
