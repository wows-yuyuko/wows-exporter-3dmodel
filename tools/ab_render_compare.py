"""A/B 渲染像素对照（headless Chrome + CDP）。

用途：导出参数改动前后各渲染一次，按「像素级差」判定有没有伤到画面 ——
比人工截图靠谱，也是 [docs/todo-backlog.md](../docs/todo-backlog.md) P1-5 要的硬标准。

典型三组：

    # crack 剔除前/后（--damage）
    python -m tools.ab_render_compare --a "<url?root=.../before/>" --b "<.../after/>"

    # decal 剔除前/后（--decals）
    python -m tools.ab_render_compare ...

    # --lod 0 / --lod 2
    python -m tools.ab_render_compare ...

判定口径（参考 d3d11 文档 §7.5）：两条同时满足才算一致
  - 通道 MAE ≤ 2/255
  - 「显著差异像素」占比 ≤ 0.5%（|Δ|>24 的像素；用来抓小面积但幅度大的变化，
    比如贴花消失 —— 这种情况 MAE 很低、却正是要发现的）
  超过即 FAIL，去看 diff.png；对「预期会变」的对照（如 --decals 前后）应关掉
  阈值只看报告与 diff 图

依赖：`websocket-client`（`pip install websocket-client`）+ 本机 Chrome。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def _find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("未找到 Chrome/Edge，可改 CHROME_CANDIDATES")


class CDP:
    """最小 CDP 客户端：只需要导航 / 执行 / 截图 / 网络空闲判断。"""

    def __init__(self, ws_url: str):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.id = 0
        self.pending = 0

    def send(self, method: str, **params):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Network.requestWillBeSent":
                self.pending += 1
            elif msg.get("method") in ("Network.loadingFinished", "Network.loadingFailed"):
                self.pending = max(0, self.pending - 1)
            if msg.get("id") == self.id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    def ev(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")

    def drain(self):
        self.ws.settimeout(0.2)
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("method") == "Network.requestWillBeSent":
                    self.pending += 1
                elif msg.get("method") in ("Network.loadingFinished", "Network.loadingFailed"):
                    self.pending = max(0, self.pending - 1)
        except Exception:
            pass
        finally:
            self.ws.settimeout(60)

    def wait_ready(self, timeout: float = 180.0):
        """等页面把模型挂上（`#info` 出现 mapped）且网络静默 2 秒。"""
        deadline = time.time() + timeout
        info, quiet = "", None
        while time.time() < deadline:
            time.sleep(0.5)
            self.drain()
            try:
                info = self.ev("(document.getElementById('info')||{}).textContent||''") or ""
                err = self.ev("(document.getElementById('err')||{}).textContent||''") or ""
            except Exception:
                continue
            if err:
                raise RuntimeError("page error: " + err[:400])
            if "mapped" in info and self.pending == 0:
                quiet = quiet or time.time()
                if time.time() - quiet > 2.0:
                    return info
            else:
                quiet = None
        raise RuntimeError("等待页面就绪超时：" + info)

    def shot(self, path: str):
        self.ev("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        data = self.send("Page.captureScreenshot", format="png")["data"]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))


def _launch(url: str, port: int, size: str, profile: str) -> subprocess.Popen:
    return subprocess.Popen([
        _find_chrome(), "--headless=new", "--disable-gpu",
        "--enable-unsafe-swiftshader", "--use-angle=swiftshader",
        "--no-first-run", "--hide-scrollbars", "--disable-extensions",
        f"--remote-debugging-port={port}", "--remote-allow-origins=*",
        f"--user-data-dir={profile}", f"--window-size={size}", url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _connect(port: int, timeout: float = 60.0) -> CDP:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
            page = next((t for t in pages if t.get("type") == "page"), None)
            if page:
                return CDP(page["webSocketDebuggerUrl"])
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("连不上 CDP")


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B 渲染像素对照")
    ap.add_argument("--a", required=True, help="基准页 URL")
    ap.add_argument("--b", required=True, help="对照页 URL")
    ap.add_argument("--out", default=".workbuddy/tmp/ab", help="输出目录")
    ap.add_argument("--size", default="1280,800")
    ap.add_argument("--port", type=int, default=9400)
    ap.add_argument("--mae", type=float, default=2.0, help="MAE 阈值（0-255）")
    ap.add_argument("--frac", type=float, default=0.005,
                    help="显著差异像素（|Δ|>24）占比上限，默认 0.5%%")
    ap.add_argument("--expect-change", action="store_true",
                    help="预期会变的对照：只出报告与 diff，不判 PASS/FAIL")
    args = ap.parse_args()

    try:
        from PIL import Image, ImageChops
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"需要 Pillow/numpy：{exc}")

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    profile = os.path.join(out, "profile")

    proc = _launch(args.a, args.port, args.size, profile)
    try:
        c = _connect(args.port)
        c.send("Page.enable")
        c.send("Runtime.enable")
        c.send("Network.enable")
        info_a = c.wait_ready()
        pa = os.path.join(out, "a.png")
        c.shot(pa)

        c.send("Page.navigate", url=args.b)
        info_b = c.wait_ready()
        pb = os.path.join(out, "b.png")
        c.shot(pb)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    im_a = Image.open(pa).convert("RGB")
    im_b = Image.open(pb).convert("RGB")
    if im_a.size != im_b.size:
        im_b = im_b.resize(im_a.size)
    a = np.asarray(im_a, np.float32)
    b = np.asarray(im_b, np.float32)
    d = np.abs(a - b)
    mae = float(d.mean())
    per_px = d.max(axis=2)
    p99 = float(np.percentile(per_px, 99))
    mx = float(d.max())
    frac_big = float((per_px > 24).mean())

    diff = ImageChops.difference(im_a, im_b)
    diff_path = os.path.join(out, "diff.png")
    diff.save(diff_path)

    print("A: %s\n   %s" % (args.a, info_a))
    print("B: %s\n   %s" % (args.b, info_b))
    print("MAE=%.3f/255  p99=%.1f  max=%.0f  显著差异像素=%.3f%%"
          % (mae, p99, mx, frac_big * 100.0))
    print("阈值：MAE≤%.1f 且 显著差异≤%.2f%%" % (args.mae, args.frac * 100.0))
    print("产物：%s / b.png / diff.png" % pa)
    if args.expect_change:
        print("判定：SKIP（--expect-change，人工看 diff.png）")
        return 0
    ok = mae <= args.mae and frac_big <= args.frac
    print("判定：%s" % ("PASS（画面一致）" if ok else "FAIL（超出阈值，看 diff.png）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
