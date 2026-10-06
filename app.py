import json
import datetime as dt
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import streamlit as st

import core

st.set_page_config(page_title="Pin Autopilot", page_icon="📌", layout="wide")


@st.cache_resource
def get_store():
    return core.open_store()


try:
    store = get_store()
except Exception as e:
    st.error(f"Can't connect to your Google Sheet yet: {e}")
    st.info("Check SHEET_ID and GOOGLE_SERVICE_ACCOUNT in the app's Secrets (see SETUP.md).")
    st.stop()

S = store.settings
try:
    S.reload()
except gspread.exceptions.APIError as e:
    if "429" in str(e):
        st.warning("Google's free read limit was hit. Wait one minute, then refresh this page.")
        st.stop()
    raise
tz = ZoneInfo(S.get("timezone") or "UTC")
now = dt.datetime.now(tz)


@st.cache_data(show_spinner=False, max_entries=200)
def preview(url, headline, tpl):
    return core.render_pin(url, headline, tpl)


page = st.sidebar.radio("📌 Pin Autopilot", ["Products", "Preview pins", "Export (manual upload)", "Activity", "Settings"])

# ------------------------------------------------------------------ Products
if page == "Products":
    st.header("Products")
    c1, c2 = st.columns(2)
    with c1:
        url = st.text_input("Etsy product link", placeholder="https://www.etsy.com/listing/123456789/...")
        if st.button("➕ Add product") and url:
            try:
                with st.spinner("Fetching listing..."):
                    st.success(core.add_products_by_urls(store, [url]))
            except Exception as e:
                st.error(f"Could not add: {e}")
        with st.expander("Add many links at once"):
            bulk = st.text_area("One link per line", height=150)
            if st.button("Add all links") and bulk.strip():
                try:
                    with st.spinner("Fetching listings..."):
                        st.success(core.add_products_by_urls(store, bulk.splitlines()))
                except Exception as e:
                    st.error(f"Stopped: {e}")
    with c2:
        st.write(f"Shop: **{S.get('shop_name') or 'not set (see Settings)'}**")
        if st.button("📥 Import / refresh ALL listings from my shop"):
            try:
                with st.spinner("Importing your shop (can take a minute)..."):
                    st.success(f"Added {core.sync_shop(store)} new products")
            except Exception as e:
                st.error(f"Import failed: {e}")
        st.caption("New listings are also picked up automatically once a day.")

    products, pins = store.products.all(), store.pins.all()
    ready = {}
    for p in pins:
        if p["status"] == "ready":
            ready[p["listing_id"]] = ready.get(p["listing_id"], 0) + 1
    if not products:
        st.info("No products yet. Paste a link above or import your shop.")
    else:
        df = pd.DataFrame([{"active": p["status"] == "active", "title": p["title"][:80],
                            "ready pins": ready.get(p["listing_id"], 0), "posted": core.num(p["pins_posted"]),
                            "clicks": core.num(p["clicks"]), "last posted": (p["last_posted_at"] or "")[:10],
                            "id": p["listing_id"]} for p in products])
        st.caption(f"{len(products)} products. Untick 'active' to pause a product.")
        edited = st.data_editor(df, disabled=[c for c in df.columns if c != "active"], hide_index=True)
        if st.button("Save pause/active changes"):
            by = {p["listing_id"]: p for p in products}
            upd = []
            for _, r in edited.iterrows():
                p = by[str(r["id"])]
                want = "active" if r["active"] else "paused"
                if p["status"] in ("active", "paused") and p["status"] != want:
                    upd.append((p["_row"], {"status": want}))
            store.products.bulk_update(upd)
            st.success(f"Saved {len(upd)} change(s)")

# ------------------------------------------------------------- Preview pins
elif page == "Preview pins":
    st.header("Preview pins")
    products = store.products.all()
    if not products:
        st.info("Add a product first.")
        st.stop()
    names = {f"{p['title'][:70]} ({p['listing_id']})": p for p in products}
    pr = names[st.selectbox("Product", list(names))]
    if st.button("✨ Generate new pins for this product now"):
        try:
            with st.spinner("Writing pins..."):
                st.success(f"Created {core.generate_for(store, pr)} pins")
        except Exception as e:
            st.error(f"Failed: {e}")
    rows = [p for p in store.pins.all() if p["listing_id"] == pr["listing_id"] and p["status"] == "ready"]
    st.caption(f"{len(rows)} ready pins in the pool. The scheduler picks from these automatically; "
               "delete any you don't like.")
    cols = st.columns(4)
    for i, p in enumerate(rows[:12]):
        with cols[i % 4]:
            try:
                st.image(preview(p["image_url"], p["headline"], p["template"]))
            except Exception as e:
                st.warning(f"Preview failed: {e}")
            st.markdown(f"**{p['title']}**")
            st.caption(p["description"][:180])
            if st.button("🗑 Delete", key=p["pin_id"]):
                store.pins.update(p["_row"], status="deleted")
                st.rerun()

# ------------------------------------------------------------------- Export
elif page == "Export (manual upload)":
    st.header("Export pins for manual upload")
    st.write("Use this while Pinterest's API approval is pending. You get a ZIP with ready pin images and a "
             "`pins.csv` (title, description, link, board, suggested posting time). Upload them in Pinterest "
             "and schedule them there.")
    products = store.products.all()
    pins = store.pins.all()
    have = {p["listing_id"] for p in pins if p["status"] == "ready"}
    todo = [p for p in products if p["status"] == "active" and p["listing_id"] not in have]
    st.subheader("1. Make sure pins exist")
    st.write(f"{len(have)} products have ready pins; {len(todo)} active products have none yet.")
    n = st.number_input("Generate for how many products now (free AI daily limit applies)", 1, 100, 20)
    if todo and st.button("✨ Generate pins for products without any"):
        bar = st.progress(0.0)
        made = 0
        for i, pr in enumerate(todo[:int(n)]):
            try:
                core.generate_for(store, pr)
                made += 1
                S.set("gen_today", str(core.num(S.get("gen_today")) + 1))
            except Exception as e:
                st.error(f"Stopped after {made} products: {e}")
                break
            bar.progress((i + 1) / min(len(todo), int(n)))
        st.success(f"Generated pins for {made} products")
    st.subheader("2. Build the download")
    days = st.number_input("Days of posting to cover", 1, 30, 14)
    names = {f"{p['title'][:60]} ({p['listing_id']})": p["listing_id"] for p in products if p["status"] == "active"}
    chosen = st.multiselect("Only these products (leave empty for all)", list(names))
    if st.button("📦 Build export"):
        bar = st.progress(0.0, text="Rendering pins...")
        try:
            data, count = core.export_batch(store, now, int(days), [names[c] for c in chosen] or None,
                                            progress=lambda i, t: bar.progress(i / max(t, 1)))
            st.session_state["export"] = (data, count)
        except Exception as e:
            st.error(f"Export failed: {e}")
    if "export" in st.session_state:
        data, count = st.session_state["export"]
        st.success(f"{count} pins ready")
        st.download_button("⬇️ Download ZIP", data, file_name="pins_export.zip", mime="application/zip")
        st.caption("Exported pins are marked as used, so the next export gives you new ones.")

# ----------------------------------------------------------------- Activity
elif page == "Activity":
    st.header("Activity")
    st.subheader("Today's planned posting times")
    st.write(", ".join(s.strftime("%H:%M") for s in core.todays_slots(S, now)) + f"  ({S.get('timezone')})")
    if st.button("🧪 Post one pin right now (test)"):
        logs = []
        try:
            ok = core.post_next(store, now, log=logs.append, force=True)
        except Exception as e:
            logs.append(str(e))
        st.write("\n\n".join(logs) or "Done")
    st.subheader("Hour weights (learns from your results)")
    st.bar_chart(pd.Series(core.hour_weights(S)))
    pins = store.pins.all()
    posted = sorted([p for p in pins if p["status"] == "posted"], key=lambda p: p["posted_at"], reverse=True)
    failed = [p for p in pins if p["status"] == "failed"]
    st.write(f"Posted: **{len(posted)}** | Ready: **{sum(p['status'] == 'ready' for p in pins)}** | "
             f"Failed: **{len(failed)}**")
    if posted:
        st.dataframe(pd.DataFrame([{"posted": p["posted_at"][:16], "title": p["title"][:70],
                                    "impressions": core.num(p["impressions"]), "clicks": core.num(p["clicks"]),
                                    "saves": core.num(p["saves"])} for p in posted[:100]]))
    if failed:
        with st.expander("Failed pins"):
            st.dataframe(pd.DataFrame([{"title": p["title"][:60], "error": p["error"]} for p in failed[:50]]))

# ----------------------------------------------------------------- Settings
else:
    st.header("Settings")
    with st.form("general"):
        shop = st.text_input("Etsy shop name", S.get("shop_name"))
        tzname = st.text_input("Your buyers' time zone (e.g. America/New_York, Europe/London)", S.get("timezone"))
        cap = st.number_input("Pins per day (start with 5-8)", 1, 24, int(S.get("daily_cap") or 6))
        batch = st.number_input("Pins generated per product per batch", 3, 15, int(S.get("pins_per_batch") or 8))
        gen = st.number_input("Max products to generate for per day (free AI limit)", 5, 200,
                              int(S.get("gen_daily_cap") or 40))
        gap = st.number_input("Min days before the same product is posted again", 0, 14,
                              int(S.get("min_gap_days") or 2))
        if st.form_submit_button("Save"):
            try:
                ZoneInfo(tzname)
                for k, v in [("shop_name", shop.strip()), ("timezone", tzname.strip()), ("daily_cap", cap),
                             ("pins_per_batch", batch), ("gen_daily_cap", gen), ("min_gap_days", gap)]:
                    S.set(k, v)
                st.success("Saved")
            except Exception as e:
                st.error(f"Check the time zone name: {e}")

    st.subheader("Pinterest connection")
    with st.form("pinterest"):
        at = st.text_input("Access token", type="password")
        rt = st.text_input("Refresh token (optional but recommended)", type="password")
        if st.form_submit_button("Save tokens") and at:
            S.set("pin_access_token", at.strip())
            S.set("pin_refresh_token", rt.strip())
            S.set("pin_expires_at", str(__import__("time").time() + 20 * 86400))
            st.success("Saved")
    if st.button("Load my Pinterest boards"):
        try:
            boards = core.list_boards(core.pin_token(S))
            S.set("boards", json.dumps(boards))
            st.success(f"Loaded {len(boards)} boards")
        except Exception as e:
            st.error(f"Failed: {e}")
    st.write("Boards:", ", ".join(json.loads(S.get("boards") or "{}")) or "none loaded yet")
    with st.form("boardnames"):
        st.caption("No Pinterest API yet? Type your board names (comma-separated) so pins get assigned to them.")
        txt = st.text_input("Board names", ", ".join(json.loads(S.get("boards") or "{}")))
        if st.form_submit_button("Save board names"):
            old_b = json.loads(S.get("boards") or "{}")
            S.set("boards", json.dumps({b.strip(): old_b.get(b.strip(), "") for b in txt.split(",") if b.strip()}))
            st.success("Saved")
