#!/usr/bin/env python3
"""斗鱼弹幕 AI 回复机器人。

链路：收弹幕（WSS，无需登录）→ 关键词匹配 → Gemini → 发送队列（限流）→ 发弹幕（需扫码登录）。

配置：config.yaml（房间号、触发词、Gemini、人设、限流）
密钥：GEMINI_API_KEY 环境变量
登录态：data/cookie.txt（login.py 扫码生成）
代理：PROXY_URL 环境变量（如 http://127.0.0.1:8080）；为空则直连。
"""
import json
import os
import queue
import random
import re
import signal
import ssl
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
import websocket
import yaml

from douyu_proto import (
    DM_ENDPOINTS, SEND_ENDPOINT, UA,
    pack_frame, parse_stt, recv_heartbeat, recv_joingroup,
    recv_loginreq, send_chatmessage, send_loginreq, unpack_frames,
)

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CONFIG_PATH = os.environ.get("CONFIG_PATH", str(ROOT / "config.yaml"))
LOG_FILE = DATA / "bot.log"

PROXY_URL = os.environ.get("PROXY_URL", "").strip()
_pu = urlparse(PROXY_URL) if PROXY_URL else None
WS_PROXY_KWARGS = ({"http_proxy_host": _pu.hostname, "http_proxy_port": _pu.port or 3128}
                   if _pu and _pu.hostname else {})
# 斗鱼 WSS 的 TLS 握手在某些 OpenSSL 版本下失败，放宽证书校验
WS_SSLOPT = {"cert_reqs": ssl.CERT_NONE}
REQUESTS_PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else {}

_stop = threading.Event()
_log_lock = threading.Lock()


def log(msg: str):
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- 登录态（多账号） ----------
_devid_cache = {}


def load_cookie(path):
    try:
        s = Path(path).read_text("utf-8").strip()
        return s or None
    except OSError:
        return None


def parse_cookie_fields(raw: str) -> dict:
    fields = {}
    for p in raw.split(";"):
        if "=" in p:
            k, v = p.strip().split("=", 1)
            fields[unquote(k)] = unquote(v)
    return fields


def resolve_devid(cookie_raw, devid_file: Path) -> str:
    """dy_did 必须与登录态一致：cookie 内 dy_did → 专属 devid 文件 → 首次生成落盘。
    每个账号用独立的 devid 文件，避免多账号互相串号。"""
    key = str(devid_file)
    if _devid_cache.get(key):
        return _devid_cache[key]
    m = re.search(r"dy_did=([^;\s]+)", cookie_raw or "")
    devid = m.group(1) if m else None
    if not devid:
        try:
            devid = devid_file.read_text("utf-8").strip() or None
        except OSError:
            devid = None
    if not devid:
        devid = "".join(random.choice("0123456789abcdef") for _ in range(32))
        try:
            devid_file.parent.mkdir(parents=True, exist_ok=True)
            devid_file.write_text(devid, encoding="utf-8")
        except OSError:
            pass
    _devid_cache[key] = devid
    return devid


class Account:
    """一个斗鱼发送账号：独立 cookie 文件 + 独立 devid。"""

    def __init__(self, name: str, cookie_file: str):
        self.name = name
        self.cookie_file = ROOT / cookie_file if not os.path.isabs(cookie_file) else Path(cookie_file)
        # 兼容旧版：data/cookie.txt 继续用 data/devid.txt；新账号用同名 .devid.txt
        if self.cookie_file == DATA / "cookie.txt":
            self.devid_file = DATA / "devid.txt"
        else:
            self.devid_file = self.cookie_file.parent / (self.cookie_file.stem + ".devid.txt")

    def cookie(self):
        return load_cookie(self.cookie_file)

    def fields(self):
        raw = self.cookie()
        return parse_cookie_fields(raw) if raw else {}

    def uid(self):
        return self.fields().get("acf_uid", "")

    def nickname(self):
        """聊天中显示的昵称（acf_nickname），用于展示和过滤自己的弹幕；
        取不到则回退用户名/uid。"""
        f = self.fields()
        return (unquote(f.get("acf_nickname", ""))
                or unquote(f.get("acf_username", ""))
                or f.get("acf_uid", ""))

    def healthy(self):
        f = self.fields()
        return bool(f.get("acf_uid") and f.get("acf_dmjwt_token"))


# ---------- Gemini ----------
class GeminiClient:
    """AI 回复。两种模式（二选一）：
    1) 中继模式：GEMINI_RELAY_URL + GEMINI_RELAY_TOKEN（推荐，key 不进容器，
       走宿主机 gemini-relay 服务经保险库调用；请求直连不走代理）。
    2) 直连模式：GEMINI_API_KEY（通用镜像/自行部署用）。
    """

    def __init__(self, cfg: dict):
        self.key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.relay_url = os.environ.get("GEMINI_RELAY_URL", "").strip()
        self.relay_token = (os.environ.get("GEMINI_RELAY_TOKEN", "").strip()
                             or os.environ.get("RELAY_TOKEN", "").strip())
        self.model = cfg.get("model", "gemini-3-flash-preview")
        self.system_prompt = cfg.get("system_prompt", "")
        self.max_tokens = int(cfg.get("max_output_tokens", 150))
        self.temperature = float(cfg.get("temperature", 0.9))
        self.timeout = int(cfg.get("timeout_sec", 25))

    def reply(self, room_id: str, sender: str, content: str, history=None):
        """返回 AI 回复文本；失败返回 None（静默跳过）。
        history: [(sender, question, answer), ...] 最近的对话，用于多轮衔接。"""
        if history:
            lines = []
            for sdr, q, a in history:
                lines.append(f"观众「{sdr}」说：「{q[:80]}」")
                lines.append(f"你回复：「{a[:80]}」")
            hist_txt = "以下是本直播间最近的对话：\n" + "\n".join(lines) + "\n"
            prompt = (f"在斗鱼 {room_id} 号直播间里，你是一个风趣的 AI 助手。\n"
                      f"{hist_txt}"
                      f"现在观众「{sender}」发弹幕说：「{content}」。\n"
                      f"请结合上面的对话内容直接给出你的回复，不要带任何解释。")
        else:
            prompt = (f"在斗鱼 {room_id} 号直播间里，观众「{sender}」发弹幕说：「{content}」。"
                      f"请直接给出你的回复，不要带任何解释。")
        if self.relay_url and self.relay_token:
            return self._reply_relay(prompt)
        if self.key:
            return self._reply_direct(prompt)
        log("[ai] 未配置 GEMINI_RELAY_URL/GEMINI_API_KEY，跳过")
        return None

    def _reply_relay(self, prompt: str):
        # 中继在宿主机 127.0.0.1，直连不走代理
        no_proxy = {"http": None, "https": None}
        try:
            r = requests.post(
                self.relay_url,
                headers={"X-Relay-Token": self.relay_token},
                json={"prompt": prompt, "system": self.system_prompt,
                      "model": self.model, "max_tokens": self.max_tokens,
                      "temperature": self.temperature},
                proxies=no_proxy, timeout=self.timeout)
            j = r.json()
            if r.status_code != 200 or "text" not in j:
                log(f"[ai] 中继错误 {r.status_code}: {str(j)[:120]}")
                return None
            return j["text"].strip() or None
        except Exception as e:
            log(f"[ai] 中继异常: {type(e).__name__} {str(e)[:80]}")
            return None

    def _reply_direct(self, prompt: str):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent")
        body = {
            "systemInstruction": {"parts": [{"text": self.system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }
        try:
            r = requests.post(url, params={"key": self.key}, json=body,
                              proxies=REQUESTS_PROXIES, timeout=self.timeout)
            j = r.json()
            if r.status_code != 200:
                log(f"[ai] 接口错误 {r.status_code}: {str(j)[:120]}")
                return None
            cands = j.get("candidates") or []
            if not cands:
                log(f"[ai] 无候选回复: {str(j)[:120]}")
                return None
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            return text or None
        except Exception as e:
            log(f"[ai] 调用异常: {type(e).__name__} {str(e)[:80]}")
            return None


# ---------- 弹幕接收 ----------
class DanmakuReceiver(threading.Thread):
    """WSS 收弹幕（无需登录），断线自动换线路重连。收到 chatmsg 回调 on_msg(room_id, sender, content)。
    stop_ev 用于热加载时单独停止某个房间；全局 _stop 退出时同样生效。"""

    def __init__(self, room_id: str, on_msg, stop_ev=None):
        super().__init__(daemon=True)
        self.room_id = room_id
        self.on_msg = on_msg
        self.stop_ev = stop_ev

    def _stopped(self):
        return _stop.is_set() or (self.stop_ev is not None and self.stop_ev.is_set())

    def run(self):
        endpoints = DM_ENDPOINTS[:]
        while not self._stopped():
            random.shuffle(endpoints)
            for url in endpoints:
                if self._stopped():
                    return
                try:
                    self._serve(url)
                except Exception as e:
                    import traceback as _tb
                    log(f"[recv] {urlparse(url).hostname}: {type(e).__name__}: {str(e)[:120]}，5s 后重连")
                    log(f"[recv] traceback: {_tb.format_exc(limit=3)[-300:]}")
                    time.sleep(5)
            time.sleep(3)

    def _serve(self, url: str):
        ws = websocket.create_connection(
            url, timeout=50, sslopt=WS_SSLOPT, **WS_PROXY_KWARGS,
        )
        try:
            ws.send(pack_frame(recv_loginreq(self.room_id)))
            ws.send(pack_frame(recv_joingroup(self.room_id)))
            log(f"[recv] 已加入房间 {self.room_id}（{url.split('/')[2]}）")
            last_hb = time.time()
            while not self._stopped():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    raw = None
                # 无论是否有弹幕，每 40s 固定发一次心跳，避免活跃房间长期不触发超时分支
                if time.time() - last_hb >= 40:
                    try:
                        ws.send(pack_frame(recv_heartbeat()))
                        last_hb = time.time()
                    except Exception:
                        raise ConnectionError("heartbeat send failed")
                if raw is None:
                    continue  # 只是超时，心跳已在上面处理
                if not raw:
                    raise ConnectionError("empty frame")
                if isinstance(raw, str):
                    continue
                for body in unpack_frames(raw):
                    if not body.startswith("type@="):
                        continue
                    d = parse_stt(body)
                    if d.get("type") == "chatmsg":
                        sender = d.get("nn", "").strip()
                        content = d.get("txt", "").strip()
                        if sender and content:
                            self.on_msg(self.room_id, sender, content)
        finally:
            try:
                ws.close()
            except Exception:
                pass


# ---------- 弹幕发送（多账号轮流） ----------
class DanmakuSender:
    """账号池发送：每个账号独立 cookie/devid，按轮询（round-robin）分配发送任务。
    无可用账号 / 某账号登录态失效时自动跳过，保证不中断。"""

    def __init__(self, accounts):
        self.accounts = accounts
        self._rr = 0
        self._lock = threading.Lock()

    def _next_account(self):
        healthy = [a for a in self.accounts if a.healthy()]
        if not healthy:
            return None
        with self._lock:
            acct = healthy[self._rr % len(healthy)]
            self._rr += 1
        return acct

    def send(self, room_id: str, content: str):
        acct = self._next_account()
        if not acct:
            return False, "无可用账号：请先在 WebUI 扫码登录"
        cookie_raw = acct.cookie()
        f = parse_cookie_fields(cookie_raw)
        uid = f.get("acf_uid", "")
        jwt = f.get("acf_dmjwt_token", "")
        if not uid or not jwt:
            return False, f"账号 {acct.name} 登录态缺少关键字段，请重新扫码"
        devid = resolve_devid(cookie_raw, acct.devid_file)
        headers = {
            "Cookie": cookie_raw + "; dy_did=" + devid,
            "Origin": "https://www.douyu.com",
            "User-Agent": UA,
        }
        ws = websocket.create_connection(
            SEND_ENDPOINT, header=headers, timeout=10, sslopt=WS_SSLOPT, **WS_PROXY_KWARGS,
        )
        try:
            ws.send(pack_frame(send_loginreq(room_id, f, devid)))
            loginres = self._recv_until(ws, "loginres", 6)
            if not loginres or f"userid@={uid}" not in loginres:
                return False, f"账号 {acct.name} 网关登录被拒（可能风控或登录态过期），请重新扫码"
            ws.send(pack_frame(send_chatmessage(content, devid, uid)))
            echo = self._recv_until(ws, "chatmsg", 3)
            if echo:
                return True, f"已发送（{acct.name}）"
            return True, f"已提交（{acct.name}，公屏未回显，房间可能限制发言）"
        finally:
            try:
                ws.close()
            except Exception:
                pass

    @staticmethod
    def _recv_until(ws, want_type: str, timeout_s: float):
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                raw = ws.recv()
            except Exception:
                return None
            if isinstance(raw, str) or not raw:
                continue
            for body in unpack_frames(raw):
                d = parse_stt(body)
                if d.get("type") == "error":
                    log(f"[send] 网关错误码: {d.get('code')}")
                    return None
                if d.get("type") == want_type:
                    return body
        return None


# ---------- 关键词匹配 ----------
class Matcher:
    def __init__(self, cfg: dict, limits: dict):
        t = cfg.get("triggers", {})
        self.mode = t.get("mode", "contains")
        self.keywords = [k for k in t.get("keywords", []) if k]
        self.mention_prefix = t.get("mention_prefix", "@AI")
        self.cooldown = int(limits.get("user_cooldown_sec", 90))
        self.dedupe_win = int(limits.get("dedupe_window_sec", 300))
        self._user_last = {}
        self._dedupe = {}

    def match(self, sender: str, content: str, my_names: set) -> bool:
        if sender in my_names:
            return False  # 不回复自己账号的弹幕，防自激
        hit = False
        if self.mode == "mention":
            hit = content.startswith(self.mention_prefix)
        else:  # contains
            hit = any(k in content for k in self.keywords)
        if not hit:
            return False
        now = time.time()
        if now - self._user_last.get(sender, 0) < self.cooldown:
            return False
        dk = (sender, content)
        if dk in self._dedupe and now - self._dedupe[dk] < self.dedupe_win:
            return False
        self._user_last[sender] = now
        self._dedupe[dk] = now
        # 顺手清理过期去重记录
        if len(self._dedupe) > 2000:
            self._dedupe = {k: v for k, v in self._dedupe.items()
                            if now - v < self.dedupe_win}
        return True


def normalize_config(cfg: dict) -> dict:
    """兼容旧版单房间配置：room_id/triggers → rooms[0]；无 accounts 则用 data/cookie.txt 建默认账号。"""
    if "rooms" not in cfg and cfg.get("room_id"):
        t = cfg.get("triggers", {})
        cfg["rooms"] = [{
            "id": str(cfg["room_id"]),
            "keywords": t.get("keywords", []),
            "mode": t.get("mode", "contains"),
        }]
    if "accounts" not in cfg:
        cfg["accounts"] = [{"name": "默认账号", "cookie_file": "data/cookie.txt"}]
    return cfg


# ---------- 运行时状态（热加载时原地更新） ----------
state = {
    "matchers": {},        # room_id -> Matcher
    "receivers": {},       # room_id -> (thread, stop_ev)
    "room_opts": {},       # room_id -> {"offline_monitor": bool}
    "history": {},         # room_id -> deque[(sender, question, answer)] 最近对话
    "history_rounds": 6,   # 记住最近几轮，0=关闭
    "my_names": set(),
    "mention_prefix": "@AI",
    "send_interval": 5,
    "max_reply": 60,
}
sender_pool = None
_reload = threading.Event()
_first_load = True

ROOM_STATUS_FILE = DATA / "room_status.json"  # webui 后台线程写入的开播状态
STATUS_STALE_SEC = 1800  # 状态超过 30 分钟视为未知，未知时不断开（fail-open）


def remember(room_id: str, sdr: str, question: str, answer: str):
    """把一轮问答记入该房间的短期记忆（只记实际发出去的回复）。"""
    n = state["history_rounds"]
    if n <= 0:
        return
    hist = state["history"].setdefault(room_id, deque())
    hist.append((sdr, question, answer))
    while len(hist) > n:
        hist.popleft()


def room_is_live(rid):
    """返回 True（直播中）/ False（已下播）/ None（未知）。"""
    try:
        st = json.loads(ROOM_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    e = st.get(str(rid))
    if not isinstance(e, dict) or time.time() - e.get("ts", 0) > STATUS_STALE_SEC:
        return None
    return bool(e.get("live"))


def live_watchdog():
    """看门狗：对关闭了"下播后继续监控"的房间，下播时断开连接、开播时恢复。
    状态未知时不断开，避免误伤。"""
    while not _stop.is_set():
        time.sleep(60)
        try:
            for rid, opts in list(state["room_opts"].items()):
                if not opts.get("enabled", True):
                    continue  # 手动暂停的房间，看门狗不碰
                if opts.get("offline_monitor", True):
                    continue
                live = room_is_live(rid)
                running = rid in state["receivers"]
                if live is False and running:
                    thr, ev = state["receivers"].pop(rid)
                    ev.set()
                    log(f"[watchdog] 房间{rid} 已下播，停止监控（下播监控已关闭）")
                elif live is True and not running:
                    ev = threading.Event()
                    thr = DanmakuReceiver(rid, on_msg, ev)
                    state["receivers"][rid] = (thr, ev)
                    thr.start()
                    log(f"[watchdog] 房间{rid} 已开播，恢复监控")
        except Exception as e:
            log(f"[watchdog] 异常: {type(e).__name__} {str(e)[:80]}")


def on_msg(room_id: str, sdr: str, text: str):
    matcher = state["matchers"].get(room_id)
    if not matcher:
        return
    if matcher.match(sdr, text, state["my_names"]):
        log(f"[hit] [房间{room_id}] {sdr}: {text[:40]}")
        try:
            ai_q.put_nowait((room_id, sdr, text))
        except queue.Full:
            log("[hit] AI 队列满，丢弃")


def apply_config():
    """（重）读配置并对账：新增房间只启动新连接，删除房间只停该房间，
    已有房间的连接不受影响。账号/限流参数原地更新。"""
    global sender_pool, _first_load
    tag = "init" if _first_load else "reload"
    cfg = normalize_config(load_config())
    rooms = [r for r in cfg.get("rooms", []) if str(r.get("id", "")).isdigit()][:10]
    if not rooms:
        log("[reload] 配置里没有有效房间，跳过本次加载")
        return
    limits = cfg.get("limits", {})
    state["send_interval"] = int(limits.get("send_interval_sec", 5))
    state["max_reply"] = int(limits.get("max_reply_chars", 60))
    default_mode = cfg.get("triggers", {}).get("mode", "contains")
    state["mention_prefix"] = cfg.get("triggers", {}).get("mention_prefix", "@AI")

    new_ids = [str(r["id"]) for r in rooms]
    # 停掉被删除的房间
    for rid in list(state["receivers"]):
        if rid not in new_ids:
            thr, ev = state["receivers"].pop(rid)
            ev.set()
            state["matchers"].pop(rid, None)
            state["room_opts"].pop(rid, None)
            state["history"].pop(rid, None)
            log(f"[reload] 停止房间 {rid} 监控")
    # 新增房间建连接；所有房间刷新触发词与监控选项
    for r in rooms:
        rid = str(r["id"])
        opts = {"offline_monitor": bool(r.get("offline_monitor", True)),
                "enabled": bool(r.get("enabled", True))}
        state["room_opts"][rid] = opts
        state["matchers"][rid] = Matcher(
            {"triggers": {"mode": r.get("mode", default_mode),
                          "keywords": r.get("keywords", []),
                          "mention_prefix": state["mention_prefix"]}},
            limits)
        # 手动暂停 → 不建连接；下播监控关闭且当前下播 → 不建连接（看门狗会在开播时恢复）
        live = room_is_live(rid)
        want = opts["enabled"] and (opts["offline_monitor"] or live is not False)
        if want and rid not in state["receivers"]:
            ev = threading.Event()
            thr = DanmakuReceiver(rid, on_msg, ev)
            state["receivers"][rid] = (thr, ev)
            thr.start()
        elif not want and rid in state["receivers"]:
            thr, ev = state["receivers"].pop(rid)
            ev.set()
            reason = "已手动暂停" if not opts["enabled"] else "下播中且已关闭下播监控"
            log(f"[{tag}] 房间{rid}{reason}，停止监控")
    # 账号池原地更新
    new_accounts = [Account(str(a.get("name", f"账号{i+1}")),
                            str(a.get("cookie_file", f"data/cookies/acct{i+1}.txt")))
                    for i, a in enumerate(cfg.get("accounts", [])[:10])]
    if not new_accounts:
        log("[reload] 配置里没有发送账号，跳过账号更新")
    else:
        if sender_pool is None:
            sender_pool = DanmakuSender(new_accounts)
        else:
            sender_pool.accounts[:] = new_accounts
            sender_pool._rr = 0
        state["my_names"] = {a.nickname() for a in new_accounts if a.nickname()}
    # Gemini 参数热更新（模型/人设/温度，无需重启）
    g = cfg.get("gemini", {})
    try:
        if g.get("model"):
            gemini.model = str(g["model"])
        if g.get("system_prompt"):
            gemini.system_prompt = str(g["system_prompt"])
        if g.get("max_output_tokens"):
            gemini.max_tokens = int(g["max_output_tokens"])
        if g.get("temperature") is not None:
            gemini.temperature = float(g["temperature"])
    except Exception:
        pass
    state["history_rounds"] = max(0, min(20, int(g.get("history_rounds", 6))))
    _first_load = False
    logged = [f"{a.name}(uid={a.uid()})" for a in new_accounts if a.healthy()]
    log(f"[{tag}] 房间: {', '.join(new_ids)}；已登录账号: {logged or '无（只能收不能发）'}")
    for rid in new_ids:
        m = state["matchers"][rid]
        log(f"[{tag}] [房间{rid}] 触发词: {m.keywords or state['mention_prefix']}（{m.mode}）")


def ai_worker():
    while not _stop.is_set():
        try:
            room_id, sdr, text = ai_q.get(timeout=1)
        except queue.Empty:
            continue
        matcher = state["matchers"].get(room_id)
        prefix = state["mention_prefix"]
        # mention 模式下去掉前缀再问 AI
        q = text[len(prefix):].strip() if matcher and matcher.mode == "mention" else text
        reply = gemini.reply(room_id, sdr, q or text,
                             history=list(state["history"].get(room_id, ())))
        if reply:
            reply = reply.replace("\n", " ").strip()
            if len(reply) > state["max_reply"]:
                reply = reply[:state["max_reply"]] + "…"
            remember(room_id, sdr, (q or text)[:80], reply)
            try:
                send_q.put_nowait((room_id, sdr, reply, 0))
            except queue.Full:
                log("[ai] 发送队列满，丢弃回复")


def send_worker():
    last_send = 0.0
    max_attempts = 3
    while not _stop.is_set():
        try:
            item = send_q.get(timeout=1)
        except queue.Empty:
            continue
        room_id, sdr, reply, attempt = item if len(item) == 4 else (*item[:3], 0)
        wait = state["send_interval"] - (time.time() - last_send)
        if wait > 0:
            time.sleep(wait)
        if _stop.is_set():
            return
        text = f"@{sdr} {reply}"
        try:
            ok, msg = sender_pool.send(room_id, text)
            log(f"[send] [房间{room_id}] @{sdr}: {'OK' if ok else 'FAIL'} {msg}")
        except Exception as e:
            if attempt + 1 < max_attempts:
                log(f"[send] [房间{room_id}] 异常: {type(e).__name__} {str(e)[:80]}，60s 后重试({attempt + 2}/{max_attempts})")
                try:
                    send_q.put_nowait((room_id, sdr, reply, attempt + 1))
                except queue.Full:
                    log("[send] 队列满，丢弃该回复")
                time.sleep(60)
            else:
                log(f"[send] [房间{room_id}] 异常: {type(e).__name__} {str(e)[:80]}，已达最大重试次数，丢弃")
        last_send = time.time()


def main():
    global ai_q, send_q, gemini
    cfg = load_config()
    gemini = GeminiClient(cfg.get("gemini", {}))
    ai_q = queue.Queue(maxsize=200)
    send_q = queue.Queue(maxsize=200)
    try:
        (DATA / "bot.pid").write_text(str(os.getpid()))
    except Exception:
        pass

    apply_config()

    threading.Thread(target=ai_worker, daemon=True).start()
    threading.Thread(target=send_worker, daemon=True).start()
    threading.Thread(target=live_watchdog, daemon=True).start()

    def _sig_term(*_):
        _stop.set()
    def _sig_hup(*_):
        _reload.set()
    signal.signal(signal.SIGTERM, _sig_term)
    signal.signal(signal.SIGINT, _sig_term)
    signal.signal(signal.SIGHUP, _sig_hup)
    log("[init] 机器人启动，Ctrl+C 退出")
    while not _stop.is_set():
        if _reload.is_set():
            _reload.clear()
            try:
                apply_config()
                log("[reload] 配置已热加载，现有房间监控未中断")
            except Exception as e:
                log(f"[reload] 失败: {type(e).__name__} {str(e)[:100]}")
        time.sleep(1)
    log("[init] 已退出")


if __name__ == "__main__":
    main()
