# -*- coding: utf-8 -*-
"""用一次验证码换来 appKey，之后就不需要再验证了。

用法：
    python -I bind_appkey.py push        # 向可信设备推送验证码
    python -I bind_appkey.py 123456      # 用验证码换取 appKey 并存盘

换到的 appKey 是 32 字节，长期有效（appKeyVersion 不变就一直能用），
存到 cache/appkeys.json，程序以后解密照片直接用，不用再打扰你。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import client_auth          # noqa: E402
import keybag               # noqa: E402
import probe                # noqa: E402

CACHE = os.path.join(BASE, "cache", "appkeys.json")


def session_and_cookies():
    kv = client_auth.get_client_cookies(BASE, verbose=False)
    if not kv:
        raise SystemExit("拿不到登录态：请先确认「小米云服务」客户端在运行")
    s = probe.make_session()
    s.headers["Referer"] = "https://i.mi.com/"
    client_auth.apply_to_session(s, kv)
    return s, kv


def push():
    s, kv = session_and_cookies()
    r = s.post(keybag.API + "/mic/keybag/v1/keybagCaptchaPush",
               data={"data": json.dumps({"hsid": keybag.HSID, "from": "web"}),
                     "serviceToken": kv["serviceToken"]}, timeout=30)
    print("推送结果：HTTP %d  %s" % (r.status_code, r.text[:200]))
    return 0 if r.json().get("code") == 0 else 1


def exchange(captcha):
    s, kv = session_and_cookies()
    uid = int(kv["userId"]) if str(kv["userId"]).isdigit() else kv["userId"]
    info = keybag.enc_info(s)
    zone, ssz, nonce = info["zone"], info["serverSignZone"], info["nonce"]
    print("zone=%s serverSignZone=%s nonce=%s..." % (zone, ssz, nonce[:14]))

    priv, pub = keybag.make_keypair()
    tpub = keybag.tpub_string(pub)
    fields = [keybag.HSID, uid, keybag.CLIENT_SECURE_LEVEL, keybag.TAG, tpub, captcha]
    payload = {
        "hsid": keybag.HSID, "userId": uid, "captchaCode": captcha,
        "clientSign": keybag._sign(fields), "nonce": nonce, "tpubString": tpub,
        "clientSecureLevel": keybag.CLIENT_SECURE_LEVEL,
        "serverSignZone": ssz, "tag": keybag.TAG,
        "clientSignField": ["hsid", "userId", "clientSecureLevel", "tag",
                            "tpubString", "captchaCode"],
    }
    rsa_enc, iv, data, aad = keybag.seal(payload, keybag.hsm_public_key(zone))
    ticket = "~".join([str(zone), rsa_enc, iv, data, aad])
    body = {"hsid": keybag.HSID, "shaType": "SHA1", "nameSpace": "gallery",
            "clientTicket": ticket, "appId": keybag.APP_ID, "nonce": nonce,
            "tpubString": tpub, "clientSecureLevel": keybag.CLIENT_SECURE_LEVEL}
    r = s.post(keybag.API + "/mic/keybag/v1/getAppkeyTokenByCaptcha",
               data={"data": json.dumps(body), "serviceToken": kv["serviceToken"]},
               timeout=30)
    j = r.json()
    print("兑换结果：HTTP %d  code=%s  %s" % (r.status_code, j.get("code"),
                                              j.get("description")))
    if j.get("code") != 0:
        print(r.text[:400])
        return 1

    # 先把响应原样存下来：验证码一次性的，万一解包出错不至于白费
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    dump = os.path.join(os.path.dirname(CACHE), "last_keybag_resp.json")
    with open(dump, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
    print("原始响应已存", dump)
    d = j.get("data") or {}
    for k, v in d.items():
        if isinstance(v, str):
            print("   %-20s len=%d" % (k, len(v)))
        else:
            print("   %-20s %s" % (k, v))

    j["_private_key"] = priv
    et, aks = keybag.unwrap_appkeys(j)
    print("\n✅ encryptToken：%d 字符" % len(et or ""))
    print("✅ appKey %d 条：" % len(aks))
    for a in aks:
        print("    version=%s  len=%s" % (a.get("appKeyVersion"), len(a.get("appKey") or "")))
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump({"encryptToken": et, "appKeyList": aks},
              open(CACHE, "w", encoding="utf-8"))
    print("已保存", CACHE)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    arg = sys.argv[1]
    if arg == "bind":
        # 推送后立刻等输入，避免验证码在往返途中过期
        if push() != 0:
            sys.exit(1)
        code = input("请输入手机上收到的 6 位验证码：").strip()
        sys.exit(exchange(code))
    sys.exit(push() if arg == "push" else exchange(arg))
