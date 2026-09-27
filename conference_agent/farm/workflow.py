"""Conference-app workflow on an Android device — app-agnostic.

Capture: scroll the attendee list, read the accessibility tree, cluster text nodes into rows
(name / title / company), dedupe, stop when nothing new appears.

Send: find the app's search box → type name → open first match → verify name in tree → find a
message/meeting button → type message → tap send → back. Every "find X" first tries deterministic
matching on the accessibility tree; if that fails, an LLM is given the visible labels and asked
which one to tap. Each successful choice is remembered in a per-app recipe so subsequent
contacts are deterministic.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

from ..llm import _complete, _json
from .device import AndroidDevice, DeviceError, Node

RECIPES = Path.home() / ".conference-agent" / "android_recipes"

SEARCH_WORDS = ("search", "find", "filter")
MESSAGE_WORDS = ("message", "request meeting", "meeting", "connect", "chat", "contact", "schedule")
SEND_WORDS = ("send", "submit", "request", "done")
TITLE_HINTS = re.compile(r"\b(ceo|cfo|coo|cio|cto|vp|vice president|president|director|manager|head|chief|owner|"
                         r"founder|partner|engineer|analyst|administrator|specialist|lead|controller|coordinator|"
                         r"supervisor|officer|consultant|sales|operations|dispatch)\b", re.I)


# ----------------------------------------------------------------- recipe
def load_recipe(package: str) -> dict:
    p = RECIPES / f"{package}.json"
    return json.load(open(p)) if p.exists() else {}


def save_recipe(package: str, r: dict) -> None:
    RECIPES.mkdir(parents=True, exist_ok=True)
    json.dump(r, open(RECIPES / f"{package}.json", "w"), indent=1)


# ------------------------------------------------------------------ helpers
def _texts(d: AndroidDevice) -> list[Node]:
    return [n for n in d.nodes() if n.label.strip() and n.height < 400]


def _rows_from_tree(nodes: list[Node]) -> list[dict]:
    """Group visible text nodes by their clickable list-item ancestor bounds; fall back to y-proximity."""
    # sort by y then x
    nodes = sorted(nodes, key=lambda n: (n.bounds[1], n.bounds[0]))
    rows: list[list[Node]] = []
    for n in nodes:
        if rows and abs(n.bounds[1] - rows[-1][-1].bounds[1]) < 110 and n.bounds[1] < rows[-1][0].bounds[3] + 90:
            rows[-1].append(n)
        else:
            rows.append([n])
    out = []
    for r in rows:
        labels = [x.label.strip() for x in r if x.label.strip()]
        labels = [l for l in labels if len(l) > 1 and not re.fullmatch(r"[\W\d]+", l)]
        if not labels:
            continue
        name, title, company = _assign(labels)
        if name and _looks_like_name(name):
            out.append({"name": name, "title": title, "company": company, "y": r[0].center[1]})
    return out


def _assign(labels: list[str]) -> tuple[str, str, str]:
    name = labels[0]
    rest = labels[1:]
    title = company = ""
    for l in rest:
        # "Title at Company" / "Title, Company" / "Title | Company"
        m = re.match(r"^(.*?)\s+(?:at|@|-|–|\||,)\s+(.+)$", l)
        if m and TITLE_HINTS.search(m.group(1)) and not title:
            title, company = m.group(1).strip(), m.group(2).strip()
        elif TITLE_HINTS.search(l) and not title:
            title = l
        elif not company:
            company = l
    return name, title, company


def _looks_like_name(s: str) -> bool:
    w = s.split()
    return 2 <= len(w) <= 4 and all(re.match(r"^[A-Za-z][A-Za-z'.\-]*$", x) for x in w) and not TITLE_HINTS.search(s)


def _same_person(a: str, b: str) -> bool:
    ta = set(re.findall(r"[a-z]+", a.lower()))
    tb = set(re.findall(r"[a-z]+", b.lower()))
    return bool(ta and tb) and len(ta & tb) >= min(2, len(tb))


def _ask_llm_which(cfg: dict, goal: str, labels: list[str]) -> Optional[str]:
    if not labels:
        return None
    sys_p = ("You control a mobile app for a user. Given a goal and the list of visible UI labels, reply ONLY "
             'with JSON {"label": "<exact label to tap>", "none": true|false}. Set none=true if nothing fits.')
    out = _json(_complete(cfg, sys_p, f"GOAL: {goal}\nLABELS:\n" + "\n".join(f"- {l}" for l in labels[:80]), 80))
    if out.get("none") or not out.get("label"):
        return None
    return out["label"]


def _find_and_tap(d: AndroidDevice, cfg: dict, recipe: dict, key: str, words: tuple[str, ...], goal: str,
                  prefer_editable: bool = False) -> bool:
    # 1) remembered
    if recipe.get(key):
        r = recipe[key]
        ns = d.find(res_id=r.get("res_id", "")) if r.get("res_id") else d.find(text=r.get("label", ""))
        if ns:
            d.tap_node(ns[0]); return True
    nodes = d.nodes()
    # 2) heuristics
    cands = []
    for n in nodes:
        lab = (n.label + " " + n.res_id).lower()
        if prefer_editable and n.editable:
            cands.append((0, n))
        elif any(w in lab for w in words):
            cands.append((1 if n.clickable else 2, n))
    if cands:
        cands.sort(key=lambda t: (t[0], t[1].depth))
        n = cands[0][1]
        recipe[key] = {"res_id": n.res_id, "label": n.label}
        d.tap_node(n); return True
    # 3) LLM
    labels = [n.label for n in nodes if n.label.strip() and (n.clickable or n.editable)]
    pick = _ask_llm_which(cfg, goal, labels)
    if pick:
        ns = d.find(text=pick, contains=False) or d.find(text=pick)
        if ns:
            recipe[key] = {"res_id": ns[0].res_id, "label": ns[0].label}
            d.tap_node(ns[0]); return True
    return False


# ------------------------------------------------------------------- capture
def capture_attendees(d: AndroidDevice, cfg: dict, max_scrolls: int = 300,
                      progress: Optional[Callable[[int, list[dict]], None]] = None) -> list[dict]:
    """Assumes the attendee list is on screen. Scrolls to the end collecting rows."""
    d.scroll_up_to_top()
    seen: dict[str, dict] = {}
    stale = 0
    for i in range(max_scrolls):
        rows = _rows_from_tree(_texts(d))
        new = 0
        for r in rows:
            k = f"{r['name'].lower()}|{r['company'].lower()}"
            if k not in seen:
                seen[k] = {"name": r["name"], "title": r["title"], "company": r["company"], "screen": i}
                new += 1
        if progress:
            progress(i, list(seen.values()))
        stale = stale + 1 if new == 0 else 0
        if stale >= 3:
            break
        d.scroll_down(0.7)
        time.sleep(0.8)
    return list(seen.values())


# --------------------------------------------------------------------- send
def send_message(d: AndroidDevice, cfg: dict, attendee: dict, message: str, dry_run: bool = True,
                 package: str = "") -> dict:
    package = package or d.current_package()
    recipe = load_recipe(package)
    log = {"name": attendee["name"], "ok": False, "step": "search", "detail": "", "package": package}
    try:
        if not _find_and_tap(d, cfg, recipe, "search", SEARCH_WORDS, "open the attendee search box", prefer_editable=True):
            log["detail"] = "search box not found"; return log
        time.sleep(0.6)
        # make sure an editable is focused; if the tap opened a search screen, tap its field
        if not any(n.editable for n in d.nodes()):
            _find_and_tap(d, cfg, recipe, "search_field", SEARCH_WORDS, "focus the search text field", prefer_editable=True)
        d.select_all_delete()
        d.type_text(attendee["name"])
        time.sleep(1.8)
        # open first result that matches the name
        hits = [n for n in d.nodes() if n.label and _same_person(n.label, attendee["name"])]
        if not hits:
            log["detail"] = "no search result matched"; _recover(d); return log
        d.tap_node(sorted(hits, key=lambda n: n.bounds[1])[0])
        time.sleep(1.5)
        log["step"] = "verify"
        if not any(_same_person(n.label, attendee["name"]) for n in d.nodes() if n.label):
            log["detail"] = "profile name mismatch — skipped"; _recover(d); return log
        log["step"] = "compose"
        if not _find_and_tap(d, cfg, recipe, "message_button", MESSAGE_WORDS,
                             "open the send-message / request-meeting action on this profile"):
            log["detail"] = "message button not found"; _recover(d); return log
        time.sleep(1.2)
        if not any(n.editable for n in d.nodes()):
            _find_and_tap(d, cfg, recipe, "message_field", ("message", "note", "write"), "focus the message text field",
                          prefer_editable=True)
            time.sleep(0.5)
        fields = [n for n in d.nodes() if n.editable]
        if fields:
            d.tap_node(sorted(fields, key=lambda n: -n.height)[0])
        d.type_text(message)
        time.sleep(0.8)
        log["screenshot"] = d.screenshot(f"/tmp/ca_android_{attendee['name'].replace(' ', '_')}.png")
        if dry_run:
            log.update(ok=True, step="dry_run", detail="composed, not sent"); _recover(d, 3); save_recipe(package, recipe)
            return log
        log["step"] = "send"
        if not _find_and_tap(d, cfg, recipe, "send_button", SEND_WORDS, "send the message / submit the meeting request"):
            log["detail"] = "send button not found"; _recover(d, 3); return log
        time.sleep(1.2)
        log.update(ok=True, detail="sent")
        _recover(d, 2)
        save_recipe(package, recipe)
        return log
    except DeviceError as e:
        log["detail"] = str(e)
        return log


def _recover(d: AndroidDevice, backs: int = 2) -> None:
    for _ in range(backs):
        d.back(); time.sleep(0.6)
