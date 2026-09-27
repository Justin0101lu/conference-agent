"""Farm HTTP API — runs on the host that has the emulators (your Mac). The hosted web app talks to it.

    uvicorn conference_agent.farm.server:app --host 0.0.0.0 --port 8765
    cloudflared tunnel --url http://localhost:8765     # free public URL, no port-forwarding

Auth: every request carries `X-Farm-Token` == FARM_TOKEN env.
"""
from __future__ import annotations

import base64
import os
import secrets
import threading
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .device import DeviceError
from .manager import Farm, preflight
from .workflow import capture_attendees, send_message

FARM_TOKEN = os.environ.get("FARM_TOKEN") or secrets.token_urlsafe(16)
app = FastAPI(title="Conference Agent Phone Farm")
farm = Farm()
_jobs: dict[str, dict] = {}


def auth(x_farm_token: Optional[str] = Header(default=None)) -> None:
    if x_farm_token != FARM_TOKEN:
        raise HTTPException(401, "bad token")


@app.get("/health")
def health():
    return {"ok": True, **preflight(), "active": len(farm.active())}


@app.post("/sessions", dependencies=[Depends(auth)])
def create_session():
    try:
        s = farm.create()
    except DeviceError as e:
        raise HTTPException(429, str(e))
    threading.Thread(target=farm.wait_ready, args=(s.id,), daemon=True).start()
    return {"id": s.id, "serial": s.serial, "status": s.status}


@app.get("/sessions/{sid}", dependencies=[Depends(auth)])
def get_session(sid: str):
    s = farm.get(sid)
    if not s:
        raise HTTPException(404)
    farm.touch(sid)
    return {"id": s.id, "serial": s.serial, "status": s.status, "error": s.error}


@app.delete("/sessions/{sid}", dependencies=[Depends(auth)])
def stop_session(sid: str):
    farm.stop(sid)
    return {"ok": True}


def _dev(sid: str):
    s = farm.get(sid)
    if not s or s.status != "ready":
        raise HTTPException(409, "session not ready")
    farm.touch(sid)
    return s.device()


@app.get("/sessions/{sid}/screen.png", dependencies=[Depends(auth)])
def screen(sid: str):
    d = _dev(sid)
    data = d.adb("exec-out", "screencap", "-p", binary=True)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-store"})


class Tap(BaseModel):
    x: int
    y: int


@app.post("/sessions/{sid}/tap", dependencies=[Depends(auth)])
def tap(sid: str, t: Tap):
    _dev(sid).tap(t.x, t.y)
    return {"ok": True}


class Swipe(BaseModel):
    x1: int; y1: int; x2: int; y2: int; ms: int = 300


@app.post("/sessions/{sid}/swipe", dependencies=[Depends(auth)])
def swipe(sid: str, s: Swipe):
    _dev(sid).swipe(s.x1, s.y1, s.x2, s.y2, s.ms)
    return {"ok": True}


class Text(BaseModel):
    text: str


@app.post("/sessions/{sid}/type", dependencies=[Depends(auth)])
def type_text(sid: str, t: Text):
    _dev(sid).type_text(t.text)
    return {"ok": True}


@app.post("/sessions/{sid}/key/{key}", dependencies=[Depends(auth)])
def key(sid: str, key: str):
    _dev(sid).shell(f"input keyevent {key.upper()}")
    return {"ok": True}


class Install(BaseModel):
    apk_url: Optional[str] = None
    package: Optional[str] = None  # opens Play Store page


@app.post("/sessions/{sid}/install", dependencies=[Depends(auth)])
def install(sid: str, i: Install):
    d = _dev(sid)
    if i.apk_url:
        d.install_url(i.apk_url)
    elif i.package:
        d.open_play_store(i.package)
    return {"ok": True, "packages": d.packages()}


@app.get("/sessions/{sid}/ui", dependencies=[Depends(auth)])
def ui(sid: str):
    d = _dev(sid)
    return {"package": d.current_package(),
            "nodes": [{"label": n.label, "id": n.res_id, "bounds": n.bounds, "clickable": n.clickable,
                       "editable": n.editable} for n in d.nodes() if n.label or n.editable]}


class CaptureReq(BaseModel):
    cfg: dict
    max_scrolls: int = 300


@app.post("/sessions/{sid}/capture", dependencies=[Depends(auth)])
def capture(sid: str, r: CaptureReq):
    d = _dev(sid)
    job = secrets.token_hex(4)
    _jobs[job] = {"type": "capture", "status": "running", "rows": [], "screens": 0}

    def run():
        try:
            def prog(i, rows):
                _jobs[job].update(rows=rows, screens=i)
            rows = capture_attendees(d, r.cfg, r.max_scrolls, prog)
            _jobs[job].update(status="done", rows=rows)
        except Exception as e:  # noqa: BLE001
            _jobs[job].update(status="error", error=str(e))
    threading.Thread(target=run, daemon=True).start()
    return {"job": job}


class SendReq(BaseModel):
    cfg: dict
    targets: list[dict]  # [{name, title, company, message}]
    dry_run: bool = True


@app.post("/sessions/{sid}/send", dependencies=[Depends(auth)])
def send(sid: str, r: SendReq):
    d = _dev(sid)
    job = secrets.token_hex(4)
    _jobs[job] = {"type": "send", "status": "running", "logs": []}

    def run():
        try:
            pkg = d.current_package()
            for t in r.targets:
                lg = send_message(d, r.cfg, t, t["message"], dry_run=r.dry_run, package=pkg)
                if lg.get("screenshot"):
                    lg["screenshot_b64"] = base64.b64encode(open(lg["screenshot"], "rb").read()).decode()
                    del lg["screenshot"]
                _jobs[job]["logs"].append(lg)
                time.sleep(0.8)
            _jobs[job]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            _jobs[job].update(status="error", error=str(e))
    threading.Thread(target=run, daemon=True).start()
    return {"job": job}


@app.get("/jobs/{job}", dependencies=[Depends(auth)])
def job_status(job: str):
    if job not in _jobs:
        raise HTTPException(404)
    return _jobs[job]


@app.get("/sessions/{sid}/view", response_class=HTMLResponse)
def view(sid: str, token: str = ""):
    """Minimal browser remote-control page (screenshot polling + tap/swipe/type). Embedded in the web app."""
    if token != FARM_TOKEN:
        raise HTTPException(401)
    return f"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{{margin:0;background:#111;color:#eee;font:14px system-ui}} #wrap{{display:flex;flex-direction:column;align-items:center;gap:8px;padding:8px}}
img{{max-height:80vh;border:1px solid #444;border-radius:12px;touch-action:none}} input{{width:70%;padding:8px}} button{{padding:8px 12px}}</style></head>
<body><div id=wrap><img id=s src="/sessions/{sid}/screen.png?t=0">
<div><input id=t placeholder="type text then Enter"><button onclick="key('KEYCODE_BACK')">Back</button><button onclick="key('KEYCODE_HOME')">Home</button></div>
<div style="opacity:.6">tap = tap · drag = swipe · type in box + Enter</div></div>
<script>
const H={{'X-Farm-Token':'{FARM_TOKEN}','Content-Type':'application/json'}};const img=document.getElementById('s');
let nat=[1080,2400];let down=null;
async function refresh(){{const r=await fetch('/sessions/{sid}/screen.png?t='+Date.now(),{{headers:H}});const b=await r.blob();const u=URL.createObjectURL(b);const i=new Image();i.onload=()=>{{nat=[i.naturalWidth,i.naturalHeight];img.src=u}};i.src=u;}}
setInterval(refresh,1200);refresh();
function pos(e){{const r=img.getBoundingClientRect();const p=e.touches?e.touches[0]:e;return [Math.round((p.clientX-r.left)/r.width*nat[0]),Math.round((p.clientY-r.top)/r.height*nat[1])];}}
img.addEventListener('pointerdown',e=>{{down=pos(e);e.preventDefault();}});
img.addEventListener('pointerup',async e=>{{if(!down)return;const up=pos(e);const d=Math.hypot(up[0]-down[0],up[1]-down[1]);
 if(d<15){{await fetch('/sessions/{sid}/tap',{{method:'POST',headers:H,body:JSON.stringify({{x:up[0],y:up[1]}})}});}}
 else{{await fetch('/sessions/{sid}/swipe',{{method:'POST',headers:H,body:JSON.stringify({{x1:down[0],y1:down[1],x2:up[0],y2:up[1],ms:300}})}});}}
 down=null;setTimeout(refresh,500);}});
document.getElementById('t').addEventListener('keydown',async e=>{{if(e.key==='Enter'){{await fetch('/sessions/{sid}/type',{{method:'POST',headers:H,body:JSON.stringify({{text:e.target.value}})}});e.target.value='';setTimeout(refresh,500);}}}});
async function key(k){{await fetch('/sessions/{sid}/key/'+k,{{method:'POST',headers:H}});setTimeout(refresh,500);}}
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    print(f"FARM_TOKEN={FARM_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("FARM_PORT", "8765")))
