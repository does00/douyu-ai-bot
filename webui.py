#!/usr/bin/env python3
"""斗鱼 AI 弹幕机器人 — Web 管理后台（标准库实现，无额外依赖）。

功能：状态看板 / 房间号+触发词配置 / Gemini Key 配置（中继/直连切换）
      / 斗鱼扫码登录 / 服务重启 / 日志查看。

监听 127.0.0.1:18021，Basic Auth（admin / .env 中 WEBUI_PASSWORD）。
以 root 经 systemd 运行，需读写 config.yaml/.env/data 并 systemctl 重启服务。
"""
import base64
import hashlib
import html
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CONFIG_FILE = ROOT / "config.yaml"
ENV_FILE = ROOT / ".env"
LOG_FILE = DATA / "bot.log"

PORT = int(os.environ.get("WEBUI_PORT", "18021"))

VERSION = "1.1.7"
ADMIN_USER = "admin"
RELAY_URL_DEFAULT = "http://127.0.0.1:18020/generate"


# ---------- .env / config.yaml 读写 ----------
def read_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def write_env(updates):
    """updates: {key: value or None(删除)}，保留其他行。"""
    lines = []
    seen = set()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k = s.split("=", 1)[0].strip()
                if k in updates:
                    seen.add(k)
                    if updates[k] is not None:
                        lines.append(f"{k}={updates[k]}")
                    continue
            lines.append(line)
    for k, v in updates.items():
        if k not in seen and v is not None:
            lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)


def ensure_webui_password():
    env = read_env()
    pw = env.get("WEBUI_PASSWORD", "")
    if not pw:
        pw = secrets.token_hex(8)
        write_env({"WEBUI_PASSWORD": pw})
        print(f"[webui] 已生成管理密码（.env WEBUI_PASSWORD）: {pw}", flush=True)
    return pw


def write_config(cfg):
    CONFIG_FILE.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")


def svc_active(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def svc_restart(name):
    r = subprocess.run(["systemctl", "restart", name],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[:200]


def bot_apply_config():
    """让机器人应用新配置：优先 SIGHUP 热加载（现有房间监控不中断），
    失败时回退到 systemctl restart。返回 (mode, ok, desc)，
    mode 为 'reload' 或 'restart'。"""
    pid = None
    try:  # pidfile 优先（docker 双容器共享 data 卷也适用）
        pid = int((ROOT / "data" / "bot.pid").read_text().strip() or 0) or None
    except Exception:
        pid = None
    if not pid:
        try:
            r = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "douyu-ai-bot"],
                               capture_output=True, text=True, timeout=10)
            pid = int((r.stdout or "").strip() or 0) or None
        except Exception:
            pid = None
    if pid:
        try:
            os.kill(pid, signal.SIGHUP)
            return "reload", True, "热加载（监控未中断）"
        except Exception:
            pass
    ok, desc = svc_restart("douyu-ai-bot")
    return "restart", ok, desc


def recent_logs(n=30):
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    wanted = [l for l in lines if any(t in l for t in ("[hit]", "[send]", "[ai]", "[init]", "[recv]"))]
    return wanted[-n:]


_HIT_NEW = re.compile(r"\[hit\] \[房间(\d+)\] ([^:]{1,40}): (.*)")
_HIT_OLD = re.compile(r"\[hit\] ([^:]{1,40}): (.*)")


def recent_hits(n=30):
    """解析最近的命中记录：[{t, room, sender, content}]。"""
    if not LOG_FILE.exists():
        return []
    out = []
    for line in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _HIT_NEW.search(line)
        if m:
            room, sender, content = m.group(1), m.group(2), m.group(3)
        else:
            m2 = _HIT_OLD.search(line)
            if not m2:
                continue
            room, sender, content = "", m2.group(1), m2.group(2)
        t = line[1:17] if line.startswith("[") else ""
        out.append({"t": t, "room": room, "sender": sender, "content": content[:60]})
    return out[-n:][::-1]


def load_bot_config():
    """读 config.yaml 并做新旧兼容（复用 bot.normalize_config 逻辑的轻量版）。"""
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    if "rooms" not in cfg and cfg.get("room_id"):
        t = cfg.get("triggers", {})
        cfg["rooms"] = [{"id": str(cfg["room_id"]), "keywords": t.get("keywords", []),
                         "mode": t.get("mode", "contains")}]
    if "accounts" not in cfg:
        cfg["accounts"] = [{"name": "默认账号", "cookie_file": "data/cookie.txt"}]
    return cfg


def account_cookie_path(name):
    safe = re.sub(r"\W+", "_", name).strip("_") or "acct"
    return str(DATA / "cookies" / f"{safe}.txt")


def cookie_nickname(cookie_path):
    """从 cookie 文件读 acf_nickname（斗鱼显示昵称），取不到返回 ''。"""
    try:
        raw = Path(cookie_path).read_text(encoding="utf-8")
    except OSError:
        return ""
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith("acf_nickname="):
            return unquote(part.split("=", 1)[1])
    return ""


# ---------- 房间信息（主播名 + 开播状态，带缓存，后台逐个刷新） ----------
UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
ROOM_NAMES_FILE = DATA / "room_names.json"
ROOM_STATUS_FILE = DATA / "room_status.json"  # {room_id: {"live": bool, "ts": float}}，供 bot 看门狗读取
_room_names = {}
_room_names_lock = threading.Lock()


def _load_room_names():
    try:
        return json.loads(ROOM_NAMES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_room_names():
    try:
        ROOM_NAMES_FILE.write_text(json.dumps(_room_names, ensure_ascii=False),
                                   encoding="utf-8")
    except OSError:
        pass


def get_owner_names():
    with _room_names_lock:
        return dict(_room_names)


def _room_info_refresher():
    """后台刷新各房间信息：主播名 + 开播状态（show_status==1 为直播中）。
    经 _douyu_request 统一限流（与取流共享 55s 间隔）；
    结果落盘缓存（room_names.json / room_status.json），重启不丢失。"""
    while True:
        try:
            ids = [str(r.get("id", "")) for r in load_bot_config().get("rooms", [])]
            for rid in ids:
                if not rid:
                    continue
                try:
                    r = _douyu_request("GET", f"https://www.douyu.com/betard/{rid}")
                    room = r.json().get("room") or {}
                    name = (room.get("owner_name") or "").strip()
                    live = room.get("show_status") == 1
                    with _room_names_lock:
                        if name:
                            _room_names[rid] = name
                            _save_room_names()
                        try:
                            st = json.loads(ROOM_STATUS_FILE.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            st = {}
                        st[rid] = {"live": live, "ts": time.time()}
                        ROOM_STATUS_FILE.write_text(json.dumps(st), encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(60)


def get_room_status():
    try:
        return json.loads(ROOM_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------- 斗鱼取流（供“实时监看”用）：getEncryption + 签名 + getH5PlayV1 ----------
# 浏览器直接播后端返回的流地址（HLS 优先），视频走浏览器→斗鱼 CDN，不经过本服务转发。
WATCH_QN = {"原画": "0", "蓝光": "8", "超清": "4", "高清": "3", "流畅": "2"}
DOUYU_DID = "10000000000000000000000000001501"
_douyu_gate = threading.Lock()
_douyu_next_ok = 0.0  # www.douyu.com 下次允许请求的时间戳（与房间信息刷新共享 55s 间隔）
_stream_cache = {}    # (rid, qn) -> {"url","kind","ts"}，流地址约 300s 过期
_stream_lock = threading.Lock()
_watch_quality = "超清"
_watch_heartbeat = 0.0  # 前端监看页心跳，90s 内有效时才取流（没人看就不浪费请求额度）


def _douyu_request(method, url, **kw):
    """www.douyu.com 统一出口：全局串行 + 每次间隔 55s 以上（egress 代理高频限流）。"""
    global _douyu_next_ok
    with _douyu_gate:
        wait = _douyu_next_ok - time.time()
        if wait > 0:
            time.sleep(wait)
        proxy = os.environ.get("PROXY_URL", "").strip()
        kw.setdefault("proxies", {"http": proxy, "https": proxy} if proxy else {})
        hdrs = kw.setdefault("headers", {})
        hdrs.setdefault("User-Agent", UA_WEB)
        kw.setdefault("timeout", 20)
        try:
            return requests.request(method, url, **kw)
        finally:
            _douyu_next_ok = time.time() + 55


_douyu_stream_gate = threading.Lock()
_douyu_stream_next_ok = 0.0
def _douyu_stream_request(method, url, **kw):
    """取流专用出口：独立 55s 限流，不被房间状态轮询阻塞。"""
    global _douyu_stream_next_ok
    with _douyu_stream_gate:
        wait = _douyu_stream_next_ok - time.time()
        if wait > 0:
            time.sleep(wait)
        proxy = os.environ.get("PROXY_URL", "").strip()
        kw.setdefault("proxies", {"http": proxy, "https": proxy} if proxy else {})
        hdrs = kw.setdefault("headers", {})
        hdrs.setdefault("User-Agent", UA_WEB)
        kw.setdefault("timeout", 20)
        try:
            return requests.request(method, url, **kw)
        finally:
            _douyu_stream_next_ok = time.time() + 55


def _douyu_stream_sign(rid, ts, key, rand_str, enc_time, is_special):
    f = rand_str
    for _ in range(enc_time):
        f = hashlib.md5((f + key).encode()).hexdigest()
    suffix = "" if is_special == 1 else f"{rid}{ts}"
    return hashlib.md5((f + key + suffix).encode()).hexdigest()


def resolve_stream_url(rid, qn="超清"):
    """取房间可播流地址，优先 HLS。返回 {"url","kind","ts"} 或 {"error":...}。
    斗鱼经 egress 代理偶发 RST，失败自动重试 3 次（_douyu_request 自带 55s 间隔）。"""
    key = (str(rid), qn)
    with _stream_lock:
        c = _stream_cache.get(key)
        if c and time.time() - c["ts"] < 240:
            return c
    last_err = "未知错误"
    for attempt in range(3):
        try:
            r = _douyu_stream_request(
                "GET",
                f"https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={DOUYU_DID}")
            j = r.json()
            if j.get("error") != 0 or not j.get("data"):
                last_err = "getEncryption 失败"
                continue
            d = j["data"]
            try:
                ts = int(parsedate_to_datetime(r.headers.get("Date", "")).timestamp())
            except Exception:
                ts = int(time.time())
        except Exception as e:
            last_err = f"getEncryption 异常: {type(e).__name__}"
            continue
        auth = _douyu_stream_sign(str(rid), ts, d["key"], d["rand_str"],
                                  d["enc_time"], d["is_special"])
        try:
            r = _douyu_stream_request("POST", f"https://www.douyu.com/lapi/live/getH5PlayV1/{rid}", data={
                "enc_data": d["enc_data"], "tt": str(ts), "did": DOUYU_DID, "auth": auth,
                "cdn": "", "rate": WATCH_QN.get(qn, "4"),
                "hevc": "0", "fa": "0", "ive": "0"})
            j = r.json()
        except Exception as e:
            last_err = f"getH5PlayV1 异常: {type(e).__name__}"
            continue
        if j.get("error") != 0 or not j.get("data"):
            last_err = f"取流失败: {str(j)[:100]}"
            continue
        dd = j["data"]
        if dd.get("hls_url") and dd.get("hls_live"):
            url, kind = dd["hls_url"] + "/" + dd["hls_live"], "hls"
        elif dd.get("rtmp_url") and dd.get("rtmp_live"):
            url, kind = dd["rtmp_url"] + "/" + dd["rtmp_live"], "flv"
        else:
            last_err = "无可用流地址"
            continue
        item = {"url": url, "kind": kind, "ts": time.time()}
        with _stream_lock:
            _stream_cache[key] = item
        return item
    print(f"[webui] 取流失败 rid={rid} qn={qn}: {last_err}", flush=True)
    return {"error": last_err}


def _stream_refresher():
    """有人在看时，保持各直播中房间的流地址新鲜（<240s）。"""
    while True:
        try:
            if time.time() - _watch_heartbeat < 90:
                cfg = load_bot_config()
                status = get_room_status()
                for r in cfg.get("rooms", []):
                    rid = str(r.get("id", ""))
                    if not rid or not (status.get(rid) or {}).get("live"):
                        continue
                    key = (rid, _watch_quality)
                    with _stream_lock:
                        c = _stream_cache.get(key)
                        stale = (not c) or time.time() - c["ts"] > 240
                    if stale:
                        resolve_stream_url(rid, _watch_quality)
        except Exception:
            pass
        time.sleep(30)


_room_names.update(_load_room_names())


def ai_mode(env):
    if env.get("GEMINI_RELAY_URL"):
        return "relay"
    if env.get("GEMINI_API_KEY"):
        return "direct"
    return "none"


def mask_key(k):
    k = (k or "").strip()
    if len(k) <= 8:
        return "已设置" if k else "未设置"
    return f"已设置（…{k[-4:]}）"


# ---------- 斗鱼扫码登录会话（单例） ----------
from douyu_auth import QRLoginSession, login_uid  # noqa: E402

login_sess = {"obj": None, "account": None, "lock": threading.Lock()}


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "DouyuBotWebUI/1.0"

    def _auth_ok(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            user, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            return False
        return user == ADMIN_USER and pwd == self.server.webui_password

    def _require_auth(self):
        if not self._auth_ok():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="douyu-ai-bot"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("需要登录".encode("utf-8"))
            return False
        return True

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def log_message(self, *a):
        pass

    # ----- GET -----
    def do_GET(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._serve_index()
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/login/poll":
            self._api_login_poll()
        elif path == "/api/watch":
            self._api_watch()
        else:
            self._json({"error": "not found"}, 404)

    # ----- POST -----
    def do_POST(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                self._api_config()
            elif path == "/api/accounts":
                self._api_accounts()
            elif path == "/api/ai":
                self._api_ai()
            elif path == "/api/login/qr":
                self._api_login_qr()
            elif path == "/api/service/restart":
                self._api_restart()
            elif path == "/api/watch":
                self._api_watch_post()
            elif path == "/api/send":
                self._api_send()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ----- API 实现 -----
    def _api_status(self):
        from douyu_auth import login_uid
        env = read_env()
        cfg = load_bot_config()
        status_map = get_room_status()
        rooms = []
        for r in cfg.get("rooms", []):
            rid = str(r.get("id", ""))
            st = status_map.get(rid)
            rooms.append({
                "id": rid,
                "keywords": r.get("keywords", []),
                "mode": r.get("mode", cfg.get("triggers", {}).get("mode", "contains")),
                "owner": get_owner_names().get(rid, ""),
                "offline_monitor": bool(r.get("offline_monitor", True)),
                "enabled": bool(r.get("enabled", True)),
                "live": st["live"] if isinstance(st, dict) and "live" in st else None,
            })
        accounts = []
        for a in cfg.get("accounts", []):
            name = str(a.get("name", ""))
            cf = a.get("cookie_file", "")
            p = (ROOT / cf) if cf and not os.path.isabs(cf) else Path(cf)
            uid = login_uid(str(p))
            nick = cookie_nickname(str(p)) if uid else ""
            accounts.append({"name": name, "uid": uid or None,
                             "nickname": nick or None,
                             "logged_in": bool(uid), "cookie_file": cf})
        self._json({
            "bot_active": svc_active("douyu-ai-bot"),
            "relay_active": svc_active("gemini-relay"),
            "rooms": rooms,
            "accounts": accounts,
            "model": cfg.get("gemini", {}).get("model", ""),
            "history_rounds": int(cfg.get("gemini", {}).get("history_rounds", 6)),
            "mention_prefix": cfg.get("triggers", {}).get("mention_prefix", "@AI"),
            "ai_mode": ai_mode(env),
            "version": VERSION,
            "relay_url": env.get("GEMINI_RELAY_URL", RELAY_URL_DEFAULT),
            "proxy_url": env.get("PROXY_URL", ""),
            "key_hint": mask_key(env.get("GEMINI_API_KEY", "")),
            "hits": recent_hits(30),
            "owners": get_owner_names(),
            "logs": recent_logs(30),
        })

    def _api_config(self):
        body = self._read_json()
        cfg = load_bot_config()
        if "rooms" in body:
            rooms = body["rooms"]
            if not isinstance(rooms, list) or not rooms or len(rooms) > 10:
                return self._json({"error": "房间数须在 1~10 之间"}, 400)
            new_rooms = []
            seen = set()
            for r in rooms:
                rid = str(r.get("id", "")).strip()
                if not re.fullmatch(r"\d{1,10}", rid) or rid in seen:
                    return self._json({"error": f"房间号无效或重复: {rid}"}, 400)
                seen.add(rid)
                kws = [str(k).strip() for k in r.get("keywords", []) if str(k).strip()]

                mode = r.get("mode", "contains")
                if mode not in ("contains", "mention"):
                    return self._json({"error": f"房间 {rid} mode 非法"}, 400)
                new_rooms.append({"id": rid, "mode": mode, "keywords": kws[:20],
                                  "offline_monitor": bool(r.get("offline_monitor", True)),
                                  "enabled": bool(r.get("enabled", True))})
            cfg["rooms"] = new_rooms
            cfg.pop("room_id", None)  # 旧字段不再使用
        if "model" in body:
            cfg.setdefault("gemini", {})["model"] = str(body["model"]).strip()
        if "history_rounds" in body:
            try:
                hr = max(0, min(20, int(body["history_rounds"])))
            except (TypeError, ValueError):
                hr = 6
            cfg.setdefault("gemini", {})["history_rounds"] = hr
        if "mention_prefix" in body:
            cfg.setdefault("triggers", {})["mention_prefix"] = str(body["mention_prefix"]).strip() or "@AI"
        write_config(cfg)
        mode, ok, msg = bot_apply_config()
        self._json({"ok": ok, "mode": mode, "restart": msg,
                    "rooms": [r["id"] for r in cfg.get("rooms", [])]})

    def _api_accounts(self):
        body = self._read_json()
        action = body.get("action")
        cfg = load_bot_config()
        accounts = cfg.get("accounts", [])
        if action == "add":
            name = str(body.get("name", "")).strip()[:20]
            if not name:
                return self._json({"error": "账号名不能为空"}, 400)
            if any(a.get("name") == name for a in accounts):
                return self._json({"error": "账号名已存在"}, 400)
            if len(accounts) >= 10:
                return self._json({"error": "账号数已达 10 个上限"}, 400)
            accounts.append({"name": name, "cookie_file": account_cookie_path(name)})
        elif action == "remove":
            name = str(body.get("name", ""))
            if len(accounts) <= 1:
                return self._json({"error": "至少保留一个账号"}, 400)
            accounts = [a for a in accounts if a.get("name") != name]
            if len(accounts) == len(cfg.get("accounts", [])):
                return self._json({"error": "账号不存在"}, 404)
        else:
            return self._json({"error": "action 非法"}, 400)
        cfg["accounts"] = accounts
        write_config(cfg)
        mode, ok, msg = bot_apply_config()
        self._json({"ok": ok, "mode": mode, "restart": msg,
                    "accounts": [a["name"] for a in accounts]})

    def _api_ai(self):
        body = self._read_json()
        mode = body.get("ai_mode")
        if mode not in ("relay", "direct"):
            return self._json({"error": "ai_mode 非法"}, 400)
        env = read_env()
        updates = {}
        if mode == "relay":
            relay_url = str(body.get("relay_url", "")).strip() or RELAY_URL_DEFAULT
            if not relay_url.startswith(("http://", "https://")):
                return self._json({"error": "中继地址须以 http:// 或 https:// 开头"}, 400)
            if env.get("GEMINI_RELAY_URL") != relay_url:
                updates["GEMINI_RELAY_URL"] = relay_url
        else:
            key = str(body.get("key", "")).strip()
            if not key and not env.get("GEMINI_API_KEY"):
                return self._json({"error": "直连模式需要填写 Gemini Key"}, 400)
            if key:
                updates["GEMINI_API_KEY"] = key
            if env.get("GEMINI_RELAY_URL"):
                updates["GEMINI_RELAY_URL"] = None  # 删除，走直连
        proxy_url = str(body.get("proxy_url", "")).strip()
        if proxy_url and not proxy_url.startswith(("http://", "https://")):
            return self._json({"error": "代理地址须以 http:// 或 https:// 开头"}, 400)
        if (env.get("PROXY_URL") or "") != proxy_url:
            updates["PROXY_URL"] = proxy_url if proxy_url else None
        # 模型存 config.yaml（支持热加载）
        model = str(body.get("model", "")).strip()
        cfg_changed = False
        if model:
            cfg = load_bot_config()
            if cfg.get("gemini", {}).get("model") != model:
                cfg.setdefault("gemini", {})["model"] = model
                save_bot_config(cfg)
                cfg_changed = True
        # .env 有改动才需重启；仅改模型走 SIGHUP 热加载
        if updates:
            write_env(updates)
            ok, msg = svc_restart("douyu-ai-bot")
            self._json({"ok": ok, "restart": msg, "ai_mode": mode})
        elif cfg_changed:
            ok = bot_apply_config()
            self._json({"ok": ok, "restart": "配置已热加载" if ok else "热加载失败", "ai_mode": mode})
        else:
            self._json({"ok": True, "restart": "无改动", "ai_mode": mode})

    def _api_login_qr(self):
        from douyu_auth import QRLoginSession
        body = self._read_json()
        cfg = load_bot_config()
        name = str(body.get("account", "")).strip()
        acct = next((a for a in cfg.get("accounts", []) if a.get("name") == name), None)
        if not acct:
            return self._json({"error": "账号不存在，请先添加账号"}, 400)
        cf = acct.get("cookie_file", "")
        cookie_path = (ROOT / cf) if cf and not os.path.isabs(cf) else Path(cf)
        with login_sess["lock"]:
            sess = QRLoginSession(cookie_path=str(cookie_path))
            try:
                url, expire = sess.start()
            except RuntimeError as e:
                return self._json({"error": str(e)}, 502)
            png = QRLoginSession.qr_png_bytes(url)
            login_sess["obj"] = sess
            login_sess["account"] = name
        data_url = "data:image/png;base64," + base64.b64encode(png).decode()
        self._json({"qr": data_url, "expire": expire, "account": name})

    def _api_login_poll(self):
        with login_sess["lock"]:
            sess = login_sess["obj"]
        if sess is None:
            return self._json({"status": "none", "info": "请先生成二维码"})
        status, info = sess.poll_once()
        if status in ("done", "expired"):
            with login_sess["lock"]:
                login_sess["obj"] = None
        self._json({"status": status, "info": info})

    def _api_restart(self):
        body = self._read_json()
        svc = body.get("service")
        if svc == "bot":
            name = "douyu-ai-bot"
        elif svc == "relay":
            name = "gemini-relay"
        else:
            return self._json({"error": "service 非法"}, 400)
        ok, msg = svc_restart(name)
        self._json({"ok": ok, "msg": msg})

    def _api_watch(self):
        """GET /api/watch：返回各房间直播状态 + 已缓存的流地址（不阻塞取流）。"""
        global _watch_heartbeat
        _watch_heartbeat = time.time()  # 顺便当心跳
        cfg = load_bot_config()
        status_map = get_room_status()
        owners = get_owner_names()
        rooms = []
        with _stream_lock:
            cache = dict(_stream_cache)
        for r in cfg.get("rooms", []):
            rid = str(r.get("id", ""))
            st = status_map.get(rid) or {}
            c = cache.get((rid, _watch_quality))
            rooms.append({
                "id": rid,
                "owner": owners.get(rid, ""),
                "live": st.get("live"),
                "enabled": r.get("enabled", True) is not False,
                "url": c["url"] if c else "",
                "kind": c["kind"] if c else "",
                "url_ts": c["ts"] if c else 0,
            })
        self._json({"rooms": rooms, "quality": _watch_quality,
                    "qualities": list(WATCH_QN.keys())})

    def _api_send(self):
        """POST /api/send：{rid, text}，手动发送弹幕。"""
        body = self._read_json()
        rid = str(body.get("rid", "")).strip()
        text = str(body.get("text", "")).strip()
        if not rid or not text:
            return self._json({"error": "缺少 rid 或 text"}, 400)
        if len(text) > 50:
            return self._json({"error": "弹幕太长（最多50字）"}, 400)
        try:
            from bot import Account, DanmakuSender
            cfg = load_config()
            accounts = [Account(a.get("name", "账号"), a.get("cookie_file", ""))
                        for a in cfg.get("accounts", [])]
            sender = DanmakuSender(accounts)
            ok, msg = sender.send(rid, text)
            return self._json({"ok": ok, "msg": msg or ("已发送" if ok else "发送失败")})
        except Exception as e:
            return self._json({"error": f"发送异常：{e}"}, 500)

    def _api_watch_post(self):
        """POST /api/watch：{action: heartbeat|quality|refresh}。"""
        global _watch_heartbeat, _watch_quality
        body = self._read_json()
        action = body.get("action")
        if action == "heartbeat":
            _watch_heartbeat = time.time()
            return self._json({"ok": True})
        if action == "quality":
            q = body.get("quality", "")
            if q not in WATCH_QN:
                return self._json({"error": "清晰度非法"}, 400)
            _watch_quality = q
            with _stream_lock:
                _stream_cache.clear()
            _watch_heartbeat = time.time()
            return self._json({"ok": True, "quality": q})
        if action == "refresh":
            rid = str(body.get("rid", ""))
            if not rid:
                return self._json({"error": "rid 非法"}, 400)
            _watch_heartbeat = time.time()
            # 异步取流：斗鱼限流下单次取流约需 2 分钟，不能阻塞 HTTP 请求；
            # 前端轮询 /api/watch 等地址进缓存后自动挂载播放器。
            threading.Thread(target=resolve_stream_url, args=(rid, _watch_quality),
                             daemon=True).start()
            return self._json({"ok": True, "pending": True})
        return self._json({"error": "action 非法"}, 400)

    # ----- 前端页面 -----
    def _serve_index(self):
        page = INDEX_HTML
        raw = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>斗鱼 AI 机器人 · 管理后台</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#0f1420;color:#e8ecf4;margin:0;padding:20px}
h1{font-size:20px;margin:0 0 16px}h2{font-size:15px;margin:0 0 10px;color:#9fb0d0}
.card{background:#182032;border:1px solid #26314a;border-radius:10px;padding:16px;margin-bottom:14px;max-width:760px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
label{color:#9fb0d0;font-size:13px;min-width:86px}
input[type=text],input[type=password],select{background:#0f1420;border:1px solid #33405e;color:#e8ecf4;
border-radius:6px;padding:8px 10px;font-size:14px}
button{background:#2f6fed;border:0;color:#fff;border-radius:6px;padding:8px 16px;font-size:14px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}button.danger{background:#b23a3a}button.ghost{background:#26314a}
button.small{padding:4px 10px;font-size:12px}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px}
.ok{background:#123d2a;color:#4ade80}.bad{background:#3d1a1a;color:#f87171}.warn{background:#3d2f12;color:#fbbf24}
.kw{display:inline-block;background:#26314a;border-radius:16px;padding:4px 8px 4px 12px;margin:3px;font-size:13px}
.kw button{padding:2px 8px;margin-left:6px;font-size:12px}
#logs{background:#0b0f18;border-radius:6px;padding:10px;font:12px/1.7 monospace;white-space:pre-wrap;
max-height:300px;overflow:auto;color:#a8b6d0}
#qrimg{width:220px;height:220px;background:#fff;border-radius:8px;display:none;margin:8px 0}
.hint{font-size:12px;color:#7d8db0}.msg{font-size:13px;margin-top:8px;min-height:18px}
#rooms{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
#st_card .row{flex-wrap:nowrap}
.pair-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;align-items:stretch;justify-items:stretch}
.pair-grid>.card{margin-bottom:0 !important;min-height:100%;box-sizing:border-box;max-width:none}
#st_card #st_detail{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.roombox{border:1px solid #2a3654;border-radius:8px;padding:10px;background:#141b2c;min-width:0}
.roombox.paused{opacity:.55;border-style:dashed}
.acct{display:flex;gap:8px;align-items:center;padding:8px;border:1px solid #2a3654;border-radius:8px;margin:6px 0;background:#141b2c}
.acct .nm{font-weight:600}
table.hits{width:100%;border-collapse:collapse;font-size:13px}
table.hits td,table.hits th{border-bottom:1px solid #26314a;padding:6px 8px;text-align:left;vertical-align:top}
table.hits th{color:#9fb0d0;font-weight:600}
.rid{color:#7dd3fc;font-family:monospace}.tm{color:#7d8db0;font-size:12px;white-space:nowrap}
.wgrid{display:grid;gap:10px;margin-top:8px}
.wgrid.c1{grid-template-columns:1fr}.wgrid.c2{grid-template-columns:1fr 1fr}.wgrid.c3{grid-template-columns:1fr 1fr 1fr}.wgrid.c4{grid-template-columns:1fr 1fr 1fr 1fr}
.wtile{position:relative;background:#0b0f18;border:1px solid #2a3654;border-radius:8px;overflow:hidden;display:flex;flex-direction:column;aspect-ratio:16/10;cursor:pointer}
.wtile .wvid{position:relative;flex:1 1 auto;min-height:0;cursor:pointer}
.wtile .wvid video{position:absolute;inset:0;width:100%;height:100%;background:#000;display:block;object-fit:contain}

.wtile .wtag{position:absolute;left:8px;top:6px;font-size:12px;background:rgba(0,0,0,.55);padding:2px 10px;border-radius:12px}
.wtile .wlive{position:absolute;right:8px;top:6px;font-size:12px}
.wtile .woff{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#7d8db0;font-size:14px}
.wtile .werr{position:absolute;left:0;right:0;bottom:0;font-size:12px;color:#fbbf24;background:rgba(0,0,0,.6);padding:4px 8px;display:none}
.wtile .wvol{position:absolute;left:8px;bottom:8px;display:flex;align-items:center;gap:6px;background:rgba(0,0,0,.55);padding:3px 10px 3px 8px;border-radius:12px;opacity:.9;z-index:2}
.wtile .wvol:hover{opacity:1}
.wtile .wvol input{width:80px;margin:0;padding:0;cursor:pointer;accent-color:#7aa2ff}
.wdmk{position:absolute;inset:0;overflow:hidden;pointer-events:none;z-index:1}
.wdmk-it{position:absolute;right:0;transform:translateX(100%);white-space:nowrap;color:#fff;font-size:15px;line-height:28px;text-shadow:0 1px 2px rgba(0,0,0,.9),0 0 6px rgba(0,0,0,.6);animation-name:wdmkfly;animation-timing-function:linear;animation-fill-mode:forwards}
@keyframes wdmkfly{to{transform:translateX(calc(-100vw - 100%))}}
.wtile .wbtns{position:absolute;right:8px;bottom:8px;display:flex;gap:6px;z-index:2}
.wtile .wbtns button{background:rgba(0,0,0,.55);border:1px solid #33415c;color:#9ab8e0;border-radius:12px;padding:3px 10px;font-size:12px;cursor:pointer}
.wtile .wbtns button:hover{border-color:#3b82f6;color:#fff}
.wtile .wbtns button.off{opacity:.5}
.wtile .wsend{display:flex;gap:6px;padding:6px 8px;align-items:center;background:#0e1422;border-top:1px solid #1e2a3f}
.wtile .wsend input{flex:1;min-width:0;background:#182032;border:1px solid #2a3654;color:#dbe4ff;border-radius:6px;padding:6px 8px;font-size:13px}
.wtile .wsend button{flex:none;background:#1e2a3f;border:1px solid #33415c;color:#cfe0ff;border-radius:6px;padding:6px 12px;font-size:13px;cursor:pointer}
.wtile .wsend button:hover{background:#3b82f6;color:#fff}
.wtile .wsend button:disabled{opacity:.5;cursor:default}
.wtile .wmsg{font-size:11px;color:#7d8db0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:90px}
</style></head><body>
<h1>🤖 斗鱼 AI 弹幕机器人 · 管理后台</h1>

<div class="card" style="max-width:none" id="st_card"><h2>状态</h2>
<div class="row"><span class="badge" id="b_bot">…</span><span class="badge" id="b_relay">…</span>
<span class="badge" id="b_ai">…</span></div>
<div class="row hint" id="st_detail"></div>
<div class="row"><button class="ghost" onclick="doRestart('bot')">重启机器人</button>
<button class="ghost" onclick="doRestart('relay')">重启中继</button>
<button class="ghost" onclick="loadStatus()">刷新状态</button></div>
<div class="msg" id="msg_status"></div></div>

<div class="card" style="max-width:none"><h2>实时监看 <span class="hint">（与机器人同一批房间 · 视频直连斗鱼 CDN，不经过服务器转发）</span></h2>
<div class="row"><label>布局</label><select id="w_layout" onchange="setWLayout()">
<option value="2">2×2</option><option value="1">1×1</option><option value="3">3×3</option><option value="4">4×4</option></select>
<label>清晰度</label><select id="w_qn" onchange="setWQuality()"></select>
<button class="ghost small" onclick="loadWatch(true)">刷新流地址</button>
<span class="hint">默认静音自动播放，点击画面切换该路声音（一次只一路有声）</span></div>
<div id="w_grid" class="wgrid c2"></div>
<div class="msg" id="msg_watch"></div></div>

<div class="card" style="max-width:none"><h2>监控房间 <span class="hint">（最多 10 个，每房独立触发词）</span></h2>
<div id="rooms"></div>
<div class="row"><input type="text" id="new_room" placeholder="新房间号（纯数字）" style="width:200px">
<button class="ghost" onclick="addRoom()">添加房间</button></div>
<div class="row"><label>mention 前缀</label><input type="text" id="f_prefix" style="width:160px">
<span class="hint">mention 模式下弹幕以此前缀开头才触发</span></div>
<div class="row">
<label>对话记忆</label><input type="number" id="f_history" min="0" max="20" style="width:64px"><span class="hint">轮，0=关闭</span></div>
<div class="row"><button onclick="saveRooms()">保存房间配置</button><span class="hint">保存后热加载，已有房间监控不中断</span></div>
<div class="msg" id="msg_rooms"></div></div>

<div class="pair-grid">
<div class="card"><h2>命中记录 <span class="hint">（房间 / 发送者 / 内容）</span></h2>
<table class="hits"><thead><tr><th>时间</th><th>房间</th><th>发送者</th><th>内容</th></tr></thead>
<tbody id="hits_body"><tr><td colspan="4" class="hint">加载中…</td></tr></tbody></table></div>
<div class="card"><h2>发送账号 <span class="hint">（最多 10 个，轮流发送）</span></h2>
<div id="accts"></div>
<div class="row"><input type="text" id="new_acct" placeholder="新账号名" style="width:200px">
<button class="ghost" onclick="addAcct()">添加账号</button></div>
<div class="msg" id="msg_acct"></div>
<div class="card" style="background:#141b2c;margin:10px 0 0"><h2>扫码登录</h2>
<div class="row"><label>账号</label><select id="login_acct"></select>
<button onclick="genQR()">生成二维码</button><span class="hint" id="qr_status"></span></div>
<img id="qrimg" alt="扫码二维码"><div class="msg" id="msg_login"></div></div></div>
</div>

<div class="pair-grid">
<div class="card"><h2>最近日志</h2>
<div class="row"><button class="ghost" onclick="loadStatus()">刷新</button></div>
<div id="logs"></div></div>
<div class="card"><h2>AI Key</h2>
<div class="row"><label>调用模式</label><select id="f_aimode">
<option value="relay">中继模式（走服务器保险库 Key）</option>
<option value="direct">直连模式（用下面填写的 Key）</option></select>
<span class="hint" id="key_hint"></span></div>
<div class="row"><label>Gemini Key</label><input type="password" id="f_key" placeholder="AIza…（留空则不修改）" style="width:260px"></div>
<div class="row"><label>中继地址</label><input type="text" id="f_relayurl" placeholder="http://127.0.0.1:18020/generate" style="width:260px"></div>
<div class="row"><label>代理地址</label><input type="text" id="f_proxyurl" placeholder="http://127.0.0.1:8080（直连 Gemini 需代理时填）" style="width:260px"></div>
<div class="row"><label>Gemini 模型</label><select id="f_model" style="width:260px">
<option value="gemini-3-flash-preview">gemini-3-flash-preview（推荐）</option>
<option value="gemini-2.5-flash">gemini-2.5-flash</option>
<option value="gemini-2.5-pro">gemini-2.5-pro</option>
<option value="gemini-2.0-flash">gemini-2.0-flash</option>
<option value="gemini-1.5-flash">gemini-1.5-flash</option>
<option value="gemini-1.5-pro">gemini-1.5-pro</option>
</select></div>
<div class="row"><button onclick="saveAI()">保存 AI 设置</button></div>
<div class="msg" id="msg_ai"></div>
<div class="hint">中继模式优先，走下面填写的中继地址调用；切到直连模式会停用中继、改用上面填写的 Key。Key 仅保存不回显。改这里会重启机器人。</div></div>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/flv.js@1.6.2/dist/flv.min.js"></script>
<script>
let rooms=[];
async function api(p,o={}){const r=await fetch(p,Object.assign({headers:{'Content-Type':'application/json'}},o));
const j=await r.json();if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j;}
function badge(id,txt,cls){const e=document.getElementById(id);e.textContent=txt;e.className='badge '+cls;}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
async function loadStatus(){try{const s=await api('/api/status');
badge('b_bot','机器人 '+(s.bot_active?'运行中':'未运行'),s.bot_active?'ok':'bad');
badge('b_relay','中继 '+(s.relay_active?'运行中':'未运行'),s.relay_active?'ok':'bad');
badge('b_ai','AI: '+(s.ai_mode==='relay'?'中继':s.ai_mode==='direct'?'直连':'未配置'),s.ai_mode==='none'?'bad':'ok');
const nLogin=s.accounts.filter(a=>a.logged_in).length;
const roomNames=s.rooms.map(r=>r.owner||r.id).join('、');
const nPaused=s.rooms.filter(r=>r.enabled===false).length;
document.getElementById('st_detail').textContent=
`v${s.version||'?'} · ${s.rooms.length} 个房间${nPaused?`（${nPaused} 已暂停）`:''}：${roomNames} · ${s.accounts.length} 个账号（${nLogin} 已登录）· 模型 ${s.model}`;
// 命中记录
owners=s.owners||{};
document.getElementById('hits_body').innerHTML=s.hits.length?s.hits.map(h=>{
const rl=h.room?esc(owners[h.room]||h.room):'—';
return `<tr><td class="tm">${esc(h.t)}</td><td class="rid">${rl}</td><td>${esc(h.sender)}</td><td>${esc(h.content)}</td></tr>`;}).join('')
:'<tr><td colspan="4" class="hint">暂无命中</td></tr>';
// 房间（首次加载时初始化编辑器；之后用最新 owner 名刷新显示）
if(!rooms.length&&s.rooms.length){rooms=s.rooms.map(r=>({id:r.id,mode:r.mode,keywords:r.keywords.slice(),owner:r.owner||'',live:r.live??null,offline_monitor:r.offline_monitor!==false,enabled:r.enabled!==false}));renderRooms();}
else if(rooms.length){let ch=false;rooms.forEach((r,i)=>{const s2=s.rooms[i]||{};
const o=s2.owner||'';if(r.owner!==o){r.owner=o;ch=true;}
if(r.live!==(s2.live??null)){r.live=s2.live??null;ch=true;}});if(ch)renderRooms();}
if(!document.getElementById('f_model').value){
document.getElementById('f_model').value=s.model;
document.getElementById('f_history').value=s.history_rounds??6;
document.getElementById('f_prefix').value=s.mention_prefix||'@AI助手';
document.getElementById('f_aimode').value=s.ai_mode==='direct'?'direct':'relay';}
if(!document.getElementById('f_relayurl').value){
document.getElementById('f_relayurl').value=s.relay_url||'';}
if(!document.getElementById('f_proxyurl').value){
document.getElementById('f_proxyurl').value=s.proxy_url||'';}
// 账号
renderAccts(s.accounts);
document.getElementById('key_hint').textContent='当前 Key：'+s.key_hint;
document.getElementById('logs').textContent=s.logs.join('\\n')||'(暂无日志)';
}catch(e){badge('b_bot','状态获取失败','bad');}}
// ---- 房间编辑 ----
let owners={};
function roomLabel(r){return esc(r.owner||r.id);}
function liveBadge(l){if(l===true)return '<span class="badge ok">直播中</span>';
if(l===false)return '<span class="badge warn">已下播</span>';return '<span class="badge">未知</span>';}
function renderRooms(){document.getElementById('rooms').innerHTML=rooms.map((r,i)=>
`<div class="roombox${r.enabled===false?' paused':''}"><div class="row"><label>房间</label><b>${roomLabel(r)}</b>${liveBadge(r.live)}${r.enabled===false?'<span class="badge warn">已暂停</span>':''}
<label>房间号</label>
<input type="text" value="${esc(r.id)}" style="width:120px" onchange="rooms[${i}].id=this.value.trim()">
<label>模式</label><select onchange="rooms[${i}].mode=this.value">
<option value="contains"${r.mode==='contains'?' selected':''}>contains</option>
<option value="mention"${r.mode==='mention'?' selected':''}>mention</option></select>
<button class="ghost small" onclick="toggleRoom(${i})">${r.enabled===false?'恢复监控':'暂停监控'}</button>
<button class="danger small" onclick="delRoom(${i})">删除</button></div>
<div class="row"><label>触发词</label><input type="text" id="kw_in_${i}" placeholder="输入后回车添加" style="width:200px">
<button class="ghost small" onclick="addKw(${i})">添加</button>
<label class="hint"><input type="checkbox" ${r.offline_monitor?'checked':''} onchange="rooms[${i}].offline_monitor=this.checked"> 下播后继续监控</label></div>
<div>${r.keywords.map((k,j)=>`<span class="kw">${esc(k)}<button class="danger" onclick="delKw(${i},${j})">✕</button></span>`).join('')}</div>
</div>`).join('');}
function addKw(i){const el=document.getElementById('kw_in_'+i);const v=el.value.trim();
if(v&&!rooms[i].keywords.includes(v)&&rooms[i].keywords.length<20){rooms[i].keywords.push(v);el.value='';renderRooms();}}
function delKw(i,j){rooms[i].keywords.splice(j,1);renderRooms();}
function addRoom(){const v=document.getElementById('new_room').value.trim();
if(!/^\\d{1,10}$/.test(v)){alert('房间号须为纯数字');return;}
if(rooms.some(r=>r.id===v)){alert('房间已存在');return;}
if(rooms.length>=10){alert('最多 10 个房间');return;}
rooms.push({id:v,mode:'contains',keywords:[],owner:'',live:null,offline_monitor:true,enabled:true});document.getElementById('new_room').value='';renderRooms();}
function toggleRoom(i){rooms[i].enabled=rooms[i].enabled===false?true:false;renderRooms();
document.getElementById('msg_rooms').textContent='已'+(rooms[i].enabled?'恢复':'暂停')+'（点"保存房间配置"后生效）';}
function delRoom(i){if(rooms.length<=1){alert('至少保留一个房间');return;}rooms.splice(i,1);renderRooms();}
async function saveRooms(){const m=document.getElementById('msg_rooms');m.textContent='保存中…';

try{const j=await api('/api/config',{method:'POST',body:JSON.stringify({rooms:rooms,
history_rounds:parseInt(document.getElementById('f_history').value)||0,
mention_prefix:document.getElementById('f_prefix').value.trim()})});
m.textContent='已保存，'+(j.mode==='reload'?'配置已热加载（监控未中断）':'机器人重启'+(j.ok?'成功':'失败：'+j.restart));loadStatus();
}catch(e){m.textContent='失败：'+e.message;}}
// ---- 账号 ----
function renderAccts(accts){document.getElementById('accts').innerHTML=accts.map(a=>{
const label=a.logged_in?('已登录 '+(a.nickname||('uid '+a.uid))):'未登录';
return `<div class="acct"><span class="nm">${esc(a.name)}</span>
<span class="badge ${a.logged_in?'ok':'warn'}">${esc(label)}</span>
<button class="danger small" onclick="delAcct('${esc(a.name)}')">删除</button></div>`;}).join('');
document.getElementById('login_acct').innerHTML=accts.map(a=>`<option>${esc(a.name)}</option>`).join('');}
async function addAcct(){const v=document.getElementById('new_acct').value.trim();if(!v)return;
const m=document.getElementById('msg_acct');m.textContent='添加中…';
try{const j=await api('/api/accounts',{method:'POST',body:JSON.stringify({action:'add',name:v})});
document.getElementById('new_acct').value='';
m.textContent='已添加，'+(j.mode==='reload'?'配置已热加载（监控未中断）':'机器人重启'+(j.ok?'成功':'失败'));loadStatus();
}catch(e){m.textContent='失败：'+e.message;}}
async function delAcct(name){if(!confirm('删除账号 '+name+'？'))return;
const m=document.getElementById('msg_acct');m.textContent='删除中…';
try{const j=await api('/api/accounts',{method:'POST',body:JSON.stringify({action:'remove',name:name})});
m.textContent='已删除，'+(j.mode==='reload'?'配置已热加载（监控未中断）':'机器人重启'+(j.ok?'成功':'失败'));loadStatus();}catch(e){m.textContent='失败：'+e.message;}}
// ---- AI ----
async function saveAI(){const m=document.getElementById('msg_ai');m.textContent='保存中…';
try{const j=await api('/api/ai',{method:'POST',body:JSON.stringify({
ai_mode:document.getElementById('f_aimode').value,key:document.getElementById('f_key').value,
relay_url:document.getElementById('f_relayurl').value.trim(),
proxy_url:document.getElementById('f_proxyurl').value.trim(),
model:document.getElementById('f_model').value})});
document.getElementById('f_key').value='';
m.textContent='已保存（'+(j.ai_mode==='relay'?'中继模式':'直连模式')+'），机器人重启'+(j.ok?'成功':'失败');
loadStatus();}catch(e){m.textContent='失败：'+e.message;}}
async function doRestart(svc){const m=document.getElementById('msg_status');m.textContent='重启中…';
try{const j=await api('/api/service/restart',{method:'POST',body:JSON.stringify({service:svc})});
m.textContent=j.ok?'重启成功':'重启失败：'+j.msg;setTimeout(loadStatus,2000);
}catch(e){m.textContent='失败：'+e.message;}}
// ---- 扫码登录 ----
let pollTimer=null;
async function genQR(){const acct=document.getElementById('login_acct').value;
const m=document.getElementById('msg_login');const qs=document.getElementById('qr_status');
const img=document.getElementById('qrimg');m.textContent='生成中…';img.style.display='none';
try{const j=await api('/api/login/qr',{method:'POST',body:JSON.stringify({account:acct})});
img.src=j.qr;img.style.display='block';
qs.textContent=`账号「${acct}」请用斗鱼 APP 扫码（${j.expire}s 内有效）`;m.textContent='';
clearInterval(pollTimer);pollTimer=setInterval(pollLogin,3000);
}catch(e){m.textContent='失败：'+e.message;}}
async function pollLogin(){const m=document.getElementById('msg_login');
const qs=document.getElementById('qr_status');
try{const j=await api('/api/login/poll');
if(j.status==='done'){clearInterval(pollTimer);qs.textContent='';
m.textContent='✅ 登录成功（uid '+j.info+'）';
document.getElementById('qrimg').style.display='none';loadStatus();}
else if(j.status==='expired'){clearInterval(pollTimer);qs.textContent='二维码已过期，请重新生成';}
else if(j.status==='scanned'){qs.textContent='已扫码，请在手机上确认…';}
else if(j.status==='error'){clearInterval(pollTimer);m.textContent='失败：'+j.info;}
}catch(e){}}
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.id.startsWith('kw_in_')){
const i=+e.target.id.slice(6);addKw(i);}});
loadStatus();setInterval(loadStatus,15000);

// ---------- 实时监看 ----------
let wPlayers={}, wInit=false, wSig='', wPendingRefresh={}, wUrls={};
function setWLayout(){const v=document.getElementById('w_layout').value;
try{localStorage.setItem('w_layout',v);}catch(e){}
const g=document.getElementById('w_grid');g.className='wgrid c'+v;}
(function(){try{const v=localStorage.getItem('w_layout');if(v&&['1','2','3','4'].includes(v)){document.getElementById('w_layout').value=v;document.getElementById('w_grid').className='wgrid c'+v;}}catch(e){}})();
async function ensureWUrl(rid){
const now=Date.now();
if(wPendingRefresh[rid]&&now-wPendingRefresh[rid]<180000)return;
wPendingRefresh[rid]=now;
try{await api('/api/watch',{method:'POST',body:JSON.stringify({action:'refresh',rid:String(rid)})});}catch(e){}}
async function setWQuality(){const q=document.getElementById('w_qn').value;
try{await api('/api/watch',{method:'POST',body:JSON.stringify({action:'quality',quality:q})});
Object.keys(wPlayers).forEach(destroyWPlayer);wPlayers={};wUrls={};wSig='';loadWatch();}catch(e){
document.getElementById('msg_watch').textContent='切换失败：'+e.message;}}
function destroyWPlayer(rid){const p=wPlayers[rid];if(!p)return;
try{if(p.destroy)p.destroy();}catch(e){}delete wPlayers[rid];}
function attachWPlayer(rid,url,kind){
const v=document.getElementById('wv_'+rid);if(!v)return;
wUrls[rid]=url;
destroyWPlayer(rid);
const e0=document.querySelector('#wt_'+rid+' .werr');if(e0)e0.style.display='none';
const onFatal=()=>{const e=document.querySelector('#wt_'+rid+' .werr');
if(e){e.style.display='block';e.textContent='流中断，正在换地址…';}
destroyWPlayer(rid);ensureWUrl(rid);};
if(kind==='hls'&&window.Hls&&Hls.isSupported()){
const h=new Hls({maxBufferLength:30});wPlayers[rid]=h;
h.on(Hls.Events.ERROR,(ev,data)=>{if(data.fatal)onFatal();});
h.loadSource(url);h.attachMedia(v);
}else if(kind==='flv'&&window.flvjs&&flvjs.isSupported()){
const p=flvjs.createPlayer({type:'flv',url:url});wPlayers[rid]=p;
p.on(flvjs.Events.ERROR,()=>onFatal());
p.attachMediaElement(v);p.load();
}else{v.src=url;}
const sv=getWVol(rid);v.volume=sv/100;v.muted=(sv==0);
v.muted=true;v.play().catch(()=>{});updateWMuteIcon(rid);}
function focusWAudio(rid){
const v=document.getElementById('wv_'+rid);if(!v)return;
v.muted=!v.muted;
if(!v.muted&&v.volume===0){const sv=getWVol(rid);v.volume=(sv>0?sv:50)/100;}
updateWMuteIcon(rid);}
function updateWMuteIcon(rid){
const v=document.getElementById('wv_'+rid);const ic=document.getElementById('wvic_'+rid);
if(ic)ic.textContent=(v&&!v.muted&&v.volume>0)?'🔊':'🔇';}
function setWVol(rid,val){
try{localStorage.setItem('wvol_'+rid,val);}catch(e){}
const v=document.getElementById('wv_'+rid);
if(v){v.volume=val/100;v.muted=(val==0);}
updateWMuteIcon(rid);}
function getWVol(rid){try{const v=localStorage.getItem('wvol_'+rid);if(v!==null)return Math.max(0,Math.min(100,parseInt(v)||0));}catch(e){}return 100;}
/* 弹幕：浏览器直连斗鱼 WSS + 飘屏，按房间独立开关 */
const DM_EPS=[8501,8502,8503,8504,8505,8506].map(p=>'wss://danmuproxy.douyu.com:'+p+'/');
function sttUnesc(v){return v.replace(/@S/g,'/').replace(/@A/g,'@');}
function parseSTT(b){const d={};b.split('/').forEach(p=>{const i=p.indexOf('@=');if(i>0)d[p.slice(0,i)]=sttUnesc(p.slice(i+2));});return d;}
function packSTT(s,pt){const e=new TextEncoder().encode(s);const bl=8+e.length+1;const bf=new ArrayBuffer(12+e.length+1);const dv=new DataView(bf);dv.setInt32(0,bl,true);dv.setInt32(4,bl,true);dv.setUint16(8,pt||689,true);new Uint8Array(bf).set(e,12);return bf;}
function unpackSTT(bf){const out=[];const u8=new Uint8Array(bf);const dv=new DataView(bf);const td=new TextDecoder();let o=0;while(o+12<=u8.length){const bl=dv.getInt32(o,true),t=bl+4;if(bl<9||o+t>u8.length)break;out.push(td.decode(u8.subarray(o+12,o+t-1)));o=t;}return out;}
const dmkConns={};
function dmkSync(liveIds){
Object.keys(dmkConns).forEach(id=>{if(liveIds.indexOf(id)<0)dmkDrop(id);});
liveIds.forEach(id=>{if(!dmkConns[id])dmkDial(id);});}
function dmkDial(rid){
const st={eps:DM_EPS.slice().sort(()=>Math.random()-0.5),i:0,dead:false,ws:null,hb:0};
dmkConns[rid]=st;
function redial(){if(st.dead)return;clearInterval(st.hb);setTimeout(dial,5000);}
function dial(){
if(st.dead)return;
let ws;try{ws=new WebSocket(st.eps[st.i++%st.eps.length]);}catch(e){redial();return;}
st.ws=ws;ws.binaryType='arraybuffer';
ws.onopen=()=>{try{ws.send(packSTT('type@=loginreq/roomid@='+rid+'/'));ws.send(packSTT('type@=joingroup/rid@='+rid+'/gid@=-9999/'));}catch(e){}
st.hb=setInterval(()=>{try{ws.send(packSTT('type@=mrkl/'));}catch(e){}},40000);};
ws.onmessage=ev=>{if(typeof ev.data==='string')return;
try{unpackSTT(ev.data).forEach(b=>{if(b.indexOf('type@=')!==0)return;const d=parseSTT(b);
if(d.type==='chatmsg'&&d.nn&&d.txt)dmkShow(rid,d.nn.trim(),d.txt.trim());});}catch(e){}};
ws.onclose=redial;ws.onerror=()=>{try{ws.close();}catch(e){}};}
dial();}
function dmkDrop(rid){const st=dmkConns[rid];if(!st)return;st.dead=true;clearInterval(st.hb);try{st.ws&&st.ws.close();}catch(e){}delete dmkConns[rid];}
function dmkOn(rid){try{return localStorage.getItem('wdmk_'+rid)!=='0';}catch(e){return true;}}
function toggleDmk(rid){
try{localStorage.setItem('wdmk_'+rid,dmkOn(rid)?'0':'1');}catch(e){}
const layer=document.getElementById('wdmk_'+rid);
if(layer&&!dmkOn(rid))layer.innerHTML='';
const b=document.getElementById('wdb_'+rid);
if(b){b.textContent=dmkOn(rid)?'弹幕开':'弹幕关';b.classList.toggle('off',!dmkOn(rid));}}
async function sendWDanmaku(rid){
const inp=document.getElementById('wsi_'+rid);const msg=document.getElementById('wmsg_'+rid);
const text=(inp.value||'').trim();if(!text)return;
inp.disabled=true;msg.textContent='发送中…';
try{
const j=await api('/api/send',{method:'POST',body:JSON.stringify({rid:String(rid),text:text})});
if(j.ok){msg.textContent='✓ '+j.msg;inp.value='';}
else{msg.textContent='✗ '+(j.error||j.msg||'失败');}
}catch(e){msg.textContent='✗ '+e.message;}
inp.disabled=false;
setTimeout(()=>{if(msg.textContent)msg.textContent='';},5000);}
function dmkShow(rid,nn,txt){
if(!dmkOn(rid))return;
const layer=document.getElementById('wdmk_'+rid);if(!layer)return;
const rows=Math.max(3,Math.floor((layer.clientHeight||140)/28));
layer._rr=((layer._rr||0)+1)%rows;
const el=document.createElement('div');el.className='wdmk-it';
el.style.top=(layer._rr*28)+'px';el.style.animationDuration='15s';
el.textContent=nn+'：'+txt;layer.appendChild(el);
el.addEventListener('animationend',()=>el.remove());
while(layer.children.length>80)layer.firstChild.remove();}
function renderWTiles(rooms){
const g=document.getElementById('w_grid');
g.innerHTML=rooms.length?rooms.map(r=>{
const nm=r.owner||('房间 '+r.id);
if(!r.live)return `<div class="wtile" id="wt_${r.id}"><div class="woff">${esc(nm)} · 未开播</div></div>`;
return `<div class="wtile" id="wt_${r.id}">
<div class="wvid" onclick="focusWAudio('${r.id}')"><video id="wv_${r.id}" playsinline></video>
<span class="wtag">${esc(nm)}</span><span class="wlive badge ok">直播中</span>
<div class="werr"></div>
<div class="wvol" onclick="event.stopPropagation()" title="音量"><span id="wvic_${r.id}">🔊</span><input type="range" min="0" max="100" value="${getWVol(r.id)}" oninput="setWVol('${r.id}',this.value)"></div>
<div class="wdmk" id="wdmk_${r.id}"></div>
<div class="wbtns" onclick="event.stopPropagation()"><button id="wdb_${r.id}" onclick="toggleDmk('${r.id}')" title="弹幕开关">弹幕开</button></div></div>
<div class="wsend" onclick="event.stopPropagation()"><input id="wsi_${r.id}" maxlength="50" placeholder="发弹幕…" onkeydown="if(event.key==='Enter')sendWDanmaku('${r.id}')"><button onclick="sendWDanmaku('${r.id}')">发送</button><span class="wmsg" id="wmsg_${r.id}"></span></div></div>`;}).join('')
:'<div class="hint">还没有房间，先在下方添加监控房间</div>';}
function attachWPending(rooms){
rooms.forEach(r=>{
if(!r.live)return;
if(r.url){
if(wUrls[r.id]!==r.url){destroyWPlayer(r.id);attachWPlayer(r.id,r.url,r.kind);}
else if(!wPlayers[r.id]){const v=document.getElementById('wv_'+r.id);
if(v&&!v.src&&!v.currentSrc)attachWPlayer(r.id,r.url,r.kind);}}
else ensureWUrl(r.id);});}
async function loadWatch(force){
try{
if(!wInit){wInit=true;
const q0=await api('/api/watch');const sel=document.getElementById('w_qn');
sel.innerHTML=(q0.qualities||['超清']).map(x=>'<option'+(x===q0.quality?' selected':'')+'>'+x+'</option>').join('');}
if(force){
const j0=await api('/api/watch');
for(const r of j0.rooms){if(r.live&&!wPlayers[r.id]){
try{const jr=await api('/api/watch',{method:'POST',body:JSON.stringify({action:'refresh',rid:r.id})});
if(jr.ok){wSig='';}}catch(e){}}}}
const j=await api('/api/watch');
const sig=j.rooms.map(r=>r.id+':'+(r.live?1:0)).join(',');
if(sig!==wSig){wSig=sig;renderWTiles(j.rooms);}
dmkSync(j.rooms.filter(r=>r.live).map(r=>String(r.id)));
attachWPending(j.rooms);
}catch(e){document.getElementById('msg_watch').textContent='监看加载失败：'+e.message;}
}
loadWatch();setInterval(loadWatch,30000);setInterval(()=>{api('/api/watch',
{method:'POST',body:JSON.stringify({action:'heartbeat'})}).catch(()=>{});},30000);
</script></body></html>
"""


def main():
    pw = ensure_webui_password()
    server = ThreadingHTTPServer((os.environ.get("WEBUI_HOST", "127.0.0.1"), PORT), Handler)
    server.webui_password = pw
    server.daemon_threads = True
    threading.Thread(target=_room_info_refresher, daemon=True).start()
    threading.Thread(target=_stream_refresher, daemon=True).start()
    print(f"[webui] listening on 127.0.0.1:{PORT} (admin/{'*' * 8})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
