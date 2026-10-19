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
  · **含 cashout（PP 的 ECU）** —— 服务器给的兑现价已实测反推成固定表（ECO_RATES），
    作为第 5 个候选参与比大小。满靴时兑现永远不划算，所以基准值一格都没动

【算式】
  把已出牌从 8 副靴 416 张里扣掉，得到剩余牌组成；
  枚举全部 10×10×10 = 1000 组「P1 × P2 × 庄家明牌」；
  每组取最优动作（Stand / Hit / Double / Split / CashOut）的 EV；
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
DAS = True                       # PP 规则：拆牌后可加倍（EVO 不可）。回滚改 False 即回到 EVO 口径
SHOE_TOTAL = DECKS * 52          # 416
LABEL = "PDEV       : "          # 11 字符宽，与 "True Count :" / "Total Card :" 对齐

# ═════════════════════════ PP cashout 定价表（实测反推）═════════════════════════
# 来源：cashout_capture 于 2026-10-17~18 抓包 33k 样本，按 cashOut ÷ bet 反推
# 已验证：同一格跨注金(5/10/20/25/30/40/50)、跨 TC(-4~+2)、分牌前后全部一致
#        → 定价是静态价，只取决于 (牌型, 玩家点数, 庄家明牌)，与剩余牌、注金无关
# 结构：kind → {点数: {明牌: 返还倍数}}；倍数含本金，净收益 = 倍数 - 1
# 覆盖：hard 160 / soft 80 / pair 90 = 330 格；缺 21 点(服务器不报价)、hard 4 点与 soft 12 点(两牌不可达)
# 2026-10-19 补：pair 6 对 A = 0.68（实盘 5 条，其中 1 条干净 + 4 条为 0.68 的整数倍多份注）
ECO_RATES = {
    "hard": {
        5: {"2": 0.86, "3": 0.89, "4": 0.93, "5": 0.97, "6": 0.99, "7": 0.87, "8": 0.8, "9": 0.72, "10": 0.62, "A": 0.7},
        6: {"2": 0.84, "3": 0.88, "4": 0.91, "5": 0.96, "6": 0.98, "7": 0.83, "8": 0.77, "9": 0.69, "10": 0.59, "A": 0.68},
        7: {"2": 0.88, "3": 0.91, "4": 0.95, "5": 0.98, "6": 1.02, "7": 0.92, "8": 0.77, "9": 0.7, "10": 0.61, "A": 0.67},
        8: {"2": 0.96, "3": 0.99, "4": 1.03, "5": 1.06, "6": 1.1, "7": 1.07, "8": 0.93, "9": 0.77, "10": 0.68, "A": 0.78},
        9: {"2": 1.06, "3": 1.11, "4": 1.17, "5": 1.24, "6": 1.31, "7": 1.16, "8": 1.08, "9": 0.93, "10": 0.77, "A": 0.92},
        10: {"2": 1.35, "3": 1.4, "4": 1.45, "5": 1.5, "6": 1.57, "7": 1.38, "8": 1.27, "9": 1.13, "10": 0.93, "A": 1.07},
        11: {"2": 1.46, "3": 1.51, "4": 1.56, "5": 1.61, "6": 1.66, "7": 1.45, "8": 1.34, "9": 1.21, "10": 1.02, "A": 1.13},
        12: {"2": 0.73, "3": 0.75, "4": 0.78, "5": 0.82, "6": 0.83, "7": 0.77, "8": 0.71, "9": 0.64, "10": 0.56, "A": 0.63},
        13: {"2": 0.69, "3": 0.73, "4": 0.78, "5": 0.82, "6": 0.83, "7": 0.71, "8": 0.66, "9": 0.6, "10": 0.52, "A": 0.59},
        14: {"2": 0.69, "3": 0.73, "4": 0.78, "5": 0.82, "6": 0.83, "7": 0.66, "8": 0.61, "9": 0.56, "10": 0.48, "A": 0.54},
        15: {"2": 0.69, "3": 0.73, "4": 0.78, "5": 0.82, "6": 0.83, "7": 0.62, "8": 0.57, "9": 0.51, "10": 0.44, "A": 0.5},
        16: {"2": 0.69, "3": 0.73, "4": 0.78, "5": 0.82, "6": 0.83, "7": 0.58, "8": 0.53, "9": 0.48, "10": 0.41, "A": 0.47},
        17: {"2": 0.83, "3": 0.87, "4": 0.91, "5": 0.94, "6": 0.99, "7": 0.88, "8": 0.6, "9": 0.56, "10": 0.52, "A": 0.51},
        18: {"2": 1.11, "3": 1.13, "4": 1.16, "5": 1.19, "6": 1.27, "7": 1.38, "8": 1.09, "9": 0.8, "10": 0.75, "A": 0.89},
        19: {"2": 1.37, "3": 1.39, "4": 1.41, "5": 1.43, "6": 1.48, "7": 1.6, "8": 1.58, "9": 1.27, "10": 0.97, "A": 1.27},
        20: {"2": 1.63, "3": 1.63, "4": 1.65, "5": 1.66, "6": 1.69, "7": 1.76, "8": 1.78, "9": 1.74, "10": 1.42, "A": 1.64},
    },
    "soft": {
        13: {"2": 1.03, "3": 1.06, "4": 1.09, "5": 1.12, "6": 1.17, "7": 1.11, "8": 1.04, "9": 0.95, "10": 0.82, "A": 0.93},
        14: {"2": 1.01, "3": 1.04, "4": 1.07, "5": 1.12, "6": 1.17, "7": 1.06, "8": 1.0, "9": 0.91, "10": 0.78, "A": 0.89},
        15: {"2": 0.98, "3": 1.01, "4": 1.05, "5": 1.12, "6": 1.17, "7": 1.02, "8": 0.96, "9": 0.87, "10": 0.75, "A": 0.85},
        16: {"2": 0.96, "3": 0.99, "4": 1.04, "5": 1.11, "6": 1.17, "7": 0.98, "8": 0.92, "9": 0.84, "10": 0.72, "A": 0.81},
        17: {"2": 0.99, "3": 1.04, "4": 1.11, "5": 1.18, "6": 1.24, "7": 1.04, "8": 0.91, "9": 0.84, "10": 0.73, "A": 0.8},
        18: {"2": 1.11, "3": 1.16, "4": 1.23, "5": 1.29, "6": 1.37, "7": 1.39, "8": 1.09, "9": 0.89, "10": 0.78, "A": 0.89},
        19: {"2": 1.37, "3": 1.39, "4": 1.41, "5": 1.43, "6": 1.48, "7": 1.6, "8": 1.58, "9": 1.27, "10": 0.97, "A": 1.26},
        20: {"2": 1.63, "3": 1.63, "4": 1.64, "5": 1.66, "6": 1.69, "7": 1.76, "8": 1.78, "9": 1.74, "10": 1.42, "A": 1.64},
    },
    "pair": {
        4: {"2": 0.9, "3": 0.97, "4": 1.04, "5": 1.12, "6": 1.19, "7": 0.98, "8": 0.83, "9": 0.75, "10": 0.64, "A": 0.73},
        6: {"2": 0.85, "3": 0.93, "4": 1.01, "5": 1.09, "6": 1.16, "7": 0.92, "8": 0.77, "9": 0.69, "10": 0.6, "A": 0.68},
        8: {"2": 0.97, "3": 1.0, "4": 1.03, "5": 1.07, "6": 1.14, "7": 1.07, "8": 0.93, "9": 0.78, "10": 0.68, "A": 0.79},
        10: {"2": 1.35, "3": 1.4, "4": 1.45, "5": 1.5, "6": 1.56, "7": 1.38, "8": 1.28, "9": 1.13, "10": 0.93, "A": 1.07},
        12: {"2": 0.77, "3": 0.86, "4": 0.94, "5": 1.03, "6": 1.1, "7": 0.77, "8": 0.71, "9": 0.64, "10": 0.56, "A": 0.63},
        14: {"2": 0.84, "3": 0.92, "4": 1.0, "5": 1.09, "6": 1.18, "7": 0.9, "8": 0.61, "9": 0.56, "10": 0.47, "A": 0.54},
        16: {"2": 1.01, "3": 0.73, "4": 0.78, "5": 0.82, "6": 0.83, "7": 0.58, "8": 0.53, "9": 0.58, "10": 0.41, "A": 0.6},
        18: {"2": 1.17, "3": 1.23, "4": 1.29, "5": 1.35, "6": 1.43, "7": 1.39, "8": 1.2, "9": 0.89, "10": 0.75, "A": 0.89},
        20: {"2": 1.62, "3": 1.63, "4": 1.64, "5": 1.66, "6": 1.69, "7": 1.76, "8": 1.78, "9": 1.74, "10": 1.42, "A": 1.64},
    },
}


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


DEALER_KEY = {1: "A", 10: "10"}     # 定价表的明牌 key：A / 2..9 / 10


def eco_multiple(i, j, total, soft, up):
    """查这一手的 cashout 返还倍数；表里没有这格返回 None（则该手不出现 CashOut 候选）。"""
    if i == j:
        kind, score = "pair", total
    elif soft:
        kind, score = "soft", total + 10
    else:
        kind, score = "hard", total
    row = ECO_RATES[kind].get(score)
    if not row:
        return None
    return row.get(DEALER_KEY.get(up) or str(up))


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
                if t > 21:
                    per += pv * (-1.0)
                elif DAS:                      # PP：拆牌后可加倍
                    per += pv * max(self.ev_stand(t, up),
                                    self.ev_hit(t, s, up),
                                    self.ev_double(t, s, up))
                else:                          # EVO：拆牌后只能 hit/stand
                    per += pv * max(self.ev_stand(t, up), self.ev_hit(t, s, up))
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
            base = max(a[0] for a in acts)
        else:
            q = self.q_bj(up) if (up == 1 or up == 10) else 0.0
            if up == 1 and self.peek_a:
                base = (1 - q) * max(a[0] for a in acts) - q
            elif (up == 1 or up == 10) and self.enhc:
                base = max((1 - q) * a[0] - q * a[1] for a in acts)
            else:
                base = max((1 - q) * a[0] for a in acts) - q

        # ── 第 5 候选：Cash Out ──────────────────────────────────────────
        # 服务器给的是固定兑现价（只跟 牌型/点数/明牌 有关），净收益 = 倍数 - 1。
        # 兑现是确定收益：不乘 (1-q)、不扣庄家 BJ 概率。
        mult = eco_multiple(i, j, t, s, up)
        if mult is not None:
            base = max(base, mult - 1.0)
        return base


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
        pdev        PDEV（已含 cashout 第 5 候选）
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
    """板面那一行：'PDEV       : -0.0054714 [6]'（含 cashout；[ ] 是已出张数）"""
    r = compute_pdev(cards)
    return "%s%.7f [%d]" % (LABEL, r["pdev"], r["issued"])


# ═════════════════════════ 自检基准（从 pdev_local.js 的 EXPECT 抄来，逐帧对账）═════════════════════════
EXPECT = {
    # ── DAS 口径（PP 规则：拆后可加倍，2026-10-18 起）──
    # ── 2026-10-18 起再叠加 cashout 第 5 候选（ECO_RATES）──
    #    满靴 / 6 张这两档 cashout 一格都没被选中，数值与加之前完全相同
    #    只有牌堆被打薄、继续赌变差的帧才会变（VIP35 那档从 -0.0060405 提到 -0.0025958）
    "满靴": {
        "cards": [],
        "pdev": -0.00533082514671704,
        "remaining": 416,
    },
    "6 张 7,K,7,2,2,8": {
        "cards": ["7", "K", "7", "2", "2", "8"],
        "pdev": -0.00435109497422002,
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
        "pdev": -0.002595793044411817,
        "remaining": 287,
        # 不含 cashout 时：-0.006040520424947918（回滚参考，勿删）
    },
}

# ── 旧 EVO 口径基准（DAS=False 时用；回滚参考，勿删）──
# 满靴            -0.006476944408866543
# 7,K,7,2,2,8     -0.005471394935613659
# VIP35 129 张    -0.007095334086665857


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
