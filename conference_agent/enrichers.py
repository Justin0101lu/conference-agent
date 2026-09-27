"""Pluggable public-data enrichers. Each takes an attendee dict and returns a dict of extra facts.

Built-in:
- fmcsa: US DOT carrier census (trucking). Free Socrata API, no key.
- web: DuckDuckGo HTML search (no key) + optional page fetch for a company blurb.

Add your own: subclass Enricher, register in ENRICHERS, reference by name in config.yaml.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any

import requests

UA = {"User-Agent": "conference-agent/0.1 (+https://github.com/Justin0101lu/conference-agent)"}


class Enricher:
    name = "base"

    def enrich(self, attendee: dict, cfg: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class FMCSAEnricher(Enricher):
    """Look up a company in the FMCSA motor carrier census (data.transportation.gov)."""

    name = "fmcsa"
    URL = "https://data.transportation.gov/resource/az4n-8mr2.json"
    FIELDS = ("dot_number,legal_name,dba_name,phy_city,phy_state,phy_country,power_units,total_drivers,"
              "carrier_operation,classdef,mcs150_date,status_code,email_address,phone")

    def enrich(self, attendee: dict, cfg: dict) -> dict:
        co = (attendee.get("company") or "").strip()
        if not co:
            return {}
        core = _core_name(co)
        if len(core) < 3:
            return {}
        where = f"upper(legal_name) like '%{core.upper()}%' OR upper(dba_name) like '%{core.upper()}%'"
        try:
            r = requests.get(self.URL, params={"$where": where, "$limit": 10, "$select": self.FIELDS,
                                               "$order": "power_units DESC"},
                             headers=UA, timeout=20)
            r.raise_for_status()
            rows = r.json()
        except Exception as e:  # noqa: BLE001
            return {"fmcsa_error": str(e)}
        if not rows:
            return {"fmcsa_match": None}
        # Prefer active carriers, then most power units
        rows.sort(key=lambda x: (x.get("status_code") != "A", -_int(x.get("power_units"))))
        b = rows[0]
        return {
            "fmcsa_match": {
                "dot_number": b.get("dot_number"), "legal_name": b.get("legal_name"), "dba_name": b.get("dba_name"),
                "city": b.get("phy_city"), "state": b.get("phy_state"), "country": b.get("phy_country"),
                "power_units": _int(b.get("power_units")), "drivers": _int(b.get("total_drivers")),
                "carrier_operation": b.get("carrier_operation"), "class": b.get("classdef"),
                "mcs150_date": (b.get("mcs150_date") or "")[:10], "status": b.get("status_code"),
                "company_email_domain": _domain(b.get("email_address")),
            },
            "fmcsa_candidates": len(rows),
        }


class WebEnricher(Enricher):
    """Free web search (DuckDuckGo HTML) for company + person. Returns top snippets and homepage blurb."""

    name = "web"

    def enrich(self, attendee: dict, cfg: dict) -> dict:
        co = (attendee.get("company") or "").strip()
        name = (attendee.get("name") or "").strip()
        out: dict[str, Any] = {}
        if co:
            out["company_search"] = _ddg(f"{co} company")[:4]
            site = _first_site(out["company_search"])
            if site:
                out["company_website"] = site
                blurb = _fetch_text(site)
                if blurb:
                    out["company_blurb"] = blurb[:1500]
        if name and co:
            out["person_search"] = _ddg(f'"{name}" {co}')[:4]
        return out


ENRICHERS: dict[str, type[Enricher]] = {
    FMCSAEnricher.name: FMCSAEnricher,
    WebEnricher.name: WebEnricher,
}


# ----------------------------------------------------------------- helpers
def _domain(email: str | None) -> str:
    m = re.search(r"@([\w.-]+\.[a-z]{2,})", (email or "").lower())
    return m.group(1) if m else ""


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


_STOP = {"inc", "llc", "co", "corp", "corporation", "company", "ltd", "the", "incorporated", "lp", "llp",
         "group", "holdings", "l.l.c.", "l.l.c", "ltd."}


def _core_name(s: str) -> str:
    toks = [t for t in re.split(r"[\s,.]+", s) if t and t.lower().strip(".") not in _STOP]
    core = " ".join(toks[:3])
    return core.replace("'", "''")


def _ddg(q: str) -> list[dict]:
    try:
        r = requests.get("https://html.duckduckgo.com/html/", params={"q": q}, headers=UA, timeout=20)
        r.raise_for_status()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?'
                         r'<a class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S):
        href, title, snip = m.groups()
        href = _unwrap_ddg(href)
        out.append({"url": href, "title": _clean(title), "snippet": _clean(snip)})
    return out


def _unwrap_ddg(href: str) -> str:
    if "uddg=" in href:
        q = urllib.parse.urlparse(href).query
        return urllib.parse.parse_qs(q).get("uddg", [href])[0]
    return href


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


_SKIP_DOMAINS = ("linkedin.com", "facebook.com", "zoominfo.com", "dnb.com", "bloomberg.com", "wikipedia.org",
                 "yelp.com", "glassdoor.com", "indeed.com", "crunchbase.com", "youtube.com", "x.com", "twitter.com")


def _first_site(results: list[dict]) -> str:
    for r in results:
        u = r.get("url", "")
        if u.startswith("http") and not any(d in u for d in _SKIP_DOMAINS):
            p = urllib.parse.urlparse(u)
            return f"{p.scheme}://{p.netloc}"
    return ""


def _fetch_text(url: str) -> str:
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if r.status_code != 200:
            return ""
        t = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = html.unescape(re.sub(r"\s+", " ", t)).strip()
        return t
    except Exception:  # noqa: BLE001
        return ""
