# 斗鱼弹幕 AI 回复机器人

读取指定斗鱼直播间实时弹幕，内容命中触发词后转发给 Gemini，AI 回复自动发回弹幕。
支持多房间、多账号轮流发送，带 Web 管理后台。

## 架构

```
斗鱼弹幕 WSS ──→ bot.py ──┬── 关键词匹配 ──→ Gemini API（直连 Key / 自建中继二选一）
                           ├── 发送队列（5s 限流）──→ 斗鱼发弹幕网关
                           └── 日志 data/bot.log
```

- **收弹幕**：`wss://danmuproxy.douyu.com:8501~8506`，无需登录，6 线路轮换 + 断线重连
- **发弹幕**：`wss://wsproxy.douyu.com:6675`，需要斗鱼扫码登录
- **AI 调用**：直连模式用你自己的 Gemini API Key；也可以自建中继（见下文）
- 协议实现见 `douyu_proto.py`（STT 封包/解包、vk 签名）

## 快速开始（Docker Compose）

```bash
# 1. 准备配置
cp .env.example .env                 # 填 WEBUI_PASSWORD 和 GEMINI_API_KEY
cp config.example.yaml config.yaml   # 填房间号和触发词
mkdir -p data

# 2. 启动
docker compose up -d --build

# 3. 打开 Web 管理后台：http://<服务器IP>:18021（用户名 admin）
#    在「账号登录」卡片用斗鱼 APP 扫码登录，即可开始收发弹幕

# 看日志
docker logs -f douyu-ai-bot
```

### 裸机运行（不用 Docker）

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && cp config.example.yaml config.yaml   # 填好
./venv/bin/python bot.py        # 机器人
./venv/bin/python webui.py      # Web 后台（另一个终端）
```

## 配置说明

`.env`：

| 项 | 说明 |
|---|---|
| `WEBUI_PASSWORD` | Web 后台密码（用户名固定 `admin`），必填 |
| `GEMINI_API_KEY` | 直连模式：你的 Gemini Key（[获取](https://aistudio.google.com/apikey)） |
| `GEMINI_RELAY_URL` | 中继模式：自建中继地址；直连模式留空 |
| `RELAY_TOKEN` | 中继鉴权口令（与中继端一致） |
| `PROXY_URL` | 可选，HTTP 代理如 `http://127.0.0.1:8080`；为空直连 |

`config.yaml`：

| 项 | 说明 |
|---|---|
| `rooms[].id` | 斗鱼房间号 |
| `rooms[].mode` | `contains` 含关键词即触发；`mention` 以前缀开头才触发 |
| `rooms[].keywords` | 触发词列表 |
| `rooms[].offline_monitor` | 下播后是否继续监控（默认 true） |
| `rooms[].enabled` | 手动启停（WebUI 可点） |
| `accounts[]` | 发送账号列表（最多 10 个），按 round-robin 轮流发送 |
| `gemini.model` | 默认 `gemini-3-flash-preview` |
| `gemini.history_rounds` | 每房间对话记忆轮数（默认 6，0=关闭），多轮可连续对话 |
| `gemini.system` | AI 人设 |

## Web 管理后台功能

- **状态看板**：机器人运行状态、各房间开播状态、登录账号、AI 模式、命中记录、最近日志
- **房间管理**：增删房间、每房间触发词/模式/下播监控开关/暂停-恢复（保存后热加载，不中断其他房间）
- **AI 设置**：直连 Key / 中继地址填写（改动需重启机器人）
- **账号登录**：按账号生成二维码，斗鱼 APP 扫码即完成登录

## 中继模式（可选）

不想把 Gemini Key 放进机器人容器/服务器时，可以自建一个带口令鉴权的中继服务，
把 `GEMINI_RELAY_URL` 指向它（`RELAY_TOKEN` 两端一致）。中继接口：

```
POST {RELAY_URL}   Header: X-Relay-Token: <RELAY_TOKEN>
{"prompt": "...", "system": "...", "model": "...",
 "max_tokens": 150, "temperature": 0.9}
→ {"text": "..."} 或 {"error": "..."}
```

## 风控与限制

- 发弹幕走你的斗鱼账号：高频会触发账号级临时风控（冷却数十分钟），发送间隔建议 ≥5s
- 机器人不回复自己的弹幕（防自激循环），相同内容去重
- "仅粉丝可发言"的房间会静默吞掉回复
- 发弹幕的 vk 签名密钥是逆向值，斗鱼改 JS 后需重新提取

## 安全

- `.env`、`config.yaml`、`data/`（含登录 cookie）绝不要提交到 git
- Web 后台是 HTTP Basic Auth，不要直接暴露到公网；对外请用反向代理加 HTTPS
  或只监听内网（`WEBUI_HOST` 默认为 `127.0.0.1`）

## 免责声明

本项目通过非官方接口收发斗鱼弹幕，仅供学习研究。自动发送弹幕可能违反斗鱼
用户协议，存在账号被限制或封禁的风险，请自行评估后使用，风险自负。

## License

MIT
