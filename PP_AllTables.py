#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PP 大厅全量桌号采集器 —— 不做任何桌型筛选，服务端推什么就记什么。

原采集器（ws-collector.py / PPBJ_Table_List.py）里有一句
    if data.get("tableType") != "BLACKJACK": continue
把黑杰克以外的桌（百家乐/轮盘/骰宝/龙虎/游戏秀…）全滤掉了。
本脚本去掉这层筛选。

两种取数方式：
  1) HTTP 大厅接口（推荐，一次拿全）：
        https://<gs-host>/api/ui/lobby?JSESSIONID=<sid>
     不带 session 会回 401 <response><session>offline</session></response>
  2) WebSocket 大厅（老方式，服务端逐条推）：
        wss://<host>/ws   +  subscribe{casinoId, key[], currency}

用法：
    python PP_AllTables.py <JSESSIONID>
    python PP_AllTables.py <JSESSIONID> --host gs3.pplc001.net
    python PP_AllTables.py --ws            # 走 WebSocket 大厅

产物：
    lobby_raw.txt     原始响应（不解析，先留证据）
    All_Tables.txt    全量桌号： 名字 | 桌号 | 类型
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT_RAW = HERE / "lobby_raw.txt"
OUT_ALL = HERE / "All_Tables.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

LOBBY_HOSTS = ["gs3.pplc001.net", "gs1.pplc001.net", "gs2.pplc001.net",
               "gs4.pplc001.net", "gs5.pplc001.net", "gs6.pplc001.net",
               "games.pplc001.net"]

WS_LOBBY = "wss://dga.pplc001.net/ws"
CASINO_ID = "ppcdg00000001767"

ID_KEYS = ("tableId", "key", "snapId", "tableKey", "id")
NAME_KEYS = ("tableName", "name", "displayName", "tableDescription")
TYPE_KEYS = ("tableType", "gameType", "tableSubtype", "gameCode", "subtype")


# ────────────────────────────────────────────────────────────── HTTP 大厅 ──
def fetch_http_lobby(sid: str, hosts: list[str]):
    for h in hosts:
        url = f"https://{h}/api/ui/lobby"
        try:
            r = requests.get(url, params={"JSESSIONID": sid}, timeout=15,
                             headers={"User-Agent": UA}, allow_redirects=False)
        except Exception as e:
            print(f"  [x] {h:<20} 连不上：{type(e).__name__}")
            continue
        body = r.text
        print(f"  [{'+' if r.status_code == 200 else ' '}] {h:<20} "
              f"HTTP {r.status_code}  {len(body)} 字节")
        if "session" in body[:200].lower() and "offline" in body[:200].lower():
            print("        ↑ session 无效或已过期")
            continue
        if r.status_code == 200 and body.strip():
            return h, body
    return None, None


# ────────────────────────────────────────────────── WebSocket 大厅（不筛型） ──
def collect_ws_lobby(duration: float = 40.0):
    import asyncio
    import websockets

    keys = [str(k) for k in
            [594, 521, 596, 522, 538, 518, 593, 563, 515, 590, 555, 543,
             597, 565, 561, 511, 560, 592, 524, 520, 525, 567, 517, 566,
             544, 523, 528, 536, 553, 562, 516, 556, 540, 564, 513, 595,
             552, 526, 514, 529, 537, 602, 530, 527, 606, 568, 301, 570,
             303, 304, 519, 535, 626, 542, 541, 604, 605, 551, 554, 305,
             302, 512, 569, 321, 322, 591, 612, 613, 614, 615, 616, 617,
             618, 619, 641, 642, 643, 644, 645, 646, 647, 658, 659, 660,
             661, 620, 621, 607, 608, 609, 598, 599, 600, 601, 603]]

    async def run():
        frames = []
        async with websockets.connect(WS_LOBBY, open_timeout=12, max_size=None) as ws:
            await ws.send(json.dumps({
                "type": "subscribe", "isDeltaEnabled": True,
                "casinoId": CASINO_ID, "currency": "MYR", "key": keys,
            }))
            print(f"[WS] 已订阅 {len(keys)} 个 key，收 {duration:.0f} 秒…")
            t0 = time.time()
            while time.time() - t0 < duration:
                try:
                    raw = await asyncio.wait_for(ws.recv(),
                                                 timeout=duration - (time.time() - t0))
                except asyncio.TimeoutError:
                    break
                frames.append(raw)
        return frames

    return asyncio.run(run())


# ─────────────────────────────────────────────────────────── 通用抽取 ──
def _walk(obj, out):
    if isinstance(obj, dict):
        ids = [obj.get(k) for k in ID_KEYS if isinstance(obj.get(k), (str, int))]
        names = [obj.get(k) for k in NAME_KEYS if isinstance(obj.get(k), str)]
        if ids and names:
            types = [str(obj.get(k)) for k in TYPE_KEYS if obj.get(k)]
            out.append({
                "id": str(ids[0]),
                "name": names[0],
                "type": types[0] if types else "?",
                "raw": obj,
            })
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


def extract(text: str):
    """先当 JSON 抽；抽不到再当「原始帧逐行 JSON」抽。返回去重后的记录列表。"""
    records = []
    try:
        _walk(json.loads(text), records)
    except Exception:
        pass
    if not records:                      # WS 逐行帧
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    _walk(json.loads(line), records)
                except Exception:
                    pass
    seen, uniq = set(), []
    for r in records:
        k = (r["name"], r["id"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def natural_key(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", s)]


def save(records):
    type_counts = {}
    for r in records:
        type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1
    lines = [f"{r['name']} | {r['id']} | {r['type']}" for r in records]
    OUT_ALL.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n共 {len(records)} 张桌（**未做任何筛选**）→ {OUT_ALL.name}")
    print("按类型：" + "  ".join(f"{k}={v}" for k, v in
                                 sorted(type_counts.items(), key=lambda x: -x[1])))
    for r in records[:10]:
        print(f"   {r['name']} | {r['id']} | {r['type']}")
    if len(records) > 10:
        print(f"   … 其余 {len(records) - 10} 张见文件")
    return type_counts


def main():
    ap = argparse.ArgumentParser(description="PP 大厅全量桌号采集（不筛类型）")
    ap.add_argument("sid", nargs="?", help="JSESSIONID")
    ap.add_argument("--host", help="指定大厅 host，默认逐个试")
    ap.add_argument("--ws", action="store_true", help="改走 WebSocket 大厅")
    ap.add_argument("--ws-seconds", type=float, default=40.0)
    args = ap.parse_args()

    if args.ws:
        frames = collect_ws_lobby(args.ws_seconds)
        text = "\n".join(frames)
        OUT_RAW.write_text(text, encoding="utf-8")
        print(f"\n收到 {len(frames)} 帧 → {OUT_RAW.name}")
        if frames:
            print("首帧：" + frames[0][:200])
        recs = extract(text)
        if recs:
            save(recs)
        else:
            print("\n⚠️ 没抽到任何桌 → 服务端没推数据（可能大厅入口对当前 casino 已失效）")
            print("   已把原始帧写进 lobby_raw.txt，可据此判读。")
        return

    if not args.sid:
        ap.error("需要 JSESSIONID，或用 --ws")

    hosts = [args.host] if args.host else LOBBY_HOSTS
    print(f"[HTTP 大厅] 试 {len(hosts)} 个 host…")
    host, body = fetch_http_lobby(args.sid, hosts)
    if not body:
        print("\n× 所有 host 都没拿到数据（session 无效 / 已过期）")
        return
    OUT_RAW.write_text(body, encoding="utf-8")
    print(f"\n命中 host = {host}，原文 {len(body)} 字节 → {OUT_RAW.name}")
    recs = extract(body)
    if not recs:
        print("\n⚠️ 拿到响应但没抽出桌子 —— 结构可能和我猜的不一样，")
        print(f"   先看 {OUT_RAW.name} 的实际结构（文件里是原文，不是我猜的）。")
        return
    save(recs)


if __name__ == "__main__":
    sys.exit(main())
