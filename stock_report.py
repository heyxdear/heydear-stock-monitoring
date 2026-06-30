#!/usr/bin/env python3
"""
Printeers Stock Monitoring
Fetches warehouse stock from the Printeers v2 API, saves a daily snapshot,
compares to the previous snapshot, groups SKUs by product category, and
posts a Slack Block Kit report (category headers + per-category tables).

Stock columns:
  Stock = physicalQuantity (units physically in the warehouse)
  Avail = logicalQuantity  (available = physical minus reserved open orders)

Usage:
  python3 stock_report.py [--weekly] [--post] [--no-save]
"""
import json, os, sys, glob, urllib.request, urllib.error
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG   = os.path.join(BASE_DIR, "config", "printeers-config.txt")
SNAPDIR  = os.path.join(BASE_DIR, "snapshots")
LOWSTOCK_DAYS = 14
RUNRATE_WINDOW = 7
MAX_NAME = 30
# SKUs, die wegen Printeers-SKU-Logik nicht beruecksichtigt werden
EXCLUDE_SKUS = {"HEYDEA2007", "HEYDEA2008", "HEYDEA2009"}  # standalone pouches

# Component coverage rules: the "component" stock must always be >= the "driver"
# stock (1:1 dependency). "technical" SKUs are duplicates shown for context but
# not counted as real stock. Add further pairs here as needed.
COMPONENT_RULES = [
    {
        "label": "Dog tag keychain needs a lobster claw chain",
        "driver": "HEYDEA0003",          # real sellable stock (Front image)
        "technical": ["HEYDEA0004"],     # Back image — technical duplicate only
        "component": "HEYDEA1003",       # lobster claw — must stay >= driver
    },
    {
        "label": "Every acrylic ornament needs a silver hanger",
        "driver_match": "acrylic ornament",  # sum of all acrylic ornament SKUs
        "driver_label": "all acrylic ornaments",
        "component": "HEYDEA1001",           # ornament hanger silver — >= total
    },
    {
        "label": "Shopping cart keychain needs its (larger) lobster claw chain",
        "driver": "HEYDEA0016",              # Keychain steel shopping cart
        "component": "HEYDEA1015",           # bigger lobster claw chain — on the way
    },
]

# Tracked component/driver SKUs are always shown, even at 0 stock
ALWAYS_SHOW = set()
for _r in COMPONENT_RULES:
    ALWAYS_SHOW.add(_r["component"])
    if "driver" in _r:
        ALWAYS_SHOW.add(_r["driver"])

# Category order + emoji + lowercase keyword matchers (checked top-to-bottom)
CATEGORIES = [
    ("ornaments", "✨", "Acrylic Ornaments", ["acrylic ornament", "ornament hanger", "ornament"]),
    ("bracelets", "📿", "Bracelets",         ["bracelet"]),
    ("keychains", "🔑", "Keychains",         ["keychain", "key chain", "lucky charm",
                                              "lobster claw", "dog tag", "shopping cart",
                                              "leather sublimation"]),
    ("bags",      "🎁", "Gift Bags & Packaging", ["bag", "pouch", "sleeve", "velvet",
                                              "satin", "drawstring", "linen"]),
    ("other",     "🗂️", "Other",             []),
]

# Visual sub-groups inside a category (daily report only). First match wins.
SUBGROUPS = {
    "keychains": [
        ("Dog Tag",       ["dog tag"]),          # front, back and lobster claw chain
        ("Sublimation",   ["sublimation"]),
        ("Lucky Charm",   ["lucky charm"]),
        ("Shopping Cart", ["shopping cart"]),
    ],
}

def split_subgroups(cat_key, cat_rows):
    defs = SUBGROUPS.get(cat_key)
    if not defs:
        return None
    used, groups = set(), []
    for label, kws in defs:
        sub = [r for r in cat_rows
               if id(r) not in used and any(k in r["name"].lower() for k in kws)]
        for r in sub: used.add(id(r))
        if sub: groups.append((label, sub))
    rest = [r for r in cat_rows if id(r) not in used]
    if rest: groups.append(("Other", rest))
    return groups

def categorize(name):
    n = name.lower()
    for key, emoji, label, kws in CATEGORIES:
        if any(kw in n for kw in kws):
            return key
    return "other"

def load_config(path):
    """Read config from file if present, then overlay environment variables.
    In CI (e.g. GitHub Actions) there is no file; secrets come from env vars."""
    cfg = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    for k in ("PRINTEERS_SECRET_KEY", "PRINTEERS_ENV", "SLACK_WEBHOOK_URL",
              "SLACK_BOT_NAME", "SLACK_BOT_ICON"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg

def fetch_items(secret, base_url):
    items, cursor = [], None
    while True:
        url = base_url + "/items" + (f"?cursor={cursor}" if cursor else "")
        req = urllib.request.Request(url, headers={"X-Printeers-Secret-Key": secret})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        items.extend(data.get("items", []))
        cursor = data.get("nextCursor")
        if not cursor: break
    return items

def post_to_slack(webhook, text, blocks, username, icon):
    payload = {"text": text, "blocks": blocks}
    if username: payload["username"] = username
    if icon:     payload["icon_emoji"] = icon
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")

def snap_path(d): return os.path.join(SNAPDIR, f"{d}.json")
def load_snapshot(p):
    with open(p) as f: return json.load(f).get("items", [])
def index_by_sku(items): return {i["sku"]: i for i in items}
def prior_snapshots(today_str):
    files = sorted(glob.glob(os.path.join(SNAPDIR, "*.json")))
    return [f for f in files if os.path.basename(f) != f"{today_str}.json"]
def fmt(n): return f"{n:,}".replace(",", ".")
def trunc(s, n):
    s = s.strip();  return s if len(s) <= n else s[:n-1] + "…"

def avg_runrate(sku, history):
    series = []
    for d, items in history:
        idx = index_by_sku(items)
        if sku in idx: series.append((d, idx[sku]["physicalQuantity"]))
    series.sort(); series = series[-(RUNRATE_WINDOW + 1):]
    deltas = [series[i-1][1] - series[i][1] for i in range(1, len(series))
              if series[i-1][1] - series[i][1] > 0]
    return (sum(deltas) / len(deltas)) if deltas else 0.0

# ---- Block Kit helpers ----
def b_header(text):  return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}
def b_section(md):   return {"type": "section", "text": {"type": "mrkdwn", "text": md[:2900]}}
def b_divider():     return {"type": "divider"}
def b_context(md):   return {"type": "context", "elements": [{"type": "mrkdwn", "text": md[:2900]}]}

def code_table(rows, cols):
    """rows: list of tuples; cols: list of (header, width, align).
    width=None means an unpadded final column (for full-length product names)."""
    def cell(v, w, a):
        if w is None: return str(v)
        return f"{v:>{w}}" if a == "r" else f"{v:<{w}}"
    header = "  ".join(cell(h, w, a) for h, w, a in cols)
    body = ["  ".join(cell(v, w, a) for v, (h, w, a) in zip(r, cols)) for r in rows]
    width = max([len(header)] + [len(b) for b in body])
    return "```\n" + header + "\n" + "-" * width + "\n" + "\n".join(body) + "\n```"

def category_table_text(cat_rows, weekly=False):
    # Numbers + SKU first (aligned), full product name LAST (unpadded, never cut)
    if weekly:
        cols = [("Sold", 6, "r"), ("Left", 6, "r"), ("SKU", 12, "l"), ("Product", None, "l")]
        data = [(fmt(r["sold"]), fmt(r["phys"]), r["sku"], r["name"].strip()) for r in cat_rows]
    else:
        cols = [("Stock", 6, "r"), ("Resv", 5, "r"), ("Sold", 6, "r"),
                ("SKU", 12, "l"), ("Product", None, "l")]
        data = []
        for r in cat_rows:
            sold = "" if r["delta"] is None else (fmt(r["delta"]) if r["delta"] > 0 else ("0" if r["delta"] == 0 else "+" + fmt(-r["delta"])))
            data.append((fmt(r["phys"]), fmt(r["reserved"]), sold, r["sku"], r["name"].strip()))
    return code_table(data, cols)

def category_table_block(cat_rows, weekly=False):
    return b_section(category_table_text(cat_rows, weekly))

def grouped(rows):
    """Return list of (key,emoji,label,cat_rows) in category order, skipping empties."""
    out = []
    for key, emoji, label, _ in CATEGORIES:
        cat = [r for r in rows if r["cat"] == key]
        if cat: out.append((key, emoji, label, cat))
    return out

# ---- Daily report ----
def subgroup_for(cat_key, name):
    defs = SUBGROUPS.get(cat_key)
    if not defs:
        return None
    for label, kws in defs:
        if any(k in name.lower() for k in kws):
            return label
    return "Other"

def compute_coverage(today):
    """Coverage verdicts anchored to the category/subgroup of their driver, so
    component checks appear inline with the relevant product group."""
    out = []
    for rule in COMPONENT_RULES:
        comp = today.get(rule["component"])
        if not comp:
            continue
        if "driver_match" in rule:
            kw = rule["driver_match"].lower(); tech = set(rule.get("technical", []))
            drivers = [it for sku, it in today.items()
                       if kw in it["name"].lower() and sku != rule["component"] and sku not in tech]
            if not drivers:
                continue
            driver_qty = sum(it["physicalQuantity"] for it in drivers)
            driver_label = rule.get("driver_label", "all drivers")
            cat_name = rule["driver_match"]
        else:
            drv = today.get(rule["driver"])
            if not drv:
                continue
            driver_qty = drv["physicalQuantity"]; driver_label = drv["name"].strip()
            cat_name = drv["name"]
        diff = comp["physicalQuantity"] - driver_qty
        cov = comp["name"].strip()
        if diff >= 0:
            text = f"🔗 Component check — {cov} ({fmt(comp['physicalQuantity'])}) ≥ {driver_label} ({fmt(driver_qty)}) ✅ covered · buffer {fmt(diff)}"
        else:
            text = f"🚨 *SHORTFALL* — {cov} ({fmt(comp['physicalQuantity'])}) below {driver_label} ({fmt(driver_qty)}). Order at least *{fmt(-diff)}* more."
        cat_key = categorize(cat_name)
        out.append({"cat": cat_key, "sub": subgroup_for(cat_key, cat_name),
                    "shortfall": diff < 0, "text": text})
    return out

def build_report(today_items, prev_items, prev_date, history):
    today = index_by_sku(today_items)
    prev  = index_by_sku(prev_items) if prev_items else {}
    rows = []
    for sku, it in today.items():
        if sku in EXCLUDE_SKUS: continue
        phys = it["physicalQuantity"]
        if phys <= 0 and sku not in ALWAYS_SHOW: continue
        logi = it["logicalQuantity"]
        delta = (prev[sku]["physicalQuantity"] - phys) if sku in prev else None
        rate = avg_runrate(sku, history)
        rows.append({"sku": sku, "name": it["name"], "phys": phys, "logi": logi,
                     "delta": delta, "reserved": max(phys - logi, 0),
                     "rate": rate, "days_left": (phys / rate) if rate > 0 else None,
                     "cat": categorize(it["name"])})

    today_str = date.today().strftime("%a, %d %b %Y")
    blocks = [b_header(f"📦 Printeers Stock Report"),
              b_context(f"*{today_str}* · " + ("first run — sell-through starts tomorrow"
                        if not prev_items else f"stock & sell-through vs {prev_date}"))]

    low = sorted([r for r in rows if r["days_left"] is not None and r["days_left"] <= LOWSTOCK_DAYS],
                 key=lambda r: r["days_left"])
    if low:
        txt = f"⚠️ *Reorder soon* (≤{LOWSTOCK_DAYS} days, based on avg daily sales)\n"
        txt += "\n".join(f"• `{r['sku']}` {r['name'].strip()} — *{fmt(r['phys'])}* left · ~{r['rate']:.0f}/day · ⏳ ~{r['days_left']:.0f}d" for r in low)
        blocks += [b_divider(), b_section(txt)]

    coverage = compute_coverage(today)
    def add_verdicts(ckey, csub):
        for cv in coverage:
            if cv["cat"] == ckey and cv["sub"] == csub:
                blocks.append(b_section(cv["text"]) if cv["shortfall"] else b_context(cv["text"]))

    for key, emoji, label, cat in grouped(rows):
        cat.sort(key=lambda r: r["name"].lower())
        blocks += [b_divider(), b_header(f"{emoji} {label} · {len(cat)} SKUs")]
        subs = split_subgroups(key, cat)
        if subs:
            for sublabel, subrows in subs:
                subrows.sort(key=lambda r: r["name"].lower())
                blocks.append(b_section(f"*{sublabel}*\n" + category_table_text(subrows, weekly=False)))
                add_verdicts(key, sublabel)
        else:
            blocks.append(category_table_block(cat, weekly=False))
            add_verdicts(key, None)

    restocks = sorted([r for r in rows if r["delta"] and r["delta"] < 0], key=lambda r: r["delta"])
    if restocks:
        txt = "📥 *Restocked* (inbound, not a sale)\n" + "\n".join(
            f"• `{r['sku']}` {r['name'].strip()} — ➕ +{fmt(-r['delta'])} → {fmt(r['phys'])} in stock" for r in restocks)
        blocks += [b_divider(), b_section(txt)]
    elif prev_items:
        blocks += [b_divider(), b_section(f"📥 *Inbound deliveries*\nNo restocks since {prev_date} — stock moved through sales only.")]

    # Count only finished products: exclude component SKUs and technical "back image" sides,
    # otherwise one sale is counted multiple times (front + back + component).
    component_skus = {r.get("component") for r in COMPONENT_RULES}
    def _is_product(r):
        return (r["sku"] not in component_skus) and ("back image" not in r["name"].lower())
    sold_total = sum(r["delta"] for r in rows if r["delta"] and r["delta"] > 0 and _is_product(r))
    reserved_total = sum(r["reserved"] for r in rows)
    foot = f"📊 {len(rows)} SKUs shown · zero-stock hidden (except tracked components) · source: Printeers API v2"
    if prev_items:
        foot = f"🛒 Sold since {prev_date}: *{fmt(sold_total)}* finished products (components & back sides not double counted) · 🔒 Reserved: {fmt(reserved_total)}\n" + foot
    sold_period = f"Sold = units sold since {prev_date} (last report)" if prev_items else "Sold = n/a on first run"
    blocks += [b_divider(), b_context("Stock = physical in warehouse · Resv = reserved (open orders) · " + sold_period),
               b_context(foot)]

    text = f"Printeers Stock Report {today_str} — {len(rows)} SKUs in stock"
    return text, blocks

# ---- Weekly report ----
def build_weekly_report(today_items, history):
    cutoff = date.today() - timedelta(days=7)
    snaps = []
    for d, items in sorted(history):
        try: dd = date.fromisoformat(d)
        except ValueError: continue
        if dd >= cutoff: snaps.append((d, items))
    today = index_by_sku(today_items)
    sold = {}
    for i in range(1, len(snaps)):
        a = index_by_sku(snaps[i-1][1]); b = index_by_sku(snaps[i][1])
        for sku, it in b.items():
            if sku in EXCLUDE_SKUS: continue
            if sku in a:
                d = a[sku]["physicalQuantity"] - it["physicalQuantity"]
                if d > 0: sold[sku] = sold.get(sku, 0) + d

    today_str = date.today().strftime("%d %b %Y")
    first = snaps[0][0] if snaps else None
    blocks = [b_header("🗓️ Printeers Weekly Stock Balance"),
              b_context(f"week ending *{today_str}* · " + ("not enough history yet — figures build up daily"
                        if len(snaps) < 2 else f"sold over the last {len(snaps)-1} day(s), since {first}"))]

    rows = []
    for sku, units in sold.items():
        it = today.get(sku, {})
        rows.append({"sku": sku, "name": it.get("name", sku), "sold": units,
                     "phys": it.get("physicalQuantity", 0), "cat": categorize(it.get("name", sku))})

    if rows:
        for key, emoji, label, cat in grouped(rows):
            cat.sort(key=lambda r: r["name"].lower())
            blocks += [b_divider(), b_header(f"{emoji} {label} · {fmt(sum(c['sold'] for c in cat))} sold"),
                       category_table_block(cat, weekly=True)]
    else:
        blocks += [b_divider(), b_section("😴 No sales recorded this week.")]

    total = sum(sold.values())
    blocks += [b_divider(), b_context(
        f"🛒 Total sold this week: *{fmt(total)}* units across {len(rows)} SKUs · "
        f"{len(snaps)} daily snapshot(s) used · source: Printeers API v2")]
    text = f"Printeers Weekly Stock Balance week ending {today_str} — {fmt(total)} units sold"
    return text, blocks


def _gh(cfg):
    return cfg.get("GITHUB_TOKEN",""), cfg.get("GITHUB_REPO","")

def github_snapshot_exists(cfg, day):
    token, repo = _gh(cfg)
    if not token or not repo:
        return False
    url = f"https://api.github.com/repos/{repo}/contents/snapshots/{day}.json?ref=main"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/vnd.github+json",
                                               "User-Agent": "heydear-stock-bot"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404: return False
        raise

def github_put_snapshot(cfg, day, path):
    token, repo = _gh(cfg)
    if not token or not repo:
        return "no github config"
    import base64
    content = base64.b64encode(open(path, "rb").read()).decode()
    api = f"https://api.github.com/repos/{repo}/contents/snapshots/{day}.json"
    body = json.dumps({"message": f"snapshot {day} (backup)", "content": content, "branch": "main"}).encode()
    req = urllib.request.Request(api, data=body, method="PUT",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json",
                                          "User-Agent": "heydear-stock-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return f"HTTP {r.status}"

def main():
    no_save = "--no-save" in sys.argv
    do_post = "--post" in sys.argv
    weekly  = "--weekly" in sys.argv
    backup  = "--backup" in sys.argv
    cfg = load_config(CONFIG)
    if backup:
        if github_snapshot_exists(cfg, str(date.today())):
            print(f"[backup] cloud already posted {date.today()} — skipping.", file=sys.stderr); return
        do_post = True
    secret = cfg.get("PRINTEERS_SECRET_KEY", "")
    if not secret or secret == "HIER_DEINEN_KEY_EINTRAGEN":
        print("ERROR: PRINTEERS_SECRET_KEY missing in config.", file=sys.stderr); sys.exit(1)
    base = "https://api.printeers.com/v2" if cfg.get("PRINTEERS_ENV","production") != "test" \
           else "https://api.test-printeers.com/v2"

    items = fetch_items(secret, base)
    today_str = str(date.today())
    os.makedirs(SNAPDIR, exist_ok=True)
    if not no_save:
        with open(snap_path(today_str), "w") as f:
            json.dump({"fetchedAt": datetime.now().isoformat(), "items": items}, f)

    priors = prior_snapshots(today_str)
    prev_items, prev_date = (None, None)
    if priors:
        prev_items = load_snapshot(priors[-1])
        prev_date = os.path.basename(priors[-1]).replace(".json", "")
    history = [(os.path.basename(f).replace(".json",""), load_snapshot(f))
               for f in sorted(glob.glob(os.path.join(SNAPDIR, "*.json")))]

    text, blocks = build_weekly_report(items, history) if weekly \
                   else build_report(items, prev_items, prev_date, history)

    # console preview
    print(text)
    for b in blocks:
        if b["type"] == "header": print("\n##", b["text"]["text"])
        elif b["type"] == "section": print(b["text"]["text"])
        elif b["type"] == "context": print("_" + b["elements"][0]["text"] + "_")
        elif b["type"] == "divider": print("—")

    if do_post:
        webhook = cfg.get("SLACK_WEBHOOK_URL", "")
        if not webhook or webhook == "HIER_WEBHOOK_URL_EINTRAGEN":
            print("ERROR: SLACK_WEBHOOK_URL missing in config.", file=sys.stderr); sys.exit(2)
        resp = post_to_slack(webhook, text, blocks, cfg.get("SLACK_BOT_NAME"), cfg.get("SLACK_BOT_ICON"))
        print(f"[slack] {resp}", file=sys.stderr)
        if backup and not no_save:
            try:
                print("[backup] pushed snapshot:", github_put_snapshot(cfg, today_str, snap_path(today_str)), file=sys.stderr)
            except Exception as e:
                print(f"[backup] snapshot push failed: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
