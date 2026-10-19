"""多座位逻辑离线自检：用假 WebSocket 跑真实 listen_table，验证
   PDEV > 0 → 最多 3 个位；PDEV <= 0 → 退回 1 个位；下注按座位各发一次。
   不连服务器、不登录、不产生任何真实请求。
"""
import asyncio
import json
import re
import sys

import main


class FakeWS:
    def __init__(self):
        self.sent = []
        self.q = asyncio.Queue()

    async def send(self, s):
        self.sent.append(s)

    def push(self, obj):
        self.q.put_nowait(json.dumps(obj))

    def stop(self):
        self.q.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.q.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        self.stop()


def sitdowns(ws):
    return [int(m.group(1)) for s in ws.sent
            for m in [re.search(r'<sitdown[^>]*seatNum="(\d+)"', s)] if m]


def unseats(ws):
    return [int(m.group(1)) for s in ws.sent
            for m in [re.search(r'<unseat[^>]*seatNum="(\d+)"', s)] if m]


def bets(ws):
    return [int(m.group(1)) for s in ws.sent
            for m in [re.search(r'<bet seat="(\d+)"', s)] if m]


def decisions(ws):
    """→ [(座位号, 手号)]"""
    return [(m.group(1), m.group(2)) for s in ws.sent
            for m in [re.search(r'<decision[^>]*seatNum="(\d+)"[^>]*hand="(\d)"', s)] if m]


def card(code):
    """伪造一张牌：scanCode 首字符 0→10 / 1→A / 其余按面值"""
    return {"scanCode": code}


def confirm(ws, gid, seats):
    """模拟这些座位本局拿到牌（= 下注被平台接受）"""
    for s in seats:
        ws.push({"playerSeat": {"seatNumber": s, "gameId": gid,
                                "h0": {"cards": [card("5H"), card("7S")], "score": 12}}})


async def run():
    ws = FakeWS()
    main.websockets.connect = lambda *a, **k: _Conn(ws)
    main.BET_DELAY_MIN = main.BET_DELAY_MAX = 0.2
    main.SIT_CONFIRM_TIMEOUT = 3
    main.UI["active"] = False
    main.SEAT_MODE["v"] = main.SEAT_MODE_PDEV

    pdev = {"v": 0.02}
    main.update_pdev = lambda cards: main.UI.update({"pdev": pdev["v"], "pdev_issued": "test"})

    # 打薄牌堆当基线（正 PDEV 场景由 patch 控制，这里只保证 taken 立即成立）
    carry = {"cards": ["2"] * 40, "tc": 2.0, "seat": None}

    task = asyncio.create_task(main.listen_table("TEST", "wss://x/y", 10, 10, carry))

    ws.push({"subscribe": {"channel": "table-test"}})
    await asyncio.sleep(3.4)                       # initial_report 等 3 秒后坐下
    first = sitdowns(ws)
    print("① 初次坐下请求:", first)
    assert len(first) == 1, "初次应只坐 1 个位"

    seat0 = str(first[0])
    ws.push({"seat": {"num": int(seat0), "event": "sit", "user_id": "U1", "screen_name": "me"}})
    await asyncio.sleep(0.2)

    # ── 正 PDEV：应补到 3 个位 ──
    ws.push({"betsopen": {"game": "g1"}})
    await asyncio.sleep(0.8)
    sd = sitdowns(ws)
    print("② PDEV +0.02 → sitdown 累计:", sd)
    assert len(sd) == 3, f"正 PDEV 应坐满 3 个位，实际 {len(sd)}"
    new_seats = [s for s in sd if s != int(seat0)]
    for s in new_seats:
        ws.push({"seat": {"num": s, "event": "sit", "user_id": "U1", "screen_name": "me"}})
    await asyncio.sleep(0.3)
    await asyncio.sleep(0.5)
    print("③ 本局下注座位:", bets(ws))
    assert len(bets(ws)) == 3, f"3 个位应各下 1 注，实际 {bets(ws)}"
    confirm(ws, "g1", [int(seat0)] + new_seats)
    await asyncio.sleep(0.3)

    # ── 转负 PDEV：应退到 1 个位 ──
    pdev["v"] = -0.01
    main.UI["pdev"] = -0.01
    ws.push({"betsopen": {"game": "g2"}})
    await asyncio.sleep(1.0)
    us = unseats(ws)
    print("④ PDEV -0.01 → unseat:", us)
    assert len(us) == 2, f"应退掉 2 个位，实际 {len(us)}"
    remaining = sorted(set(sitdowns(ws)) - set(us))
    print("   剩下座位:", remaining)
    assert len(remaining) == 1, "应只剩 1 个位"
    confirm(ws, "g2", remaining)
    await asyncio.sleep(0.3)

    ws.push({"betsopen": {"game": "g3"}})
    await asyncio.sleep(1.0)
    print("⑤ 退位后本局下注座位:", bets(ws)[-1:])
    assert len(bets(ws)) == 5, f"3+1+1 共应下注 5 次，实际 {len(bets(ws))}"
    assert len(bets(ws)[-1:]) == 1 and bets(ws)[-1] == bets(ws)[-2], \
        "退位后每局只应对剩下的那个座位下注"
    confirm(ws, "g3", remaining)

    ws.stop()
    await asyncio.wait_for(task, timeout=5)
    print("\n✅ 全部通过")


async def run_fixed():
    """座位模式 1（永久 1 个位）：PDEV 正数也不补位、不退位、只下 1 注"""
    ws = FakeWS()
    main.websockets.connect = lambda *a, **k: _Conn(ws)
    main.SEAT_MODE["v"] = main.SEAT_MODE_FIXED
    main.UI["pdev"] = 0.02
    main.update_pdev = lambda cards: main.UI.update({"pdev": 0.02, "pdev_issued": "test"})
    carry = {"cards": ["2"] * 40, "tc": 2.0, "seat": None}

    task = asyncio.create_task(main.listen_table("TEST2", "wss://x/y", 10, 10, carry))
    ws.push({"subscribe": {"channel": "table-test2"}})
    await asyncio.sleep(3.4)
    first = sitdowns(ws)
    print("⑥ [fixed 1 seat] 初次坐下请求:", first)
    assert len(first) == 1, "初次应只坐 1 个位"
    ws.push({"seat": {"num": int(first[0]), "event": "sit", "user_id": "U1", "screen_name": "me"}})
    await asyncio.sleep(0.2)

    ws.push({"betsopen": {"game": "f1"}})
    await asyncio.sleep(1.0)
    print("⑦ [fixed 1 seat] PDEV +0.02 → sitdown / unseat:", sitdowns(ws), unseats(ws))
    assert len(sitdowns(ws)) == 1, f"固定模式不该补位，实际 {sitdowns(ws)}"
    assert not unseats(ws), f"固定模式不该退位，实际 {unseats(ws)}"
    print("⑧ [fixed 1 seat] 本局下注座位:", bets(ws))
    assert len(bets(ws)) == 1, f"固定模式每局只应下 1 注，实际 {bets(ws)}"

    ws.stop()
    await asyncio.wait_for(task, timeout=5)
    print("\n✅ 全部通过")


async def run_multi_decision():
    """三座位同一局同时收到 decisionOffer（三个 id 完全相同）→ 必须各发一条 decision"""
    ws = FakeWS()
    main.websockets.connect = lambda *a, **k: _Conn(ws)
    main.SEAT_MODE["v"] = main.SEAT_MODE_PDEV
    main.BET_DELAY_MIN = main.BET_DELAY_MAX = 0.2
    main.ACTION_DELAY_MIN = main.ACTION_DELAY_MAX = 0.15
    main.UI["active"] = False
    main.UI["pdev"] = 0.02
    main.update_pdev = lambda cards: main.UI.update({"pdev": 0.02, "pdev_issued": "test"})
    main.choose_action = lambda *a, **k: ("h", [{"decision": "Hit", "ev": 0.01}])
    carry = {"cards": ["2"] * 40, "tc": 2.0, "seat": None}

    task = asyncio.create_task(main.listen_table("TEST3", "wss://x/y", 10, 10, carry))
    ws.push({"subscribe": {"channel": "table-test3"}})
    await asyncio.sleep(3.4)
    first = sitdowns(ws)[0]
    ws.push({"seat": {"num": first, "event": "sit", "user_id": "U1", "screen_name": "me"}})
    await asyncio.sleep(0.2)
    ws.push({"betsopen": {"game": "m1"}})
    await asyncio.sleep(0.8)
    seats = sitdowns(ws)
    for s in seats:
        ws.push({"seat": {"num": s, "event": "sit", "user_id": "U1", "screen_name": "me"}})
    await asyncio.sleep(0.3)
    ws.push({"playerSeat": {"seatNumber": -1, "gameId": "m1",
                            "h0": {"cards": [card("5H")], "score": 5}}})
    await asyncio.sleep(0.1)
    for s in seats:
        ws.push({"playerSeat": {"seatNumber": s, "gameId": "m1",
                                "h0": {"cards": [card("5H"), card("7S")], "score": 12,
                                       "activeHand": True,
                                       "decisionOffer": {"id": "OFFER-1",
                                                         "canDouble": True,
                                                         "canSplit": False}}}})
        await asyncio.sleep(0.05)

    await asyncio.sleep(1.2)
    got = decisions(ws)
    print("⑨ 三座位（offer id 完全相同）收到的 decision:", got)
    assert len(got) == 3, f"三个座位应各发一条 decision，实际 {len(got)}"
    assert sorted(s for s, _ in got) == sorted(str(s) for s in seats), \
        f"座位号应各一条，实际 {got}"

    ws.stop()
    await asyncio.wait_for(task, timeout=5)
    print("\n✅ 全部通过")


class _Conn:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *a):
        return False


if __name__ == "__main__":
    try:
        asyncio.run(run())
        asyncio.run(run_fixed())
        asyncio.run(run_multi_decision())
    except AssertionError as e:
        print("\n❌", e)
        sys.exit(1)
