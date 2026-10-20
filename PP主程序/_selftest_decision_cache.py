# -*- coding: utf-8 -*-
"""验证：新决策缓存逻辑在两个座位交替帧下，能否同时保留两边的 canDecide/cashOut"""
import sys, types, json

# 只测纯逻辑：直接复刻 publish_pp_update 里的 decision 组装 + 缓存更新
decision_cache = {}

def on_player_seat(table_name, ps_data):
    raw_seat = ps_data.get("seatNumber")
    if raw_seat is None:
        return
    mapped = "0" if raw_seat == -1 else str(raw_seat + 1)
    dc = decision_cache.setdefault(table_name, {})
    for hk, hv in ps_data.items():
        if not (hk.startswith("h") and isinstance(hv, dict)):
            continue
        flat = f"{mapped}_{hk}"
        do = hv.get("decisionOffer")
        has = isinstance(do, dict)
        if not has and not hv.get("cards"):
            continue
        st = dc.setdefault(flat, {})
        if has:
            st["canDecide"] = True
            if do.get("cashOut") is not None:
                st["cashOut"] = do["cashOut"]
        elif hv.get("activeHand") is False and not hv.get("decisionTimer"):
            st.pop("canDecide", None)
            st.pop("cashOut", None)

def build_players(hands_state, dc):
    out = {}
    for flat, (score, cards) in hands_state.items():
        e = {"score": score, "cards": cards, "bet": 10}
        d = dc.get(flat)
        if isinstance(d, dict):
            if d.get("cashOut") is not None:
                e["cashOut"] = d["cashOut"]
            if d.get("canDecide"):
                e["canDecide"] = True
        out[flat] = e
    return out

# 场景：座位1(h0) 和 座位3(h0) 都是我，先后做决策
hands = {"1_h0": ("16", ["10","6"]), "3_h0": ("14", ["9","5"])}

# 帧1：座位1 拿到 decisionOffer（无 cashOut）
on_player_seat("T1", {"seatNumber": 0, "h0": {"score": "16", "cards": [{"scanCode": "05"},{"scanCode": "13"}], "activeHand": True, "decisionOffer": {"canDouble": True}}})
p1 = build_players(hands, decision_cache["T1"])
print("帧1(座位1决策,无cashOut):", json.dumps(p1, ensure_ascii=False))
assert p1["1_h0"].get("canDecide") is True, "座位1 应该 canDecide"
assert "3_h0" not in p1 or not p1["3_h0"].get("canDecide"), "座位3 此时不该有"

# 帧2：座位3 拿到 decisionOffer（有 cashOut 11.0）
on_player_seat("T1", {"seatNumber": 2, "h0": {"score": "14", "cards": [{"scanCode": "07"},{"scanCode": "03"}], "activeHand": True, "decisionOffer": {"cashOut": 11.0}}})
p2 = build_players(hands, decision_cache["T1"])
print("帧2(座位3决策,有cashOut):", json.dumps(p2, ensure_ascii=False))
assert p2["1_h0"].get("canDecide") is True, "★ 座位1 的 canDecide 必须还在"
assert p2["3_h0"].get("canDecide") is True, "座位3 应 canDecide"
assert p2["3_h0"].get("cashOut") == 11.0, "座位3 cashOut 应为 11.0"

print("\n✅ 两个座位同时保留了决策标记 —— 前端两边都会 fetchEv")

# 帧3：座位1 打完（activeHand False，无 offer）→ 撤掉标记
on_player_seat("T1", {"seatNumber": 0, "h0": {"score": "18", "cards": [{"scanCode": "05"},{"scanCode": "13"},{"scanCode": "02"}], "activeHand": False}})
p3 = build_players(hands, decision_cache["T1"])
print("帧3(座位1已行动完):", json.dumps(p3, ensure_ascii=False))
assert not p3["1_h0"].get("canDecide"), "座位1 决策结束应撤标记"
assert p3["3_h0"].get("canDecide") is True, "座位3 不受影响"
print("✅ 撤标记正确，不串局")
