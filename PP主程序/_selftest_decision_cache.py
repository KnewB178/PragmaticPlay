# -*- coding: utf-8 -*-
"""验证决策状态：轮流熄灭是否正确
   场景：我坐 1 号和 7 号位
     1) 轮到 1 号  → 1 号亮
     2) 1 号做完   → 1 号灭，7 号仍不亮（还没轮到）
     3) 中间别人(2/3号)做决策 → 不影响我任何一个位
     4) 轮到 7 号  → 7 号亮
     5) 7 号做完   → 7 号灭
"""
import json

decision_cache = {}

def on_frame(table, ps_data):
    """复刻 PPBJ.py 的决策缓存更新逻辑"""
    raw_seat = ps_data.get("seatNumber")
    if raw_seat is None:
        return
    mapped = "0" if raw_seat == -1 else str(raw_seat + 1)
    dc = decision_cache.setdefault(table, {})
    for hk, hv in ps_data.items():
        if not (hk.startswith("h") and isinstance(hv, dict)):
            continue
        flat = f"{mapped}_{hk}"
        do = hv.get("decisionOffer")
        has = isinstance(do, dict)
        if not has and not hv.get("cards"):
            continue
        st = dc.setdefault(flat, {})
        if has or "decisionTimer" in hv:
            st["canDecide"] = True
            if has and do.get("cashOut") is not None:
                st["cashOut"] = do["cashOut"]
        else:
            st.pop("canDecide", None)
            st.pop("cashOut", None)

def lit(table):
    """当前亮着决策的手"""
    return sorted(k for k, v in decision_cache.get(table, {}).items() if v.get("canDecide"))

C = lambda *a: [{"scanCode": x} for x in a]

# 1) 轮到 1 号
on_frame("T", {"seatNumber": 0, "h0": {"score": "16", "cards": C("05", "13"),
                                      "decisionOffer": {"canDouble": True}}})
print("1) 轮到1号:", lit("T"), "  期望 ['1_h0']")
assert lit("T") == ["1_h0"]

# 7 号先出现在桌上（有牌，但没轮到他）
on_frame("T", {"seatNumber": 6, "h0": {"score": "13", "cards": C("04", "09")}})
print("   7号上桌(无offer):", lit("T"), "  期望 ['1_h0']")
assert lit("T") == ["1_h0"], "7号没 offer 不该亮"

# 2) 1 号做完（同一帧：有牌、无 offer、无 timer）
on_frame("T", {"seatNumber": 0, "h0": {"score": "18", "cards": C("05", "13", "02")}})
print("2) 1号做完:", lit("T"), "  期望 []")
assert lit("T") == [], "1号做完必须灭"

# 3) 别人(2号、3号)做决策，不能影响我
on_frame("T", {"seatNumber": 1, "h0": {"score": "20", "cards": C("00", "11"),
                                      "decisionOffer": {"cashOut": 50.0}}})
on_frame("T", {"seatNumber": 2, "h0": {"score": "15", "cards": C("06", "09"),
                                      "decisionOffer": {"cashOut": 33.0}}})
mine = [k for k in lit("T") if k in ("1_h0", "7_h0")]
print("3) 别人做决策，我的位:", mine, "  期望 []（我的位不受影响）")
assert mine == [], "别人决策不该点亮我的位"
assert lit("T") == ["2_h0", "3_h0"], "别人自己的位亮着是对的（前端只渲染被追踪玩家）"

# 4) 轮到 7 号
on_frame("T", {"seatNumber": 6, "h0": {"score": "13", "cards": C("04", "09"),
                                      "decisionOffer": {"cashOut": 21.5}}})
print("4) 轮到7号，我的位:", [k for k in lit("T") if k in ("1_h0", "7_h0")], "  期望 ['7_h0']")
assert [k for k in lit("T") if k in ("1_h0", "7_h0")] == ["7_h0"]
assert decision_cache["T"]["7_h0"].get("cashOut") == 21.5
assert not decision_cache["T"].get("1_h0", {}).get("canDecide"), "1号做完后不该再被点亮"

# 5) 7 号做完
on_frame("T", {"seatNumber": 6, "h0": {"score": "19", "cards": C("04", "09", "06")}})
print("5) 7号做完，我的位:", [k for k in lit("T") if k in ("1_h0", "7_h0")], "  期望 []")
assert [k for k in lit("T") if k in ("1_h0", "7_h0")] == [], "7号做完必须灭"

# 6) decisionTimer 存在时不能灭（还在决策窗口）
on_frame("T", {"seatNumber": 0, "h0": {"score": "16", "cards": C("05", "13"),
                                      "decisionOffer": {"canDouble": True}, "decisionTimer": 8}})
on_frame("T", {"seatNumber": 0, "h0": {"score": "16", "cards": C("05", "13"),
                                      "decisionTimer": 5}})
print("6) 只剩 decisionTimer，我的位:", [k for k in lit("T") if k in ("1_h0", "7_h0")], "  期望 ['1_h0']")
assert "1_h0" in lit("T"), "还有 decisionTimer 说明还在他手上，不能灭"

print("\n✅ 轮流熄灭全部正确")
