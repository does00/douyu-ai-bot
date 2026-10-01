#!/usr/bin/env python3
"""斗鱼 STT 协议基础：封包/解包、转义、vk 签名。
逻辑摘自 douyu-monitor-wall（stream_server.py），整理为独立模块。
"""
import hashlib
import struct


# wsproxy 发弹幕 loginreq 的 vk 签名密钥（2026-09-28 从官方弹幕 JS 逆向验证）
# 公式：vk = md5(rt + SECRET + devid)，rt 为秒级时间戳
VK_SECRET = 'r5*^5;}2#${XF[h+;\'./.Q\'1;,-]f\'p['

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 弹幕接收端点（9503 已废弃，现行 8501~8506，必须 wss）
DM_ENDPOINTS = [f"wss://danmuproxy.douyu.com:850{i}/" for i in range(1, 7)]
# 弹幕发送网关
SEND_ENDPOINT = "wss://wsproxy.douyu.com:6675/"


def pack_frame(payload: str, ptype: int = 689) -> bytes:
    """12 字节头 STT 帧：[int32 bodyLen]x2 + [uint16 协议号] + [2B 零] + payload + \\0"""
    body = 8 + len(payload.encode("utf-8")) + 1
    return (struct.pack("<ii", body, body) + struct.pack("<H", ptype)
            + b"\x00\x00" + payload.encode("utf-8") + b"\x00")


def unpack_frames(buf: bytes):
    """从二进制缓冲里拆出 STT 文本帧列表。"""
    out = []
    off = 0
    while off + 12 <= len(buf):
        blen = int.from_bytes(buf[off:off + 4], "little")
        total = blen + 4
        if blen < 9 or off + total > len(buf):
            break
        out.append(buf[off + 12:off + total - 1].decode("utf-8", "ignore"))
        off += total
    return out


def stt_escape(v: str) -> str:
    """发送侧转义：@ -> @A，/ -> @S，防止用户内容注入协议字段。"""
    return v.replace("@", "@A").replace("/", "@S")


def stt_unescape(v: str) -> str:
    """接收侧反转义。"""
    return v.replace("@S", "/").replace("@A", "@")


def parse_stt(body: str) -> dict:
    """把 type@=chatmsg/nn@=xxx/... 解析成 dict（值做反转义）。"""
    d = {}
    for part in body.split("/"):
        if "@=" in part:
            k, v = part.split("@=", 1)
            d[k] = stt_unescape(v)
    return d


def calc_vk(rt: str, devid: str) -> str:
    return hashlib.md5((rt + VK_SECRET + devid).encode("utf-8")).hexdigest()


def recv_loginreq(rid: str) -> str:
    """弹幕接收用老格式登录包（带 ver/dfl 的新格式实测无响应）。"""
    return f"type@=loginreq/roomid@={rid}/"


def recv_joingroup(rid: str) -> str:
    return f"type@=joingroup/rid@={rid}/gid@=-9999/"


def recv_heartbeat() -> str:
    return "type@=mrkl/"


def send_loginreq(rid: str, f: dict, devid: str) -> str:
    """发弹幕网关 loginreq：字段不全/devid 与 Cookie 不一致会被踢。"""
    rt = str(int(__import__("time").time()))
    vk = calc_vk(rt, devid)
    return (
        f"type@=loginreq/roomid@={rid}/dfl@=sn@AA=105@ASss@AA=1/"
        f"username@={f.get('acf_username', '')}/password@=/"
        f"ltkid@={f.get('acf_ltkid', '')}/biz@=1/stk@={f.get('acf_stk', '')}/"
        f"devid@={devid}/ct@={f.get('acf_ct', '0') or '0'}/"
        f"pt@=2/cvr@=0/tvr@=7/apd@=/jwt@={f.get('acf_dmjwt_token', '')}/"
        f"rt@={rt}/vk@={vk}/ver@=20220825/aver@=218101901/dmbt@=electron/dmbv@=41/"
    )


def send_chatmessage(content: str, devid: str, uid: str) -> str:
    now_ms = int(__import__("time").time() * 1000)
    return (
        f"pe@=0/content@={stt_escape(content)}/col@=0/type@=chatmessage/dy@={devid}/"
        f"sender@={uid}/ifs@=0/nc@=0/dat@=0/rev@=0/"
        f"tts@={now_ms // 1000}/admzq@=0/cst@={now_ms}/"
    )
