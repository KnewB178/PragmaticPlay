# -*- coding: utf-8 -*-
"""
PP CashOut 抓包器（feed 模式 · 零凭据 · 零干扰）

数据源：直接订阅现成 feed  ws://pp.knewb2u.com:8888/ws
  · pp/decision/<桌名>  —— decisionOffer 触发帧（cashOut 开价，PPBJ.py:440-450 推的）
  · pp/table/<桌名>     —— 桌面快照（庄家牌 / 注金 / 整靴已出牌 / TC / PDEV）

不需要粘贴 wss URL、不需要 JSESSIONID，跟主程序同时跑也不冲突。

产出（samples/ 目录）：
  · raw_<时间>.jsonl     —— 全部原始帧（离线拟合用）
  · samples_<时间>.csv   —— 已对齐快照的样本行（Excel 可直接开）：
       time, table, seat, hand, score, cards, dealer_up,
       cash_out, bet, cash_out_ratio, tc, pdev, issued
    其中 cash_out_ratio = cashOut ÷ bet（拟合定价规则的核心列）

用法：双击 start.bat（或 python capture_cashout.py）→ Ctrl+C 结束。
"""

import asyncio
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import websockets

FEED_URL = "ws://pp.knewb2u.com:8888/ws"

HERE = Path(__file__).resolve().parent
SAMPLES_DIR = HERE / "samples"
TS = datetime.now().strftime("%Y%m%d_%H%M%S")

CARD_MAP = {"0": "10", "1": "A"}   # scanCode 首字符 → 点数（与主程序一致）

CSV_FIELDS = ["time", "table", "seat", "hand", "score", "cards",
              "dealer_up", "cash_out", "bet", "cash_out_ratio",
              "cards_match", "bet_suspect", "tc", "pdev", "issued"]


def to_num(v):
    """'2,500' / '2500' / 2500 → float；其余 → None"""
    try:
        f = float(str(v).replace(",", "").strip())
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def log(msg: str):
    print(msg, flush=True)


def conv_cards(codes) -> str:
    """pp/decision 的 cards 是 scanCode 首字符（'0'=10，'1'=A）→ 转成点数串"""
    return " ".join(CARD_MAP.get(str(c)[:1], str(c)[:1]) for c in (codes or []))


async def heartbeat(ws):
    """feed 是服务端单向推，不需要 ping；留个空任务位以防协议变化"""
    while True:
        await asyncio.sleep(60)


async def listen(feed_url: str, raw_path: Path, csv_path: Path):
    snapshots = {}            # 桌名 → 最新 pp/table 快照
    seen = {}                 # (桌名, 座位, 手, 牌, cashOut) → 首次出现时间（同一条 offer 会重推）
    DEDUPE_WINDOW = 300       # 同一手同一价 5 分钟内只记一次（一个回合不会超过它）
    stats = {"raw": 0, "sample": 0, "tables": set()}
    last_report = time.monotonic()

    raw_f = open(raw_path, "a", encoding="utf-8")
    csv_f = open(csv_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(csv_f, fieldnames=CSV_FIELDS)
    writer.writeheader()

    async with websockets.connect(feed_url) as ws:
        hb = asyncio.create_task(heartbeat(ws))
        log("  [✅] Feed connected — recording... (Ctrl+C to stop)")
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if m.get("type") != "mqtt_msg":
                    continue
                p = m.get("payload") or {}
                topic = p.get("topic", "")
                body = p.get("payload")

                # ── 桌面快照 → 留最新一份（对齐庄家/注金/整靴用）──
                if topic.startswith("pp/table/") and isinstance(body, dict):
                    name = topic[len("pp/table/"):]
                    snapshots[name] = body
                    stats["tables"].add(name)

                # ── 决策帧 → cashOut 样本 ──
                elif topic.startswith("pp/decision/") and isinstance(body, dict):
                    name = topic[len("pp/decision/"):]
                    co = body.get("cashOut")
                    if not isinstance(co, (int, float)):
                        continue
                    seat = body.get("seatNumber")
                    hand = body.get("hand", "")
                    # 同一条 offer 会被服务器/中继重复推，按内容去重（不看时间戳）
                    dedupe_key = (name, seat, hand,
                                  tuple(str(c) for c in (body.get("cards") or [])), co)
                    now_m = time.monotonic()
                    if now_m - seen.get(dedupe_key, -1e9) < DEDUPE_WINDOW:
                        continue
                    seen[dedupe_key] = now_m
                    t = p.get("time") or datetime.now().strftime("%H:%M:%S")

                    # 对齐最新快照：庄家明牌 / 注金 / 整靴账本
                    snap = snapshots.get(name) or {}
                    dealer_cards = ((snap.get("dealer") or {}).get("cards")) or []
                    dealer_up = dealer_cards[0] if dealer_cards else ""
                    players = snap.get("players") or {}
                    join = f"{(seat + 1) if isinstance(seat, int) else ''}_{hand}"
                    mine = players.get(join) or {}
                    bet = to_num(mine.get("bet"))

                    # 手牌核对：快照里这个座位的牌必须跟 decision 帧一致，
                    # 不一致说明快照滞后 → 这行的 bet / dealer_up 不可信
                    snap_cards = [str(c) for c in (mine.get("cards") or [])]
                    dec_cards = [CARD_MAP.get(str(c)[:1], str(c)[:1])
                                 for c in (body.get("cards") or [])]
                    cards_match = (snap_cards == dec_cards) and bool(dec_cards)

                    ratio = ""
                    if bet:
                        ratio = round(co / bet, 4)
                    # 兑现倍数物理范围约 0.2 ~ 2：超出说明 bet 是旧值/占位值
                    bet_suspect = ""
                    if ratio != "" and not (0.05 <= ratio <= 2.5):
                        bet_suspect = 1

                    row = {
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "table": name,
                        "seat": seat,
                        "hand": hand,
                        "score": body.get("score"),
                        "cards": conv_cards(body.get("cards")),
                        "dealer_up": dealer_up,
                        "cash_out": co,
                        "bet": bet if bet is not None else "",
                        "cash_out_ratio": ratio,
                        "cards_match": 1 if cards_match else 0,
                        "bet_suspect": bet_suspect,
                        "tc": snap.get("tc", ""),
                        "pdev": snap.get("pdev", ""),
                        "issued": len(snap.get("cards") or []),
                    }
                    writer.writerow(row)
                    csv_f.flush()
                    stats["sample"] += 1
                    log(f"  [💰] SAMPLE #{stats['sample']} | {name} seat {seat} {hand} "
                        f"| [ {row['cards']} ] {row['score']} | up {dealer_up} "
                        f"| cashOut {co} ÷ bet {bet if bet is not None else '?'} "
                        f"= {ratio if ratio != '' else '?'}"
                        f"{' ⚠bet可疑' if bet_suspect else ''}")

                # 全量原始帧（两个 topic 都录）
                raw_f.write(json.dumps(
                    {"recv": datetime.now().isoformat(timespec="milliseconds"),
                     "topic": topic, "msg": m}, ensure_ascii=False) + "\n")
                raw_f.flush()
                stats["raw"] += 1

                if time.monotonic() - last_report >= 60:
                    last_report = time.monotonic()
                    log(f"  [··] {stats['raw']} frames | {stats['sample']} samples "
                        f"| {len(stats['tables'])} tables live")
        finally:
            hb.cancel()
            raw_f.close()
            csv_f.close()
    return stats


def main():
    feed_url = sys.argv[1] if len(sys.argv) > 1 else FEED_URL
    print("  ───────────────────────────────────────────────")
    print("  PP CashOut Capture (feed mode · no login · no bet)")
    print(f"  feed: {feed_url}")
    print("  ───────────────────────────────────────────────")

    SAMPLES_DIR.mkdir(exist_ok=True)
    raw_path = SAMPLES_DIR / f"raw_{TS}.jsonl"
    csv_path = SAMPLES_DIR / f"samples_{TS}.csv"
    log(f"  [📁] raw   → {raw_path}")
    log(f"  [📁] csv   → {csv_path}")

    while True:
        try:
            stats = asyncio.run(listen(feed_url, raw_path, csv_path))
            log(f"  [⚠] Feed closed ({stats['raw']} frames, {stats['sample']} samples) "
                f"— reconnecting in 3s…")
            time.sleep(3)
        except KeyboardInterrupt:
            log(f"  [🛑] Stopped by user. frames in {raw_path.name}, "
                f"samples in {csv_path.name}")
            break
        except Exception as e:
            log(f"  [❌] {e.__class__.__name__}: {e} — reconnecting in 3s…")
            time.sleep(3)


if __name__ == "__main__":
    main()
