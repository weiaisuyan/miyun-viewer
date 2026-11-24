# -*- coding: utf-8 -*-
"""
小米云 · 只读查看器（pywebview 桌面窗口）

四个页签：相册 / 便签 / 录音 / 云盘
  全部只读浏览，不自动下载任何东西；
  只有用户勾选并点「下载选中」时才写盘。

启动：python app.py
"""
import base64
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import hashlib
import io
import mimetypes
import shutil
import requests
import webview
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from paths import data_dir, res_dir                       # noqa: E402
from probe import (API, SEP, dpapi_protect, dpapi_unprotect, get_cookie,  # noqa: E402
                   keep_only_token, load_all_cookies, load_all_cookies_raw,
                   load_session, make_session, note_pass_token, note_service_token,
                   pick_working_token, save_session, service_tokens, verify_session)
import client_auth                                        # noqa: E402
import keybag                                             # noqa: E402
import cache_gc                                           # noqa: E402
import wv_cookies                                         # noqa: E402

BASE = data_dir()                    # 打包后 = exe 所在目录（可写）
# 界面一般打进 exe（res_dir() = 解包临时目录）。改 HTML 时把 MIYUN_UI_DIR 指到源码的
# ui/ 目录，服务端每次请求都从磁盘读，刷新页面即生效，不用为了改一行字重新打包。
_DEV_UI = (os.environ.get("MIYUN_UI_DIR") or "").strip()
UI_DIR = _DEV_UI if (_DEV_UI and os.path.isdir(_DEV_UI)) else os.path.join(res_dir(), "ui")
# ── 设置：缓存目录 / 上限 / 自动 ────────────────────────────────────
# 存在 exe 旁边的 settings.json；改缓存目录要重启才生效（CACHE_DIR 在启动时定死）。
SETTINGS_PATH = os.path.join(BASE, "settings.json")
DEFAULT_CACHE_DIR = os.path.join(BASE, "cache")
DEFAULT_CACHE_LIMIT = 3 * 1024 * 1024 * 1024


def _load_settings():
    out = {"cache_dir": "", "cache_limit": DEFAULT_CACHE_LIMIT, "cache_auto": False}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            j = json.load(f) or {}
        if isinstance(j, dict):
            for k in list(out):
                if k in j:
                    out[k] = j[k]
    except Exception:
        pass
    return out


def _save_settings(s):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SETTINGS_PATH)


SETTINGS = _load_settings()


def _valid_cache_dir(d):
    d = (d or "").strip()
    if not d:
        return DEFAULT_CACHE_DIR
    try:
        d = os.path.abspath(os.path.expanduser(d))
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".miyun_write_test")
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
        return d
    except Exception as e:
        print("[设置] 缓存目录不可用（%s），回退默认：%s" % (d, e))
        return DEFAULT_CACHE_DIR


def pick_dir():
    """弹一个 Windows 文件夹选择框，返回用户选的路径。

    不走 pywebview 的对话框：那货要在 UI 线程里调，HTTP 线程去调容易把窗口卡住。
    这里起一个独立的 PowerShell（STA）弹 FolderBrowserDialog，选完把路径带回来。
    """
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = '选择缓存目录';"
        "$d.ShowNewFolderButton = $true;"
        "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){"
        "  [Console]::Out.Write($d.SelectedPath) }"
    )
    try:
        pr = subprocess.run(["powershell", "-STA", "-NoProfile", "-Command", ps],
                            capture_output=True, timeout=300, **_hidden_run_kw())
        out = (pr.stdout or b"").decode("utf-8", "replace").strip()
        if not out:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _check_dir(d):
    """校验一个目录能不能写；能写返回绝对路径，不能返回 None。"""
    try:
        d = os.path.abspath(os.path.expanduser(d))
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".miyun_write_test")
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
        return d
    except Exception:
        return None


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def migrate_cache(old, new):
    """把现有缓存复制到新目录（appkeys 等也一起），返回 (文件数, 字节数)。

    用复制而不是直接移走：复制时应用还在跑，搬走会让当前会话读不到缓存。
    复制完重启生效；确认新目录正常后，旧目录可以手动删。
    """
    if os.path.abspath(old) == os.path.abspath(new) or not os.path.isdir(old):
        return 0, 0
    need = _dir_size(old)
    try:
        free = shutil.disk_usage(new).free
    except Exception:
        free = 0
    if free and need > free - 100 * 1024 * 1024:
        raise RuntimeError("目标盘剩余空间不够（需要 %.1f GB，只剩 %.1f GB）"
                           % (need / 1073741824.0, free / 1073741824.0))
    n = b = 0
    for root, _dirs, files in os.walk(old):
        rel = os.path.relpath(root, old)
        dst_root = new if rel == "." else os.path.join(new, rel)
        os.makedirs(dst_root, exist_ok=True)
        for fn in files:
            s = os.path.join(root, fn)
            t = os.path.join(dst_root, fn)
            if os.path.exists(t):
                continue
            try:
                shutil.copy2(s, t)
                n += 1
                b += os.path.getsize(t)
            except Exception as e:
                print("[设置] 迁移失败 %s：%s" % (s, e))
    return n, b


CACHE_DIR = _valid_cache_dir(SETTINGS.get("cache_dir"))
HOME_DL = os.path.join(os.path.expanduser("~"), "Downloads")
os.makedirs(CACHE_DIR, exist_ok=True)


def cache_limit_bytes():
    """上限：自动 = 缓存盘剩余空间的一半；手动就用设置里的值。"""
    if SETTINGS.get("cache_auto"):
        try:
            free = shutil.disk_usage(CACHE_DIR).free
            return max(512 * 1024 * 1024, int(free * 0.5))
        except Exception:
            pass
    try:
        return max(256 * 1024 * 1024,
                   int(SETTINGS.get("cache_limit") or DEFAULT_CACHE_LIMIT))
    except Exception:
        return DEFAULT_CACHE_LIMIT


cache_gc.CACHE_LIMIT = cache_limit_bytes()
cache_gc.CACHE_LOW = int(cache_gc.CACHE_LIMIT * 0.8)

# 打包成单文件 exe 后，首次运行把内置的 appKey 复制到 exe 旁边（之后就地更新）
for _n in ("appkeys.json", "appkeys_note.json"):
    _dst = os.path.join(CACHE_DIR, _n)
    _src = os.path.join(res_dir(), "cache", _n)
    if not os.path.exists(_dst) and os.path.exists(_src):
        try:
            with open(_src, "rb") as _f, open(_dst, "wb") as _g:
                _g.write(_f.read())
        except Exception:
            pass

# ── 底线开关：绝不碰官方客户端的登录态 ─────────────────────────────
# 历史教训：直接读取「小米云服务」客户端进程内存里的 serviceToken，
# 每取一次服务端都会轮换票据，官方客户端手里的那张随之作废；
# 反复读取会让官方客户端掉线、需要重新登录。
#
# 现在默认关闭。永久方案是 silent_relogin()：只用我们自己扫码时存下来的
# passToken 去账号服务换新票，全程不读、不写、不启动、不结束客户端。
# 代价：本地没有 passToken 时会直接让你扫码，而不是偷偷去客户端偷票。
#
# 真要临时恢复这条路，改成 True —— 但请先读完上面两段。
ALLOW_CLIENT_BORROW = False

# 官方客户端的 silentRelogin URL（从 app.asar 的 micloudpc.js 原样抄的）。
# callback 里带的是 i.mi.com/sts 的固定 sign + followup，少了它服务端回 70016。
SILENT_LOGIN_URL = (
    "https://account.xiaomi.com/pass/serviceLogin"
    "?callback=https%3A%2F%2Fi.mi.com%2Fsts%3Fsign%3DFFBBANTmX8sZr7uMrJWjMRfR0TU%253D"
    "%26followup%3Dhttps%253A%252F%252Fi.mi.com%252FsilentLogin"
    "&sid=i.mi.com&_bal=true&checkSafePhone=false"
    "&_snsNone=true&_showQr=false&_locale=zh_CN"
)

LOCK = threading.RLock()
S = None
TOKEN = None
AUTH = {"ok": False}          # 当前是否已有可用登录态

# 取票单例闸：同一时刻只允许一条登录链路在跑。
# 没有它的话，前端每个 401 都会打一次 /api/relogin，各自起线程去拉起一个客户端，
# 结果就是「窗口一个接一个往外弹」。
LOGIN_GATE = threading.Lock()
LOGIN_RUNNING = False

# 登录过程对外展示的状态（界面右上角的进度就是读它）
LOGIN = {"state": "idle", "step": "", "detail": "", "since": time.time(),
         "err": None, "pct": 0}


def set_login(state=None, step=None, detail=None, pct=None, err=None):
    if state is not None:
        LOGIN["state"] = state
        LOGIN["since"] = time.time()
    if step is not None:
        LOGIN["step"] = step
        LOGIN["since"] = time.time()
    if detail is not None:
        LOGIN["detail"] = detail
    if pct is not None:
        LOGIN["pct"] = int(pct)
    if err is not None:
        LOGIN["err"] = err


class NeedLogin(Exception):
    pass


class NeedStrongAuth(Exception):
    """会话本身是好的，但这个服务要求更强的身份验证（比如待办）。

    小米把一部分服务判为「敏感」：静默续期换来的票打它们会 401，
    服务端给的跳转目标是 identity/authStart（身份验证）。
    这种情况不该把整个界面拖去扫码 —— 相册/笔记/录音都还能用。
    """
    pass


# 需要「强身份」的服务前缀。打到这些路径时，401 不当作整体登录失效。
STRONG_AUTH_PREFIX = ("/todo/",)


# ---------------------------------------------------------------- 登录态复用
# ---------------------------------------------------------------- 设备登录
# 扫码是「网页快捷登录」，服务端不把它当设备：不进设备管理、istrudev=false、
# 待办这类敏感服务还要额外验证。
# 官方客户端做的是「设备验证登录」：输密码 → 手机收到验证码 → 填回去。
# 这里用内嵌窗口加载小米官方的登录页，让用户走同一条路。
# 密码只在小米自己的页面里输入，本程序既不接触也不保存。
DEVICE = {"running": False, "win": None, "closed": False}
DEVICE_LOCK = threading.Lock()

DEVICE_LOGIN_URL = SILENT_LOGIN_URL.replace("checkSafePhone=false",
                                            "checkSafePhone=true")

# 设备登录窗口的 cookie 靠 CDP 从 WebView2 里读出来
# （pywebview 的 get_cookies 在本机返回 0 条，见 wv_cookies.py 顶部说明）。
# 端口要在 WebView2 创建之前通过环境变量传给它，见 main()。
# 0 = 这次启动不开放调试端口（正常已登录时的默认值）。
WV_CDP_PORT = 0


def saved_login_ok():
    """只读检查磁盘上的登录票：有没过期的 passToken / serviceToken 就算已登录。

    只看 session.dat 的只读副本，不写盘、不刷新、不清理，完全不动登录态。
    用来决定启动时要不要开放调试端口：已登录 = 不会出现设备登录窗口 = 不需要。
    """
    try:
        recs = load_all_cookies_raw()
    except Exception:
        return False
    now = time.time()
    for r in recs or []:
        if r.get("name") not in ("passToken", "serviceToken") or not r.get("value"):
            continue
        exp = r.get("expires")
        if exp is None or float(exp) > now + 3600:
            return True
    return False


def _free_local_port():
    """拿一个当前空闲的本地端口（登录页需要调试口时用）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def cdp_port_for_startup():
    """决定这次启动要不要开调试端口，返回端口号（0 = 不开）。

    默认只读判断，不碰登录文件、不刷新票：
      * 环境里已经指定 remote-debugging-port → 用它（显式调试）；
      * MIYUN_CDP_PORT 指定 → 用它（显式调试）；
      * 本地有可用登录票 → 0（不开放，设备登录窗口不会出现）；
      * 没有可用票、马上要显示登录页 → 随机空闲端口（设备登录读 cookie 要用）。
    """
    args = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    m = re.search(r"--remote-debugging-port=(\d+)", args)
    if m:
        return int(m.group(1))
    forced = (os.environ.get("MIYUN_CDP_PORT") or "").strip()
    if forced:
        try:
            return int(forced)
        except ValueError:
            return 0
    if saved_login_ok():
        return 0
    return _free_local_port()


def device_login_start():
    """开一个内嵌窗口做设备验证登录（密码 + 手机验证码）。"""
    with DEVICE_LOCK:
        if DEVICE["running"]:
            print("[登录] 设备登录窗口已经开着")
            return {"ok": True, "already": True}
        DEVICE.update({"running": True, "closed": False, "win": None})

    def _worker():
        global S, TOKEN
        try:
            set_login("device", "请在弹出的窗口里登录",
                      "输入小米账号密码，再按手机上收到的验证码完成验证", pct=0)
            win = webview.create_window("登录小米账号（小米官方页面）",
                                        DEVICE_LOGIN_URL,
                                        width=520, height=760,
                                        min_size=(420, 620))
            DEVICE["win"] = win

            def _on_closed():
                DEVICE["closed"] = True
            try:
                win.events.closed += _on_closed
            except Exception:
                pass

            jar = {}          # (name, domain) -> value
            cdp_recs = []     # CDP 读到的完整记录（带 expires，优先用这份）

            def collect():
                """把当前页面的 cookie 收进 jar。

                WebView2 的 get_cookies() 只返回「当前 URL」能看到的 cookie：
                停在 account.xiaomi.com 时能拿到 passToken，跳到 i.mi.com 之后
                就只能拿到 serviceToken。所以两个阶段都要采一次。
                """
                try:
                    for c in (win.get_cookies() or []):
                        if c.value:
                            jar[(c.name, (c.domain or "").lstrip("."))] = c.value
                except Exception:
                    pass

            def has(name):
                return any(k[0] == name for k in jar)

            # 阶段 1：等登录完成。判据有两个：
            #   a) 采到 serviceToken（说明已经落到 i.mi.com）；
            #   b) 窗口 URL 停在 i.mi.com —— 本机 get_cookies() 返回 0 条，
            #      只能靠 URL 判断登录真的走完了（原因见 wv_cookies.py 顶部）。
            deadline = time.time() + 240
            tick = 0
            landed = False
            while time.time() < deadline and not DEVICE["closed"]:
                time.sleep(1)
                try:
                    collect()
                except Exception as e:
                    print("[登录] 读窗口 cookie 出错：%s %s" % (type(e).__name__, e))
                tick += 1
                cur = ""
                try:
                    cur = win.get_current_url() or ""
                except Exception:
                    pass
                if tick % 5 == 0:
                    print("[登录] 设备登录窗口 当前=%s  已采集=%d 个"
                          % (cur[:90] or "读取失败", len(jar)))
                if has("serviceToken"):
                    break
                if "i.mi.com" in cur and "account.xiaomi.com" not in cur:
                    landed = True
                    break

            if DEVICE["closed"] and not has("serviceToken") and not has("passToken"):
                set_login("qr", "已取消设备登录", "可以改用扫码登录", pct=0)
                return

            # 页面已落到 i.mi.com 但 pywebview 一条都没采到：
            # 走 CDP 从 WebView2 里读。这是本机的正常路径，不是兜底。
            # 注意：CDP 拿到的通常是 account 域那几件（含 passToken），
            # 可能没有 i.mi.com 的 serviceToken —— 有 passToken 就够了，
            # 下面 silent_relogin() 会用它换票。
            if not has("serviceToken") and landed:
                print("[登录] 页面已落到 i.mi.com，改用 CDP 读取 cookie")
                got = wv_cookies.get_cookies(WV_CDP_PORT)
                if got:
                    cdp_recs = got
                    for c in got:
                        jar[(c["name"], c["domain"])] = c["value"]
                    print("[登录] CDP 采到 %d 条：%s"
                          % (len(got), sorted({c["name"] for c in got})))

            if not has("passToken") and not has("serviceToken"):
                set_login("qr", "设备登录超时", "没有检测到登录完成，可以改用扫码", pct=0)
                return

            # 阶段 2：切回账号域补 passToken（长期票的根）
            if not has("passToken"):
                try:
                    win.load_url("https://account.xiaomi.com/")
                    time.sleep(3)
                    collect()
                    if not has("passToken"):
                        got = wv_cookies.get_cookies(WV_CDP_PORT, retries=4)
                        if got:
                            cdp_recs = got
                            for c in got:
                                jar[(c["name"], c["domain"])] = c["value"]
                except Exception:
                    pass

            print("[登录] 设备登录窗口采集到：%s" % sorted({k[0] for k in jar}))

            s = make_session()
            s.headers["Referer"] = "https://i.mi.com/"
            if cdp_recs:
                # CDP 记录带 expires / secure / httpOnly，比裸 value 完整
                for c in cdp_recs:
                    try:
                        s.cookies.set(c["name"], c["value"], domain=c["domain"],
                                      path=c["path"], expires=c["expires"],
                                      secure=c["secure"])
                    except Exception:
                        pass
            else:
                for (name, dom), val in jar.items():
                    try:
                        s.cookies.set(name, val, domain=dom or "i.mi.com", path="/")
                    except Exception:
                        pass

            ok = False
            for t in service_tokens(s):
                keep_only_token(s, t)
                if verify_session(s):
                    ok = True
                    break
            try:
                save_session(s, merge=True)       # 先把新 passToken 落盘
            except Exception:
                pass

            if not ok:
                # 窗口里只拿到 passToken 也没关系，用它走静默续期换票
                ok = silent_relogin()

            if ok:
                # 关键：把窗口里拿到的会话正式交给程序用。
                # 少了这三行，登录窗口成功了、票也存下来了，但主界面还是
                # 「登录中」——因为 S/TOKEN/AUTH 从来没被更新。
                # 这条路径是离线用假窗口跑测出来的，不是猜的。
                fresh_tok = get_cookie(s, "serviceToken")
                if fresh_tok:
                    keep_only_token(s, fresh_tok)
                    # 设备验证登录拿到的是「受信任设备」票，尤其要登记住
                    note_service_token(fresh_tok)
                    note_pass_token(get_cookie(s, "passToken"))
                    _PASS_KEEP_AT[0] = time.time()
                    _save_keepalive_at(_PASS_KEEP_AT[0])
                    with LOCK:
                        S = s
                        TOKEN = fresh_tok
                        AUTH["ok"] = True
                names = sorted({k[0] for k in jar})
                print("[登录] 设备登录成功，窗口 cookie：%s" % names)
                print("[登录] 拿到 passToken：%s，trust_device_auth_token：%s"
                      % ("passToken" in names, "trust_device_auth_token" in names))
                set_login("ready", "登录就绪", "设备验证登录成功", pct=100)
            else:
                set_login("qr", "设备登录没换到票", "可以改用扫码登录", pct=0)
        except Exception as e:
            print("[登录] 设备登录失败：%s %s" % (type(e).__name__, e))
            set_login("qr", "设备登录失败", str(e), pct=0)
        finally:
            w = DEVICE.get("win")
            if w is not None:
                try:
                    w.destroy()
                except Exception:
                    pass
            with DEVICE_LOCK:
                DEVICE["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True}


_SILENT_LOCK = threading.Lock()
_SILENT_AT = [0.0]           # 上次尝试续期的时刻
_SILENT_OK = [False]         # 上次尝试的结果
_SILENT_FAIL = [0]           # 连续失败次数（成功后归零）
_PASS_KEEP_AT = [0.0]        # 上次续期成功的时刻（保活线程据此判断该不该续）

MIN_RETRY = 10.0             # 两次续期之间的最短间隔（秒）
MAX_BACKOFF = 900.0          # 失败后的退避上限：15 分钟

# 上次成功续期的时刻，落盘保存。
# 为什么要落盘：票的续期窗口只有 30 天，程序关了就不会续。
# 记在磁盘上，下次启动才知道"这次该不该立刻续一次"，
# 官方客户端也是每次启动都续一遍（日志 init login → silentRelogin → login sucess）。
KEEPALIVE_FILE = os.path.join(CACHE_DIR, "last_relogin.json")
KEEPALIVE_MAX_GAP = 7 * 86400      # 超过 7 天没续过，启动就补一次（窗口 30 天，留足余量）


def _load_keepalive_at():
    try:
        with open(KEEPALIVE_FILE, "r", encoding="utf-8") as f:
            return float(json.load(f).get("at") or 0.0)
    except Exception:
        return 0.0


def _save_keepalive_at(t):
    try:
        tmp = KEEPALIVE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": float(t)}, f)
        os.replace(tmp, KEEPALIVE_FILE)
    except Exception:
        pass


def silent_relogin():
    """并发闸 + 退避。

    为什么要退避：每续一次都要拿 passToken 去打登录接口，服务端会把票轮换掉。
    一次会话被几十个 401 连环触发续期，等于自己把票往死里用（实测就是把登录态
    用废的那条路）。失败后更不能立刻重试 —— 那只会让服务端更快把会话判成异常。

    冷却期内返回的是「上一次的结果」而不是无脑 True：上次失败还报成功，
    调用方会拿着旧票再打一次，白吃一个 401。
    """
    with _SILENT_LOCK:
        now = time.time()
        gap = MIN_RETRY if _SILENT_FAIL[0] == 0 else min(
            MIN_RETRY * (2 ** min(_SILENT_FAIL[0], 6)), MAX_BACKOFF)
        waited = now - _SILENT_AT[0]
        if waited < gap:
            print("[登录] 续期冷却中（还要等 %.0f 秒），跳过本次" % (gap - waited))
            return _SILENT_OK[0]
        _SILENT_AT[0] = now
        ok = _silent_relogin_once()
        _SILENT_OK[0] = ok
        _SILENT_FAIL[0] = 0 if ok else _SILENT_FAIL[0] + 1
        return ok


def _silent_relogin_once():
    """用磁盘上的 passToken 静默换一张新的 serviceToken —— 完全不碰客户端。

    这是「长期免登录」的正解，抄的是官方客户端 `compiled/main/micloudpc.js`
    里那条 `silentRelogin` 的 URL（从 app.asar 里读出来的，原样照搬）：

        https://account.xiaomi.com/pass/serviceLogin
            ?callback=https%3A%2F%2Fi.mi.com%2Fsts%3Fsign%3D<固定sign>
                     %26followup%3Dhttps%253A%252F%252Fi.mi.com%252FsilentLogin
            &sid=i.mi.com&_bal=true&checkSafePhone=false
            &_snsNone=true&_showQr=false&_locale=zh_CN

    关键就是那个 `callback`：里面带着 i.mi.com/sts 的 sign 和 followup。
    少了它，服务端直接回 70016 登录验证失败（我踩过这个坑）。
    带上它 + passToken，302 两跳之后就把新的 serviceToken 发出去了：

        302 -> i.mi.com/sts?sign=...&followup=.../silentLogin&d=wb_ff...
        302 -> i.mi.com/silentLogin          ← 这一跳落 serviceToken

    全程只用自己的票，不读、不写、不启动、不结束官方客户端。
    """
    global S, TOKEN
    cookies = load_all_cookies()
    by_name = {}
    for c in cookies:
        if c.get("value"):
            by_name.setdefault(c["name"], c["value"])
    pt = by_name.get("passToken")
    if not pt:
        print("[登录] 本地没有 passToken，静默续期不可用")
        return False

    s = make_session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://i.mi.com/",
    })
    # 按磁盘上的**原域**逐条挂回去：一条 cookie 只发一处。
    # 以前把 passToken 同时挂到 account.xiaomi.com 和 .xiaomi.com，
    # 发往账号域的请求会带两份同名票，服务端取哪份全看实现；
    # 而且从来没带 trust_device_auth_token / i.mi.com_istrudev，
    # 服务端就一直按「非受信任设备」签发短票。
    WANT = ("passToken", "cUserId", "userId", "deviceId", "pass_ua",
            "trust_device_auth_token", "i.mi.com_istrudev")
    for c in cookies:
        n, v = c.get("name"), c.get("value")
        if n not in WANT or not v:
            continue
        try:
            s.cookies.set(n, v,
                          domain=(c.get("domain") or "account.xiaomi.com"),
                          path=c.get("path") or "/")
        except Exception:
            pass

    try:
        r = s.get(SILENT_LOGIN_URL, timeout=30, allow_redirects=True)
    except Exception as e:
        print("[登录] 静默续期请求失败：%s %s" % (type(e).__name__, e))
        return False

    tok = get_cookie(s, "serviceToken")
    if not tok:
        print("[登录] 静默续期没换到票（HTTP %s，最终 URL %s）"
              % (r.status_code, r.url[:80]))
        return False

    s.headers["Referer"] = "https://i.mi.com/"
    keep_only_token(s, tok)
    # 续期链路上服务端可能同时轮换 passToken：登记下来，
    # 之后任何旧 session 落盘都覆盖不了它。
    new_pt = get_cookie(s, "passToken")
    if new_pt:
        note_pass_token(new_pt)
    note_service_token(tok)
    _PASS_KEEP_AT[0] = time.time()
    _save_keepalive_at(_PASS_KEEP_AT[0])
    with LOCK:
        S = s
        TOKEN = tok
        AUTH["ok"] = True
    try:
        save_session(s, merge=True)     # 合并 + 去重 + 原子写
    except Exception:
        pass
    print("[登录] 用 passToken 静默续期成功，serviceToken 长度 %d" % len(tok))
    return True


def recover_from_client(force=False):
    """遇到 401 时，从正在运行的小米云服务客户端内存里换一套 cookie。

    客户端持有「受信任设备」会话，服务端每次响应都会 set-cookie 换新票，
    所以只要客户端开着，这里拿到的就永远是新鲜的，不用扫码。

    force=False：优先用 10 分钟内的缓存票，避免每个 401 都去扫进程、
    拿客户端的票打验证请求（那会反过来消耗掉客户端自己的登录态）。
    force=True ：刚被服务端拒了，缓存作废，必须重新扫。
    """
    global S, TOKEN
    # 开关放在函数内部：401 的重试分支会绕过 borrow_login_ticket 直接调这里，
    # 只在 borrow 里判断是拦不住的（这是之前的漏洞）。
    if not ALLOW_CLIENT_BORROW:
        return False
    if force:
        client_auth.invalidate(BASE)
    try:
        kv = client_auth.get_client_cookies(BASE, use_cache=True)
    except Exception:
        kv = None
    if not kv:
        return False
    s = make_session()
    s.headers["Referer"] = "https://i.mi.com/"
    client_auth.apply_to_session(s, kv)
    with LOCK:
        S = s
        TOKEN = kv.get("serviceToken")
        AUTH["ok"] = True
    try:
        save_session(s)          # 顺手存一份，客户端关掉后还能顶一阵
    except Exception:
        pass
    print("[登录] 已从小米云服务客户端换票，serviceToken 长度 %d"
          % len(TOKEN or ""))
    return True


CLIENT_EXE = r"C:\Program Files\MI\Xiaomi Cloud\小米云服务.exe"
BORROW_TIMEOUT = 45           # 拉客户端取票最多等这么久（秒）；超时就直接扫码，不让人干等


def _kill_our_tree(pid):
    """只结束我们自己启动的那一棵进程树，绝不碰用户自己开的实例。"""
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, timeout=40,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        return True
    except Exception:
        return False


def _start_client_hidden():
    """静默启动小米云服务客户端：最小化、不抢焦点。

    Electron 客户端会忽略大部分启动参数自己把窗口显示出来，
    所以启动后再调一次 ShowWindow(SW_HIDE) 兜底。
    返回 pid；失败返回 None。

    调用前必须确认客户端没在跑：两个实例共用同一个 user-data-dir，
    cookie 数据库会互相覆盖，等于把用户自己的登录态搞坏。
    """
    if client_auth.client_pids():
        print("[登录] 客户端已在运行，不再启动第二个实例")
        return None
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7                      # SW_SHOWMINNOACTIVE：最小化且不激活
        p = subprocess.Popen([CLIENT_EXE], close_fds=True,
                             startupinfo=si,
                             creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)))
    except Exception:
        return None

    # 客户端窗口是异步创建的，给它一点时间再藏起来。
    # 64 位下 HWND 必须声明成 c_void_p，否则句柄被截断会藏错窗口。
    def _hide_later(pid):
        try:
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            u32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
            u32.IsWindowVisible.argtypes = [wintypes.HWND]
            u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                     ctypes.POINTER(wintypes.DWORD)]
            CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            for _ in range(16):                 # 最多盯 8 秒
                time.sleep(0.5)
                found = []

                def _cb(hwnd, _lparam):
                    wpid = wintypes.DWORD(0)
                    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                    if wpid.value == pid and u32.IsWindowVisible(hwnd):
                        found.append(hwnd)
                    return True

                u32.EnumWindows(CB(_cb), 0)
                for hwnd in found:
                    u32.ShowWindow(hwnd, 0)     # SW_HIDE
                if found:
                    print("[登录] 已隐藏客户端窗口 %d 个，pid=%d" % (len(found), pid))
                    return
        except Exception:
            pass

    threading.Thread(target=_hide_later, args=(p.pid,), daemon=True).start()
    return p.pid


def borrow_login_ticket():
    """本地票失效时取票。

    ⚠️ 这条路默认是关的，见下面的 ALLOW_CLIENT_BORROW。
    真正在用的是 silent_relogin()：只拿我们自己磁盘上的 passToken 去续期。

    客户端已经在跑  → 直接从它内存里读（不启动、不结束任何进程）。
    客户端没在跑    → 程序自己把它拉起来，读到票后立刻结束**自己拉起的这棵树**。
    """
    if not ALLOW_CLIENT_BORROW:
        print("[登录] 客户端取票通道已关闭（保护官方客户端登录态），改走扫码")
        return False

    set_login("borrowing", "检查本地登录票", "先看本地保存的登录票还能不能用 …", pct=8)
    if recover_from_client():
        set_login("ready", "登录就绪", "复用了小米云服务客户端的受信任会话", pct=100)
        return True

    running = client_auth.client_pids()
    started_pid = None
    if running:
        set_login("borrowing", "正在读取登录票",
                  "小米云服务已经在运行，直接从它的内存里读（不会关掉你开着的它）", pct=20)
    else:
        if not os.path.exists(CLIENT_EXE):
            set_login("qr", "需要扫码登录",
                      "没找到「小米云服务」客户端，改用手机扫码", pct=0)
            start_qr()
            return False
        set_login("borrowing", "正在启动小米云服务",
                  "本地登录票已过期。程序会自己打开「小米云服务」取一张登录票，"
                  "取到后立刻关掉；通常需要 20–60 秒，这期间不用管它。", pct=12)
        try:
            started_pid = _start_client_hidden()
            if started_pid is None:
                raise RuntimeError("Popen failed")
            print("[登录] 已静默启动小米云服务取票，pid=%d" % started_pid)
        except Exception as e:
            set_login("qr", "需要扫码登录",
                      "启动小米云服务失败（%s），改用手机扫码" % type(e).__name__, pct=0)
            start_qr()
            return False

    t0 = time.time()
    # 客户端本来就在跑：票要么有要么没有，多等不会变出来，只等一小会儿就转扫码；
    # 是我们自己刚拉起来的：给客户端启动+登录的时间，等久一点。
    wait_sec = BORROW_TIMEOUT if started_pid else 12
    try:
        while time.time() - t0 < wait_sec:
            elapsed = time.time() - t0
            pct = min(92, 15 + elapsed / wait_sec * 77)
            set_login(None, "正在读取登录票",
                      "已经等了 %.0f 秒，正在从客户端内存里取票 …" % elapsed, pct=pct)
            if recover_from_client():
                set_login("ready", "登录就绪",
                          "已取到登录票" + ("，正在关闭刚才启动的客户端" if started_pid else ""),
                          pct=100)
                return True
            time.sleep(2)
    finally:
        if started_pid:
            _kill_our_tree(started_pid)
            print("[登录] 已关闭为取票启动的小米云服务，pid=%d" % started_pid)

    if not started_pid:
        set_login("qr", "需要扫码登录",
                  "小米云服务客户端里没有可用的登录态，改用手机扫码", pct=0)
    else:
        set_login("qr", "需要扫码登录",
                  "等了 %d 秒还没取到票，改用手机扫码" % wait_sec, pct=0)
    start_qr()
    return False


# ---------------------------------------------------------------- 基础请求
def _my_cookies_map():
    """只从「我们自己的会话」里取 cookie，绝不碰官方客户端。

    以前这里直接调 client_auth.get_client_cookies()，缓存没过期还好，
    一旦过期就会去扫客户端进程内存 —— 那条路会消耗掉客户端的登录态。
    现在改成：当前内存会话 → 磁盘 session.dat，都没有就算了。
    """
    out = {}
    with LOCK:
        s = S
    if s is not None:
        try:
            for c in s.cookies:
                out.setdefault(c.name, c.value)
        except Exception:
            pass
    if "userId" not in out:
        for c in load_all_cookies():
            out.setdefault(c["name"], c["value"])
    return out


# 批量扫描模式：时间轴要探测几百次，每次都落盘（DPAPI 加密 + fsync）会把
# 178 秒耗在磁盘 I/O 上（实测：独立脚本 16 秒 vs 走 api_get 178 秒）。
# 置 True 时 _sync_token 只更新内存里的 TOKEN，最后统一存一次。
BULK_SCAN = [False]


def _sync_token(s):
    """把服务端刚轮换出来的票收下、写回磁盘。

    为什么必须有：小米每次响应都可能 set-cookie 换一张新 serviceToken，旧票随即作废。
    以前请求完就不管了，磁盘上永远停着「已经作废的那张」——
    用一次废一次，过几天打开就登不上了。这是「登录态越来越短」的第二个原因。

    现在两件事一起做：① 把 serviceToken 归一到当前这张；
    ② **整份 cookie 合并落盘**（服务端还会下发新的 passToken / 设备票 / 新的 expires，
    以前只挑 serviceToken 存，其余全丢）。
    """
    global TOKEN
    try:
        toks = service_tokens(s)
    except Exception:
        return
    if not toks:
        return
    with LOCK:
        cur = TOKEN
    if cur in toks and len(toks) == 1:
        keep_only_token(s, cur)      # 顺手清掉同名不同域的旧条目
    elif cur in toks:
        # 老票还在，但同时冒出新票：只留新的那张
        new = [t for t in toks if t != cur][-1]
        keep_only_token(s, new)
        with LOCK:
            TOKEN = new
        note_service_token(new)
    else:
        # 老票已经不在 session 里了：服务端换过票，收下新的
        new = toks[-1]
        keep_only_token(s, new)
        with LOCK:
            TOKEN = new
        note_service_token(new)
        print("[登录] serviceToken 已轮换（长 %d）" % len(new))
    try:
        # 批量扫描（时间轴探测）时不落盘：每次 DPAPI+fsync 约 0.4 秒，
        # 几百次探测会从 16 秒劣化到 178 秒。扫完由调用方统一存一次。
        if BULK_SCAN[0]:
            return
        if save_session(s, merge=True):
            print("[登录] 服务端新下发的 cookie 已写回磁盘")
    except Exception:
        pass


def api_get(path, **params):
    r = None
    relogined = False
    # 最多 4 次：① 原请求 ② 票被别的线程轮换 → 换票重试 ③ 真失效 → 续期后重试
    for attempt in range(4):
        # 只在取 session 的那一瞬加锁：网络请求本身不占锁，
        # 否则缩略图 / 列表 / 便签的请求会互相排队（之前就是这里把「加载更多」卡死的）
        with LOCK:
            s, tok = S, TOKEN
        r = s.get(API + path, params=params, timeout=60)
        # 只在成功时收票：401 响应里服务端也会下发一张（废）serviceToken，
        # 要是把它收下并写回磁盘，就把好票覆盖掉了 —— 结果是
        # 「登录成功 → 下一个请求 401 → 续期 → 又 401」的死循环。
        if r.status_code != 401:
            _sync_token(s)
            break
        # 401 要分两种：
        #   a) serviceToken 每响应轮换一次，并发请求里别的线程已经换过票了 ——
        #      这种 401 换张票重试就行，**绝不能**去续期（续期才是耗票的那个动作）；
        #   b) 整段会话真的失效。
        with LOCK:
            tok_now = TOKEN
        if not relogined and tok_now != tok:
            print("[登录] 401：票已被其它请求轮换，换新票重试")
            continue
        if not relogined and silent_relogin():
            relogined = True
            continue
        if not relogined and recover_from_client():
            relogined = True
            continue
        if path.startswith(STRONG_AUTH_PREFIX):
            raise NeedStrongAuth(path)
        raise NeedLogin()
    try:
        j = r.json()
    except Exception:
        raise RuntimeError("接口 %s 返回非 JSON（HTTP %s）" % (path, r.status_code))
    if j.get("code") == 401 or j.get("R") == 401:
        if silent_relogin() or recover_from_client():
            with LOCK:
                s = S
            r = s.get(API + path, params=params, timeout=60)
            j = r.json()
            if not (j.get("code") == 401 or j.get("R") == 401):
                return j
        raise NeedLogin()
    return j


def api_post(path, data):
    r = None
    relogined = False
    for attempt in range(4):
        with LOCK:
            s, tok = S, TOKEN
        r = s.post(API + path, data={"data": json.dumps(data),
                                     "serviceToken": tok}, timeout=60)
        if r.status_code != 401:
            _sync_token(s)
            break
        with LOCK:
            tok_now = TOKEN
        if not relogined and tok_now != tok:
            print("[登录] 401：票已被其它请求轮换，换新票重试")
            continue
        if not relogined and silent_relogin():
            relogined = True
            continue
        if not relogined and recover_from_client():
            relogined = True
            continue
        if path.startswith(STRONG_AUTH_PREFIX):
            raise NeedStrongAuth(path)
        raise NeedLogin()
    return r.json()


def api_post_form(path, data):
    """像网页版那样表单提交：字段原样 + serviceToken。"""
    relogined = False
    for attempt in range(4):
        with LOCK:
            s, tok = S, TOKEN
        payload = dict(data)
        payload["serviceToken"] = tok
        r = s.post(API + path, data=payload, timeout=60)
        if r.status_code != 401:
            _sync_token(s)
            break
        with LOCK:
            tok_now = TOKEN
        if not relogined and tok_now != tok:
            print("[登录] 401：票已被其它请求轮换，换新票重试")
            continue
        if not relogined and silent_relogin():
            relogined = True
            continue
        if not relogined and recover_from_client():
            relogined = True
            continue
        if path.startswith(STRONG_AUTH_PREFIX):
            raise NeedStrongAuth(path)
        raise NeedLogin()
    try:
        return r.json()
    except Exception:
        raise RuntimeError("接口 %s 返回非 JSON（HTTP %s）" % (path, r.status_code))


def raw_get(url, **kw):
    return requests.get(url, timeout=kw.pop("timeout", 300), **kw)


# ---------------------------------------------------------------- 工具
def rc4(key, data):
    S_ = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S_[i] + key[i % len(key)]) & 255
        S_[i], S_[j] = S_[j], S_[i]
    i = j = 0
    out = bytearray(len(data))
    for n, b in enumerate(data):
        i = (i + 1) & 255
        j = (j + S_[i]) & 255
        S_[i], S_[j] = S_[j], S_[i]
        out[n] = b ^ S_[(S_[i] + S_[j]) & 255]   # 以前这里写成了全局 S（requests.Session），一走 RC4 分支就必崩
    return bytes(out)


MAGIC = (b"\xff\xd8", b"\x89P", b"GIF8", b"RIFF", b"II*\x00", b"MM\x00*", b"\x00\x00\x00")


def _img_complete(data):
    """整段解码一遍：能画出来才算解对了。
    只检查文件头是不够的 —— 第一块无论按哪种 RC4 解都一样，从第二块才开始分叉，
    结果就是「上半截正常、下半截坏掉」，而文件头看着完全合法。"""
    try:
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()      # HEIC 也要能判，不然一律判成"解不开"
        except Exception:
            pass
        im = Image.open(io.BytesIO(data))
        im.load()
        return True
    except Exception:
        return False


def _decrypt_parts(parts, key):
    """RC4 有两种实现：整段连续一个密钥流 / 每块各自重置密钥流。按能否解出完整图来选。"""
    joined = b"".join(parts)
    a = rc4(key, joined)
    if _img_complete(a):
        return a
    per = b"".join(rc4(key, p) for p in parts)
    if _img_complete(per):
        print("[解密] 逐块 RC4 才完整（连续密钥流版本从中间就坏了）")
        return per
    print("[解密] 两种 RC4 都解不完整，先返回连续流结果")
    return a


def decrypt_blocks(blocks, key_hex):
    """下载所有块并（若有密钥）RC4 解密。"""
    parts = []
    for b in blocks:
        urls = b.get("urls") or []
        if not urls:
            continue
        u = urls[0].replace("http://", "https://")
        parts.append(raw_get(u, timeout=300).content)
    if not key_hex:
        return b"".join(parts)
    return _decrypt_parts(parts, bytes.fromhex(key_hex))


# ---------------------------------------------------------------- 端到端解密
APPKEYS = {}


def load_appkeys():
    """读 bind_appkey.py 换来的 appKey（32 字节，长期有效）。"""
    global APPKEYS
    try:
        with open(os.path.join(CACHE_DIR, "appkeys.json"), encoding="utf-8") as f:
            j = json.load(f)
        import base64 as _b64
        for a in j.get("appKeyList") or []:
            v = str(a.get("appKeyVersion"))
            s = (a.get("appKey") or "").replace("-", "+").replace("_", "/")
            APPKEYS[v] = _b64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))
        print("[解密] 已加载 appKey %d 个：%s" % (len(APPKEYS), list(APPKEYS)))
    except Exception as e:
        APPKEYS = {}
        print("[解密] 没有 appKey（%s），加密照片将无法预览" % type(e).__name__)


def b64u(s):
    s = (s or "").replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))


load_appkeys()


def aes_ctr(key, iv, data):
    d = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    return d.update(data) + d.finalize()


def unlock_record_key(info):
    """encryptInfo -> 32 字节 recordKey（用 appKey 解 encryptedRecordKey）。"""
    if not info:
        raise RuntimeError("缺少 encryptInfo")
    ver = str(info.get("appKeyVersion"))
    ak = APPKEYS.get(ver)
    if not ak:
        raise RuntimeError("没有版本 %s 的 appKey" % ver)
    er = b64u(info.get("encryptedRecordKey"))
    return AESGCM(ak).decrypt(b64u(info.get("recordIV")), er[:-4], None)


def unlock_note_record_key(info):
    """便签体系（含笔记插图）用 micloud 命名空间的 appKey，和相册那套不同。"""
    if not info:
        raise RuntimeError("缺少 encryptInfo")
    ver = str(info.get("appKeyVersion"))
    ak = NOTE_APPKEYS.get(ver) or APPKEYS.get(ver)
    if not ak:
        raise RuntimeError("没有版本 %s 的便签 appKey" % ver)
    er = b64u(info.get("encryptedRecordKey"))
    return AESGCM(ak).decrypt(b64u(info.get("recordIV")), er[:-4], None)


def decrypt_media(info, blob):
    """用 encryptInfo 里的 recordIV 作 AES-CTR 计数器解整段媒体。"""
    return aes_ctr(unlock_record_key(info), b64u(info.get("recordIV")), blob)


# ---------------------------------------------------------------- 便签解密
# 便签跟照片不是同一套密钥：keybag 的命名空间是 "micloud"，照片是 "gallery"，
# 两套 appKey 值不同。好在 micloud 这套可以用换相册 appKey 时拿到的
# encryptToken 直接再要一次，不用再收短信/验证码。
# 密文格式（客户端 common.js）：
#   16 字节魔数头 k + AES-GCM(recordKey, iv = 完整的 16 字节 recordIV)，无 AAD
NOTE_APPKEYS = {}
NOTE_KEY_FILE = os.path.join(CACHE_DIR, "appkeys_note.json")
NOTE_MAGIC = bytes([1] + list(range(17, 31)) + [126])      # common.js: k = [1,17..30,126]


def load_note_appkeys():
    """读 cache/appkeys_note.json。"""
    global NOTE_APPKEYS
    NOTE_APPKEYS = {}
    try:
        with open(NOTE_KEY_FILE, encoding="utf-8") as f:
            j = json.load(f)
        for a in j.get("appKeyList") or []:
            NOTE_APPKEYS[str(a.get("appKeyVersion"))] = b64u(a.get("appKey"))
        if NOTE_APPKEYS:
            print("[便签] 已加载 appKey %d 个：%s"
                  % (len(NOTE_APPKEYS), list(NOTE_APPKEYS)))
    except Exception:
        pass


def fetch_note_appkeys():
    """用相册那次的 encryptToken，再要一套 micloud 命名空间的 appKey。"""
    global NOTE_APPKEYS
    try:
        with open(os.path.join(CACHE_DIR, "appkeys.json"), encoding="utf-8") as f:
            et = (json.load(f) or {}).get("encryptToken") or ""
    except Exception:
        et = ""
    if not et:
        print("[便签] 没有 encryptToken，无法换便签 appKey")
        return False
    try:
        kv = _my_cookies_map()
        if not kv:
            return False
        uid = int(kv["userId"]) if str(kv.get("userId", "")).isdigit() else 0
        with LOCK:
            s, tk = S, TOKEN
        j = keybag.get_appkeys(s, tk, uid, name_space="micloud", encrypt_token=et)
        d = j.get("data") or {}
        if not d.get("appkeyTicket"):
            print("[便签] 服务端没给 appkeyTicket：%s" % j.get("description"))
            return False
        inner = keybag.unseal(d["appkeyTicket"], j["_private_key"],
                              d["tephemeralKey"], d["ephemeralKeyIv"],
                              d["ephemeralKeyAad"])
    except Exception as e:
        print("[便签] 换 appKey 失败：%s %s" % (type(e).__name__, e))
        return False
    aks = inner.get("appKey") or []
    try:
        with open(NOTE_KEY_FILE, "w", encoding="utf-8") as f:
            json.dump({"encryptToken": inner.get("encryptToken") or "",
                       "appKeyList": aks}, f)
    except Exception:
        pass
    NOTE_APPKEYS = {str(a.get("appKeyVersion")): b64u(a.get("appKey")) for a in aks}
    print("[便签] 已换到 appKey %d 个：%s" % (len(NOTE_APPKEYS), list(NOTE_APPKEYS)))
    return bool(NOTE_APPKEYS)


def decrypt_note(text, info):
    """便签正文/摘要：AES-GCM，IV 用完整的 16 字节 recordIV。"""
    if not text or not info:
        return None
    ver = str(info.get("appKeyVersion"))
    ak = NOTE_APPKEYS.get(ver)
    if not ak:
        fetch_note_appkeys()
        ak = NOTE_APPKEYS.get(ver)
        if not ak:
            return None
    iv = b64u(info.get("recordIV"))
    rk = AESGCM(ak).decrypt(iv, b64u(info.get("encryptedRecordKey"))[:-4], None)
    raw = b64u(text)
    if not raw.startswith(NOTE_MAGIC):
        raise RuntimeError("便签密文头不对")
    return AESGCM(rk).decrypt(iv, raw[len(NOTE_MAGIC):], None).decode("utf-8", "replace")


def _b64u_nopad(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _cur_appkey_version():
    vers = [int(k) for k in NOTE_APPKEYS if str(k).isdigit() and int(k) > 1]
    return max(vers) if vers else int(next(iter(NOTE_APPKEYS)))


def encrypt_fields(fields):
    """跟客户端一致：一条记录一个 recordKey + 一个 IV，各字段共用同一份 encryptInfo。
    密钥封装 = AES-GCM(appKey, iv=recordIV) 再在末尾补 4 字节 recordKey 的 CRC32。
    """
    if not NOTE_APPKEYS:
        raise RuntimeError("没有便签 appKey，无法写入")
    ver = _cur_appkey_version()
    ak = NOTE_APPKEYS[str(ver)]
    rk, iv = os.urandom(32), os.urandom(16)
    wrapped = AESGCM(ak).encrypt(iv, rk, None) + struct.pack(">I", zlib.crc32(rk))
    info = {"appKeyVersion": ver, "recordIV": _b64u_nopad(iv),
            "encryptedRecordKey": _b64u_nopad(wrapped)}
    out = {k: _b64u_nopad(NOTE_MAGIC + AESGCM(rk).encrypt(iv, v.encode("utf-8"), None))
           for k, v in fields.items() if v is not None}
    return out, info


def note_create(title, content, folder_id="0"):
    """新建一条 markdown 笔记，返回新笔记 id。"""
    title = (title or "").strip()
    content = (content or "").strip()
    if not title and not content:
        raise RuntimeError("标题和正文不能都为空")
    body = content or title
    first = next((l for l in body.splitlines() if l.strip()), "")
    snippet = re.sub(r"^#+\s*", "", first).strip()[:80]
    head = title or snippet or "无标题"
    enc, info = encrypt_fields({"content": body, "snippet": snippet})
    now = int(time.time() * 1000)
    entry = {
        "content": enc["content"], "snippet": enc["snippet"],
        "colorId": 0, "folderId": str(folder_id or "0"), "alertDate": 0,
        "type": "mdNote", "createDate": now, "modifyDate": now,
        "subject": head,
        "extraInfo": json.dumps({"title": head, "note_content_type": "main",
                                 "isPrivate": False, "clientUuid": str(uuid.uuid4()),
                                 "relationType": "main"}, ensure_ascii=False),
        "setting": {}, "encryptInfo": info,
    }
    j = api_post_form("/note/v2/note",
                      {"entry": json.dumps(entry, ensure_ascii=False)})
    if j.get("result") != "ok":
        raise RuntimeError(j.get("description") or j.get("reason") or "创建笔记失败")
    return {"id": str(((j.get("data") or {}).get("entry") or {}).get("id") or "")}


def note_save(nid, title, content):
    """改一条已有笔记：保留原条目上的其它字段，只换正文/标题/摘要。"""
    j = api_get("/note/v2/note/%s" % nid)
    entry = (j.get("data") or {}).get("entry") or {}
    if not entry:
        raise RuntimeError("读不到这条笔记")
    title = (title or "").strip()
    content = (content or "").strip()
    first = next((l for l in content.splitlines() if l.strip()), "")
    snippet = re.sub(r"^#+\s*", "", first).strip()[:80]
    head = title or snippet or "无标题"
    enc, info = encrypt_fields({"content": content, "snippet": snippet})
    try:
        extra = json.loads(entry.get("extraInfo") or "{}")
    except Exception:
        extra = {}
    extra["title"] = head
    entry.update({
        "content": enc["content"], "snippet": enc["snippet"],
        "subject": head, "encryptInfo": info,
        "extraInfo": json.dumps(extra, ensure_ascii=False),
        "modifyDate": int(time.time() * 1000),
        "type": entry.get("type") or "mdNote",
        "status": "normal",
    })
    for k in ("id", "tag"):
        if entry.get(k) is not None:
            entry[k] = str(entry[k])
    j2 = api_post_form("/note/v2/note/%s" % nid,
                       {"entry": json.dumps(entry, ensure_ascii=False)})
    if j2.get("result") != "ok":
        raise RuntimeError(j2.get("description") or j2.get("reason") or "保存失败")
    return {"id": nid}


def _todo_max_ids():
    if TODO_CACHE.get("maxId") is not None:
        return TODO_CACHE["maxId"], TODO_CACHE["maxSort"]
    ids, sorts, token, guard = [], [], None, 0
    while True:
        params = {"limit": 200}
        if token:
            params["syncToken"] = json.dumps(token, separators=(",", ":"))
        d = api_get("/todo/v1/user/records", **params).get("data") or {}
        for r in d.get("records") or []:
            e = (r.get("contentJson") or {}).get("entity") or {}
            try:
                ids.append(int(e.get("id")))
            except Exception:
                pass
            try:
                sorts.append(int(e.get("customSortId")))
            except Exception:
                pass
        nxt = d.get("syncToken")
        guard += 1
        if (not d.get("hasMore") or not isinstance(nxt, dict)
                or nxt == token or guard > 30):
            break
        token = nxt
    mx, ms = (max(ids) if ids else 7000), (max(sorts) if sorts else 0)
    TODO_CACHE.update({"maxId": mx, "maxSort": ms})
    return mx, ms


def todo_create(title, subs=None):
    """新建代办，带子项时按客户端格式写 listType=1 + subTodoEntities。"""
    title = (title or "").strip()
    subs = [str(s).strip() for s in (subs or []) if str(s).strip()]
    if not title:
        raise RuntimeError("内容不能为空")
    if subs:
        content = json.dumps({"title": title, "isExpand": True,
                              "subTodoEntities": [{"content": s, "isFinish": False}
                                                  for s in subs]}, ensure_ascii=False)
        plain = "\n".join([title] + subs)
        list_type = 1
    else:
        content = plain = title
        list_type = 0
    enc, info = encrypt_fields({"content": content, "plainText": plain})
    now = int(time.time() * 1000)
    mx, ms = _todo_max_ids()
    ent = {
        "id": mx + 1, "customSortId": ms + 1048576,
        "createTime": now, "lastModifiedTime": now, "inputType": 0,
        "content": enc["content"], "plainText": enc["plainText"],
        "listType": list_type, "isFinish": 0, "markFinishTime": 0, "encryptInfo": info,
        "expireTime": 0, "hideType": 0, "folderId": 0, "colorLabel": 0,
        "source": 0, "localStatus": 0, "serverStatus": 0, "is_finish": False,
        "syncId": -1, "syncEtag": 0,
        "remindType": 0, "remindRepeatType": 0, "remindTime": 0,
        "firstRemindTime": 0,
        "audioFileField": "", "audioFileName": "", "audioFileSize": 0,
    }
    rec = {"type": "entity", "contentJson": dict(ent, assets=[])}
    j = api_post_form("/todo/v1/user/records",
                      {"record": json.dumps(rec, ensure_ascii=False)})
    if j.get("result") != "ok":
        raise RuntimeError(j.get("description") or j.get("reason") or "创建代办失败")
    TODO_CACHE.update({"at": 0, "maxId": mx + 1, "maxSort": ms + 1048576})
    return {"ok": True}


def note_to_text(markup):
    """小米笔记的富文本标记 -> 纯文本（勾选框用 ☐ 占位）。"""
    if not markup:
        return ""
    t = re.sub(r'<input[^>]*type="checkbox"[^>]*>', "☐ ", markup)
    t = re.sub(r"<img[^>]*/?>", "[图片]", t)
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"<bullet[^>]*/?>", "• ", t)
    t = re.sub(r"<text[^>]*>", "\n", t)
    t = re.sub(r"</text>", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = (t.replace("&nbsp;", " ").replace("&lt;", "<")
         .replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&"))
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


load_note_appkeys()


# 条目 id -> 各尺寸的加密信息，供 /api/thumb 用
MEDIA_META = {}


def remember_meta(it, mid):
    tei = it.get("thumbEncryptInfo") or {}
    META_LOCK.acquire()
    try:
        MEDIA_META[mid] = {
            "small": {"enc": tei.get("smallThumbEncryptInfo"),
                      "url": (it.get("thumbnailInfo") or {}).get("data")},
            "large": {"enc": tei.get("largeThumbEncryptInfo"),
                      "url": (it.get("bigThumbnailInfo") or {}).get("data")},
            "full": {"enc": it.get("encryptInfo"),
                     "url": (it.get("thumbnailInfo") or {}).get("data"),
                     "id": it.get("id")},
        }
    finally:
        META_LOCK.release()


META_LOCK = threading.Lock()
# 缩略图同时最多解 6 张：一屏几十张一起下载+解密会把其它请求全拖慢
THUMB_SEM = threading.Semaphore(6)


def thumb_bytes(mid, size="small"):
    """取缩略图（必要时下载+解密），带磁盘缓存。"""
    cache_file = os.path.join(CACHE_DIR, "thumbs",
                              "%s_%s.jpg" % (re.sub(r"\W", "_", str(mid)), size))
    if os.path.exists(cache_file):
        cache_gc.touch(cache_file)          # 记一次「刚用过」，LRU 才不会误删
        with open(cache_file, "rb") as f:
            return f.read()
    with META_LOCK:
        meta = (MEDIA_META.get(str(mid)) or {}).get(size) or {}
    if not meta.get("enc") or not meta.get("url"):
        raise RuntimeError("这个尺寸没有加密信息")
    if os.path.exists(cache_file):          # 排队期间别人已经解好了
        with open(cache_file, "rb") as f:
            return f.read()
    with THUMB_SEM:
        blob = raw_get(meta["url"], timeout=120).content
        data = decrypt_media(meta["enc"], blob)
        if size == "small" and len(data) > 250 * 1024:
            data = _shrink_thumb(data)          # 视频缩略图动辄 1.5 MB，网格扛不住
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "wb") as f:
            f.write(data)
    return data


def _shrink_thumb(data, max_w=480):
    """缩略图下采样（只用于 148px 的格子）：1.5 MB → 30 KB 级别，网格瞬间出图。"""
    try:
        import io as _io

        from PIL import Image
        im = Image.open(_io.BytesIO(data))
        if im.width <= max_w:
            return data
        h = max(1, round(im.height * max_w / im.width))
        im = im.resize((max_w, h), Image.LANCZOS)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        out = _io.BytesIO()
        im.save(out, "JPEG", quality=82, optimize=True)
        return out.getvalue()
    except Exception as e:
        print("[缩略图] 下采样失败（直接用原图）：%s" % e)
        return data


FULL_SEM = threading.Semaphore(2)


def _sniff(data):
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:4] == b"\x89PNG":
        return "png"
    if data[4:8] == b"ftyp":
        return "heic"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return "unknown"


def _heic_to_jpeg(data):
    """HEIC → JPEG：Windows 的 WebView2 不认 HEIC，转一次但只转一次（有缓存）。"""
    try:
        import io

        import pillow_heif
        from PIL import Image, ImageOps
        pillow_heif.register_heif_opener()
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "JPEG", quality=92, optimize=True)
        return out.getvalue()
    except Exception as e:
        print("[原图] HEIC 转换失败：%s %s" % (type(e).__name__, e))
        return None


def full_cache_path(mid):
    """原图磁盘缓存位置（cache/full/<id>.jpg）——界面也要用它判断「这张已经存过了」。"""
    return os.path.join(CACHE_DIR, "full", re.sub(r"\W", "_", str(mid)) + ".jpg")


def full_bytes(mid):
    """原图：从 /gallery/image/storage 拿直链，整段下载后按 encryptInfo 解密。"""
    key = re.sub(r"\W", "_", str(mid))
    cache_file = full_cache_path(mid)
    if os.path.exists(cache_file):
        cache_gc.touch(cache_file)
        with open(cache_file, "rb") as f:
            return f.read()
    with FULL_SEM:
        if os.path.exists(cache_file):
            cache_gc.touch(cache_file)
            with open(cache_file, "rb") as f:
                return f.read()
        res = api_get("/gallery/image/storage", id=mid)
        dat = res.get("data") or {}
        kss = dat.get("kss") or {}
        info = dat.get("encryptInfo")
        parts = []
        for b in kss.get("blocks") or []:
            urls = b.get("urls") or []
            if urls:
                parts.append(raw_get(urls[0].replace("http://", "https://"),
                                     timeout=300).content)
        data = b"".join(parts)
        if not data:
            raise RuntimeError("服务器没给原图数据")
        if info and APPKEYS:
            data = decrypt_media(info, data)
        elif kss.get("secure_key"):
            data = _decrypt_parts(parts, bytes.fromhex(kss.get("secure_key")))
        kind = _sniff(data)
        if kind == "heic":
            conv = _heic_to_jpeg(data)
            if conv:
                data = conv
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "wb") as f:
            f.write(data)
    return data


VIDEO_STATE = {}
VIDEO_LOCK = threading.Lock()
# 取消标记：worker 每个 chunk 检查一次，被取消就停手并删掉半截文件
VIDEO_CANCEL = set()


def _video_path(mid):
    return os.path.join(CACHE_DIR, "video",
                        re.sub(r"\W", "_", str(mid)) + ".mp4")


def video_state(mid):
    p = _video_path(mid)
    if os.path.exists(p):
        cache_gc.touch(p)
        return {"state": "ready", "size": os.path.getsize(p), "err": None,
                "got": os.path.getsize(p), "total": os.path.getsize(p), "pct": 100}
    st = VIDEO_STATE.get(str(mid)) or {}
    got = st.get("got", 0)
    total = st.get("total", 0)
    pct = int(got * 100 / total) if total else 0
    return {"state": st.get("state", "idle"), "size": 0, "err": st.get("err"),
            "got": got, "total": total, "pct": pct}


def video_start(mid):
    """后台把原视频下载并解密到磁盘；页面轮询 /api/video_state 等它 ready。"""
    mid = str(mid)
    if os.path.exists(_video_path(mid)):
        return
    with VIDEO_LOCK:
        st = VIDEO_STATE.get(mid) or {}
        if st.get("state") == "running":
            return
        VIDEO_STATE[mid] = {"state": "running", "err": None, "got": 0, "total": 0}
    threading.Thread(target=_video_worker, args=(mid,), daemon=True).start()


PLAY_STATE = {}
PLAY_LOCK = threading.Lock()


def _play_path(mid):
    """播放版（H.264）缓存路径，跟原片放一起。"""
    return os.path.join(CACHE_DIR, "video", re.sub(r"\W", "_", str(mid)) + ".play.mp4")


def _hidden_run_kw():
    """子进程窗口：打包成无控制台的 exe 后，不显式隐藏的话 ffmpeg 会各自弹黑窗。"""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                     # SW_HIDE
    return {"startupinfo": si, "creationflags": 0x08000000}   # CREATE_NO_WINDOW


def _ffmpeg_bin():
    import shutil as _sh
    for p in (os.environ.get("MIYUN_FFMPEG"),
              os.path.join(BASE, "ffmpeg.exe"),
              os.path.join(BASE, "ffmpeg", "bin", "ffmpeg.exe"),
              _sh.which("ffmpeg")):
        if p and os.path.exists(p):
            return p
    return None


def _probe_media(path):
    """ffprobe 一把取宽高与时长；拿不到就返回 (0,0,0)。"""
    ff = _ffmpeg_bin()
    if not ff:
        return (0, 0, 0.0)
    probe = os.path.join(os.path.dirname(ff), "ffprobe.exe")
    if not os.path.exists(probe):
        probe = "ffprobe"
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60, **_hidden_run_kw()).stdout
        d = json.loads(out or "{}")
        st = (d.get("streams") or [{}])[0]
        dur = float((d.get("format") or {}).get("duration") or 0)
        return (int(st.get("width") or 0), int(st.get("height") or 0), dur)
    except Exception:
        return (0, 0, 0.0)


def video_play_state(mid):
    mid = str(mid)
    p = _play_path(mid)
    if os.path.exists(p):
        cache_gc.touch(p)
        return {"state": "ready", "pct": 100, "err": None, "msg": ""}
    st = PLAY_STATE.get(mid) or {}
    return {"state": st.get("state", "idle"), "pct": st.get("pct", 0),
            "err": st.get("err"), "msg": st.get("msg", "")}


def video_play_start(mid):
    """生成 720p H.264 播放版。为什么需要它：WebView2 对一部分 H.265 原片直接
    报 MEDIA_ERR_DECODE（实测 2960×1666 的那条秒挂、1440×3200 的能播），
    转一份 H.264 就一定能播；只做一次，之后直接秒开。"""
    mid = str(mid)
    if os.path.exists(_play_path(mid)):
        return
    with PLAY_LOCK:
        st = PLAY_STATE.get(mid) or {}
        if st.get("state") == "running":
            return
        PLAY_STATE[mid] = {"state": "running", "pct": 0, "err": None, "msg": "准备中"}
    threading.Thread(target=_play_worker, args=(mid,), daemon=True).start()


def _play_worker(mid):
    try:
        src = _video_path(mid)
        if not os.path.exists(src):
            video_start(mid)
            t0 = time.time()
            while True:
                time.sleep(0.6)
                st = video_state(mid)
                if os.path.exists(src) and st.get("state") == "ready":
                    break
                if st.get("state") == "error":
                    raise RuntimeError(st.get("err") or "原片下载失败")
                PLAY_STATE[mid]["msg"] = "先取原片 %d%%" % int(st.get("pct") or 0)
                if time.time() - t0 > 1800:
                    raise RuntimeError("原片下载超时")

        ff = _ffmpeg_bin()
        if not ff:
            raise RuntimeError("找不到 ffmpeg，无法转码")
        w, h, dur = _probe_media(src)
        dst = _play_path(mid) + ".part"
        cmd = [ff, "-y", "-i", src, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        if h > 720:
            cmd += ["-vf", "scale=-2:720"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", "-f", "mp4", dst]
        PLAY_STATE[mid].update({"msg": "转码中", "pct": 0})
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True, bufsize=1, **_hidden_run_kw())
        for line in pr.stdout:
            line = line.strip()
            if line.startswith("out_time_us=") and dur > 0:
                try:
                    us = int(line.split("=", 1)[1])
                except Exception:
                    continue
                PLAY_STATE[mid]["pct"] = max(0, min(99, int(us / (dur * 1000000.0) * 100)))
        pr.wait(timeout=900)
        if pr.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError("ffmpeg 转码失败（code=%s）" % pr.returncode)
        os.replace(dst, _play_path(mid))
        PLAY_STATE[mid] = {"state": "ready", "pct": 100, "err": None, "msg": ""}
    except Exception as e:
        try:
            if os.path.exists(_play_path(mid) + ".part"):
                os.remove(_play_path(mid) + ".part")
        except Exception:
            pass
        PLAY_STATE[mid] = {"state": "error", "pct": 0, "err": str(e), "msg": ""}


def video_cancel(mid):
    """取消正在进行的下载：置标记 → worker 下一个 chunk 就停手并删半截文件。"""
    mid = str(mid)
    VIDEO_CANCEL.add(mid)
    VIDEO_STATE[mid] = {"state": "idle", "err": None, "got": 0, "total": 0}
    return {"ok": True}


def _video_worker(mid):
    try:
        res = api_get("/gallery/video/storage", id=mid)
        dat = res.get("data") or {}
        kss = dat.get("kss") or {}
        info = dat.get("encryptInfo")
        blocks = kss.get("blocks") or []

        # 总大小：优先用块声明，其次用文件列表里的 size
        total = 0
        for b in blocks:
            try:
                total += int(b.get("size") or 0)
            except (TypeError, ValueError):
                pass
        if not total:
            try:
                total = int(dat.get("size") or 0)
            except (TypeError, ValueError):
                total = 0
        VIDEO_STATE[mid] = {"state": "running", "err": None, "got": 0, "total": total}

        # 边下边解：AES-CTR 是流式的，可以按块解密后立刻写盘，
        # 这样磁盘上的 .part 文件一直在长大，进度也是真的。
        path = _video_path(mid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"

        if info and APPKEYS:
            key = unlock_record_key(info)
            iv = b64u(info.get("recordIV"))
            dec = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
        elif kss.get("secure_key"):
            dec = None
            rkey = bytes.fromhex(kss["secure_key"])
        else:
            dec = None
            rkey = None

        got = 0
        cancelled = False
        with open(tmp, "wb") as f:
            for b in blocks:
                if cancelled:
                    break
                urls = b.get("urls") or []
                if not urls:
                    continue
                u = urls[0].replace("http://", "https://")
                r = requests.get(u, stream=True, timeout=(20, 120))
                r.raise_for_status()
                for chunk in r.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    if mid in VIDEO_CANCEL:      # 用户点了取消
                        cancelled = True
                        r.close()
                        break
                    got += len(chunk)
                    if dec is not None:
                        f.write(dec.update(chunk))
                    elif rkey is not None:
                        f.write(rc4(rkey, chunk))
                    else:
                        f.write(chunk)
                    VIDEO_STATE[mid] = {"state": "running", "err": None,
                                        "got": got, "total": total or got}
            if dec is not None and not cancelled:
                f.write(dec.finalize())

        if cancelled:
            try:
                os.remove(tmp)
            except OSError:
                pass
            VIDEO_CANCEL.discard(mid)
            VIDEO_STATE[mid] = {"state": "idle", "err": None, "got": 0, "total": 0}
            print("[视频] %s 已取消，半截文件已删除" % mid)
            return

        if got == 0:
            raise RuntimeError("服务器没给视频数据")

        size = os.path.getsize(tmp)
        head = open(tmp, "rb").read(16)
        if b"ftyp" not in head:
            print("[视频] 解密结果不像 MP4，前 16 字节：%s" % head.hex())
        os.replace(tmp, path)
        VIDEO_STATE[mid] = {"state": "ready", "err": None,
                            "got": size, "total": size}
        print("[视频] 已就绪 %s（%.1f MB）" % (mid, size / 1048576.0))
    except NeedLogin:
        VIDEO_STATE[mid] = {"state": "error", "err": "登录已过期"}
    except Exception as e:
        VIDEO_STATE[mid] = {"state": "error", "err": "%s" % e}
        print("[视频] %s 取原视频失败：%s %s" % (mid, type(e).__name__, e))
        try:
            os.remove(_video_path(mid) + ".part")
        except OSError:
            pass


def sanitize(name, fallback):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "")).strip(" .")
    return name or fallback


def record_clean_name(name):
    """小米录音文件名两种形态：
       老：标题_20240821181055.mp3_20_1_1724235077000_1724577257173
       新：标题.m4a_685_0_1795051464615_1795056819960
    """
    name = name or ""
    m = re.match(r"^(.*?)_(\d{14})\.([A-Za-z0-9]+)_(\d+)_", name)
    if m:
        title, ts, ext, dur = m.groups()
        return "%s_%s.%s" % (title.strip() or "录音", ts, ext), int(dur)
    m = re.match(r"^(.*?)\.([A-Za-z0-9]+)_(\d+)_\d+_\d+", name)
    if m:
        title, ext, dur = m.groups()
        return "%s.%s" % (title.strip() or "录音", ext), int(dur)
    return (name or "录音"), None


def record_label(raw, clean):
    """给人看的名字：有标题就用标题，纯数字的再退回干净文件名。"""
    base = re.sub(r"\.[A-Za-z0-9]+$", "", clean or "")
    # 老客户端会存成「9543763(9543763)」，括号里是同一串数字，只留一份
    m0 = re.match(r"^(\d+)\(\1\)$", base)
    if m0:
        return m0.group(1)
    m = re.match(r"^(.*?)_\d{14}$", base)
    if m and re.search(r"[^\d_]", m.group(1) or ""):
        title = m.group(1).strip()
        m1 = re.match(r"^(\d+)\(\1\)$", title)     # 「9543763(9543763)_2026…」
        return m1.group(1) if m1 else title
    if re.search(r"[^\d_]", base):
        return base.strip()
    return clean


def record_ts(raw):
    """从文件名里取录音时间（毫秒）。"""
    raw = raw or ""
    for m in re.finditer(r"(?<!\d)(\d{14})(?!\d)", raw):      # 年月日时分秒
        try:
            t = time.mktime(time.strptime(m.group(1), "%Y%m%d%H%M%S"))
        except Exception:
            continue
        if 946684800 <= t <= 4102444800:                      # 2000 ~ 2100 年
            return int(t * 1000)
    for m in re.finditer(r"(?<!\d)(\d{13})(?!\d)", raw):      # 毫秒时间戳
        ms = int(m.group(1))
        if 946684800000 <= ms <= 4102444800000:
            return ms
    return 0


# ---------------------------------------------------------------- 相册
def pick_thumb(it, big=False):
    for key in (("bigThumbnailInfo", "thumbnailInfo") if big else ("thumbnailInfo", "bigThumbnailInfo")):
        info = it.get(key) or {}
        u = info.get("data") or info.get("url")
        if u:
            return u
    return None


def album_name(a):
    nm = a.get("name")
    if nm:
        return nm
    aid = str(a.get("albumId"))
    # 服务端对系统相簿不返回名字，名字是客户端定的：
    # id=1 相机、id=2 截屏、id=1000 私密相册（和官方客户端一致）。
    return {"1": "相机", "2": "截屏", "1000": "私密相册"}.get(aid, "相簿 %s" % aid)


def album_display_name(nm):
    """小米相册重名时服务端会在名字后面加 _<13位毫秒时间戳> 保证唯一
    （例如「记录」和「记录_1782918851835」是两本不同的相簿）。
    手机端展示时会把这段后缀吃掉，这里同样只做展示处理，id 不变。"""
    m = re.match(r"^(.*?)_(\d{13})$", nm or "")
    if m:
        ms = int(m.group(2))
        if 946684800000 <= ms <= 4102444800000:      # 2000 ~ 2100 年
            return m.group(1)
    return nm


def list_albums():
    res = api_get("/gallery/user/album/list", pageNum=0, pageSize=50,
                  isShared="false", numOfThumbnails=1)
    out = []
    for a in (res.get("data") or {}).get("albums") or []:
        th = (a.get("thumbnails") or [{}])[0]
        raw_name = album_name(a)
        out.append({
            "id": str(a.get("albumId")),
            "name": album_display_name(raw_name),
            "rawName": raw_name,
            "count": int(a.get("mediaCount") or 0),
            "thumb": th.get("url"),
        })
    out.sort(key=lambda x: (x["id"] not in ("1", "2"), -x["count"]))
    for x in out:
        x["private"] = (x["id"] == "1000")

    # 空相簿（0 项）：**本地隐藏，不删云端**。
    # 云端这些是手机自动建的（扫一扫/便签/Twitter 等），删了会影响手机端，
    # 而用户的诉求是"看不见就行"。所以只在列表里过滤，云端一个字节都不动。
    out = [x for x in out if x["count"] > 0 or x["id"] in ("1", "2", "1000")]

    # 同名相簿合并（方案 B）。服务端确实存在「资料」和「资料_<13位时间戳>」两本，
    # 内容零重叠（167 + 127 张，交集 0）—— 是同一主题被拆成了两本。
    # 手机端显示成一本，这里跟随手机端：合并成一条，id 用逗号连起来，
    # 点进去两本内容按拍摄时间混排。服务端结构不动（不删、不改名）。
    merged = {}
    order = []
    for x in out:
        nm = x["name"]
        if nm in merged:
            merged[nm]["ids"].append(x["id"])
            merged[nm]["count"] += x["count"]
        else:
            y = dict(x)
            y["ids"] = [x["id"]]
            merged[nm] = y
            order.append(nm)
    out = [merged[k] for k in order]
    for x in out:
        x["id"] = ",".join(x["ids"])
        if len(x["ids"]) > 1:
            x["mergedFrom"] = len(x["ids"])
    return out


# ---------------------------------------------------------------- 逆地理编码
# 小米自己没有「经纬度 → 地址」的接口（40+ 条候选路径全是 404；网页版那些地址
# 是服务端早期算好写进 item.geoInfo 的，新照片已不再产出）。所以这里用 Photon
# （OSM 数据，免 key，实测国内直连 0.6~1.0s，name 标签本身就是中文）。
GEO_CACHE_DIR = os.path.join(CACHE_DIR, "geo")
GEO_LOCK = threading.Lock()
_GEO_LAST = [0.0]


def reverse_geo(lat, lon):
    """经纬度 → 中文地址。先查本地缓存，再调 Photon；拿不到就返回 address=None。"""
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return {"address": None}
    key = "%.5f_%.5f" % (lat, lon)
    p = os.path.join(GEO_CACHE_DIR, key + ".json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass

    # Photon 是 fair use，最多 1 请求/秒
    with GEO_LOCK:
        gap = 1.0 - (time.time() - _GEO_LAST[0])
        if gap > 0:
            time.sleep(gap)
        _GEO_LAST[0] = time.time()

    out = {"address": None, "raw": {}}
    try:
        s = requests.Session()
        s.trust_env = False          # 直连实测可用；走代理反而慢
        r = s.get("https://photon.komoot.io/reverse",
                  params={"lat": lat, "lon": lon, "limit": 1},
                  headers={"User-Agent": "MiCloudViewer/1.0"}, timeout=8)
        feat = (((r.json() or {}).get("features") or [{}])[0]) or {}
        props = feat.get("properties") or {}
        # 直辖市里 state 和 city 经常一样，拼接前去重
        seen = []
        for x in (props.get("state"), props.get("city"), props.get("district"),
                  props.get("locality"), props.get("street"), props.get("housenumber")):
            if x and x not in seen:
                seen.append(x)
        out = {"address": "".join(seen) or None,
               "raw": {"city": props.get("city"), "district": props.get("district"),
                       "street": props.get("street"), "name": props.get("name")}}
    except Exception as e:
        out = {"address": None,
               "raw": {"error": "%s: %s" % (type(e).__name__, e)}}

    try:
        os.makedirs(GEO_CACHE_DIR, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    return out


# 相簿的「客户端展示顺序 / 折叠分组」，只存本地，不改云端任何东西
ALBUM_CFG_PATH = os.path.join(BASE, "album_layout.json")


def album_layout_get():
    try:
        with open(ALBUM_CFG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    return {"order": [str(x) for x in d.get("order") or []],
            "folded": [str(x) for x in d.get("folded") or []],
            "foldOpen": bool(d.get("foldOpen"))}


def album_layout_set(d):
    data = {"order": [str(x) for x in d.get("order") or []],
            "folded": [str(x) for x in d.get("folded") or []],
            "foldOpen": bool(d.get("foldOpen"))}
    tmp = ALBUM_CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ALBUM_CFG_PATH)
    return data


def _gallery_one(group_id, page, limit=60, start_date=None, end_date=None,
                 is_video=False):
    """单本相簿的一页（内部函数，返回 (items, is_last)）。

    用的是小米网页版自己在用的 /gallery/user/galleries：
    不传日期时服务端默认从最新往最旧给，pageNum 逐页往后翻。
    start_date / end_date 传 YYYYMMDD 可以按日期区间取（实测单月精确生效）。
    """
    params = {"pageNum": int(page), "pageSize": limit, "isShared": "false"}
    if is_video:
        # 实测（2026-11-23）：服务端只认 onlyVideo=true；isVideo / video / mediaType 一律被忽略，
        # 传了也照样返回图片 + 视频混排。这一条错了，「视频」页就会把整本相册都列出来。
        params["onlyVideo"] = "true"
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    gid = str(group_id or "").strip()
    # "0" / 空 = 全部照片（不传 albumId 才拿全）；"1" 是相机相簿，要真的传 albumId=1
    if gid and gid != "0":
        params["albumId"] = gid
    res = api_get("/gallery/user/galleries", **params)
    d = res.get("data") or {}
    items = []
    for it in d.get("galleries") or []:
        th = it.get("thumbnailInfo") or {}
        bg = it.get("bigThumbnailInfo") or {}
        mid = str(it.get("id"))
        remember_meta(it, mid)
        ex = it.get("exifInfo") or {}
        items.append({
            "id": mid,
            "type": it.get("type") or "image",
            "name": it.get("fileName") or it.get("title") or mid,
            "size": int(it.get("size") or 0),
            "date": int(it.get("dateTaken") or it.get("sortTime") or 0),
            # 拍摄信息：时间/尺寸/机身/光圈/焦距/快门/闪光 + GPS（接口本来就给，以前没取）
            "exif": {k: ex[k] for k in (
                "dateTime", "imageWidth", "imageLength", "orientation", "make", "model",
                "FNumber", "focalLength", "exposureTime", "ISO", "flash",
                "GPSLatitude", "GPSLongitude", "GPSLatitudeRef", "GPSLongitudeRef",
                "GPSAltitude", "GPSDateStamp", "GPSTimeStamp",
            ) if k in ex},
            # 小米后端以前算好的中文地址（只有少数老照片有，命中率约 0.35%），
            # 有就直接用，省一次外部请求
            "geoAddr": ((((it.get("geoInfo") or {}).get("address") or {})
                         .get("addressLines") or [None])[0]),
            "thumb": th.get("data") or th.get("url"),
            "big": bg.get("data") or bg.get("url") or th.get("data") or th.get("url"),
            "enc": bool(it.get("encryptInfo")),
            # 有 appKey 就走本地解密代理，没有才退回直连（老照片本来就是明文）
            "thumbUrl": ("/api/thumb?id=%s&size=small" % mid) if it.get("thumbEncryptInfo") and APPKEYS
                        else (th.get("data") or th.get("url")),
            "bigUrl": ("/api/thumb?id=%s&size=large" % mid) if it.get("thumbEncryptInfo") and APPKEYS
                      else (bg.get("data") or bg.get("url") or th.get("data") or th.get("url")),
            "fullUrl": ("/api/full?id=%s" % mid) if (it.get("type") or "image") == "image" else None,
            # 原图已经在本地缓存里？界面据此直接出原图，不用再点一次「查看原图」
            "fullCached": os.path.exists(full_cache_path(mid)),
        })
    return items, bool(d.get("isLastPage")) or not items


def gallery_page(group_id, page, limit=60, start_date=None, end_date=None,
                 is_video=False):
    """按拍摄时间倒序分页。

    group_id 可能是逗号分隔的多个相簿 id（同名相簿合并后的结果，见 list_albums）。
    这时把每本都取回来合并、按拍摄时间统一排序后再分页，效果就是"一本完整的相簿"。
    start_date / end_date（YYYYMMDD）用于时间轴按月取数。
    """
    gid = str(group_id or "").strip()
    ids = [x for x in gid.split(",") if x]
    if len(ids) > 1:
        merged = []
        for one in ids:
            p = 0
            while p < 60:                      # 单本最多翻 60 页兜底
                batch, last = _gallery_one(one, p, 200, start_date, end_date,
                                            is_video=is_video)
                merged.extend(batch)
                if last or not batch:
                    break
                p += 1
        # 同名的两本云端相簿是「合并展示」：同一张照片如果同时属于两本，
        # 服务端会在两边各返回一次——不按 id 去重，界面里就会看到两遍。
        seen, uniq = set(), []
        for x in merged:
            mid = str(x.get("id") or "")
            if mid in seen:
                continue
            seen.add(mid)
            uniq.append(x)
        merged = uniq
        merged.sort(key=lambda x: -(x.get("date") or 0))
        start = int(page) * int(limit)
        return {"items": merged[start:start + int(limit)],
                "page": int(page),
                "lastPage": start + int(limit) >= len(merged)}
    items, last = _gallery_one(gid, page, limit, start_date, end_date,
                               is_video=is_video)
    return {"items": items, "page": int(page), "lastPage": last}


# 时间轴月份分布的磁盘缓存：探测一次 16 秒，不可能每次打开都重算
TL_CACHE_DIR = os.path.join(CACHE_DIR, "timeline")
TL_LOCK = threading.Lock()


def _tl_session():
    """给时间轴探测专用的独立会话：不碰全局 S/TOKEN/LOCK。

    为什么必须独立：时间轴要探测几十到几百次，走 api_get 每次都要抢全局 LOCK，
    前端所有请求（相册列表、缩略图）会被堵在后面 → 界面卡住几十秒。

    取票顺序：① 主会话里当前那张（内存里，最鲜活）② 磁盘上的候选逐张试。
    只拿磁盘票不行 —— 程序运行中磁盘那份常常是旧的（主会话轮换后没及时落盘）。
    """
    s2 = make_session()
    s2.headers["Referer"] = "https://i.mi.com/"
    # ① 先把主会话的 cookie 整体复制过来
    try:
        with LOCK:
            cur = S
            cur_tok = TOKEN
        if cur is not None:
            for c in cur.cookies:
                try:
                    s2.cookies.set(c.name, c.value,
                                   domain=c.domain or "i.mi.com",
                                   path=c.path or "/")
                except Exception:
                    pass
        if cur_tok:
            keep_only_token(s2, cur_tok)
            try:
                r = s2.get(API + "/status/lite/profile", timeout=20)
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    return s2
            except Exception:
                pass
    except Exception:
        pass

    # ② 退回磁盘候选，逐张试
    try:
        toks = load_session(s2)
        if pick_working_token(s2, toks):
            return s2
    except Exception:
        pass
    return None


def _tl_one(sess, aid, page=0, limit=1, sd=None, ed=None):
    """用指定会话取一页（不走 api_get，不抢锁）。"""
    p = {"pageNum": page, "pageSize": limit, "isShared": "false"}
    if aid and aid not in ("0", "1"):
        p["albumId"] = aid
    if sd:
        p["startDate"] = sd
    if ed:
        p["endDate"] = ed
    try:
        r = sess.get(API + "/gallery/user/galleries", params=p, timeout=60)
        d = (r.json() or {}).get("data") or {}
        return d.get("galleries") or []
    except Exception:
        return []


def gallery_timeline(group_id, video=False):
    """时间轴数据 —— 用官方自己的接口，一次拿全。

    官方前端就是这么做的（app.asar → compiled/renderer/gallery.js）：
        GET /gallery/user/timeline?albumId=&isShared=false
    返回 { dayCount: {"YYYYMMDD": 张数, ...}, indexHash: int }
    实测 1928 天 / 全部照片，耗时 0.04 秒。

    以前我用「逐月探测」去猜，要 300+ 次请求、几十秒——完全是错的。
    """
    gid = str(group_id or "").strip()
    # 官方只接受单个 albumId；合并相簿（逗号分隔）就逐本取再合并
    ids = [x for x in gid.split(",") if x] or [""]

    def _one(aid):
        try:
            r = requests.Session()
            r.trust_env = False
            r.headers.update({
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
                "Referer": "https://i.mi.com/",
            })
            with LOCK:
                cur = S
            if cur is not None:
                for c in cur.cookies:
                    try:
                        r.cookies.set(c.name, c.value,
                                      domain=c.domain or "i.mi.com", path=c.path or "/")
                    except Exception:
                        pass
            # "0" / 空 = 全部照片；其余（含 1=相机）都按相簿取
            tp = {"albumId": ("" if aid in ("", "0") else aid), "isShared": "false"}
            if video:
                tp["onlyVideo"] = "true"        # 同一个参数：时间轴也能只看视频
            res = r.get(API + "/gallery/user/timeline", params=tp, timeout=60)
            return ((res.json() or {}).get("data") or {}).get("dayCount") or {}
        except Exception:
            return {}

    day_count = {}
    for aid in ids:
        for k, v in _one(aid).items():
            day_count[k] = day_count.get(k, 0) + int(v or 0)

    # 聚合成「年 → 月 → 天」，前端画刻度用
    from collections import defaultdict
    years = defaultdict(lambda: defaultdict(int))     # y -> {m: count}
    year_total = defaultdict(int)
    month_days = defaultdict(int)                      # (y,m) -> 有照片的天数
    for k, n in day_count.items():
        if len(k) != 8 or not k.isdigit():
            continue
        y, m = int(k[:4]), int(k[4:6])
        years[y][m] += n
        year_total[y] += n
        month_days[(y, m)] += 1

    months = []
    for y in sorted(years, reverse=True):
        for m in sorted(years[y], reverse=True):
            months.append({"y": y, "m": m, "n": years[y][m],
                           "d": month_days[(y, m)]})
    out = {
        "months": months,
        "years": [{"y": y, "n": year_total[y]} for y in sorted(year_total, reverse=True)],
        "days": day_count,
        "level": "day",
        "from": (min(year_total) if year_total else 0),
        "to": (max(year_total) if year_total else 0),
        "at": time.time(),
    }
    return out
# ---------------------------------------------------------------- 笔记（新版）
# 老的 /note/full/ 是「便签」那一套（type=note，HTML 标记）；用户要的是
# 新版「笔记」：/note/v2/full/page（type=mdNote，正文就是 markdown 纯文本），
# 分类也在 v2 的 folders 里。例外的是 /todo/v1/user/records —— 代办单独一套接口。
def _plain(md):
    """mdNote 的正文是 markdown 纯文本；老格式才带 <text>/<img>/<bullet> 标记。"""
    if not md:
        return ""
    if any(t in md for t in ("<text", "<img", "<bullet", "<input")):
        md = note_to_text(md)
    md = (md.replace("&nbsp;", " ").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&"))
    return md.strip()


def _dec_field(e, field):
    """解一个加密字段；没加密就原样返回。"""
    raw = e.get(field)
    if not raw:
        return ""
    if e.get("encryptInfo"):
        try:
            return decrypt_note(raw, e.get("encryptInfo")) or ""
        except Exception:
            return ""
    return raw


def notes_v2():
    """全部笔记 + 分类。按 syncTag 游标翻页，直到 lastPage。"""
    entries, folders, tag, guard = [], None, None, 0
    while True:
        params = {"limit": 200}
        if tag:
            params["syncTag"] = tag
        d = api_get("/note/v2/full/page", **params).get("data") or {}
        batch = d.get("entries") or []
        entries.extend(batch)
        if folders is None:
            folders = d.get("folders") or []
        nxt = d.get("syncTag")
        guard += 1
        if d.get("lastPage") or not batch or guard > 40 or nxt == tag:
            break
        tag = nxt

    fmap = {}
    for f in folders:
        if f.get("deleteTime"):
            continue
        fmap[str(f.get("id"))] = f.get("subject") or "未命名分类"

    notes, seen = [], set()
    for e in entries:
        if e.get("deleteTime"):
            continue
        nid = str(e.get("id"))
        if nid in seen:
            continue
        seen.add(nid)
        try:
            extra = json.loads(e.get("extraInfo") or "{}")
        except Exception:
            extra = {}
        text = _plain(_dec_field(e, "snippet"))
        flat = re.sub(r"\s+", " ", text).strip()
        title = (extra.get("title") or "").strip() or e.get("subject") or ""
        notes.append({
            "id": nid,
            "title": title or flat[:18] or "（无标题）",
            "preview": flat[:180] or ("加密笔记，点开读取" if e.get("encryptInfo") else "（空）"),
            "modified": int(e.get("modifyDate") or 0),
            "created": int(e.get("createDate") or 0),
            "folder": str(e.get("folderId") or "0"),
            "enc": bool(e.get("encryptInfo")),
        })
    notes.sort(key=lambda x: -x["modified"])

    counts = {}
    for n in notes:
        counts[n["folder"]] = counts.get(n["folder"], 0) + 1
    flist = [{"id": k, "name": v, "count": counts.get(k, 0)}
             for k, v in fmap.items()]
    flist.sort(key=lambda f: -f["count"])
    if counts.get("0"):
        flist.append({"id": "0", "name": "未分类", "count": counts["0"]})
    return {"folders": flist, "notes": notes}


def remember_note_files(entry):
    st = entry.get("setting") or {}
    if not isinstance(st, dict):
        return
    for f in st.get("data") or []:
        dig, fid = f.get("digest"), f.get("fileId")
        if dig and fid:
            NOTE_FILES[dig] = fid


def note_detail(nid):
    e = (api_get("/note/v2/note/%s" % nid).get("data") or {}).get("entry") or {}
    remember_note_files(e)
    try:
        extra = json.loads(e.get("extraInfo") or "{}")
    except Exception:
        extra = {}
    body = _plain(_dec_field(e, "content")) or _plain(_dec_field(e, "snippet"))
    return {
        "id": nid,
        "title": (extra.get("title") or e.get("subject") or "无标题"),
        "content": body,
        "enc": bool(e.get("encryptInfo")),
        "modified": int(e.get("modifyDate") or 0),
        "created": int(e.get("createDate") or 0),
        "tag": str(e.get("tag") or ""),
    }


NOTE_FILES = {}          # 笔记图片 digest -> fileId（详情里的 setting.data）


def note_image(digest):
    """把正文里的 ![](https://com.miui.notes/image/<digest>) 取回本地。"""
    digest = re.sub(r"[^0-9a-fA-F]", "", digest or "")
    if not digest:
        raise RuntimeError("参数不对")
    path = os.path.join(CACHE_DIR, "noteimg", digest + ".img")
    if os.path.exists(path):
        cache_gc.touch(path)
        with open(path, "rb") as f:
            return f.read()
    fid = NOTE_FILES.get(digest)
    if not fid:
        for e in (api_get("/note/v2/full/page", limit=200).get("data") or {}).get("entries") or []:
            remember_note_files(e)
        fid = NOTE_FILES.get(digest)
    if not fid:
        raise RuntimeError("这张图不在任何笔记的附件里")
    j = api_get("/file/full/v2", type="note_img", fileid=fid)
    d = j.get("data") or {}
    kss = d.get("kss") or {}
    buf = bytearray()
    for b in kss.get("blocks") or []:
        urls = b.get("urls") or []
        if urls:
            buf += raw_get(urls[0].replace("http://", "https://"), timeout=300).content
    data = bytes(buf)
    if not data:
        raise RuntimeError("服务器没给图片数据")
    info = d.get("encryptInfo")
    if info and NOTE_APPKEYS:
        data = aes_ctr(unlock_note_record_key(info),
                       b64u(info.get("recordIV")), data)
    elif kss.get("secure_key"):
        data = decrypt_blocks(kss.get("blocks"), kss.get("secure_key"))
    if data[:4] not in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb",
                        b"\x89PNG", b"GIF8", b"RIFF") and data[:2] != b"\xff\xd8":
        raise RuntimeError("图片解密失败")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return data


def note_delete(ids):
    """彻底删除（先进回收站再清空）。"""
    done = []
    for nid in ids:
        nid = str(nid)
        j = api_get("/note/v2/note/%s" % nid)
        e = (j.get("data") or {}).get("entry") or {}
        tag = str(e.get("tag") or nid)
        for purge in ("false", "true"):
            r = api_post_form("/note/v2/full/%s/delete" % nid,
                              {"tag": tag, "purge": purge})
            if r.get("result") != "ok":
                raise RuntimeError(r.get("description") or "删除失败")
            tag = str((r.get("data") or {}).get("tag") or tag)
        done.append(nid)
    return {"deleted": done}


# ---------------------------------------------------------------- 代办
TODO_CACHE = {"at": 0, "items": []}
TODO_RAW = {}                       # tid -> 原始记录（带 eTag），改/删时直接用
TODO_FETCH_LOCK = threading.Lock()  # 同时只允许一个完整拉取
TODO_REFRESH_AFTER = 300            # 缓存超过 5 分钟就在后台刷一次


def _todo_cache_path():
    return os.path.join(CACHE_DIR, "todos.dat")


def _todo_cache_load():
    """读磁盘上的待办缓存（DPAPI 加密，和登录票同一套保护）。只读。"""
    try:
        with open(_todo_cache_path(), "rb") as f:
            o = json.loads(dpapi_unprotect(f.read()).decode("utf-8"))
        if isinstance(o, dict) and isinstance(o.get("items"), list):
            return o
    except Exception:
        pass
    return None


def _todo_cache_save():
    try:
        blob = json.dumps({"at": TODO_CACHE.get("at") or 0,
                           "items": TODO_CACHE.get("items") or [],
                           "maxId": TODO_CACHE.get("maxId"),
                           "maxSort": TODO_CACHE.get("maxSort"),
                           "raw": TODO_RAW or {}},
                          ensure_ascii=False).encode("utf-8")
        data = dpapi_protect(blob)
        p = _todo_cache_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception as e:
        print("[代办] 缓存写盘失败：%s" % e)


def _todo_invalidate():
    """本地改过一条代办：标记缓存过期；下次打开先显示旧的，后台再刷。

    同时把盘上的缓存重写一遍：改过的那条原始记录（eTag）已经被调用方
    从 TODO_RAW 里删掉了，不能还留在盘上，否则下次启动拿旧 eTag 去改会冲突。
    """
    TODO_CACHE["at"] = 0
    _todo_cache_save()


def _todo_parse(ent):
    """/todo/v1/user/records 的 entity：content 是纯文本，或者是带子项的 JSON。"""
    text = _plain(_dec_field(ent, "content"))
    title, subs = text, []
    if text.lstrip().startswith("{") and "subTodoEntities" in text:
        try:
            j = json.loads(text)
            title = (j.get("title") or "").strip()
            subs = [{"text": (s.get("content") or "").strip(),
                     "done": bool(s.get("isFinish"))}
                    for s in j.get("subTodoEntities") or []]
        except Exception:
            pass
    # 完成状态以 markFinishTime 为准：完成时才写这个时间戳；
    # isFinish 这个字段在服务端恒为 1，不能用来判断。
    # 例外是「有子项且子项全做完」的清单：小米客户端把它显示成已完成，
    # 但父项自己的 markFinishTime 仍是 0。
    done = bool(ent.get("markFinishTime")) or bool(subs) and all(s["done"] for s in subs)
    return {
        "title": title.strip() or "（空代办）",
        "subs": subs,
        "done": done,
        "created": int(ent.get("createTime") or 0),
        "remind": int(ent.get("remindTime") or 0),
        "finished": int(ent.get("markFinishTime") or 0),
    }


def _todo_fetch_all(min_at=0):
    """完整拉取全部代办（含已完成），翻页用 syncToken 游标。

    有磁盘缓存时，用户打开待办不需要等这 17 页；
    只有没缓存/强制刷新/后台预热才走这里。
    """
    with TODO_FETCH_LOCK:
        # 等锁期间别的线程已经刷完了，直接用，不重复拉一遍
        if TODO_CACHE["items"] and (TODO_CACHE.get("at") or 0) > min_at:
            return TODO_CACHE["items"]
        out, seen, token, guard = [], set(), None, 0
        max_id, max_sort = 0, 0
        raw = {}
        while True:
            params = {"limit": 200}
            if token:
                params["syncToken"] = json.dumps(token, separators=(",", ":"))
            j = api_get("/todo/v1/user/records", **params)
            d = j.get("data") or {}
            recs = d.get("records") or []
            fresh = 0
            for r in recs:
                if r.get("status") not in (None, "normal"):
                    continue
                cj = r.get("contentJson") or {}
                ent = cj.get("entity") or {}
                tid = str(ent.get("syncId") or r.get("id") or "")
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                fresh += 1
                it = _todo_parse(ent)
                it["id"] = tid
                out.append(it)
                raw[tid] = r
                try:
                    max_id = max(max_id, int(ent.get("id")))
                except Exception:
                    pass
                try:
                    max_sort = max(max_sort, int(ent.get("customSortId")))
                except Exception:
                    pass
            nxt = d.get("syncToken")
            guard += 1
            if not d.get("hasMore") or not fresh or not isinstance(nxt, dict) or guard > 30:
                break
            if nxt == token:
                break
            token = nxt
        out.sort(key=lambda x: (x["done"], -(x["created"] or 0)))
        TODO_CACHE.update({"at": time.time(), "items": out,
                           "maxId": max_id or None, "maxSort": max_sort or None})
        TODO_RAW.clear()
        TODO_RAW.update(raw)
        _todo_cache_save()
        print("[代办] 共 %d 条（未完成 %d）"
              % (len(out), sum(1 for x in out if not x["done"])))
        return out


def _todo_refresh_bg():
    """后台刷一遍待办缓存，不挡用户这次打开。"""
    at0 = TODO_CACHE.get("at") or 0

    def work():
        try:
            _todo_fetch_all(min_at=at0)
        except Exception as e:
            print("[代办] 后台刷新失败：%s %s" % (type(e).__name__, e))

    threading.Thread(target=work, daemon=True).start()


def todo_list(force=False, since=0):
    """全部代办（含已完成）。

    有本地缓存就立刻返回（打开秒出）；缓存超过 5 分钟就在后台向小米刷新。
    没缓存 / 强制刷新时才同步拉取（3386 条约 17 页，要几秒）。

    since：前端手上那份缓存的 at。强制刷新时如果后台已经把缓存刷到比它新，
    直接返回新缓存，不再重复拉一遍。
    """
    if not TODO_CACHE["items"]:
        o = _todo_cache_load()
        if o and o.get("items"):
            TODO_CACHE.update({"at": float(o.get("at") or 0),
                               "items": o["items"],
                               "maxId": o.get("maxId"),
                               "maxSort": o.get("maxSort")})
            if isinstance(o.get("raw"), dict):
                TODO_RAW.clear()
                TODO_RAW.update(o["raw"])
    if not force and TODO_CACHE["items"]:
        if time.time() - (TODO_CACHE.get("at") or 0) > TODO_REFRESH_AFTER:
            _todo_refresh_bg()
        return TODO_CACHE["items"]
    min_at = float(since or 0) if force else (TODO_CACHE.get("at") or 0)
    return _todo_fetch_all(min_at=min_at)


def _todo_find_record(tid):
    """按 id 找原始记录（带 eTag）。

    以前只查第一页 200 条，代办超过 200 条时，改/删老条目会报「找不到」。
    现在先用刚拉全的缓存，缓存没有再逐页翻。
    """
    tid = str(tid)
    hit = TODO_RAW.get(tid)
    if hit:
        return hit
    token, guard = None, 0
    while True:
        params = {"limit": 200}
        if token:
            params["syncToken"] = json.dumps(token, separators=(",", ":"))
        d = api_get("/todo/v1/user/records", **params).get("data") or {}
        for r in d.get("records") or []:
            e = (r.get("contentJson") or {}).get("entity") or {}
            if str(e.get("syncId")) == tid:
                TODO_RAW[tid] = r
                return r
        nxt = d.get("syncToken")
        guard += 1
        if (not d.get("hasMore") or not isinstance(nxt, dict)
                or nxt == token or guard > 30):
            break
        token = nxt
    return None


# ---------------------------------------------------------------- 录音
REC_CACHE = {"at": 0.0, "items": None}


def record_all(force=False):
    """小米录音接口只支持 offset 正序翻页，这里一次取全、按录音时间倒序缓存。"""
    if (not force and REC_CACHE["items"] is not None
            and time.time() - REC_CACHE["at"] < 300):
        return REC_CACHE["items"]
    out, off = [], 0
    while True:
        res = api_get("/sfs/ns/recorder/dir/0/list",
                      offset=off, limit=1000, _dc=int(time.time() * 1000))
        batch = [x for x in (res.get("data") or {}).get("list") or []
                 if x.get("type") == "file"]
        for it in batch:
            raw = it.get("name") or ""
            clean, dur = record_clean_name(raw)
            out.append({
                "id": str(it.get("id")),
                "name": clean,
                "label": record_label(raw, clean),
                "raw": raw,
                "size": int(it.get("size") or 0),
                "date": record_ts(raw) or int(it.get("create_time") or 0),
                "dur": dur,
                "enc": it.get("encryptInfo"),     # 录音是加密存的，播放/下载都要它
            })
        if len(batch) < 1000:
            break
        off += 1000
        if off > 50000:
            break
    out.sort(key=lambda x: -(x["date"] or 0))
    REC_CACHE.update({"at": time.time(), "items": out})
    print("[录音] 共 %d 条" % len(out))
    return out


def record_page(offset=0, limit=100, force=False):
    return record_all(force)[offset:offset + limit]


def record_geturl(rid):
    """取录音的下载信息：不只是 url，还可能带 encryptInfo（录音文件是加密存的）。"""
    res = api_get("/sfs/ns/recorder/file/%s/storage/geturl" % rid)
    return res.get("data") or {}


def record_url(rid):
    return record_geturl(rid).get("url")


# ── 录音播放：本地代理 + 必要时转码 ──────────────────────────────────
# 小米返回的是 CDN 直链，WebView 里直接 <audio src> 有两个坑：
#   ① 录音常是 AMR，Chromium 不支持，play() 直接失败；
#   ② 直链可能挑 Referer / 过期，浏览器拿不到。
# 所以统一走本地：下载到 cache/rec_audio，能直接播的原样发，不能播的 ffmpeg 转 m4a。
REC_AUDIO_DIR = os.path.join(CACHE_DIR, "rec_audio")
REC_AUDIO_LOCK = threading.Lock()
_AUDIO_OK = ("mp3", "m4a", "wav", "ogg", "flac", "webm")


def _audio_kind(head):
    if head[:5] == b"#!AMR":
        return "amr"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF":
        return "wav"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    return ""


def _audio_ctype(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return {"m4a": "audio/mp4", "mp3": "audio/mpeg", "wav": "audio/wav",
            "ogg": "audio/ogg", "flac": "audio/flac", "webm": "audio/webm",
            "amr": "audio/amr"}.get(ext, "application/octet-stream")


def _record_enc_from_cache(rid):
    for x in (REC_CACHE.get("items") or []):
        if str(x.get("id")) == str(rid) and x.get("enc"):
            return x["enc"]
    return None


def record_audio_file(rid):
    """把一条录音准备成本地可播放文件，返回路径（有缓存就直接用）。

    录音文件是**加密存储**的：拿到的字节不是 mp3，直接丢给 ffmpeg 会失败。
    解密信息在 geturl 的 encryptInfo 里（个别账号在录音列表项上），
    先用 appKey 解出 recordKey，再用 AES-CTR 解整段，再做格式判断/转码。
    """
    rid = str(rid)
    os.makedirs(REC_AUDIO_DIR, exist_ok=True)
    base = os.path.join(REC_AUDIO_DIR, re.sub(r"\W", "_", rid)[:80])
    with REC_AUDIO_LOCK:
        for ext in ("m4a", "mp3", "wav", "ogg", "flac", "webm", "amr"):
            p = base + "." + ext
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        info = record_geturl(rid)
        url = info.get("url")
        if not url:
            raise RuntimeError("这条录音没有播放地址")
        enc = (info.get("encryptInfo") or info.get("fileEncryptInfo")
               or info.get("enc") or _record_enc_from_cache(rid))
        tmp = base + ".download"
        r = requests.get(url, stream=True, timeout=180, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
            "Referer": "https://i.mi.com/",
        })
        if r.status_code != 200:
            raise RuntimeError("下载录音失败（HTTP %s）" % r.status_code)
        try:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(262144):
                    if chunk:
                        f.write(chunk)
        finally:
            r.close()
        if enc:
            try:
                with open(tmp, "rb") as f:
                    blob = f.read()
                blob = decrypt_media(enc, blob)
                with open(tmp, "wb") as f:
                    f.write(blob)
            except Exception as e:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                raise RuntimeError("录音解密失败：%s" % e)
        with open(tmp, "rb") as f:
            kind = _audio_kind(f.read(16))
        if kind in _AUDIO_OK:
            dst = base + "." + kind
            os.replace(tmp, dst)
            return dst
        ff = _ffmpeg_bin()
        if not ff:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise RuntimeError("录音是 %s 格式，浏览器放不了；没找到 ffmpeg 无法转码"
                               % (kind or "未知"))
        dst = base + ".m4a"
        pr = subprocess.run([ff, "-y", "-i", tmp, "-vn", "-c:a", "aac",
                             "-b:a", "96k", "-movflags", "+faststart", dst],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            timeout=1800, **_hidden_run_kw())
        err = (pr.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        try:
            os.remove(tmp)
        except Exception:
            pass
        if pr.returncode != 0 or not os.path.exists(dst):
            hint = err[-1][:160] if err else ""
            raise RuntimeError("录音转码失败（code=%s）%s"
                               % (pr.returncode, ("：" + hint) if hint else ""))
        return dst


def todo_toggle(tid, done):
    """勾选/取消一条代办。

    官方接口是 POST /todo/v1/user/records/{id}/update，必须带上一版 eTag；
    没带就会返回 uniqueKey 冲突、什么都不改。
    """
    tid = str(tid)
    rec = _todo_find_record(tid)
    if not rec:
        raise RuntimeError("找不到这条代办")
    ent = dict(rec["contentJson"]["entity"])
    ent["assets"] = []
    now = int(time.time() * 1000)
    ent["markFinishTime"] = now if done else 0
    ent["lastModifiedTime"] = now
    ent["is_finish"] = bool(done)
    res = api_post_form("/todo/v1/user/records/%s/update" % tid, {
        "record": json.dumps({"type": "entity", "contentJson": ent},
                             ensure_ascii=False),
        "previousETag": rec.get("eTag")})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "更新失败")
    TODO_RAW.pop(tid, None)
    _todo_invalidate()
    return {"ok": True, "done": bool(done)}


def todo_update(tid, title, subs=None):
    """修改一条代办的标题和子项。

    和 todo_toggle 一样必须带上一版 eTag，否则服务端认为冲突、什么都不改。
    subs 里每项是 {"text": ..., "done": bool}；已有子项的完成状态要保留，
    否则用户改个错别字就会把所有子项打回未完成。
    """
    tid = str(tid)
    title = (title or "").strip()
    if not title:
        raise RuntimeError("内容不能为空")
    subs = [{"text": str(s.get("text") or "").strip(), "done": bool(s.get("done"))}
            for s in (subs or []) if str(s.get("text") or "").strip()]

    rec = _todo_find_record(tid)
    if not rec:
        raise RuntimeError("找不到这条代办")

    if subs:
        content = json.dumps({"title": title, "isExpand": True,
                              "subTodoEntities": [{"content": s["text"],
                                                   "isFinish": s["done"]}
                                                  for s in subs]},
                             ensure_ascii=False)
        plain = "\n".join([title] + [s["text"] for s in subs])
        list_type = 1
    else:
        content = plain = title
        list_type = 0

    enc, info = encrypt_fields({"content": content, "plainText": plain})
    ent = dict(rec["contentJson"]["entity"])
    ent["assets"] = []
    ent.update({"content": enc["content"], "plainText": enc["plainText"],
                "listType": list_type, "encryptInfo": info,
                "lastModifiedTime": int(time.time() * 1000)})
    res = api_post_form("/todo/v1/user/records/%s/update" % tid, {
        "record": json.dumps({"type": "entity", "contentJson": ent},
                             ensure_ascii=False),
        "previousETag": rec.get("eTag")})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "保存失败")
    TODO_RAW.pop(tid, None)
    _todo_invalidate()
    return {"ok": True}


def todo_delete(tid):
    """删除一条代办：POST /todo/v1/user/records/{id}/delete + prevETag。"""
    tid = str(tid)
    rec = _todo_find_record(tid)
    if not rec:
        raise RuntimeError("找不到这条代办")
    res = api_post_form("/todo/v1/user/records/%s/delete" % tid,
                        {"prevETag": rec.get("eTag")})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "删除失败")
    TODO_RAW.pop(tid, None)
    _todo_invalidate()
    return {"ok": True}


def record_delete(rid):
    """录音移进回收站（permanent=false）。"""
    res = api_post_form("/sfs/ns/recorder/file/%s/delete" % rid,
                        {"permanent": "false"})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "删除失败")
    REC_CACHE.update({"at": 0})
    return {"ok": True}


def record_trash():
    res = api_get("/sfs/ns/recorder/deleted", offset=0, limit=200, _dc=1)
    out = []
    for it in (res.get("data") or {}).get("list") or []:
        raw = it.get("name") or ""
        clean, dur = record_clean_name(raw)
        out.append({"id": str(it.get("id")), "ver": str(it.get("ver") or ""),
                    "name": clean, "label": record_label(raw, clean),
                    "size": int(it.get("size") or 0),
                    "deleted": int(it.get("modify_time") or 0)})
    return {"items": out}


def record_recover(rid, ver):
    res = api_post_form("/sfs/ns/recorder/deleted/%s/%s/recover" % (rid, ver), {})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "恢复失败")
    REC_CACHE.update({"at": 0})
    return {"ok": True}


def record_purge(rid, ver):
    res = api_post_form("/sfs/ns/recorder/deleted/%s/%s/purge" % (rid, ver), {})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "彻底删除失败")
    REC_CACHE.update({"at": 0})
    return {"ok": True}


# ---------------------------------------------------------------- 云盘
DRIVE_IMG = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif",
             "tif", "tiff", "avif", "raw"}
DRIVE_VID = {"mp4", "mov", "mkv", "avi", "webm", "m4v", "wmv", "flv", "3gp"}
DRIVE_AUD = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "amr"}


def drive_kind(ext, typ=None):
    if typ == "folder":
        return "folder"
    if ext in DRIVE_IMG:
        return "image"
    if ext in DRIVE_VID:
        return "video"
    if ext in DRIVE_AUD:
        return "audio"
    if ext == "pdf":
        return "pdf"
    if ext in ("doc", "docx"):
        return "word"
    if ext in ("xls", "xlsx", "csv"):
        return "excel"
    if ext in ("ppt", "pptx"):
        return "ppt"
    if ext in ("zip", "rar", "7z", "tar", "gz", "bz2", "xz"):
        return "archive"
    if ext in ("txt", "md", "json", "xml", "ini", "log", "yaml", "yml"):
        return "text"
    if ext in ("py", "js", "ts", "html", "css", "java", "c", "cpp", "go",
               "rs", "sh", "bat", "ps1"):
        return "code"
    return "other"


def drive_page(parent_id=0, page_no=1, limit=100):
    res = api_get("/drive/v2/user/folders/children", parentId=parent_id,
                  pageNo=page_no, limit=limit, type="")
    d = res.get("data") or {}
    out = []
    for x in d.get("records") or []:
        name = x.get("name") or ""
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        th = x.get("thumbnailInfo") or {}
        out.append({
            "id": str(x.get("id")),
            "name": name,
            "type": x.get("type") or "doc",
            "isFolder": x.get("type") == "folder",
            "size": int(x.get("size") or 0),
            "date": int(x.get("modifyTime") or x.get("createTime") or 0),
            "ext": ext,
            "kind": drive_kind(ext, x.get("type")),
            "thumb": (th.get("data") or th.get("url")) if isinstance(th, dict) else None,
        })
    # 服务端给的就是新的在前；这里只把文件夹提到前面，组内保持原顺序
    out.sort(key=lambda x: not x["isFolder"])
    return {"items": out, "allCount": d.get("allCount"), "hasMore": d.get("hasMore")}


DRIVE_INFO = {}
DRIVE_LOCK = threading.Lock()
DRIVE_DIR = os.path.join(CACHE_DIR, "drive")


def drive_file_info(fid):
    fid = str(fid)
    with DRIVE_LOCK:
        if fid in DRIVE_INFO:
            return DRIVE_INFO[fid]
    res = api_get("/drive/v2/user/files/%s" % fid)
    d = res.get("data") or {}
    if not d:
        raise RuntimeError("找不到这个文件")
    with DRIVE_LOCK:
        DRIVE_INFO[fid] = d
    return d


def drive_file_bytes(fid):
    """下载 + 解密一个云盘文件，返回本地缓存路径。

    云盘和便签同一套加密：4MB 一块、每块 IV 在 recordIV 上按块号偏移，整体 AES-CTR；
    recordKey 用 micloud 命名空间的 appKey 解。下载回来的是密文，直接存会是乱码。
    """
    fid = str(fid)
    d = drive_file_info(fid)
    os.makedirs(DRIVE_DIR, exist_ok=True)
    p = os.path.join(DRIVE_DIR, re.sub(r"\W", "_", fid)[:80] + ".bin")
    want = (d.get("sha1") or "").lower()
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                data = f.read()
            if not want or hashlib.sha1(data).hexdigest() == want:
                return p
        except Exception:
            pass
    u = (d.get("storage") or {}).get("downloadUrl")
    if not u:
        raise RuntimeError("这个文件没有下载地址")
    blob = raw_get(u, timeout=300).content
    enc = d.get("encryptInfo") or {}
    if enc:
        rk = unlock_note_record_key(enc)
        iv = b64u(enc.get("recordIV"))
        out = bytearray()
        for i in range(0, len(blob), UP_BLOCK):
            out += aes_ctr(rk, calc_block_iv(iv, i // UP_BLOCK), blob[i:i + UP_BLOCK])
        blob = bytes(out)
    if want and hashlib.sha1(blob).hexdigest() != want:
        raise RuntimeError("解密校验失败（sha1 不一致）")
    with open(p, "wb") as f:
        f.write(blob)
    return p


def drive_open(fid):
    """双击非图片/视频文件：下载解密后复制到「下载」目录，用系统默认程序打开。"""
    d = drive_file_info(fid)
    p = drive_file_bytes(fid)
    name = sanitize(d.get("name") or os.path.basename(p), "file.bin")
    os.makedirs(HOME_DL, exist_ok=True)
    dst = os.path.join(HOME_DL, name)
    base, ext = os.path.splitext(dst)
    n = 1
    while os.path.exists(dst):
        dst = "%s (%d)%s" % (base, n, ext)
        n += 1
    shutil.copyfile(p, dst)
    os.startfile(dst)
    return {"ok": True, "path": dst}


# ---------------------------------------------------------------- 上传
# 官方客户端的分块规则：每块 4MB，每块用独立 IV 做 AES-CTR；
# 第 i 块的 IV = 把 recordIV 当 128 位大端整数，从倒数第 3 字节起加 4*i 并向前进位。
# 相册用 gallery 命名空间的 appKey，云盘用 micloud（便签）那一套。
UP_BLOCK = 4 * 1024 * 1024
GAL_EXT = {"png", "jpg", "bmp", "jpeg", "gif", "jpe", "raw",
           "mov", "mp4", "heic", "heif", "avci"}
GAL_VIDEO_EXT = {"mov", "mp4"}
MIME_FIX = {".heic": "image/heic", ".heif": "image/heif",
            ".avci": "image/avci", ".raw": "image/x-raw"}

UP_JOBS = {}
UP_SEQ = [0]
UP_TMP = os.path.join(CACHE_DIR, "up_tmp")


def calc_block_iv(iv, idx):
    """官方 calcBlockIv：从倒数第 3 字节开始加 4*idx，带进位。"""
    out = bytearray(iv)
    flag = 4 * idx
    for i in range(len(out) - 3, -1, -1):
        v = out[i] + flag
        out[i] = v % 256
        if v < 256:
            break
        flag = v // 256
    return bytes(out)


def mime_of(path):
    ext = os.path.splitext(path)[1].lower()
    return (MIME_FIX.get(ext) or mimetypes.guess_type(path)[0]
            or "application/octet-stream")


def _pick_appkey(bag):
    vers = [int(k) for k in bag if str(k).isdigit() and int(k) > 1]
    ver = max(vers) if vers else int(next(iter(bag)))
    return ver, bag[str(ver)]


def _prep_encrypted(path, bag):
    """读文件 -> 分块加密，返回 encryptInfo / 各块密文与校验值 / 整文件 sha1。"""
    ver, ak = _pick_appkey(bag)
    rk, iv = os.urandom(32), os.urandom(16)
    wrapped = AESGCM(ak).encrypt(iv, rk, None) + struct.pack(">I", zlib.crc32(rk))
    info = {"appKeyVersion": ver, "recordIV": _b64u_nopad(iv),
            "encryptedRecordKey": _b64u_nopad(wrapped)}
    h_all, h_enc, blocks = hashlib.sha1(), hashlib.sha1(), []
    with open(path, "rb") as f:
        idx = 0
        while True:
            raw = f.read(UP_BLOCK)
            if not raw:
                break
            h_all.update(raw)
            enc = aes_ctr(rk, calc_block_iv(iv, idx), raw)
            h_enc.update(enc)
            blocks.append({"enc": enc, "size": len(raw),
                           "sha1": hashlib.sha1(raw).hexdigest(),
                           "md5": hashlib.md5(raw).hexdigest(),
                           "encryptedSha1": hashlib.sha1(enc).hexdigest()})
            idx += 1
    info["encryptedSha1"] = h_enc.hexdigest()
    return {"info": info, "blocks": blocks, "sha1": h_all.hexdigest(),
            "size": os.path.getsize(path)}


THUMB_SIZES = (270, 1080)          # 小图 / 大图，官方 SDK 固定这两档
THUMB_QUALITY = 0.70               # 官方 compressInt=70
_UID = [0]


def _thumb_record_iv(image_sha1, size):
    """缩略图的 recordIV = sha1(原图sha1 + 尺寸) 取前 16 字节，base64url 无填充。
    官方 thumbnail.js：sha1(imageSha1 + size) -> hex -> 前16字节 -> base64。"""
    h = hashlib.sha1((image_sha1 + str(size)).encode("utf-8")).hexdigest()
    return _b64u_nopad(bytes.fromhex(h)[:16])


def _make_thumb(path, size_info):
    """按服务端给的 width/height/compressInt 生成缩略图（JPEG），返回字节。"""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    w = int((size_info or {}).get("width") or 0)
    h = int((size_info or {}).get("height") or 0)
    ci = int((size_info or {}).get("compressInt") or 70)
    q = 0.7 if (ci < 60 or ci > 95) else ci / 100.0
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if w and h:
            im = ImageOps.fit(im, (w, h), method=Image.LANCZOS, centering=(0.5, 0.5))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=int(round(q * 100)), optimize=True)
    return buf.getvalue()


def _thumb_pack(raw, image_sha1, image_info, size):
    """按官方 getThumbHash：沿用原图 recordKey，IV 换成缩略图自己的 IV。
    返回 {blob, sha1, md5, size, encryptInfo, encryptedSha1}。"""
    rk = unlock_record_key(image_info)            # 原图的 32 字节 recordKey
    riv = b64u(_thumb_record_iv(image_sha1, size))
    enc = aes_ctr(rk, riv, raw)                   # AES-CTR，单块，不切分
    wrapped = (AESGCM(APPKEYS[str(image_info.get("appKeyVersion"))])
               .encrypt(riv, rk, None) + struct.pack(">I", zlib.crc32(rk)))
    return {
        "blob": enc, "raw": raw,
        "sha1": hashlib.sha1(raw).hexdigest(),
        "md5": hashlib.md5(raw).hexdigest(),
        "size": len(raw),
        "encryptedSha1": hashlib.sha1(enc).hexdigest(),
        "encryptInfo": {"appKeyVersion": image_info.get("appKeyVersion"),
                        "recordIV": _b64u_nopad(riv),
                        "encryptedSha1": hashlib.sha1(enc).hexdigest(),
                        "encryptedRecordKey": _b64u_nopad(wrapped)},
    }


def _upload_thumbs(fid, path, image_sha1, image_info, thumb_status, job=None):
    """上传 270/1080 两档缩略图。服务端 thumbStatus 说 custom 才需要自己传。"""
    st = thumb_status or {}
    if not st.get("isClientUploadThumb"):
        return
    todo = []
    for size, info_key, status_key in ((270, "smallSizeInfo", "smallStatus"),
                                       (1080, "largeSizeInfo", "largeStatus")):
        status = st.get(status_key)
        if status in ("custom", "useOriginal"):
            continue
        info = st.get(info_key) or {}
        try:
            raw = _make_thumb(path, info)
        except Exception as e:
            print("[缩略图] %d 档生成失败：%s" % (size, e))
            continue
        todo.append((size, _thumb_pack(raw, image_sha1, image_info, size)))
    if not todo:
        return
    if job is not None:
        job["phase"] = "上传缩略图"

    uid = str(_user_id() or "")
    infos = [{"requestThumbMeta": {"resolution": size, "sha1": t["sha1"],
                                   "md5": t["md5"], "size": t["size"]},
              "encryptInfo": t["encryptInfo"]} for size, t in todo]
    res = api_post_form("/gallery/user/full/%s/create/thumbnail" % fid,
                        {"creatorId": uid, "retry": "0",
                         "thumbnailInfos": json.dumps(infos)})
    if res.get("result") != "ok":
        raise RuntimeError("创建缩略图上传失败：%s"
                           % (res.get("reason") or res.get("description")))
    kss = (res.get("data") or {}).get("kss") or res.get("kss") or {}
    urls = kss.get("urls") or []
    if not urls:
        return
    small, large = kss.get("small") or {}, kss.get("large") or {}
    uploads, metas = [], []
    for size, t in todo:
        k = small if size == 270 else large
        if not k or k.get("is_existed"):
            continue
        uploads.append((k, t))
    if not uploads:
        return
    results = []
    for k, t in uploads:
        r = requests.post("%s/upload_thumbnail?meta=%s" % (urls[0], k.get("meta")),
                          data=t["blob"], timeout=180,
                          headers={"Content-Type": "application/octet-stream"})
        results.append(r.json())
    for idx, (k, t) in enumerate(uploads):
        metas.append({"kssCommit": results[idx], "uploadId": k.get("uploadId"),
                      "encryptInfo": t["encryptInfo"]})
    r2 = api_post_form("/gallery/user/full/%s/commit/thumbnail" % fid,
                       {"creatorId": uid, "retry": "0",
                        "commitMetas": json.dumps(metas)})
    if r2.get("result") != "ok":
        raise RuntimeError("提交缩略图失败：%s"
                           % (r2.get("reason") or r2.get("description")))


def _user_id():
    """相册缩略图接口要 creatorId；本机 cookie 库里读一次并缓存。"""
    try:
        if _UID[0]:
            return _UID[0]
        kv = _my_cookies_map()
        if kv and str(kv.get("userId", "")).isdigit():
            _UID[0] = int(kv["userId"])
    except Exception:
        pass
    return _UID[0]


def _upload_blocks(kss, blocks, job=None):
    """逐块 POST 到 node_urls[0]，返回 commit_metas。已存在的块直接复用。"""
    node = (kss.get("node_urls") or [""])[0]
    metas = kss.get("block_metas") or []
    commits = []
    for i, bm in enumerate(metas):
        if bm.get("is_existed") == 1 and bm.get("commit_meta"):
            commits.append({"commit_meta": bm["commit_meta"]})
            continue
        url = "%s/upload_block_chunk?chunk_pos=0&&file_meta=%s&block_meta=%s" % (
            node, kss.get("file_meta"), bm.get("block_meta"))
        last = None
        for attempt in range(3):
            try:
                r = requests.post(url, data=blocks[i]["enc"], timeout=600,
                                  headers={"Content-Type": "application/octet-stream"})
                j = r.json()
                if j.get("commit_meta"):
                    commits.append({"commit_meta": j["commit_meta"]})
                    last = None
                    break
                last = RuntimeError("第 %d 块上传失败：%s"
                                    % (i + 1, j.get("reason") or j.get("description")))
            except Exception as e:
                last = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        if last:
            raise last
        if job is not None:
            job["bytes"] = min(job["total"], job["bytes"] + blocks[i]["size"])
            job["phase"] = "上传 %d/%d 块" % (i + 1, len(metas))
    return commits


def _image_size(path):
    """取图片宽高（相册加密上传的 exifInfo 需要），取不到就返回 None。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def gallery_upload(path, album_id, job=None, shown_name=None):
    """上传一张图片/一段视频到相册（端到端加密）。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext not in GAL_EXT:
        raise RuntimeError("相册不支持 .%s 格式" % ext)
    is_video = ext in GAL_VIDEO_EXT
    size = os.path.getsize(path)
    if not is_video and size > 100 * 1024 * 1024:
        raise RuntimeError("单张图片不能超过 100MB")
    if size > 4 * 1024 * 1024 * 1024:
        raise RuntimeError("单个文件不能超过 4GB")
    gid = str(album_id or "1")
    if gid == "1000" and is_video:
        raise RuntimeError("私密相册不支持上传视频")

    st = os.stat(path)
    name = sanitize(shown_name, "upload.bin") if shown_name else os.path.basename(path)
    title = os.path.splitext(name)[0]
    mime = mime_of(name)
    if job is not None:
        job["phase"] = "加密中"
        job["total"] = size
        job["bytes"] = 0

    pack = _prep_encrypted(path, APPKEYS) if APPKEYS else None
    content = {"type": "video" if is_video else "image", "groupId": gid,
               "mimeType": mime, "fileName": name, "title": title,
               "sha1": pack["sha1"] if pack else None, "size": size,
               "dateTaken": int(st.st_mtime * 1000),
               "dateModified": int(st.st_mtime * 1000)}
    if pack:
        wh = None if is_video else _image_size(path)
        if wh:
            content["exifInfo"] = {"imageWidth": wh[0], "imageLength": wh[1],
                                   "orientation": 1}
        form = {"data": json.dumps({
            "content": content,
            "block_infos": [{k: b[k] for k in
                             ("sha1", "md5", "encryptedSha1", "size")}
                            for b in pack["blocks"]]}),
            "withThumbnail": "false",
            "encryptInfo": json.dumps(pack["info"]),
            "isClientUploadThumbnail": "true"}
    else:
        # 没有 appKey（没开加密的老账号）：让服务端自己生成缩略图
        content.pop("sha1")
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(UP_BLOCK), b""):
                h.update(chunk)
        content["sha1"] = h.hexdigest()
        form = {"data": json.dumps({"content": content, "block_infos": []}),
                "withThumbnail": "true"}

    res = api_post_form("/gallery/user/full", form)
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "创建上传失败")
    d = res.get("data") or {}
    kss = d.get("kss") or {}
    fid = str((d.get("content") or {}).get("id") or "")
    if not fid:
        raise RuntimeError("服务端没有返回文件 id")

    # 同一张图已经在云端（sha1 相同）时，服务端只回 fileExists、不给 kss，
    # 这时不需要传分块也不用提交，直接复用已有那条记录（官方客户端同样处理）。
    if d.get("fileExists") or not kss:
        if pack and not is_video:
            try:
                _upload_thumbs(fid, path, pack["sha1"], pack["info"],
                               (d.get("content") or {}).get("thumbStatus"), job)
            except Exception as e:
                print("[缩略图] 上传失败：%s" % e)
        if job is not None:
            job["phase"] = "已存在，直接复用"
        return fid

    if pack:
        if job is not None:
            job["phase"] = "上传分块"
        commits = _upload_blocks(kss, pack["blocks"], job)
    else:
        # 明文：逐块读原文件直接上传
        blocks = []
        with open(path, "rb") as f:
            while True:
                raw = f.read(UP_BLOCK)
                if not raw:
                    break
                blocks.append({"enc": raw, "size": len(raw)})
        commits = _upload_blocks(kss, blocks, job)

    kss = dict(kss)
    kss["commit_metas"] = commits
    r2 = api_post_form("/gallery/user/full/%s/storage" % fid,
                       {"data": json.dumps({"kss": kss})})
    if r2.get("result") != "ok":
        raise RuntimeError(r2.get("reason") or r2.get("description") or "提交上传失败")

    # 缩略图：加密相册必须由客户端算好再传，否则服务端只给一张空白占位，
    # 复制/移动到别的相簿还会被拒绝（50096 缩略图上传未完成）。
    if pack and not is_video:
        try:
            _upload_thumbs(fid, path, pack["sha1"], pack["info"],
                           (d.get("content") or {}).get("thumbStatus"), job)
        except Exception as e:
            # 缩略图失败不该让整次上传算失败，照片本身已经在云端了
            print("[缩略图] 上传失败：%s" % e)
    return fid


def drive_upload(path, parent_id, job=None, shown_name=None):
    """上传任意文件到云盘（端到端加密）。"""
    size = os.path.getsize(path)
    if size > 4 * 1024 * 1024 * 1024:
        raise RuntimeError("单个文件不能超过 4GB")
    st = os.stat(path)
    name = sanitize(shown_name, "upload.bin") if shown_name else os.path.basename(path)
    if job is not None:
        job["phase"] = "加密中"
        job["total"] = size
        job["bytes"] = 0

    pack = _prep_encrypted(path, NOTE_APPKEYS or APPKEYS)
    content = {"fileName": name, "size": size, "sha1": pack["sha1"],
               "mimeType": mime_of(path), "channel": "PC",
               "parentId": str(parent_id or "0"), "recordId": "0",
               "localCreateTime": int(st.st_ctime * 1000),
               "localModifyTime": int(st.st_mtime * 1000)}
    kss_in = {"block_infos": [{k: b[k] for k in
                               ("sha1", "md5", "encryptedSha1", "size")}
                              for b in pack["blocks"]]}
    res = api_post_form("/drive/v2/user/files/create", {
        "data": json.dumps(content), "kss": json.dumps(kss_in),
        "recordId": "0", "encryptInfo": json.dumps(pack["info"]),
        "isClientUploadThumbnail": "false"})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "创建上传失败")
    d = res.get("data") or {}
    kss = d.get("kss") or {}
    if job is not None:
        job["phase"] = "上传分块"
    commits = _upload_blocks(kss, pack["blocks"], job)
    r2 = api_post_form("/drive/v2/user/files/commit", {
        "uploadId": d.get("uploadId"), "file_meta": kss.get("file_meta"),
        "commit_metas": json.dumps(commits), "parentId": str(parent_id or "0"),
        "encryptInfo": json.dumps(pack["info"]),
        "isClientUploadThumbnail": "false"})
    if r2.get("result") != "ok":
        raise RuntimeError(r2.get("reason") or r2.get("description") or "提交上传失败")
    return str((r2.get("data") or {}).get("id") or "")


def _upload_worker(jid):
    job = UP_JOBS[jid]
    for it in job["items"]:
        if job["state"] == "cancel":
            break
        it["state"] = "running"
        job["current"] = it["name"]
        job["phase"] = "准备中"
        job["bytes"] = 0
        job["total"] = it["size"]
        try:
            if job["kind"] == "gallery":
                gallery_upload(it["path"], job["target"], job, it["name"])
            else:
                drive_upload(it["path"], job["target"], job, it["name"])
            it["state"] = "done"
            job["phase"] = "完成"
        except NeedLogin:
            it["state"] = "error"
            it["error"] = "登录已过期"
            job["state"] = "login"
            return
        except Exception as e:
            it["state"] = "error"
            it["error"] = str(e)
            job["phase"] = "失败"
        finally:
            # 从浏览器收来的临时文件，上传完（成功或失败）就删掉
            if os.path.dirname(it["path"]) == UP_TMP:
                try:
                    os.unlink(it["path"])
                except OSError:
                    pass
        job["done"] += 1
    job["state"] = "done" if job["state"] != "cancel" else "cancel"
    job["current"] = ""


def upload_start(kind, paths, target, names=None):
    """names：显示用文件名。从浏览器收来的临时文件带随机前缀，
    真实文件名要单独传进来，否则相册里会出现 0184e01a_xxx.png 这种名字。"""
    names = list(names or [])
    files = []
    for i, p in enumerate(paths):
        p = str(p or "")
        if not p or not os.path.isfile(p):
            continue
        shown = names[i] if i < len(names) and names[i] else os.path.basename(p)
        files.append({"path": p, "name": shown,
                      "size": os.path.getsize(p), "state": "wait", "error": ""})
    if not files:
        raise RuntimeError("没有可上传的文件")
    UP_SEQ[0] += 1
    jid = "up%d" % UP_SEQ[0]
    UP_JOBS[jid] = {"id": jid, "kind": kind, "target": str(target or ""),
                    "items": files, "done": 0, "total": len(files),
                    "bytes": 0, "phase": "准备中",
                    "current": "", "state": "running"}
    threading.Thread(target=_upload_worker, args=(jid,), daemon=True).start()
    return {"job": jid}


def upload_receive(kind, target, name, stream, length):
    """浏览器把文件字节直接 POST 过来，先落临时文件，再交给上传线程。

    WebView2 里的 <input type=file> / 拖拽只给 File 对象、拿不到真实路径，
    所以走这条路：浏览器流式发送，服务端边收边写盘，不在内存里堆整份文件。
    """
    name = sanitize(name, "upload.bin")
    os.makedirs(UP_TMP, exist_ok=True)
    tmp = os.path.join(UP_TMP, "%s_%s" % (uuid.uuid4().hex[:8], name))
    left = int(length or 0)
    with open(tmp, "wb") as f:
        while left > 0:
            chunk = stream.read(min(262144, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if os.path.getsize(tmp) == 0:
        os.unlink(tmp)
        raise RuntimeError("收到的文件是空的")
    return upload_start(kind, [tmp], target, names=[name])


def upload_progress(jid=None):
    jobs = UP_JOBS if not jid else ({jid: UP_JOBS[jid]} if jid in UP_JOBS else {})
    out = []
    for j in jobs.values():
        cur = next((x for x in j["items"] if x["state"] == "running"), None)
        out.append({
            "id": j["id"], "kind": j["kind"], "state": j["state"],
            "done": j["done"], "total": j["total"],
            "phase": j["phase"], "current": j["current"],
            "fileBytes": j["bytes"], "fileTotal": cur["size"] if cur else 0,
            "items": [{"name": x["name"], "state": x["state"],
                       "error": x["error"], "size": x["size"]}
                      for x in j["items"]],
        })
    return {"jobs": out}


# ---------------------------------------------------------------- 相册管理
def album_create(name):
    name = (name or "").strip()
    if not name:
        raise RuntimeError("相簿名不能为空")
    res = api_post_form("/gallery/album/full", {"fileName": name})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "新建相簿失败")
    c = (res.get("data") or {}).get("content") or {}
    return {"id": str(c.get("id") or ""), "name": c.get("fileName") or name}


def album_rename(album_id, name):
    name = (name or "").strip()
    if not name:
        raise RuntimeError("相簿名不能为空")
    content = {"fileName": name, "id": str(album_id), "type": "group",
               "status": "custom", "dateModified": int(time.time() * 1000)}
    res = api_post_form("/gallery/album/put", {
        "content": json.dumps(content, ensure_ascii=False),
        "ckey": str(int(time.time() * 1000))})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "改名失败")
    return {"ok": True}


def album_delete(album_id):
    res = api_post_form("/gallery/album/full/%s/delete" % album_id, {"tag": "0"})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "删除相簿失败")
    return {"ok": True}


# ── 删除相簿（含里面的照片）─────────────────────────────────────────────
# 服务端不允许直接删非空相簿（返回 "Can't delete non-empty album"）。
# 正确做法：先把相簿里的照片逐张移进回收站，再删空相簿。
# 大相簿几百张，一次请求做不完，所以放后台线程跑，前端轮询进度。
ALBUM_DEL_JOBS = {}
ALBUM_DEL_SEQ = [0]
ALBUM_DEL_LOCK = threading.Lock()


def start_album_delete(group_id):
    ids = [x for x in str(group_id or "").split(",") if x]
    if not ids:
        raise RuntimeError("相簿 id 为空")
    with ALBUM_DEL_LOCK:
        ALBUM_DEL_SEQ[0] += 1
        jid = "adel%d" % ALBUM_DEL_SEQ[0]
        job = {"id": jid, "state": "running", "total": 0, "done": 0,
               "deleted": 0, "failed": 0, "albums": len(ids),
               "albums_deleted": 0, "err": None}
        ALBUM_DEL_JOBS[jid] = job
    threading.Thread(target=_album_delete_worker, args=(jid, ids), daemon=True).start()
    return jid


def _album_delete_worker(jid, ids):
    job = ALBUM_DEL_JOBS[jid]
    old_bulk = BULK_SCAN[0]
    BULK_SCAN[0] = True          # 批量删时不每张都落盘（几百次 fsync 会拖到几分钟）
    try:
        all_items = []
        for aid in ids:
            page = 0
            while page < 200:
                d = gallery_page(aid, page, 200)
                batch = d.get("items") or []
                all_items.extend(batch)
                if d.get("lastPage") or not batch:
                    break
                page += 1
        seen, items = set(), []
        for it in all_items:
            k = str(it.get("id"))
            if k in seen:
                continue
            seen.add(k)
            items.append(it)
        job["total"] = len(items)
        for i, it in enumerate(items, 1):
            try:
                res = api_post_form("/gallery/info/delete", {"id": str(it.get("id"))})
                if res.get("result") == "ok":
                    job["deleted"] += 1
                else:
                    job["failed"] += 1
            except Exception:
                job["failed"] += 1
            job["done"] = i
            if i % 20 == 0:
                try:
                    save_session(S, merge=True)
                except Exception:
                    pass
        ok = 0
        for aid in ids:
            try:
                res = api_post_form("/gallery/album/full/%s/delete" % aid, {"tag": "0"})
                if res.get("result") == "ok":
                    ok += 1
            except Exception:
                pass
        job["albums_deleted"] = ok
        job["state"] = "done"
    except Exception as e:
        job["state"] = "error"
        job["err"] = str(e)
    finally:
        BULK_SCAN[0] = old_bulk
        try:
            save_session(S, merge=True)
        except Exception:
            pass


def photo_delete(ids):
    """移进回收站（不是彻底删）。"""
    done, fail = [], []
    for i in ids:
        try:
            res = api_post_form("/gallery/info/delete", {"id": str(i)})
            (done if res.get("result") == "ok" else fail).append(str(i))
        except Exception:
            fail.append(str(i))
    return {"deleted": done, "failed": fail}


def photo_move(ids, album_id):
    done, fail = [], []
    for i in ids:
        try:
            res = api_post_form("/gallery/image/%s/move" % i,
                                {"tag": "1", "albumId": str(album_id)})
            (done if res.get("result") == "ok" else fail).append(str(i))
        except Exception:
            fail.append(str(i))
    return {"moved": done, "failed": fail}


def photo_copy(ids, album_id):
    done, fail = [], []
    for i in ids:
        try:
            res = api_post_form("/gallery/full/%s/copy" % i,
                                {"tag": "1", "toAlbum": str(album_id)})
            (done if res.get("result") == "ok" else fail).append(str(i))
        except Exception:
            fail.append(str(i))
    return {"copied": done, "failed": fail}


def trash_list(limit=200, max_tag=0):
    res = api_get("/gallery/deleted_v2", maxTag=max_tag, limit=int(limit))
    d = res.get("data") or {}
    out = []
    for x in d.get("content") or []:
        mid = str(x.get("id"))
        remember_meta(x, mid)
        tei = x.get("thumbEncryptInfo") or {}
        out.append({
            "id": mid, "tag": str(x.get("tag") or ""),
            "name": x.get("fileName") or x.get("title") or "",
            "type": x.get("type") or "image",
            "size": int(x.get("size") or 0),
            "deleted": int(x.get("deleteTime") or 0),
            "thumb": pick_thumb(x),
            "thumbUrl": ("/api/thumb?id=%s&size=small" % mid) if tei and APPKEYS else pick_thumb(x),
            "bigUrl": ("/api/thumb?id=%s&size=large" % mid) if tei and APPKEYS else pick_thumb(x, big=True),
        })
    return {"items": out, "lastPage": bool(d.get("lastPage")),
            "nextTag": (out[-1]["tag"] if out else 0)}


def trash_recover(ids, tags=None):
    """恢复回收站里的照片：id + tag 一起提交。tags 由前端从当前列表带过来。"""
    pairs = {str(k): str(v) for k, v in (tags or {}).items() if v}
    if not pairs:
        res = api_get("/gallery/deleted_v2", maxTag=0, limit=200)
        for x in (res.get("data") or {}).get("content") or []:
            pairs[str(x.get("id"))] = str(x.get("tag") or "")
    done, fail = [], []
    for i in ids:
        tag = pairs.get(str(i))
        if not tag:
            fail.append(str(i))
            continue
        try:
            r = api_post_form("/gallery/info/recovery", {"id": str(i), "tag": tag})
            (done if r.get("result") == "ok" else fail).append(str(i))
        except Exception:
            fail.append(str(i))
    return {"recovered": done, "failed": fail}


def trash_purge(ids, tags=None):
    pairs = {str(k): str(v) for k, v in (tags or {}).items() if v}
    if not pairs:
        res = api_get("/gallery/deleted_v2", maxTag=0, limit=200)
        for x in (res.get("data") or {}).get("content") or []:
            pairs[str(x.get("id"))] = str(x.get("tag") or "")
    done, fail = [], []
    for i in ids:
        tag = pairs.get(str(i))
        if not tag:
            fail.append(str(i))
            continue
        try:
            r = api_post_form("/gallery/info/purge", {"id": str(i), "ckey": tag})
            (done if r.get("result") == "ok" else fail).append(str(i))
        except Exception:
            fail.append(str(i))
    return {"purged": done, "failed": fail}


# ---------------------------------------------------------------- 云盘管理
def drive_mkdir(name, parent_id="0"):
    name = (name or "").strip()
    if not name:
        raise RuntimeError("文件夹名不能为空")
    res = api_post_form("/drive/v2/user/folders/create",
                        {"name": name, "parentId": str(parent_id or "0")})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "新建文件夹失败")
    return {"id": str((res.get("data") or {}).get("id") or "")}


def drive_rename(fid, name):
    name = (name or "").strip()
    if not name:
        raise RuntimeError("名字不能为空")
    res = api_post_form("/drive/v2/user/records/rename",
                        {"id": str(fid), "newName": name})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "改名失败")
    return {"ok": True}


def drive_delete(fid, is_folder=False, permanent=False):
    if is_folder:
        res = api_post_form("/drive/user/folders/%s/delete" % fid, {})
    else:
        res = api_post_form("/drive/user/files/%s/delete" % fid,
                            {"data": json.dumps({"content":
                                                 {"permanent": bool(permanent)}})})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "删除失败")
    return {"ok": True}


def drive_move(fid, parent_id, name=None, is_folder=False):
    content = {"parentId": str(parent_id or "0")}
    if name:
        content["name"] = name
    path = ("/drive/user/folders/%s/set_meta" % fid if is_folder
            else "/drive/user/files/%s/set_meta" % fid)
    res = api_post_form(path, {"data": json.dumps({"content": content})})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "移动失败")
    return {"ok": True}


def drive_copy(fid, parent_id, name):
    res = api_post_form("/drive/user/files/%s/copy" % fid,
                        {"data": json.dumps({"content":
                                             {"name": name,
                                              "parentId": str(parent_id or "0")}})})
    if res.get("result") != "ok":
        raise RuntimeError(res.get("reason") or res.get("description") or "复制失败")
    return {"ok": True}


# ---------------------------------------------------------------- 下载
JOBS = {}
JOB_SEQ = [0]


def start_job(kind, items, outdir):
    JOB_SEQ[0] += 1
    jid = "job%d" % JOB_SEQ[0]
    JOBS[jid] = {"id": jid, "kind": kind, "total": len(items), "done": 0,
                 "current": "", "errors": [], "state": "running", "dir": outdir}
    t = threading.Thread(target=_job_worker, args=(jid, kind, items, outdir), daemon=True)
    t.start()
    return jid


def _save(path, data):
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def _job_worker(jid, kind, items, outdir):
    job = JOBS[jid]
    os.makedirs(outdir, exist_ok=True)
    for it in items:
        name = sanitize(it.get("name"), it.get("id", "file"))
        job["current"] = name
        try:
            if kind in ("image", "video"):
                path_ = ("/gallery/image/storage" if kind == "image"
                         else "/gallery/video/storage")
                res = api_get(path_, id=it["id"])
                dat = res.get("data") or {}
                kss = dat.get("kss") or {}
                blocks = kss.get("blocks") or []
                info = dat.get("encryptInfo")
                buf = b""
                for b in blocks:
                    urls = b.get("urls") or []
                    if urls:
                        buf += raw_get(urls[0].replace("http://", "https://"),
                                       timeout=300).content
                if info and APPKEYS:
                    data = decrypt_media(info, buf)      # 端到端加密：AES-CTR
                elif kss.get("secure_key"):
                    data = decrypt_blocks(blocks, kss.get("secure_key"))
                else:
                    raise RuntimeError("这条没有可用的密钥，无法解密")
                if not os.path.splitext(name)[1]:
                    name += ".jpg" if kind == "image" else ".mp4"
            elif kind == "record":
                # 录音是加密存的：下载也走解密链路，别把密文当 mp3 存下来
                p = record_audio_file(it["id"])
                with open(p, "rb") as f:
                    data = f.read()
                name = os.path.splitext(name)[0] + (os.path.splitext(p)[1] or ".mp3")
            elif kind == "drive":
                # 云盘文件是加密存的：先走解密缓存，别把密文存成下载文件
                d = drive_file_info(it["id"])
                p = drive_file_bytes(it["id"])
                with open(p, "rb") as f:
                    data = f.read()
                name = sanitize(d.get("name") or name, name)
            else:
                raise RuntimeError("未知类型 %s" % kind)
            path = os.path.join(outdir, name)
            base, ext = os.path.splitext(path)
            n = 1
            while os.path.exists(path):
                path = "%s(%d)%s" % (base, n, ext)
                n += 1
            _save(path, data)
        except NeedLogin:
            job["errors"].append("%s：登录已过期" % name)
            job["state"] = "login"
            return
        except Exception as e:
            job["errors"].append("%s：%s" % (name, e))
        job["done"] += 1
    job["state"] = "done"
    job["current"] = ""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoMiYunViewer/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _send_file(self, path, ctype):
        """带 Range 支持地发本地文件：<video> 才能拖动进度条。"""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            try:
                a, _, b = rng[6:].partition("-")
                if a:
                    start = int(a)
                if b:
                    end = min(size - 1, int(b))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                code = 206
            except Exception:
                start, end, code = 0, size - 1, 200
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "max-age=3600")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                p = os.path.join(UI_DIR, "index.html")
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            # 静态资源（vendor 里的开源库、图标等）：不鉴权，直接取自 ui 目录
            if u.path.startswith("/vendor/") or u.path.rsplit(".", 1)[-1] in (
                    "css", "js", "svg", "png", "woff2", "map"):
                rel = u.path.lstrip("/").replace("..", "")
                p = os.path.join(UI_DIR, rel)
                if os.path.isfile(p):
                    ext = rel.rsplit(".", 1)[-1].lower()
                    ct = {"css": "text/css; charset=utf-8",
                          "js": "text/javascript; charset=utf-8",
                          "svg": "image/svg+xml", "png": "image/png",
                          "woff2": "font/woff2"}.get(ext, "application/octet-stream")
                    return self._send_file(p, ct)
                return self._send(404, b"", "text/plain")
            if u.path == "/api/status":
                return self._json({"login": AUTH["ok"], "qr": QR["state"],
                                   "err": QR["err"],
                                   "login_state": LOGIN["state"],
                                   "step": LOGIN["step"],
                                   "detail": LOGIN["detail"],
                                   "pct": LOGIN["pct"],
                                   "elapsed": round(time.time() - LOGIN["since"], 1)})
            if u.path == "/api/login_state":
                return self._json({"state": QR["state"], "img": QR["img"],
                                   "err": QR["err"]})
            if u.path == "/api/cache_stats":
                # 缓存占用明细：给侧栏那个「缓存 xx MB」入口用
                st = cache_gc.stats(CACHE_DIR)
                lim = cache_limit_bytes()
                cache_gc.CACHE_LIMIT = lim
                cache_gc.CACHE_LOW = int(lim * 0.8)
                try:
                    free = shutil.disk_usage(CACHE_DIR).free
                except Exception:
                    free = 0
                return self._json({"total": st["total"], "count": st["count"],
                                   "limit": lim, "parts": st["parts"],
                                   "dir": CACHE_DIR, "default_dir": DEFAULT_CACHE_DIR,
                                   "custom_dir": SETTINGS.get("cache_dir") or "",
                                   "auto": bool(SETTINGS.get("cache_auto")),
                                   "limit_setting": int(SETTINGS.get("cache_limit")
                                                        or DEFAULT_CACHE_LIMIT),
                                   "free": free})
            if u.path == "/api/pick_dir":
                return self._json(pick_dir())
            if u.path == "/api/settings":
                try:
                    free = shutil.disk_usage(CACHE_DIR).free
                except Exception:
                    free = 0
                return self._json({"cache_dir": CACHE_DIR,
                                   "default_dir": DEFAULT_CACHE_DIR,
                                   "custom_dir": SETTINGS.get("cache_dir") or "",
                                   "auto": bool(SETTINGS.get("cache_auto")),
                                   "limit": cache_limit_bytes(),
                                   "limit_setting": int(SETTINGS.get("cache_limit")
                                                        or DEFAULT_CACHE_LIMIT),
                                   "free": free})
            # 取票还没完成时，其它接口一律回 401，让界面停在登录页显示进度
            if not AUTH["ok"] and u.path.startswith("/api/"):
                return self._json({"error": "login"}, 401)
            if u.path == "/api/account":
                return self._json(account_info())
            if u.path == "/api/albums":
                return self._json({"albums": list_albums()})
            if u.path == "/api/album_layout":
                return self._json(album_layout_get())
            if u.path == "/api/album_delete_state":
                return self._json(ALBUM_DEL_JOBS.get(q.get("job")) or {})
            if u.path == "/api/items":
                return self._json(gallery_page(q.get("album", "0"),
                                               int(q.get("page", 0)),
                                               int(q.get("limit", 60)),
                                               q.get("sd"), q.get("ed"),
                                               is_video=(q.get("video") == "1")))
            if u.path == "/api/gallery_months":
                return self._json(gallery_timeline(q.get("album", "0"),
                                                   video=(q.get("video") == "1")))
            if u.path == "/api/reverse_geo":
                return self._json(reverse_geo(q.get("lat"), q.get("lon")))
            if u.path == "/api/notes":
                return self._json(notes_v2())
            if u.path == "/api/note":
                return self._json(note_detail(q["id"]))
            if u.path == "/api/note_img":
                data = note_image(q.get("digest"))
                ctype = ("image/png" if data[:4] == b"\x89PNG"
                         else "image/gif" if data[:4] == b"GIF8"
                         else "image/webp" if data[:4] == b"RIFF"
                         else "image/jpeg")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/todos":
                try:
                    since = float(q.get("since") or 0)
                except ValueError:
                    since = 0
                return self._json({"todos": todo_list(q.get("refresh") == "1", since),
                                   "at": TODO_CACHE.get("at") or 0})
            if u.path == "/api/video_start":
                video_start(q["id"])
                return self._json(video_state(q["id"]))
            if u.path == "/api/video_play_start":
                video_play_start(q["id"])
                return self._json(video_play_state(q["id"]))
            if u.path == "/api/video_play_state":
                return self._json(video_play_state(q["id"]))
            if u.path == "/api/video_play":
                p = _play_path(q["id"])
                if not os.path.exists(p):
                    return self._json({"error": "not ready"}, 404)
                return self._send_file(p, "video/mp4")
            if u.path == "/api/video_cancel":
                return self._json(video_cancel(q["id"]))
            if u.path == "/api/video_state":
                return self._json(video_state(q["id"]))
            if u.path == "/api/video":
                p = _video_path(q["id"])
                if not os.path.exists(p):
                    return self._json({"error": "not ready"}, 404)
                return self._send_file(p, "video/mp4")
            if u.path == "/api/full":
                try:
                    img = full_bytes(q["id"])
                except Exception as e:
                    import traceback; print("[原图] %s 失败：%s\n%s" % (q.get("id"), e, traceback.format_exc()))
                    return self._send(404, b"", "text/plain")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(img)
                return
            if u.path == "/api/thumb":
                try:
                    img = thumb_bytes(q["id"], q.get("size", "small"))
                except Exception as e:
                    print("[缩略图] %s 解密失败：%s" % (q.get("id"), e))
                    return self._send(404, b"", "text/plain")
                hdr = {"Cache-Control": "max-age=86400"}
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img)))
                for k, v in hdr.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(img)
                return
            if u.path == "/api/records":
                return self._json({"records": record_page(int(q.get("offset", 0)),
                                                          int(q.get("limit", 100)),
                                                          q.get("refresh") == "1")})
            if u.path == "/api/record_url":
                return self._json({"url": record_url(q["id"])})
            if u.path == "/api/record_prepare":
                try:
                    p = record_audio_file(q["id"])
                    return self._json({"ok": True, "size": os.path.getsize(p)})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)})
            if u.path == "/api/record_audio":
                try:
                    p = record_audio_file(q["id"])
                    return self._send_file(p, _audio_ctype(p))
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
            if u.path == "/api/drive":
                return self._json(drive_page(q.get("parent", 0), int(q.get("page", 1))))
            if u.path == "/api/drive_file":
                try:
                    fid = q["id"]
                    d = drive_file_info(fid)
                    p = drive_file_bytes(fid)
                    mime = (d.get("mimeType")
                            or mimetypes.guess_type(d.get("name") or "")[0]
                            or "application/octet-stream")
                    return self._send_file(p, mime)
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
            if u.path == "/api/trash":
                return self._json(trash_list(int(q.get("limit", 200)),
                                             q.get("maxTag", 0)))
            if u.path == "/api/record_trash":
                return self._json(record_trash())
            if u.path == "/api/upload_progress":
                return self._json(upload_progress(q.get("job")))
            if u.path == "/api/downloads":
                return self._json({"jobs": list(JOBS.values())})
            return self._json({"error": "not found"}, 404)
        except NeedStrongAuth as e:
            # 会话本身没坏（相册/笔记/录音都还能用），只是这个服务要更强的身份
            return self._json({"error": "strongauth", "path": str(e)}, 403)
        except NeedLogin:
            AUTH["ok"] = False
            return self._json({"error": "login",
                               "msg": "登录态失效：请先打开「小米云服务」客户端，或在窗口里扫码"}, 401)
        except Exception as e:
            return self._json({"error": type(e).__name__, "msg": str(e)}, 500)

    def do_PUT(self):
        """浏览器直传文件：PUT /api/upload_raw?kind=drive&target=0&name=xxx"""
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if not AUTH["ok"]:
                return self._json({"error": "login"}, 401)
            if u.path != "/api/upload_raw":
                return self._json({"error": "not found"}, 404)
            n = int(self.headers.get("Content-Length") or 0)
            return self._json(upload_receive(q.get("kind") or "drive",
                                             q.get("target") or "0",
                                             q.get("name") or "upload.bin",
                                             self.rfile, n))
        except NeedLogin:
            AUTH["ok"] = False
            return self._json({"error": "login"}, 401)
        except Exception as e:
            return self._json({"error": type(e).__name__, "msg": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        try:
            if u.path == "/api/login":
                start_qr()
                return self._json({"ok": True})
            if u.path == "/api/device_login":
                # 设备验证登录：弹一个内嵌窗口，让用户在小米官方页面输密码
                return self._json(device_login_start())
            if u.path == "/api/relogin":
                # 界面上的「重新登录」：先自己取票，取不到再弹二维码。
                # ensure_login 内部有单例闸，重复调用会立刻返回，不会叠出多个客户端窗口。
                threading.Thread(target=ensure_login, daemon=True).start()
                return self._json({"ok": True})
            if u.path == "/api/album_layout":
                return self._json(album_layout_set(body))
            if u.path == "/api/note_create":
                return self._json(note_create(body.get("title"), body.get("content"),
                                              body.get("folder") or "0"))
            if u.path == "/api/settings":
                restart = False
                if "cache_auto" in body:
                    SETTINGS["cache_auto"] = bool(body.get("cache_auto"))
                if "cache_limit" in body:
                    try:
                        SETTINGS["cache_limit"] = max(
                            256 * 1024 * 1024,
                            int(body.get("cache_limit") or DEFAULT_CACHE_LIMIT))
                    except Exception:
                        return self._json({"ok": False, "error": "上限不是有效数字"}, 400)
                migrated = [0, 0]
                if "cache_dir" in body:
                    d = str(body.get("cache_dir") or "").strip()
                    target = None
                    if d:
                        nd = _check_dir(d)
                        if not nd:
                            return self._json({"ok": False, "error": "这个目录不可写"}, 400)
                        if os.path.abspath(nd) != os.path.abspath(CACHE_DIR):
                            target = nd
                    else:
                        if (os.path.abspath(CACHE_DIR)
                                != os.path.abspath(DEFAULT_CACHE_DIR)):
                            target = DEFAULT_CACHE_DIR
                    if target:
                        try:
                            n, b = migrate_cache(CACHE_DIR, target)
                        except Exception as e:
                            return self._json({"ok": False, "error": str(e)}, 400)
                        SETTINGS["cache_dir"] = ("" if os.path.abspath(target)
                                                 == os.path.abspath(DEFAULT_CACHE_DIR)
                                                 else target)
                        restart = True
                        migrated = [n, b]
                _save_settings(SETTINGS)
                lim = cache_limit_bytes()
                cache_gc.CACHE_LIMIT = lim
                cache_gc.CACHE_LOW = int(lim * 0.8)
                if not restart:
                    cache_gc.gc_in_background(CACHE_DIR, lim)
                return self._json({"ok": True, "restart": restart, "limit": lim,
                                   "dir": CACHE_DIR,
                                   "auto": bool(SETTINGS.get("cache_auto")),
                                   "migrated_files": migrated[0],
                                   "migrated_bytes": migrated[1]})
            if u.path == "/api/logout":
                return self._json(logout_account())
            if u.path == "/api/open_url":
                # 界面里的外链一律交给系统默认浏览器打开，
                # 绝不让应用自己的 WebView 导航到外部页面。
                url = str(body.get("url") or "").strip()
                if not re.match(r"^https?://", url, re.I):
                    return self._json({"ok": False, "err": "bad url"}, 400)
                try:
                    import webbrowser
                    webbrowser.open(url)
                    return self._json({"ok": True})
                except Exception as e:
                    return self._json({"ok": False, "err": "%s" % e}, 500)
            if u.path == "/api/cache_clear":
                # 手动清空缓存：删光缩略图/原图/原视频/笔记图
                try:
                    freed, n = cache_gc.clear_all(CACHE_DIR)
                except Exception as e:
                    return self._json({"ok": False, "error": "%s" % e})
                print("[缓存] 手动清空：%d 个文件，释放 %.1f MB" % (n, freed / 1048576.0))
                st = cache_gc.stats(CACHE_DIR)
                return self._json({"ok": True, "freed": freed, "removed": n,
                                   "total": st["total"], "count": st["count"]})
            if u.path == "/api/note_save":
                return self._json(note_save(str(body.get("id") or ""),
                                            body.get("title"), body.get("content")))
            if u.path == "/api/note_delete":
                ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
                return self._json(note_delete(ids))
            if u.path == "/api/todo_create":
                return self._json(todo_create(body.get("title"), body.get("subs")))
            if u.path == "/api/download":
                jid = start_job(body.get("kind"), body.get("items") or [],
                                body.get("dir") or HOME_DL)
                return self._json({"job": jid})
            if u.path == "/api/upload":
                return self._json(upload_start(body.get("kind"),
                                               body.get("paths") or [],
                                               body.get("target")))
            # ---- 相册管理
            if u.path == "/api/album_create":
                return self._json(album_create(body.get("name")))
            if u.path == "/api/album_rename":
                return self._json(album_rename(body.get("id"), body.get("name")))
            if u.path == "/api/album_delete":
                return self._json({"job": start_album_delete(body.get("id"))})
            if u.path == "/api/photo_delete":
                return self._json(photo_delete(body.get("ids") or []))
            if u.path == "/api/photo_move":
                return self._json(photo_move(body.get("ids") or [],
                                             body.get("album")))
            if u.path == "/api/photo_copy":
                return self._json(photo_copy(body.get("ids") or [],
                                             body.get("album")))
            if u.path == "/api/trash_recover":
                return self._json(trash_recover(body.get("ids") or [],
                                                body.get("tags") or {}))
            if u.path == "/api/trash_purge":
                return self._json(trash_purge(body.get("ids") or [],
                                              body.get("tags") or {}))
            # ---- 云盘管理
            if u.path == "/api/drive_mkdir":
                return self._json(drive_mkdir(body.get("name"),
                                              body.get("parent")))
            if u.path == "/api/drive_rename":
                return self._json(drive_rename(body.get("id"), body.get("name")))
            if u.path == "/api/drive_delete":
                return self._json(drive_delete(body.get("id"),
                                               bool(body.get("folder")),
                                               bool(body.get("permanent"))))
            if u.path == "/api/drive_open":
                try:
                    return self._json(drive_open(body.get("id")))
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 500)
            if u.path == "/api/drive_move":
                return self._json(drive_move(body.get("id"), body.get("parent"),
                                             body.get("name"),
                                             bool(body.get("folder"))))
            if u.path == "/api/drive_copy":
                return self._json(drive_copy(body.get("id"), body.get("parent"),
                                             body.get("name")))
            # ---- 代办 / 录音
            if u.path == "/api/todo_toggle":
                return self._json(todo_toggle(body.get("id"),
                                              bool(body.get("done"))))
            if u.path == "/api/todo_update":
                return self._json(todo_update(body.get("id"), body.get("title"),
                                              body.get("subs") or []))
            if u.path == "/api/todo_delete":
                return self._json(todo_delete(body.get("id")))
            if u.path == "/api/record_delete":
                return self._json(record_delete(body.get("id")))
            if u.path == "/api/record_recover":
                return self._json(record_recover(body.get("id"),
                                                 body.get("ver")))
            if u.path == "/api/record_purge":
                return self._json(record_purge(body.get("id"),
                                               body.get("ver")))
            return self._json({"error": "not found"}, 404)
        except NeedStrongAuth as e:
            return self._json({"error": "strongauth", "path": str(e)}, 403)
        except NeedLogin:
            return self._json({"error": "login"}, 401)
        except Exception as e:
            return self._json({"error": type(e).__name__, "msg": str(e)}, 500)


# ---------------------------------------------------------------- 登录
QR = {"state": "idle", "img": None, "err": None}
QR_LOCK = threading.Lock()


def _qr_flow():
    """后台跑扫码登录；二维码以 base64 交给前端显示。"""
    global S, TOKEN
    sess = make_session()
    try:
        dc = str(int(time.time() * 1000))
        r = sess.get("https://account.xiaomi.com/longPolling/loginUrl",
                     params={"sid": "i.mi.com", "_qrsize": "240",
                             "_locale": "zh_CN", "_dc": dc}, timeout=25)
        j = json.loads(r.text.replace(SEP, ""))
        if j.get("code") != 0 or not j.get("qr"):
            raise RuntimeError("取二维码失败：%s" % (j.get("description") or j.get("code")))
        img = sess.get(j["qr"], timeout=25).content
        if not img.startswith(b"\x89PNG"):
            raise RuntimeError("二维码图片下载失败")
        QR["img"] = base64.b64encode(img).decode()
        QR["state"] = "waiting"
        set_login("qr", "用手机扫码登录", "打开小米手机「扫一扫」，扫描后确认即可", pct=0)
        deadline = time.time() + int(j.get("timeout") or 120) + 20
        while time.time() < deadline:
            try:
                lr = sess.get(j["lp"], timeout=95)
            except Exception:
                continue
            try:
                lj = json.loads(lr.text.replace(SEP, "").strip())
            except Exception:
                continue
            if lj.get("location"):
                sess.get(lj["location"], timeout=25)
                break
            code = lj.get("code")
            if code not in (0, None):
                raise RuntimeError("扫码返回异常 code=%s" % code)
        else:
            raise RuntimeError("二维码已超时，请重新获取")
        sess.get("https://i.mi.com/silentLogin", timeout=25)
        sess.headers["Referer"] = "https://i.mi.com/"

        # 扫码流程可能同时落下两张 serviceToken（.i.mi.com 和 i.mi.com 各一份），
        # 其中一张是废的。逐张验证，只留能用的那张 —— 否则下次启动
        # load_session 会因为同名冲突直接抛异常，永远登不上。
        cands = service_tokens(sess)
        print("[登录] 扫码后拿到 %d 张候选 serviceToken" % len(cands))
        tok = None
        for i, t in enumerate(cands):
            keep_only_token(sess, t)
            if verify_session(sess):
                fresh = service_tokens(sess)
                tok = fresh[0] if fresh else t
                keep_only_token(sess, tok)
                print("[登录] 第 %d 张可用（轮换后长度 %d）" % (i, len(tok)))
                break
            print("[登录] 第 %d 张无效" % i)
        if not tok:
            raise RuntimeError("扫码成功但拿到的 serviceToken 都用不了")
        has_pt = any(c.name == "passToken" for c in sess.cookies)
        # 登记最新票：之后并发的旧会话落盘也覆盖不了它
        note_service_token(tok)
        note_pass_token(get_cookie(sess, "passToken"))
        _PASS_KEEP_AT[0] = time.time()
        _save_keepalive_at(_PASS_KEEP_AT[0])
        with LOCK:
            S = sess
            TOKEN = tok
            AUTH["ok"] = True
            save_session(sess, merge=True)
        print("[登录] 扫码成功，本地 cookie：%s" % sorted({c.name for c in sess.cookies}))
        print("[登录] passToken：%s" % ("已保存" if has_pt else "未拿到"))
        QR["state"] = "ok"
        set_login("ready", "登录就绪",
                  "扫码登录成功" + ("，以后不再需要扫码"
                                    if any(c.name == "passToken" for c in sess.cookies)
                                    else "（未取到长期票，下次可能还需扫码）"), pct=100)
    except Exception as e:
        QR["state"] = "error"
        QR["err"] = str(e)
        set_login("error", "扫码登录失败", str(e), pct=0, err=str(e))


def logout_account():
    """退出登录：先备份 session.dat，再清掉登录票，重新走扫码登录。

    只动我们自己的会话文件，不碰官方客户端（红线）。
    """
    global S, TOKEN
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak_dir = os.environ.get("MIYUN_BACKUP_DIR") or os.path.join(BASE, "backup")
    bak = None
    src = os.path.join(BASE, "session.dat")
    try:
        os.makedirs(bak_dir, exist_ok=True)
        if os.path.exists(src):
            bak = os.path.join(bak_dir, "小米云_session_%s.dat" % ts)
            shutil.copy2(src, bak)
    except Exception as e:
        print("[退出] 备份登录票失败：%s" % e)
    try:
        for fn in os.listdir(BASE):
            if fn == "session.dat" or fn.startswith("session.dat.bak_"):
                try:
                    os.remove(os.path.join(BASE, fn))
                except Exception:
                    pass
    except Exception as e:
        print("[退出] 清理会话文件失败：%s" % e)
    with LOCK:
        S = None
        TOKEN = None
    AUTH["ok"] = False
    try:
        QR.update({"state": "idle", "img": None, "err": None})
        start_qr()
    except Exception as e:
        print("[退出] 重新拉起扫码失败：%s" % e)
    return {"ok": True, "backup": bak}


def start_qr():
    with QR_LOCK:
        if QR["state"] == "waiting":
            return
        QR.update({"state": "starting", "img": None, "err": None})
    threading.Thread(target=_qr_flow, daemon=True).start()


ACCOUNT_CACHE = {"at": 0, "data": None}


def account_info(force=False):
    """账号资料：名字、头像、userId。只读，缓存 10 分钟。"""
    if (not force and ACCOUNT_CACHE["data"]
            and time.time() - ACCOUNT_CACHE["at"] < 600):
        return ACCOUNT_CACHE["data"]
    uid, name, avatar = "", "", ""
    try:
        for c in load_all_cookies():
            if c.get("name") == "userId":
                uid = str(c.get("value") or "")
                break
    except Exception:
        pass
    try:
        res = api_get("/status/lite/profile")
        d = res.get("data") if isinstance(res.get("data"), dict) else res
        src = {}
        if isinstance(d, dict):
            src.update(d)
            for nest in ("userInfo", "profile", "user"):
                if isinstance(d.get(nest), dict):
                    src.update(d[nest])
        for k in ("miliaoNick", "userName", "nickName", "nickname", "name"):
            if src.get(k):
                name = str(src[k])
                break
        for k in ("miliaoIcon", "icon", "avatar", "iconUrl", "headImg", "headUrl"):
            if src.get(k):
                avatar = str(src[k])
                break
        if not uid and src.get("userId"):
            uid = str(src.get("userId"))
    except Exception as e:
        print("[账号] 读取资料失败：%s" % e)
    out = {"userId": uid,
           "name": name or ("小米账号" + (" " + uid[-4:] if uid else "")),
           "avatar": avatar}
    ACCOUNT_CACHE.update({"at": time.time(), "data": out})
    return out


def ensure_login():
    """取票顺序：本地会话 → 小米云服务（没开就自己拉起来取，取完关掉）→ 扫码。

    单例闸：正在取票时再被调用，直接返回当前登录态，不再起第二条链路。
    没有它的话，前端每个 401 都会拉一个新线程去启动一个客户端窗口。
    """
    global S, TOKEN, LOGIN_RUNNING

    with LOGIN_GATE:
        if LOGIN_RUNNING:
            print("[登录] 已有取票在进行中，跳过本次重复请求")
            return AUTH["ok"]
        LOGIN_RUNNING = True
    try:
        return _ensure_login_once()
    finally:
        with LOGIN_GATE:
            LOGIN_RUNNING = False


def _ensure_login_once():
    global S, TOKEN
    set_login("checking", "正在检查登录状态", "读取本地保存的登录票 …", pct=5)
    s = make_session()
    s.headers["Referer"] = "https://i.mi.com/"
    try:
        cands = load_session(s)      # 可能是多张新旧不同的 serviceToken
        print("[登录] 本地有 %d 张候选 serviceToken" % len(cands))
        for i, tok in enumerate(cands):
            keep_only_token(s, tok)
            if verify_session(s):
                # verify 这一下服务端可能已经轮换出票了，必须把 TOKEN 同步成
                # 轮换后的那张，否则下一个请求还在用旧票 —— 立刻 401。
                fresh = service_tokens(s)
                tok = fresh[0] if fresh else tok
                keep_only_token(s, tok)
                note_service_token(tok)    # 这张是刚被服务端确认过的
                with LOCK:
                    TOKEN = tok
                # 合并写：以前 merge=False 只留当前会话的 cookie，
                # 会把磁盘上的 passToken / trust_device_auth_token 一并抹掉
                save_session(s, merge=True)
                S = s
                AUTH["ok"] = True
                set_login("ready", "登录就绪", "复用本地登录票，无需扫码", pct=100)
                print("[登录] 第 %d 张可用，复用本地会话，免扫码" % i)
                # 距上次成功续期太久就补一次：票的续期窗口只有 30 天，
                # 程序关着的时候不会续，开的时候必须把它推上去。
                # 官方客户端也是每次启动都静默续一遍（init login → login sucess）。
                if time.time() - _load_keepalive_at() > KEEPALIVE_MAX_GAP:
                    print("[登录] 距上次续期超过 7 天，后台补续一次")
                    threading.Thread(target=silent_relogin, daemon=True).start()
                return True
            print("[登录] 第 %d 张无效" % i)
    except Exception as e:
        print("[登录] 读取本地会话失败：%s %s" % (type(e).__name__, e))
    # 2) 用自己的 passToken 静默续期（不碰客户端，这是长期免登录的正解）
    set_login("checking", "正在续期登录票", "用本地保存的 passToken 换一张新票 …", pct=12)
    if silent_relogin():
        set_login("ready", "登录就绪", "已用本地 passToken 自动续期", pct=100)
        return True

    # 3) 兜底：客户端取票（默认关闭，见 ALLOW_CLIENT_BORROW）
    if borrow_login_ticket():
        return True
    S = make_session()
    S.headers["Referer"] = "https://i.mi.com/"
    AUTH["ok"] = False
    print("[登录] 取票失败，改为扫码")
    # 客户端通道关掉后，没人会替我们拉起扫码流程了，这里必须自己开
    set_login("qr", "需要扫码登录",
              "本地没有可用的长期票，扫一次码之后就不用再扫了", pct=0)
    start_qr()
    return False


# ---------------------------------------------------------------- 后台保活
def _keepalive_loop():
    """低频保活：按天续一次，不刷接口。

    为什么按时间而不是按请求：passToken 只在「走账号登录接口」时才刷新，
    天天浏览相册并不会延长它的寿命。官方客户端 cookie 库实测：
    expires 恒等于 last_access + 30 天，靠的就是每次启动走一次续期
    （日志：silentRelogin → 隐藏 webview → login sucess）。
    客户端每次启动续一次；我们常驻，就按 24 小时续一次，调用量级相当。

    失败不做额外重试 —— silent_relogin() 自己带指数退避，这里只管"该不该续"。
    """
    while True:
        time.sleep(1800)
        try:
            if not AUTH.get("ok"):
                continue
            idle = time.time() - _PASS_KEEP_AT[0]
            if idle < 24 * 3600:
                continue
            print("[保活] 距上次成功续期已超过 %.1f 小时，续一次" % (idle / 3600.0))
            silent_relogin()
        except Exception as e:
            print("[保活] %s %s" % (type(e).__name__, e))


def _prewarm_todos():
    """启动后后台把待办缓存热起来：用户点「代办」时能秒出。"""
    try:
        time.sleep(10)                      # 让首屏和登录先跑完
        print("[代办] 后台预热 …")
        todo_list()
    except Exception as e:
        print("[代办] 预热失败：%s %s" % (type(e).__name__, e))


def _prewarm_timeline():
    """启动后后台预热时间轴缓存：首次打开相册不用等 40+ 秒。

    只预热「全部照片」和照片最多的几本，串行执行、每本之间歇 1 秒。

    ⚠ 必须用**独立 session**，不能走 api_get：
    预热要探测几百次，走 api_get 会长时间占着全局 LOCK，
    前端所有请求（列表/缩略图/相册）都排在后面 → 界面卡住。
    """
    try:
        time.sleep(10)                      # 等首屏取票和列表先跑完

        # 独立会话，从磁盘复制一份 cookie，跟主会话完全隔离
        s2 = make_session()
        try:
            toks2 = load_session(s2)
            if not pick_working_token(s2, toks2):
                print("[时间轴] 预热跳过：独立会话取不到可用票")
                return
        except Exception as e:
            print("[时间轴] 预热跳过：%s" % e)
            return

        albs = list_albums()
        targets = [{"id": "0", "name": "全部照片", "count": 0}]
        targets += [a for a in albs if a["id"] == "1"]      # 相机
        rest = [a for a in albs if a["id"] not in ("1", "2", "1000")]
        rest.sort(key=lambda x: -x["count"])
        targets.extend(rest[:3])

        for a in targets:
            try:
                cp = os.path.join(TL_CACHE_DIR, re.sub(r"\W", "_", a["id"])[:80] + ".json")
                if os.path.exists(cp):
                    with open(cp, "r", encoding="utf-8") as f:
                        o = json.load(f)
                    if o.get("months") and (time.time() - (o.get("at") or 0)) < 12 * 3600:
                        continue
                print("[时间轴] 后台预热：%s" % a["name"])
                # 官方接口一次拿全（0.04 秒），预热成本几乎为零
                gallery_timeline(a["id"])
                time.sleep(1)
            except Exception as e:
                print("[时间轴] 预热 %s 失败：%s" % (a["name"], e))
    except Exception as e:
        print("[时间轴] 预热线程出错：%s %s" % (type(e).__name__, e))


def _months_via(group_id, sess):
    """用指定 session 跑一遍月份探测（给预热线程用，不碰全局 S/LOCK）。"""
    ids = [x for x in str(group_id or "").split(",") if x]
    if not ids:
        return {"months": []}
    import calendar
    import datetime

    def one(aid, page=0, limit=1, sd=None, ed=None):
        p = {"pageNum": page, "pageSize": limit, "isShared": "false"}
        if aid and aid not in ("0", "1"):
            p["albumId"] = aid
        if sd:
            p["startDate"] = sd
        if ed:
            p["endDate"] = ed
        try:
            r = sess.get(API + "/gallery/user/galleries", params=p, timeout=60)
            d = (r.json() or {}).get("data") or {}
            return d.get("galleries") or []
        except Exception:
            return []

    first = one(ids[0], 0, 1)
    newest = 0
    for it in first:
        newest = max(newest, it.get("dateTaken") or it.get("sortTime") or 0)
    for aid in ids[1:]:
        for it in one(aid, 0, 1):
            newest = max(newest, it.get("dateTaken") or it.get("sortTime") or 0)
    if not newest:
        return {"months": []}
    dt = datetime.datetime.fromtimestamp(newest / 1000)
    y_new, m_new = dt.year, dt.month

    months = []
    empty = 0
    for y in range(y_new, y_new - 40, -1):
        if empty >= 24:
            break
        for m in range(12, 0, -1):
            if y == y_new and m > m_new:
                continue
            sd = "%d%02d01" % (y, m)
            ed = "%d%02d%02d" % (y, m, calendar.monthrange(y, m)[1])
            found = 0
            for aid in ids:
                if one(aid, 0, 1, sd, ed):
                    found = 1
                    break
            if found:
                empty = 0
                n = 0
                for aid in ids:
                    n += len(one(aid, 0, 200, sd, ed))
                months.append({"y": y, "m": m, "n": n, "sd": sd, "ed": ed})
            else:
                empty += 1
                if empty >= 24:
                    break

    months.sort(key=lambda x: (-x["y"], -x["m"]))
    out = {"months": months, "from": (months[-1]["y"] if months else y_new),
           "to": y_new, "at": time.time()}
    try:
        os.makedirs(TL_CACHE_DIR, exist_ok=True)
        cp = os.path.join(TL_CACHE_DIR, re.sub(r"\W", "_", str(group_id))[:80] + ".json")
        tmp = cp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, cp)
    except Exception:
        pass
    return out


def main():
    global WV_CDP_PORT
    print("=" * 56)
    print(" 小米云 · 只读查看器")
    print("=" * 56)

    # 调试端口什么时候开（必须在 WebView2 创建之前决定，晚了不生效）：
    #   * 正常已登录启动：**不开**。固定端口等于把窗口的控制权交给本机
    #     任何程序——别的 Agent 能通过 CDP 打开任意页面、执行脚本
    #     （2026-11-24 实际发生过：窗口被导航到外部测试页）。
    #   * 本地没有可用登录票、马上要显示登录页：才开，而且用随机空闲端口，
    #     给「设备登录」窗口读 cookie 用（wv_cookies.get_cookies）。
    #   * MIYUN_CDP_PORT=<端口>：显式调试用，照开。
    # 不管开不开，登录流程、登录文件、登录态都不受影响。
    _args = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    WV_CDP_PORT = cdp_port_for_startup()
    if WV_CDP_PORT and "remote-debugging-port=" not in _args:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            (_args + " --remote-debugging-port=%d" % WV_CDP_PORT).strip())
    print("[启动] 调试端口：%s" % (
        ("127.0.0.1:%d（登录页需要）" % WV_CDP_PORT) if WV_CDP_PORT else "关闭"))

    # 注意：这里**不要**清理 WebView2 的 profile 目录。
    # 以前每次启动都删一遍 %TEMP%\miyun-webview-profile。但如果上一次是被
    # 强杀的，它的 msedgewebview2.exe 子进程还活着、还锁着这些文件，删一半
    # 之后新开的 WebView2 拿到的是残缺 profile —— 窗口一片白、什么都不显示。
    # 路径现在已经固定，正常退出时 pywebview 自己会删，不需要我们再动手。

    # 缓存超上限就先淘汰一批最久没看的，别等磁盘满了才动手
    try:
        cache_gc.gc_in_background(CACHE_DIR, cache_limit_bytes())
    except Exception:
        pass

    # 先把窗口和服务起来，再去取票：这样取票慢的时候界面能显示进度，
    # 而不是黑屏等一分钟。
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("本地服务：http://127.0.0.1:%d" % port)
    # 上次续期时刻：决定这次启动要不要补一次（票的续期窗口只有 30 天）
    _PASS_KEEP_AT[0] = _load_keepalive_at()
    threading.Thread(target=ensure_login, daemon=True).start()
    threading.Thread(target=_keepalive_loop, daemon=True).start()
    threading.Thread(target=_prewarm_timeline, daemon=True).start()
    threading.Thread(target=_prewarm_todos, daemon=True).start()

    webview.create_window("小米云", "http://127.0.0.1:%d/" % port,
                          width=1280, height=820, min_size=(980, 640),
                          text_select=True)      # 允许选中/复制便签正文
    # 窗口/任务栏图标：和 exe 图标、界面左上角 logo 用同一张 app.ico
    icon = os.path.join(res_dir(), "app.ico")
    # storage_path 固定下来：不用每次新建一个临时目录，退出时 pywebview
    # 自己会删（private_mode 下 clear_user_data 会 rmtree 这个路径）。
    # private_mode=False：不然 WebView2 跑在无痕模式，cookie 只存内存不落盘，
    # 设备登录窗口每次都要重新输密码。关掉之后登录态能持久，下次开窗自动登录。
    webview.start(icon=icon if os.path.isfile(icon) else None,
                  storage_path=cache_gc.webview_profile_dir(),
                  private_mode=False)


if __name__ == "__main__":
    try:
        # line_buffering：程序被强杀时日志不会丢在缓冲区里，
        # 排查登录问题全靠这些行
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:
        pass
    main()
