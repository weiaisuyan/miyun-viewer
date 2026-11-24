# -*- coding: utf-8 -*-
"""四模块接口验证：相册 / 便签 / 录音 / 云盘。只读，不下载、不改云端数据。"""
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe import (API, SESS_FILE, load_session, make_session, qr_login,
                   save_session, verify_session)

def show(title, obj, n=6):
    print("\n--- %s ---" % title)
    if isinstance(obj, list):
        print("条数:", len(obj))
        for x in obj[:n]:
            print("  ", json.dumps(x, ensure_ascii=False)[:220])
    else:
        print(json.dumps(obj, ensure_ascii=False)[:1200])

def main():
    s = make_session()
    s.headers["Referer"] = "https://i.mi.com/"
    tok = None
    if os.path.exists(SESS_FILE):
        try:
            tok = load_session(s)
        except Exception:
            tok = None
        if not (tok and verify_session(s)):
            s = make_session()
            s.headers["Referer"] = "https://i.mi.com/"
            tok = None
    if not tok:
        print("需要扫码登录一次（生成 session.dat 后长期免扫）")
        tok = qr_login(s)
        save_session(s)
    print("会话就绪：serviceToken %s..." % tok[:10])

    # 1) 图片/相册
    r = s.get(API + "/gallery/user/album/list",
              params={"pageNum": 0, "pageSize": 5, "isShared": "false",
                      "numOfThumbnails": 1}, timeout=30)
    show("相册 album/list", r.json())

    # 2) 便签
    r = s.get(API + "/note/full/page", params={"ts": 0, "limit": 5}, timeout=30)
    show("便签 note/full/page", r.json())

    # 3) 录音
    r = s.get(API + "/sfs/ns/recorder/dir/0/list",
              params={"offset": 0, "limit": 20, "_dc": int(time.time() * 1000)},
              timeout=40)
    print("\n--- 录音 dir/0/list ---")
    print("HTTP", r.status_code)
    try:
        j = r.json()
    except Exception:
        print(r.text[:400])
        j = None
    if j:
        print(json.dumps({k: v for k, v in j.items() if not isinstance(v, list)},
                         ensure_ascii=False)[:500])
        data = j.get("data") or j
        if isinstance(data, dict):
            print("data 键:", list(data.keys())[:20])
            for k, v in data.items():
                if isinstance(v, list):
                    print("列表字段 %s: %d 条" % (k, len(v)))
                    for x in v[:5]:
                        print("   ", json.dumps(x, ensure_ascii=False)[:300])
                else:
                    print("%s = %r" % (k, v))

    # 4) 云盘
    r = s.get(API + "/drive/v2/user/folders/children",
              params={"parentId": 0, "pageNo": 1, "limit": 30, "type": ""},
              timeout=30)
    show("云盘 folders/children", r.json())

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
