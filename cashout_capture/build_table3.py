# -*- coding: utf-8 -*-
"""
build_table3.py —— 把 cashOut 样本拆成「硬牌 / 软牌 / 对子」三张定价表

为什么拆：
  同一「点数 + 庄家明牌」，牌型不同服务器开价不同。
  实测 6 点 vs 庄家 5：3+3（对子）1.09，2+4（硬牌）0.96 —— 差 0.13。
  原因是分牌权：对子可以拆两手打，兑现要价更高。

另外修掉 build_table.py 的一个解析漏洞：
  软牌的 score 字段是 "8/18"（硬点/软点），int(float()) 直接抛异常，
  所以主表其实**丢掉了全部软牌样本**（约 2974 行）。本脚本按 "/" 正确识别。

用法：
  python build_table3.py                 # 读 samples/，输出 eco_table3.csv / .html
  python build_table3.py --dir E:\\xxx
"""

import argparse
import csv
import os
from collections import Counter, defaultdict
from datetime import datetime

import build_table as bt          # 复用 norm_dealer / resolve / DEALER_KEYS

SCORES = list(range(4, 22))
DEALER_KEYS = bt.DEALER_KEYS
CONFIRM_N = 3

KINDS = [("hard", "硬牌"), ("soft", "软牌"), ("pair", "对子")]
KIND_CN = dict(KINDS)


# ---------------------------------------------------------------- 牌型识别

def card_val(c):
    c = c.strip().upper()
    if c == "A":
        return 11
    if c in ("10", "J", "Q", "K", "T"):
        return 10
    try:
        return int(c)
    except ValueError:
        return 0


def split_cards(raw):
    return [c for c in str(raw or "").replace("/", " ").split() if c]


def parse_score(raw):
    """返回 (用于决策的总点数, 是否软牌)

    PP 的 score 字段：硬牌 "18"，软牌 "8/18"（软点在后）。
    """
    s = str(raw or "").strip()
    if "/" in s:
        a, b = s.split("/", 1)
        return int(float(b)), True
    return int(float(s)), False


def classify(cards, is_soft):
    """返回 (kind, pair_rank) —— pair_rank 只在对子时有值"""
    cs = split_cards(cards)
    if len(cs) == 2:
        v0, v1 = card_val(cs[0]), card_val(cs[1])
        if v0 == v1 and v0 > 0:
            return "pair", v0
    if is_soft:
        return "soft", None
    return "hard", None


# ---------------------------------------------------------------- 聚合

def build3(rows):
    cells = defaultdict(lambda: {"values": Counter(), "raw": []})
    pair_rank = defaultdict(Counter)      # (score,dealer) → 对子点数分布
    skipped = Counter()

    for r in rows:
        try:
            co = float(r["cash_out"])
            bet = float(r["bet"] or 0)
        except (ValueError, KeyError, TypeError):
            skipped["字段缺失"] += 1
            continue
        if bet <= 0:
            skipped["注金未知"] += 1
            continue
        try:
            score, is_soft = parse_score(r.get("score"))
        except (ValueError, TypeError):
            skipped["点数无法解析"] += 1
            continue
        if not (4 <= score <= 21):
            skipped["点数越界"] += 1
            continue
        dealer = bt.norm_dealer(r.get("dealer_up"))
        if dealer not in DEALER_KEYS:
            skipped["庄家明牌未知"] += 1
            continue
        ratio = round(co / bet, 2)
        if not (bt.RATIO_MIN <= ratio <= bt.RATIO_MAX):
            skipped["倍数不合理(注金错)"] += 1
            continue

        kind, pr = classify(r.get("cards"), is_soft)
        cell = cells[(score, dealer, kind)]
        cell["values"][ratio] += 1
        cell["raw"].append((ratio, co, bet, r.get("cards", "")))
        if pr:
            pair_rank[(score, dealer)][pr] += 1

    if skipped:
        print("[i] 丢弃统计: " + "，".join(f"{k} {v}" for k, v in skipped.items()))
    return cells, pair_rank


def collect3(cells):
    grid = {}
    for score in SCORES:
        for dealer in DEALER_KEYS:
            for kind, _ in KINDS:
                cell = cells.get((score, dealer, kind))
                if not cell:
                    grid[(score, dealer, kind)] = None
                    continue
                mult, conf, flags = bt.resolve(cell)
                grid[(score, dealer, kind)] = {
                    "mult": mult, "conf": conf, "flags": flags,
                    "n": sum(cell["values"].values()),
                    "obs": dict(cell["values"]),
                }
    return grid


def collect_pooled(grid):
    """点数级（不含对子）作为回退值：样本不足时用它"""
    pooled = {}
    for score in SCORES:
        for dealer in DEALER_KEYS:
            ws = Counter()
            for kind in ("hard", "soft"):
                g = grid.get((score, dealer, kind))
                if g:
                    ws[round(g["mult"], 2)] += g["n"]
            pooled[(score, dealer)] = ws.most_common(1)[0][0] if ws else None
    return pooled


def _near_multiple(v, base, k_max=6):
    """v 是否是 base 的 2..k 倍（多份注造成的假高价）"""
    if not base:
        return 0
    for k in range(2, k_max + 1):
        if abs(v - k * base) <= 0.03 * k:
            return k
    return 0


def clean_pairs(grid, pooled, max_premium=0.35):
    """把对子格里「多份注」造成的假高价剔掉。

    对子格样本少（很多格 1-5 条），一旦整格都是 double/分牌后的 2 份注，
    众数会直接锁定在 2× 值上（例：18 点 vs A 出现 1.78 = 2 × 硬牌 0.89）。
    做法：以同格硬牌值为锚，先剥掉整数倍观测，再对残余溢价做上限校验。
    """
    for score in SCORES:
        for dealer in DEALER_KEYS:
            g = grid[(score, dealer, "pair")]
            if not g:
                continue
            h = grid[(score, dealer, "hard")]
            anchor = h["mult"] if h else pooled.get((score, dealer))
            if not anchor:
                continue

            kept = {}
            stripped = 0
            for v, n in g["obs"].items():
                if _near_multiple(v, anchor):
                    stripped += n
                else:
                    kept[v] = n

            if stripped:
                g["flags"].append(f"对子格剥离多份注×{stripped}")

            if not kept:
                g["mult"], g["conf"] = anchor, "weak"
                g["fallback"] = True
                g["flags"].append("对子样本全为多份注→取点数级值")
                continue

            best = Counter(kept).most_common(1)[0][0]
            if abs(best - anchor) > max_premium:
                g["mult"], g["conf"] = anchor, "weak"
                g["fallback"] = True
                g["flags"].append(f"溢价 {best - anchor:+.2f} 超上限→取点数级值")
                continue
            g["mult"] = best
            g["n"] = sum(kept.values())
    return grid


# ---------------------------------------------------------------- 输出

def write_csv(grid, pooled, out_path):
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "dealer", "kind", "eco_multiple", "eco_ev",
                    "samples", "confidence", "is_fallback", "fallback_multiple",
                    "observations", "flag"])
        for score in SCORES:
            for dealer in DEALER_KEYS:
                for kind, _ in KINDS:
                    g = grid[(score, dealer, kind)]
                    fb = pooled.get((score, dealer))
                    fb_s = f"{fb:.2f}" if fb is not None else ""
                    if not g:
                        w.writerow([score, dealer, kind, "", "", 0, "empty", 1,
                                    fb_s, "", "无样本→用回退值"])
                        continue
                    obs = " / ".join(f"{v}x{n}" for v, n in sorted(g["obs"].items()))
                    w.writerow([score, dealer, kind, f"{g['mult']:.2f}",
                                f"{g['mult'] - 1:+.2f}", g["n"], g["conf"],
                                1 if g.get("fallback") else 0, fb_s,
                                obs, "；".join(g["flags"])])


def matrix_html(grid, pooled, kind, title, note):
    """生成单张热力图"""
    def color(m):
        if m is None:
            return "#f2f3f5", "#b9bcc4"
        t = max(0.0, min(1.0, (m - 0.35) / (1.80 - 0.35)))
        if t < 0.5:
            k = t / 0.5
            r, g, b = int(22 + 223 * k), int(163 + 82 * k), int(74 + 171 * k)
        else:
            k = (t - 0.5) / 0.5
            r, g, b = int(245 - 25 * k), int(245 - 207 * k), int(245 - 207 * k)
        return f"rgb({r},{g},{b})", ("#fff" if t > 0.62 or t < 0.16 else "#1f2329")

    rows = []
    for score in SCORES:
        tds = []
        any_data = False
        for dealer in DEALER_KEYS:
            g = grid[(score, dealer, kind)]
            if not g:
                fb = pooled.get((score, dealer))
                if fb is None:
                    tds.append('<td style="background:#f7f8fa;color:#c9ccd1" title="无数据">·</td>')
                else:
                    tds.append(f'<td style="background:#f7f8fa;color:#a0a4ab" '
                               f'title="本牌型无样本，回退到点数级 {fb:.2f}">{fb:.2f}~</td>')
                continue
            any_data = True
            bg, fg = color(g["mult"])
            if g.get("fallback"):
                tag = "~"
            else:
                tag = {"confirmed": "", "suspect": "!"}.get(g["conf"], "°")
            tip = f"{score} 点 vs {dealer} · {g['n']} 条 · {g['conf']}"
            if g["flags"]:
                tip += " · " + "；".join(g["flags"])
            tds.append(f'<td style="background:{bg};color:{fg}" title="{tip}">'
                       f'{g["mult"]:.2f}{tag}</td>')
        if not any_data:
            continue
        rows.append(f"<tr><th>{score}</th>{''.join(tds)}</tr>")

    head = "".join(f"<th>庄家 {d}</th>" for d in DEALER_KEYS)
    return f"""
<h2>{title}</h2>
<div class="note">{note}</div>
<table><thead><tr><th>点数</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"""


def diff_html(grid):
    """对子 − 硬牌 的溢价表"""
    rows, cnt = [], 0
    for score in SCORES:
        tds = []
        for dealer in DEALER_KEYS:
            p = grid[(score, dealer, "pair")]
            h = grid[(score, dealer, "hard")]
            if not p or not h or p.get("fallback"):
                tds.append('<td style="background:#f7f8fa;color:#c9ccd1" '
                           'title="对子无有效样本">·</td>')
                continue
            d = p["mult"] - h["mult"]
            cnt += 1
            if abs(d) < 0.02:
                bg, fg = "#f2f3f5", "#646a73"
            elif d > 0:
                t = min(1.0, d / 0.20)
                bg = f"rgb({int(245 - 15 * t)},{int(230 - 150 * t)},{int(230 - 150 * t)})"
                fg = "#fff" if t > 0.6 else "#1f2329"
            else:
                t = min(1.0, -d / 0.20)
                bg = f"rgb({int(228 - 60 * t)},{int(240 - 30 * t)},{int(232 - 40 * t)})"
                fg = "#1f2329"
            tds.append(f'<td style="background:{bg};color:{fg}" '
                       f'title="对子 {p["mult"]:.2f} - 硬牌 {h["mult"]:.2f}">'
                       f'{d:+.2f}</td>')
        rows.append(f"<tr><th>{score}</th>{''.join(tds)}</tr>")
    head = "".join(f"<th>庄家 {d}</th>" for d in DEALER_KEYS)
    return f"""
<h2>对子溢价（对子 − 硬牌）</h2>
<div class="note">红 = 对子开价更高（分牌权的价值）；灰 = 两者一致。共 {cnt} 格可比。</div>
<table><thead><tr><th>点数</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"""


def write_html(grid, pooled, out_path, n_samples):
    css = """
 body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      background:#fff;color:#1f2329;margin:0;padding:28px 24px}
 h1{font-size:20px;margin:0 0 6px}
 h2{font-size:15px;margin:30px 0 4px;padding-top:18px;border-top:1px solid #eceef1}
 .sub{color:#646a73;font-size:13px;margin-bottom:16px}
 .note{color:#8a8f99;font-size:12px;margin-bottom:10px}
 .stats{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}
 .stat{border:1px solid #e5e6eb;border-radius:8px;padding:10px 16px;min-width:96px}
 .stat b{display:block;font-size:22px;line-height:1.2}
 .stat span{font-size:12px;color:#646a73}
 table{border-collapse:separate;border-spacing:2px;font-variant-numeric:tabular-nums}
 th{font-size:12px;font-weight:500;color:#646a73;padding:6px 4px;text-align:center}
 tbody th{font-weight:600;color:#1f2329;font-size:13px;width:44px}
 td{width:56px;height:32px;text-align:center;border-radius:5px;font-size:12.5px;
     font-weight:600;cursor:default}
 .legend{margin-top:22px;font-size:12px;color:#646a73;line-height:1.9}
 code{background:#f2f3f5;padding:1px 5px;border-radius:4px}
"""

    totals = Counter()
    for (sc, d, k), g in grid.items():
        if g:
            totals[k] += g["n"]

    parts = [matrix_html(grid, pooled, k, f"{cn}（n={totals[k]}）",
                         {"hard": "两张以上的非软牌；不含对子。",
                          "soft": "含 A 且 A 按 11 点计（score 字段为 x/y 两个数）。",
                          "pair": "两张同点数（可拆牌），含 A,A。"}[k])
             for k, cn in KINDS]
    parts.append(diff_html(grid))

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>PP cashOut 定价表 · 三态拆分</title>
<style>{css}</style></head><body>
<h1>PP cashOut 定价表 · 硬牌 / 软牌 / 对子</h1>
<div class="sub">格子 = 兑现能拿回注金的倍数 → 兑现 EV = 倍数 − 1。<br>
不带符号 = 样本 ≥3 且无分歧；° = 样本不足或存在分歧；! = 横向单调校验异常；<code>1.23~</code> = 本牌型无样本，回退点数级值。</div>
<div class="stats">
  <div class="stat"><b>{n_samples}</b><span>原始样本行</span></div>
  <div class="stat"><b>{totals['hard']}</b><span>硬牌样本</span></div>
  <div class="stat"><b>{totals['soft']}</b><span>软牌样本</span></div>
  <div class="stat"><b>{totals['pair']}</b><span>对子样本</span></div>
</div>
{''.join(parts)}
<div class="legend">
  <b>为什么拆：</b>对子有分牌权，服务器给的兑现价更高（实测 6 点 vs 庄家 5：3+3 → 1.09，2+4 → 0.96）。<br>
  <b>软牌为什么单独一栏：</b>软牌还能再要牌不爆，价值高于同点数的硬牌。<br>
  <b>用法：</b>决策时按自己实际牌型查对应那一栏；该格空就用回退值（带 <code>~</code> 的浅灰数字）。<br>
  <b>生成时间：</b>{datetime.now():%Y-%m-%d %H:%M}
</div>
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)


def print_matrix(grid, pooled, kind, title):
    print(f"\n{title}")
    print("=" * 100)
    print("点数 |" + "".join(f"{d:>9}" for d in DEALER_KEYS))
    print("-" * 100)
    for score in SCORES:
        line, any_data = f"{score:>4} |", False
        for dealer in DEALER_KEYS:
            g = grid[(score, dealer, kind)]
            if not g:
                fb = pooled.get((score, dealer))
                line += f"{(f'{fb:.2f}~' if fb else '·'):>9}"
                continue
            any_data = True
            if g.get("fallback"):
                line += f"{g['mult']:>8.2f}~"
                continue
            mark = {"confirmed": "", "suspect": "!"}.get(g["conf"], "*")
            line += f"{g['mult']:>8.2f}{mark}"
        if any_data:
            print(line)


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--dir", default=os.path.join(here, "samples"))
    args = ap.parse_args()

    rows = bt.load_samples(args.dir)
    cells, pair_rank = build3(rows)
    grid = collect3(cells)
    pooled = collect_pooled(grid)
    clean_pairs(grid, pooled)

    csv_out = os.path.join(args.dir, "eco_table3.csv")
    html_out = os.path.join(args.dir, "eco_table3.html")
    write_csv(grid, pooled, csv_out)
    write_html(grid, pooled, html_out, len(rows))

    for k, cn in KINDS:
        print_matrix(grid, pooled, k, f"{cn} —— 兑现倍数")

    # 对子溢价汇总
    print("\n对子 vs 硬牌（同点数同明牌，列差 ≥0.03 的）")
    print("-" * 74)
    rows2 = []
    for score in SCORES:
        for dealer in DEALER_KEYS:
            p, h = grid[(score, dealer, "pair")], grid[(score, dealer, "hard")]
            if p and h and not p.get("fallback"):
                rows2.append((p["mult"] - h["mult"], score, dealer, p["mult"], h["mult"]))
    rows2.sort(reverse=True)
    for d, sc, dl, pm, hm in rows2[:8]:
        print(f"  {sc:>2} 点 vs {dl:<2}  对子 {pm:.2f} vs 硬牌 {hm:.2f}  溢价 {d:+.2f}")
    if rows2:
        print(f"\n  可比格 {len(rows2)} 个，平均溢价 {sum(r[0] for r in rows2) / len(rows2):+.3f}，"
              f"正溢价 {(sum(1 for r in rows2 if r[0] > 0.02))} 格 / 负溢价 "
              f"{(sum(1 for r in rows2 if r[0] < -0.02))} 格")

    print(f"\n[done] 三态表已写入\n  {csv_out}\n  {html_out}")


if __name__ == "__main__":
    main()
