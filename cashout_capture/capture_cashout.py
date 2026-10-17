# -*- coding: utf-8 -*-
"""
PP CashOut 抓包器（旁观模式：不下注、不坐下、不干扰牌局）

用途：
  收集「玩家手牌 + 庄家明牌 + 服务器 cashOut 开价」样本，
  给老板拟合 PP 兑现定价规则用（PDEV 算上 ECO 的前置条件）。

用法：
  1. 双击 start.bat（或 python capture_cashout.py）
  2. 粘贴完整 wss URL（跟主程序一样，必须含 JSESSIONID= 和 tableId=）
  3. 挂机旁观：桌上任何玩家的 decisionOffer 带 cashOut 都会被记下
  4. Ctrl+C 结束；产物在 samples/ 目录：
     · raw_<时间>.jsonl     —— 全部原始帧（完整牌账，离线拟合用）
     · samples_<时间>.csv   —— 解析好的样本行（Excel 可直接开）

⚠ 硬约束：同一 JSESSIONID 只能一处连接。
   主程序正在跑的话，请先停主程序，或用另一个会话的 URL。
"""

import asyncio
import csv
import json
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
import websockets

try:
    import certifi
    _WSS_SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _WSS_SSL = ssl.create_default_context()

HERE = Path(__file__).resolve().parent
SAMPLES_DIR = HERE / "samples"
TS = datetime.now().strftime("%Y%m%d_%H%M%S")

CARD_MAP = {"0": "10", "1": "A"}   # scanCode 首字符 → 点数（与主程序一致）

CSV_FIELDS = ["time", "game_id", "seat", "hand", "cards", "score",
              "dealer_up", "cash_out", "bet", "offer_id"]


def log(msg: str):
    print(msg, flush=True)


# ═══════════════════════ 启动输入 ═══════════════════════
def ask_url():
    """读入完整 wss URL，取出 JSESSIONID 与 tableId（与主程序同款交互）"""
    print("  Paste the full wss URL:")
    while True:
        try:
            u = input("  URL: ").strip()
        except EOFError:
            raise RuntimeError("no URL entered")
        if "JSESSIONID=" not in u or "tableId=" not in u:
            print("  URL must contain JSESSIONID= and tableId= — paste again:")
            continue
        q = parse_qs(urlparse(u).query)
        sid = q.get("JSESSIONID", [""])[0]
        tid = q.get("tableId", [""])[0]
        if not sid or not tid:
            print("  Could not parse JSESSIONID / tableId — paste again:")
            continue
        log(f"  [✅] JSESSIONID: {sid}")
        log(f"  [🎯] tableId: {tid}")
        return sid, tid


def build_ws_url(sid: str, tid: str) -> str:
    """tableConfig 换真实 ws 地址（与主程序 build_ws_url 同源）"""
    url = ("https://games.pragmaticplaylive.net/api/ui/tableConfig"
           f"?JSESSIONID={sid}&tableId={tid}&operatorNotification=true")
    resp = requests.get(url, timeout=8, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    resp.raise_for_status()
    params = resp.json().get("params") or {}
    ws_address = params.get("ws_address")
    if not ws_address:
        raise RuntimeError("no ws_address in tableConfig response")
    return f"{ws_address}?JSESSIONID={sid}&tableId={tid}&type=json&version=3"


# ═══════════════════════ 解析 ═══════════════════════
def hand_cards(hand: dict) -> list:
    out = []
    for c in (hand.get("cards") or []):
        code = c.get("scanCode", "") if isinstance(c, dict) else ""
        if code:
            out.append(CARD_MAP.get(code[0], code[0]))
    return out


def extract_samples(d: dict, dealer_up: dict, seen_offers: set) -> list:
    """从一帧里抽出所有带 cashOut 的手 → 样本行列表（同一条 offer 只记一次）"""
    rows = []
    ps = d.get("playerSeat")
    if not isinstance(ps, dict):
        return rows
    gid = str(ps.get("gameId") or "")
    seat = ps.get("seatNumber")
    if seat == -1:
        return rows          # 庄家帧没有 cashOut
    for hk in sorted(k for k in ps if k.startswith("h") and len(k) == 2):
        hand = ps.get(hk)
        if not isinstance(hand, dict):
            continue
        offer = hand.get("decisionOffer")
        if not isinstance(offer, dict):
            continue
        co = offer.get("cashOut")
        if not isinstance(co, (int, float)):
            continue
        oid = str(offer.get("id") or f"{gid}/{seat}/{hk}/{co}")
        if oid in seen_offers:
            continue
        seen_offers.add(oid)
        rows.append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "game_id": gid,
            "seat": seat,
            "hand": hk,
            "cards": " ".join(hand_cards(hand)),
            "score": hand.get("score"),
            "dealer_up": dealer_up.get(gid, ""),
            "cash_out": co,
            "bet": hand.get("bet"),
            "offer_id": oid,
        })
    return rows


# ═══════════════════════ 监听 ═══════════════════════
async def heartbeat(ws, get_channel):
    while True:
        await asyncio.sleep(10)
        try:
            await ws.send(f'<ping channel="{get_channel()}" '
                          f'time="{int(time.time() * 1000)}"/>')
        except Exception:
            return


async def listen(url: str, raw_path: Path, csv_path: Path):
    """连上桌、订阅确认后全程录帧；返回 (帧数, 样本数)"""
    seen_offers = set()
    dealer_up = {}            # game_id → 庄家明牌点数
    ch = {"v": ""}
    stats = {"raw": 0, "sample": 0}

    raw_f = open(raw_path, "a", encoding="utf-8")
    csv_f = open(csv_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(csv_f, fieldnames=CSV_FIELDS)
    writer.writeheader()

    async with websockets.connect(
            url, ssl=_WSS_SSL if url.startswith("wss") else None) as ws:
        hb_task = asyncio.create_task(heartbeat(ws, lambda: ch["v"]))
        log("  [✅] Connected — recording... (Ctrl+C to stop)")
        try:
            async for raw in ws:
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(d, dict):
                    continue

                # subscribe 确认 → 记 channel 供心跳
                if not ch["v"] and "subscribe" in d:
                    ch["v"] = (d.get("subscribe") or {}).get("channel") or ch["v"]
                    log(f"  [🔗] subscribe confirmed, channel={ch['v']}")
                    continue

                # 庄家帧 → 记明牌（game_id 对齐）
                ps = d.get("playerSeat")
                if isinstance(ps, dict) and ps.get("seatNumber") == -1:
                    h0 = ps.get("h0") or {}
                    cs = hand_cards(h0)
                    gid = str(ps.get("gameId") or "")
                    if cs and gid:
                        dealer_up[gid] = cs[0]

                # 全量原始帧 → jsonl（离线拟合要完整牌账）
                raw_f.write(json.dumps(
                    {"recv": datetime.now().isoformat(timespec="milliseconds"),
                     "msg": d}, ensure_ascii=False) + "\n")
                raw_f.flush()
                stats["raw"] += 1

                # cashOut 样本 → csv
                for row in extract_samples(d, dealer_up, seen_offers):
                    writer.writerow(row)
                    csv_f.flush()
                    stats["sample"] += 1
                    log(f"  [💰] SAMPLE #{stats['sample']} | "
                        f"game …{row['game_id'][-6:]} seat {row['seat']} {row['hand']} | "
                        f"cards [ {row['cards']} ] | up {row['dealer_up']} | "
                        f"cashOut {row['cash_out']} | bet {row['bet']}")

                if stats["raw"] % 500 == 0 and stats["raw"]:
                    log(f"  [··] {stats['raw']} frames | {stats['sample']} cashout samples")
        finally:
            hb_task.cancel()
            raw_f.close()
            csv_f.close()
    return stats["raw"], stats["sample"]


# ═══════════════════════ 主入口 ═══════════════════════
def main():
    print("  ───────────────────────────────────────────────")
    print("  PP CashOut Capture (observe-only · no bet · no sit)")
    print("  ───────────────────────────────────────────────")
    print("  [⚠] 同一 JSESSIONID 只能一处连接 —")
    print("      主程序在跑的话请先停掉，或用另一个会话的 URL。")

    sid, tid = ask_url()
    SAMPLES_DIR.mkdir(exist_ok=True)
    raw_path = SAMPLES_DIR / f"raw_{TS}.jsonl"
    csv_path = SAMPLES_DIR / f"samples_{TS}.csv"

    try:
        ws_url = build_ws_url(sid, tid)
    except Exception as e:
        log(f"  [❌] tableConfig failed: {e}")
        log("      JSESSIONID 可能已过期 — 重新粘贴一个新 URL。")
        sys.exit(1)

    log(f"  [📁] raw   → {raw_path}")
    log(f"  [📁] csv   → {csv_path}")
    log("  [ℹ] 桌上下注的玩家越多，cashOut 样本来得越快；")
    log("      长时间 0 样本 = cashOut 可能只推给下注玩家本人，届时需加下注模式。")

    while True:
        try:
            raw_n, smp_n = asyncio.run(listen(ws_url, raw_path, csv_path))
            log(f"  [⚠] Connection closed ({raw_n} frames, {smp_n} samples) "
                f"— reconnecting in 5s…")
            time.sleep(5)
        except KeyboardInterrupt:
            log(f"  [🛑] Stopped by user. frames saved in {raw_path.name}, "
                f"samples in {csv_path.name}")
            break
        except Exception as e:
            log(f"  [❌] {e.__class__.__name__}: {e}")
            log("      JSESSIONID 可能已过期 — Ctrl+C 退出后重新粘贴新 URL。")
            log("      5 秒后重试…")
            time.sleep(5)


if __name__ == "__main__":
    main()
