# 🎟️ Conference Agent

**Your conference app only shows names. This agent turns them into booked meetings.**

Most conference apps (Cvent, Swapcard, Whova, Trimble Insight…) list attendees with just a name and maybe a company, and let you send a meeting request in-app. Doing that for 1,000 attendees by hand is a full day. Conference Agent does it for you:

1. **Paste your website** → the agent reads it and drafts your ICP, your "who to skip" list, and a meeting-request template in your voice. Edit anything.
2. **Mirror your phone** (macOS iPhone Mirroring) → the agent scrolls the attendee list in the conference app, screenshots every screen, and reads out every name / title / company with vision.
3. **Research every attendee** → free ZoomInfo search (title, seniority, *whether* they have an email/phone — without unlocking it), FMCSA carrier census (trucking), web search + homepage. An LLM decides fit / skip with a reason and writes a one-line description of what *their* company does.
4. **Approve** → each message is your template with one swapped clause. Edit, tick, and the agent opens each profile in the app via search, **verifies the name with vision**, pastes your message and taps Send.
5. **Unlock (paid)** → if you want email, phone and LinkedIn to sync to your CRM, pay per contact (Stripe) and get a HubSpot / Salesforce-ready CSV. Research itself never spends a credit.

Built by [Justin Lu](https://truckpedia.io) after running it live at a trucking conference. Works for any industry — the trucking bits (FMCSA) are just a plugin.

## 🔒 ZoomInfo credit guarantee

The research pipeline **cannot** spend ZoomInfo credits:

- `conference_agent/zoominfo.py` implements only `companies/search` and `contacts/search` — both [free](https://docs.zoominfo.com/docs/credit-usage-and-limits).
- Every URL passes a guard that raises `CreditGuardError` on `/enrich`, `/lookup/contact`, `/bulk`, `/redeem`, `/research`, `/account-summary`.
- The client has no `enrich()` method. `tests/test_credit_guard.py` asserts it.
- Any email/phone fields are stripped from responses defensively.

Paid unlock lives in a separate module (`unlock.py`) that refuses to run without a verified Stripe payment covering exactly those contact IDs. Enrich costs 1 bulk credit per *new* record on the operator's ZoomInfo account — that's what the user is paying for.

## Quick start

```bash
git clone https://github.com/Justin0101lu/conference-agent && cd conference-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
export OPENAI_API_KEY=sk-...                                   # or ANTHROPIC_API_KEY
export ZOOMINFO_CLIENT_ID=... ZOOMINFO_CLIENT_SECRET=...       # optional (GTM API, client-credentials)
export STRIPE_SECRET_KEY=sk_live_... STRIPE_PRICE_CENTS=300    # optional, enables paid unlock
streamlit run app.py
```

No phone? Upload a CSV or paste `Name | Title | Company` lines in step 2. CLI for batch:

```bash
python -m conference_agent.cli attendees.csv --config config.yaml --out results/
```

## Phone control (macOS)

Uses Apple's **iPhone Mirroring**. Requirements: macOS 15+, iPhone on iOS 18+, same Apple ID, phone **locked**. Mouse/keyboard events go through CGEvents (JXA), text is delivered via clipboard → iOS *Paste* popup (typing and ⌘V don't work through Mirroring).

Each conference app has its own tap points. Recipes live in `conference_agent/recipes/*.yaml` (screenshot-pixel coordinates at 2×). `trimble_insight.yaml` is calibrated; `generic.yaml` is a template. Use the **⚙️ Calibrate** tab to screenshot, set points, test-tap, and export a new recipe.

Safety: the send loop opens a profile, reads the name back with vision, and **skips** on mismatch. Start with *Dry run* on.

## Configure for your company

```yaml
seller:   {company: Acme, sender_name: Jane, what_we_do: "..."}
icp:      {description: "...", exclude: "..."}
enrichers: [web]              # add fmcsa for US trucking; write your own in enrichers.py
message:
  template: |                 # {first_name} {company_description} {company} {sender}
    Hi {first_name},
    I build X for companies like yours - {company_description}. ...
```

Only `{company_description}` is AI-written (6–14 words, e.g. *"dry bulk carriers running owner-operators across dump, hopper, and pneumatic trailers"*). Everything else is verbatim your template — it reads like you every time.

## Hosting

- **Research + approve + unlock**: any host — Streamlit Community Cloud, Docker (`Dockerfile` included), Fly, Railway.
- **Phone capture + auto-send**: must run on the Mac that has iPhone Mirroring (the host build shows those buttons disabled). Typical setup: host the web app, and run `streamlit run app.py` locally on show days.

## Tests

```bash
pytest -q
```

## License

MIT
