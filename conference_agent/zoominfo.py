"""ZoomInfo client — SEARCH ONLY.

Hard guarantee: this module can never spend ZoomInfo credits.
- Only the free Search endpoints are implemented (companies/search, contacts/search).
- Every outbound request passes through `_guard_url`, which raises if the path
  contains any credit-consuming route (enrich, lookup/contact, bulk, redeem, ...).
- Search responses contain NO emails or phone numbers — only `hasEmail`,
  `hasDirectPhone`, `hasMobilePhone` hints. We surface those hints as-is so the
  user knows what ZoomInfo *has*, without unlocking it.

Supports two auth styles:
  1. GTM API (current):  OAuth2 client credentials -> https://api.zoominfo.com/gtm
  2. Legacy Enterprise API: username/password  -> https://api.zoominfo.com/authenticate
"""
from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

FORBIDDEN_PATH_PATTERNS = (
    r"/enrich",          # /data/v1/contacts/enrich, /enrich/contact, /enrich/company ...
    r"/lookup/contact",  # legacy lookup-by-id returns full contact -> credits
    r"/bulk",            # bulk redeem
    r"/redeem",
    r"/account-summary",  # AI action credits
    r"/research",
    r"/recommend",
)


class CreditGuardError(RuntimeError):
    """Raised when code attempts to call a credit-consuming ZoomInfo endpoint."""


def _guard_url(url: str) -> None:
    low = url.lower()
    for pat in FORBIDDEN_PATH_PATTERNS:
        if re.search(pat, low):
            raise CreditGuardError(
                f"Blocked: '{url}' matches credit-consuming pattern '{pat}'. "
                "This product only allows free ZoomInfo Search endpoints."
            )


@dataclass
class ZiCompany:
    id: str
    name: str
    website: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ZiContact:
    id: str
    first_name: str
    last_name: str
    job_title: str = ""
    management_level: str = ""
    company_id: str = ""
    company_name: str = ""
    accuracy: Optional[int] = None
    has_email: Optional[bool] = None
    has_direct_phone: Optional[bool] = None
    has_mobile_phone: Optional[bool] = None
    raw: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class ZoomInfoSearchClient:
    """Search-only ZoomInfo client. There is intentionally no enrich() method."""

    GTM_BASE = "https://api.zoominfo.com/gtm"
    LEGACY_BASE = "https://api.zoominfo.com"

    def __init__(
        self,
        mode: str = "gtm",
        client_id: str = "",
        client_secret: str = "",
        username: str = "",
        password: str = "",
        timeout: int = 30,
    ):
        if mode not in ("gtm", "legacy"):
            raise ValueError("mode must be 'gtm' or 'legacy'")
        self.mode = mode
        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token: str = ""
        self._token_exp: float = 0
        self.session = requests.Session()

    # ------------------------------------------------------------------ auth
    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        if self.mode == "gtm":
            url = f"{self.GTM_BASE}/oauth/v1/token"
            _guard_url(url)
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            r = self.session.post(
                url,
                headers={"Authorization": f"Basic {basic}", "Accept": "application/json",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            j = r.json()
            self._token = j["access_token"]
            self._token_exp = time.time() + int(j.get("expires_in", 900))
        else:
            url = f"{self.LEGACY_BASE}/authenticate"
            _guard_url(url)
            r = self.session.post(url, json={"username": self.username, "password": self.password},
                                  timeout=self.timeout)
            r.raise_for_status()
            self._token = r.json()["jwt"]
            self._token_exp = time.time() + 3500
        return self._token

    def _post(self, url: str, body: dict, params: dict | None = None, retries: int = 4) -> dict:
        _guard_url(url)
        for attempt in range(retries):
            tok = self._auth()
            headers = {"Authorization": f"Bearer {tok}"}
            if self.mode == "gtm":
                headers.update({"accept": "application/vnd.api+json",
                                "content-type": "application/vnd.api+json"})
            else:
                headers.update({"Content-Type": "application/json"})
            r = self.session.post(url, json=body, params=params, headers=headers, timeout=self.timeout)
            if r.status_code in (429, 500, 504) and attempt < retries - 1:
                time.sleep(min(2 ** attempt, 15))
                continue
            if r.status_code == 401 and attempt < retries - 1:
                self._token = ""
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("ZoomInfo request failed after retries")

    # ---------------------------------------------------------------- search
    def search_companies(self, name: str, rpp: int = 5) -> list[ZiCompany]:
        if self.mode == "gtm":
            url = f"{self.GTM_BASE}/data/v1/companies/search"
            j = self._post(url, {"data": {"type": "CompanySearch", "attributes": {"companyName": name}}},
                           params={"page[size]": rpp})
            out = []
            for d in j.get("data", []):
                a = d.get("attributes", {}) or {}
                out.append(ZiCompany(id=str(d.get("id", "")), name=a.get("name", ""),
                                     website=a.get("website", ""), city=a.get("city", ""),
                                     state=a.get("state", ""), country=a.get("country", ""), raw=a))
            return out
        url = f"{self.LEGACY_BASE}/search/company"
        j = self._post(url, {"companyName": name, "rpp": rpp, "page": 1})
        out = []
        for d in j.get("data", []):
            out.append(ZiCompany(id=str(d.get("id", "")), name=d.get("name", ""),
                                 website=d.get("companyWebsite", "") or d.get("website", ""),
                                 city=d.get("companyCity", ""), state=d.get("companyState", ""),
                                 country=d.get("companyCountry", ""), raw=d))
        return out

    def search_contacts(
        self,
        company_name: str = "",
        company_id: str = "",
        first_name: str = "",
        last_name: str = "",
        rpp: int = 25,
    ) -> list[ZiContact]:
        if self.mode == "gtm":
            attrs: dict[str, Any] = {}
            if company_id:
                attrs["companyId"] = company_id
            elif company_name:
                attrs["companyName"] = company_name
            if first_name:
                attrs["firstName"] = first_name
            if last_name:
                attrs["lastName"] = last_name
            url = f"{self.GTM_BASE}/data/v1/contacts/search"
            j = self._post(url, {"data": {"type": "ContactSearch", "attributes": attrs}},
                           params={"page[size]": rpp, "sort": "-contactAccuracyScore"})
            out = []
            for d in j.get("data", []):
                a = d.get("attributes", {}) or {}
                co = a.get("company", {}) or {}
                ml = a.get("managementLevel")
                if isinstance(ml, list):
                    ml = ", ".join(ml)
                out.append(ZiContact(
                    id=str(d.get("id", "")), first_name=a.get("firstName", ""), last_name=a.get("lastName", ""),
                    job_title=a.get("jobTitle", ""), management_level=ml or "",
                    company_id=str(co.get("id", "")), company_name=co.get("name", ""),
                    accuracy=a.get("contactAccuracyScore"), has_email=a.get("hasEmail"),
                    has_direct_phone=a.get("hasDirectPhone"), has_mobile_phone=a.get("hasMobilePhone"),
                    raw=_strip_contact_data(a)))
            return out
        body: dict[str, Any] = {"rpp": rpp, "page": 1}
        if company_id:
            body["companyId"] = company_id
        elif company_name:
            body["companyName"] = company_name
        if first_name:
            body["firstName"] = first_name
        if last_name:
            body["lastName"] = last_name
        url = f"{self.LEGACY_BASE}/search/contact"
        j = self._post(url, body)
        out = []
        for d in j.get("data", []):
            out.append(ZiContact(
                id=str(d.get("id", "")), first_name=d.get("firstName", ""), last_name=d.get("lastName", ""),
                job_title=d.get("jobTitle", ""), management_level=d.get("managementLevel", "") or "",
                company_id=str((d.get("company") or {}).get("id", d.get("companyId", ""))),
                company_name=(d.get("company") or {}).get("name", d.get("companyName", "")),
                accuracy=d.get("contactAccuracyScore"), has_email=d.get("hasEmail"),
                has_direct_phone=d.get("hasDirectPhone"), has_mobile_phone=d.get("hasMobilePhone"),
                raw=_strip_contact_data(d)))
        return out

    # ------------------------------------------------------------ high-level
    def lookup_attendee(self, first_name: str, last_name: str, company: str) -> dict:
        """Best-effort match of one attendee: company record + their contact record + top execs.
        Returns a dict with no PII beyond names/titles."""
        result: dict[str, Any] = {"company": None, "person": None, "top_contacts": [], "error": None}
        try:
            cos = self.search_companies(company, rpp=5)
            best = _pick_company(company, cos)
            if best:
                result["company"] = best.__dict__ | {"raw": None}
            people = self.search_contacts(company_id=best.id if best else "", company_name="" if best else company,
                                          first_name=first_name, last_name=last_name, rpp=5)
            if people:
                result["person"] = _contact_public(people[0])
            if best:
                execs = self.search_contacts(company_id=best.id, rpp=10)
                result["top_contacts"] = [_contact_public(c) for c in execs[:10]]
        except CreditGuardError:
            raise
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)
        return result


_PII_KEYS = {"email", "phone", "mobilePhone", "directPhone", "personalEmail", "supplementalEmail",
             "emailAlt", "directPhoneAlt", "mobilePhoneAlt", "companyPhone"}


def _strip_contact_data(a: dict) -> dict:
    """Defensive: even if an endpoint someday returns contact data, drop it."""
    return {k: v for k, v in a.items() if k not in _PII_KEYS}


def _contact_public(c: ZiContact) -> dict:
    return {
        "zoominfo_id": c.id, "name": c.name, "title": c.job_title, "management_level": c.management_level,
        "company": c.company_name, "accuracy": c.accuracy,
        "zoominfo_has_email": c.has_email, "zoominfo_has_direct_phone": c.has_direct_phone,
        "zoominfo_has_mobile": c.has_mobile_phone,
    }


_STOP = {"inc", "llc", "co", "corp", "corporation", "company", "ltd", "the", "of", "and", "&", "incorporated",
         "lp", "llp", "dba", "group", "holdings", "services", "service"}


def _toks(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _STOP}


def _pick_company(query: str, cands: list[ZiCompany]) -> Optional[ZiCompany]:
    if not cands:
        return None
    q = _toks(query)
    best, best_score = None, -1.0
    for c in cands:
        t = _toks(c.name)
        if not q or not t:
            score = 0.0
        else:
            score = len(q & t) / max(1, min(len(q), len(t)))
            if q == t:
                score += 1
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 0.5 else cands[0]
