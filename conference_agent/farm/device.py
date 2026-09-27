"""Android device driver over ADB.

Works against emulators (emulator-5554) or real phones. All UI reading goes through the
accessibility tree (`uiautomator dump`) so names/titles come back as text — no OCR.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, Optional

ADB = shutil.which("adb") or os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")


class DeviceError(RuntimeError):
    pass


@dataclass
class Node:
    text: str
    desc: str
    res_id: str
    cls: str
    bounds: tuple[int, int, int, int]  # x1,y1,x2,y2
    clickable: bool
    scrollable: bool
    editable: bool
    children: list["Node"] = field(default_factory=list)
    depth: int = 0

    @property
    def label(self) -> str:
        return self.text or self.desc

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]

    def walk(self) -> Iterable["Node"]:
        yield self
        for c in self.children:
            yield from c.walk()


def _parse_bounds(s: str) -> tuple[int, int, int, int]:
    m = re.findall(r"\d+", s or "")
    return tuple(int(v) for v in m[:4]) if len(m) >= 4 else (0, 0, 0, 0)  # type: ignore[return-value]


def _node(el: ET.Element, depth: int = 0) -> Node:
    a = el.attrib
    n = Node(text=a.get("text", ""), desc=a.get("content-desc", ""), res_id=a.get("resource-id", ""),
             cls=a.get("class", ""), bounds=_parse_bounds(a.get("bounds", "")),
             clickable=a.get("clickable") == "true", scrollable=a.get("scrollable") == "true",
             editable=a.get("class", "").endswith("EditText"), depth=depth)
    n.children = [_node(c, depth + 1) for c in el]
    return n


class AndroidDevice:
    def __init__(self, serial: str, timeout: int = 30):
        self.serial = serial
        self.timeout = timeout
        self._size: Optional[tuple[int, int]] = None

    # ------------------------------------------------------------ low level
    def adb(self, *args: str, binary: bool = False, timeout: Optional[int] = None) -> bytes | str:
        cmd = [ADB, "-s", self.serial, *args]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout or self.timeout)
        if r.returncode != 0:
            raise DeviceError(f"adb {' '.join(args[:3])} failed: {r.stderr.decode(errors='ignore')[:300]}")
        return r.stdout if binary else r.stdout.decode(errors="ignore")

    def shell(self, cmd: str, timeout: Optional[int] = None) -> str:
        return self.adb("shell", cmd, timeout=timeout)  # type: ignore[return-value]

    def booted(self) -> bool:
        try:
            return self.shell("getprop sys.boot_completed").strip() == "1"
        except DeviceError:
            return False

    def wait_boot(self, timeout: int = 240) -> None:
        t0 = time.time()
        subprocess.run([ADB, "-s", self.serial, "wait-for-device"], timeout=timeout)
        while time.time() - t0 < timeout:
            if self.booted():
                time.sleep(2)
                return
            time.sleep(2)
        raise DeviceError("device did not boot")

    def size(self) -> tuple[int, int]:
        if not self._size:
            m = re.search(r"(\d+)x(\d+)", self.shell("wm size"))
            self._size = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        return self._size

    # ------------------------------------------------------------- actions
    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {x} {y}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> None:
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {ms}")

    def scroll_down(self, fraction: float = 0.6) -> None:
        w, h = self.size()
        y1 = int(h * 0.75)
        y2 = int(h * (0.75 - fraction))
        self.swipe(w // 2, y1, w // 2, y2, 500)

    def scroll_up_to_top(self, times: int = 12) -> None:
        w, h = self.size()
        for _ in range(times):
            self.swipe(w // 2, int(h * 0.3), w // 2, int(h * 0.9), 200)

    def back(self) -> None:
        self.shell("input keyevent KEYCODE_BACK")

    def home(self) -> None:
        self.shell("input keyevent KEYCODE_HOME")

    def type_text(self, text: str) -> None:
        """Type into the focused field. Uses ADBKeyboard if installed (unicode + newlines), else input text."""
        if self._has_adbkeyboard():
            import base64
            b = base64.b64encode(text.encode("utf-8")).decode()
            self.shell(f"am broadcast -a ADB_INPUT_B64 --es msg {b}")
            return
        for i, line in enumerate(text.split("\n")):
            if i:
                self.shell("input keyevent KEYCODE_ENTER")
            if line:
                self.shell(f"input text {shlex.quote(_escape_for_input(line))}")

    def _has_adbkeyboard(self) -> bool:
        return "com.android.adbkeyboard" in self.shell("ime list -s")

    def enable_adbkeyboard(self) -> None:
        self.shell("ime enable com.android.adbkeyboard/.AdbIME")
        self.shell("ime set com.android.adbkeyboard/.AdbIME")

    def clear_field(self) -> None:
        self.shell("input keyevent KEYCODE_MOVE_END")
        self.shell("input keyevent --longpress " + " ".join(["KEYCODE_DEL"] * 60))

    def select_all_delete(self) -> None:
        self.shell("input keyevent KEYCODE_CTRL_LEFT KEYCODE_A")
        self.shell("input keyevent KEYCODE_DEL")

    # ------------------------------------------------------------ reading
    def screenshot(self, path: str) -> str:
        data = self.adb("exec-out", "screencap", "-p", binary=True)
        with open(path, "wb") as f:
            f.write(data)  # type: ignore[arg-type]
        return path

    def ui_tree(self) -> Node:
        for attempt in range(3):
            try:
                self.shell("uiautomator dump /sdcard/ui.xml", timeout=20)
                xml = self.adb("exec-out", "cat", "/sdcard/ui.xml", binary=True)
                root = ET.fromstring(xml)  # type: ignore[arg-type]
                return _node(root)
            except (DeviceError, ET.ParseError):
                time.sleep(1)
        raise DeviceError("uiautomator dump failed")

    def nodes(self) -> list[Node]:
        return [n for n in self.ui_tree().walk()]

    def find(self, text: str = "", res_id: str = "", cls: str = "", contains: bool = True,
             clickable: Optional[bool] = None) -> list[Node]:
        out = []
        for n in self.nodes():
            if text:
                lab = n.label.lower()
                ok = (text.lower() in lab) if contains else (lab == text.lower())
                if not ok:
                    continue
            if res_id and not n.res_id.endswith(res_id):
                continue
            if cls and not n.cls.endswith(cls):
                continue
            if clickable is not None and n.clickable != clickable:
                continue
            out.append(n)
        return out

    def tap_node(self, n: Node) -> None:
        self.tap(*n.center)

    def tap_text(self, text: str, contains: bool = True) -> bool:
        ns = self.find(text=text, contains=contains)
        # prefer clickable, else walk up: tap center of the text anyway
        ns.sort(key=lambda n: (not n.clickable, n.depth))
        if ns:
            self.tap_node(ns[0])
            return True
        return False

    def current_package(self) -> str:
        out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -1")
        m = re.search(r"([a-zA-Z0-9_.]+)/[a-zA-Z0-9_.]+", out)
        return m.group(1) if m else ""

    # -------------------------------------------------------------- apps
    def install_apk(self, path: str) -> None:
        self.adb("install", "-r", "-g", path, timeout=300)

    def install_url(self, url: str) -> None:
        import tempfile
        import urllib.request
        fd, p = tempfile.mkstemp(suffix=".apk")
        os.close(fd)
        urllib.request.urlretrieve(url, p)
        try:
            self.install_apk(p)
        finally:
            os.remove(p)

    def open_play_store(self, package: str) -> None:
        self.shell(f"am start -a android.intent.action.VIEW -d 'market://details?id={package}'")

    def launch(self, package: str) -> None:
        self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def packages(self) -> list[str]:
        return [l.replace("package:", "").strip() for l in self.shell("pm list packages -3").splitlines() if l]


def _escape_for_input(s: str) -> str:
    # `input text` treats space as separator; %s is the escape for space. Strip chars it can't send.
    s = s.replace(" ", "%s")
    return re.sub(r"[^\x20-\x7e%]", "", s)
