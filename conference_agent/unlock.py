"""PAID unlock tier — the ONLY place ZoomInfo Enrich is allowed.

The free product (zoominfo.py) is hard-blocked from enrichment. This module is deliberately
separate, imported only by the paid flow, and wraps every enrich call in a payment check:

  1. User picks contacts -> we quote N x price.
  2. Stripe Checkout session created; user pays.
  3. On a verified `paid` session we call ZoomInfo Enrich Contacts (1 bulk credit per NEW record,
     billed to the operator's ZoomInfo account — which is why the operator charges the user).
  4. We return email / phone / LinkedIn and a CRM-ready CSV.

Operator config (env):
  STRIPE_SECRET_KEY, STRIPE_PRICE_CENTS (default 300 = $3/contact), APP_BASE_URL
  ZOOMINFO_* creds with enrich scope (api:data:contact).
"""
from __future__ import annotations

import base64
import csv
import io
import os
import time
from typing import Any

import requests

GTM_BASE = "https://api.zoominfo.com/gtm"
LEGACY_BASE = "https://api.zoominfo.com"
ENRICH_FIELDS = ["id", "firstName", "lastName", "jobTitle", "managementLevel", "email", "phone", "mobilePhone",
                 "directPhone", "companyName", "companyId", "companyWebsite", "externalUrls", "linkedInUrl",
                 "city", "state", "country", "contactAccuracyScore"]


class PaymentRequired(RuntimeError):
    pass


# ------------------------------------------------------------------- stripe
def price_cents() -> int:
    return int(os.getenv("STRIPE_PRICE_CENTS", "300"))


def stripe_enabled() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY"))


def create_checkout(contact_ids: list[str], base_url: str) -> dict:
    """Returns {url, session_id}. Uses Stripe REST directly (no SDK dependency)."""
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key:
        raise PaymentRequired("Stripe not configured (STRIPE_SECRET_KEY)")
    n = len(contact_ids)
    data = {
        "mode": "payment",
        "success_url": f"{base_url}?unlock_session={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}?unlock_cancelled=1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(price_cents()),
        "line_items[0][price_data][product_data][name]": "Contact unlock (email + phone + LinkedIn)",
        "line_items[0][quantity]": str(n),
        "metadata[contact_ids]": ",".join(contact_ids)[:490],
        "metadata[count]": str(n),
    }
    r = requests.post("https://api.stripe.com/v1/checkout/sessions", auth=(key, ""), data=data, timeout=30)
    r.raise_for_status()
    j = r.json()
    return {"url": j["url"], "session_id": j["id"]}


def verify_paid(session_id: str) -> dict:
    key = os.getenv("STRIPE_SECRET_KEY")
    r = requests.get(f"https://api.stripe.com/v1/checkout/sessions/{session_id}", auth=(key, ""), timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("payment_status") != "paid":
        raise PaymentRequired(f"session {session_id} not paid ({j.get('payment_status')})")
    ids = (j.get("metadata", {}) or {}).get("contact_ids", "")
    return {"paid": True, "contact_ids": [i for i in ids.split(",") if i], "amount": j.get("amount_total")}


# ----------------------------------------------------------------- zoominfo
def _token(zi: dict) -> str:
    if zi.get("mode", "gtm") == "gtm":
        basic = base64.b64encode(f"{zi['client_id']}:{zi['client_secret']}".encode()).decode()
        r = requests.post(f"{GTM_BASE}/oauth/v1/token", headers={"Authorization": f"Basic {basic}"},
                          data={"grant_type": "client_credentials"}, timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]
    r = requests.post(f"{LEGACY_BASE}/authenticate", json={"username": zi["username"], "password": zi["password"]},
                      timeout=30)
    r.raise_for_status()
    return r.json()["jwt"]


def enrich_contacts(zi: dict, contact_ids: list[str], paid_session: dict) -> list[dict]:
    """PAID. Requires a verified Stripe session covering these ids. 25 per request max."""
    if not paid_session.get("paid"):
        raise PaymentRequired("enrich called without a paid session")
    allowed = set(paid_session.get("contact_ids") or [])
    if allowed and not set(contact_ids) <= allowed:
        raise PaymentRequired("some contact ids were not covered by the payment")
    tok = _token(zi)
    out: list[dict] = []
    for i in range(0, len(contact_ids), 25):
        batch = contact_ids[i:i + 25]
        if zi.get("mode", "gtm") == "gtm":
            body = {"data": [{"type": "ContactEnrich", "attributes": {"personId": cid}} for cid in batch],
                    "outputFields": ENRICH_FIELDS}
            r = requests.post(f"{GTM_BASE}/data/v1/contacts/enrich", json=body, timeout=60,
                              headers={"Authorization": f"Bearer {tok}", "accept": "application/vnd.api+json",
                                       "content-type": "application/vnd.api+json"})
            r.raise_for_status()
            for d in r.json().get("data", []):
                a = d.get("attributes", {}) or {}
                out.append(_norm(str(d.get("id", "")), a))
        else:
            body = {"matchPersonInput": [{"personId": int(cid)} for cid in batch], "outputFields": ENRICH_FIELDS}
            r = requests.post(f"{LEGACY_BASE}/enrich/contact", json=body, timeout=60,
                              headers={"Authorization": f"Bearer {tok}"})
            r.raise_for_status()
            for res in (r.json().get("data", {}) or {}).get("result", []):
                for a in res.get("data", []) or []:
                    out.append(_norm(str(a.get("id", "")), a))
        time.sleep(0.3)
    return out


def _norm(cid: str, a: dict[str, Any]) -> dict:
    li = a.get("linkedInUrl") or ""
    if not li:
        for u in a.get("externalUrls", []) or []:
            if isinstance(u, dict) and "linkedin" in (u.get("url") or ""):
                li = u["url"]
            elif isinstance(u, str) and "linkedin" in u:
                li = u
    return {"zoominfo_id": cid, "first_name": a.get("firstName", ""), "last_name": a.get("lastName", ""),
            "title": a.get("jobTitle", ""), "company": a.get("companyName", ""),
            "website": a.get("companyWebsite", ""), "email": a.get("email", ""),
            "direct_phone": a.get("directPhone") or a.get("phone", ""), "mobile_phone": a.get("mobilePhone", ""),
            "linkedin": li, "city": a.get("city", ""), "state": a.get("state", ""), "country": a.get("country", "")}


def crm_csv(rows: list[dict], fmt: str = "hubspot") -> str:
    """CRM-ready CSV. hubspot | salesforce | generic."""
    maps = {
        "hubspot": [("First Name", "first_name"), ("Last Name", "last_name"), ("Email", "email"),
                    ("Phone Number", "direct_phone"), ("Mobile Phone Number", "mobile_phone"),
                    ("Job Title", "title"), ("Company Name", "company"), ("Website URL", "website"),
                    ("LinkedIn URL", "linkedin"), ("City", "city"), ("State/Region", "state"), ("Country", "country")],
        "salesforce": [("FirstName", "first_name"), ("LastName", "last_name"), ("Email", "email"),
                       ("Phone", "direct_phone"), ("MobilePhone", "mobile_phone"), ("Title", "title"),
                       ("Company", "company"), ("Website", "website"), ("LinkedIn__c", "linkedin"),
                       ("City", "city"), ("State", "state"), ("Country", "country")],
    }
    cols = maps.get(fmt) or [(k, k) for k in rows[0].keys()] if rows else []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in cols])
    for r in rows:
        w.writerow([r.get(c[1], "") for c in cols])
    return buf.getvalue()
