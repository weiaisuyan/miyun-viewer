# -*- coding: utf-8 -*-
"""从「小米云服务」客户端进程内存里读它正在用的 serviceToken（只读）。

用途：让我们的查看器复用官方客户端的登录态，避免反复扫码。
不写目标进程、不注入、不挂起，只调用 OpenProcess/ReadProcessMemory。
"""
import ctypes
import ctypes.wintypes as wt
import re
import sys

import requests

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong),
                ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD),
                ("__alignment1", wt.DWORD),
                ("RegionSize", ctypes.c_ulonglong),
                ("State", wt.DWORD),
                ("Protect", wt.DWORD),
                ("Type", wt.DWORD),
                ("__alignment2", wt.DWORD)]


k32 = ctypes.windll.kernel32
TOKEN_RE = re.compile(rb"[A-Za-z0-9+/=]{300,460}")


def pids_by_name(name_part):
    import subprocess
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                         capture_output=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                         ).stdout.decode("gbk", "replace")
    pids = []
    for line in out.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) >= 2 and name_part in cols[0]:
            pids.append(int(cols[1]))
    return pids


def read_region(h, base, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(got))
    return buf.raw[:got.value] if ok else b""


def scan_pid(pid, max_bytes=800 * 1024 * 1024):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return []
    found = []
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION64()
    scanned = 0
    try:
        while scanned < max_bytes:
            if not k32.VirtualQueryEx(ctypes.c_void_p(h), ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            if (mbi.State == MEM_COMMIT and not (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS))
                    and mbi.RegionSize < 256 * 1024 * 1024):
                data = read_region(h, mbi.BaseAddress, mbi.RegionSize)
                scanned += len(data)
                for m in TOKEN_RE.finditer(data):
                    found.append(m.group(0))
            addr = mbi.BaseAddress + mbi.RegionSize
    finally:
        k32.CloseHandle(h)
    return found


def main():
    pids = pids_by_name("小米")
    print("客户端进程:", pids)
    cands = set()
    for pid in pids:
        got = scan_pid(pid)
        print("  pid %-6d 候选串 %d" % (pid, len(got)))
        cands.update(got)
    print("去重后候选:", len(cands))
    # 只保留长度接近 364 且像 base64 的候选（我们的票是 364 字符 / 解码 272 字节）
    cands = {c for c in cands if 356 <= len(c) <= 372
             and sum(1 for ch in c if ch in b"+/") >= 10}
    print("长度筛选后:", len(cands))
    s = requests.Session()
    s.trust_env = False
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/130.0.0.0 Safari/537.36")
    for i, c in enumerate(list(cands)[:120]):
        s.cookies.clear()
        s.cookies.set("serviceToken", c.decode(), domain="i.mi.com", path="/")
        try:
            r = s.get("https://i.mi.com/status/lite/profile", timeout=15)
        except Exception:
            continue
        if r.status_code == 200:
            print("\n✅ 找到可用 serviceToken（第 %d 个候选，长 %d）" % (i, len(c)))
            print("   前 16 字符:", c[:16].decode())
            open("client_token.txt", "w").write(c.decode())
            return
    print("\n未找到可用 token")


if __name__ == "__main__":
    sys.exit(main())
