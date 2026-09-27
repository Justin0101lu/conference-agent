"""Pipeline: attendee list -> (ZoomInfo search + enrichers) -> ICP -> message -> dossier.

Resume-safe: results are keyed by (name, company) and cached to a JSON file.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
from pathlib import Path
from typing import Callable, Iterable, Optional

import yaml

from .enrichers import ENRICHERS
from .llm import classify_and_describe, dossier, render_message
from .zoominfo import CreditGuardError, ZoomInfoSearchClient

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p) as f:
        cfg = yaml.safe_load(f) or {}
    # env overrides for secrets
    zi = cfg.setdefault("zoominfo", {})
    zi["client_id"] = os.getenv("ZOOMINFO_CLIENT_ID", zi.get("client_id", ""))
    zi["client_secret"] = os.getenv("ZOOMINFO_CLIENT_SECRET", zi.get("client_secret", ""))
    zi["username"] = os.getenv("ZOOMINFO_USERNAME", zi.get("username", ""))
    zi["password"] = os.getenv("ZOOMINFO_PASSWORD", zi.get("password", ""))
    return cfg


def make_zi(cfg: dict) -> Optional[ZoomInfoSearchClient]:
    zi = cfg.get("zoominfo", {})
    if not zi.get("enabled", True):
        return None
    mode = zi.get("mode", "gtm")
    if mode == "gtm" and zi.get("client_id") and zi.get("client_secret"):
        return ZoomInfoSearchClient("gtm", client_id=zi["client_id"], client_secret=zi["client_secret"])
    if mode == "legacy" and zi.get("username") and zi.get("password"):
        return ZoomInfoSearchClient("legacy", username=zi["username"], password=zi["password"])
    return None


def split_name(name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0].title(), ""
    return parts[0].title(), parts[-1].title()


def key_of(a: dict) -> str:
    return f"{(a.get('name') or '').strip().lower()}|{(a.get('company') or '').strip().lower()}"


def parse_attendees(rows: Iterable[dict]) -> list[dict]:
    """Normalise arbitrary CSV headers to name/title/company."""
    out = []
    for r in rows:
        low = {str(k).strip().lower(): (v if v is not None else "") for k, v in r.items()}
        name = low.get("name") or f"{low.get('first name', low.get('first', ''))} {low.get('last name', low.get('last', ''))}".strip()
        company = low.get("company") or low.get("organization") or low.get("organisation") or low.get("account") or ""
        title = low.get("title") or low.get("job title") or low.get("role") or ""
        if name and company:
            out.append({"name": str(name).strip(), "title": str(title).strip(), "company": str(company).strip()})
    return out


def process_one(a: dict, cfg: dict, zi: Optional[ZoomInfoSearchClient], want_dossier: bool = True) -> dict:
    facts: dict = {}
    fn, ln = split_name(a["name"])
    # ZoomInfo — search only
    if zi:
        try:
            facts["zoominfo"] = zi.lookup_attendee(fn, ln, a["company"])
        except CreditGuardError as e:
            raise e
        except Exception as e:  # noqa: BLE001
            facts["zoominfo"] = {"error": str(e)}
    # Public enrichers
    for name in cfg.get("enrichers", []):
        cls = ENRICHERS.get(name)
        if not cls:
            continue
        try:
            facts[name] = cls().enrich(a, cfg)
        except Exception as e:  # noqa: BLE001
            facts[name] = {"error": str(e)}
    # Prefer ZoomInfo title if attendee list had none
    zi_person = (facts.get("zoominfo") or {}).get("person") or {}
    if not a.get("title") and zi_person.get("title"):
        a["title"] = zi_person["title"]

    cls_out = classify_and_describe(a, facts, cfg)
    rec = {**a, **cls_out, "facts": facts}
    rec["message"] = render_message(a, cls_out["company_description"], cfg) if cls_out["icp_fit"] else ""
    rec["dossier"] = dossier(a, facts, cfg) if (want_dossier and cls_out["icp_fit"]) else ""
    return rec


def run(attendees: list[dict], cfg: dict, cache_path: str | None = None, workers: int = 4,
        want_dossier: bool = True, progress: Optional[Callable[[int, int, dict], None]] = None) -> list[dict]:
    cache: dict[str, dict] = {}
    if cache_path and Path(cache_path).exists():
        cache = {key_of(r): r for r in json.load(open(cache_path))}
    zi = make_zi(cfg)
    todo = [a for a in attendees if key_of(a) not in cache]
    done = 0
    total = len(attendees)

    def _save():
        if cache_path:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            json.dump(list(cache.values()), open(cache_path, "w"), indent=1)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_one, dict(a), cfg, zi, want_dossier): a for a in todo}
        for f in cf.as_completed(futs):
            a = futs[f]
            try:
                rec = f.result()
            except CreditGuardError:
                raise
            except Exception as e:  # noqa: BLE001
                rec = {**a, "icp_fit": False, "icp_reason": f"error: {e}", "company_description": "",
                       "facts": {}, "message": "", "dossier": ""}
            cache[key_of(rec)] = rec
            done += 1
            _save()
            if progress:
                progress(done + (total - len(todo)), total, rec)
    ordered = [cache[key_of(a)] for a in attendees if key_of(a) in cache]
    return ordered
