"""Conference Agent — Streamlit UI.

Flow:  1 Your website -> ICP & template   2 Phone: capture attendee list   3 Research + qualify
       4 Approve messages -> agent sends via the phone   5 (Paid) unlock email/phone/LinkedIn -> CRM CSV
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from conference_agent import phone, unlock
from conference_agent.onboarding import derive_profile
from conference_agent.pipeline import load_config, make_zi, parse_attendees, run
from conference_agent.zoominfo import CreditGuardError

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / ("config.yaml" if (ROOT / "config.yaml").exists() else "config.example.yaml")
WORK = Path(tempfile.gettempdir()) / "conference_agent"
WORK.mkdir(exist_ok=True)

st.set_page_config(page_title="Conference Agent", page_icon="🎟️", layout="wide")
st.title("🎟️ Conference Agent")
st.caption("Your website → ICP. Your phone's conference app → attendee list. Free ZoomInfo + public data → "
           "who's worth meeting. Your voice → meeting requests, sent by the agent. **Zero ZoomInfo credits for research.**")

S = st.session_state


def _secret(name: str) -> str:
    """Env var, or Streamlit Cloud secret."""
    v = os.getenv(name, "")
    if not v:
        try:
            v = str(st.secrets.get(name, ""))
        except Exception:  # noqa: BLE001
            v = ""
    return v


for _k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ZOOMINFO_CLIENT_ID", "ZOOMINFO_CLIENT_SECRET",
           "ZOOMINFO_USERNAME", "ZOOMINFO_PASSWORD", "STRIPE_SECRET_KEY", "STRIPE_PRICE_CENTS", "APP_BASE_URL"):
    if _secret(_k) and not os.getenv(_k):
        os.environ[_k] = _secret(_k)

S.setdefault("cfg", load_config(CFG_PATH))
cfg = S["cfg"]

# =============================================================== sidebar: keys
with st.sidebar:
    st.header("Keys")
    llm = cfg.setdefault("llm", {})
    llm["provider"] = st.selectbox("LLM", ["openai", "anthropic"], index=0 if llm.get("provider", "openai") == "openai" else 1)
    llm["model"] = st.text_input("Model (text)", llm.get("model") or ("gpt-4o-mini" if llm["provider"] == "openai" else "claude-3-5-haiku-latest"))
    llm["vision_model"] = st.text_input("Model (vision, for phone screenshots)", llm.get("vision_model") or ("gpt-4o" if llm["provider"] == "openai" else "claude-3-5-sonnet-latest"))
    env_key = "OPENAI_API_KEY" if llm["provider"] == "openai" else "ANTHROPIC_API_KEY"
    _typed = st.text_input(env_key, llm.get("api_key") or _secret(env_key), type="password",
                           help="Pre-filled from the server's secret if one is configured.")
    llm["api_key"] = _typed or _secret(env_key)

    zi = cfg.setdefault("zoominfo", {})
    zi["enabled"] = st.checkbox("ZoomInfo (search only — free)", zi.get("enabled", True))
    if zi["enabled"]:
        zi["mode"] = st.selectbox("Auth", ["gtm", "legacy"], index=0 if zi.get("mode", "gtm") == "gtm" else 1)
        if zi["mode"] == "gtm":
            zi["client_id"] = st.text_input("Client ID", zi.get("client_id", ""), type="password")
            zi["client_secret"] = st.text_input("Client secret", zi.get("client_secret", ""), type="password")
        else:
            zi["username"] = st.text_input("Username", zi.get("username", ""))
            zi["password"] = st.text_input("Password", zi.get("password", ""), type="password")
        st.caption("🔒 Research calls only `/companies/search` + `/contacts/search`. Enrich/lookup/bulk are blocked in code.")
    st.divider()
    st.download_button("⬇️ Export config.yaml", yaml.safe_dump({k: v for k, v in cfg.items()}, sort_keys=False),
                       "config.yaml", "text/yaml")

STEPS = ["1 · Your company", "2 · Attendees (phone)", "3 · Research & qualify", "4 · Approve & send",
         "5 · Unlock contacts (paid)", "⚙️ Calibrate phone"]
S.setdefault("step", STEPS[0])
S["step"] = st.radio("Step", STEPS, index=STEPS.index(S["step"]), horizontal=True, label_visibility="collapsed")

# ============================================================ 1 · onboarding
if S["step"] == STEPS[0]:
    st.subheader("Tell the agent who you are — paste your website")
    c1, c2 = st.columns([3, 1])
    url = c1.text_input("Website", cfg.get("seller", {}).get("website", ""), placeholder="https://truckpedia.io")
    sender = c2.text_input("Your first name", cfg.get("seller", {}).get("sender_name", ""))
    if st.button("🔎 Read my site and draft ICP + message", type="primary", disabled=not url):
        with st.spinner("Reading site…"):
            try:
                prof = derive_profile(url, cfg, sender)
                cfg.update(prof)
                st.success("Drafted. Edit anything below.")
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    seller = cfg.setdefault("seller", {})
    icp = cfg.setdefault("icp", {})
    msg = cfg.setdefault("message", {})
    seller["company"] = st.text_input("Company", seller.get("company", ""))
    seller["sender_name"] = sender or seller.get("sender_name", "")
    seller["what_we_do"] = st.text_area("What you do", seller.get("what_we_do", ""), height=80)
    icp["description"] = st.text_area("Who you want to meet (ICP)", icp.get("description", ""), height=80)
    icp["exclude"] = st.text_area("Who to skip", icp.get("exclude", ""), height=60)
    cfg["enrichers"] = st.multiselect("Public data sources", ["web", "fmcsa"], default=cfg.get("enrichers", ["web"]),
                                      help="fmcsa = US motor-carrier census (trucking only)")
    msg["template"] = st.text_area("Meeting request template", msg.get("template", ""), height=230,
                                   help="{first_name} {company_description} {company} {sender}. Only {company_description} is AI-written.")

# ========================================================= 2 · attendee list
if S["step"] == STEPS[1]:
    st.subheader("Pull the attendee list from the conference app on your phone")
    st.markdown("**Setup (macOS):** open *iPhone Mirroring*, **lock your phone**, open the conference app to the "
                "attendee list. Keep the mirroring window visible and don't touch the mouse during capture.")
    avail = phone.is_available()
    st.info("iPhone Mirroring detected ✅" if avail else "iPhone Mirroring not running — you can still upload a CSV below.",
            icon="📱" if avail else "⚠️")
    rc1, rc2 = st.columns([1, 2])
    recipe_name = rc1.selectbox("App recipe", phone.Recipe.available(), index=max(0, phone.Recipe.available().index("trimble_insight") if "trimble_insight" in phone.Recipe.available() else 0))
    S["recipe"] = phone.Recipe.load(recipe_name)
    rc2.caption(S["recipe"].data.get("notes", ""))
    max_screens = st.slider("Max screens to scroll", 5, 400, 120)
    if st.button("📸 Capture attendee list from phone", disabled=not avail, type="primary"):
        out_dir = WORK / f"screens_{int(time.time())}"
        bar = st.progress(0.0, "Scrolling…")
        tbl = st.empty()

        def _p(i, rows):
            bar.progress(min(1.0, i / max_screens), f"screen {i} · {len(rows)} attendees so far")
            tbl.dataframe(pd.DataFrame(rows)[["name", "title", "company"]], height=240, use_container_width=True)

        try:
            rows = phone.capture_attendee_list(cfg, S["recipe"], str(out_dir), max_screens=max_screens, progress=_p)
            S["attendees"] = parse_attendees(rows)
            bar.progress(1.0, f"Done — {len(S['attendees'])} attendees")
        except phone.PhoneError as e:
            st.error(str(e))
    st.markdown("**…or upload / paste a list**")
    up = st.file_uploader("CSV with name / title / company", type=["csv"])
    with st.form("paste_form", border=False):
        raw = st.text_area("Paste `Name | Title | Company` per line", height=120)
        st.form_submit_button("Use pasted list")
    if up is not None:
        S["attendees"] = parse_attendees(pd.read_csv(up).fillna("").to_dict("records"))
    elif raw.strip():
        rows = []
        for line in raw.strip().splitlines():
            p = [x.strip() for x in line.split("|")]
            if len(p) >= 3:
                rows.append({"name": p[0], "title": p[1], "company": p[2]})
            elif len(p) == 2:
                rows.append({"name": p[0], "title": "", "company": p[1]})
        S["attendees"] = parse_attendees(rows)
    if S.get("attendees"):
        st.success(f"{len(S['attendees'])} attendees ready")
        st.dataframe(pd.DataFrame(S["attendees"]), use_container_width=True, height=260)

# ======================================================== 3 · research
if S["step"] == STEPS[2]:
    st.subheader("Research every attendee, keep the ones that fit")
    att = S.get("attendees") or []
    if not att:
        st.warning("No attendees yet — do step 2.")
    else:
        c1, c2, c3 = st.columns(3)
        workers = c1.slider("Parallel workers", 1, 8, 4)
        want_dossier = c2.checkbox("Pre-meeting briefs", True)
        if c3.button("🔍 Test ZoomInfo (free call)"):
            z = make_zi(cfg)
            if not z:
                st.warning("ZoomInfo not configured — public data only.")
            else:
                try:
                    st.json([c.__dict__ | {"raw": None} for c in z.search_companies(att[0]["company"], rpp=3)])
                except CreditGuardError as e:
                    st.error(str(e))
                except Exception as e:  # noqa: BLE001
                    st.error(f"ZoomInfo error: {e}")
        if st.button("🚀 Run research", type="primary"):
            cache = WORK / "cache.json"
            cache.unlink(missing_ok=True)
            bar = st.progress(0.0)
            live = st.empty()
            shown = []

            def _prog(done, total, rec):
                bar.progress(done / total, f"{done}/{total} · {rec['name']} — {'✅ fit' if rec['icp_fit'] else '⏭ skip'}")
                shown.append(rec)
                live.dataframe(pd.DataFrame([{"name": r["name"], "company": r["company"], "title": r.get("title", ""),
                                              "fit": r["icp_fit"], "why": r["icp_reason"]} for r in shown]),
                               height=260, use_container_width=True)

            try:
                S["results"] = run(att, cfg, cache_path=str(cache), workers=workers, want_dossier=want_dossier, progress=_prog)
                S["approved"] = {r["name"]: True for r in S["results"] if r["icp_fit"]}
                bar.progress(1.0, "Done")
            except CreditGuardError as e:
                st.error(f"Credit guard tripped — nothing spent. {e}")
    res = S.get("results") or []
    if res:
        fit = [r for r in res if r["icp_fit"]]
        st.markdown(f"**{len(fit)} fit · {len(res) - len(fit)} skipped**")
        for r in fit:
            zp = ((r.get("facts") or {}).get("zoominfo") or {}).get("person") or {}
            hint = (f" · ZI: {zp.get('title','')} · email {'✓' if zp.get('zoominfo_has_email') else '–'} "
                    f"mobile {'✓' if zp.get('zoominfo_has_mobile') else '–'}") if zp else ""
            with st.expander(f"{r['name']} — {r.get('title','')} @ {r['company']}{hint}"):
                st.markdown(r.get("dossier") or "_no brief_")
                with st.expander("raw facts"):
                    st.json(r["facts"])
        st.download_button("⬇️ results.json", json.dumps(res, indent=1), "results.json")

# ====================================================== 4 · approve & send
if S["step"] == STEPS[3]:
    st.subheader("Approve each message, then let the agent send them through the phone")
    res = S.get("results") or []
    fit = [r for r in res if r["icp_fit"]]
    if not fit:
        st.warning("Run research first.")
    else:
        S.setdefault("approved", {})
        S.setdefault("edited", {})
        ca, cb, cc = st.columns(3)
        if ca.button("Approve all"):
            S["approved"] = {r["name"]: True for r in fit}
        if cb.button("Clear all"):
            S["approved"] = {r["name"]: False for r in fit}
        cc.metric("Approved", sum(1 for v in S["approved"].values() if v))
        for r in fit:
            with st.container(border=True):
                h1, h2 = st.columns([5, 1])
                h1.markdown(f"**{r['name']}** — {r.get('title','')} @ {r['company']}")
                S["approved"][r["name"]] = h2.checkbox("send", S["approved"].get(r["name"], True), key=f"ap_{r['name']}")
                S["edited"][r["name"]] = st.text_area("message", S["edited"].get(r["name"], r["message"]),
                                                       key=f"msg_{r['name']}", height=180, label_visibility="collapsed")
        st.divider()
        avail = phone.is_available()
        dry = st.checkbox("Dry run (compose but don't tap Send)", True)
        st.caption("The agent opens each profile via the app's search, verifies the name with vision, pastes the "
                   "message, and taps Send. Keep iPhone Mirroring visible; don't touch the mouse.")
        if st.button("📨 Send approved messages via phone", type="primary", disabled=not avail):
            targets = [r for r in fit if S["approved"].get(r["name"])]
            bar = st.progress(0.0)
            log_box = st.empty()
            logs = []
            for i, r in enumerate(targets):
                bar.progress(i / max(1, len(targets)), f"{r['name']}…")
                lg = phone.send_message(cfg, S["recipe"], r, S["edited"].get(r["name"], r["message"]), dry_run=dry)
                logs.append(lg)
                log_box.dataframe(pd.DataFrame(logs)[["name", "ok", "step", "detail"]], use_container_width=True)
                time.sleep(1.0)
            bar.progress(1.0, f"Done · {sum(1 for l in logs if l['ok'])}/{len(targets)} ok")
            S["send_log"] = logs
        buf = io.StringIO()
        pd.DataFrame([{"name": r["name"], "title": r.get("title", ""), "company": r["company"],
                       "message": S["edited"].get(r["name"], r["message"]),
                       "approved": S["approved"].get(r["name"], False)} for r in fit]).to_csv(buf, index=False)
        st.download_button("⬇️ outreach.csv (manual sending)", buf.getvalue(), "outreach.csv", "text/csv")

# ====================================================== 5 · paid unlock
if S["step"] == STEPS[4]:
    st.subheader("Unlock email · phone · LinkedIn for CRM sync")
    st.markdown(f"Research never spends ZoomInfo credits. Unlocking does — **${unlock.price_cents()/100:.2f} per contact**, "
                "billed via Stripe, then enriched through ZoomInfo on the operator's account and handed to you as a CRM-ready CSV.")
    res = S.get("results") or []
    fit = [r for r in res if r["icp_fit"]]
    zi_ids = {r["name"]: (((r.get("facts") or {}).get("zoominfo") or {}).get("person") or {}).get("zoominfo_id")
              for r in fit}
    unlockable = [r for r in fit if zi_ids.get(r["name"])]
    if not fit:
        st.warning("Run research first.")
    elif not unlockable:
        st.info("No attendees matched in ZoomInfo search, so nothing to unlock. Enable ZoomInfo in the sidebar and re-run.")
    else:
        pick = st.multiselect("Contacts to unlock", [r["name"] for r in unlockable],
                              default=[r["name"] for r in unlockable])
        n = len(pick)
        st.metric("Total", f"${n * unlock.price_cents() / 100:.2f}")
        base_url = os.getenv("APP_BASE_URL", "http://localhost:8501")
        qs = st.query_params
        if qs.get("unlock_session"):
            try:
                paid = unlock.verify_paid(qs["unlock_session"])
                with st.spinner("Enriching…"):
                    rows = unlock.enrich_contacts(cfg["zoominfo"], paid["contact_ids"], paid)
                S["unlocked"] = rows
                st.success(f"Unlocked {len(rows)} contacts")
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
        if not unlock.stripe_enabled():
            st.warning("Operator has not configured Stripe (STRIPE_SECRET_KEY) — unlock is disabled on this deployment.")
        elif st.button(f"💳 Pay & unlock {n} contacts", type="primary", disabled=n == 0):
            ids = [zi_ids[nm] for nm in pick]
            try:
                co = unlock.create_checkout(ids, base_url)
                st.link_button("Continue to Stripe Checkout →", co["url"])
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
        if S.get("unlocked"):
            st.dataframe(pd.DataFrame(S["unlocked"]), use_container_width=True)
            fmt = st.selectbox("CRM format", ["hubspot", "salesforce", "generic"])
            st.download_button("⬇️ CRM import CSV", unlock.crm_csv(S["unlocked"], fmt), f"contacts_{fmt}.csv", "text/csv")

# ========================================================= calibrate
if S["step"] == STEPS[5]:
    st.subheader("Calibrate tap points for your conference app")
    st.caption("Points are screenshot pixels (2× Retina). Take a screenshot of each screen, hover to read coordinates, "
               "and set them here. Save as a new recipe YAML in `conference_agent/recipes/`.")
    rec = S.get("recipe") or phone.Recipe.load("generic")
    if phone.is_available() and st.button("📸 Screenshot phone now"):
        p = phone.screenshot(str(WORK / "calib.png"))
        st.image(p, width=344)
    pts = rec.data.setdefault("points", {})
    cols = st.columns(2)
    for i, k in enumerate(["scroll_anchor", "search_box", "first_result", "message_button", "message_field",
                           "send_button", "back_button", "confirm_button"]):
        v = pts.get(k, [0, 0])
        with cols[i % 2]:
            x = st.number_input(f"{k} x", value=int(v[0]), key=f"cx_{k}")
            y = st.number_input(f"{k} y", value=int(v[1]), key=f"cy_{k}")
            if k == "confirm_button" and x == 0 and y == 0:
                pts.pop(k, None)
            else:
                pts[k] = [x, y]
    if phone.is_available():
        t = st.selectbox("Test tap", list(pts.keys()))
        if st.button("Tap it"):
            phone.tap(*pts[t])
            time.sleep(1)
            st.image(phone.screenshot(str(WORK / "calib_after.png")), width=344)
    st.download_button("⬇️ Save recipe YAML", yaml.safe_dump(rec.data, sort_keys=False), f"{rec.name}.yaml", "text/yaml")
