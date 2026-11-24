# -*- coding: utf-8 -*-
"""从运行中的 WebView2 读 cookie（走 CDP），不碰磁盘文件。

为什么需要它
------------
pywebview 的 window.get_cookies() 有两个坑，本机实测都会踩：
  1. 它按「窗口当前 URL」过滤（CoreWebView2.CookieManager.GetCookiesAsync(self.url)），
     而登录页会自己跳转，python 侧看到的 URL 不一定同步；
  2. 它把 Set-Cookie 的过期时间当字符串解析，解析失败会**静默跳过整条 cookie**。
结果就是：设备验证登录明明走完了（页面落到 i.mi.com/silentLogin），
get_cookies() 仍返回 0 条，界面只能一直白等。

WebView2 是 Chromium 内核，启动参数里加 --remote-debugging-port 之后，
可以用 CDP 的 Network.getAllCookies 拿到**整个 profile** 的 cookie，
包括 HttpOnly 的 passToken，也不受页面 URL 限制。

前提（由 app.py 的 main() 负责设置）：
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=... --remote-debugging-port=<port>

安全边界：CDP 只绑 127.0.0.1，读到的 cookie 只在内存里用，落盘仍走 DPAPI。
"""
import asyncio
import json
import time
import urllib.request

DEFAULT_PORT = 9333

# 只收小米域的 cookie，别把别的站点的东西也捞进来
HOST_SUFFIXES = ("mi.com", "xiaomi.com")


def _http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def list_targets(port=DEFAULT_PORT, timeout=5):
    """列出 CDP targets（/json）。"""
    return _http_json("http://127.0.0.1:%d/json" % port, timeout)


def _cookies_from_ws(ws_url, timeout=20):
    """连到某个 page target，调 Network.getAllCookies。"""
    import websockets          # 延迟 import：只有真要用 CDP 时才需要它

    async def _run():
        async with websockets.connect(ws_url, max_size=16 * 1024 * 1024,
                                      open_timeout=timeout) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                left = max(0.5, deadline - time.time())
                raw = await asyncio.wait_for(ws.recv(), timeout=left)
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return (msg.get("result") or {}).get("cookies") or []
            raise TimeoutError("CDP 读 cookie 超时")

    return asyncio.run(_run())


def get_cookies(port=DEFAULT_PORT, retries=10, wait=1.0):
    """返回 [{name,value,domain,path,expires,secure,http_only}]，只保留小米域。

    读不到返回 None（区别于「读到了但为空」）。
    会重试：窗口刚跳转完成时 CDP target 可能还没就绪。
    """
    last = None
    for _ in range(max(1, retries)):
        try:
            targets = list_targets(port)
            pages = [t for t in targets
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if not pages:
                raise RuntimeError("没有可用的 page target")
            # 优先挑停在小米域的窗口（登录窗口那个）
            pages.sort(key=lambda t: 0 if any(s in (t.get("url") or "")
                                              for s in HOST_SUFFIXES) else 1)
            raw = _cookies_from_ws(pages[0]["webSocketDebuggerUrl"])
            out = []
            for c in raw:
                name = c.get("name")
                value = c.get("value")
                if not name or not value:
                    continue
                dom = (c.get("domain") or "").lstrip(".").lower()
                if not any(dom == s or dom.endswith("." + s) for s in HOST_SUFFIXES):
                    continue
                exp = c.get("expires")
                exp = int(exp) if isinstance(exp, (int, float)) and exp > 0 else None
                out.append({
                    "name": name,
                    "value": value,
                    "domain": dom,
                    "path": c.get("path") or "/",
                    "expires": exp,
                    "secure": bool(c.get("secure")),
                    "http_only": bool(c.get("httpOnly")),
                })
            print("[CDP] 读到 %d 条小米 cookie" % len(out))
            return out
        except Exception as e:
            last = e
            time.sleep(wait)
    print("[CDP] 读 cookie 失败：%s %s" % (type(last).__name__, last))
    return None


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    recs = get_cookies(port, retries=3)
    if recs is None:
        print("FAIL")
    else:
        for r in sorted(recs, key=lambda x: (x["name"], x["domain"])):
            print("  %-32s %-24s len=%d" % (r["name"], r["domain"], len(r["value"])))
