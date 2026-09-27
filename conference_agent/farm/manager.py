"""Emulator farm manager: one Android emulator per user session, on this host.

Free-tier design: runs wherever this process runs (your Mac, an Oracle Always-Free ARM VM, ...).
Each session = an AVD clone booted headless on its own console port. Screen streaming to the
browser is provided by ws-scrcpy (see farm/ws-scrcpy) or by polling screenshots as a fallback.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .device import ADB, AndroidDevice, DeviceError

ANDROID_HOME = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or os.path.expanduser(
    "~/Library/Android/sdk")
EMULATOR = shutil.which("emulator") or f"{ANDROID_HOME}/emulator/emulator"
AVDMANAGER = shutil.which("avdmanager") or f"{ANDROID_HOME}/cmdline-tools/latest/bin/avdmanager"
BASE_AVD = os.environ.get("FARM_BASE_AVD", "farm0")
SYSTEM_IMAGE = os.environ.get("FARM_SYSTEM_IMAGE", "system-images;android-34;google_apis;arm64-v8a")
STATE_DIR = Path(os.environ.get("FARM_STATE_DIR", os.path.expanduser("~/.conference-agent/farm")))
MAX_SESSIONS = int(os.environ.get("FARM_MAX_SESSIONS", "3"))
IDLE_TIMEOUT_S = int(os.environ.get("FARM_IDLE_TIMEOUT_S", str(2 * 3600)))


@dataclass
class Session:
    id: str
    avd: str
    port: int
    serial: str
    pid: int
    created: float
    last_used: float
    status: str = "booting"  # booting | ready | stopped | error
    error: str = ""
    meta: dict = field(default_factory=dict)

    def device(self) -> AndroidDevice:
        return AndroidDevice(self.serial)


class Farm:
    def __init__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state_file = STATE_DIR / "sessions.json"
        self.sessions: dict[str, Session] = self._load()

    # ------------------------------------------------------------ state
    def _load(self) -> dict[str, Session]:
        if self.state_file.exists():
            try:
                return {k: Session(**v) for k, v in json.load(open(self.state_file)).items()}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save(self) -> None:
        json.dump({k: asdict(v) for k, v in self.sessions.items()}, open(self.state_file, "w"), indent=1)

    def _alive(self, s: Session) -> bool:
        try:
            os.kill(s.pid, 0)
        except OSError:
            return False
        return True

    def refresh(self) -> None:
        for s in list(self.sessions.values()):
            if s.status != "stopped" and not self._alive(s):
                s.status = "stopped"
            if s.status == "booting" and s.device().booted():
                s.status = "ready"
            if s.status == "ready" and time.time() - s.last_used > IDLE_TIMEOUT_S:
                self.stop(s.id)
        self._save()

    def active(self) -> list[Session]:
        self.refresh()
        return [s for s in self.sessions.values() if s.status in ("booting", "ready")]

    # ------------------------------------------------------------ lifecycle
    def _free_port(self) -> int:
        used = {s.port for s in self.active()}
        try:
            out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10).stdout
            used |= {int(m) for m in re.findall(r"emulator-(\d+)", out)}
        except Exception:  # noqa: BLE001
            pass
        for p in range(5554, 5554 + 2 * (MAX_SESSIONS + 4), 2):
            if p not in used:
                return p
        raise DeviceError("no free emulator port")

    def _ensure_avd(self, name: str) -> None:
        avd_root = Path(os.environ.get("ANDROID_AVD_HOME", os.path.expanduser("~/.android/avd")))
        if (avd_root / f"{name}.ini").exists():
            return
        base_ini = avd_root / f"{BASE_AVD}.ini"
        if base_ini.exists():
            # clone the base AVD (fast, no sdkmanager). Skip snapshots/locks/caches so the clone is independent.
            src_dir = avd_root / f"{BASE_AVD}.avd"
            dst_dir = avd_root / f"{name}.avd"
            shutil.copytree(src_dir, dst_dir, symlinks=True,
                            ignore=shutil.ignore_patterns("snapshots", "*.lock", "cache.img*", "*.qcow2.tmp"))
            txt = base_ini.read_text().replace(BASE_AVD, name)
            (avd_root / f"{name}.ini").write_text(txt)
            cfg = dst_dir / "config.ini"
            cfg.write_text(cfg.read_text().replace(BASE_AVD, name))
            return
        subprocess.run([AVDMANAGER, "create", "avd", "-n", name, "-k", SYSTEM_IMAGE, "-d", "pixel_6", "--force"],
                       input=b"no\n", check=True, timeout=120)

    def create(self, meta: Optional[dict] = None) -> Session:
        if len(self.active()) >= MAX_SESSIONS:
            raise DeviceError(f"farm full ({MAX_SESSIONS} sessions). Try again later.")
        sid = uuid.uuid4().hex[:8]
        avd = f"farm_{sid}"
        self._ensure_avd(avd)
        port = self._free_port()
        log = open(STATE_DIR / f"{avd}.log", "w")
        proc = subprocess.Popen(
            [EMULATOR, "-avd", avd, "-port", str(port), "-no-window", "-no-audio", "-no-boot-anim",
             "-gpu", "swiftshader_indirect", "-no-snapshot-save", "-no-snapshot-load", "-netdelay", "none", "-netspeed", "full"],
            stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "ANDROID_HOME": ANDROID_HOME,
                                                      "ANDROID_SDK_ROOT": ANDROID_HOME},
            start_new_session=True)
        s = Session(id=sid, avd=avd, port=port, serial=f"emulator-{port}", pid=proc.pid, created=time.time(),
                    last_used=time.time(), meta=meta or {})
        self.sessions[sid] = s
        self._save()
        return s

    def wait_ready(self, sid: str, timeout: int = 240) -> Session:
        s = self.sessions[sid]
        try:
            s.device().wait_boot(timeout)
            s.status = "ready"
            # sane defaults for automation
            d = s.device()
            d.shell("settings put global window_animation_scale 0")
            d.shell("settings put global transition_animation_scale 0")
            d.shell("settings put global animator_duration_scale 0")
            d.shell("settings put system screen_off_timeout 2147483647")
            d.shell("svc power stayon true")
        except DeviceError as e:
            s.status, s.error = "error", str(e)
        self._save()
        return s

    def touch(self, sid: str) -> None:
        if sid in self.sessions:
            self.sessions[sid].last_used = time.time()
            self._save()

    def stop(self, sid: str) -> None:
        s = self.sessions.get(sid)
        if not s:
            return
        try:
            subprocess.run([ADB, "-s", s.serial, "emu", "kill"], timeout=15, capture_output=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.killpg(s.pid, 15)
        except OSError:
            pass
        s.status = "stopped"
        self._save()
        # delete the cloned AVD to reclaim disk
        avd_root = Path(os.environ.get("ANDROID_AVD_HOME", os.path.expanduser("~/.android/avd")))
        shutil.rmtree(avd_root / f"{s.avd}.avd", ignore_errors=True)
        (avd_root / f"{s.avd}.ini").unlink(missing_ok=True)

    def get(self, sid: str) -> Optional[Session]:
        self.refresh()
        return self.sessions.get(sid)


def preflight() -> dict:
    """What's installed? Used by the UI to show setup instructions."""
    return {
        "adb": os.path.exists(ADB), "emulator": os.path.exists(EMULATOR), "android_home": ANDROID_HOME,
        "base_avd": (Path(os.path.expanduser("~/.android/avd")) / f"{BASE_AVD}.ini").exists(),
        "max_sessions": MAX_SESSIONS,
    }
