# PP 主程序 说明文档

> 目录：`D:/MyProject/Pragmatic Play Blackjack/PP主程序/`
> 最后更新：2026-10-20（commit `dc7c2c9`）
> 本文档只讲**主程序（网络端）**。自动下注程序 AutoBot 是另一套，两者独立运行、互不依赖。

---

## 一、这是什么

**主程序 = 数据采集 + 网络端看板。**

它干三件事：

1. **登录** 198198 帐号，拿到 PP 桌台的 wss 连接地址
2. **连上 PP 桌台**，实时收牌，算 TC / PDEV
3. **把每桌状态推到网络端**，在网页上输入玩家名字，就能看到牌局详情和 EV 决策

一句话：**它是"看"的工具，不是"下注"的工具。**

| | 主程序（本文档） | AutoBot |
|---|---|---|
| 目录 | `PP主程序/` | `AutoBot/` |
| 干什么 | 采集数据 + 网页看板 + 算 EV 提示 | 自动坐下、自动下注 |
| 怎么用 | 浏览器打开 `http://<IP>:8888` | 跑 exe，看终端面板 |
| 会不会下注 | **不会** | 会 |

---

## 二、文件清单

### 核心文件（必须有）

| 文件 | 作用 |
|---|---|
| `PPBJ.py` | **数据管道**。连 PP 桌台 wss，收帧记牌，算 TC/PDEV，把结果 publish 到 MQTT |
| `pp_receiver_vps.py` | **网络端服务**。订阅 MQTT，提供网页（端口 8888）和 `/ev` 计算接口 |
| `PP EV.py` | **EV 决策引擎**。输入已出牌 + 庄家明牌 + 自己手牌，输出各动作 EV 排名 |
| `pdev_local.py` | **PDEV 引擎**。composition-dependent 期望值，含 ECU（提前兑现）计价表 |

### 登录 / 抓链接

| 文件 | 作用 |
|---|---|
| `login_198198.py` | AES 加密密码登录 198198，列出游戏、进入游戏、拿 loadUrl |
| `WSS.py` | 用 Playwright 打开浏览器，自动登录并抓取桌台的 wss URL |
| `Build_WS_URLs.py` | 把 JSESSIONID + 桌号拼成完整 wss URL，写入 `WS_Config.txt` |
| `restart_chain.py` | 刷新链：停 PPBJ → 重跑 WSS.py 抓新链接 → 重启 PPBJ |

### 配置 / 数据

| 文件 | 作用 |
|---|---|
| `config.ini` | 198198 帐号（前缀 + 起止编号 + 统一密码）、网页 Reboot 密码 |
| `WS_Config.txt` | 每行一个完整桌台 wss URL（含 JSESSIONID 和 tableId） |
| `table_id.txt` | `桌名 \| 桌号`，把 tableId 转成看得懂的名字 |
| `default_tables.txt` | 备用桌名列表 |
| `requirements.txt` | Python 依赖 |

### 测试

| 文件 | 作用 |
|---|---|
| `_selftest_decision_cache.py` | 验证"两个座位交替决策时决策标记不互相覆盖" |

---

## 三、数据流

```
198198 网站
   │ login_198198.py / WSS.py 登录
   ↓
WS_Config.txt（完整 wss URL，含 JSESSIONID）
   │
   ↓ PPBJ.py 连每张桌
PP 桌台 WebSocket
   │ playerSeat / betsopen / voip_cc 帧
   ↓
PPBJ.py 记牌 → TC、PDEV
   │ MQTT publish
   ↓ 主题 pp/table/<桌名>
本地 MQTT Broker（127.0.0.1:8881）
   │
   ↓ 订阅 pp/table/#
pp_receiver_vps.py（端口 8888）
   │
   ├─→ 浏览器网页（WebSocket 实时推送）
   └─→ POST /ev → PP EV.py → 返回 EV 排名
```

**关键点：网络端本身不算 EV。** 它只是把数据转给 `PP EV.py` 算，再把结果贴到网页上。

---

## 四、MQTT 主题

Broker 在 `127.0.0.1:8881`，账号密码都是 `MQTT@@##admin`。

| 主题 | 谁发 | 内容 |
|---|---|---|
| `pp/table/<桌名>` | PPBJ.py | 每桌完整快照：TC、PDEV、已出牌、庄家明牌、每个座位的牌 + 决策状态 |
| `pp/decision/<桌名>` | PPBJ.py | 单条决策邀请（`decisionOffer` / `currentDecision` / `cashOut`） |
| `pp/gameend/<桌名>` | PPBJ.py | 一局结束 |
| `pp/offertimer/<桌名>` | PPBJ.py | 保险邀请定时 |

### `pp/table` 的 payload 结构

```json
{
  "tc": 1.25,
  "pdev": -0.0053,
  "total": 128,
  "cards": ["2", "5", "A"],
  "timestamp": "2026-10-20 21:30:00",
  "dealer": { "score": "8", "cards": ["8"] },
  "players": {
    "1_h0": { "score": "16", "cards": ["10", "6"], "bet": 10,
              "canDecide": true, "cashOut": 12.5 },
    "3_h0": { "score": "14", "cards": ["9", "5"], "bet": 10,
              "canDecide": true }
  },
  "player_names": { "1": "EyeBot_01", "3": "EyeBot_05" }
}
```

- `players` 的 key 格式：`<座位号>_<手号>`，座位号 1-7（0 = 庄家），手号 `h0` / `h1`（拆牌）
- `canDecide` = 这一手轮到决策（**这是触发 EV 的关键字段**）
- `cashOut` = 可提前兑现的金额，不是每手都有

---

## 五、网页怎么用

浏览器打开 `http://<服务器IP>:8888`

1. 右上角 **user** 按钮 → 弹出 **Track Player**
2. 填 **Player name**（玩家名）和 **Bet amount**（注额）
3. 确认后，只要这个玩家出现在任何一桌，面板自动跟随
4. 面板里每个座位显示：座位号、玩家名、手牌、点数、EV 决策

### 网页上显示什么

- **黄字金额** = CashOut 报价（不是每手都有）
- **EV 小格** = 决策建议，例如 `S -0.38` / `H -0.51` / `SR +0.25`
  - `H` = Hit（要牌）、`S` = Stand（停牌）、`D` = Double（加倍）
  - `SR` = Surrender / CashOut（兑现）、`P` = Split（分牌）

### 页面按钮

- **Reboot** = 重启 PPBJ（会重新抓链接）。密码在 `config.ini` 的 `[web] restart_password`，默认 `PragmaticPlay`

### HTTP 接口

| 路由 | 方法 | 作用 |
|---|---|---|
| `/` | GET | 网页本身 |
| `/ws` | WS | 实时推送（MQTT 消息转给浏览器） |
| `/ev` | POST | 算 EV。参数 `{cards, dealer, player, cashOut, betAmount}` |
| `/restart` | POST | 重启 PPBJ，body 带 `{password}` |
| `/restart_status` | GET | 查重启进度 |
| `/status` | GET | 查 MQTT 连接状态 |

---

## 六、启动方式

### 首次准备

```bash
pip install -r requirements.txt
```

`WSS.py` 还需要浏览器内核：

```bash
python -m playwright install chromium
```

### 正常启动顺序

```
1. MQTT Broker 先起（127.0.0.1:8881）
2. python pp_receiver_vps.py      ← 网络端，占 8888
3. python PPBJ.py                 ← 数据管道（读 WS_Config.txt）
```

### 需要刷新桌台链接时

```bash
python restart_chain.py
```

它会：停 PPBJ → 跑 `WSS.py`（内部调 `Build_WS_URLs.py`）→ 重写 `WS_Config.txt` → 重启 PPBJ。

---

## 七、配置说明

### `config.ini`

```ini
[198198]
username_prefix = EyeBot_     # 帐号前缀
username_start  = 01          # 起始编号
username_end    = 16          # 结束编号 → EyeBot_01 … EyeBot_16
password        = Aaaa7788!   # 统一密码
base_url        = https://www.198198.io

[web]
restart_password = PragmaticPlay   # 网页 Reboot 密码
```

### `table_id.txt`

```
Blackjack 1 | m24f0zhzupicvchc
Blackjack 2 | 22cm03wwntad3rjv
```

左边是显示名，右边是 PP 的 tableId（也是 wss URL 里的 `tableId` 参数）。

### `pp_receiver_vps.py` 顶部常量

```python
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 8881
HTTP_PORT = 8888
```

---

## 八、EV 触发规则（重要）

**轮到你这一手决策，就出 EV。** 不再要求必须有 CashOut。

前端判断逻辑：

```javascript
const _hasCo = (h.cashOut !== undefined && h.cashOut !== null)
if (!h.canDecide && !_hasCo) continue    // 两者都没有才跳过
```

`canDecide` 由 `PPBJ.py` 判定：

- 服务器给了 `decisionOffer` → `canDecide = true`
- 这一手打完（`activeHand=false` 且无 `decisionTimer`）→ 撤掉标记
- **新一局（`betsopen`）→ 整个缓存清空**，防止上一局的 cashOut 串到本局

### 决策缓存 `decision_cache`

```python
decision_cache = {}   # 桌名 → { "1_h0": {"canDecide": True, "cashOut": 12.5}, … }
```

**为什么需要它**：PP 服务器一次只推**一个座位**的帧。如果每帧重建决策表，同一份数据里就只有那一个座位，另一手永远没有决策信息 → 网络端那手永远不算 EV。

改成整局累积之后，**每次推送都带着全部座位的决策状态**，不管哪一帧先到、哪一帧后到，两个座位都能算 EV。

---

## 九、坑与注意事项

### 1. MQTT 必须先起

`PPBJ.py` 和 `pp_receiver_vps.py` 都硬连 `127.0.0.1:8881`。Broker 没起 → PPBJ 启动就报错。

### 2. JSESSIONID 会过期

`WS_Config.txt` 里的链接带 JSESSIONID，几分钟到几小时就失效。表现：桌台连不上或没数据。解决：跑 `restart_chain.py` 重新抓。

### 3. 两个文件必须同时更新

`PPBJ.py` 和 `pp_receiver_vps.py` 之间有 `canDecide` 这个约定：

- 只换 `PPBJ.py` → 前端不认 `canDecide`，等于没改
- 只换前端 → 后端不发 `canDecide`，也等于没改

### 4. 改完要重启两个进程

`PPBJ.py` 和 `pp_receiver_vps.py` 都是长驻进程。改完文件必须**两个都重启**才生效。

### 5. 备份位置

```
PP主程序/_backups/<文件名>.bak-before-<改动>-<HHMMSS>
```

### 6. EV 缓存 key 含 cashOut 和 betAmount

```javascript
const key = cards + "|" + dealer + "|" + player + "|co=" + cashOut + "|bet=" + betAmount
```

原来 key 只看牌面，导致"同一手先用无兑现价算过一次，之后有 cashOut 还拿旧结果"。现在不会再串。

### 7. 网页去抖已去掉

原来 `pp/table` 有 250ms 去抖（后一条取消前一条）。实测同桌帧间隔 **38~44% 在 250ms 以内**，两个座位几乎同时出决策时，前一个会被直接吞掉。现在改成逐条处理。

---

## 十、排错速查

| 现象 | 先查什么 |
|---|---|
| 网页打不开 | `pp_receiver_vps.py` 有没有跑？端口 8888 通不通？ |
| 网页开了但没数据 | MQTT 起了吗？`PPBJ.py` 跑了吗？`WS_Config.txt` 的 JSESSIONID 过期没？ |
| 有牌但 TC 一直是 0 | 桌没接管（`voip_cc` 换鞋信号没到），或 feed 没给基线 |
| 有牌但 EV 不出 | 那一手有没有 `canDecide`？玩家名匹配上了吗（Track Player 填的名字）？ |
| 两个座位只出一个 EV | 检查两个文件是不是都更新了、都重启了 |
| EV 数字明显不对 | `betAmount` 填对了吗？CashOut 是绝对金额，要除以注额才是 EV |

---

## 十一、改动记录

| 日期 | 改动 |
|---|---|
| 2026-10-20 | 决策提示改为「轮到决策即算 EV」，不再只认 CashOut。新增 `decision_cache` 整局累积；前端触发条件改 `canDecide \|\| cashOut`；EV 缓存 key 补 cashOut / betAmount；去掉 250ms 去抖 |

---

## 附录：`login_198198.py` 单独用法

这部分是登录脚本的原始说明，保留备查。

### 运行

```bat
pip install -r requirements.txt
python login_198198.py
```

### 查看和进入游戏

```bat
python login_198198.py --list-games        # 列出可玩游戏
python login_198198.py --find Speed        # 按名称/代码过滤
python login_198198.py --game PGS_135      # 直接进游戏，打印 loadUrl 及参数
python login_198198.py --game 402
python login_198198.py --game ig-33
```

`--game` 会调 `/api/playGame`，打印 `loadUrl`，并把查询字符串里的参数（`gameCode`、`uuid`、`token` 等）解析成 JSON 显示。

### 抓桌台 wss URL

```bat
python -m playwright install chromium
python WSS.py
```

程序自动登录，进入 PP 真人游戏，抓取游戏建立的 `wss://` 连接，完整 URL 写入 `WS_Config.txt`。

支持批量账号（按 `config.ini` 的前缀 + 编号范围生成）：

```ini
[198198]
username_prefix = EyeBot_
username_start = 01
username_end = 15
password = 统一密码
```

生成 `EyeBot_01` ~ `EyeBot_15`，并发处理（默认 3 个，可用 `--workers 5` 调整），全部抓完才一次性写入。

也可以指定游戏：

```bat
python WSS.py --game 402
```

### 说明

- 密码在本地用网站要求的 AES 方式加密后再发送，不会明文出现在请求里
- `target_url` 留空则只登录并输出 token，不请求页面
- 登录成功后脚本会把 token 作为请求头附加到会话，再请求目标页面
