#!/usr/bin/env python3
"""
bot.py - a bot that really plays $MEAT JUMP (https://meatjump.netlify.app).

How it works
------------
1. Talks raw Chrome DevTools Protocol (websocket) to a Chrome started with
   --remote-debugging-port (default http://127.0.0.1:9222).
2. Opens its own tab, installs a READ-ONLY Phaser hook via
   Page.addScriptToEvaluateOnNewDocument: it wraps Phaser.Scenes.Systems.prototype.step
   so the live Scene instance can be read from the page (the game bundle keeps no
   global reference). The wrapper calls the original method unchanged - the game
   logic, the physics and the score are untouched. Nothing in the game is patched,
   localStorage is never written by this script.
3. The page-side planner (JS below) re-implements the game's own motion model
   (gravity 1600, max horizontal speed 420, accel 1400, drag 800, bounce -900 /
   spring -1400, landing tolerance |dx| < 55+10) and picks the highest platform
   the player can still reach, then returns u in {-1, 0, 1}.
4. Catch rule used by the planner (verified in the bundle): the game catches the
   player when its centre is within 4 px above the platform top (rise needed from a
   launch platform is dSY-43), so the max climbable step is ~288 px - above the
   generator's 283 px maximum. Disappear platforms are never collidable. Spikes are
   only hazards. Breakables vanish after the first touch.
5. Python turns u into REAL browser key events (Input.dispatchKeyEvent,
   ArrowLeft/ArrowRight) - the only input the game ever sees.
5. On death the final score is read out of the game's own ScoreSystem /
   ResultsScene, localStorage['meat_jump_save'] is read back, screenshots are
   saved. The bot plays until it dies.

Run:  python3 bot.py [--port 9222] [--url https://meatjump.netlify.app/] [--max-seconds 600]
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import urllib.request

import websockets

# ----------------------------------------------------------------------------
# page-side JS: read-only scene hook + planner
# ----------------------------------------------------------------------------
_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "planner_hook.js"), "r", encoding="utf-8") as _f:
    HOOK_JS = _f.read()


# ----------------------------------------------------------------------------
# minimal CDP client over a raw websocket (flat session)
# ----------------------------------------------------------------------------
class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, ws, session_id=None):
        self.ws = ws
        self.session_id = session_id
        self._id = 0
        self.events = []
        self.log = lambda *a: None

    async def send(self, method, session=True, **params):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params}
        if session and self.session_id:
            msg["sessionId"] = self.session_id
        await self.ws.send(json.dumps(msg))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=30)
            m = json.loads(raw)
            if m.get("id") == self._id:
                if "error" in m:
                    raise CDPError(f"{method}: {m['error']}")
                return m.get("result", {})
            if "method" in m:
                self.events.append(m)
                if len(self.events) > 200:
                    del self.events[:100]

    async def js(self, expr, by_value=True):
        r = await self.send("Runtime.evaluate", expression=expr, returnByValue=by_value,
                            awaitPromise=True, timeout=10000)
        if r.get("exceptionDetails"):
            raise CDPError(str(r["exceptionDetails"])[:300])
        res = r.get("result", {})
        return res.get("value")

    async def key(self, code, name, down):
        await self.send("Input.dispatchKeyEvent",
                        type=("rawKeyDown" if down else "keyUp"),
                        windowsVirtualKeyCode=code, nativeVirtualKeyCode=code,
                        key=name, code=name, autoRepeat=False)

    async def click(self, x, y):
        for t, extra in (("mouseMoved", {}), ("mousePressed", {"button": "left", "clickCount": 1}),
                         ("mouseReleased", {"button": "left", "clickCount": 1})):
            await self.send("Input.dispatchMouseEvent", type=t, x=x, y=y, **extra)
            await asyncio.sleep(0.05)


def discover_ws(port):
    """GET /json/version on the loopback port, bypassing env proxies."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=8) as r:
        return json.loads(r.read().decode())["webSocketDebuggerUrl"]


# ----------------------------------------------------------------------------
# run
# ----------------------------------------------------------------------------
async def run(args):
    ws_url = discover_ws(args.port)
    out = {"_peak": 0, "_peak_t": 0, "cdp": ws_url, "url": args.url, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    keys_down = set()
    telemetry = []
    shots = []

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024, proxy=None) as ws:
        br = CDP(ws, None)                      # browser-level connection
        tgt = await br.send("Target.createTarget", session=False, url="about:blank")
        target_id = tgt["targetId"]
        att = await br.send("Target.attachToTarget", session=False, targetId=target_id, flatten=True)
        sid = att["sessionId"]
        pg = CDP(ws, sid)
        br.log = pg.log = print
        out["target_id"] = target_id

        await pg.send("Page.enable")
        await pg.send("Runtime.enable")
        # install the hook BEFORE any game code runs
        await pg.send("Page.addScriptToEvaluateOnNewDocument", source=HOOK_JS)
        await pg.send("Target.activateTarget", session=False, targetId=target_id)
        await pg.send("Page.bringToFront")
        await pg.send("Page.navigate", url=args.url)

        # wait for phaser + hook
        t0 = time.time()
        ok = False
        while time.time() - t0 < 40:
            await asyncio.sleep(0.5)
            try:
                v = await pg.js("(function(){try{return (window.Phaser?'P':'')+(window.__M?'M':'')}catch(e){return 'E'}})()")
            except CDPError:
                v = None
            if v == "PM":
                ok = True
                break
        out["hook_installed"] = ok
        if not ok:
            out["error"] = "game page did not expose Phaser/hook in 40s"
            print(json.dumps(out))
            return out

        # wait for the menu (HomeScene) then click PLAY like a human would
        t0 = time.time()
        menu = False
        while time.time() - t0 < 30:
            st = await pg.js("window.__stats()")
            if isinstance(st, dict) and "HomeScene" in (st.get("scenes") or []):
                menu = True
                break
            await asyncio.sleep(0.5)
        out["menu_seen"] = menu

        rect = await pg.js("(function(){const c=document.querySelector('canvas');const r=c.getBoundingClientRect();"
                           "return {l:r.left,t:r.top,w:r.width,h:r.height}})()")
        out["canvas_rect"] = rect
        gx, gy = 240.0, 854 * 0.762            # PLAY button centre in game coords
        cx = rect["l"] + gx / 480 * rect["w"]
        cy = rect["t"] + gy / 854 * rect["h"]
        for attempt in range(4):
            await pg.click(cx, cy)
            await asyncio.sleep(1.0)
            st = await pg.js("window.__stats()")
            if isinstance(st, dict) and (st.get("started") or "GameScene" in (st.get("scenes") or [])):
                break
        st = await pg.js("window.__stats()")
        out["playscene"] = bool(isinstance(st, dict) and "GameScene" in (st.get("scenes") or []))

        # ---- control loop -------------------------------------------------
        KEY = {1: (39, "ArrowRight"), -1: (37, "ArrowLeft")}

        async def set_dir(new):
            nonlocal keys_down
            if new == getattr(set_dir, "u", 0):
                return
            old = getattr(set_dir, "u", 0)
            for who in (old, new):
                if who in KEY and who != 0:
                    kc, name = KEY[who]
                    down = (who == new)
                    if down and kc in keys_down:
                        continue
                    if not down and kc not in keys_down:
                        continue
                    await pg.key(kc, name, down)
                    (keys_down.add if down else keys_down.discard)(kc)
            set_dir.u = new

        t0 = time.time()
        last_tel = 0.0
        last_focus_check = 0.0
        death_at = None
        u = 0
        cycles = 0
        results = None
        while True:
            el = time.time() - t0
            if el > args.max_seconds:
                out["stopped_by"] = "max_seconds"
                break
            try:
                plan = json.loads(await pg.js("JSON.stringify(window.__plan())"))
            except (CDPError, json.JSONDecodeError) as e:
                out["error"] = f"plan failed: {e}"
                break
            cycles += 1
            await asyncio.sleep(0.004)          # ~250 Hz decision rate
            nu = int(plan.get("u", 0)) if plan.get("ok") else 0
            if nu != u:
                await set_dir(nu)
                u = nu
            if plan.get("ok"):
                u = nu

            # un-pause if the tab lost visibility and Phaser auto-paused the game
            if el - last_focus_check > 1.0:
                last_focus_check = el
                st = await pg.js("window.__stats()")
                if isinstance(st, dict):
                    if "PauseScene" in (st.get("scenes") or []):
                        await pg.send("Page.bringToFront")
                        await pg.key(27, "Escape", True)
                        await pg.key(27, "Escape", False)
                        out["unpaused"] = out.get("unpaused", 0) + 1
                    if el - last_tel >= 1.0:
                        last_tel = el
                        rec = {"t": round(el, 2), "score": st.get("score"), "h_m": st.get("maxHeight"),
                               "dead": st.get("dead"), "u": st.get("u"), "cyc": cycles}
                        telemetry.append(rec)
                        print(f"[{el:6.1f}s] score={rec['score']} maxHeight={rec['h_m']}m u={rec['u']}", flush=True)
                    sc_now = st.get("score") or 0
                    if sc_now > out.get("_peak", 0):
                        out["_peak"] = sc_now; out["_peak_t"] = el
                    elif el - out.get("_peak_t", 0) > 18 and el > 25:
                        out["stopped_by"] = "soft_lock (score frozen %ds)" % int(el - out["_peak_t"])
                        out["soft_lock_score"] = sc_now
                        await shoot(pg, args, shots, "softlock")
                        break
                    if st.get("dead"):
                        # read the score straight off the game objects at the moment of death
                        if death_at is None:
                            death_at = el
                            out["death_state"] = st
                            await shoot(pg, args, shots, "death")
                    if st.get("results"):
                        results = st["results"]
                        break
            if death_at is not None and el - death_at > 3.5 and results is None:
                await asyncio.sleep(0.5)
                st = await pg.js("window.__stats()")
                if isinstance(st, dict) and st.get("results"):
                    results = st["results"]
                    break

        # ---- wind down ----------------------------------------------------
        await set_dir(0)
        await asyncio.sleep(1.2)
        st = await pg.js("window.__stats()")
        out["final_stats"] = st
        out["results_scene"] = results
        out["localstorage_after"] = await pg.js("window.__localSave()")
        out["score"] = (results or {}).get("score", (st or {}).get("maxScore"))
        out["height_m"] = (results or {}).get("height", (st or {}).get("maxHeight"))
        out["meat"] = (results or {}).get("meat", (st or {}).get("meat"))
        out["combo"] = (results or {}).get("combo", (st or {}).get("maxCombo"))
        out["target_met"] = bool((out.get("score") or 0) > 43399)
        out["seconds"] = round(time.time() - t0, 1)
        out["cycles"] = cycles
        out["telemetry_samples"] = len(telemetry)
        await shoot(pg, args, shots, "final")
        # canvas-only crop: cleanest view of the in-game score
        await shoot(pg, args, shots, "final_canvas", clip=True)
        out["screenshots"] = shots
        out["telemetry_last"] = telemetry[-5:]
        out["keep_tab_open"] = True
        try:
            pm = await pg.js("JSON.stringify(window.__postmortem())")
            pm_path = os.path.join(args.outdir, f"postmortem_{int(time.time())}.json")
            with open(pm_path, "w") as f:
                f.write(pm if isinstance(pm, str) else json.dumps(pm))
            out["postmortem_file"] = pm_path
        except Exception as e:
            out["postmortem_error"] = str(e)

    with open(os.path.join(args.outdir, "run_telemetry.jsonl"), "a") as f:
        for rec in telemetry:
            f.write(json.dumps(rec) + "\n")
    with open(os.path.join(args.outdir, "run_result.json"), "w") as f:
        json.dump(out, f, indent=2)
    print("\n=== RESULT ===")
    print(json.dumps(out, indent=2)[:4000])
    return out


async def shoot(pg, args, shots, tag, clip=False):
    params = {"format": "png"}
    if clip:
        rect = await pg.js("(function(){const c=document.querySelector('canvas');const r=c.getBoundingClientRect();"
                           "return {x:r.left,y:r.top,width:r.width,height:r.height,scale:1}})()")
        params["clip"] = rect
    r = await pg.send("Page.captureScreenshot", **params)
    path = os.path.join(args.outdir, f"meatjump_{tag}_{int(time.time())}.png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(r["data"]))
    shots.append(path)
    print(f"screenshot -> {path}", flush=True)
    return path


def main():
    # websockets>=14 proxies by default when proxy env vars exist; CDP is on
    # loopback and must never go through the system proxy.
    for k in list(os.environ):
        if "proxy" in k.lower():
            os.environ.pop(k, None)
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--url", default="https://meatjump.netlify.app/")
    ap.add_argument("--max-seconds", type=float, default=600.0)
    ap.add_argument("--outdir", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
