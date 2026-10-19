# -*- coding: utf-8 -*-
"""线上 PDEV 是否含 ECO —— 实时抓 feed 对账

做法：
  1. 订阅 ws://pp.knewb2u.com:8888/ws，收 pp/table/<桌名> 帧
  2. 帧里带 cards（整靴已出牌）和 pdev（VPS 算出来推上来的值）
  3. 本地用同一份 pdev_local.py 各算一次：
        A = 含 ECO（现在的代码）
        B = 不含 ECO（把 eco_multiple 打回 None）
  4. 对比线上 pdev 落在哪一边

判定：
  线上 == A 且 A != B  → VPS 已生效
  线上 == B 且 A != B  → VPS 还是旧版（没换文件或没重启）
  A == B              → 这一帧看不出差别（满靴/牌厚，cashout 没胜出），换下一帧
"""
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

import websockets

FEED_URL = "ws://pp.knewb2u.com:8888/ws"
PDEV_PATH = r"D:\MyProject\Pragmatic Play Blackjack\AutoBot\pdev_local.py"

MAX_FRAMES = 60          # 最多对账多少帧（去重后）
MIN_CARDS = 20           # 只看已出牌 >= 20 张的帧（越厚越容易看出差别）
TIMEOUT = 240            # 总体超时（秒）

spec = importlib.util.spec_from_file_location("pdev_local", PDEV_PATH)
pdev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pdev)

_orig_eco = pdev.eco_multiple


def _eco_off(i, j, total, soft, up):
    return None


def compute(cards, with_eco):
    pdev.eco_multiple = _orig_eco if with_eco else _eco_off
    try:
        # 必须关缓存：否则第二次调用直接命中第一次的结果，永远“无差别”
        return pdev.compute_pdev(cards, use_cache=False)["pdev"]
    finally:
        pdev.eco_multiple = _orig_eco


def log(m):
    print(m, flush=True)


async def main():
    seen = set()
    n_same = 0
    n_checked = 0
    verdicts = []
    t0 = time.monotonic()

    log(f"  feed : {FEED_URL}")
    log(f"  引擎 : {PDEV_PATH}")
    log(f"  只看已出牌 >= {MIN_CARDS} 张的帧，最多对账 {MAX_FRAMES} 帧\n")

    async with websockets.connect(FEED_URL, max_size=8 * 1024 * 1024) as ws:
        log("  [OK] feed 已连上，等桌面推帧…\n")
        while True:
            if n_checked >= MAX_FRAMES:
                break
            if time.monotonic() - t0 > TIMEOUT:
                log("  [超时] 到时退出")
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                continue

            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") != "mqtt_msg":
                continue
            env = msg.get("payload") or {}
            topic = env.get("topic") or ""
            if not topic.startswith("pp/table/"):
                continue
            body = env.get("payload")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception:
                    continue
            if not isinstance(body, dict):
                continue

            cards = body.get("cards") or []
            pub = body.get("pdev")
            total = len(cards)
            if pub is None or total < MIN_CARDS:
                continue

            key = (total, tuple(cards))
            if key in seen:
                continue
            seen.add(key)

            a = compute(cards, True)
            b = compute(cards, False)
            if abs(a - b) < 1e-9:
                n_same += 1
                log(f"  [无差别] {topic[9:]:<22s} 已出{total:>3d}张  含/不含 = {a:+.6f}（cashout 未胜出）")
                continue

            n_checked += 1
            hit_a = abs(pub - a) < 2e-6
            hit_b = abs(pub - b) < 2e-6
            tag = "含ECO" if hit_a else ("不含ECO" if hit_b else "都对不上")
            verdicts.append(tag)
            log(f"  [对账] {topic[9:]:<22s} 已出{total:>3d}张 | 线上 {pub:+.6f} | 含 {a:+.6f} | 不含 {b:+.6f} → {tag}")

    log("\n" + "=" * 70)
    if not verdicts:
        log(f"  结论：无法判定 —— 收到 {n_same} 帧，全部是「含/不含」无差别的情况")
        log("  （说明这些帧里 cashout 一次都没胜出，看不出 VPS 有没有换。多等几帧或等桌打薄）")
    else:
        from collections import Counter
        c = Counter(verdicts)
        log(f"  对账帧数 {len(verdicts)} | " + " / ".join(f"{k} {v}" for k, v in c.items()))
        if c["含ECO"] == len(verdicts):
            log("  ✅ 结论：线上 PDEV 已经包含 ECO")
        elif c["不含ECO"] == len(verdicts):
            log("  ❌ 结论：线上 PDEV 仍是不含 ECO 的旧版（文件没换，或换了但没重启 PPBJ）")
        else:
            log("  ⚠ 结论：混合 —— 可能中途重启过；看上面的时间顺序")
    log("=" * 70)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
