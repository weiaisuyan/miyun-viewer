# -*- coding: utf-8 -*-
"""小米 keybag 客户端：换取 appKey，用于解密端到端加密（E2EE）的照片和笔记。

链路（照抄客户端 compiled/renderer/common.js 的实现）
-----------------------------------------------------
1. GET  /mic/keybag/v1/getEncInfo?hsid=2&appId=micloud
        -> zone / serverSignZone / nonce / maxAppkey.appKeyVersion
2. 本地生成 RSA-2048 密钥对（RSA-OAEP，SHA-1）
3. 自身公钥导出成 tpubString：标准 spki base64 去掉前 32 字符（"MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A"），
   每 64 字符一行，套 "-----BEGIN RSA PUBLIC KEY-----"
4. HSM 公钥 = 代码里的常量表 HSM_KEYS[zone]（前缀 MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A）
5. clientSign = base64url_nopad( SHA1( "hsid~userId~level~nonce~serverSignZone~tpubString~encryptToken" 的 hex ) )
6. 随机 32 字节 AES 密钥 + 12 字节 IV + 16 字节 AAD；
   AES-GCM 加密上面那坨字段；再用 HSM 公钥 RSA-OAEP 加密 AES 密钥；
   拼成 "zone~rsaKey~iv~data~aad" = clientTokenTicket
7. POST /mic/keybag/v1/getAppkeyByToken  {clientTokenTicket, nameSpace, nonce, tpubString, ...}
        -> tokenAppkeyTicket / tephemeralKey / ephemeralKeyIv / ephemeralKeyAad
8. 用自己的私钥 RSA-OAEP 解 tephemeralKey 得到 AES 密钥，再 AES-GCM 解 tokenAppkeyTicket
        -> { encryptToken, appKey: [{appKeyVersion, appKey}] }
"""
import base64
import hashlib
import json
import os
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

API = "https://i.mi.com"
HSID = 2
APP_ID = "micloud"
CLIENT_SECURE_LEVEL = 900000
TAG = "1"
SPKI_PREFIX = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A"

try:
    from paths import res_dir as _res_dir
    _RES = _res_dir()
except Exception:
    _RES = os.path.dirname(os.path.abspath(__file__))

_HSM_FILE = os.path.join(_RES, "cache", "hsm_keys.json")


def _load_hsm():
    try:
        with open(_HSM_FILE, encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}


# 客户端 common.js 里的 HSM 公钥常量表 v[zone]（已导出到 cache/hsm_keys.json）
HSM_KEYS = _load_hsm()


# ------------------------------------------------------------------ 编码
def b64u(b):
    """bytes -> base64url（去掉 '='，+ / 换成 - _）"""
    return (base64.b64encode(b).decode("ascii")
            .rstrip("=").replace("+", "-").replace("/", "_"))


def b64u_dec(s):
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))


def sha1_hex_bytes(text):
    return hashlib.sha1(text.encode("utf-8")).digest()


# ------------------------------------------------------------------ RSA
def make_keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


def tpub_string(pub):
    """导出成小米约定的 tpubString（去掉 spki 标准头）。"""
    der = pub.public_bytes(serialization.Encoding.DER,
                           serialization.PublicFormat.SubjectPublicKeyInfo)
    b64 = base64.b64encode(der).decode("ascii")
    if b64.startswith(SPKI_PREFIX):
        b64 = b64[len(SPKI_PREFIX):]
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return ("-----BEGIN RSA PUBLIC KEY-----\n" + "\n".join(lines)
            + "\n-----END RSA PUBLIC KEY-----\n")


def hsm_public_key(zone):
    if zone not in HSM_KEYS:
        raise RuntimeError("没有 zone=%s 的 HSM 公钥常量" % zone)
    der = base64.b64decode(SPKI_PREFIX + HSM_KEYS[zone])
    return serialization.load_der_public_key(der)


# ------------------------------------------------------------------ 加封
def seal(payload_obj, hsm_pub):
    """把 payload 加密成 "rsaKey~iv~data~aad"（不含前面的 zone）。"""
    key = os.urandom(32)
    iv = os.urandom(12)
    aad = os.urandom(16)
    data = AESGCM(key).encrypt(iv, json.dumps(payload_obj).encode("utf-8"), aad)
    rsa_enc = hsm_pub.encrypt(key, padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA1()),
        algorithm=hashes.SHA1(), label=None))
    return b64u(rsa_enc), b64u(iv), b64u(data), b64u(aad)


def unseal(ticket, priv, tephemeral, eph_iv, eph_aad):
    """解开服务端返回的 tokenAppkeyTicket。

    注意和 clientTokenTicket 不一样：这里 ticket 是**一整段 base64**，
    不是 "x~a~b~c~d" 五段。五段那个是我们发给服务端的。
    """
    key = priv.decrypt(b64u_dec(tephemeral), padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA1()),
        algorithm=hashes.SHA1(), label=None))
    inner = AESGCM(key).decrypt(b64u_dec(eph_iv), b64u_dec(ticket),
                                b64u_dec(eph_aad))
    return json.loads(inner.decode("utf-8"))


# ------------------------------------------------------------------ 主流程
def enc_info(session):
    r = session.get(API + "/mic/keybag/v1/getEncInfo",
                    params={"hsid": HSID, "appId": APP_ID}, timeout=25)
    return (r.json() or {}).get("data") or {}


def _sign(fields):
    """clientSign = base64url( SHA-256("字段1~字段2~...") 的原始 32 字节 )

    注意：客户端用的是 SHA-256（common.js 里 b.c），不是 shaType 参数里的 SHA1
    —— 那个 SHA1 只约束 RSA-OAEP。
    """
    return b64u(hashlib.sha256("~".join(str(x) for x in fields).encode("utf-8")).digest())


def get_appkeys(session, service_token, user_id, name_space="gallery",
                encrypt_token="", last_version=0, debug=False):
    """走完 keybag 链路，返回服务端响应 dict（含 tokenAppkeyTicket 等）。"""
    info = enc_info(session)
    zone = info["zone"]
    ssz = info["serverSignZone"]
    nonce = info["nonce"]
    priv, pub = make_keypair()
    tpub = tpub_string(pub)

    fields = [HSID, user_id, CLIENT_SECURE_LEVEL, nonce, ssz, tpub, encrypt_token]
    sign = _sign(fields)
    payload = {
        "hsid": HSID, "userId": user_id, "encryptToken": encrypt_token,
        "clientSign": sign, "clientSecureLevel": CLIENT_SECURE_LEVEL,
        "tpubString": tpub, "nonce": nonce, "tag": TAG,
        "serverSignZone": ssz,
        "clientSignField": ["hsid", "userId", "clientSecureLevel", "nonce",
                            "serverSignZone", "tpubString", "encryptToken"],
    }
    hsm = hsm_public_key(zone)
    rsa_enc, iv, data, aad = seal(payload, hsm)
    ticket = "~".join([str(zone), rsa_enc, iv, data, aad])

    body = {"hsid": HSID, "shaType": "SHA1", "nameSpace": name_space,
            "clientTokenTicket": ticket, "appId": APP_ID, "nonce": nonce,
            "tpubString": tpub, "clientSecureLevel": CLIENT_SECURE_LEVEL,
            "lastAppKeyVersion": last_version}
    r = session.post(API + "/mic/keybag/v1/getAppkeyByToken",
                     data={"data": json.dumps(body), "serviceToken": service_token},
                     timeout=30)
    if debug:
        print("  getAppkeyByToken -> HTTP %d" % r.status_code)
        print("  body:", r.text[:400])
    j = r.json()
    j["_private_key"] = priv
    j["_tpub"] = tpub
    return j


def unwrap_appkeys(resp):
    """解包 getAppkeyByToken 的响应，返回 (encryptToken, [{appKeyVersion, appKey}])。"""
    d = resp.get("data") or {}
    ticket = d.get("tokenAppkeyTicket")
    if not ticket:
        return None, []
    inner = unseal(ticket, resp["_private_key"], d.get("tephemeralKey"),
                   d.get("ephemeralKeyIv"), d.get("ephemeralKeyAad"))
    return inner.get("encryptToken"), inner.get("appKey") or []
