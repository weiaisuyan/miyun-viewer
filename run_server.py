# -*- coding: utf-8 -*-
"""调试用：只起本地 HTTP 服务（不开窗口），供浏览器预览验证界面。"""
import os, sys, threading
from http.server import ThreadingHTTPServer
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import app as A

A.ensure_login()
srv = ThreadingHTTPServer(("127.0.0.1", 8799), A.Handler)
print("serving on 8799")
srv.serve_forever()
