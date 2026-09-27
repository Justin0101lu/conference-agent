"""iPhone Mirroring driver (macOS only).

Controls the conference app through Apple's iPhone Mirroring window using CGEvents via JXA
(mouse/scroll/right-click). Keyboard typing and cmd+V do NOT work through Mirroring — text is
delivered via clipboard + iOS "Paste" popup.

Coordinates: recipes use *screenshot pixels* (2x Retina). native = window_origin + px / 2.

Two jobs:
  capture_attendee_list()  -> scroll + screenshot the whole list, vision-extract rows
  send_message()           -> tap row, open message box, paste, send, verify, back
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml

APP = "iPhone Mirroring"
SCALE = 2.0
RECIPES_DIR = Path(__file__).resolve().parent / "recipes"


class PhoneError(RuntimeError):
    pass


# ----------------------------------------------------------------- low level
def _osa(script: str) -> str:
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise PhoneError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def _osa_as(script: str) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise PhoneError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def is_available() -> bool:
    if os.uname().sysname != "Darwin":
        return False
    r = subprocess.run(["pgrep", "-x", "iPhone Mirroring"], capture_output=True, text=True, timeout=5)
    return r.returncode == 0


def window_rect() -> tuple[int, int, int, int]:
    """(x, y, w, h) of the mirroring window in native points. Always query fresh — it moves.
    Uses CGWindowList (no Automation/Accessibility permission needed)."""
    js_path = Path(__file__).resolve().parent / "winrect.js"
    r = subprocess.run(["osascript", "-l", "JavaScript", str(js_path)], capture_output=True, text=True, timeout=20)
    out = r.stdout.strip()
    nums = [int(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", out)]
    if r.returncode != 0 or len(nums) < 4:
        raise PhoneError("iPhone Mirroring window not found. Open iPhone Mirroring, lock your phone, "
                         "and open the conference app.")
    global _WIN_ID
    _WIN_ID = nums[4] if len(nums) > 4 else 0
    return nums[0], nums[1], nums[2], nums[3]


_WIN_ID = 0


def _native(px: float, py: float) -> tuple[int, int]:
    x, y, _, _ = window_rect()
    return int(x + px / SCALE), int(y + py / SCALE)


def tap(px: float, py: float, hold_loops: int = 2_000_000) -> None:
    nx, ny = _native(px, py)
    _osa(f"""
ObjC.import("CoreGraphics");
var d = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseDown, $.CGPointMake({nx}, {ny}), $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, d);
for (var i = 0; i < {hold_loops}; i++) {{}}
var u = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseUp, $.CGPointMake({nx}, {ny}), $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, u);
"ok"
""")


def right_click(px: float, py: float) -> None:
    nx, ny = _native(px, py)
    _osa(f"""
ObjC.import("CoreGraphics");
var d = $.CGEventCreateMouseEvent(null, $.kCGEventRightMouseDown, $.CGPointMake({nx}, {ny}), $.kCGMouseButtonRight);
$.CGEventPost($.kCGHIDEventTap, d);
for (var i = 0; i < 2000000; i++) {{}}
var u = $.CGEventCreateMouseEvent(null, $.kCGEventRightMouseUp, $.CGPointMake({nx}, {ny}), $.kCGMouseButtonRight);
$.CGEventPost($.kCGHIDEventTap, u);
"ok"
""")


def scroll(px: float, py: float, ticks: int = 80, delta: int = -10, delay_loops: int = 50_000) -> None:
    """delta<0 scrolls down (reveals more). ~80 ticks @ -10 ≈ one screen. Too-fast scrolling locks the phone."""
    nx, ny = _native(px, py)
    _osa(f"""
ObjC.import("CoreGraphics");
var m = $.CGEventCreateMouseEvent(null, $.kCGEventMouseMoved, $.CGPointMake({nx}, {ny}), $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, m);
for (var d = 0; d < 5000000; d++) {{}}
for (var i = 0; i < {ticks}; i++) {{
  var s = $.CGEventCreateScrollWheelEvent(null, 0, 1, {delta});
  $.CGEventPost($.kCGHIDEventTap, s);
  for (var d2 = 0; d2 < {delay_loops}; d2++) {{}}
}}
"ok"
""")


def scroll_to_top(px: float, py: float) -> None:
    scroll(px, py, ticks=3000, delta=10, delay_loops=5000)


def screenshot(path: str) -> str:
    """Capture just the mirroring window. `screencapture -R` fails on some multi-scale displays, so we
    grab the full screen and crop (accounting for the backing scale factor)."""
    x, y, w, h = window_rect()
    # 1) by window id — works even if partially occluded, no display-geometry issues
    if _WIN_ID:
        r = subprocess.run(["screencapture", "-x", "-o", "-l", str(_WIN_ID), path], capture_output=True, timeout=20)
        if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            _normalise(path)
            return path
    # 2) by rect
    r = subprocess.run(["screencapture", "-x", f"-R{x},{y},{w},{h}", path], capture_output=True, timeout=20)
    if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
        _normalise(path)
        return path
    # 3) full screen + crop
    from PIL import Image
    full = path + ".full.png"
    subprocess.run(["screencapture", "-x", full], check=True, timeout=20)
    im = Image.open(full)
    # backing scale: full capture px / logical screen pt
    logical_w = _logical_screen_width()
    s = im.width / logical_w if logical_w else SCALE
    box = (int(x * s), int(y * s), int((x + w) * s), int((y + h) * s))
    im.crop(box).save(path)
    os.remove(full)
    return path


def _normalise(path: str) -> None:
    """Window captures include a title bar + shadow. Crop to the device area and force 688x1528 so recipe
    coordinates stay stable."""
    from PIL import Image
    im = Image.open(path)
    tw, th = int(344 * SCALE), int(764 * SCALE)
    if (im.width, im.height) == (tw, th):
        return
    # trim transparent shadow margins if present
    if im.mode == "RGBA":
        bbox = im.getchannel("A").getbbox()
        if bbox:
            im = im.crop(bbox)
    # drop a title bar if the aspect is taller than the device
    if im.height / im.width > th / tw + 0.02:
        extra = im.height - int(im.width * th / tw)
        im = im.crop((0, extra, im.width, im.height))
    im.convert("RGB").resize((tw, th)).save(path)


def _logical_screen_width() -> int:
    try:
        out = _osa("ObjC.import('AppKit'); $.NSScreen.mainScreen.frame.size.width;")
        return int(float(out))
    except Exception:  # noqa: BLE001
        return 0


def set_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)


def paste_via_popup(field_px: float, field_py: float, paste_offset: tuple[int, int] = (180, 10),
                    locate: Optional[Callable[[str, str], Optional[tuple[int, int]]]] = None) -> None:
    """Right-click the text field, then tap 'Paste' in the iOS popup. If a `locate(image, label)` vision
    callback is supplied, use it to find the Paste button; else use the recipe offset."""
    right_click(field_px, field_py)
    time.sleep(1.0)
    if locate:
        shot = screenshot("/tmp/ca_paste_popup.png")
        pt = locate(shot, "Paste")
        if pt:
            tap(*pt)
            return
    tap(field_px + paste_offset[0], field_py + paste_offset[1])


# ------------------------------------------------------------------- vision
def _img_b64(path: str) -> str:
    return base64.b64encode(open(path, "rb").read()).decode()


def vision_json(cfg: dict, image_path: str, prompt: str, max_tokens: int = 1500) -> dict | list:
    """Send an image + prompt to the configured LLM, parse JSON back."""
    llm = cfg.get("llm", {})
    provider = llm.get("provider", "openai")
    model = llm.get("vision_model") or llm.get("model") or ("gpt-4o-mini" if provider == "openai"
                                                             else "claude-3-5-haiku-latest")
    b64 = _img_b64(image_path)
    if provider == "openai":
        from openai import OpenAI
        cl = OpenAI(api_key=llm.get("api_key") or os.getenv("OPENAI_API_KEY"))
        r = cl.chat.completions.create(model=model, max_tokens=max_tokens, temperature=0, messages=[{
            "role": "user", "content": [{"type": "text", "text": prompt},
                                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}])
        txt = r.choices[0].message.content or ""
    else:
        import anthropic
        cl = anthropic.Anthropic(api_key=llm.get("api_key") or os.getenv("ANTHROPIC_API_KEY"))
        r = cl.messages.create(model=model, max_tokens=max_tokens, temperature=0, messages=[{
            "role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                      "data": b64}},
                                        {"type": "text", "text": prompt}]}])
        txt = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    m = re.search(r"[\[{].*[\]}]", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return []


EXTRACT_PROMPT = ("This is a screenshot of a conference app attendee list. Extract EVERY attendee row visible. "
                  "Return JSON list: [{\"name\":..., \"title\":..., \"company\":..., \"y\": <vertical pixel centre "
                  "of the row in this image>}]. Use empty strings for missing title/company. No commentary.")


def extract_rows(cfg: dict, image_path: str) -> list[dict]:
    rows = vision_json(cfg, image_path, EXTRACT_PROMPT)
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("name"):
            out.append({"name": str(r["name"]).strip(), "title": str(r.get("title", "")).strip(),
                        "company": str(r.get("company", "")).strip(), "y": r.get("y")})
    return out


def locate_label(cfg: dict, image_path: str, label: str) -> Optional[tuple[int, int]]:
    res = vision_json(cfg, image_path,
                      f"Find the UI element labelled '{label}' (button, menu item, or tab) in this phone screenshot. "
                      f"Return JSON {{\"x\": <centre x px>, \"y\": <centre y px>, \"found\": true|false}}.", 100)
    if isinstance(res, dict) and res.get("found") and res.get("x") is not None:
        return int(res["x"]), int(res["y"])
    return None


def read_profile_name(cfg: dict, image_path: str) -> str:
    res = vision_json(cfg, image_path, "This is a phone screenshot of a person's profile in a conference app. "
                                        "Return JSON {\"name\": \"<the person's full name as shown>\"}.", 60)
    return (res.get("name", "") if isinstance(res, dict) else "").strip()


# ------------------------------------------------------------------- recipes
@dataclass
class Recipe:
    name: str
    data: dict

    @classmethod
    def load(cls, name_or_path: str) -> "Recipe":
        p = Path(name_or_path)
        if not p.exists():
            p = RECIPES_DIR / f"{name_or_path}.yaml"
        d = yaml.safe_load(open(p))
        return cls(d.get("name", p.stem), d)

    @staticmethod
    def available() -> list[str]:
        return sorted(p.stem for p in RECIPES_DIR.glob("*.yaml"))

    def pt(self, key: str) -> tuple[float, float]:
        v = self.data["points"][key]
        return float(v[0]), float(v[1])


# ------------------------------------------------------------------ workflows
def capture_attendee_list(cfg: dict, recipe: Recipe, out_dir: str, max_screens: int = 400,
                          progress: Optional[Callable[[int, list[dict]], None]] = None) -> list[dict]:
    """Scroll the whole attendee list, screenshot each screen, extract rows. Dedupes by name+company.
    Stops when a screen adds no new rows twice in a row (end of list)."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    sx, sy = recipe.pt("scroll_anchor")
    scroll_to_top(sx, sy)
    time.sleep(1.0)
    seen: dict[str, dict] = {}
    stale = 0
    for i in range(max_screens):
        shot = screenshot(f"{out_dir}/screen_{i:03d}.png")
        rows = extract_rows(cfg, shot)
        new = 0
        for r in rows:
            k = f"{r['name'].lower()}|{r['company'].lower()}"
            if k not in seen:
                seen[k] = {"name": r["name"], "title": r["title"], "company": r["company"], "screen": i}
                new += 1
        if progress:
            progress(i, list(seen.values()))
        stale = stale + 1 if new == 0 else 0
        if stale >= 2:
            break
        scroll(sx, sy, ticks=int(recipe.data.get("scroll_ticks", 80)))
        time.sleep(float(recipe.data.get("scroll_settle_s", 1.2)))
    return list(seen.values())


def _search_and_open(cfg: dict, recipe: Recipe, attendee: dict) -> bool:
    """Use the app's search box to find the attendee, tap the first result, verify the profile name."""
    tap(*recipe.pt("search_box"))
    time.sleep(0.8)
    set_clipboard(attendee["name"])
    paste_via_popup(*recipe.pt("search_box"), paste_offset=tuple(recipe.data.get("paste_offset", [180, 10])),
                    locate=lambda img, lab: locate_label(cfg, img, lab) if recipe.data.get("vision_paste") else None)
    time.sleep(float(recipe.data.get("search_settle_s", 2.0)))
    tap(*recipe.pt("first_result"))
    time.sleep(1.5)
    shot = screenshot("/tmp/ca_profile.png")
    shown = read_profile_name(cfg, shot)
    return _same_person(shown, attendee["name"])


def _same_person(a: str, b: str) -> bool:
    ta = set(re.findall(r"[a-z]+", a.lower()))
    tb = set(re.findall(r"[a-z]+", b.lower()))
    return bool(ta and tb) and len(ta & tb) >= min(2, len(tb))


def send_message(cfg: dict, recipe: Recipe, attendee: dict, message: str, dry_run: bool = False) -> dict:
    """Full send flow for one attendee. Returns {ok, step, detail}. Never sends if the profile name
    doesn't match (vision check)."""
    log = {"name": attendee["name"], "ok": False, "step": "open", "detail": ""}
    try:
        if not _search_and_open(cfg, recipe, attendee):
            log["detail"] = "profile name mismatch — skipped"
            _back(recipe, 2)
            return log
        log["step"] = "compose"
        tap(*recipe.pt("message_button"))
        time.sleep(1.5)
        tap(*recipe.pt("message_field"))
        time.sleep(0.8)
        set_clipboard(message)
        paste_via_popup(*recipe.pt("message_field"), paste_offset=tuple(recipe.data.get("paste_offset", [180, 10])),
                        locate=lambda img, lab: locate_label(cfg, img, lab) if recipe.data.get("vision_paste") else None)
        time.sleep(1.0)
        shot = screenshot("/tmp/ca_compose.png")
        log["screenshot"] = shot
        if dry_run:
            log.update(ok=True, step="dry_run", detail="composed, not sent")
            _back(recipe, int(recipe.data.get("back_taps_after_compose", 3)))
            return log
        log["step"] = "send"
        tap(*recipe.pt("send_button"))
        time.sleep(1.5)
        if "confirm_button" in recipe.data.get("points", {}):
            tap(*recipe.pt("confirm_button"))
            time.sleep(1.2)
        log.update(ok=True, detail="sent")
        _back(recipe, int(recipe.data.get("back_taps_after_send", 2)))
        return log
    except PhoneError as e:
        log["detail"] = str(e)
        return log


def _back(recipe: Recipe, n: int) -> None:
    for _ in range(n):
        tap(*recipe.pt("back_button"))
        time.sleep(1.0)
