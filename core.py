"""Pin Autopilot - shared logic used by app.py (Streamlit) and worker.py (GitHub Actions)."""
import os, io, csv, json, re, time, uuid, random, base64, zipfile, datetime as dt
from zoneinfo import ZoneInfo

import requests
import gspread
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageColor


# ----------------------------------------------------------------- config
def cfg(name, default=None):
    """Read a secret from environment (GitHub Actions) or Streamlit secrets."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        import streamlit as st
        v = st.secrets.get(name, default)
        return v if v else default
    except Exception:
        return default


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_iso(s):
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except Exception:
        return None


def num(x):
    try:
        return int(float(x))
    except Exception:
        return 0


# ---------------------------------------------------------------- storage
PRODUCT_COLS = ["listing_id", "title", "url", "description", "tags", "image_urls", "status",
                "added_at", "last_posted_at", "pins_posted", "impressions", "clicks", "saves"]
PIN_COLS = ["pin_id", "listing_id", "title", "description", "alt_text", "headline", "board",
            "template", "image_url", "status", "created_at", "posted_at", "pinterest_pin_id",
            "impressions", "clicks", "saves", "error"]

DEFAULTS = {
    "shop_name": "", "timezone": "America/New_York", "daily_cap": "6", "pins_per_batch": "8",
    "min_ready": "3", "gen_daily_cap": "40", "gen_today": "0", "gen_date": "", "min_gap_days": "2",
    "last_sync": "", "last_metrics": "", "hour_weights": "", "boards": "{}",
    "pin_access_token": "", "pin_refresh_token": "", "pin_expires_at": "0",
}


class Table:
    TTL = 20  # seconds; keeps us well under Google's free read limit (60 reads/minute)

    def __init__(self, sh, name, cols, existing=None):
        self.cols = cols
        self._cache, self._ts = None, 0.0
        ws = (existing or {}).get(name)
        if ws is None:
            try:
                ws = sh.worksheet(name)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(name, rows=1000, cols=len(cols))
                ws.append_row(cols)
        self.ws = ws

    def all(self):
        if self._cache is None or time.time() - self._ts > self.TTL:
            recs = self.ws.get_all_records(numericise_ignore=["all"])
            for i, r in enumerate(recs):
                r["_row"] = i + 2
            self._cache, self._ts = recs, time.time()
        return [dict(r) for r in self._cache]

    def add(self, rows):
        if rows:
            self.ws.append_rows([[r.get(c, "") for c in self.cols] for r in rows],
                                value_input_option="RAW")
            self._cache = None

    def bulk_update(self, updates):
        data = []
        for row, kw in updates:
            for k, v in kw.items():
                data.append({"range": gspread.utils.rowcol_to_a1(row, self.cols.index(k) + 1),
                             "values": [[v]]})
        for i in range(0, len(data), 400):
            self.ws.batch_update(data[i:i + 400], value_input_option="RAW")
        if data:
            self._cache = None

    def update(self, row, **kw):
        self.bulk_update([(row, kw)])


class Settings:
    def __init__(self, table):
        self.t = table
        self.reload()

    def reload(self):
        self.d, self.rows = dict(DEFAULTS), {}
        for r in self.t.all():
            self.d[r["key"]] = r["value"]
            self.rows[r["key"]] = r["_row"]

    def get(self, k):
        return str(self.d.get(k, "") or "")

    def set(self, k, v):
        v = str(v)
        self.d[k] = v
        if k in self.rows:
            self.t.update(self.rows[k], value=v)
        else:
            self.t.add([{"key": k, "value": v}])
            self.reload()


class Store:
    def __init__(self, sh):
        existing = {w.title: w for w in sh.worksheets()}   # one request instead of three
        self.products = Table(sh, "products", PRODUCT_COLS, existing)
        self.pins = Table(sh, "pins", PIN_COLS, existing)
        self.settings = Settings(Table(sh, "settings", ["key", "value"], existing))


def open_store():
    raw = cfg("GOOGLE_SERVICE_ACCOUNT")
    creds = json.loads(raw) if isinstance(raw, str) else dict(raw)
    gc = gspread.service_account_from_dict(creds)
    return Store(gc.open_by_key(cfg("SHEET_ID")))


# ------------------------------------------------------------------- Etsy
ETSY = "https://openapi.etsy.com/v3/application"


def etsy_get(path, **params):
    # If you get 401 errors, put "keystring:shared_secret" in ETSY_API_KEY (Etsy's newer format).
    r = requests.get(ETSY + path, headers={"x-api-key": cfg("ETSY_API_KEY")}, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def listing_id_from_url(u):
    m = re.search(r"/listing/(\d+)", u or "")
    return m.group(1) if m else None


def parse_listing(l):
    imgs = [i["url_fullxfull"] for i in sorted(l.get("images", []) or [], key=lambda i: i.get("rank", 0))
            if i.get("url_fullxfull")]
    lid = str(l["listing_id"])
    return {"listing_id": lid, "title": l.get("title", ""), "url": f"https://www.etsy.com/listing/{lid}",
            "description": (l.get("description") or "")[:1200], "tags": ", ".join(l.get("tags") or []),
            "image_urls": json.dumps(imgs[:6]), "status": "active", "added_at": now_iso(),
            "last_posted_at": "", "pins_posted": "0", "impressions": "0", "clicks": "0", "saves": "0"}


def fetch_listing(lid):
    return parse_listing(etsy_get(f"/listings/{lid}", includes="Images"))


def fetch_shop_listings(shop_name):
    shop_id = etsy_get("/shops", shop_name=shop_name)["results"][0]["shop_id"]
    out, offset = [], 0
    while True:
        d = etsy_get(f"/shops/{shop_id}/listings/active", limit=100, offset=offset, includes="Images")
        out += [parse_listing(l) for l in d["results"]]
        offset += 100
        if offset >= d.get("count", 0):
            break
        time.sleep(0.4)
    return out


def add_products_by_urls(store, urls):
    have = {p["listing_id"] for p in store.products.all()}
    new, skipped, bad = [], 0, 0
    for u in urls:
        lid = listing_id_from_url(u.strip())
        if not lid:
            bad += 1
        elif lid in have:
            skipped += 1
        else:
            new.append(fetch_listing(lid))
            have.add(lid)
            time.sleep(0.4)
    store.products.add(new)
    return f"Added {len(new)} | already in list: {skipped} | not recognised: {bad}"


def sync_shop(store):
    name = store.settings.get("shop_name")
    if not name:
        return 0
    found = fetch_shop_listings(name)
    have = {p["listing_id"] for p in store.products.all()}
    new = [l for l in found if l["listing_id"] not in have]
    store.products.add(new)
    return len(new)


# ----------------------------------------------------------------- Gemini
def gemini_pins(pr, board_names, n, avoid_titles):
    model = cfg("GEMINI_MODEL", "gemini-2.5-flash-lite")
    prompt = f"""You write Pinterest pins for an Etsy digital product.
Return ONLY a JSON list of {n} objects with these keys:
- headline: max 7 words, the big text shown on the pin image
- title: max 90 characters, natural and keyword-rich
- description: 300-450 characters, natural keyword-rich sentences, ends with a gentle call to action to get it on Etsy, no hashtags
- alt_text: max 200 characters describing the image
- board: one name copied exactly from this list: {json.dumps(board_names)}
Every pin must use a different search keyword or angle (use case, audience, occasion, problem solved).
Do not mention prices, discounts or claims you cannot verify.

Product title: {pr['title']}
Tags: {pr['tags']}
Description: {pr['description'][:700]}
Do not reuse these existing pin titles: {json.dumps(avoid_titles[:20])}"""
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": cfg("GEMINI_API_KEY")},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}},
        timeout=90)
    r.raise_for_status()
    data = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return data


TEMPLATES = ["photo_top", "overlay", "frame"]


def generate_for(store, pr, n=None):
    S = store.settings
    n = n or int(S.get("pins_per_batch") or 8)
    boards = json.loads(S.get("boards") or "{}")
    imgs = json.loads(pr["image_urls"] or "[]")
    if not imgs:
        raise ValueError("product has no images")
    existing = [p["title"] for p in store.pins.all() if p["listing_id"] == pr["listing_id"]]
    items = gemini_pins(pr, list(boards) or ["General"], n, existing)
    off, rows = random.randint(0, 2), []
    for i, it in enumerate(items[:n]):
        rows.append({
            "pin_id": uuid.uuid4().hex[:10], "listing_id": pr["listing_id"],
            "title": str(it.get("title", pr["title"]))[:100],
            "description": str(it.get("description", ""))[:800],
            "alt_text": str(it.get("alt_text", ""))[:500],
            "headline": str(it.get("headline", pr["title"]))[:80],
            "board": it.get("board", ""),
            "template": f"{TEMPLATES[(i + off) % len(TEMPLATES)]}:{random.randint(0, len(PALETTES) - 1)}",
            "image_url": imgs[i % len(imgs)], "status": "ready", "created_at": now_iso()})
    store.pins.add(rows)
    return len(rows)


# ------------------------------------------------------------ pin images
W, H = 1000, 1500
PALETTES = [("#F4EFE6", "#2B2B2B", "#C8553D"), ("#1F2A44", "#FFFFFF", "#F2B84B"),
            ("#E8F1EC", "#1E3A2F", "#3F8F6B"), ("#FBE9E7", "#4A2C2A", "#D9776B"),
            ("#2B2B2B", "#FFFFFF", "#F2B84B")]


def _font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default(size)


def _wrap(d, text, f, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_text(d, text, box, fill):
    x0, y0, x1, y1 = box
    size = 110
    while True:
        f = _font(size)
        lines = _wrap(d, text, f, x1 - x0)
        lh = int(size * 1.2)
        if len(lines) * lh <= y1 - y0 or size <= 40:
            break
        size -= 6
    y = y0 + (y1 - y0 - len(lines) * lh) // 2
    for ln in lines:
        w = d.textlength(ln, font=f)
        d.text((x0 + (x1 - x0 - w) / 2, y), ln, font=f, fill=fill)
        y += lh


def render_pin(image_url, headline, template):
    name, pal = template.split(":")
    bg, fg, acc = PALETTES[int(pal) % len(PALETTES)]
    r = requests.get(image_url, timeout=30)
    r.raise_for_status()
    src = Image.open(io.BytesIO(r.content)).convert("RGB")
    if name == "overlay":
        base = ImageOps.fit(src, (W, H)).convert("RGBA")
        base.alpha_composite(Image.new("RGBA", (W, 560), ImageColor.getrgb(bg) + (225,)), (0, H - 700))
        canvas = base.convert("RGB")
        d = ImageDraw.Draw(canvas)
        _draw_text(d, headline, (70, H - 680, W - 70, H - 170), fg)
        d.rectangle([W // 2 - 60, H - 150, W // 2 + 60, H - 140], fill=acc)
    elif name == "frame":
        canvas = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(canvas)
        d.rectangle([0, 0, W, 16], fill=acc)
        _draw_text(d, headline, (60, 60, W - 60, 420), fg)
        photo = ImageOps.fit(src, (860, 1000))
        mask = Image.new("L", (860, 1000), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, 859, 999], radius=36, fill=255)
        canvas.paste(photo, (70, 440), mask)
        d.rectangle([0, H - 16, W, H], fill=acc)
    else:  # photo_top
        canvas = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(canvas)
        canvas.paste(ImageOps.fit(src, (W, 1000)), (0, 0))
        d.rectangle([0, 1000, W, 1012], fill=acc)
        _draw_text(d, headline, (60, 1040, W - 60, 1450), fg)
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=88)
    return buf.getvalue()


# -------------------------------------------------------------- Pinterest
def pin_base():
    # Trial-access apps may need https://api-sandbox.pinterest.com/v5 - set PINTEREST_API_BASE if so.
    return cfg("PINTEREST_API_BASE", "https://api.pinterest.com/v5")


def pin_token(S):
    if not S.get("pin_access_token"):
        raise ValueError("Add your Pinterest tokens in Settings first")
    exp = float(S.get("pin_expires_at") or 0)
    if (S.get("pin_refresh_token") and cfg("PINTEREST_APP_ID") and cfg("PINTEREST_APP_SECRET")
            and time.time() > exp - 3600):
        try:
            r = requests.post(pin_base() + "/oauth/token",
                              auth=(cfg("PINTEREST_APP_ID"), cfg("PINTEREST_APP_SECRET")),
                              data={"grant_type": "refresh_token", "refresh_token": S.get("pin_refresh_token")},
                              timeout=30)
            r.raise_for_status()
            j = r.json()
            S.set("pin_access_token", j["access_token"])
            S.set("pin_expires_at", str(time.time() + j.get("expires_in", 2592000)))
            if j.get("refresh_token"):
                S.set("pin_refresh_token", j["refresh_token"])
        except Exception as e:
            print("token refresh failed:", e)
    return S.get("pin_access_token")


def list_boards(token):
    out, bm = {}, None
    while True:
        r = requests.get(pin_base() + "/boards", headers={"Authorization": f"Bearer {token}"},
                         params={"page_size": 100, **({"bookmark": bm} if bm else {})}, timeout=30)
        r.raise_for_status()
        j = r.json()
        out.update({b["name"]: b["id"] for b in j.get("items", [])})
        bm = j.get("bookmark")
        if not bm:
            return out


def create_pin(token, board_id, title, desc, link, alt, jpeg):
    body = {"board_id": board_id, "title": title[:100], "description": desc[:800], "link": link,
            "alt_text": alt[:500],
            "media_source": {"source_type": "image_base64", "content_type": "image/jpeg",
                             "data": base64.b64encode(jpeg).decode()}}
    r = requests.post(pin_base() + "/pins", headers={"Authorization": f"Bearer {token}"}, json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def pin_metrics(token, pin_id):
    end = dt.date.today()
    r = requests.get(f"{pin_base()}/pins/{pin_id}/analytics", headers={"Authorization": f"Bearer {token}"},
                     params={"start_date": str(end - dt.timedelta(days=30)), "end_date": str(end),
                             "metric_types": "IMPRESSION,OUTBOUND_CLICK,SAVE"}, timeout=30)
    r.raise_for_status()
    s = (r.json().get("all") or {}).get("summary_metrics") or {}
    return int(s.get("IMPRESSION", 0)), int(s.get("OUTBOUND_CLICK", 0)), int(s.get("SAVE", 0))


# -------------------------------------------------------- smart scheduler
DEFAULT_HOURS = {0: .3, 1: .3, 2: .3, 3: .3, 4: .3, 5: .5, 6: 1, 7: 2, 8: 3, 9: 2, 10: 2, 11: 2, 12: 3,
                 13: 2, 14: 2, 15: 2, 16: 2, 17: 3, 18: 3, 19: 4, 20: 5, 21: 5, 22: 3, 23: 1}

SEASON = {1: ["planner", "goal", "budget", "new year", "habit"], 2: ["valentine", "love", "wedding"],
          3: ["spring", "easter", "planner"], 4: ["easter", "spring", "wedding"],
          5: ["mother", "wedding", "teacher", "graduation"], 6: ["wedding", "father", "summer", "graduation"],
          7: ["summer", "back to school", "teacher"], 8: ["back to school", "teacher", "classroom"],
          9: ["fall", "halloween", "teacher", "planner"], 10: ["halloween", "thanksgiving", "fall", "christmas"],
          11: ["christmas", "thanksgiving", "holiday", "advent", "gift"],
          12: ["christmas", "holiday", "gift", "new year", "planner"]}


def hour_weights(S):
    try:
        w = {int(k): float(v) for k, v in json.loads(S.get("hour_weights") or "{}").items()}
    except Exception:
        w = {}
    return {h: w.get(h, DEFAULT_HOURS[h]) for h in range(24)}


def todays_slots(S, now):
    """Today's posting times: hours drawn by weight (best hours most likely), fixed per day."""
    cap = max(1, min(int(S.get("daily_cap") or 6), 24))
    rng = random.Random(now.strftime("%Y-%m-%d"))
    pool = {h: w for h, w in hour_weights(S).items() if w >= 1.0}   # skip dead-of-night hours
    cap, hours = min(cap, len(pool)), []
    for _ in range(cap):
        hs = list(pool)
        h = rng.choices(hs, [pool[x] for x in hs])[0]
        hours.append(h)
        del pool[h]
    return sorted(now.replace(hour=h, minute=rng.randint(0, 59), second=0, microsecond=0)
                  for h in sorted(hours))


def learn_hours(S, pins, tz):
    agg = {}
    for p in pins:
        t = parse_iso(p["posted_at"])
        if p["status"] != "posted" or not t:
            continue
        h = t.astimezone(tz).hour
        score = num(p["clicks"]) * 5 + num(p["saves"]) * 2 + num(p["impressions"]) * 0.01
        a = agg.setdefault(h, [0.0, 0])
        a[0] += score
        a[1] += 1
    avg = {h: a[0] / a[1] for h, a in agg.items() if a[1] >= 3}
    if len(avg) < 3:
        return
    mx = max(avg.values()) or 1
    w = dict(DEFAULT_HOURS)
    for h, v in avg.items():
        w[h] = 0.5 * DEFAULT_HOURS[h] + 0.5 * (v / mx) * 5
    S.set("hour_weights", json.dumps(w))


def pick_pin(S, products, pins, now, last_listing):
    ready = {}
    for p in pins:
        if p["status"] == "ready":
            ready.setdefault(p["listing_id"], []).append(p)
    gap = int(S.get("min_gap_days") or 2)
    words = SEASON.get(now.month, []) + SEASON.get(now.month % 12 + 1, [])[:2]
    cands = []
    for pr in products:
        if pr["status"] != "active" or pr["listing_id"] not in ready:
            continue
        lp = parse_iso(pr["last_posted_at"])
        days = (dt.datetime.now(dt.timezone.utc) - lp).days if lp else 30
        posted = num(pr["pins_posted"])
        w = 1.0 + min(days, 30) * 0.1                              # not posted for a while -> more likely
        w *= 0.15 if days < gap else 1.0                            # posted very recently -> much less likely
        w += 2.0 if posted < 3 else 0.0                             # new products get a push
        w += min(num(pr["clicks"]) / max(posted, 1), 5) * 0.6       # products that earn clicks get more slots
        text = (pr["title"] + " " + pr["tags"]).lower()
        w += 1.5 if any(k in text for k in words) else 0.0          # seasonal fit
        if pr["listing_id"] == last_listing:
            w *= 0.05                                               # never the same product twice in a row
        cands.append((pr, w))
    if not cands:
        return None
    pr = random.choices([c[0] for c in cands], [c[1] for c in cands])[0]
    return pr, random.choice(ready[pr["listing_id"]])



def pin_link(pr, pin):
    return (f"{pr['url']}?utm_source=pinterest&utm_medium=social"
            f"&utm_campaign={pr['listing_id']}&utm_content={pin['pin_id']}")


def export_batch(store, now, days=14, listing_ids=None, max_pins=150, progress=None):
    """Manual-upload mode: render ready pins into a ZIP (images + pins.csv) with suggested post times.
    Products are interleaved, times come from the smart hour weights. Exported pins are marked so they
    are never exported or auto-posted twice. Returns (zip_bytes, number_of_pins)."""
    S = store.settings
    products = {p["listing_id"]: p for p in store.products.all() if p["status"] == "active"}
    by = {}
    for p in store.pins.all():
        if p["status"] == "ready" and p["listing_id"] in products and (not listing_ids or p["listing_id"] in listing_ids):
            by.setdefault(p["listing_id"], []).append(p)
    lists = list(by.values())
    random.shuffle(lists)
    order, k = [], 0
    while any(k < len(l) for l in lists):          # round-robin so products are mixed, not in blocks
        order += [l[k] for l in lists if k < len(l)]
        k += 1
    slots = []
    for d in range(1, days + 1):
        slots += todays_slots(S, now + dt.timedelta(days=d))
    order = order[:min(len(slots), max_pins)]
    buf, rows, done = io.BytesIO(), [], []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (p, slot) in enumerate(zip(order, slots), 1):
            pr = products[p["listing_id"]]
            try:
                jpeg = render_pin(p["image_url"], p["headline"], p["template"])
            except Exception as e:
                print("skip pin:", e)
                continue
            fname = f"{len(rows) + 1:03d}_{p['pin_id']}.jpg"
            z.writestr(fname, jpeg)
            rows.append([fname, p["title"], p["description"], pin_link(pr, p), p["board"],
                         slot.strftime("%Y-%m-%d %H:%M")])
            done.append((p["_row"], {"status": "exported", "posted_at": now_iso()}))
            if progress:
                progress(i, len(order))
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["image_file", "title", "description", "destination_link", "board", "suggested_post_time"])
        w.writerows(rows)
        z.writestr("pins.csv", "\ufeff" + out.getvalue())
    store.pins.bulk_update(done)
    return buf.getvalue(), len(rows)

# ------------------------------------------------------------ worker steps
def post_next(store, now, log=print, force=False):
    S = store.settings
    boards = json.loads(S.get("boards") or "{}")
    if not boards:
        log("No Pinterest boards loaded (Settings -> Load boards).")
        return False
    products, pins = store.products.all(), store.pins.all()
    posted = [p for p in pins if p["status"] == "posted"]
    if not force:
        today = [p for p in posted if parse_iso(p["posted_at"])
                 and parse_iso(p["posted_at"]).astimezone(now.tzinfo).date() == now.date()]
        due = sum(1 for s in todays_slots(S, now) if s <= now)
        if len(today) >= due:
            log(f"Nothing due (posted today {len(today)}, slots passed {due}).")
            return False
    last = max(posted, key=lambda p: p["posted_at"], default=None)
    choice = pick_pin(S, products, pins, now, last["listing_id"] if last else None)
    if not choice:
        log("No ready pins in the pool.")
        return False
    pr, pin = choice
    token = pin_token(S)
    try:
        jpeg = render_pin(pin["image_url"], pin["headline"], pin["template"])
        link = pin_link(pr, pin)
        board_id = boards.get(pin["board"]) or next(iter(boards.values()))
        if not board_id:
            log("Boards have no Pinterest IDs yet. Settings -> Load my Pinterest boards.")
            return False
        res = create_pin(token, board_id, pin["title"], pin["description"], link, pin["alt_text"], jpeg)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        log(f"Pinterest/image error {code}: {getattr(e.response, 'text', '')[:200]}")
        if code not in (0, 401, 429) and code < 500:
            store.pins.update(pin["_row"], status="failed", error=f"{code}: {e.response.text[:150]}")
        return False
    store.pins.update(pin["_row"], status="posted", posted_at=now_iso(), pinterest_pin_id=res.get("id", ""))
    store.products.update(pr["_row"], last_posted_at=now_iso(), pins_posted=str(num(pr["pins_posted"]) + 1))
    log(f"Posted: {pin['title']}")
    return True


def replenish(store, now, log=print):
    S = store.settings
    today = now.strftime("%Y-%m-%d")
    if S.get("gen_date") != today:
        S.set("gen_date", today)
        S.set("gen_today", "0")
    budget = int(S.get("gen_daily_cap") or 40) - num(S.get("gen_today"))
    if budget <= 0:
        return
    pins = store.pins.all()
    ready, total = {}, {}
    for p in pins:
        total[p["listing_id"]] = total.get(p["listing_id"], 0) + 1
        if p["status"] == "ready":
            ready[p["listing_id"]] = ready.get(p["listing_id"], 0) + 1
    need = [p for p in store.products.all()
            if p["status"] == "active" and ready.get(p["listing_id"], 0) < int(S.get("min_ready") or 3)]
    need.sort(key=lambda p: (total.get(p["listing_id"], 0), num(p["pins_posted"])))
    for pr in need[:min(3, budget)]:
        try:
            n = generate_for(store, pr)
            S.set("gen_today", str(num(S.get("gen_today")) + 1))
            log(f"Generated {n} pins for: {pr['title'][:60]}")
        except Exception as e:
            log(f"Generation stopped: {e}")
            break


def refresh_metrics(store, now, log=print):
    S = store.settings
    token = pin_token(S)
    pins, products = store.pins.all(), store.products.all()
    recent = sorted([p for p in pins if p["status"] == "posted" and p["pinterest_pin_id"]],
                    key=lambda p: p["posted_at"], reverse=True)[:50]
    updates, fails = [], 0
    for p in recent:
        t = parse_iso(p["posted_at"])
        if not t or (dt.datetime.now(dt.timezone.utc) - t).days < 2:
            continue
        try:
            imp, clk, sav = pin_metrics(token, p["pinterest_pin_id"])
        except Exception as e:
            fails += 1
            log(f"metrics failed: {e}")
            if fails >= 3:
                break
            continue
        p.update(impressions=imp, clicks=clk, saves=sav)
        updates.append((p["_row"], {"impressions": imp, "clicks": clk, "saves": sav}))
        time.sleep(0.3)
    store.pins.bulk_update(updates)
    tot = {}
    for p in pins:
        if p["status"] == "posted":
            a = tot.setdefault(p["listing_id"], [0, 0, 0])
            a[0] += num(p["impressions"]); a[1] += num(p["clicks"]); a[2] += num(p["saves"])
    store.products.bulk_update([(pr["_row"], {"impressions": tot[pr["listing_id"]][0],
                                              "clicks": tot[pr["listing_id"]][1],
                                              "saves": tot[pr["listing_id"]][2]})
                               for pr in products if pr["listing_id"] in tot])
    learn_hours(S, pins, now.tzinfo)
    S.set("last_metrics", now.strftime("%Y-%m-%d"))


def run_cycle(store, log=print):
    S = store.settings
    now = dt.datetime.now(ZoneInfo(S.get("timezone") or "UTC"))
    today = now.strftime("%Y-%m-%d")
    steps = [("sync", lambda: S.get("last_sync") != today and (sync_shop(store), S.set("last_sync", today))),
             ("replenish", lambda: replenish(store, now, log)),
             ("metrics", lambda: S.get("last_metrics") != today and refresh_metrics(store, now, log)),
             ("post", lambda: post_next(store, now, log))]
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            log(f"[{name}] failed: {e}")
