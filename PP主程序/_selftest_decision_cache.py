# -*- coding: utf-8 -*-
"""验证：顺序轮流下，决策提示该亮就亮、做完就灭（先只管一个座位）

场景（我只坐 1 号位）：
  1) 轮到 1 号         → 1 号亮
  2) 1 号做完，轮到 2 号（别人） → 1 号立刻灭
  3) 再轮到 3 号（别人）→ 还是灭
  4) 下一局又轮到我    → 再亮
  5) 我做牌途中收到没有 offer 的帧（发牌帧）→ 保持不变（不能误灭）
"""
decision_cache = {}

def now_turn(ps_data):
    raw_seat = ps_data.get("seatNumber")
    if raw_seat is None:
        return set()
    mapped = "0" if raw_seat == -1 else str(raw_seat + 1)
    out = set()
    for hk, hv in ps_data.items():
        if not (hk.startswith("h") and isinstance(hv, dict)):
            continue
        if isinstance(hv.get("decisionOffer"), dict):
            out.add(f"{mapped}_{hk}")
    return out

def on_frame(table, ps_data):
    turn = now_turn(ps_data)
    raw_seat = ps_data.get("seatNumber")
    if raw_seat is None:
        return
    mapped = "0" if raw_seat == -1 else str(raw_seat + 1)
    dc = decision_cache.setdefault(table, {})
    for flat in turn:
        dc.setdefault(flat, {})["canDecide"] = True
    for hk, hv in ps_data.items():
        if not (hk.startswith("h") and isinstance(hv, dict)):
            continue
        do = hv.get("decisionOffer")
        if isinstance(do, dict) and do.get("cashOut") is not None:
            dc.setdefault(f"{mapped}_{hk}", {})["cashOut"] = do["cashOut"]
    for hk, hv in ps_data.items():
        if not (hk.startswith("h") and isinstance(hv, dict)):
            continue
        if hv.get("currentDecision"):
            st = dc.get(f"{mapped}_{hk}")
            if isinstance(st, dict):
                st.pop("canDecide", None)
                st.pop("cashOut", None)
    if turn:
        for k, st in dc.items():
            if k not in turn:
                st.pop("canDecide", None)
                st.pop("cashOut", None)

def lit():
    return sorted(k for k, v in decision_cache.get("T", {}).items() if v.get("canDecide"))

C = lambda *a: [{"scanCode": x} for x in a]

# 1) 轮到我（1号）
on_frame("T", {"seatNumber": 0, "gameId": "G1",
               "h0": {"score": "16", "cards": C("05", "13"), "decisionOffer": {"canDouble": True}}})
print("1) 轮到我(1号):", lit(), " 期望 ['1_h0']")
assert lit() == ["1_h0"]

# 2) 我做完，轮到 2 号（别人）
on_frame("T", {"seatNumber": 1, "gameId": "G1",
               "h0": {"score": "20", "cards": C("00", "11"), "decisionOffer": {"cashOut": 50.0}}})
print("2) 轮到2号:", lit(), " 期望 ['2_h0']  ← 我的 1_h0 已熄灭")
assert "1_h0" not in lit(), "★ 我做完后 1_h0 必须熄灭"
assert lit() == ["2_h0"]

# 3) 轮到 3 号（别人）
on_frame("T", {"seatNumber": 2, "gameId": "G1",
               "h0": {"score": "15", "cards": C("06", "09"), "decisionOffer": {"cashOut": 33.0}}})
print("3) 轮到3号:", lit(), " 期望 ['3_h0']")
assert lit() == ["3_h0"]

# 4) 发牌帧（没 offer）→ 不能误灭当前亮着的
before = lit()
on_frame("T", {"seatNumber": 2, "gameId": "G1",
               "h0": {"score": "18", "cards": C("06", "09", "03")}})
print("4) 收到无offer的帧:", lit(), f" 期望保持 {before}")
assert lit() == before, "没有 offer 的帧不该改变现状"

# 5) 新一局又轮到我
clear = decision_cache["T"] = {}
on_frame("T", {"seatNumber": 0, "gameId": "G2",
               "h0": {"score": "12", "cards": C("04", "08"), "decisionOffer": {"cashOut": 12.0}}})
print("5) 新局轮到我:", lit(), " 期望 ['1_h0']")
assert lit() == ["1_h0"]

# 6) 我又做完 → 轮到庄家（seatNumber -1 带 offer 的情况）
on_frame("T", {"seatNumber": 0, "gameId": "G2",
               "h0": {"score": "19", "cards": C("04", "08", "07"), "decisionOffer": {"cashOut": 19.0}}})
print("6) 同一手继续轮到我(仍有offer):", lit(), " 期望 ['1_h0']")
assert lit() == ["1_h0"]

on_frame("T", {"seatNumber": -1, "gameId": "G2",
               "h0": {"score": "17", "cards": C("10", "07")}})
print("   庄家帧(无offer):", lit(), " 期望 ['1_h0']（不误灭）")
assert lit() == ["1_h0"]

# 7) ★ 我提交了决策（currentDecision 有值）→ 提示必须立刻消失，不等别人
decision_cache["T"] = {}
on_frame("T", {"seatNumber": 0, "gameId": "G3",
               "h0": {"score": "13", "cards": C("05", "08"), "decisionOffer": {"cashOut": 13.0}}})
print("7a) 轮到我:", lit(), " 期望 ['1_h0']")
assert lit() == ["1_h0"]

on_frame("T", {"seatNumber": 0, "gameId": "G3",
               "h0": {"score": "13", "cards": C("05", "08"), "currentDecision": "HIT"}})
print("7b) 我按了 HIT(无offer):", lit(), " 期望 [] ← 提交即灭")
assert lit() == [], "★ 玩家一提交决策，提示必须立刻消失"

on_frame("T", {"seatNumber": 0, "gameId": "G3",
               "h0": {"score": "13", "cards": C("05", "08", "02"), "currentDecision": "HIT"}})
print("7c) 补牌后仍带 currentDecision:", lit(), " 期望 []（不乱亮）")
assert lit() == []

on_frame("T", {"seatNumber": 0, "gameId": "G3",
               "h0": {"score": "15", "cards": C("05", "08", "02"), "decisionOffer": {}}})
print("7d) 再次轮到我(新offer):", lit(), " 期望 ['1_h0']")
assert lit() == ["1_h0"], "收到下一条可决策要重新亮"

print("\n✅ 单座位：做完立刻灭；提交决策立刻灭；再轮到时重新亮；不会被无 offer 的帧误灭")
