"""LLM layer: ICP classification, one-line company description, meeting message, and dossier.

Provider is picked from config: `llm.provider` = openai | anthropic. Key from env
(OPENAI_API_KEY / ANTHROPIC_API_KEY) or config. Everything is a single JSON-returning call
so it's cheap and deterministic to parse.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any


def _client(cfg: dict):
    llm = cfg.get("llm", {})
    provider = llm.get("provider", "openai")
    model = llm.get("model") or ("gpt-4o-mini" if provider == "openai" else "claude-3-5-haiku-latest")
    if provider == "openai":
        from openai import OpenAI
        key = llm.get("api_key") or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        return provider, model, OpenAI(api_key=key)
    if provider == "anthropic":
        import anthropic
        key = llm.get("api_key") or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        return provider, model, anthropic.Anthropic(api_key=key)
    raise ValueError(f"unknown llm provider {provider}")


def _complete(cfg: dict, system: str, user: str, max_tokens: int = 1200) -> str:
    provider, model, cl = _client(cfg)
    if provider == "openai":
        r = cl.chat.completions.create(model=model, temperature=0.3, max_tokens=max_tokens,
                                       messages=[{"role": "system", "content": system},
                                                 {"role": "user", "content": user}])
        return r.choices[0].message.content or ""
    r = cl.messages.create(model=model, max_tokens=max_tokens, temperature=0.3, system=system,
                           messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def _json(s: str) -> dict:
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def classify_and_describe(attendee: dict, facts: dict, cfg: dict) -> dict:
    """Returns {icp_fit: bool, icp_reason: str, company_description: str}."""
    icp = cfg.get("icp", {})
    sys_p = ("You qualify conference attendees for a B2B seller. Reply ONLY with JSON: "
             '{"icp_fit": true|false, "icp_reason": "<1 sentence>", '
             '"company_description": "<a noun phrase describing what the company does, 6-14 words, '
             'lowercase, no company name, no marketing adjectives. It will be inserted after the words '
             "'companies like yours - ' so do NOT repeat them. Example: 'dry bulk carriers running "
             "owner-operators across dump, hopper, and pneumatic trailers'>\"}")
    user = (f"SELLER: {cfg.get('seller', {}).get('company')} — {cfg.get('seller', {}).get('what_we_do')}\n"
            f"ICP DEFINITION: {icp.get('description')}\n"
            f"EXCLUDE: {icp.get('exclude')}\n\n"
            f"ATTENDEE: {attendee.get('name')} — {attendee.get('title')} @ {attendee.get('company')}\n"
            f"FACTS (public data):\n{json.dumps(_trim(facts), indent=1)[:4000]}")
    out = _json(_complete(cfg, sys_p, user, 400))
    desc = re.sub(r"^\s*(companies|firms|businesses)\s+like\s+yours\s*[-–—:]?\s*", "",
                  out.get("company_description", ""), flags=re.I).strip().rstrip(".")
    return {"icp_fit": bool(out.get("icp_fit")), "icp_reason": out.get("icp_reason", ""),
            "company_description": desc}


def render_message(attendee: dict, company_description: str, cfg: dict) -> str:
    """Fill the user's template. No LLM creativity here — the template IS the voice."""
    t = cfg.get("message", {}).get("template", "")
    first = (attendee.get("name") or "").strip().split(" ")[0].title()
    return (t.replace("{first_name}", first)
             .replace("{company_description}", company_description.strip().rstrip("."))
             .replace("{company}", attendee.get("company", ""))
             .replace("{sender}", cfg.get("seller", {}).get("sender_name", "")))


def dossier(attendee: dict, facts: dict, cfg: dict) -> str:
    sys_p = ("You write short pre-meeting briefs for a founder meeting a prospect at a conference. "
             "Use ONLY the supplied facts; where something is unknown say 'not found'. Never invent. "
             "Markdown, <= 250 words, sections: Person / Company / Signals / How to approach.")
    user = (f"SELLER: {cfg.get('seller', {}).get('company')} — {cfg.get('seller', {}).get('what_we_do')}\n"
            f"ATTENDEE: {attendee.get('name')} — {attendee.get('title')} @ {attendee.get('company')}\n"
            f"FACTS:\n{json.dumps(_trim(facts), indent=1)[:6000]}")
    return _complete(cfg, sys_p, user, 700)


def _trim(d: dict) -> dict:
    """Drop bulky raw fields before sending to the LLM."""
    def go(x: Any):
        if isinstance(x, dict):
            return {k: go(v) for k, v in x.items() if k not in ("raw",)}
        if isinstance(x, list):
            return [go(i) for i in x[:6]]
        if isinstance(x, str):
            return x[:800]
        return x
    return go(d)
