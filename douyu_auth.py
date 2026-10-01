#!/usr/bin/env python3
"""斗鱼扫码登录可复用模块：QRLoginSession 供 CLI（login.py）与 WebUI（webui.py）共用。

流程：start() 生成二维码 → 用户用斗鱼 APP 扫码 → poll_once() 轮询
→ done 时登录态写入 data/cookie.txt。
"""
import io
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

PROXY = os.environ.get("PROXY_URL", "").strip()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else {}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

URL_QR_START = "https://passport.douyu.com/scan/generateCode"
URL_QR_POLL = "https://passport.douyu.com/japi/scan/auth"


class QRLoginSession:
    """一次扫码登录会话。start() 后反复 poll_once() 直到 done/expired。

    cookie_path: 登录态写入路径（多账号时每个账号一个文件），
    默认 data/cookie.txt。
    """

    def __init__(self, cookie_path=None):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.s.proxies.update(PROXIES)
        self.cookie_path = Path(cookie_path) if cookie_path else DATA / "cookie.txt"
        self.code = ""
        self.qr_url = ""
        self.expire = 300
        self.deadline = 0.0
        self._scanned_announced = False

    def start(self):
        """生成二维码，返回 (qr_url, expire)。失败抛 RuntimeError。"""
        r = self.s.post(
            URL_QR_START, data={"client_id": "1", "isMultiAccount": "0"},
            headers={"Referer": "https://passport.douyu.com/index/login",
                     "Origin": "https://passport.douyu.com",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15)
        j = r.json()
        if j.get("error") != 0 or not j.get("data"):
            raise RuntimeError(f"生成二维码失败: {j}")
        self.qr_url = j["data"]["url"]
        self.code = j["data"]["code"]
        self.expire = j["data"].get("expire", 300)
        self.deadline = time.time() + self.expire
        (DATA / "qr_url.txt").write_text(self.qr_url, encoding="utf-8")
        return self.qr_url, self.expire

    def poll_once(self):
        """轮询一次。返回 (status, info)：
        waiting（未扫码）| scanned（已扫码待确认）| expired（过期）
        | done（成功，info=uid）| error（info=错误信息）。"""
        if time.time() > self.deadline:
            return "expired", "二维码已过期，请重新生成"
        try:
            r = self.s.get(
                URL_QR_POLL, params={"time": int(time.time() * 1000), "code": self.code},
                headers={"Referer": "https://passport.douyu.com/index/login"},
                timeout=15)
            j = r.json()
        except Exception as e:
            return "error", f"轮询异常: {e}"
        err = j.get("error")
        if err == -2:
            return "waiting", "等待扫码"
        if err == -1:
            return "expired", "二维码已过期，请重新生成"
        if err == 0:
            ticket_url = (j.get("data") or {}).get("url")
            if ticket_url:
                try:
                    self.s.get(ticket_url, params={"callback": "appClient_json_callback"},
                               headers={"Referer": "https://passport.douyu.com/index/login"},
                               timeout=15)
                except Exception:
                    pass
            pairs = [f"{c.name}={c.value}" for c in self.s.cookies
                     if c.domain and "douyu" in c.domain]
            keys = [p.split("=")[0] for p in pairs]
            if any(k in keys for k in ("acf_uid", "acf_auth")):
                self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
                self.cookie_path.write_text("; ".join(pairs), encoding="utf-8")
                uid = next((p.split("=", 1)[1] for p in pairs
                            if p.startswith("acf_uid=")), "")
                return "done", uid
            return "error", "换票后未拿到登录态，请重试"
        if not self._scanned_announced:
            self._scanned_announced = True
        return "scanned", "已扫码，请在手机上确认"

    @staticmethod
    def qr_png_bytes(url, box_size=8, border=2):
        """二维码内容 URL → PNG 字节。"""
        import qrcode
        img = qrcode.make(url, box_size=box_size, border=border)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def login_uid(cookie_path=None):
    """从 cookie 文件解析当前登录 uid，未登录返回 ''。"""
    p = Path(cookie_path) if cookie_path else DATA / "cookie.txt"
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith("acf_uid="):
            return part.split("=", 1)[1]
    return ""
