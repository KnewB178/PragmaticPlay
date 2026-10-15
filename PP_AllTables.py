# -*- coding: utf-8 -*-
"""
PP_AllTables.py —— Pragmatic Play 大厅「全量桌号」采集器（不做任何桌型筛选）

为什么之前采不到
────────────────
1. 大厅 WS 换域名了：老的 wss://dga.pplc001.net/ws 现在对任何 casinoId 都回
   {"error":"Casino not supported: ... (key: NNN)"}。
   新的（由 PP 前端 /api/env/getAll 的 "dgaWS" 字段给出）是：
       wss://dga-lc.pplc001.net/ws
2. 原来的 ws-collector.py / PPBJ_Table_List.py 里有一行
   `if msg.get("tableType") != "BLACKJACK": return`
   —— 所有非 21 点的桌都被这一行丢掉了。

本脚本做什么
────────────
连上大厅 → 服务端会主动下发一条 {"tableKey":[...]} 帧，这就是该 casino
**全部桌号**（含百家乐 / 轮盘 / 骰宝 / 龙虎 / One Blackjack / 游戏秀 …）。
然后拿这份全量 key 再订阅一次，把所有桌的桌名 / 类型收下来。

用法
────
    python PP_AllTables.py                      # 用默认 casinoId / MYR
    python PP_AllTables.py <casinoId> [currency] [seconds]

产物（写在脚本同目录）
────
    All_Tables.txt   : 桌名 | 桌号 | snapId | 桌型 | 子类型
                       （桌号 = 帧里的 tableId；snapId = 你原本 table_id.txt 里那种串，
                         两个都能直接塞进 tableConfig / wss URL 的 tableId 参数）
    Server_Keys.txt  : 服务端下发的全部 key（一行一个，不筛选）
    lobby_raw.jsonl  : 服务端原始帧留证

无鉴权：大厅 WS 不需要 JSESSIONID，只需要 casinoId。
"""

import asyncio
import json
import os
import sys
import time

import websockets

DGA_WS = "wss://dga-lc.pplc001.net/ws"

# 默认值 = 本项目一直在用的运营商 casino（跟余额接口 /api/ui/balance 返回的一致）
DEFAULT_CASINO = "ppcdg00000001767"
DEFAULT_CURRENCY = "MYR"

# 种子 key：先随便订几个已知桌，服务端就会把全量 tableKey 回给你
SEED_KEYS = ["515", "566", "403", "227", "1101", "801"]

HERE = os.path.dirname(os.path.abspath(__file__))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _log(msg):
    print(msg, flush=True)


async def collect(casino, currency, seconds):
    all_keys = []          # 服务端下发的全量 key
    tables = {}            # tableId -> {name, type, subtype, ...}
    raw_frames = []
    errors = []

    async with websockets.connect(DGA_WS, additional_headers={"User-Agent": UA},
                                  open_timeout=20, close_timeout=5,
                                  max_size=8 * 1024 * 1024) as ws:
        _log("  [connected] %s" % DGA_WS)

        await ws.send(json.dumps({"type": "available", "casinoId": casino}))
        await asyncio.sleep(0.4)
        await ws.send(json.dumps({"type": "subscribe", "isDeltaEnabled": True,
                                  "casinoId": casino, "key": SEED_KEYS,
                                  "currency": currency}))

        subscribed_all = False
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, seconds - (time.time() - t0)))
            except asyncio.TimeoutError:
                break
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            raw_frames.append(raw)

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            if isinstance(msg, dict) and "error" in msg:
                errors.append(msg["error"])
                continue

            # ① 服务端下发的全量 key
            if isinstance(msg, dict) and msg.get("tableKey") and not all_keys:
                all_keys = list(msg["tableKey"])
                _log("  [tableKey] 服务端下发全部桌号: %d 个" % len(all_keys))
                # ② 拿全量 key 再订一次（分批，避免 message too big）
                for i in range(0, len(all_keys), 250):
                    chunk = all_keys[i:i + 250]
                    await ws.send(json.dumps({"type": "subscribe", "isDeltaEnabled": True,
                                              "casinoId": casino, "key": chunk,
                                              "currency": currency}))
                    await asyncio.sleep(0.3)
                subscribed_all = True

            # ③ 桌帧
            if isinstance(msg, dict) and msg.get("tableId") and msg.get("tableName"):
                tid = str(msg["tableId"])
                snap = _snap_id(msg.get("relativeSnapPath") or msg.get("tableImage") or "") or tid
                tables[tid] = {
                    "name": msg.get("tableName"),
                    "type": msg.get("tableType"),
                    "subtype": msg.get("tableSubtype"),
                    "snap": snap,
                    "open": msg.get("tableOpen", True),
                    "seats": msg.get("totalSeatedPlayers"),
                    "min_bet": (msg.get("tableLimits") or {}).get("minBet"),
                    "fields": sorted(msg.keys()),
                }

        try:
            await ws.close()
        except Exception:
            pass

    return all_keys, tables, raw_frames, errors, subscribed_all


def _snap_id(path):
    """从 relativeSnapPath 里抽出 snapId（跟原 ws-collector.py 的规则一致）"""
    import re
    m = re.search(r"assets/snaps/([^/]+)", path or "")
    return m.group(1) if m else ""


def write_outputs(casino, currency, all_keys, tables, raw_frames, errors):
    keys_path = os.path.join(HERE, "Server_Keys.txt")
    with open(keys_path, "w", encoding="utf-8") as f:
        f.write("# casinoId=%s  currency=%s  %s\n" % (casino, currency, time.strftime("%Y-%m-%d %H:%M:%S")))
        f.write("# 服务端大厅下发的一条 tableKey 帧，就是该 casino 的全部桌号（未做任何筛选）\n")
        for k in all_keys:
            f.write("%s\n" % k)

    def sortkey(item):
        tid, info = item
        return (str(info.get("type") or ""), str(info.get("name") or ""))

    out_path = os.path.join(HERE, "All_Tables.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# casinoId=%s  currency=%s  %s\n" % (casino, currency, time.strftime("%Y-%m-%d %H:%M:%S")))
        f.write("# 桌名 | 桌号 | snapId | 桌型 | 子类型\n")
        for tid, info in sorted(tables.items(), key=sortkey):
            f.write("%s | %s | %s | %s | %s\n" % (info.get("name"), tid, info.get("snap"),
                                                  info.get("type"), info.get("subtype")))

    raw_path = os.path.join(HERE, "lobby_raw.jsonl")
    with open(raw_path, "w", encoding="utf-8") as f:
        for r in raw_frames:
            f.write(r + "\n")

    return out_path, keys_path, raw_path


async def main():
    args = sys.argv[1:]
    casino = args[0] if len(args) > 0 else DEFAULT_CASINO
    currency = args[1] if len(args) > 1 else DEFAULT_CURRENCY
    seconds = float(args[2]) if len(args) > 2 else 20.0

    _log("=" * 70)
    _log("PP 大厅全量桌号采集（不做桌型筛选）")
    _log("  casinoId = %s   currency = %s   采集 %ss" % (casino, currency, seconds))
    _log("=" * 70)

    all_keys, tables, raw_frames, errors, ok = await collect(casino, currency, seconds)

    _log("")
    _log("  tableKey 全量桌号 : %d" % len(all_keys))
    _log("  实际回帧的桌      : %d" % len(tables))
    _log("  错误帧            : %d" % len(errors))
    if errors:
        _log("  首个错误: %s" % errors[0][:160])

    if not all_keys:
        _log("\n  [x] 没拿到 tableKey —— 确认 casinoId 是否正确。")
        return

    by_type = {}
    for info in tables.values():
        by_type.setdefault(info.get("type") or "?", []).append(info.get("name"))
    _log("")
    _log("  按桌型统计：")
    for t, names in sorted(by_type.items()):
        _log("    %-16s %3d 桌   %s" % (t, len(names), ", ".join(sorted(str(n) for n in names))[:90]))

    out, keys, raw = write_outputs(casino, currency, all_keys, tables, raw_frames, errors)
    _log("")
    _log("  已写出：")
    _log("    %s" % out)
    _log("    %s" % keys)
    _log("    %s" % raw)


if __name__ == "__main__":
    asyncio.run(main())
