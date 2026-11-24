# -*- coding: utf-8 -*-
"""从小米云服务客户端进程内存里「只读」取它当前的登录 cookie。

为什么这么做
------------
扫码拿到的 serviceToken 是「普通设备」会话票，服务端很快就作废，
所以程序老弹二维码。而小米云服务客户端持有「受信任设备」会话
（trust_device_auth_token + i.mi.com_istrudev），并且服务端每响应一次
就 set-cookie 换一张新票，只要客户端在跑，它的票就永不过期。
我们把那整套 cookie 取过来用，就彻底免扫码。

安全边界
--------
只调用 OpenProcess(PROCESS_VM_READ) + ReadProcessMemory：
不注入、不挂起、不写、不杀进程、不改客户端任何文件。

取到的凭据只在内存里用，落盘走 DPAPI（与 probe.save_session 同一套）。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import time

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

CLIENT_NAME = "小米云服务"
MAX_REGION = 256 * 1024 * 1024       # 单个内存区超过这个跳过的，避免撑爆内存
MAX_SCAN = 1600 * 1024 * 1024        # 每个进程最多扫这么多

# 一条完整 cookie 串：... ; ... ; serviceToken=xxxx
CS_RE = re.compile(rb"(?:[A-Za-z0-9_.\-]{1,40}=[^;\x00-\x1f]{1,400};\s*){1,12}"
                   rb"serviceToken=[A-Za-z0-9+/=]{100,600}")


class _MBI64(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong),
                ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD),
                ("__alignment1", wt.DWORD),
                ("RegionSize", ctypes.c_ulonglong),
                ("State", wt.DWORD),
                ("Protect", wt.DWORD),
                ("Type", wt.DWORD),
                ("__alignment2", wt.DWORD)]


_k32 = ctypes.windll.kernel32


# ------------------------------------------------------------------ 进程
# 打包成 console=False 的窗口程序后，子进程默认会弹一个黑色控制台窗口。
# client_pids() 在每个 401 上都会被调用，不抑制就是「窗口一直闪」。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def client_pids():
    """返回正在运行的「小米云服务」客户端进程 PID 列表。"""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=30,
                             creationflags=_NO_WINDOW).stdout
    except Exception:
        return []
    txt = out.decode("gbk", "replace")
    pids = []
    for line in txt.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) >= 2 and CLIENT_NAME in cols[0]:
            try:
                pids.append(int(cols[1]))
            except ValueError:
                pass
    return pids


def _read(h, base, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = _k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size,
                                ctypes.byref(got))
    return buf.raw[:got.value] if ok else b""


def _scan_pid(pid):
    """扫一个进程，返回它内存里出现的所有 cookie 串（bytes）。"""
    h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return []
    found = []
    addr = 0
    scanned = 0
    mbi = _MBI64()
    try:
        while scanned < MAX_SCAN:
            if not _k32.VirtualQueryEx(ctypes.c_void_p(h), ctypes.c_void_p(addr),
                                       ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            ok = (mbi.State == MEM_COMMIT
                  and not mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS)
                  and mbi.RegionSize < MAX_REGION)
            if ok:
                data = _read(h, mbi.BaseAddress, mbi.RegionSize)
                scanned += len(data)
                if data and b"serviceToken" in data:
                    for m in CS_RE.finditer(data):
                        found.append(m.group(0))
            addr = mbi.BaseAddress + mbi.RegionSize
    finally:
        _k32.CloseHandle(h)
    return found


# ------------------------------------------------------------------ 解析 / 验证
def parse_cookie_string(cs):
    """'a=1; b=2; serviceToken=x' -> dict"""
    if isinstance(cs, bytes):
        cs = cs.decode("latin-1")
    kv = {}
    for part in cs.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v and "%" not in v:
                kv[k] = v
    return kv


def apply_to_session(session, kv):
    for k, v in kv.items():
        try:
            session.cookies.set(k, v, domain="i.mi.com", path="/")
        except Exception:
            pass
    return session


def verify(kv, timeout=20):
    """拿这套 cookie 打一个只读接口，200 才算可用。"""
    import requests
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/130.0.0.0 Safari/537.36"),
        "Referer": "https://i.mi.com/",
    })
    apply_to_session(s, kv)
    try:
        r = s.get("https://i.mi.com/status/lite/profile", timeout=timeout)
    except Exception:
        return False
    return r.status_code == 200 and r.text.lstrip().startswith("{")


# ------------------------------------------------------------------ 缓存
# 内存缓存：验证过的票在 TTL 内直接复用。
# 为什么必须有它：客户端那张 serviceToken 每次被拿去请求，服务端都可能轮换成新票，
# 客户端手里那份就作废了。原来每个 401 都扫一遍 9 个进程、拿 4 套票各打一次验证，
# 一天下来足以把客户端自己的登录态耗掉。
_MEM = {"base": None, "kv": None, "at": 0.0}
MEM_TTL = 600                      # 10 分钟


def _mem_get(base):
    if _MEM["kv"] and _MEM["base"] == base and (time.time() - _MEM["at"]) < MEM_TTL:
        return _MEM["kv"]
    return None


def _mem_set(base, kv):
    _MEM.update({"base": base, "kv": kv, "at": time.time()})


def invalidate(base=None):
    """作废内存缓存：这套票被服务端拒了，下次必须重新扫。"""
    _MEM.update({"base": None, "kv": None, "at": 0.0})


def _cache_path(base):
    return os.path.join(base, "cache", "client_cookies.bin")


def _cache_save(base, kv):
    try:
        sys.path.insert(0, base)
        from probe import dpapi_protect
        os.makedirs(os.path.join(base, "cache"), exist_ok=True)
        with open(_cache_path(base), "wb") as f:
            f.write(dpapi_protect(json.dumps(kv).encode("utf-8")))
    except Exception:
        pass


def _cache_load(base):
    try:
        sys.path.insert(0, base)
        from probe import dpapi_unprotect
        with open(_cache_path(base), "rb") as f:
            return json.loads(dpapi_unprotect(f.read()).decode("utf-8"))
    except Exception:
        return None


# ------------------------------------------------------------------ 主入口
def get_client_cookies(base=None, use_cache=True, verbose=False, max_verify=3):
    """返回一套现在能用的 cookie dict；失败返回 None。

    max_verify：最多拿几套「不同的票」去服务器验证。

    为什么是 3 而不是 1
    -------------------
    客户端内存里同时躺着好几张 serviceToken：有它现在正在用的那张，也有
    以前会话留下的死票。死票和活票长得一模一样（都是 384 字符、字段齐全），
    光看内存分不出来。以前只验 1 张，选哪张全看排序，抽到死票就直接退扫码页。

    为什么不能无限验
    ----------------
    每验证一次，服务端可能把这张票轮换成新的，客户端手里那份就作废，它得靠
    silentRelogin 自己救回来。验太多次会把客户端的登录态耗掉（11-19 就是这么
    搞坏的）。3 次是「命中率」和「消耗」之间的折中：5 张候选里验 3 张，
    基本必中，代价可控。

    另外：已经验证失败过的票不会再验第二次（见下面的 tried）。
    """
    def log(*a):
        if verbose:
            print(*a)

    base = base or os.path.dirname(os.path.abspath(__file__))

    # 1) 内存缓存：10 分钟内验证过的票直接用，不扫进程、不打请求
    if use_cache:
        kv = _mem_get(base)
        if kv:
            log("[client_auth] 内存缓存可用（%.0f 秒前验证）" % (time.time() - _MEM["at"]))
            return kv

    # 验证预算：整个函数最多打这么多次验证请求（含磁盘缓存那次）
    budget = max(1, int(max_verify))
    # 已经确认不能用的票，后面不再浪费请求去验
    tried = set()

    if use_cache:
        kv = _cache_load(base)
        if kv:
            budget -= 1                     # 验磁盘缓存也要花一次
            if verify(kv):
                log("[client_auth] 磁盘缓存可用")
                _mem_set(base, kv)
                return kv
            # 磁盘上这张已经废了：删掉，免得下次启动又白验一次
            tried.add(kv.get("serviceToken") or "")
            log("[client_auth] 磁盘缓存已失效，删除")
            try:
                os.remove(_cache_path(base))
            except OSError:
                pass

    pids = client_pids()
    if not pids:
        log("[client_auth] 小米云服务客户端没在运行")
        return None
    log("[client_auth] 客户端进程:", pids)

    seen = set()          # 整条 cookie 串去重
    by_token = {}         # serviceToken -> (score, kv)，按票去重
    for pid in pids:
        for cs in _scan_pid(pid):
            if cs in seen:
                continue
            seen.add(cs)
            kv = parse_cookie_string(cs)
            tok = kv.get("serviceToken")
            if not tok:
                continue
            # 带受信任设备票的优先（它才是长命的那个），字段多的优先
            score = (1 if "trust_device_auth_token" in kv else 0,
                     len(kv))
            prev = by_token.get(tok)
            if prev is None or score > prev[0]:
                by_token[tok] = (score, kv)
        log("  pid %-6d 命中 %d 条" % (pid, sum(1 for c in seen)))

    ordered = sorted(by_token.values(), key=lambda x: x[0], reverse=True)
    # 跳过已经验证失败过的（磁盘缓存那张）
    ordered = [(s, kv) for s, kv in ordered
               if (kv.get("serviceToken") or "") not in tried]
    log("[client_auth] 去重后 %d 张不同的票，剩余验证预算 %d 次"
        % (len(ordered), budget))

    for i, (_, kv) in enumerate(ordered[:max(0, budget)]):
        if verify(kv):
            log("[client_auth] 第 %d 张可用，字段 %s" % (i, sorted(kv)))
            _cache_save(base, kv)
            _mem_set(base, kv)
            return kv
        log("[client_auth] 第 %d 张无效" % i)
    log("[client_auth] 没有可用的 cookie（客户端可能已登出）")
    return None


if __name__ == "__main__":
    kv = get_client_cookies(verbose=True)
    if kv:
        print("\nOK  字段:", sorted(kv))
        print("    serviceToken 长度:", len(kv.get("serviceToken", "")))
    else:
        print("\nFAIL")
