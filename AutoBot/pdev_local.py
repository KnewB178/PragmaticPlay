#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDEV 本地计算引擎（Python 版）—— `pdev_local.js` 的逐帧等价移植
================================================================================

【这份是干什么的】
  PP 侧原本只有 TC（Hi-Lo 真数）。这份把 EVO 那套 PDEV（精准期望值）搬过来，
  让 PP 主程序也能在板面上多出一行 PDEV，跟 EVO 一模一样。

【口径（与 pdev_local.js 完全一致，一个数都不许差）】
  · 老板口径 bossMode = True
  · BJ 赔付 bjPay    = 1.2  （= 6:5；PP 桌上实际是 1.5，但按老板要求压成 1.2）
  · S17（庄家软 17 站）· 明牌 10 无 peek（全吃）· 明牌 A 不 peek
  · **只算不含 eco 的 PDEV** —— PP 桌没有固定价目表，含 ECU 版另行处理

【算式】
  把已出牌从 8 副靴 416 张里扣掉，得到剩余牌组成；
  枚举全部 10×10×10 = 1000 组「P1 × P2 × 庄家明牌」；
  每组取最优动作（Stand / Hit / Double / Split）的 EV；
  按该组出现概率加权求和 = PDEV。

【怎么用】
  from pdev_local import compute_pdev, frame_line
  r = compute_pdev(["7", "K", "7", "2", "2", "8"])
  r["pdev"]        # -0.005471394935613659
  r["remaining"]   # 410
  frame_line(...)  # 'PDEV       : -0.0054714 [6]'

【自检（必过）】
  python pdev_local.py --selftest
================================================================================
"""

import sys
from collections import Counter

VALUES = list(range(1, 11))
DECKS = 8
BJ_PAY = 1.2
SHOE_TOTAL = DECKS * 52          # 416
LABEL = "PDEV       : "          # 11 字符宽，与 "True Count :" / "Total Card :" 对齐


# ═════════════════════════ 牌面符号 ═════════════════════════
def sym_to_value(card):
    """'A'→1，'T'/'J'/'Q'/'K'/'10'→10，'2'..'9'→2..9。严格单个字符，避免脏 token 被读成牌面。"""
    s = str(card).strip().upper()
    if s == "A":
        return 1
    if s in ("10", "T", "J", "Q", "K"):
        return 10
    if len(s) == 1 and "2" <= s <= "9":
        return ord(s) - 48
    raise ValueError("无法识别的牌面: %r" % (card,))


def norm_card_sym(card):
    v = sym_to_value(card)
    return "A" if v == 1 else ("T" if v == 10 else str(v))


def key_name(v):
    return "A" if v == 1 else ("T" if v == 10 else str(v))


def count_cards(cards):
    """把已出牌清单折成 {牌值: 张数}；认不出的 token 直接跳过（与 pdev.py 的宽松度一致）"""
    removed = {}
    for c in (cards or []):
        try:
            v = sym_to_value(c)
        except ValueError:
            continue
        removed[v] = removed.get(v, 0) + 1
    return removed


# ═════════════════════════ 牌靴 ═════════════════════════
class Shoe:
    """8 副靴，10 个牌值（10 含 T/J/Q/K 共 16×8 = 128 张）"""

    def __init__(self, decks=DECKS, removed=None):
        self.decks = decks
        self.counts = {v: (16 * decks if v == 10 else 4 * decks) for v in VALUES}
        if removed:
            for k, n in removed.items():
                self.counts[int(k)] -= n
        self.total = sum(self.counts.values())

    def p(self, v):
        return self.counts[v] / self.total

    def prob_three(self, i, j, u):
        """有序三张联合概率 P(第一张=i, 第二张=j, 庄家明牌=u)"""
        n = self.total
        if n < 3 or self.counts[i] <= 0:
            return 0.0
        pr = self.counts[i] / n
        ci = self.counts[i] - 1
        if ci <= 0 and i == j:
            return 0.0
        cj = ci if j == i else self.counts[j]
        if cj <= 0:
            return 0.0
        pr *= cj / (n - 1)
        cu = self.counts[u]
        if u == i:
            cu -= 1
        if u == j:
            cu -= 1
        if cu <= 0:
            return 0.0
        pr *= cu / (n - 2)
        return pr


# ═════════════════════════ EV 引擎（原样对应 ev_tool.html 的 EV 引擎）═════════════════════════
class EVEngine:
    def __init__(self, shoe, boss_mode=True, bj_pay=BJ_PAY,
                 s17=True, enhc=True, peek_a=False):
        self.shoe = shoe
        self.boss_mode = boss_mode
        self.bj_pay = bj_pay
        self.s17 = s17
        self.enhc = enhc            # 明牌 10 无 peek（全吃）
        self.peek_a = peek_a        # 明牌 A 有 peek（只吃原注）
        self._dc = {}
        self._dp = {}
        self._hit = {}

    # ── 单张推进 ──
    @staticmethod
    def _step(total, soft, v):
        t = total + v
        s = soft
        if v == 1 and t + 10 <= 21:
            t += 10
            s = True
        if t > 21 and s:
            t -= 10
            s = False
        return t, s

    # ── 庄家补牌分布 ──
    def _dealer_play(self, total, soft):
        if total > 21:
            return {0: 1.0}
        if total >= 17 and not (not self.s17 and soft and total == 17):
            return {total: 1.0}
        key = (total, soft)
        c = self._dp.get(key)
        if c is not None:
            return c
        out = {}
        for v in VALUES:
            pv = self.shoe.p(v)
            if pv <= 0:
                continue
            t, s = self._step(total, soft, v)
            d = self._dealer_play(t, s)
            for k, val in d.items():
                out[k] = out.get(k, 0.0) + pv * val
        self._dp[key] = out
        return out

    def dealer(self, up):
        """返回 (q, dist)：q = 庄家 Blackjack 概率；dist = 去掉 BJ 后归一化的最终点数分布"""
        if up in self._dc:
            return self._dc[up]
        q = 0.0
        dist = {}
        for v in VALUES:
            pv = self.shoe.p(v)
            if pv <= 0:
                continue
            if up == 1 and v == 10:
                q += pv
                continue
            if up == 10 and v == 1:
                q += pv
                continue
            if up == 1:
                total, soft = 11, True
            else:
                total, soft = up, False
            t, s = self._step(total, soft, v)
            d = self._dealer_play(t, s)
            for k, val in d.items():
                dist[k] = dist.get(k, 0.0) + pv * val
        for k in dist:
            dist[k] /= (1 - q)
        self._dc[up] = (q, dist)
        return self._dc[up]

    def q_bj(self, up):
        return self.dealer(up)[0]

    # ── 玩家动作 EV ──
    def ev_stand(self, total, up):
        ev = 0.0
        for k, p in self.dealer(up)[1].items():
            if k == 0 or k < total:
                ev += p
            elif k > total:
                ev -= p
        return ev

    def ev_hit(self, total, soft, up):
        key = (total, soft, up)
        if key in self._hit:
            return self._hit[key]
        ev = 0.0
        self._hit[key] = 0.0                 # 防递归环（与原 JS 一致）
        for v in VALUES:
            pv = self.shoe.p(v)
            if pv <= 0:
                continue
            t, s = self._step(total, soft, v)
            ev += pv * (-1.0 if t > 21 else max(self.ev_stand(t, up), self.ev_hit(t, s, up)))
        self._hit[key] = ev
        return ev

    def ev_double(self, total, soft, up):
        ev = 0.0
        for v in VALUES:
            pv = self.shoe.p(v)
            if pv <= 0:
                continue
            t, s = self._step(total, soft, v)
            ev += pv * (2 * self.ev_stand(t, up) if t <= 21 else -2.0)
        return ev

    def ev_split(self, val, up):
        per = 0.0
        for v in VALUES:
            pv = self.shoe.p(v)
            if pv <= 0:
                continue
            if val == 1:
                total = 12 if v == 1 else 11 + v     # A+A = 软12；A+10 = 21
                per += pv * self.ev_stand(total, up)
            else:
                t, s = self._step(val, False, v)
                per += pv * (self.ev_stand(t, up) if t > 21
                             else max(self.ev_stand(t, up), self.ev_hit(t, s, up)))
        return 2 * per

    # ── 归一化 ──
    def col_ev(self, cond_ev, up):
        if up == 10:
            q = self.q_bj(10)
            return (1 - q) * cond_ev - q
        if up == 1:
            if self.boss_mode:
                return cond_ev                        # 老板口径：只归一化、不扣
            q = self.q_bj(1)
            return (1 - q) * cond_ev - q
        return cond_ev

    # ── 一组手牌（P1, P2, 明牌）的 EV ──
    def row_ev(self, i, j, up):
        is_bj = (i == 1 and j == 10) or (i == 10 and j == 1)
        if is_bj and up != 1 and up != 10:
            return self.bj_pay
        if is_bj and not self.boss_mode:
            return self.bj_pay * (1 - self.q_bj(up))
        if is_bj:
            return self.col_ev(self.ev_stand(21, up), up)   # 老板：按普通 21 算

        t, s = self._step(0, False, i)
        t, s = self._step(t, s, j)
        acts = [(self.ev_stand(t, up), 1),
                (self.ev_hit(t, s, up), 1),
                (self.ev_double(t, s, up), 2)]
        if i == j:
            acts.append((self.ev_split(i, up), 2))

        if up == 1 and self.boss_mode:
            return max(a[0] for a in acts)
        q = self.q_bj(up) if (up == 1 or up == 10) else 0.0
        if up == 1 and self.peek_a:
            return (1 - q) * max(a[0] for a in acts) - q
        if (up == 1 or up == 10) and self.enhc:
            return max((1 - q) * a[0] - q * a[1] for a in acts)
        return max((1 - q) * a[0] for a in acts) - q


# ═════════════════════════ 1000 行枚举 ═════════════════════════
def run_table(engine):
    shoe = engine.shoe
    rows = []
    total = 0.0
    sum_prob = 0.0
    for i in VALUES:
        for j in VALUES:
            for up in VALUES:
                prob = shoe.prob_three(i, j, up)
                ev = engine.row_ev(i, j, up)
                cont = prob * ev
                total += cont
                sum_prob += prob
                rows.append({"idx": len(rows) + 1, "p1": i, "p2": j, "d": up,
                             "prob": prob, "ev": ev, "cont": cont})
    return {"total": total, "sum_prob": sum_prob, "rows": rows}


# ═════════════════════════ 对外 API ═════════════════════════
_CACHE = {}
_CACHE_MAX = 128


def _signature(counts):
    return ",".join(str(counts.get(v, 0)) for v in VALUES)


def compute_pdev(cards, use_cache=True):
    """
    cards: 整靴已出牌清单（['7','K','7',...]；'10'/'J'/'Q'/'K' 都算 10）

    返回 dict:
        pdev        不含 eco 的 PDEV（本文件唯一口径）
        remaining   余牌张数
        issued      已出张数
        cases       枚举数（恒为 1000）
        removed     已出牌折成 {牌值: 张数}
    """
    removed = count_cards(cards)
    counts = {}
    for v in VALUES:
        counts[v] = (16 * DECKS if v == 10 else 4 * DECKS) - removed.get(v, 0)

    sig = _signature(counts) if use_cache else None
    if use_cache and sig in _CACHE:
        return _CACHE[sig]

    engine = EVEngine(Shoe(DECKS, removed))
    table = run_table(engine)

    res = {
        "pdev": table["total"],
        "remaining": engine.shoe.total,
        "cases": len(table["rows"]),
        "issued": DECKS * 52 - engine.shoe.total,
        "removed": removed,
    }
    if use_cache:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[sig] = res
    return res


def clear_cache():
    _CACHE.clear()


def frame_line(cards):
    """板面那一行：'PDEV       : -0.0054714 [6]'（不含 eco；[ ] 是已出张数）"""
    r = compute_pdev(cards)
    return "%s%.7f [%d]" % (LABEL, r["pdev"], r["issued"])


# ═════════════════════════ 自检基准（从 pdev_local.js 的 EXPECT 抄来，逐帧对账）═════════════════════════
EXPECT = {
    "满靴": {
        "cards": [],
        "pdev": -0.006476944408866543,
        "remaining": 416,
    },
    "6 张 7,K,7,2,2,8": {
        "cards": ["7", "K", "7", "2", "2", "8"],
        "pdev": -0.005471394935613659,
        "remaining": 410,
    },
    "VIP35 真实帧 129 张": {
        "cards": ["6", "2", "T", "A", "4", "5", "T", "3", "A", "8", "T", "4", "T", "T", "T",
                  "5", "9", "8", "A", "T", "2", "A", "8", "5", "T", "T", "8", "9", "4", "9",
                  "5", "7", "9", "5", "7", "4", "T", "8", "3", "6", "7", "T", "6", "T", "5",
                  "7", "8", "6", "7", "T", "8", "A", "5", "T", "7", "8", "7", "8", "T", "3",
                  "T", "4", "5", "T", "7", "9", "6", "T", "5", "T", "3", "T", "8", "T", "7",
                  "T", "6", "5", "8", "4", "9", "A", "A", "T", "T", "T", "2", "T", "T", "8",
                  "8", "3", "T", "8", "4", "A", "T", "T", "8", "9", "A", "5", "8", "T", "3",
                  "3", "T", "3", "T", "8", "3", "5", "T", "7", "2", "7", "2", "T", "9", "T",
                  "T", "6", "4", "T", "6", "T", "2", "5", "T"],
        "pdev": -0.007095334086665857,
        "remaining": 287,
    },
}


def selftest():
    out = {"ok": True, "checks": []}
    for name, exp in EXPECT.items():
        try:
            r = compute_pdev(exp["cards"], use_cache=False)
        except Exception as e:
            out["ok"] = False
            out["checks"].append({"name": name, "ok": False, "error": str(e)})
            continue
        d = abs(r["pdev"] - exp["pdev"])
        ok = d < 1e-9 and r["remaining"] == exp["remaining"]
        if not ok:
            out["ok"] = False
        out["checks"].append({
            "name": name, "ok": ok, "remaining": r["remaining"],
            "got": r["pdev"], "want": exp["pdev"], "diff": d,
        })
    return out


# ═════════════════════════ 命令行 ═════════════════════════
if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        import json
        s = selftest()
        print("SELFTEST::" + json.dumps(s, ensure_ascii=False))
        for c in s["checks"]:
            if c.get("ok"):
                print("  [OK]   %s  pdev=%.15f  remaining=%d" % (c["name"], c["got"], c["remaining"]))
            else:
                print("  [FAIL] %s  %s" % (c["name"], c))
        sys.exit(0 if s["ok"] else 1)

    import time as _t
    cards = []
    if "--cards" in argv:
        i = argv.index("--cards")
        if i + 1 < len(argv):
            cards = [x for x in argv[i + 1].replace(",", " ").split() if x]
    t0 = _t.time()
    r = compute_pdev(cards, use_cache=False)
    ms = (_t.time() - t0) * 1000
    print("已出      : %d 张" % r["issued"])
    print("余牌      : %d 张（共 %d 个 case）" % (r["remaining"], r["cases"]))
    print("PDEV      = %.10f" % r["pdev"])
    print("帧里那一行 : %s" % frame_line(cards))
    print("耗时 %.1fms" % ms)
