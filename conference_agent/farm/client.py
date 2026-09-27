"""Thin client the web app uses to talk to a farm server."""
from __future__ import annotations

import time
from typing import Optional

import requests


class FarmClient:
    def __init__(self, base_url: str, token: str, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.h = {"X-Farm-Token": token}
        self.timeout = timeout
        self.token = token

    def _r(self, method: str, path: str, **kw) -> dict:
        r = requests.request(method, self.base + path, headers=self.h, timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
        return r.json()

    def health(self) -> dict:
        return requests.get(self.base + "/health", timeout=15).json()

    def create(self) -> dict:
        return self._r("POST", "/sessions")

    def status(self, sid: str) -> dict:
        return self._r("GET", f"/sessions/{sid}")

    def wait_ready(self, sid: str, timeout: int = 300, cb=None) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.status(sid)
            if cb:
                cb(s)
            if s["status"] in ("ready", "error", "stopped"):
                return s
            time.sleep(4)
        raise TimeoutError("emulator did not boot in time")

    def stop(self, sid: str) -> None:
        self._r("DELETE", f"/sessions/{sid}")

    def view_url(self, sid: str) -> str:
        return f"{self.base}/sessions/{sid}/view?token={self.token}"

    def screen(self, sid: str) -> bytes:
        return requests.get(f"{self.base}/sessions/{sid}/screen.png", headers=self.h, timeout=30).content

    def install(self, sid: str, apk_url: Optional[str] = None, package: Optional[str] = None) -> dict:
        return self._r("POST", f"/sessions/{sid}/install", json={"apk_url": apk_url, "package": package})

    def ui(self, sid: str) -> dict:
        return self._r("GET", f"/sessions/{sid}/ui")

    def capture(self, sid: str, cfg: dict, max_scrolls: int = 300) -> str:
        return self._r("POST", f"/sessions/{sid}/capture", json={"cfg": _safe_cfg(cfg), "max_scrolls": max_scrolls})["job"]

    def send(self, sid: str, cfg: dict, targets: list[dict], dry_run: bool = True) -> str:
        return self._r("POST", f"/sessions/{sid}/send", json={"cfg": _safe_cfg(cfg), "targets": targets, "dry_run": dry_run})["job"]

    def job(self, job: str) -> dict:
        return self._r("GET", f"/jobs/{job}")


def _safe_cfg(cfg: dict) -> dict:
    """Only the LLM block is needed server-side; never ship ZoomInfo/Stripe secrets to the farm."""
    return {"llm": dict(cfg.get("llm", {}))}
