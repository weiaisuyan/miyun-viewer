# -*- coding: utf-8 -*-
"""
小米云服务 —— 只读探测脚本（第 1 步）

做什么：
  复用本机「小米云服务」客户端里已有的登录态，向小米云接口发几个只读请求，
  确认相册、云盘的数据能不能列出来、大概多少量。

不做什么：
  - 不下载任何照片/文件
  - 不修改/删除云上任何数据
  - 不把任何 token / cookie 打印到屏幕或写入磁盘

用法：
  python probe.py
"""
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from paths import data_dir as _data_dir
    _HERE = _data_dir()          # 打包后 = exe 所在目录（可写）
except Exception:
    _HERE = os.path.dirname(os.path.abspath(__file__))

APPDATA_XM = os.path.join(os.environ["APPDATA"], "XiaomiCloud")
COOKIE_DB = os.path.join(APPDATA_XM, "Network", "Cookies")
API = "https://i.mi.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_PROXY = (os.environ.get("MIYUN_PROXY") or "").strip()
PROXY = {"http": _PROXY, "https": _PROXY} if _PROXY else None
SEP = "&&&START&&&"


def read_local_auth():
    """从客户端本地库读出 userId / passToken。只读，读完即删临时副本。"""
    if not os.path.exists(COOKIE_DB):
        raise SystemExit("找不到客户端 cookie 库：%s" % COOKIE_DB)
    tmp = os.path.join(tempfile.gettempdir(), "_probe_ck.db")
    shutil.copy2(COOKIE_DB, tmp)
    try:
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT host_key, name, value FROM cookies WHERE host_key LIKE '%xiaomi.com'"
        ).fetchall()
        conn.close()
    finally:
        os.remove(tmp)
    jar = {(h, n): v for h, n, v in rows if v}
    user_id = jar.get((".account.xiaomi.com", "userId"))
    pass_token = jar.get((".account.xiaomi.com", "passToken"))
    if not user_id or not pass_token:
        raise SystemExit("本地 cookie 库里没有 userId / passToken，客户端可能未登录")
    return user_id, pass_token, jar


def make_session():
    s = requests.Session()
    s.trust_env = False          # 不自动套系统代理（i.mi.com 走国内直连）
    s.headers.update({"User-Agent": UA, "Referer": API + "/"})
    return s


def service_login(session, user_id, pass_token):
    """用 passToken 换一个 i.mi.com 的 serviceToken（等价于静默登录）。"""
    r = session.get("https://account.xiaomi.com/pass/serviceLogin",
                    params={"_json": "true", "sid": "i.mi.com"}, timeout=20)
    j = json.loads(r.text.split(SEP)[-1])
    if "_sign" in j:                      # 常规路径：先要签名，再提 passToken 换票
        r = session.post("https://account.xiaomi.com/pass/serviceLoginAuth2",
                         data={"sid": "i.mi.com", "hash": pass_token,
                               "_sign": j["_sign"], "callback": j["callback"],
                               "user": user_id, "_json": "true"}, timeout=20)
        j = json.loads(r.text.split(SEP)[-1])
    if j.get("code") != 0:
        raise SystemExit("静默登录失败 code=%s desc=%s" % (j.get("code"), j.get("desc")))
    # STS 直连会被 401 拒；走 silentLogin，由它完成换票并下发 serviceToken
    session.get("https://i.mi.com/silentLogin", timeout=20)
    token = get_cookie(session, "serviceToken")
    if not token:
        raise SystemExit("登录成功但没拿到 serviceToken")
    return token


def qr_login(session):
    """扫码登录：小米长轮询接口。二维码存成本地图片并自动打开，你用手机扫。"""
    import base64
    import time

    dc = str(int(time.time() * 1000))
    r = session.get("https://account.xiaomi.com/longPolling/loginUrl",
                    params={"sid": "i.mi.com", "_qrsize": "240",
                            "_locale": "zh_CN", "_dc": dc}, timeout=20)
    j = json.loads(r.text.replace(SEP, ""))
    if j.get("code") != 0 or not j.get("qr"):
        raise SystemExit("取二维码失败：%s" % j.get("description"))

    qr_path = os.path.join(_HERE, "login_qr.png")
    img = session.get(j["qr"], timeout=20)
    if not img.content.startswith(b"\x89PNG"):
        raise SystemExit("二维码图片下载失败")
    with open(qr_path, "wb") as f:
        f.write(img.content)
    print("  二维码已保存：%s" % qr_path)
    try:
        os.startfile(qr_path)          # 用系统默认看图工具打开
    except Exception:
        pass
    print("  请用小米手机「扫一扫」扫描二维码并在手机上确认登录 …")

    deadline = time.time() + int(j.get("timeout") or 120) + 15
    while time.time() < deadline:
        try:
            lr = session.get(j["lp"], timeout=90)
        except Exception:
            continue
        text = lr.text.replace(SEP, "").strip()
        try:
            lj = json.loads(text)
        except Exception:
            continue
        if lj.get("location"):
            session.get(lj["location"], timeout=20)
            break
        code = lj.get("code")
        if code not in (0, None):
            raise SystemExit("扫码登录返回异常 code=%s desc=%s" % (code, lj.get("desc")))
    else:
        raise SystemExit("扫码超时，请重跑一次")

    session.get("https://i.mi.com/silentLogin", timeout=20)
    token = get_cookie(session, "serviceToken")
    if not token:
        raise SystemExit("扫码成功但没拿到 serviceToken")
    print("  扫码登录：成功")
    return token


# ---------- 会话缓存（DPAPI 加密，只有当前 Windows 用户能解开） ----------
SESS_FILE = os.path.join(_HERE, "session.dat")


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(data):
    bi, bo = _blob(data), _BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
        raise OSError("CryptProtectData 失败")
    return ctypes.string_at(bo.pbData, bo.cbData)


def dpapi_unprotect(data):
    bi, bo = _blob(data), _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
        raise OSError("CryptUnprotectData 失败")
    return ctypes.string_at(bo.pbData, bo.cbData)


def get_cookie(session, name):
    """安全地取一个 cookie 的值。

    requests 的 cookies.get() 在同名 cookie 存在多个域时会抛
    CookieConflictError，小米的域名又多（i.mi.com / .i.mi.com / .mi.com），
    直接调用很容易炸。这里取「值最长的那张」—— 最长的一般是最新的。
    """
    try:
        return session.cookies.get(name)
    except Exception:
        cand = [c.value for c in session.cookies if c.name == name and c.value]
        return max(cand, key=len) if cand else None


def _norm_domain(d):
    """把 `.i.mi.com` 和 `i.mi.com` 归一成同一个域，避免同名 cookie 冲突。"""
    return (d or "").lstrip(".").lower()


# 长期票：这些 cookie 一旦有了就要一直留着，不能被后来的会话覆盖掉。
# passToken 在 .account.xiaomi.com 上，是「长期免登录」的根，只有走
# account.xiaomi.com 的登录（扫码/密码）才会下发。
KEEP_COOKIES = ("passToken", "cUserId", "userId", "deviceId",
                "trust_device_auth_token")


# 最近一次由「登录 / 续期」拿到的长期票与短票。
# 为什么必须单独记着：并发请求各自握着建立时的 session，里面的票可能已经被轮换掉了。
# 让这样的旧 session 落盘，磁盘上的新票就会被写回旧值 —— 下次启动必 70016。
_LATEST = {"pass": None, "pass_at": 0.0, "tok": None, "tok_at": 0.0}
_LATEST_LOCK = threading.Lock()
_LAST_BLOB = {"v": None}
_SAVE_LOCK = threading.Lock()      # 串行化落盘：多个请求线程同时写会踩同一个 .tmp 文件


def note_pass_token(value):
    """登记刚拿到的 passToken（扫码 / 设备登录 / 静默续期成功后调用）。"""
    if not value:
        return
    with _LATEST_LOCK:
        _LATEST["pass"] = value
        _LATEST["pass_at"] = time.time()


def note_service_token(value):
    """登记刚被服务端确认过的 serviceToken。"""
    if not value:
        return
    with _LATEST_LOCK:
        _LATEST["tok"] = value
        _LATEST["tok_at"] = time.time()


def _cookie_rec(c):
    """requests 的 cookie -> 落盘记录。

    以前只存 name/value/domain/path，现在必须把 expires 一起存：
    没有过期时间就不知道票还能活多久，只能等 401 才发现，也没法提前续。
    """
    exp = getattr(c, "expires", None)
    if isinstance(exp, str):
        try:
            exp = int(exp)
        except (TypeError, ValueError):
            exp = None
    rest = getattr(c, "_rest", None) or {}
    return {
        "name": c.name,
        "value": c.value,
        "domain": _norm_domain(c.domain),
        "path": c.path or "/",
        "expires": exp if isinstance(exp, int) else None,
        "secure": bool(getattr(c, "secure", False)),
        "http_only": "HttpOnly" in rest,
    }


def _is_broad(broad, specific):
    """broad 是 specific 的上级域（mi.com 之于 i.mi.com）。"""
    return specific == broad or specific.endswith("." + broad)


def _dedupe(recs):
    """同名 cookie 只留一份「对同一目标域不会重复发送」的记录。

    为什么必须去重：requests 按「精确域 + 上级域」两路匹配 cookie。
    磁盘上 account.xiaomi.com 和 xiaomi.com 各存一份 passToken 时，
    发往 account.xiaomi.com 的请求会把两份同名票都塞进 Cookie 头，
    服务端取哪份全看实现 —— 实测就是这样把票用废的。

    规则：一个域是另一个的上级时只留更具体的那份；
          两个域互不包含（account.xiaomi.com 与 i.mi.com）时都留，各服务各自的请求。
    """
    out = []
    for r in recs:
        drop = False
        for i, k in enumerate(out):
            if k["name"] != r["name"]:
                continue
            if k["domain"] == r["domain"]:
                # 同域同名：留过期时间更晚的那张
                if (r.get("expires") or 0) >= (k.get("expires") or 0):
                    out[i] = r
                drop = True
                break
            if _is_broad(k["domain"], r["domain"]):
                out[i] = r            # 新的更具体，顶掉宽域那份
                drop = True
                break
            if _is_broad(r["domain"], k["domain"]):
                drop = True           # 新的更宽，丢掉
                break
        if not drop:
            out.append(r)
    return out


def load_all_cookies_raw():
    """读磁盘上的原始记录（未去重、未灌进 session）。"""
    try:
        with open(SESS_FILE, "rb") as f:
            data = json.loads(dpapi_unprotect(f.read()).decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _normalize(recs):
    out = []
    for c in recs:
        if not c or not c.get("value") or not c.get("name"):
            continue
        if c["name"][0].isdigit():        # 畸形条目：Expires 被当成了 cookie 名
            continue
        out.append({
            "name": c["name"],
            "value": c["value"],
            "domain": _norm_domain(c.get("domain")),
            "path": c.get("path") or "/",
            "expires": c.get("expires") if isinstance(c.get("expires"), int) else None,
            "secure": bool(c.get("secure")),
            "http_only": bool(c.get("http_only")),
        })
    return out


def _apply_latest(recs):
    """把内存里登记的「最新票」写进记录，压过任何旧 session 带来的值。"""
    with _LATEST_LOCK:
        latest = {"passToken": _LATEST["pass"],
                  "serviceToken": _LATEST["tok"]}
    for name, val in latest.items():
        if not val:
            continue
        hit = False
        for r in recs:
            if r["name"] == name:
                r["value"] = val
                hit = True
        if not hit:
            recs.append({"name": name, "value": val,
                         "domain": "account.xiaomi.com" if name == "passToken" else "i.mi.com",
                         "path": "/", "expires": None,
                         "secure": False, "http_only": name == "passToken"})
    return recs


def save_session(session, merge=True):
    """保存会话到磁盘（合并 + 去重 + 原子写）。

    三条硬规则，都是为了不让「新票被旧值覆盖」：
    1) 合并写：只更新本次带来的，不删磁盘上其它的 —— 以前用 merge=False
       会把 trust_device_auth_token 这类票直接抹掉；
    2) 去重：见 _dedupe，避免每次请求带两份同名票；
    3) passToken / serviceToken 用 note_*_token() 登记的最新值，
       旧 session 落盘也覆盖不了它。
    内容没变就不写盘（并发请求每成功一次都写一遍纯属浪费）。
    """
    with _SAVE_LOCK:
        recs = _normalize([_cookie_rec(c) for c in session.cookies])
        if merge:
            recs += _normalize(load_all_cookies_raw())
        recs = _apply_latest(recs)
        recs = _dedupe(recs)
        blob = json.dumps(recs, sort_keys=True, ensure_ascii=False)
        if blob == _LAST_BLOB["v"]:
            return False
        tmp = SESS_FILE + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(dpapi_protect(blob.encode("utf-8")))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SESS_FILE)  # 原子替换：写一半崩了也不会留下半截文件
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        _LAST_BLOB["v"] = blob
        return True


def load_session(session):
    """把磁盘上的 cookie 灌进 session，返回所有候选 serviceToken（去重、按长度降序）。

    为什么要返回「所有」而不是一个：服务端轮换票据时，磁盘上可能同时存着
    新旧两张 serviceToken，其中一张已经失效。光看长度分不出来谁活谁死，
    只能挨个拿去试。实测过长的那张反而是死的。

    expires 一起灌回去：已过期的 cookie requests 自己就不会发送，
    省得拿一张死票去打请求再吃一次 401。
    """
    raw = load_all_cookies_raw()
    recs = _dedupe(_normalize(raw))
    # 老格式（条目里没有 expires 字段）要升级一次：
    # 把签名清掉，逼下一次 save_session 真正重写文件。
    # 不清的话「内容看起来没变 → 不写盘」会让文件一直停在旧格式。
    if raw and any("expires" not in c for c in raw):
        with _SAVE_LOCK:
            _LAST_BLOB["v"] = None
    else:
        # 记下刚读到的内容：紧接着的 save_session 若没变化就不必再写一遍盘
        with _SAVE_LOCK:
            _LAST_BLOB["v"] = json.dumps(recs, sort_keys=True, ensure_ascii=False)
    for c in recs:
        try:
            session.cookies.set(c["name"], c["value"],
                                domain=c["domain"], path=c["path"],
                                expires=c.get("expires"),
                                secure=c.get("secure"))
        except Exception:
            try:
                session.cookies.set(c["name"], c["value"],
                                    domain=c["domain"], path=c["path"])
            except Exception:
                pass
    return service_tokens(session)


def pick_working_token(session, candidates=None, timeout=20):
    """逐张验证候选 serviceToken，返回第一张真能用的。

    为什么必须有它：load_session() 返回的是「候选列表」，toks[0] 不一定是活的
    （实测长的那张常常反而是死的）。直接拿 toks[0] 去请求会得到 401，
    然后被误判成「登录态失效」——排查时踩过这个坑。

    用法：把 session 里的票逐张试，成功即 keep_only_token 收下并返回。
    全部失败返回 None。
    """
    toks = candidates if candidates is not None else service_tokens(session)
    for t in toks:
        keep_only_token(session, t)
        try:
            r = session.get(API + "/status/lite/profile", timeout=timeout)
        except Exception:
            continue
        if r.status_code == 200 and r.text.lstrip().startswith("{"):
            return t
    return None


def service_tokens(session):
    """session 里所有 serviceToken 的值（去重）。"""
    out = []
    for c in session.cookies:
        if c.name == "serviceToken" and c.value and c.value not in out:
            out.append(c.value)
    return out


def keep_only_token(session, token):
    """只保留指定的这张 serviceToken，其余同名 cookie 全部清掉。"""
    for c in list(session.cookies):
        if c.name == "serviceToken":
            try:
                session.cookies.clear(c.domain, c.path, c.name)
            except Exception:
                pass
    session.cookies.set("serviceToken", token, domain="i.mi.com", path="/")


def load_all_cookies():
    """把磁盘上的 cookie 读成记录列表，给静默登录用。

    走一遍去重：老文件里同一张票可能存了「精确域」和「上级域」两份，
    直接用会把两份同名票都发出去。
    """
    return _dedupe(_normalize(load_all_cookies_raw()))


def verify_session(session):
    try:
        r = session.get(API + "/gallery/user/album/list",
                        params={"pageNum": 0, "pageSize": 1, "isShared": "false"},
                        timeout=20)
        return r.status_code == 200 and (r.json() or {}).get("result") == "ok"
    except Exception:
        return False


class Cloud:
    def __init__(self, session, token):
        self.s, self.token = session, token

    def get(self, path, **params):
        r = self.s.get(API + path, params=params, timeout=30)
        try:
            return r.json()
        except Exception:
            raise SystemExit("GET %s 返回非 JSON（HTTP %s）" % (path, r.status_code))

    def post(self, path, data):
        r = self.s.post(API + path,
                        data={"data": json.dumps(data), "serviceToken": self.token},
                        timeout=30)
        try:
            return r.json()
        except Exception:
            raise SystemExit("POST %s 返回非 JSON（HTTP %s）" % (path, r.status_code))


def _album_name(a):
    nm = a.get("name")
    if nm:
        return nm
    aid = str(a.get("albumId"))
    if aid == "1":
        return "(全部照片)"
    if aid == "2":
        return "(回收站)"
    return "(未命名 id=%s)" % aid


def probe_gallery(c):
    print("\n【相册】")
    albums, page = [], 0
    while True:
        res = c.get("/gallery/user/album/list", pageNum=page, pageSize=50,
                    isShared="false", numOfThumbnails=1)
        if res.get("result") != "ok":
            print("  列表接口返回异常：", res.get("result"), res.get("code"))
            return
        batch = (res.get("data") or {}).get("albums") or []
        albums.extend(batch)
        if len(batch) < 50:
            break
        page += 1
        if page > 40:
            break

    print("  相簿数：%d" % len(albums))
    for a in albums:
        print("    - %-26s id=%-20s 条目=%s"
              % (_album_name(a)[:26], a.get("albumId"), a.get("mediaCount")))

    # 全量点一遍（只读元数据，不下载任何文件）
    print("\n  正在逐页统计全部条目 …")
    total, total_bytes = 0, 0
    by_type, first_ts, last_ts = {}, None, None
    tag, pages = 0, 0
    while True:
        res = c.get("/gallery/allitems", groupId=1, tag=tag, limit=100,
                    simpleResult="false", withThumbnail="false")
        if res.get("result") != "ok":
            print("  条目接口返回异常：", res.get("result"), res.get("code"))
            break
        data = res.get("data") or {}
        content = data.get("content") or []
        total += len(content)
        for it in content:
            total_bytes += int(it.get("size") or 0)
            t = it.get("type") or "?"
            by_type[t] = by_type.get(t, 0) + 1
            ts = it.get("dateTaken") or it.get("sortTime")
            if ts:
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
        pages += 1
        if pages % 10 == 0:
            print("    …已过 %d 页，累计 %d 条 / %.1f MB"
                  % (pages, total, total_bytes / 1048576.0))
        if data.get("lastPage") or not content:
            break
        nxt = data.get("syncTag")
        if nxt is None or str(nxt) == str(tag):
            break
        tag = nxt
        if pages > 400:
            print("    达到页数上限，停止")
            break

    import datetime
    def fmt(ts):
        return datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else "?"
    print("\n  统计结果：")
    print("    条目总数：%d" % total)
    print("    总体积：%.2f GB（%d 字节）" % (total_bytes / 1073741824.0, total_bytes))
    print("    按类型：%s" % ", ".join("%s=%d" % kv for kv in sorted(by_type.items())))
    print("    时间跨度：%s ~ %s" % (fmt(first_ts), fmt(last_ts)))
    if albums and albums[0].get("mediaCount"):
        print("    接口自报总数：%s（应与上面条目总数一致）" % albums[0].get("mediaCount"))


def probe_drive(c):
    print("\n【云盘】")
    res = c.get("/drive/v2/user/folders/children", parentId=0, pageNo=1, limit=100, type="")
    if res.get("result") != "ok":
        print("  列表接口返回异常：", res.get("result"), res.get("code"))
        return
    data = res.get("data") or {}
    recs = data.get("records") or []
    folders = [x for x in recs if x.get("type") == "folder"]
    files = [x for x in recs if x.get("type") != "folder"]
    print("  根目录：%d 项（文件夹 %d，文件 %d），allCount=%s"
          % (len(recs), len(folders), len(files), data.get("allCount")))
    for x in recs:
        size = x.get("size") or 0
        print("    - %-28s %-6s %s"
              % ((x.get("name") or "")[:28], x.get("type"),
                 ("%.1f MB" % (size / 1048576.0)) if size else ""))


def main():
    print("=" * 60)
    print("小米云服务 只读探测")
    print("=" * 60)
    s = make_session()
    token = None

    if os.path.exists(SESS_FILE):
        try:
            token = load_session(s)
            if token and verify_session(s):
                print("复用上次会话：有效，无需扫码")
            else:
                token = None
                print("上次会话已失效，需要重新扫码")
        except Exception:
            token = None

    if token is None:
        try:
            user_id, pass_token, jar = read_local_auth()
            print("本机登录态：已找到 userId / passToken（共 %d 条 cookie）" % len(jar))
            for (host, name), val in jar.items():
                s.cookies.set(name, val, domain=host.lstrip("."), path="/")
            token = service_login(s, user_id, pass_token)
            print("静默登录：成功")
        except (SystemExit, Exception) as e:
            print("静默登录不可用（%s），改用扫码登录。"
                  % (e if isinstance(e, SystemExit) else type(e).__name__))
            s = make_session()          # 换干净会话，避免残留 cookie 干扰
        if token is None:
            token = qr_login(s)
        save_session(s)
        print("会话已加密保存到 session.dat（下次免扫）")

    c = Cloud(s, token)
    probe_gallery(c)
    probe_drive(c)
    print("\n探测结束，未下载任何文件，未修改云端数据。")


if __name__ == "__main__":
    main()
