"""Tests for the ZoomInfo credit guard — the one thing this product must never get wrong."""
import pytest

from conference_agent.zoominfo import CreditGuardError, ZoomInfoSearchClient, _guard_url, _strip_contact_data


@pytest.mark.parametrize("url", [
    "https://api.zoominfo.com/gtm/data/v1/contacts/enrich",
    "https://api.zoominfo.com/gtm/data/v1/companies/enrich",
    "https://api.zoominfo.com/enrich/contact",
    "https://api.zoominfo.com/enrich/company",
    "https://api.zoominfo.com/lookup/contact/123",
    "https://api.zoominfo.com/bulk/contact/redeem",
    "https://api.zoominfo.com/gtm/data/v1/account-summary",
    "https://api.zoominfo.com/gtm/data/v1/contacts/research",
])
def test_guard_blocks_paid_endpoints(url):
    with pytest.raises(CreditGuardError):
        _guard_url(url)


@pytest.mark.parametrize("url", [
    "https://api.zoominfo.com/gtm/oauth/v1/token",
    "https://api.zoominfo.com/gtm/data/v1/contacts/search",
    "https://api.zoominfo.com/gtm/data/v1/companies/search",
    "https://api.zoominfo.com/authenticate",
    "https://api.zoominfo.com/search/contact",
    "https://api.zoominfo.com/search/company",
])
def test_guard_allows_free_endpoints(url):
    _guard_url(url)


def test_client_has_no_enrich_method():
    assert not any(n.lower().startswith(("enrich", "redeem", "unlock", "reveal")) for n in dir(ZoomInfoSearchClient))


def test_post_is_guarded(monkeypatch):
    c = ZoomInfoSearchClient("gtm", client_id="x", client_secret="y")
    monkeypatch.setattr(c, "_auth", lambda: "tok")
    with pytest.raises(CreditGuardError):
        c._post("https://api.zoominfo.com/gtm/data/v1/contacts/enrich", {})


def test_pii_stripped():
    a = {"firstName": "A", "email": "a@b.com", "mobilePhone": "1", "phone": "2", "jobTitle": "CEO",
         "hasEmail": True}
    s = _strip_contact_data(a)
    assert "email" not in s and "mobilePhone" not in s and "phone" not in s
    assert s["hasEmail"] is True and s["jobTitle"] == "CEO"
