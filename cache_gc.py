# -*- coding: utf-8 -*-
"""缓存清理：给缩略图/原图/原视频/笔记图设总上限，超了按「最久没用」删最旧的。

为什么需要
----------
看图看图，缓存只增不减。全部照片 9175 张，缩略图全刷一遍就 1.1 GB
（实测单张 120 KB），原图单张 2 MB。用一年会堆到几个 GB，而且都是
早就看过的内容。这里做 LRU：只保留最近用过的，旧的自动淘汰。

两条独立的清理线
----------------
1. purge_lru()    —— 缓存总量超上限时，删最久没被访问的文件。
2. sweep_webview_temp() —— pywebview 把 WebView2 的 profile 建在 %TEMP%\tmpXXXX，
   程序退出时不会删（源码 winforms.py:757 只取了临时目录名，丢了清理器）。
   每次启动清掉自己以前留下的旧目录。

安全边界
--------
只删 CACHE_DIR 下的四个子目录，和 %TEMP% 下名字匹配我们前缀的目录；
不碰用户下载的文件、不碰登录票、不碰任何别的程序的数据。
"""
import os
import shutil
import threading
import time

# 缓存总量上限。超过就淘汰最久没访问的，直到降到水位以下。
CACHE_LIMIT = 3 * 1024 * 1024 * 1024        # 3 GB
CACHE_LOW = int(CACHE_LIMIT * 0.8)          # 一次清到 80%，避免频繁触发

# 参与 LRU 的子目录（cache/ 下面）。up_tmp 是上传中转，由上传流程自己删。
LRU_DIRS = ("thumbs", "full", "video", "noteimg", "rec_audio", "drive")

# WebView2 profile 目录前缀，认准了才敢删
WEBVIEW_PREFIX = "miyun-webview-"

_GC_LOCK = threading.Lock()


def _sub_dirs(cache_dir):
    return [os.path.join(cache_dir, d) for d in LRU_DIRS]


def usage(cache_dir):
    """返回 (总字节数, 文件数)。"""
    total = 0
    count = 0
    for d in _sub_dirs(cache_dir):
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                    count += 1
                except OSError:
                    pass
    return total, count


def stats(cache_dir):
    """给界面用的明细：每个子目录各占多少。"""
    out = {"total": 0, "count": 0, "parts": {}}
    for name in LRU_DIRS:
        d = os.path.join(cache_dir, name)
        size = 0
        n = 0
        if os.path.isdir(d):
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    try:
                        size += os.path.getsize(os.path.join(root, fn))
                        n += 1
                    except OSError:
                        pass
        out["parts"][name] = {"size": size, "count": n}
        out["total"] += size
        out["count"] += n
    return out


def touch(path):
    """标记「刚用过」：更新访问时间。

    Windows 10/11 默认关闭 NTFS 的 last access 更新（性能考虑），
    光读文件不会改 atime。LRU 靠 atime 判断新旧，所以缓存命中时
    必须自己补一刀，否则「最近看过的」会被当成旧的删掉。
    """
    try:
        os.utime(path, None)
    except OSError:
        pass


def purge_lru(cache_dir, limit=None, low=None):
    """缓存超限时按 atime 从旧到新删，直到总量降到 low 以下。

    limit/low 默认取模块级 CACHE_LIMIT/CACHE_LOW（应用会按设置动态更新）。
    返回 (删掉的字节数, 删掉的文件数)。
    用 atime（最后访问时间）而不是 mtime：读一次缓存就会更新 atime，
    所以「最近看过的」会被留下。Windows 默认开启 last access 更新。
    """
    limit = CACHE_LIMIT if limit is None else int(limit)
    low = (int(limit * 0.8) if low is None else int(low))
    with _GC_LOCK:
        total, _ = usage(cache_dir)
        if total <= limit:
            return 0, 0
        # 收集所有文件，按访问时间排序（最旧的在前）
        entries = []
        for d in _sub_dirs(cache_dir):
            if not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    p = os.path.join(root, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    # 用 atime 和 mtime 里较新的那个：有些文件系统不更新 atime
                    ts = max(st.st_atime, st.st_mtime)
                    entries.append((ts, st.st_size, p))
        entries.sort(key=lambda x: x[0])
        freed = 0
        removed = 0
        for _ts, size, p in entries:
            if total - freed <= low:
                break
            try:
                os.remove(p)
                freed += size
                removed += 1
            except OSError:
                pass
        return freed, removed


def clear_all(cache_dir):
    """手动「清空缓存」：删光四个缓存子目录里的所有文件，保留目录本身。

    返回 (释放字节数, 删除文件数)。正在被占用的文件（比如正在播的视频）
    会跳过，不报错。
    """
    with _GC_LOCK:
        freed = 0
        removed = 0
        for d in _sub_dirs(cache_dir):
            if not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d, topdown=False):
                for fn in files:
                    p = os.path.join(root, fn)
                    try:
                        size = os.path.getsize(p)
                        os.remove(p)
                        freed += size
                        removed += 1
                    except OSError:
                        pass
                # 顺手清掉空子目录（不删顶层目录本身）
                if os.path.abspath(root) != os.path.abspath(d):
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass
        return freed, removed


def webview_profile_dir():
    """WebView2 profile 的固定路径（放在 %TEMP% 下，名字带我们的前缀）。

    用固定路径而不是 pywebview 默认的 tempfile.mkdtemp()：
      * 退出时 pywebview 的 clear_user_data() 能删掉确定的路径；
      * 就算被强杀没删掉，下次启动 sweep_webview_temp() 也认得出来。
    """
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or os.getcwd()
    return os.path.join(tmp, WEBVIEW_PREFIX + "profile")


def sweep_webview_temp(max_age=0):
    """清掉 %TEMP% 下我们自己的 WebView2 profile 残留。

    只删名字以 WEBVIEW_PREFIX 开头的目录。max_age 秒内的不删
    （默认 0 = 全删，因为启动时调用，此时本进程的 profile 还没建）。
    返回 (释放字节数, 删除目录数)。
    """
    tmp = os.environ.get("TEMP") or os.environ.get("TMP")
    if not tmp or not os.path.isdir(tmp):
        return 0, 0
    freed = 0
    removed = 0
    now = time.time()
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0, 0
    for name in names:
        if not name.startswith(WEBVIEW_PREFIX):
            continue
        p = os.path.join(tmp, name)
        if not os.path.isdir(p):
            continue
        if max_age:
            try:
                if now - os.path.getmtime(p) < max_age:
                    continue
            except OSError:
                continue
        size = 0
        try:
            for root, _dirs, files in os.walk(p):
                for fn in files:
                    try:
                        size += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
            shutil.rmtree(p, ignore_errors=True)
            if not os.path.exists(p):
                freed += size
                removed += 1
        except OSError:
            pass
    return freed, removed


def gc_in_background(cache_dir, limit=None):
    """开一个后台线程做 LRU 清理，不阻塞界面。"""
    def _run():
        try:
            freed, n = purge_lru(cache_dir, limit=limit)
            if n:
                print("[缓存] 超出上限，清掉 %d 个文件，释放 %.1f MB"
                      % (n, freed / 1048576.0))
        except Exception as e:
            print("[缓存] 清理失败：%s %s" % (type(e).__name__, e))
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    import sys
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    cd = os.path.join(base, "cache")
    st = stats(cd)
    print("总占用：%.1f MB / %d 个文件" % (st["total"] / 1048576.0, st["count"]))
    for k, v in st["parts"].items():
        print("  %-8s %8.1f MB  %d 个" % (k, v["size"] / 1048576.0, v["count"]))
