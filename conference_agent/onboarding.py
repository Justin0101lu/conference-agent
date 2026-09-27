"""Onboarding: derive the seller profile, ICP, and a draft message template from the user's website."""
from __future__ import annotations

import json
import re

from .enrichers import _fetch_text, UA
from .llm import _complete, _json
import requests

DEFAULT_TEMPLATE = """Hi {first_name},

I {one_liner} for companies like yours - {company_description}. {value_prop}

Open for a quick chat at the show? Not trying to sell anything. Just to connect and share.

Thanks,
{sender}"""


def fetch_site(url: str, max_chars: int = 12000) -> str:
    if not url.startswith("http"):
        url = "https://" + url
    pages = [url]
    txt = _fetch_text(url)
    # grab a couple of likely sub-pages
    try:
        html = requests.get(url, headers=UA, timeout=15).text
        for m in re.finditer(r'href="(/[a-z0-9\-/]*(about|product|solution|customer|pricing|industr)[a-z0-9\-/]*)"',
                             html, re.I):
            p = m.group(1)
            full = url.rstrip("/") + p
            if full not in pages and len(pages) < 4:
                pages.append(full)
                txt += "\n\n" + _fetch_text(full)
    except Exception:  # noqa: BLE001
        pass
    return txt[:max_chars]


def derive_profile(url: str, cfg: dict, sender_name: str = "") -> dict:
    """Returns seller / icp / message config blocks, ready to merge into cfg."""
    site = fetch_site(url)
    if len(site) < 200:
        raise RuntimeError("Could not read enough text from that website. Paste a description manually.")
    sys_p = ("You are a B2B go-to-market strategist. From a company's website text, produce JSON ONLY:\n"
             '{"company": "<name>", "what_we_do": "<2 sentences, plain, specific>", '
             '"one_liner": "<3-6 words in first person present tense, e.g. \'build AI agents\' or '
             "'run a freight factoring desk'>\", "
             '"value_prop": "<1-2 sentences, concrete, plain English. NO numbers, percentages, or statistics '
             'unless they appear verbatim on the site. No buzzwords, no exclamation marks>", '
             '"icp_description": "<who buys: industries, company types, sizes, roles>", '
             '"icp_exclude": "<who to skip at a conference: competitors, vendors, press, investors etc>", '
             '"industry_keywords": ["<3-8 words used to describe the customer industry>"], '
             '"suggested_enrichers": ["web"] plus "fmcsa" ONLY if customers are US trucking/motor carriers}')
    out = _json(_complete(cfg, sys_p, f"WEBSITE {url}\n\n{site}", 900))
    if not out:
        raise RuntimeError("LLM did not return a profile; try again.")
    first = sender_name or "Me"
    template = (DEFAULT_TEMPLATE.replace("{one_liner}", out.get("one_liner", "work"))
                .replace("{value_prop}", out.get("value_prop", "")).replace("{sender}", first))
    return {
        "seller": {"company": out.get("company", ""), "sender_name": first, "website": url,
                   "what_we_do": out.get("what_we_do", "")},
        "icp": {"description": out.get("icp_description", ""), "exclude": out.get("icp_exclude", ""),
                "keywords": out.get("industry_keywords", [])},
        "enrichers": [e for e in out.get("suggested_enrichers", ["web"]) if e in ("web", "fmcsa")] or ["web"],
        "message": {"template": template},
    }
