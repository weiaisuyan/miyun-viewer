# -*- coding: utf-8 -*-
"""路径约定：区分「只读资源」和「可写数据」。

源码运行时两者是同一个目录；
用 PyInstaller 打包成单文件 exe 后，只读资源被解到临时目录（sys._MEIPASS），
而登录票、appKey、缩略图缓存必须写在 exe 旁边，不能写进临时目录。
"""
import os
import sys


def data_dir():
    """可写目录：登录票、appKey、缩略图缓存都放这里。"""
    # 1) 环境变量（源码调试、壳进程传递）
    d = (os.environ.get("MIYUN_DATA_DIR") or "").strip()
    # 2) 命令行 --data-dir：WPF 壳直接传，避免 .cmd 按 GBK 解析中文路径
    if not d:
        for i, a in enumerate(sys.argv):
            if a == "--data-dir" and i + 1 < len(sys.argv):
                d = sys.argv[i + 1]
                break
            if a.startswith("--data-dir="):
                d = a.split("=", 1)[1]
                break
        d = (d or "").strip()
    if d and os.path.isdir(d):
        return d
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def res_dir():
    """只读资源目录：ui/、cache/hsm_keys.json。"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
