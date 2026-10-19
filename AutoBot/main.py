#!/usr/bin/env python3
r"""
PP 自动程序（Python 版 · 新建文件夹，不修改原文件夹任何文件）
跨平台：macOS 与 Windows 共用同一份代码（启动脚本：启动.command / start.bat）

流程：
  0. 启动直接读入完整 wss URL，从 URL 里取 JSESSIONID 与 tableId（只有这一种登入方式）
  1. 登入方式固定为「粘贴 URL」：JSESSIONID 直接用粘贴进来的那个
     （原「自动登入 198198 / Playwright」那套已整体移除 → 不再需要 pycryptodome、playwright）
  3. 订阅 TC 数据源 ws://pp.knewb2u.com:8888/ws，实时维护 {桌名 → TC} 与完整快照
  4. 选桌：**直连模式（USE_PASTED_TABLE = True）＝ 粘贴 URL 里是哪个 tableId 就连那张**，
     掷骰选桌（≥7 正数池 / <7 负数池 / 只收有空位的桌 / 池空等 5 秒重掷）代码保留，开关设 False 即恢复
  5. tableId → tableConfig API → 构建 wss URL（顺带取该桌最低注额）
  6. 连接后静默收帧（不打印原始帧）；固定板面显示：桌名/座位/牌靴已出牌/TC/各家点数/EV/动作
  7. 找空位坐下 → 开局(betsopen)后随机 3–5 秒押主注（金额 = 该桌最低限额）
  8. 接管 TC 自算（betsopen 时导入 feed 完整已出牌作基线；voip_cc 换鞋清空）
  9. 轮到我（decisionOffer + activeHand）→ 把手牌 / 庄家明牌 / 全 shoe 已出牌交给
     PP EV.py 算最优动作 → 随机 3–5 秒后提交 hit / stand / double / split / cashOut
 10. 庄家明牌 A：收到保险邀请(notinsured) → 按保险 EV(3p-1) 自动买 / 不买并回执
 11. 拉黑只在「下注被拒」时发生：placebetError / betValidationError / 下了注整局没收到牌
     满桌（没空位）/ 久坐被踢 / 断线 → 只断开回去重掷，不拉黑（下一轮还可能再抽到）
"""

import os, sys, json, time, random, asyncio, configparser, importlib.util, unicodedata
import hashlib, socket, uuid          # 机器指纹：自动区分「这是哪一台 VPS」
import threading
import traceback
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qs

# Windows 控制台默认 GBK，先把 stdout/stderr 切到 UTF-8 ——
# 否则打印 emoji（❌📋🧠 等）与板面边框字符会抛 UnicodeEncodeError 直接崩
if os.name == "nt":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


# 崩溃兜底：双击 .py 运行时窗口默认一闪就关，看不到出错原因。
# 这里挂住所有未捕获异常（含 import 阶段失败的），打印完整 traceback 并停住等回车。
def _crash_hold(exc_type, exc_value, exc_tb):
    try:
        print("\033[?25h", end="", flush=True)     # 恢复光标（面板会把它藏起来）
    except Exception:
        pass
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)   # Ctrl+C 按原样处理，不停
        return
    traceback.print_exception(exc_type, exc_value, exc_tb)
    print("\n[💥] Program crashed — see the error above.")
    try:
        # 只有在真正的终端里才等回车，管道/计划任务等非交互场景不会卡住
        if sys.stdin is not None and sys.stdin.isatty():
            input("Press Enter to close ...")
    except Exception:
        pass


sys.excepthook = _crash_hold

# 自动切换到项目 venv 的 Python（跨平台：Windows 走 Scripts\python.exe）
# PyInstaller 打包后（frozen）：没有 venv，数据文件（PP EV.py / table_id.txt / bot_config.ini）
# 被解压到 sys._MEIPASS → 直接用解包目录当根，路径才读得到。
_FROZEN = getattr(sys, "frozen", False)
if _FROZEN:
    _HERE = Path(getattr(sys, "_MEIPASS", str(Path(sys.executable).resolve().parent)))
else:
    # 项目根：常规布局 = 上一级（PP自动程序/ 的父目录）；自包含部署 = 本目录（依赖与本文件同级）
    _HERE = Path(__file__).resolve().parent
ROOT = _HERE if (_HERE / "table_id.txt").exists() else _HERE.parent
if os.name == "nt":
    _venv_py = ROOT / "venv" / "Scripts" / "python.exe"
else:
    _venv_py = ROOT / "venv" / "bin" / "python3"
# 用 normcase 比较，避免 Windows 盘符/大小写差异导致误判 → 反复重启自己
_same_exe = (os.path.normcase(os.path.abspath(sys.executable))
             == os.path.normcase(os.path.abspath(str(_venv_py))))
if not _FROZEN and not _same_exe and _venv_py.exists():
    # 注意：Windows 下 os.execv 不会给带空格的路径加引号（本项目路径 "PP AutoBot" 含空格），
    # 会把命令行解析成 C:\Users\user\Desktop\PP + 其余部分 → 报 "can't open file ..."。
    # 改用 subprocess（内部 list2cmdline 会自动加引号），父进程等子进程结束再退出。
    import subprocess
    sys.exit(subprocess.call([str(_venv_py)] + sys.argv))

sys.path.insert(0, str(ROOT))

# 静音 requests 的 RequestsDependencyWarning —— 本机「应用程序控制策略」会拦 charset_normalizer
# 自带的 cd.pyd，requests 因此误判「找不到字符检测库」而打这行警告。纯外观，请求本身正常。
# 注意：过滤器必须在 import requests 之前装好（该警告是 requests 在 import 那一刻发出的）。
import warnings
warnings.filterwarnings("ignore", message=r".*character detection dependency.*")

import requests
import websockets
from colorama import init, Fore, Style

# ══════════════ DNS 兜底 ══════════════
# 本机路由器/ISP 的 DNS 会把 PP 的 API 域名解析到一个「死」IP：
#   实测 games.pragmaticplaylive.net → 175.139.142.25（443 全超时，连不上）
#   而 8.8.8.8 / 1.1.1.1          → 18.67.181.x（AWS CloudFront，0.01s 就连上）
# 后果就是 build_ws_url() 的 tableConfig 请求 ConnectTimeout，选桌一直失败。
# 解决：对 pragmaticplaylive.net 下的域名改用公共 DNS 解析（UDP 53，结果缓存），
#       公共 DNS 拿不到结果时自动回退系统 DNS。完全可逆：删掉本段即恢复原行为。
import socket, struct

_DNS_SUFFIX = "pragmaticplaylive.net"
_DNS_SERVERS = ("8.8.8.8", "1.1.1.1")
_dns_cache = {}          # host -> [ip, ...]（只缓存成功结果）
_dns_retry_after = 0.0   # 公共 DNS 失败后的冷却截止时间（避免每次请求都卡 3 秒）
_orig_getaddrinfo = socket.getaddrinfo


def _dns_query_a(server: str, name: str, timeout: float = 3.0):
    """手搓一个 A 记录 DNS 查询（UDP 53），返回 IPv4 列表。"""
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00" + struct.pack(">HH", 1, 1)
    pkt = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + q
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (server, 53))
        data, _ = s.recvfrom(2048)
    finally:
        s.close()
    ancount = struct.unpack(">H", data[6:8])[0]
    idx = 12
    while data[idx] != 0:                      # 跳过问题段里的域名
        idx += data[idx] + 1
    idx += 5
    ips = []
    for _ in range(ancount):
        if data[idx] & 0xC0 == 0xC0:           # 压缩指针
            idx += 2
        else:
            while data[idx] != 0:
                idx += data[idx] + 1
            idx += 1
        typ, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[idx:idx + 10])
        idx += 10
        if typ == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in data[idx:idx + 4]))
        idx += rdlen
    return ips


def _patched_getaddrinfo(host, port, *args, **kwargs):
    """只拦 pragmaticplaylive.net：先用公共 DNS 拿 IP，再交给原生解析器。"""
    global _dns_retry_after
    if isinstance(host, str) and host.endswith(_DNS_SUFFIX):
        ips = _dns_cache.get(host)
        if ips is None and time.time() >= _dns_retry_after:
            for srv in _DNS_SERVERS:
                try:
                    ips = _dns_query_a(srv, host)
                except Exception:
                    continue
                if ips:
                    _dns_cache[host] = ips
                    print(f"  [🌐] DNS override: {host} -> {', '.join(ips)}  (via {srv})", flush=True)
                    break
            if not ips:
                _dns_retry_after = time.time() + 30   # 30 秒内不再试公共 DNS，直接走系统 DNS
        if ips:
            resolved = []
            for ip in ips:
                try:
                    resolved.extend(_orig_getaddrinfo(ip, port, *args, **kwargs))
                except Exception:
                    pass
            if resolved:
                return resolved
    return _orig_getaddrinfo(host, port, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo

# 登入方式只有一种：「粘贴完整 wss URL」。
# 原本的自动登入（login_198198 → pycryptodome；WSS → playwright）已整体移除 ——
# 那两个库都带未签名 .pyd，本机「应用程序控制策略」会拦，而且现在也用不到。

# ══════════════ numpy 兜底 ══════════════
# 本机若开着「应用程序控制策略」(WinError 4551)，numpy 的 .pyd 会被系统拦住（无签名）。
# PP EV.py 对 numpy 的用法只有一件事：np.zeros(shape, dtype) 建零数组，
# 之后全是 arr[i] / arr[i, j] 下标读写 + 一处 .shape[1]，数学语义与纯 Python 嵌套列表完全一致。
# 所以真 numpy 起不来时，往 sys.modules 塞一个只实现 zeros 的替身 —— PP EV.py 一个字都不用改，
# 换回正常机器时真 numpy 会自动生效，这个替身完全不参与。
def _numpy_fallback():
    import types

    class _Zeros:
        __slots__ = ("_d", "shape")

        def __init__(self, shape, dtype=float):
            shape = (shape,) if isinstance(shape, int) else tuple(shape)
            self.shape = shape
            fill = 0 if dtype is int else 0.0
            self._d = ([fill] * shape[0] if len(shape) == 1
                       else [[fill] * shape[1] for _ in range(shape[0])])

        def __getitem__(self, k):
            return self._d[k[0]][k[1]] if isinstance(k, tuple) else self._d[k]

        def __setitem__(self, k, v):
            if isinstance(k, tuple):
                self._d[k[0]][k[1]] = v
            else:
                self._d[k] = v

    _m = types.ModuleType("numpy")
    _m.zeros = _Zeros
    sys.modules["numpy"] = _m


try:
    import numpy  # noqa: F401 —— 没被策略拦就用真 numpy，行为与本机原本完全一致
    del numpy
except Exception as _np_err:
    # 清掉半残的 numpy 子模块，免得 PP EV.py 拿到坏模块
    for _k in [k for k in list(sys.modules) if k == "numpy" or k.startswith("numpy.")]:
        sys.modules.pop(_k, None)
    _numpy_fallback()
    print(f"  [!] numpy blocked by system policy ({_np_err.__class__.__name__}) "
          f"— PP EV.py will use the built-in zeros shim")

# 复用 EV 程序（只 import，不修改）—— composition-dependent，用 8 副牌剩牌全量推演
_ev_spec = importlib.util.spec_from_file_location("pp_ev", ROOT / "PP EV.py")
_ev_mod = importlib.util.module_from_spec(_ev_spec)
_ev_spec.loader.exec_module(_ev_mod)
calc_ev = _ev_mod.calc_ev

# PDEV 本地引擎（EVO 同款）—— pdev_local.py 是 pdev_local.js 的逐帧等价移植。
# 除 TC 外再多算一个 composition-dependent 的精准期望值。
# 口径：老板口径（明牌 A 只归一化不扣 BJ）· BJ 赔付 1.2 · S17 · 明牌 10 无 peek · 含 ECU（cashout 第 5 候选，定价表在 pdev_local.ECO_RATES）。
_pdev_spec = importlib.util.spec_from_file_location("pdev_local", ROOT / "pdev_local.py")
_pdev_mod = importlib.util.module_from_spec(_pdev_spec)
_pdev_spec.loader.exec_module(_pdev_mod)
compute_pdev = _pdev_mod.compute_pdev

init(autoreset=True)

HERE = _HERE   # 打包(frozen)后 = sys._MEIPASS；否则 = main.py 所在目录

# ─────────────────────────── 配置 ───────────────────────────
_cfg = configparser.ConfigParser()
_cfg.read(HERE / "bot_config.ini", encoding="utf-8")
USERNAME = _cfg["bot"]["username"].strip()
PASSWORD = _cfg["bot"]["password"].strip()
BASE_URL = _cfg["bot"].get("base_url", "https://www.198198.io").strip().rstrip("/")
FEED_URL = _cfg["bot"].get("feed_url", "ws://pp.knewb2u.com:8888/ws").strip()


def _load_report_cfg():
    """
    上报配置（client_id / report_url / report_token / sheet_url）。
    ★ exe 版优先读「exe 旁边那份 bot_config.ini」——每台 VPS 才能各改各的；
      读不到才退回打包进 exe 的那份。
    返回 (client_id, report_url, report_token, sheet_url, 来源说明)
    """
    cands = []
    if _FROZEN:
        cands.append(Path(sys.executable).resolve().parent / "bot_config.ini")
    cands.append(HERE / "bot_config.ini")
    for p in cands:
        try:
            c = configparser.ConfigParser()
            if not c.read(p, encoding="utf-8") or "bot" not in c:
                continue
            b = c["bot"]
            return (b.get("client_id", "").strip(),
                    b.get("report_url", "").strip(),
                    b.get("report_token", "").strip(),
                    b.get("sheet_url", "").strip(),
                    str(p))
        except Exception:
            continue
    return (_cfg["bot"].get("client_id", "").strip() if "bot" in _cfg else "",
            _cfg["bot"].get("report_url", "").strip() if "bot" in _cfg else "",
            _cfg["bot"].get("report_token", "").strip() if "bot" in _cfg else "",
            _cfg["bot"].get("sheet_url", "").strip() if "bot" in _cfg else "",
            "bundled")


def _machine_fingerprint() -> str:
    """
    自动机器指纹 —— 主机名 + 网卡 MAC 的哈希。

    作用：不用手填 client_id，程序也认得出「这是哪一台」。
    · 同一台机器每次开机算出来都一样（换目录 / 重启 / 重打包都不变）→ 服务端永远认它是同一台。
    · 换网卡 / 重装系统 → 指纹会变，服务端会当成新机器发新号（旧号那行就变「掉线」）。
    """
    try:
        raw = f"{socket.gethostname()}|{uuid.getnode():012x}"
    except Exception:
        raw = socket.gethostname() or "unknown-host"
    return "M-" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:10].upper()


CLIENT_ID, REPORT_URL, REPORT_TOKEN, SHEET_URL, REPORT_CFG_SRC = _load_report_cfg()
# 自动机器指纹：上报时一起发出去，服务端靠它认出「同一台」并自动发号 VPS-1 / VPS-2 …
# （手填了 client_id 的话以填的为准 → 保留旧行为，可随时回滚）
MACHINE_ID = _machine_fingerprint()

POOL_EMPTY_WAIT = 5      # 候选池为空时等待秒数后重掷
HEARTBEAT_INTERVAL = 10  # 心跳间隔秒数
RECONNECT_WAIT = 2       # 断开后重掷前等待秒数
BET_DELAY_MIN = 3        # 开局后随机延时下限（秒）再提交主注
BET_DELAY_MAX = 5        # 开局后随机延时上限（秒）
FALLBACK_BET = 10        # 查不到该桌最低限额时的兜底注额

ACTION_DELAY_MIN = 3     # 轮到我后随机延时下限（秒）再提交动作
ACTION_DELAY_MAX = 5     # 轮到我后随机延时上限（秒）
DECISION_WINDOW = 12     # 回合窗口保护：从收到 decisionOffer 起超过这个秒数就不再提交
USE_CASHOUT = True       # 允许「提前兑现」参与自动决策（原样交 PP EV.py 按它的公式排；False = 不兑现）

# ── 多座位：每局按「上一局结算完的 PDEV」决定坐几个位 ──
#     PDEV > 0  → 最多坐 MAX_SEATS_PER_TABLE 个位（空位不足就坐几个算几个）
#     PDEV <= 0 → 只坐 1 个位
#     每个位各下同样金额的主注；1 表示关闭多座位（回到原来的单座位行为）
MAX_SEATS_PER_TABLE = 3
SIT_CONFIRM_TIMEOUT = 8  # sitdown 发出后多少秒还没收到 sit 确认就丢弃这次请求

# ── 座位模式（启动时在「粘贴 URL」后面问一次，记进 last_inputs.json）──
#     1 = 永久 1 个位：不管 PDEV 正负，永远只打一个位（回到原来的单座行为）
#     2 = 跟 PDEV    ：PDEV > 0 最多 MAX_SEATS_PER_TABLE 个位；<= 0 只坐 1 个位
SEAT_MODE_FIXED = 1
SEAT_MODE_PDEV = 2
SEAT_MODES = {1: "fixed 1 seat", 2: "follow PDEV"}
SEAT_MODE_DEFAULT = SEAT_MODE_PDEV
SEAT_MODE = {"v": SEAT_MODE_DEFAULT}   # 运行时由 ask_seat_mode() 覆盖（值 1 / 2）

# ── 测试用：临时固定桌，跳过掷骰选桌（留空字符串 = 恢复正常掷骰选桌） ──
# 目的：先验证下注提交格式是否被平台接受。桌名必须存在于 table_id.txt。
FIXED_TABLE = ""
# ─────────────────────────────────────────────────────────────

# ── 直连模式：粘贴 URL 里是哪个 tableId 就连那张桌，跳过掷骰选桌 ──
#     True  = 直连（选桌逻辑暂时搁置，代码仍保留在 pick_table 里）
#     False = 恢复掷骰选桌（原行为）
USE_PASTED_TABLE = True
# ─────────────────────────────────────────────────────────────

# ── 掷骰选桌的桌名范围：只在这些桌名开头的桌里挑（"" = 全部桌） ──
TABLE_PREFIX = ""
# ─────────────────────────────────────────────────────────────

# ── 无基线时连上即开算（直连模式常用：粘的桌号不在 table_id.txt 就没有 feed 快照）──
#     True  = 连上后第一局就接管，已出张数/TC 从 0 开始算；收到洗牌信号照样清零
#     False = 旧行为：没基线就一直不算，直到碰上一次洗牌才接管
TAKEOVER_WITHOUT_BASELINE = True
# ─────────────────────────────────────────────────────────────

# ── 断线（非主动断开）时是否停止重连 ──
#     True  = 断线即停止（不退出程序、窗口保留，面板显示 "STOPPED: disconnected"）
#     False = 不因断线就停：等 RECONNECT_WAIT 秒后自动重连同一张桌
#     ★ 2026-10-12 按新方案关掉（要恢复「断线即停」把它改回 True 即可）
STOP_ON_DISCONNECT = False
# ─────────────────────────────────────────────────────────────

# ── 连接结束后一律停止（不重连、不换座）──
#     旧方案：用来解决「突然换座又被踢」。现已由下面的「重连必须先拿 feed 基线」取代，
#     因此预设关闭。要回到「连上一旦结束就停」把这里改成 True 即可。
#     True  = 连上之后一旦结束就停
#     False = 允许自动重连（重连的门槛见 REQUIRE_FEED_BASELINE_ON_RECONNECT）
STOP_ON_ANY_EXIT = False
# ─────────────────────────────────────────────────────────────

# ── ★ 重连必须先拿到 feed 基线（该桌已出牌清单 + TC），拿不到就直接停止 ──
#     只作用于「同一张桌断开后的自动重连」：
#       1) 先在网络端(feed)等这张桌推来一份新鲜快照（含 cards 全量）；
#       2) 拿到 → 用它当基线，再建立游戏连接（先拿基线再坐下，避免坐下不下注被 timeout 踢）；
#       3) REQUIRE_BASELINE_WAIT 秒内拿不到 → 停止（不退出、窗口保留、面板显示原因）。
#     首次连接（刚粘贴 URL 那一趟）不受影响：没有基线仍按 TAKEOVER_WITHOUT_BASELINE 从 0 起算。
#     True  = 重连必须先有基线（推荐）
#     False = 旧行为：重连不等基线，没基线就从 0 起算
REQUIRE_FEED_BASELINE_ON_RECONNECT = True
REQUIRE_BASELINE_WAIT = 10       # 重连等基线的上限（秒）
# ─────────────────────────────────────────────────────────────

# ── ★ 重连机制（粘贴 URL 后可选；这里只是「第一次问」的默认值）──
#     1 = strict      : 断线重连必须先等 feed 基线（最多 REQUIRE_BASELINE_WAIT 秒），
#                       拿不到就停（窗口保留）。＝ 旧行为，改回 1 即完全回滚。
#     2 = carry-over  : 断线重连不等 feed 基线、也不清零，直接沿用断开前那一刻的
#                       鞋级账本（已出牌清单 / TC / 座位号）接着下注。
#                       只搬「鞋」不搬「局」：局内手牌、庄家明牌、待确认注都从新帧重建。
#     选过一次就记在 last_inputs.json，下次回车 = 上次选的那个。
RECONNECT_MODE_DEFAULT = 2
RECONNECT_MODES = {1: "strict", 2: "carry-over"}
# ─────────────────────────────────────────────────────────────

# ── TC 数据源「网络端」(relay feed, pp.knewb2u.com) 开关 ──
#     True  = 正常行为：连 feed 拿「这张桌已出牌」的现成快照当 TC 基线
#     False = 完全停用网络端：不做任何 feed 连接；连上桌后直接无基线接管（TC 从 0 起算）
#     ★ 改回 True 即恢复原行为，其余代码无需改动
USE_FEED = True
# ─────────────────────────────────────────────────────────────

# ── 余额上下限自动停止 ──
#     True  = 粘贴 URL 后多问两个数字（上限 / 下限）；留空 = 该条不启用
#             余额 ≥ 上限  或  余额 ≤ 下限  → 只做标记，【打完当前这一局】才停
#             （不退出程序、窗口保留，面板显示 "STOPPED — balance …"）
#     False = 完全停用（连提示都不问），行为与改动前完全一致
USE_BALANCE_LIMIT = True
# ─────────────────────────────────────────────────────────────

# ── 面板重绘方式（解决 Windows 控制台「一直闪屏」）──
#     旧行为：每帧重画前先 \033[H\033[J 清屏 → 整屏先变空白再重画，眼睛看到就是闪。
#     新行为：绝不清屏，只把光标归位后【原地覆盖】每一行（行尾擦除）→ 没有空白瞬间，不闪。
#     另外：面板约 31 行，若控制台窗口比它矮，每帧会滚动、面板还会错位 →
#           启动时若不够高会尝试把窗口调高到刚好装下（只调高、不会调矮；失败则退回旧行为）。
#     True  = 平滑重绘（推荐）
#     False = 旧行为（清屏重画）
UI_SMOOTH_RENDER = True
PANEL_MIN_LINES = 36     # 面板行数 + 余量；窗口低于这个值就尝试调高
EVENTS_ROWS = 8          # 「Recent events」固定占几行（不足补空行 → 帧高恒定，避免残影）
# ─────────────────────────────────────────────────────────────

# ── 余额上报（多客户端汇总看板）──
#     程序在多台 VPS 上跑，老板要随时看到每台的余额 → 客户端定期 POST 到中心看板。
#     配置在 bot_config.ini：report_url / report_token（缺任一项 = 等同关闭 —— 这两项已打包进 exe）
#     client_id 可不填：程序会自动带一个机器指纹，服务端按指纹自动发号（VPS-1 / VPS-2 …）
#                 —— 填了就以填的为准（旧行为，可回滚）；每台 VPS 因此什么都不用配
#     上报时机：余额一变就报；没变则每 REPORT_HEARTBEAT 秒心跳一次
#              （心跳是必需的 —— 否则老板端分不清「余额没动」还是「这台掉线了」）
#     上报走后台线程、超时 REPORT_TIMEOUT 秒即放弃、不重试 → 绝不拖慢下注
#     True  = 启用（未填配置则自动不启用）
#     False = 完全关闭：不产生任何上报请求（回到旧行为）
REPORT_ENABLED = True
REPORT_HEARTBEAT = 30    # 余额没变时的心跳间隔（秒）
REPORT_TIMEOUT = 3       # 单次上报超时（秒）
# ─────────────────────────────────────────────────────────────

# ── 余额也推一份到 Google 表格（照 EVO / EGBJ 的做法）──
#     EVO 那边是每台机自己把 {machine_id, balance} POST 到那张表的 doPost，
#     由 doPost 按名字找行、更新/追加 → 写进 live 页。这里做同一件事：
#     同地址、同格式，PP 的余额就跟 EVO 汇进同一张表（live 页）。
#     · 缺 sheet_url = 自动停用（不产生任何请求，等同关闭）
#     · 只在「余额变化」时推（跟 EVO 一致，没变不推 → 尽量省 Google 配额）
#     · 后台线程、超时 SHEET_TIMEOUT 秒、静默失败、不重试 → 绝不拖慢下注
SHEET_TIMEOUT = 5        # 单次推送超时（秒）—— 跟 EVO 的 send_balance_async 一致
SHEET_ID_PREFIX = ""     # 名字 = 纯 IP（与 EVO 完全一致）
                         # 旧值 "PP-"（加前缀免得同机跑 EVO 撞名）已停用，保留说明方便回滚
# ─────────────────────────────────────────────────────────────

tc_map = {}    # 桌名 → 最新 TC（主程序推的）
snap_map = {}  # 桌名 → 最新完整快照 payload（含 cards 全量 / tc / timestamp）
BLACKLIST = set()  # 下注被拒的桌名（进程内有效，重启即清空，不再重复挑）

# TC 数据源开关：开局把快照搬进本地账本后即暂停收 feed（之后全靠本地自算）；
# 需要新基线时（每轮重连桌之前）自动接回
feed_ctl = {"want": USE_FEED}

# 桌台连接计时：连上（subscribe 确认）开始计，断开暂停/停止
conn_clock = {"since": None, "elapsed": 0.0, "running": False}

# 余额上下限：upper / lower 为 None = 该条不启用
balance_limit = {"upper": None, "lower": None}
# 命中上下限后【只做标记】，不立刻停；等下一局要下注时才真正停（保证当前这局打完）
stop_flag = {"reason": ""}


# ── 平滑重绘辅助（Windows 控制台防闪）─────────────────────────
_frame = {"painted": False}      # 是否已画过至少一帧（首帧要清一次屏 + 藏光标）


def _console_size():
    """返回 (列, 行)；拿不到返回 (0, 0)"""
    try:
        s = os.get_terminal_size()
        return s.columns, s.lines
    except Exception:
        return 0, 0


def _resize_console(cols: int, lines: int):
    """Windows：把控制台窗口/缓冲区调到能装下整块面板（任何失败都静默忽略）"""
    if os.name != "nt":
        return
    try:
        import ctypes

        class _COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class _RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)                     # STD_OUTPUT_HANDLE
        k.SetConsoleScreenBufferSize(h, _COORD(cols, 300))   # 先放大缓冲（不能小于窗口）
        k.SetConsoleWindowInfo(h, True,
                               ctypes.byref(_RECT(0, 0, cols - 1, lines - 1)))
        k.SetConsoleScreenBufferSize(h, _COORD(cols, lines))
    except Exception:
        pass


def _ensure_console_height():
    """窗口比面板矮 → 尝试调高（只调高不调矮，失败就算了 → 退回清屏模式）"""
    cols, rows = _console_size()
    if rows and rows >= PANEL_MIN_LINES:
        return
    _resize_console(max(cols, 90), PANEL_MIN_LINES)


def _show_cursor(show: bool):
    try:
        print("\033[?25h" if show else "\033[?25l", end="", flush=True)
    except Exception:
        pass


def conn_tick():
    """把连接时长搬进 UI（板面上显示的秒数）"""
    UI["conn_secs"] = conn_clock["elapsed"]


def conn_start():
    conn_clock["since"] = time.monotonic()
    conn_clock["elapsed"] = 0.0
    conn_clock["running"] = True
    conn_tick()


def conn_stop():
    """停止计时并把最终时长定格（断线/停止时用）"""
    if conn_clock["running"] and conn_clock["since"] is not None:
        conn_clock["elapsed"] += time.monotonic() - conn_clock["since"]
    conn_clock["since"] = None
    conn_clock["running"] = False
    conn_tick()


def conn_reset():
    conn_clock["since"] = None
    conn_clock["elapsed"] = 0.0
    conn_clock["running"] = False
    conn_tick()


# ═══════════════ 固定板面 UI（不滚动，只刷新变化值）═══════════════
from collections import deque

SHOE_TOTAL = 416          # 8 副牌
UI_W = 78                 # 板面宽度
UI = {
    "active": False,            # 进入主循环后才整屏渲染（之前正常逐行打印）
    "table": "—", "min_bet": None, "my_seat": None,
    "feed": "connecting…" if USE_FEED else "off (feed disabled)", "login": "",
    "reconnect": "",            # 本次运行选的重连机制（strict / carry-over）
    "seat_mode": "",            # 本次运行选的座位模式（fixed 1 seat / follow PDEV）
    "balance": None,            # 余额（HTTP 轮询）
    "currency": "MYR",
    "conn_secs": 0.0,           # 这张桌的已连接时长（秒）
    "stopped": "",              # 非空 = 已停止（显示 STOPPED 原因）
    "shoe_n": 0, "tc": 0.0, "taken": False,
    "pdev": None, "pdev_issued": None,   # PDEV（含 ECU）；None = 还没算出来
    "round": "—", "dealer": "—",
    "occupy": {},               # 座位 → 玩家名（连接期间）
    "scores": {},               # 本局: 座位 → 点数（暂不上屏，留着方便恢复）
    "me_hands": ["", ""],       # 我这两手牌的文字（板面 hand1 / hand2）
    "seat_hands": {},           # 多座位时：座位号 → 该座位本局手牌文字（每座位一行）
    "ev": "", "action": "",
    "events": deque(maxlen=10),
}

# ── carry-over 用的「鞋级账本」：断线那一刻的最后状态（只在进程内有效）──
#     report_tc() 每次牌变都会同步一份，所以它永远是「最后一次已知」的账本。
#     程序重启 / 换新 URL → CARRY 清空 → 回到从 0 起算（与旧行为一致）。
CARRY = {
    "table": None,      # 桌名（只接同一张桌）
    "cards": None,      # 整个 shoe 已出牌（字符串列表；None = 还没接管过）
    "tc": 0.0,
    "seat": None,       # 上次坐的座位号（str）；被占就随便找空位
    "ts": None,         # 记录时间（面板/日志用）
}


def _dwidth(s: str) -> int:
    """显示宽度：东亚宽字符（CJK、emoji）算 2，其余算 1 —— 与 macOS 终端一致"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _clip(s: str, w: int) -> str:
    s, out, width = str(s), "", 0
    for ch in s:
        cw = _dwidth(ch)
        if width + cw > w - 1:
            return out + "…"
        out += ch
        width += cw
    return out


def _pad(s: str, w: int) -> str:
    return s + " " * max(0, w - _dwidth(s))


def _hms(secs: float) -> str:
    """秒 → HH:MM:SS"""
    s = int(secs)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _tc_color(v: float) -> str:
    return Fore.GREEN if v >= 1 else (Fore.CYAN if v >= 0 else Fore.RED)


PDEV_BASELINE = -0.006476944408866543   # 满靴（0 张已出）的 PDEV —— 用来判断「比新靴好还是差」


def pdev_pct(v) -> str:
    """PDEV 小数 → 百分比文本（−0.005331 → −0.5331%），保留符号"""
    return f"{v * 100:+.4f}%"


def _pdev_txt() -> str:
    v = UI.get("pdev")
    if v is None:
        return "—"
    return f"{pdev_pct(v)} [{UI.get('pdev_issued')}]"


def _pdev_color() -> str:
    v = UI.get("pdev")
    if v is None:
        return Fore.LIGHTBLACK_EX
    if v > 0:
        return Fore.GREEN                      # 正期望：这一靴对玩家有利
    return Fore.CYAN if v >= PDEV_BASELINE else Fore.RED


def update_pdev(cards):
    """按整靴已出牌算 PDEV（含 ECU）。
    引擎自带 128 份余牌组成缓存 —— 同一状态重复算几乎零成本（实测 ~9ms/次）。
    出任何问题都只让板面显示「—」，绝不打断主流程。"""
    try:
        r = compute_pdev(cards)
        UI["pdev"] = r["pdev"]
        UI["pdev_issued"] = r["issued"]
    except Exception:
        UI["pdev"] = None
        UI["pdev_issued"] = None


def seat_disp(s) -> str:
    """座位号「显示用」换算：协议 0–6 → 显示 1–7。只影响屏幕输出，发给服务器的永远是 0–6。
    传入 list 时按多座位拼（如 "1+3+5"）。"""
    if isinstance(s, (list, tuple, set)):
        if not s:
            return "—"
        return "+".join(seat_disp(x) for x in sorted(s, key=lambda x: int(x)))
    try:
        n = int(s)
    except (TypeError, ValueError):
        return "—"
    return str(n + 1) if n >= 0 else "D"


def hand_disp(i) -> str:
    """手序号「显示用」换算：协议 0/1 → 显示 1/2。只影响屏幕输出。"""
    try:
        return str(int(i) + 1)
    except (TypeError, ValueError):
        return "—"


def render():
    """整屏重画固定板面（只在事件发生时调用，平时不滚动）"""
    if not UI["active"]:
        return
    W = UI_W
    bar = "─" * W

    def row(txt, color=""):
        t = _pad(_clip(txt, W - 2), W - 2)
        return "│ " + (color + t + Style.RESET_ALL if color else t) + " │"



    taken_txt = ("taken · local TC" if UI["taken"]
                 else ("carry-over · pending" if UI["shoe_n"] else "waiting for snapshot"))

    seats_now = UI["my_seat"] if isinstance(UI["my_seat"], list) else []

    def me_rows():
        """「Me」区块：单座位 = hand1/hand2（原样）；多座位 = 每个座位一行，最多 MAX_SEATS_PER_TABLE 行"""
        out = [row("Me")]
        if len(seats_now) > 1:
            order = sorted(seats_now, key=lambda x: int(x))
            for s in order:
                out.append(row(f"{'S' + seat_disp(s):<9} : "
                               f"{UI['seat_hands'].get(s, '')}".rstrip()))
            for _ in range(MAX_SEATS_PER_TABLE - len(order)):
                out.append(row(f"{'':<9}   ".rstrip()))      # 补齐 → 行数恒定，不跳动
            return out
        out.append(row(f"{'hand1':<9} : {UI['me_hands'][0]}".rstrip()))
        out.append(row(f"{'hand2':<9} : {UI['me_hands'][1]}".rstrip()))
        return out

    if UI.get("stopped"):
        L = ["┌" + bar + "┐",
             row("PP AutoBot"),
             row(f"{'Table':<9} : {UI['table']}"),
             row(f"{'Min bet':<9} : {UI['min_bet'] or '?'}"),
             row(f"{'My seat':<9} : {seat_disp(UI['my_seat'])}"
                 + (f"  ({UI['seat_mode']})" if UI['seat_mode'] else "")),
             row(f"{'Balance':<9} : {UI.get('currency') or 'MYR'} "
                 f"{UI['balance'] if UI['balance'] is not None else '—'}",
                 Fore.YELLOW),
             row(f"{'Connected':<9} : {_hms(UI['conn_secs'])}",
                 Fore.RED),
             row(f"{'State':<9} : STOPPED — {UI['stopped']}", Fore.RED),
             row(f"{'Feed':<9} : {UI['feed']}"),
             row(f"{'Reconnect':<9} : {UI['reconnect'] or '—'}"),
             row(f"{'Login':<9} : {UI['login']}"),
             "├" + bar + "┤",
             row(f"Shoe {UI['shoe_n']}/{SHOE_TOTAL}   {taken_txt}   TC {UI['tc']:+.2f}",
                 _tc_color(UI["tc"])),
             row(f"{'PDEV':<9} : {_pdev_txt()}", _pdev_color()),
             "├" + bar + "┤",
             row(f"{'GameID':<9} : {UI['round']}"),
             row(f"{'Dealer':<9} : {UI['dealer']}")] + me_rows()
    else:
        L = ["┌" + bar + "┐",
             row("PP AutoBot"),
             row(f"{'Table':<9} : {UI['table']}"),
             row(f"{'Min bet':<9} : {UI['min_bet'] or '?'}"),
             row(f"{'My seat':<9} : {seat_disp(UI['my_seat'])}"
                 + (f"  ({UI['seat_mode']})" if UI['seat_mode'] else "")),
             row(f"{'Balance':<9} : {UI.get('currency') or 'MYR'} "
                 f"{UI['balance'] if UI['balance'] is not None else '—'}",
                 Fore.YELLOW),
             row(f"{'Connected':<9} : {_hms(UI['conn_secs'])}", Fore.MAGENTA),
             row(f"{'State':<9} : running", Fore.GREEN),
             row(f"{'Feed':<9} : {UI['feed']}"),
             row(f"{'Reconnect':<9} : {UI['reconnect'] or '—'}"),
             row(f"{'Login':<9} : {UI['login']}"),
             "├" + bar + "┤",
             row(f"Shoe {UI['shoe_n']}/{SHOE_TOTAL}   {taken_txt}   TC {UI['tc']:+.2f}",
                 _tc_color(UI["tc"])),
             row(f"{'PDEV':<9} : {_pdev_txt()}", _pdev_color()),
             "├" + bar + "┤",
             row(f"{'GameID':<9} : {UI['round']}"),
             row(f"{'Dealer':<9} : {UI['dealer']}")] + me_rows()
    L.append(row(f"{'EV':<9} : {UI['ev']}".rstrip()))
    L.append(row(f"{'Action':<9} : {UI['action']}".rstrip()))
    L += ["└" + bar + "┘",
          f"{Fore.CYAN}{Style.DIM}  " + bar + f"{Style.RESET_ALL}",
          f"{Fore.CYAN}{Style.DIM}  Recent events (cleared each new round){Style.RESET_ALL}"]
    evs = list(UI["events"])[-EVENTS_ROWS:]
    L += [f"  {line}" for line in evs]
    L += ["  "] * (EVENTS_ROWS - len(evs))   # 补空行 → 帧高恒定（避免行数变化留残影）

    # ── 画到屏幕 ──
    # 平滑模式：光标归位后原地覆盖每行（行尾擦除），【绝不清屏】→ 不闪
    if UI_SMOOTH_RENDER:
        rows = _console_size()[1]
        if rows and rows < len(L) + 1:        # 窗口比面板矮 → 先试着调高
            _ensure_console_height()
            rows = _console_size()[1]
        if rows and rows >= len(L) + 1:
            if _frame["painted"]:
                head = "\033[H"                                  # 只归位
            else:
                head = "\033[2J\033[H"                           # 首帧清一次屏
                _frame["painted"] = True
                _show_cursor(False)                              # 藏光标（减少视觉跳动）
            print(head + "\n".join(x + "\033[K" for x in L), end="", flush=True)
            return
        _frame["painted"] = False             # 窗口装不下 → 退回清屏模式（至少能看全）

    print("\033[H\033[J", end="")          # 旧行为：光标回原点 + 清屏（固定板面不滚动）
    print("\n".join(L), flush=True)


def log(msg, color=""):
    if UI["active"]:
        UI["events"].append(f"{color}{msg}{Style.RESET_ALL}")
        render()
    else:
        print(f"{color}{msg}{Style.RESET_ALL}", flush=True)


def load_tables() -> dict:
    """读 table_id.txt：桌名 → 桌号"""
    tables = {}
    with open(ROOT / "table_id.txt", encoding="utf-8") as f:
        for line in f:
            if "|" in line:
                name, tid = [x.strip() for x in line.split("|", 1)]
                tables[name] = tid
    return tables


# ═══════════════════════ ⓪ 启动时读入登入 URL ═══════════════════════
def ask_pasted_jsid():
    """读入完整 wss URL，取出 JSESSIONID 与 tableId（%21 会还原成 !）

    直连模式下 tableId 就是「要连的那一张桌」，所以必须能从 URL 里解析出来。
    返回 (JSESSIONID, tableId)。
    """
    print("  Paste the full wss URL:")
    while True:
        try:
            u = input("  URL: ").strip()
        except EOFError:
            raise RuntimeError("no URL entered")
        if not u:
            print("  Empty input — paste again:")
            continue
        if "JSESSIONID=" not in u:
            print("  No JSESSIONID= in that URL — check and paste again:")
            continue
        q = parse_qs(urlparse(u).query)
        sid = q.get("JSESSIONID", [""])[0]
        if not sid:
            print("  Could not parse JSESSIONID from that URL — paste again:")
            continue
        tid = q.get("tableId", [""])[0]
        if USE_PASTED_TABLE and not tid:
            print("  No tableId= in that URL (direct mode needs it) — paste again:")
            continue
        log(f"  [✅] JSESSIONID: {sid}", Fore.GREEN)
        if USE_PASTED_TABLE:
            log(f"  [🎯] tableId from URL: {tid}", Fore.GREEN)
        return sid, tid


# ── 上次输入记忆（Bet / Upper / Lower）───────────────────────────────
# 存在 exe（或 main.py）同目录的 last_inputs.json；读不到就按「首次」走，绝不报错。
# 规则：直接回车 = 沿用上次输入的那一项（没有上次 = 该项不带值 / 不设限）
#       Bet 额外：输入 0 = 用该桌最低限额（auto）
_LAST_INPUTS = {"bet": None, "upper": None, "lower": None, "mode": None,
                "seat_mode": None, "loaded": False}


def _last_inputs_path() -> Path:
    base = Path(sys.executable).resolve().parent if _FROZEN else HERE
    return base / "last_inputs.json"


def _load_last_inputs() -> dict:
    """只读一次；文件坏了 / 不存在 / 没权限 → 全部当 None（= 首次运行）"""
    if _LAST_INPUTS["loaded"]:
        return _LAST_INPUTS
    _LAST_INPUTS["loaded"] = True
    try:
        with open(_last_inputs_path(), encoding="utf-8") as f:
            d = json.load(f) or {}
        for k in ("bet", "upper", "lower", "mode", "seat_mode"):
            v = d.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if k == "mode":
                if int(v) in RECONNECT_MODES:      # 只认 1 / 2，别的当没选过
                    _LAST_INPUTS[k] = int(v)
                continue
            if k == "seat_mode":
                if int(v) in SEAT_MODES:           # 只认 1 / 2，别的当没选过
                    _LAST_INPUTS[k] = int(v)
                continue
            _LAST_INPUTS[k] = float(v)
    except Exception:
        pass
    return _LAST_INPUTS


def _save_last_inputs(**kv):
    """静默保存：写不进去（只读目录等）就当没有记忆，绝不影响主链路"""
    _load_last_inputs()
    _LAST_INPUTS.update(kv)
    try:
        out = {k: _LAST_INPUTS[k] for k in ("bet", "upper", "lower", "mode", "seat_mode")}
        for k in ("mode", "seat_mode"):
            if out.get(k) is not None:
                out[k] = int(out[k])               # 存成整数，读回来好校验
        with open(_last_inputs_path(), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass


def _fmt_last(v) -> str:
    """提示行里的上次值：数字 → 1,000；None → none"""
    return f"{v:,.0f}" if v is not None else "none"


def ask_bet_amount() -> int:
    """读到合法整数注额为止。
    回车 = 沿用上次注额（没有上次 → 该桌最低限额）；输入 0 = 该桌最低限额（自动）。
    """
    last = _load_last_inputs()["bet"]
    hint = _fmt_last(last) if last else "auto"
    print(f"  Enter bet amount  (Enter = last: {hint} , 0 = table minimum):")
    while True:
        try:
            raw = input(f"  Bet [{hint}]: ").strip()
        except EOFError:
            raise RuntimeError("no bet amount entered")
        if not raw:
            if last:
                log(f"  [💰] Bet amount: {int(last)} (last used)", Fore.CYAN)
                return int(last)
            log("  [💰] Bet amount: auto (table minimum)", Fore.CYAN)
            _save_last_inputs(bet=None)
            return None
        try:
            amt = int(float(raw))
        except ValueError:
            print("  Not a number — enter an integer (e.g. 10):")
            continue
        if amt < 0:
            print("  Must be 0 or more (0 = table minimum) — enter again:")
            continue
        if amt == 0:
            log("  [💰] Bet amount: auto (table minimum)", Fore.CYAN)
            _save_last_inputs(bet=None)
            return None
        log(f"  [💰] Bet amount: {amt} (fixed for every round)", Fore.GREEN)
        _save_last_inputs(bet=amt)
        return amt


def ask_seat_mode() -> int:
    """座位模式：1 = 永久只打 1 个位；2 = 跟 PDEV 走（>0 最多 MAX_SEATS_PER_TABLE 位，<=0 1 位）。

    回车 = 上次选过的（没有上次 = SEAT_MODE_DEFAULT，当前是 2）。
    """
    last = _load_last_inputs().get("seat_mode") or SEAT_MODE_DEFAULT
    hint = f"{last} = {SEAT_MODES[last]}"
    print("  Seat mode  (1 = always 1 seat , 2 = follow PDEV):")
    while True:
        try:
            raw = input(f"  Seats [{hint}]: ").strip()
        except EOFError:
            raise RuntimeError("no seat mode entered")
        if not raw:
            mode = last
        elif raw in ("1", "2"):
            mode = int(raw)
        else:
            print("  Only 1 or 2 — enter again:")
            continue
        break
    if mode == SEAT_MODE_FIXED:
        log("  [🪑] Seat mode: fixed — 永远只打 1 个座位（不看 PDEV）", Fore.GREEN)
    else:
        log(f"  [🪑] Seat mode: follow PDEV — PDEV > 0 最多 {MAX_SEATS_PER_TABLE} 个位，"
            f"PDEV <= 0 只打 1 个", Fore.GREEN)
    _save_last_inputs(seat_mode=mode)
    return mode


def _parse_money(raw):
    """'2,500' / '2500.5' → float；空串 → None；非法 → 'BAD'"""
    s = (raw or "").strip().replace(",", "").replace("，", "").replace(" ", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return "BAD"


def ask_balance_limits():
    """
    余额上限 / 下限（各一条）。
    直接回车 = 沿用上次输入的值（从没输过 → 该项不启用 = 不设限）。
    返回 (upper, lower)；两者可为 None。
    规则：上限必须大于下限（相等或反了 → 重问下限）。
    """
    last = _load_last_inputs()
    last_up, last_lo = last["upper"], last["lower"]

    print("  Enter balance UPPER limit — stop when balance >= it")
    upper = None
    while True:
        try:
            raw = input(f"  Upper [{_fmt_last(last_up)}]: ").strip()
        except EOFError:
            raise RuntimeError("no balance upper limit entered")
        if not raw:
            upper = last_up                    # 回车 → 沿用上次（没有上次 = 不设限）
            break
        v = _parse_money(raw)
        if v == "BAD":
            print("  Not a number — enter a number (e.g. 3000):")
            continue
        upper = v
        break
    if upper is None:
        log("  [🎚️] Balance upper limit: disabled", Fore.CYAN)
    else:
        log(f"  [🎚️] Balance upper limit: {upper:,.2f}  (stop when balance >= it)", Fore.GREEN)

    print("  Enter balance LOWER limit — stop when balance <= it")
    lower = None
    first = True
    while True:
        dflt = last_lo if first else None      # 只第一次给上次值，重问时不再默认
        try:
            raw = input(f"  Lower [{_fmt_last(dflt)}]: ").strip()
        except EOFError:
            raise RuntimeError("no balance lower limit entered")
        first = False
        lower = dflt if not raw else _parse_money(raw)
        if lower == "BAD":
            print("  Not a number — enter a number (e.g. 500):")
            continue
        if lower is not None and upper is not None and lower >= upper:
            print(f"  Must be smaller than the upper limit ({upper:,.0f}) — enter again:")
            continue
        break
    if lower is None:
        log("  [🎚️] Balance lower limit: disabled", Fore.CYAN)
    else:
        log(f"  [🎚️] Balance lower limit: {lower:,.2f}  (stop when balance <= it)", Fore.GREEN)

    if upper is None and lower is None:
        log("  [🎚️] Balance limits: OFF (both disabled) — 不会因余额停止", Fore.CYAN)
    _save_last_inputs(upper=upper, lower=lower)
    return upper, lower


def ask_reconnect_mode() -> int:
    """断线后怎么重连：1 = 先等 feed 基线（旧行为）；2 = 沿用上次鞋级账本直接跑。

    回车 = 上次选过的（没有上次 = RECONNECT_MODE_DEFAULT，当前是 2）。
    """
    last = _load_last_inputs().get("mode") or RECONNECT_MODE_DEFAULT
    hint = f"{last} = {RECONNECT_MODES[last]}"
    print("  Reconnect mode  (1 = strict baseline , 2 = carry over last shoe):")
    while True:
        try:
            raw = input(f"  Reconnect [{hint}]: ").strip()
        except EOFError:
            raise RuntimeError("no reconnect mode entered")
        if not raw:
            mode = last
        elif raw in ("1", "2"):
            mode = int(raw)
        else:
            print("  Only 1 or 2 — enter again:")
            continue
        break
    if mode == 2:
        log("  [♻️] Reconnect mode: carry-over — 断线后直接用上次的鞋账本重连 "
            "(不等 feed 基线)", Fore.GREEN)
    else:
        log(f"  [📡] Reconnect mode: strict — 断线重连必须先拿到 feed 基线 "
            f"(最多 {int(REQUIRE_BASELINE_WAIT)}s，拿不到就停)", Fore.GREEN)
    _save_last_inputs(mode=mode)
    return mode
    return upper, lower


# ═══════════════════════ ⓪b 余额（HTTP 轮询，非 wss） ═══════════════════════
# 余额不在任何 wss 帧里（已实测 game / dga-lc / ws1 三条都没有），走 HTTP：
#   GET https://games.pplc001.net/api/wallet/balance?JSESSIONID=<sid>
#   响应: {"balance":"2494.25","bonusBalance":"0.00","currency":"MYR", ...}
BALANCE_URL = "https://games.pplc001.net/api/wallet/balance"
BALANCE_INTERVAL = 3      # 轮询间隔（秒）


def fetch_balance(sid: str):
    """返回 (balance 显示串, currency, 数值)，失败返回 None"""
    try:
        r = requests.get(f"{BALANCE_URL}?JSESSIONID={sid}", timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                "AppleWebKit/537.36",
                                  "Accept": "application/json, text/plain, */*"})
        r.raise_for_status()
        j = r.json() or {}
        bal = j.get("balance")
        cur = j.get("currency") or "MYR"
        if bal is None:
            return None
        return (f"{float(bal):,.2f}", cur, float(bal))
    except Exception:
        return None


def _check_balance_limit(num: float):
    """
    余额命中上限 / 下限 → 【只做标记】，绝不立即停止。
    真正停止发生在下一局 betsopen（要下注那一刻）—— 保证当前这一局先打完。
    """
    if stop_flag["reason"]:
        return                       # 已经标记过，不重复
    if not USE_BALANCE_LIMIT:
        return
    up, lo = balance_limit["upper"], balance_limit["lower"]
    if up is None and lo is None:
        return
    if up is not None and num >= up:
        stop_flag["reason"] = f"balance {num:,.2f} >= upper {up:,.2f}"
    elif lo is not None and num <= lo:
        stop_flag["reason"] = f"balance {num:,.2f} <= lower {lo:,.2f}"
    else:
        return
    UI["action"] = f"LIMIT HIT — {stop_flag['reason']} (stop after this round)"
    log(f"  [🛑] {stop_flag['reason']} — 已标记：本局打完就停，"
        f"下一局不下注（不立即断开）", Fore.RED)
    render()


# ═══════════════════════ ⓪c 余额上报（多客户端汇总看板）═══════════════════════
# 复用的就是上面这条余额轮询的结果（本来每 3 秒就有一次），不新增任何轮询。
_report_ctl = {"last_bal": None, "last_ts": 0.0}


def _report_ready() -> bool:
    # 不再要求 client_id：没手填时由服务端按机器指纹自动发号（VPS-1 / VPS-2 …）
    return bool(REPORT_ENABLED and REPORT_URL and REPORT_TOKEN)


def _report_payload(status: str, reason: str = "", bal=None) -> dict:
    return {"token": REPORT_TOKEN, "client_id": CLIENT_ID, "machine_id": MACHINE_ID,
            "balance": bal,
            "currency": UI.get("currency") or "", "table": UI.get("table") or "",
            "status": status, "reason": reason, "ts": time.time()}


def _post_report(payload: dict):
    """后台线程发送：短超时、静默失败、绝不重试（下注主链路一秒都不能被它拖住）"""
    def _run():
        try:
            requests.post(REPORT_URL, json=payload, timeout=REPORT_TIMEOUT)
        except Exception:
            pass
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


def maybe_report_balance(num):
    """
    余额一变就报；没变则每 REPORT_HEARTBEAT 秒心跳一次。
    心跳带的是【当前状态】：程序停止后窗口保留、余额轮询仍在跑，
    心跳必须继续报 stopped，否则会把看板上的「已停止」又刷回「运行中」。
    """
    if not _report_ready() or num is None:
        return
    now = time.time()
    if num == _report_ctl["last_bal"] and (now - _report_ctl["last_ts"] < REPORT_HEARTBEAT):
        return
    _report_ctl["last_bal"] = num
    _report_ctl["last_ts"] = now
    stopped = UI.get("stopped") or ""
    _post_report(_report_payload("stopped" if stopped else "running",
                                 reason=stopped, bal=num))


def report_status(status: str, reason: str = ""):
    """状态变化（停止 / 退出）时补报一次 —— 同步发送，让老板端立刻看到"""
    if not _report_ready():
        return
    try:
        requests.post(REPORT_URL,
                      json=_report_payload(status, reason, _report_ctl["last_bal"]),
                      timeout=REPORT_TIMEOUT)
    except Exception:
        pass


# ─────────────────────── ⓪d 余额推送到 Google 表格（照 EVO 那套）───────────────────────
# EVO 的 EGBJ/BJ.py 里：
#   · get_machine_id()      → 取本机出口 IP（UDP 连 8.8.8.8）当机器名
#   · send_balance_async()  → 余额一变就把 {machine_id, balance} POST 到那张表的 doPost
# 这里完全照做，只是把目的地写在 bot_config.ini 的 sheet_url（缺 = 不启用）。
_sheet_ctl = {"last_bal": object()}   # 用 object() 占位，保证第一次一定会推


def _outbound_ip() -> str:
    """出口 IP —— 跟 EVO 的 get_machine_id() 同一套做法（UDP 连 8.8.8.8 看走哪张网卡）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(2)
            s.connect(("8.8.8.8", 53))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return MACHINE_ID          # 取不到就退回机器指纹（一定不为空）


_sheet_mid_cache = None


def _sheet_machine_id() -> str:
    global _sheet_mid_cache
    if _sheet_mid_cache is None:
        _sheet_mid_cache = SHEET_ID_PREFIX + _outbound_ip()
    return _sheet_mid_cache


def _post_sheet(bal):
    """后台线程推送：短超时、静默失败、绝不重试（与 EVO 的 send_balance_async 行为一致）"""
    payload = {"machine_id": _sheet_machine_id(), "balance": float(bal)}

    def _run():
        try:
            requests.post(SHEET_URL, json=payload, timeout=SHEET_TIMEOUT)
        except Exception:
            pass
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


def maybe_push_sheet(num):
    """余额一变就推（跟 EVO 一样；没变就不推，省 Google 配额）"""
    if not SHEET_URL or num is None:
        return
    if num == _sheet_ctl["last_bal"]:
        return
    _sheet_ctl["last_bal"] = num
    _post_sheet(num)


async def balance_task(sid: str):
    """定时轮询余额 → 面板常驻显示 + 上下限检查；数值没变就不重画（避免无谓刷屏）"""
    while True:
        got = await asyncio.to_thread(fetch_balance, sid)
        if got:
            bal, cur, num = got
            if UI["balance"] != bal or UI.get("currency") != cur:
                UI["balance"] = bal
                UI["currency"] = cur
                render()
            _check_balance_limit(num)
            maybe_report_balance(num)          # 余额上报（后台线程，不阻塞）
            maybe_push_sheet(num)              # 余额推 Google 表格（照 EVO；后台线程，不阻塞）
        await asyncio.sleep(BALANCE_INTERVAL)


# ═══════════════════════ ① TC 数据源订阅 ═══════════════════════
async def feed_task():
    first, paused = True, False
    while True:
        if not feed_ctl["want"]:
            paused = True
            await asyncio.sleep(1)     # 已暂停：等主流程要新基线时把 want 翻回来
            continue
        if first or paused:
            first = paused = False
            UI["feed"] = "connected"
            log("  [📡] Feed connected (pp.knewb2u.com:8888)", Fore.GREEN)
        try:
            async with websockets.connect(FEED_URL) as ws:
                feed_ctl["ws"] = ws
                async for raw in ws:
                    try:
                        m = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if m.get("type") != "mqtt_msg":
                        continue
                    p = m.get("payload") or {}
                    topic = p.get("topic", "")
                    if topic.startswith("pp/table/"):
                        name = topic[len("pp/table/"):]
                        body = p.get("payload") or {}
                        tc_map[name] = body.get("tc", 0)
                        snap_map[name] = body   # 完整快照，接管 TC 自算时作基线
                feed_ctl["ws"] = None
        except Exception as e:
            feed_ctl["ws"] = None
            if feed_ctl["want"]:       # 非主动暂停才报警重连
                UI["feed"] = "feed down · retry in 3s"
                log(f"  [⚠️] Feed down: {e} — reconnecting in 3s", Fore.RED)
                await asyncio.sleep(3)


def feed_pause():
    """基线到手 → 立刻主动断开 feed（之后全靠本地自算）"""
    feed_ctl["want"] = False
    UI["feed"] = "off · local TC"
    ws = feed_ctl.get("ws")
    if ws is not None:
        asyncio.ensure_future(ws.close())


async def wait_for_baseline(name: str, timeout: float):
    """重连用：在 feed 侧等这张桌的新鲜快照。拿到返回该 payload，超时返回 None。

    注意：feed 是按【桌名】索引的（topic = pp/table/<桌名>）。
    直连模式下若粘贴的 tableId 不在 table_id.txt（此时 name == tableId），
    feed 里永远不会有这个 key → 等满 timeout 也拿不到 → 由调用方直接停止。
    """
    if not USE_FEED:
        return None                       # 网络端被停用 → 不可能有基线
    feed_ctl["want"] = True               # 若是暂停状态，把 feed 接回来开收
    log(f"  [⏳] Reconnect: waiting for feed baseline of '{name}' "
        f"(up to {int(timeout)}s)...", Fore.YELLOW)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = snap_map.get(name)
        if snap is not None:
            return snap
        await asyncio.sleep(0.2)
    return None


# ═══════════════════════ ③ 掷骰选桌 ═══════════════════════
def snap_free_seats(name: str):
    """按主程序快照估这桌还剩几个空位（只用于选桌前的粗筛）。

    来源是快照里的 player_names：有名字的座位算有人，没名字的算空。
    返回 0..7；None = 判断不了（没有快照，或快照里没带座位名单）。
    连桌后服务器给的座位表才是准的，这里只是尽量避开明显满桌的。
    """
    snap = snap_map.get(name)
    if not snap:
        return None
    names = snap.get("player_names")
    if not names:
        return None
    occ = [k for k, v in names.items() if str(v or "").strip()]
    return max(0, 7 - len(occ))


async def pick_table(tables: dict) -> str:
    # 测试模式：固定桌，跳过掷骰
    if FIXED_TABLE:
        if FIXED_TABLE not in tables:
            log(f"  [❌] Fixed table '{FIXED_TABLE}' is not in table_id.txt — "
                f"add a line 'name | tableId' first", Fore.RED)
            raise SystemExit(1)
        log(f"  [📌] TEST MODE: fixed table {FIXED_TABLE} (dice roll skipped)", Fore.CYAN)
        # 直接采用主程序推来的这张桌的快照（已出牌清单），到手才连桌；
        # 最多等 30 秒，等不到也照常连（第一局可能没有 TC 基线，不卡死程序）
        waited = 0
        while FIXED_TABLE not in snap_map and waited < 30:
            if waited == 0:
                log("  [⏳] Waiting for this table's snapshot from the main app...", Fore.YELLOW)
            await asyncio.sleep(2)
            waited += 2
        if FIXED_TABLE in snap_map:
            n = len(snap_map[FIXED_TABLE].get("cards") or [])
            log(f"  [📡] Snapshot received: {n} cards dealt "
                f"(stored only, loaded as baseline at round start)", Fore.GREEN)
        else:
            log("  [⚠️] Snapshot not received — connecting anyway (first round may lack a baseline)", Fore.YELLOW)
        return FIXED_TABLE

    while True:
        if not tc_map:
            log("  [⏳] No TC data yet — waiting...", Fore.YELLOW)
            await asyncio.sleep(3)
            continue

        roll = random.randint(1, 10)
        want_positive = roll >= 7
        label = "positive (>=0)" if want_positive else "negative (<0)"
        scope = f"{TABLE_PREFIX}*" if TABLE_PREFIX else "all tables"
        pool = [n for n, tc in tc_map.items()
                if n in tables and n not in BLACKLIST
                and n.startswith(TABLE_PREFIX)
                and (tc >= 0) == want_positive]

        # 再筛一层：只留有「空位」的桌（按主程序快照的座位名单粗估）
        frees = {n: snap_free_seats(n) for n in pool}
        ready = [n for n in pool if (frees[n] or 0) >= 1]
        if not ready:
            full = sum(1 for n in pool if frees[n] == 0)
            unknown = sum(1 for n in pool if frees[n] is None)
            log(f"  [🎲] Rolled {roll} -> want {label} table | scope {scope} | "
                f"no table with a free seat ({len(pool)} candidates: "
                f"{full} full, {unknown} unknown) — re-rolling in {POOL_EMPTY_WAIT}s", Fore.YELLOW)
            await asyncio.sleep(POOL_EMPTY_WAIT)
            continue

        name = random.choice(ready)
        log(f"  [🎲] Rolled {roll} -> want {label} table | scope {scope} | "
            f"{len(ready)}/{len(pool)} have a free seat | picked: {name} "
            f"(TC {tc_map[name]}, {frees[name]} free)", Fore.CYAN)
        return name


# ═══════════════════════ ④ 构建选中桌的 wss（顺带取该桌最低下注限额） ═══════════════════════
def build_ws_url(sid: str, table_id: str):
    """返回 (wss url, 该桌最低下注限额)；限额查不到时为 None"""
    url = (f"https://games.pragmaticplaylive.net/api/ui/tableConfig"
           f"?JSESSIONID={sid}&tableId={table_id}&operatorNotification=true")
    resp = requests.get(url, timeout=5, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    })
    resp.raise_for_status()
    params = resp.json().get("params") or {}
    ws_address = params.get("ws_address")
    if not ws_address:
        raise RuntimeError("no ws_address in tableConfig response")
    try:
        min_bet = int(float(params.get("table_bet_min_limit")))
    except (TypeError, ValueError):
        min_bet = None
    return (f"{ws_address}?JSESSIONID={sid}&tableId={table_id}&type=json&version=3", min_bet)


# ═══════════════════════ TC 自算（Hi-Lo，与主程序 PPBJ.py 完全同算法） ═══════════════════════
COUNT_VALUES = {"2": 1, "3": 1, "4": 1, "5": 1, "6": 1,
                "7": 0, "8": 0, "9": 0,
                "10": -1, "J": -1, "Q": -1, "K": -1, "A": -1}
CARD_MAP = {"0": "10", "1": "A"}   # scanCode 首字符 → 点数
SHOE_CARDS = 416                   # 8 副牌


def _hand_cards(hand: dict) -> list:
    """从 playerSeat 的一手牌里取出点数序列"""
    out = []
    for c in (hand.get("cards") or []):
        code = c.get("scanCode", "") if isinstance(c, dict) else ""
        if code:
            out.append(CARD_MAP.get(code[0], code[0]))
    return out


class ShoeCounter:
    """自己维护整个 shoe 的已出牌并算 TC（接管后不再依赖主程序推的 tc）"""

    def __init__(self):
        self.cards = []   # 整个 shoe 至今所有已出牌
        self.hands = {}   # "座位_手" → 该手牌已知的牌（用来取增量、避免重复扣）
        self.tc = 0.0

    def load_baseline(self, cards):
        """接管点：导入主程序的完整已出牌作为基线"""
        self.cards = list(cards or [])
        self.hands = {}
        self.recompute()

    def clear(self):
        """换鞋：整个 shoe 归零"""
        self.cards = []
        self.hands = {}
        self.tc = 0.0

    def new_round(self):
        """新一局：只清手牌记账，已出牌继续累积"""
        self.hands = {}

    def feed_player_seat(self, ps: dict) -> int:
        """把一条 playerSeat 里的新牌计入，返回新增张数"""
        seat = ps.get("seatNumber")
        seat_key = "D" if seat == -1 else str(seat)

        # 拆牌：h1 出现时，h0 若显示成两张相同的牌，截成一张（与主程序一致）
        if "h1" in ps:
            h0_key = f"{seat_key}_h0"
            known = self.hands.get(h0_key)
            if known and len(known) == 2 and known[0] == known[1]:
                self.hands[h0_key] = known[:1]

        added = 0
        for key, hand in ps.items():
            if not key.startswith("h") or not isinstance(hand, dict):
                continue
            cards = _hand_cards(hand)
            if not cards:
                continue
            hkey = f"{seat_key}_{key}"
            known = self.hands.get(hkey, [])
            if len(cards) > len(known):
                new = cards[len(known):]
                self.hands[hkey] = cards
                self.cards.extend(new)
                added += len(new)

        if added:
            self.recompute()
        return added

    def recompute(self):
        rc = sum(COUNT_VALUES.get(c, 0) for c in self.cards)
        decks_remaining = max(1, (SHOE_CARDS - len(self.cards)) / 52)
        self.tc = round(rc / decks_remaining, 2)


# ═══════════════════════ ⑤ 连接选中桌 · 打印全部内容 + 心跳 + 空位检测 + TC 自算 ═══════════════════════
# ── wss 的 TLS 证书校验：用 certifi 的证书库（requests 同款，PyInstaller 会一并打进 exe）──
# 原因：部分 Windows（尤其新装的 VPS）系统根证书库不全，而 PP 的 wss 服务器不回传完整证书链；
#       Python 不像浏览器会自动补拉缺失的中间证书（AIA），于是在那类机器上报
#       [VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate。
# requests 那条路（tableConfig）一直没事，就是因为它本来就用 certifi。实测 certifi 校验
# games.pragmaticplaylive.net / gs15.pplc001.net 均通过（Amazon 签发）。
import ssl as _ssl
import certifi as _certifi
_WSS_SSL = _ssl.create_default_context(cafile=_certifi.where())


async def listen_table(name: str, url: str, min_bet, bet_amount=None, carry_in=None):
    """carry_in = 上一趟断开时留下的鞋级账本（只给 carry-over 机制用）：
       {"cards": [...], "tc": x, "seat": "3"}
       有它 → 接管时直接拿它当基线，不再等 feed；没有 → 走原有逻辑。
    """
    ch_holder = {"ch": name}  # 默认用桌名；subscribe 确认到达后改用服务器给的 channel
    occupied = {}             # 座位号(str) → 玩家名；非 sit 事件视为起身让位
    reported = {"init": False}
    my_seat = {"v": []}       # 已确认坐下的座位号（list[str]，最多 MAX_SEATS_PER_TABLE 个）
    my_pending = {}           # 已发 sitdown、等服务器 sit 确认的座位号 → 发出时间（秒）
    leaving = set()           # 我们主动 unseat 的座位号（收到 stand 时不当作被踢）
    my_uid = {"v": None}      # 我们自己的 user_id（由坐下确认消息取得）
    bet = {"pending": {}, "last_gid": None, "task": None, "amount": None, "ok_gid": None,
           "sent_gid": None, "sent_seats": set(), "sent_seats_gid": None}
    #   待确认 gid（座位→gid）/ 本局基准 gid / 延时任务 / 本局主注额 / 最近确认过的 gid
    #   / 真正发出过注的 gid / 本局已发过注的座位 / 那份座位表属于哪一局

    dealer = {"cards": []}    # 庄家已知牌（h0，第一张即明牌），供 EV 使用
    acted = set()             # 已提交过的动作 (座位号, decisionOffer.id)，同一格只动作一次
    dec_tasks = {}            # (座位号, offer.id) → 延时动作任务
    last_ps = {}             # 座位号 → 本局该座位最新的 playerSeat 帧（多座位兜底重扫用）
    dec_lock = asyncio.Lock()  # 决策发送闸门：多座位时轮流提交，不并发
    seen_dec = set()          # 已打印过的动作回执（去重）
    ins = {"done": None}      # 本局保险是否已回应（按 gameId 记）

    def _mine(seat_no) -> bool:
        """这个座位号是不是我们的（支持多座位）"""
        return seat_no is not None and str(seat_no) in my_seat["v"]

    shoe = ShoeCounter()      # TC 自算（接管后不再用主程序推的 tc）
    taken = {"v": False}      # 是否已在 betsopen 接管
    voip = {"just": False, "detected": False, "after": False, "pending": False}

    async def conn_ticker():
        """每秒把连接时长刷上板面，到整秒才重画"""
        shown = -1
        while True:
            await asyncio.sleep(0.5)
            if not conn_clock["running"] or conn_clock["since"] is None:
                return
            conn_clock["elapsed"] = time.monotonic() - conn_clock["since"]
            secs = int(conn_clock["elapsed"])
            if secs != shown:
                shown = secs
                conn_tick()
                render()

    def report_empty(tag: str):
        UI["occupy"] = dict(occupied)
        render()
        empty = [str(n) for n in range(7) if str(n) not in occupied]
        shown = ", ".join(seat_disp(n) for n in empty) if empty else "none"
        log(f"  [💺] {tag} | Free seats: {shown}", Fore.CYAN)

    def report_tc(added=None):
        UI["shoe_n"] = len(shoe.cards)
        UI["tc"] = shoe.tc
        UI["taken"] = True
        UI["cards"] = [str(c) for c in shoe.cards]
        update_pdev(shoe.cards)          # 除 TC 外同步算 PDEV（同一份已出牌账本，不用另开）
        # 同步一份「鞋级账本」到 CARRY：断线那一秒它就是最后状态，
        # carry-over 重连时直接拿它接着算。
        CARRY["table"] = name
        CARRY["cards"] = list(shoe.cards)
        CARRY["tc"] = shoe.tc
        CARRY["seat"] = my_seat["v"][0] if my_seat["v"] else None
        CARRY["ts"] = time.strftime("%H:%M:%S")
        render()

    async with websockets.connect(url, ssl=_WSS_SSL if url.startswith("wss") else None) as ws:
        hb_task = asyncio.create_task(_heartbeat(ws, lambda: ch_holder["ch"]))
        clock_task = asyncio.create_task(_noop())   # subscribe 确认后换成真正的连接计时任务

        async def initial_report():
            # 连接后座位重放约 1 秒内推完，等 3 秒作首次空位输出
            await asyncio.sleep(3)
            if reported["init"]:
                return
            reported["init"] = True
            report_empty("after connect")
            empty = [str(n) for n in range(7) if str(n) not in occupied]
            if not empty:
                # 满桌不拉黑：只是这桌现在坐不下，断开回去重掷（下轮还可能再抽到它）
                log("  [🪑] No free seat — disconnecting (not blacklisted)", Fore.YELLOW)
                await ws.close()
                return "no free seat at this table"
            # carry-over：上次坐的位子还空着就回原位（只换座位不改逻辑）
            pref = (carry_in or {}).get("seat")
            if pref is not None and str(pref) in empty:
                seat = str(pref)
                log(f"  [♻️] Reusing last seat {seat_disp(seat)} (carry-over)", Fore.CYAN)
            else:
                seat = random.choice(empty)
            await ws.send(_sitdown(ch_holder["ch"], seat))
            my_pending[seat] = time.monotonic()
            UI["my_seat"] = list(my_seat["v"])
            log(f"  [🪑] Sitting down at seat {seat_disp(seat)}", Fore.CYAN)

        async def sit_seat(seat: str):
            """发 sitdown 并登记为待确认（确认前不会往这个位下注）"""
            await ws.send(_sitdown(ch_holder["ch"], seat))
            my_pending[seat] = time.monotonic()
            log(f"  [🪑] Sitting down at seat {seat_disp(seat)}", Fore.CYAN)

        async def leave_seat(seat: str):
            """按座位号单个退位（<unseat seatNum="N">）；立刻从座位表移除，避免再往这块下注"""
            try:
                await ws.send(_unseat(ch_holder["ch"], seat))
            except Exception:
                return
            leaving.add(seat)
            if seat in my_seat["v"]:
                my_seat["v"].remove(seat)
            bet["pending"].pop(seat, None)
            UI["my_seat"] = list(my_seat["v"])
            log(f"  [🚪] Left seat {seat_disp(seat)}", Fore.CYAN)

        async def adjust_seats(tag: str):
            """每局开始前按上一局结算完的 PDEV 调整座位数。
            PDEV > 0 → 最多 MAX_SEATS_PER_TABLE 个位；PDEV <= 0 → 1 个位。
            能坐几个算几个：空位不够就少坐，不等待、不抢位。"""
            pdev = UI.get("pdev")
            txt = pdev_pct(pdev) if isinstance(pdev, (int, float)) else "—"
            if SEAT_MODE["v"] == SEAT_MODE_FIXED:
                target = 1                      # 永久 1 个位：完全不看 PDEV
                why = "fixed mode"
            else:
                target = MAX_SEATS_PER_TABLE if (isinstance(pdev, (int, float)) and pdev > 0) else 1
                why = f"PDEV {txt}"
            # 迟迟没确认的 sitdown 直接丢掉，否则会一直被当成「已经坐着」而不补位
            now = time.monotonic()
            for s in [s for s, t in my_pending.items() if now - t > SIT_CONFIRM_TIMEOUT]:
                my_pending.pop(s, None)
                log(f"  [⚠️] sitdown not confirmed on seat {seat_disp(s)} — dropping request",
                    Fore.YELLOW)
                try:
                    await ws.send(_unseat(ch_holder["ch"], s))   # 可能其实坐上去了，顺手清掉
                except Exception:
                    pass
            cur = list(my_seat["v"]) + [s for s in my_pending if s not in my_seat["v"]]
            if len(cur) > target:
                for s in cur[target:]:
                    if s in my_seat["v"]:
                        await leave_seat(s)
                    else:
                        my_pending.pop(s, None)
                log(f"  [💺] {tag}: {why} → target {target}, left {len(cur) - target} seat(s)",
                    Fore.MAGENTA)
                return
            if len(cur) < target:
                free = [str(n) for n in range(7)
                        if str(n) not in occupied and str(n) not in my_seat["v"]
                        and str(n) not in my_pending]
                need = target - len(cur)
                pick = random.sample(free, min(need, len(free)))
                for s in pick:
                    await sit_seat(s)
                short = "" if len(pick) == need else f" (only {len(pick)} free, wanted {need})"
                log(f"  [💺] {tag}: {why} → target {target}, sitting {len(pick)} more seat(s){short}",
                    Fore.MAGENTA)

        async def place_bet_later(gid):
            """开局后随机延时 3-5 秒 → 对每个「已确认」座位各发一次主注。
            ★ 不再一遇到「座位还没确认 / uid 还没拿到」就直接放弃：
              每 0.5 秒扫一次，最多 6 秒。这样补坐的位晚一点确认，也能在同一局补上下注。
            """
            await asyncio.sleep(random.uniform(BET_DELAY_MIN, BET_DELAY_MAX))
            amt = bet_amount if bet_amount else (min_bet or FALLBACK_BET)
            for attempt in range(8):             # 3-5s 之后再扫 4 秒（发牌就不能再下注了）
                if bet["last_gid"] != gid:
                    if bet["sent_gid"] != gid:
                        log(f"  [⚠️] Bet skipped — game {gid} already replaced by "
                            f"{bet['last_gid']} before we could send", Fore.YELLOW)
                    return
                if str(bet["ok_gid"]) == str(gid):
                    return                       # 本局已经发牌 → 下注窗口关了，不能再补
                if my_uid["v"] is None or not my_seat["v"]:
                    if attempt == 0:
                        log(f"  [⏳] Bet waiting — seats={seat_disp(my_seat['v'])} "
                            f"uid={my_uid['v']} (retry up to 4s)", Fore.YELLOW)
                    await asyncio.sleep(0.5)
                    continue
                sent = []
                if bet["sent_seats_gid"] != gid:
                    bet["sent_seats_gid"] = gid
                    bet["sent_seats"].clear()        # 新的一局 → 已发座位表清零
                for seat in list(my_seat["v"]):
                    if seat in bet["sent_seats"]:
                        continue                     # 这个位本局已经发过了（确认后也不会重发）
                    try:
                        await ws.send(_lpbet(ch_holder["ch"], gid, seat, amt, my_uid["v"]))
                    except Exception:
                        return
                    bet["pending"][seat] = gid
                    bet["sent_seats"].add(seat)
                    sent.append(seat)
                if sent:
                    bet["sent_gid"] = gid
                    bet["amount"] = amt
                    UI["action"] = (f"Main bet {amt} × {len(bet['sent_seats'])} seat(s) "
                                    f"— awaiting confirm…")
                    log(f"  [💵] Main bet {amt} sent on {len(sent)} seat(s) "
                        f"{seat_disp(sent)} (game {gid}, uid {my_uid['v']}) — awaiting confirm",
                        Fore.YELLOW)
                await asyncio.sleep(0.5)

        def schedule_decision(ps: dict, seat: str):
            """轮到我：对本局这个座位上每个「正在活动且带 decisionOffer」的手，排一个延时动作"""
            gid = str(ps.get("gameId") or "")
            if not gid or my_uid["v"] is None:
                return
            for hkey in sorted(k for k in ps if k.startswith("h") and len(k) == 2):
                hand = ps.get(hkey)
                if not isinstance(hand, dict):
                    continue
                offer = hand.get("decisionOffer")
                if not isinstance(offer, dict):
                    continue
                # 只有真正轮到我这一手才动作（纯预告帧 activeHand=false 且无 decisionTimer）
                if not (hand.get("activeHand") is True or "decisionTimer" in hand):
                    continue
                # ★ 去重 key 必须带座位号：三个座位同一局同一手序（都是 h0）时，
                #   服务器给的 decisionOffer.id 可能相同 → 用 id 单独去重会把第 2、3 个位挡掉
                oid = str(offer.get("id") or "")
                key = (seat, oid)
                if not oid or key in acted:
                    continue          # 同一条 offer（含重播）只动作一次
                acted.add(key)
                if len(my_seat["v"]) > 1:
                    log(f"  [🎯] Offer: seat {seat_disp(seat)} hand {hand_disp(hkey[1:])} "
                        f"id={oid}", Fore.CYAN)
                dec_tasks[key] = asyncio.create_task(
                    act_later(gid, seat, hkey[1:], hand, offer))

        async def act_later(gid, seat, hand_idx, hand, offer):
            """轮到我 → 延时 3-5 秒 → EV 程序算最优动作 → 提交"""
            t0 = time.monotonic()
            delay = random.uniform(ACTION_DELAY_MIN, ACTION_DELAY_MAX)
            await asyncio.sleep(delay)
            if not _mine(seat) or my_uid["v"] is None:
                return
            if time.monotonic() - t0 > DECISION_WINDOW:
                log(f"  [⚠️] Decision window passed (>{DECISION_WINDOW}s) — skipping hand {hand_idx}",
                    Fore.YELLOW)
                return

            allc = list(shoe.cards)          # 全 shoe 已出牌（feed 基线 + 本地累积）
            dcards = list(dealer["cards"])   # 庄家已知牌，[0] 为明牌
            mycards = _hand_cards(hand)      # 我这一手
            bet_amt = bet["amount"] or bet_amount or min_bet or FALLBACK_BET

            dec, ordered = choose_action(allc, dcards, mycards, offer, bet_amt)
            if dec is None:
                return

            try:
                # 多座位：按座位轮流提交（同一毫秒并发发 3 条，服务器可能只认当前这个位）
                async with dec_lock:
                    await ws.send(_decision(ch_holder["ch"], gid, my_uid["v"],
                                            seat, hand_idx, dec))
                    if len(my_seat["v"]) > 1:
                        await asyncio.sleep(0.25)
            except Exception:
                return
            evtxt = " | ".join(f'{x["decision"]} {x["ev"]:+.4f}' for x in ordered)
            UI["ev"] = evtxt
            UI["action"] = f"{dec} sent (seat {seat_disp(seat)}, delay {delay:.1f}s)"
            log(f"  [▶️] Action {dec} sent (game {gid} seat {seat_disp(seat)} "
                f"hand {hand_disp(hand_idx)}) delay {delay:.1f}s", Fore.CYAN)

        async def send_insurance(gid):
            """庄家明牌 A：按保险 EV(3p-1) 决定买 / 不买，并回执（买了保额 = 主注一半）"""
            ev = (calc_ev(list(shoe.cards), ["A"], []) or {}).get("insurance_ev")
            buy = isinstance(ev, (int, float)) and ev > 0
            amt = bet_amount if bet_amount else max(1, int(round((bet["amount"] or min_bet or FALLBACK_BET) / 2)))
            # 只对本局真正下过注的座位回执（补座的位没下注就不买，免得被拒）
            seats = [s for s, g in bet["pending"].items() if str(g) == str(gid)] or list(my_seat["v"])
            if not seats:
                return
            for seat in seats:
                try:
                    await ws.send(_insurance(ch_holder["ch"], gid, my_uid["v"], seat, buy, amt))
                except Exception:
                    return
            log(f"  [🛡️] Insurance {'BUY' if buy else 'DECLINE'} × {len(seats)} seat(s) "
                f"(EV {ev:+.4f}, amount {amt} each) — replied",
                Fore.GREEN if buy else Fore.YELLOW)

        init_task = asyncio.create_task(initial_report())
        try:
            confirmed = False
            async for raw in ws:
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d = None
                if not isinstance(d, dict):
                    continue

                # ── 下注被拒：平台回执带 seat，是我们座位 → 拉黑该桌、断开重掷 ──
                if "placebetError" in d or "betValidationError" in d:
                    key = "placebetError" if "placebetError" in d else "betValidationError"
                    e = d.get(key) or {}
                    if _mine(e.get("seat")):
                        BLACKLIST.add(name)
                        log(f"  [⛔] Bet rejected ({key} {e.get('code')}: {e.get('desc', '')}) "
                            f"— blacklisting table, re-rolling", Fore.RED)
                        return f"bet rejected ({key} {e.get('code')})"
                    continue

                # ── 本局结束仍无我们座位的牌 → 判定被拒（兜底） ──
                if "bjGameEnd" in d:
                    endg = str((d.get("bjGameEnd") or {}).get("id"))
                    # 诊断：这一局我们一注都没发出去 → 明确喊出来，别让它静默过去
                    if endg and str(bet["sent_gid"]) != endg:
                        log(f"  [❌] Game {endg} ended with NO bet sent "
                            f"(seats={seat_disp(my_seat['v'])} uid={my_uid['v']} "
                            f"last_gid={bet['last_gid']})", Fore.RED)
                    pg = {str(v) for v in bet["pending"].values()}
                    # 一注都没确认过（ok_gid 也不是这一局）才算整桌被拒；部分确认只提示
                    if endg in pg and str(bet["ok_gid"]) not in pg:
                        BLACKLIST.add(name)
                        log(f"  [⛔] Round ended (game {endg}) with no cards on our seat(s) "
                            f"— blacklisting table, re-rolling", Fore.RED)
                        return "round ended with no bet confirmation"
                    continue

                # ── 开局公告 → 接管点 / 新局 / 换鞋 + 押主注 ──
                if "betsopen" in d:
                    gid = str((d.get("betsopen") or {}).get("game") or "")
                    # 余额撞上/跌穿上下限 → 此刻上一局已结束（betsopen = 新局开始），
                    # 这一局一注都不发，直接退出监听 → main() 冻结程序（窗口保留）
                    if stop_flag["reason"]:
                        log(f"  [🛑] {stop_flag['reason']} — 本局不下注，程序停止（窗口保留）",
                            Fore.RED)
                        return "STOP:" + stop_flag["reason"]
                    dealer["cards"] = []   # 新一局：清掉上一局的庄家明牌缓存，免得 EV 用错
                    # 新一局 → 板面本局区清空、动态区清屏（固定板面不滚屏）
                    UI["round"] = gid or "—"
                    UI["scores"] = {}
                    UI["dealer"] = "—"
                    UI["me_hands"] = ["", ""]
                    UI["seat_hands"] = {}
                    UI["ev"] = ""
                    UI["action"] = ""
                    UI["events"].clear()
                    render()
                    # 上一局提交的下注至今无任何确认 → 判定被平台静默拒绝
                    pg = {str(v) for v in bet["pending"].values()}
                    if bet["pending"] and gid and gid not in pg and str(bet["ok_gid"]) not in pg:
                        BLACKLIST.add(name)
                        log(f"  [⛔] Previous bet (game {sorted(pg)}) never confirmed — "
                            f"treated as rejected, blacklisting table", Fore.RED)
                        return "previous bet not confirmed"
                    bet["pending"].clear()     # 上一局的待确认表不带到本局
                    if not taken["v"]:
                        snap = None
                        if carry_in and carry_in.get("cards") is not None:
                            # carry-over：直接接上断开前的鞋账本（不等 feed）
                            shoe.load_baseline(carry_in["cards"])
                            taken["v"] = True
                            log(f"  [♻️] Carry-over takeover | resumed {len(shoe.cards)} cards, "
                                f"TC {shoe.tc:+.2f} (saved {carry_in.get('ts') or '?'}) — "
                                f"counting continues from here", Fore.MAGENTA)
                            report_tc()
                            if feed_ctl["want"]:
                                feed_pause()
                                log("  [📴] Carry-over in use — feed not needed, disconnected",
                                    Fore.CYAN)
                        else:
                            snap = snap_map.get(name)
                        if snap:
                            shoe.load_baseline(snap.get("cards"))
                            taken["v"] = True
                            log(f"  [🧮] TC takeover | baseline {len(shoe.cards)} cards | "
                                f"feed ts {snap.get('timestamp')}", Fore.MAGENTA)
                            report_tc()
                            if feed_ctl["want"]:
                                feed_pause()   # 基线到手 → 暂停收 feed，之后全靠本地自算
                                log("  [📴] Baseline received — feed disconnected "
                                    "(local TC from now, auto re-attach)", Fore.CYAN)
                        elif not taken["v"] and TAKEOVER_WITHOUT_BASELINE:
                            # 没有 feed 基线（粘的桌号不在 table_id.txt）→ 连上即开始算：
                            # 从这一刻（0 张）起累计，收到洗牌信号照样清零
                            shoe.clear()
                            taken["v"] = True
                            log("  [🧮] No feed baseline — taking over from 0 now "
                                "(count from here; a shuffle signal will still clear it)",
                                Fore.MAGENTA)
                            report_tc()
                            if feed_ctl["want"]:
                                feed_pause()
                                log("  [📴] No snapshot for this table — feed disconnected", Fore.CYAN)
                        elif not taken["v"]:
                            log("  [⏳] No feed snapshot for this table yet — not taking over this round", Fore.YELLOW)
                    elif (voip["detected"] and voip["after"]) or voip["pending"]:
                        shoe.clear()
                        log("  [🔄] Shuffle detected (voip_cc) — shoe cleared, counting from 0", Fore.MAGENTA)
                        report_tc()
                    else:
                        shoe.new_round()
                    # voip 状态复位（与 PPBJ.py 一致）
                    voip["just"] = True
                    voip["detected"] = False
                    voip["after"] = False
                    voip["pending"] = False
                    # ── 每局按「上一局结算完的 PDEV」调整座位数（只在进入新的一局时做）──
                    #    正 → 最多 MAX_SEATS_PER_TABLE 个位；非正 → 1 个位（能坐几个算几个）
                    if gid and gid != bet["last_gid"]:
                        log(f"  [🎬] New game {gid} | seats={seat_disp(my_seat['v'])} "
                            f"pending={len(my_pending)} uid={my_uid['v']}", Fore.CYAN)
                        await adjust_seats(f"game {gid}")
                    # 已坐下且本局还没真正押出去 → 随机延时 3-5 秒押主注（只押主注）
                    # ★ 用 sent_gid（真发出才算）而不是 last_gid（只是"本局基准"），
                    #   这样补的位晚一步确认也不会让整局变成一注都没发
                    if gid and my_uid["v"] is not None and gid != bet["sent_gid"]:
                        bet["last_gid"] = gid
                        if bet["task"]:
                            bet["task"].cancel()
                        bet["task"] = asyncio.create_task(place_bet_later(gid))
                    continue

                # ── 换鞋准备信号 ──
                if "voip_cc" in d:
                    voip["detected"] = True
                    if voip["just"]:
                        voip["pending"] = True
                        voip["just"] = False
                    continue

                # ── 保险邀请（庄家明牌 A）→ 按 EV 决定买/不买并回执，每局一次 ──
                if "notinsured" in d:
                    ni = d.get("notinsured") or {}
                    gid = bet["last_gid"]
                    log(f"  [🛡️] Insurance offer (seat {seat_disp(ni.get('seat'))} "
                        f"auto={ni.get('auto')}) game {gid}", Fore.CYAN)
                    if (gid and my_uid["v"] is not None and my_seat["v"]
                            and bet["amount"] and ins["done"] != gid):
                        ins["done"] = gid
                        await send_insurance(gid)
                    continue

                # ── 发牌 → 自己扣牌、自己算 TC + 下注确认 + 轮到我则算 EV 出动作 ──
                if "playerSeat" in d:
                    ps = d.get("playerSeat") or {}
                    seat_no = ps.get("seatNumber")
                    # 庄家帧（seatNumber=-1）：记下已知牌，第一张即明牌，供 EV 使用
                    if seat_no == -1:
                        h0 = ps.get("h0") or {}
                        dcs = _hand_cards(h0)
                        dealer["cards"] = dcs
                        if h0.get("score"):
                            UI["dealer"] = (f"{h0['score']} ( {' '.join(dcs)} )"
                                            if dcs else str(h0["score"]))
                    else:
                        # 各座位点数 → 板面（本局进行中逐张刷新）
                        sk = str(seat_no)
                        for hk in (k for k in ps if k.startswith("h") and len(k) == 2):
                            hh = ps.get(hk)
                            if isinstance(hh, dict) and hh.get("score"):
                                UI["scores"][sk] = str(hh["score"])
                    if voip["detected"]:
                        voip["after"] = True
                    if taken["v"]:
                        added = shoe.feed_player_seat(ps)
                        if added:
                            report_tc(added)
                    # 下注确认：本局我们某个座位真实拿到牌
                    cgid = str(ps.get("gameId"))
                    if _mine(seat_no) and bet["pending"].get(str(seat_no)) == cgid:
                        bet["pending"].pop(str(seat_no), None)
                        bet["ok_gid"] = cgid
                        log(f"  [✅] Bet confirmed (game {cgid} "
                            f"seat {seat_disp(seat_no)} got cards)", Fore.GREEN)
                    # 我们座位：轮到我 → 排动作；并打印动作回执（服务器确认我们这手做了什么）
                    if _mine(seat_no):
                        last_ps[str(seat_no)] = ps
                        # 多座位：这个座位本局的手牌（每座位一行；正在行动的手加 *）
                        if len(my_seat["v"]) > 1:
                            parts = []
                            for hk2 in sorted(k for k in ps if k.startswith("h") and len(k) == 2):
                                hh2 = ps.get(hk2)
                                if not isinstance(hh2, dict):
                                    continue
                                cs2 = _hand_cards(hh2)
                                if cs2:
                                    mark = "*" if hh2.get("activeHand") is True else ""
                                    parts.append(f"{hh2.get('score', '?')}{mark}"
                                                 f"({' '.join(cs2)})")
                            if parts:
                                UI["seat_hands"][str(seat_no)] = " | ".join(parts)
                        schedule_decision(ps, str(seat_no))
                        # 多座位兜底：服务器有时只在某一座位的帧里带整桌状态 →
                        # 顺手把其它我方座位缓存的「同一局」状态也重扫一遍（靠 (座位,id) 去重）
                        if len(my_seat["v"]) > 1:
                            for s2 in list(my_seat["v"]):
                                if s2 == str(seat_no):
                                    continue
                                p2 = last_ps.get(s2)
                                if (isinstance(p2, dict)
                                        and str(p2.get("gameId")) == str(ps.get("gameId"))):
                                    schedule_decision(p2, s2)
                        for hkey in sorted(k for k in ps if k.startswith("h") and len(k) == 2):
                            hh = ps.get(hkey)
                            if not isinstance(hh, dict):
                                continue
                            cs = _hand_cards(hh)
                            slot = int(hkey[1:]) if hkey[1:].isdigit() else -1   # 协议手号 0/1 → 板面 hand1 / hand2
                            if cs and 0 <= slot < len(UI["me_hands"]):
                                # 多座位时前面标上座位号，否则看不出这一手是哪个位的
                                tag = f"S{seat_disp(seat_no)} " if len(my_seat["v"]) > 1 else ""
                                UI["me_hands"][slot] = f"{tag}{hh.get('score', '?')} ( {' '.join(cs)} )"
                            cd = hh.get("currentDecision")
                            if (hh.get("currentDecisionAction") in ("playerDecision", "systemCall")
                                    and cd):
                                sig = (str(seat_no), str(ps.get("gameId")), hkey, cd,
                                       len(hh.get("cards") or []))
                                if sig not in seen_dec:
                                    seen_dec.add(sig)
                                    UI["action"] = (f"{cd} confirmed by server "
                                                    f"(seat {seat_disp(seat_no)} "
                                                    f"hand {hand_disp(hkey[1:])})")
                                    log(f"  [✅] Action confirmed: {cd} "
                                        f"(seat {seat_disp(seat_no)} "
                                        f"hand {hand_disp(hkey[1:])})", Fore.GREEN)
                        render()
                    continue

                # ── 座位消息 → 按座位号记账 ──
                if "seat" in d:
                    s = d.get("seat") or {}
                    num = str(s.get("num"))
                    ev = s.get("event")
                    if ev == "sit":
                        occupied[num] = s.get("screen_name", "")
                        UI["occupy"][num] = s.get("screen_name", "")
                        # 我们自己请求坐下的位子（一次可能多个）→ 转入已确认座位表
                        if num in my_pending:
                            my_pending.pop(num, None)
                            if num not in my_seat["v"]:
                                my_seat["v"].append(num)
                            if my_uid["v"] is None:
                                my_uid["v"] = s.get("user_id")
                            UI["my_seat"] = list(my_seat["v"])
                            log(f"  [✅] Seat confirmed: {seat_disp(num)} "
                                f"(user_id={my_uid['v']}) — {len(my_seat['v'])} seat(s) now", Fore.GREEN)
                    elif ev == "stand":
                        occupied.pop(num, None)
                        UI["occupy"].pop(num, None)
                        UI["scores"].pop(num, None)
                        if num in leaving:
                            leaving.discard(num)     # 我们自己退的，不算被踢
                            continue
                        if num in my_seat["v"]:
                            my_seat["v"].remove(num)
                            UI["my_seat"] = list(my_seat["v"])
                        # 我们因久坐未下注被服务器请出 → 断开，回去重新掷骰选桌（不拉黑）
                        if (s.get("type") == "timeout" and my_uid["v"]
                                and s.get("user_id") == my_uid["v"]):
                            log("  [⛔] Kicked from seat by server (stand/timeout)",
                                Fore.RED)
                            return "kicked from seat by server (stand/timeout)"
                    continue

                # ── subscribe 确认 → 记录服务器 channel，立即发首条心跳，并开始计连接时长 ──
                if not confirmed and "subscribe" in d:
                    ch = (d.get("subscribe") or {}).get("channel")
                    if ch:
                        ch_holder["ch"] = ch
                        confirmed = True
                        log(f"  [🔗] subscribe confirmed, channel={ch}", Fore.GREEN)
                        conn_start()      # 真正连上这张桌 → 开始计时（面板 Connected: HH:MM:SS）
                        clock_task.cancel()
                        clock_task = asyncio.create_task(conn_ticker())
                        await ws.send(_ping(ch))
        except websockets.ConnectionClosed:
            pass
        finally:
            conn_stop()               # 定格最终连接时长（面板上保留下来）
            clock_task.cancel()
            hb_task.cancel()
            init_task.cancel()
            if bet["task"]:
                bet["task"].cancel()
            for t in dec_tasks.values():
                t.cancel()
        return None


def _ping(channel: str) -> str:
    return f'<ping channel="{channel}" time="{int(time.time() * 1000)}"/>'


def _sitdown(channel: str, seat: str) -> str:
    return (f'<command channel="{channel}">'
            f'<sitdown gameMode="blackjack_desktop" seatNum="{seat}"></sitdown>'
            f'</command>')


def _unseat(channel: str, seat: str) -> str:
    """离开座位（按座位号单个退）—— 格式取自实测帧：
    <command channel="table-xxx"><unseat seatNum="6"></unseat></command>"""
    return (f'<command channel="{channel}">'
            f'<unseat seatNum="{seat}"></unseat>'
            f'</command>')


def _lpbet(channel: str, gid, seat, amount: int, user_id: str) -> str:
    """下注主注（只押主注，其余玩法一律 false）—— 格式取自实测抓包 bet_test_log.txt"""
    ck = str(int(time.time() * 1000))
    return (f'<command channel="{channel}">'
            f'<lpbet gameMode="blackjack_desktop" gameId="{gid}" userId="{user_id}" ck="{ck}">'
            f'<bet seat="{seat}" amount="{amount}" mainbet="true" bj21plus3="false" '
            f'perfectpair="false" betbehind="false" ck="{ck}"/>'
            f'</lpbet></command>')


# EV 动作名 → 协议 dec 值（注意 cashOut 是驼峰，其余全小写）
DEC_MAP = {"Hit": "hit", "Stand": "stand", "Double": "double",
           "Split": "split", "CashOut": "cashOut"}


def choose_action(all_cards, dealer_cards, player_cards, offer, bet_amount):
    """
    把这一手原样交给 PP EV.py 算最优动作（含 cashOut），再按服务器白名单过滤。
    不做任何自加的额外判据 —— 兑现价怎么算、算出来排第几，全按 PP EV.py 的公式。
    返回 (dec, ordered)：dec 为协议值（None = 无可用动作），ordered 为 EV 降序候选。
    """
    # 提前兑现：服务器给了就原样交进去，由 EV.py 自己的公式（cash_out ÷ 主注 − 1）参与排名
    co = None
    raw = offer.get("cashOut")
    if USE_CASHOUT and isinstance(raw, (int, float)):
        co = float(raw)

    r = calc_ev(all_cards, dealer_cards, player_cards, cash_out=co, bet_amount=bet_amount)
    ordered = r.get("all_decisions") or []

    # 服务器白名单：hit / stand 永远可发；double / split / cashOut 需被允许
    allow = {"Hit": True, "Stand": True,
             "Double": bool(offer.get("canDouble")),
             "Split": bool(offer.get("canSplit")),
             "CashOut": co is not None}
    pick = next((x for x in ordered if allow.get(x["decision"])), None)
    return (DEC_MAP[pick["decision"]] if pick else None), ordered


def _decision(channel: str, gid, user_id: str, seat, hand, dec: str) -> str:
    """提交动作 —— 格式取自实测抓包：<decision ... seatNum hand dec>（不需要 decisionOffer.id）"""
    ck = str(int(time.time() * 1000))
    return (f'<command channel="{channel}">'
            f'<decision gameMode="blackjack_desktop" gameId="{gid}" userId="{user_id}" '
            f'ck="{ck}" seatNum="{seat}" hand="{hand}" dec="{dec}"></decision>'
            f'</command>')


def _insurance(channel: str, gid, user_id: str, seat, buy: bool, amount: int) -> str:
    """保险回执：choice=y 买 / n 不买，amount 为保额 —— 格式取自实测抓包"""
    ck = str(int(time.time() * 1000))
    return (f'<command channel="{channel}">'
            f'<insurance gameMode="blackjack_desktop" gameId="{gid}" userId="{user_id}" '
            f'ck="{ck}" >'
            f'<bet choice="{"y" if buy else "n"}" seatNum="{seat}" betBehind="false" '
            f'amount="{amount}" /></insurance></command>')


async def _heartbeat(ws, get_channel):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await ws.send(_ping(get_channel()))
        except Exception:
            return


async def _noop():
    """占位任务：连接计时任务在 subscribe 确认时才真正启动（见 listen_table）"""
    return


async def stop_forever(reason: str):
    """冻结程序：停止计时、面板标注原因、窗口保留、绝不重连（永久挂起事件循环）"""
    conn_stop()
    UI["stopped"] = reason
    UI["action"] = f"STOPPED — {reason} (窗口保留，程序未退出)"
    log(f"  [🛑] Program STOPPED — {reason} — 窗口保留，不再重连。"
        " 关掉这个窗口即退出。", Fore.RED)
    render()
    await asyncio.to_thread(report_status, "stopped", reason)   # 补报一次 → 老板端立刻显示「已停止」
    await asyncio.Event().wait()          # 永不返回：不退出程序，窗口留着让你看到原因


# ═══════════════════════ 主流程 ═══════════════════════
async def main():
    print("=" * 46)
    print("  PP AutoBot (login · table pick · connect · heartbeat · EV decisions)")
    print("=" * 46)

    tables = load_tables()
    log(f"  [📋] {len(tables)} table mappings loaded (table_id.txt)")
    tables_by_id = {v: k for k, v in tables.items()}   # 桌号 → 桌名（直连模式下反查显示名）

    # 启动即走「粘贴完整 wss URL」；URL 在这里读完，避免与后面的异步日志交错
    log("  [👤] Login: pasted URL", Fore.CYAN)
    UI["login"] = "pasted URL"
    pasted_sid, pasted_tid = ask_pasted_jsid()
    seat_mode = ask_seat_mode()        # 1 = 永久 1 个位；2 = 跟 PDEV 走
    SEAT_MODE["v"] = seat_mode
    UI["seat_mode"] = ("fixed 1 seat" if seat_mode == SEAT_MODE_FIXED
                       else f"PDEV max {MAX_SEATS_PER_TABLE}")
    bet_amount = ask_bet_amount()      # 固定注额；None = 用该桌最低限额

    if USE_BALANCE_LIMIT:
        upper, lower = ask_balance_limits()   # 余额上限 / 下限（回车留空 = 该条不启用）
    else:
        upper = lower = None
        log("  [🎚️] Balance limits: OFF (USE_BALANCE_LIMIT=False)", Fore.CYAN)
    balance_limit["upper"] = upper
    balance_limit["lower"] = lower

    # 断线后怎么重连（1 = 严格等基线，2 = 沿用上次鞋账本直接跑）
    reconnect_mode = ask_reconnect_mode()
    UI["reconnect"] = RECONNECT_MODES.get(reconnect_mode, "?")

    if USE_FEED:
        feed = asyncio.create_task(feed_task())
    else:
        feed = None
        log("  [📴] 网络端(feed) 已停用 (USE_FEED=False) — 不连 pp.knewb2u.com；"
            "连上桌后直接无基线接管，TC 从 0 起算", Fore.CYAN)

    sid = pasted_sid
    build_fails = 0
    # False = 首次连接 / 刚换了新 URL（允许没有基线，从 0 起算）
    # True  = 同一张桌断开后的自动重连（必须先拿到 feed 基线，否则停）
    need_baseline = False
    asyncio.create_task(balance_task(sid))   # 余额轮询：面板常驻显示

    while True:
        # 需要新基线 → 把 TC 数据源接回来；旧快照清掉（防止拿上一次的旧账当新基线）
        # carry-over 重连不需要基线 → 不接 feed（省一次连上又断开）
        feed_ctl["want"] = USE_FEED and not (need_baseline and reconnect_mode == 2)
        snap_map.clear()
        tc_map.clear()
        if USE_PASTED_TABLE:
            # 直连模式：粘贴 URL 里是哪个 tableId 就连那张，掷骰选桌整段跳过
            table_id = pasted_tid
            name = tables_by_id.get(table_id, table_id)
            if name == table_id:
                log(f"  [📌] Direct mode: tableId {table_id} (from pasted URL) — "
                    f"dice roll skipped | 不在 table_id.txt → 没有该桌的外部 TC 快照", Fore.CYAN)
            else:
                log(f"  [📌] Direct mode: {name} (id {table_id}) from pasted URL — "
                    f"dice roll skipped", Fore.CYAN)
        else:
            name = await pick_table(tables)
            table_id = tables[name]

        # ★ 重连门禁：必须先从 feed 拿到这张桌的基线（已出牌 + TC）才允许重连；
        #   拿不到 → 停止（窗口保留）。首次连接 / 换了新 URL / carry-over 不走这里。
        if need_baseline and REQUIRE_FEED_BASELINE_ON_RECONNECT and reconnect_mode == 1:
            UI["active"] = True
            UI["stopped"] = ""
            UI["action"] = (f"waiting for feed baseline… "
                            f"(up to {int(REQUIRE_BASELINE_WAIT)}s)")
            render()
            snap = await wait_for_baseline(name, REQUIRE_BASELINE_WAIT)
            if snap is None:
                await stop_forever(f"no feed baseline for '{name}' on reconnect "
                                   f"(waited {int(REQUIRE_BASELINE_WAIT)}s)")
                return
            n = len(snap.get("cards") or [])
            log(f"  [✅] Baseline received: {n} cards, TC {snap.get('tc')} "
                f"(feed ts {snap.get('timestamp')}) — proceeding to reconnect", Fore.GREEN)
            UI["action"] = f"baseline OK ({n} cards) — connecting…"
            render()

        # ♻️ carry-over：同一张桌、且断开前已经接管过 → 直接拿那份鞋账本当基线
        #    （换桌 / 从没接管过 → None，连上后按 TAKEOVER_WITHOUT_BASELINE 从 0 起算）
        carry_in = None
        if (need_baseline and reconnect_mode == 2
                and CARRY.get("table") == name and CARRY.get("cards") is not None):
            carry_in = {"cards": list(CARRY["cards"]), "tc": CARRY["tc"],
                        "seat": CARRY.get("seat"), "ts": CARRY.get("ts")}
            log(f"  [♻️] Carry-over: resuming '{name}' with {len(carry_in['cards'])} cards, "
                f"TC {carry_in['tc']:+.2f} (saved {carry_in['ts'] or '?'}) — no feed wait",
                Fore.MAGENTA)
        elif need_baseline and reconnect_mode == 2:
            log("  [♻️] Carry-over: no saved shoe for this table — starting from 0", Fore.YELLOW)

        try:
            url, min_bet = await asyncio.to_thread(build_ws_url, sid, table_id)
            build_fails = 0
        except Exception as e:
            log(f"  [❌] Failed to build {name}: {e} — "
                f"{'retrying the same table (direct mode)' if USE_PASTED_TABLE else 'picking another table'}",
                Fore.RED)
            build_fails += 1
            if USE_PASTED_TABLE and build_fails >= 3:
                # 直连模式没有「换桌」可退，连续失败基本只有一种原因：JSESSIONID 过期
                log("  [🔁] Still failing in direct mode — paste a new URL:", Fore.YELLOW)
                pasted_sid, pasted_tid = ask_pasted_jsid()
                sid = pasted_sid
                build_fails = 0
                need_baseline = False      # 换了新 URL → 视为首次连接，不卡基线
                continue
            await asyncio.sleep(3)
            continue

        # 板面：新一桌信息 + 重置本局/牌靴区
        # carry-over 有账本时：牌靴区不清零，直接显示接着算的数值（等 betsopen 正式接管）
        UI["active"] = True
        UI["table"] = name
        UI["min_bet"] = min_bet
        UI["my_seat"] = None
        UI["taken"] = False
        UI["stopped"] = ""            # 新一轮连接 → 清掉上一次的停止标记
        UI["shoe_n"] = len(carry_in["cards"]) if carry_in else 0
        UI["tc"] = carry_in["tc"] if carry_in else 0.0
        UI["cards"] = [str(c) for c in carry_in["cards"]] if carry_in else []
        UI["scores"] = {}
        UI["occupy"] = {}
        UI["me_hands"] = ["", ""]
        UI["ev"] = ""
        UI["action"] = ""
        UI["conn_secs"] = 0.0
        conn_reset()                  # 连接时长归零，等 subscribe 确认后开始计
        render()

        log(f"  [🔌] Connecting to {name} ... (min bet {min_bet or FALLBACK_BET}, "
            f"our bet {bet_amount or 'auto=min'})", Fore.CYAN)
        stopped_reason = ""
        limit_stop = ""
        exit_reason = ""
        try:
            reason = await listen_table(name, url, min_bet, bet_amount, carry_in=carry_in)
            if reason:
                if reason.startswith("STOP:"):
                    limit_stop = reason[5:]      # 余额上下限 → 无条件停止（与断线开关无关）
                else:
                    exit_reason = reason
        except websockets.ConnectionClosed:
            # 非主动断开（网络断 / 被服务器踢）—— 主动断开走的是 reason 那条路，不是这里
            stopped_reason = "connection closed"
            log(f"  [⛔] {name} connection closed", Fore.RED)
        except Exception as e:
            stopped_reason = f"disconnected ({e})"
            log(f"  [⛔] {name} disconnected: {e}", Fore.RED)

        # 这张桌的连接「结束原因」（停止时显示在面板上）
        exit_reason = exit_reason or stopped_reason or "connection ended"

        if limit_stop:
            stop_reason = limit_stop                       # 余额上下限 → 停
        elif STOP_ON_ANY_EXIT:
            stop_reason = exit_reason                      # ★ 只要连接结束就停，绝不重连、绝不换座
        elif stopped_reason and STOP_ON_DISCONNECT:
            stop_reason = stopped_reason
        else:
            stop_reason = ""

        if stop_reason:
            await stop_forever(stop_reason)
            return

        # ── 只有「明确允许重连」才会走到这里（STOP_ON_ANY_EXIT=False 且上面没停）──
        log(f"  [↩️] {name}: {exit_reason} — re-rolling in {RECONNECT_WAIT}s", Fore.YELLOW)
        await asyncio.sleep(RECONNECT_WAIT)

        # 直连模式下只有这一张桌：它一旦被拉黑（下注被拒），重连它没意义 → 重新粘贴 URL
        if USE_PASTED_TABLE and name in BLACKLIST:
            log("  [🔁] Table blacklisted (bet rejected) — paste a new URL "
                "(换新 URL 会清空黑名单):", Fore.YELLOW)
            pasted_sid, pasted_tid = ask_pasted_jsid()
            sid = pasted_sid
            BLACKLIST.clear()
            need_baseline = False          # 新 URL = 新桌 → 视为首次连接
        else:
            # 同一张桌自动重连 → 下一趟必须先拿到 feed 基线，拿不到就停
            need_baseline = True


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\033[?25h", end="")     # 恢复光标
        report_status("stopped", "Ctrl+C exit")      # 补报一次 → 老板端立刻显示「已停止」
        print("\n[👋] Exited")
