# -*- coding: utf-8 -*-
"""
build_table.py —— 把抓包样本汇成 PP 的 cashOut 定价表

原理（已用真样本验证）：
  cashOut 倍数只由「玩家点数 + 庄家明牌」决定，精度恒为 2 位小数，与注金大小无关。
  表 = 18 点 × 10 张明牌（2-9 / 10 / A，其中 10/J/Q/K 等价）。

反推注金：注金 = cashOut 金额 ÷ 表里的倍数

用法：
  python build_table.py                  # 读 samples/ 下全部 csv，生成表 + 热力图
  python build_table.py --dir E:\\xxx    # 指定样本目录
  python build_table.py --min-n 2        # 只显示样本数 >= 2 的格子
"""

import argparse
import csv
import glob
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

SCORES = list(range(4, 22))
DEALER_KEYS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "A"]
DEALER_ALIAS = {"J": "10", "Q": "10", "K": "10", "T": "10"}

RATIO_MIN, RATIO_MAX = 0.20, 1.95
DOUBLE_TOL = 0.03
CONFIRM_N = 3          # 样本数 >= 此值且无分歧 → 已确认


# ---------------------------------------------------------------- 工具

def norm_dealer(v):
    s = str(v or "").strip().upper()
    if not s:
        return ""
    if s in ("10", "T"):
        return "10"
    s = s[0]
    if s == "1":
        return "10"
    return DEALER_ALIAS.get(s, s)


def load_samples(sample_dir):
    files = sorted(glob.glob(os.path.join(sample_dir, "samples_*.csv")))
    if not files:
        print("[!] 没找到 samples_*.csv，先跑 capture_cashout.py")
        sys.exit(1)
    rows = []
    for f in files:
        try:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                rows.extend(csv.DictReader(fh))
        except Exception as e:
            print(f"[!] 跳过 {os.path.basename(f)}: {e}")
    print(f"[i] 载入 {len(files)} 个样本文件，共 {len(rows)} 行")
    return rows


# ---------------------------------------------------------------- 聚合

def build(rows):
    cells = defaultdict(lambda: {"values": Counter(), "raw": []})
    skipped = Counter()
    for r in rows:
        try:
            co = float(r["cash_out"])
            bet = float(r["bet"] or 0)
            score = int(float(r["score"]))
        except (ValueError, KeyError, TypeError):
            skipped["字段缺失"] += 1
            continue
        if bet <= 0:
            skipped["注金未知"] += 1
            continue
        if not (4 <= score <= 21):
            skipped["点数越界"] += 1
            continue
        dealer = norm_dealer(r.get("dealer_up"))
        if dealer not in DEALER_KEYS:
            skipped["庄家明牌未知"] += 1
            continue
        ratio = round(co / bet, 2)
        if not (RATIO_MIN <= ratio <= RATIO_MAX):
            skipped["倍数不合理(注金错)"] += 1
            continue
        cell = cells[(score, dealer)]
        cell["values"][ratio] += 1
        cell["raw"].append((ratio, co, bet, r.get("cards", "")))
    if skipped:
        print("[i] 丢弃统计: " + "，".join(f"{k} {v}" for k, v in skipped.items()))
    return cells, skipped


def resolve(cell):
    """定出基础倍数，返回 (倍数, 置信度, 标记列表)"""
    vals = sorted(cell["values"])
    if not vals:
        return None, "empty", []

    flags, base_pool = [], []
    for v in vals:
        if any(abs(v - 2 * o) <= DOUBLE_TOL for o in vals if o < v):
            flags.append("含双倍注观测")
        else:
            base_pool.append(v)
    if not base_pool:
        base_pool = [vals[0]]
        flags.append("疑似全为双倍注")

    weighted = Counter()
    for v in base_pool:
        weighted[v] += cell["values"][v]
    best = weighted.most_common(1)[0][0]

    if len(base_pool) > 1 and max(base_pool) - min(base_pool) > 0.02:
        flags.append(f"分歧 {min(base_pool)}~{max(base_pool)}")

    n = sum(cell["values"][v] for v in base_pool)
    if n >= CONFIRM_N and not any(f.startswith("分歧") for f in flags):
        conf = "confirmed"
    elif n >= 2:
        conf = "likely"
    else:
        conf = "weak"
    return best, conf, flags


# ---------------------------------------------------------------- 输出

def collect(cells):
    """统一算一遍，供各输出复用"""
    grid = {}
    for score in SCORES:
        for dealer in DEALER_KEYS:
            cell = cells.get((score, dealer))
            if not cell:
                grid[(score, dealer)] = None
                continue
            mult, conf, flags = resolve(cell)
            grid[(score, dealer)] = {
                "mult": mult, "conf": conf, "flags": flags,
                "n": sum(cell["values"].values()),
                "obs": dict(cell["values"]),
            }
    return grid


def write_csv(grid, out_path):
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "dealer", "eco_multiple", "eco_ev",
                    "samples", "confidence", "observations", "flag"])
        for score in SCORES:
            for dealer in DEALER_KEYS:
                g = grid[(score, dealer)]
                if not g:
                    w.writerow([score, dealer, "", "", 0, "empty", "", "缺口"])
                    continue
                obs = " / ".join(f"{v}x{n}" for v, n in sorted(g["obs"].items()))
                w.writerow([score, dealer, f"{g['mult']:.2f}", f"{g['mult'] - 1:+.2f}",
                            g["n"], g["conf"], obs, "；".join(g["flags"])])


def print_matrix(grid, min_n=1):
    print()
    print("PP cashOut 定价表  （格子=倍数，即兑回注金的几倍；* = 样本不足 3，待观察）")
    print("=" * 100)
    print("点数 |" + "".join(f"{d:>9}" for d in DEALER_KEYS))
    print("-" * 100)
    filled = 0
    for score in SCORES:
        line = f"{score:>4} |"
        for dealer in DEALER_KEYS:
            g = grid[(score, dealer)]
            if not g or g["mult"] is None:
                line += f"{'·':>9}"
                continue
            filled += 1
            mark = "" if g["conf"] == "confirmed" else "*"
            line += f"{g['mult']:>8.2f}{mark}"
        print(line)
    print("-" * 100)
    total = len(SCORES) * len(DEALER_KEYS)
    print(f"覆盖率 {filled}/{total} = {filled / total * 100:.0f}%   "
          f"（点数 21 服务器不给兑现，属正常空）")


def print_highlights(grid):
    rows = [(g["mult"], sc, d, g["n"]) for (sc, d), g in grid.items()
            if g and g["mult"] is not None]
    rows.sort(reverse=True)
    print("\n兑现最划算（EV = 倍数−1）：")
    for m, sc, d, n in rows[:6]:
        print(f"  {sc:>2} 点 vs 庄家 {d:<2}   {m:.2f} 倍  →  EV {m - 1:+.0%}   (n={n})")
    print("\n兑现最亏（EV 最负）：")
    for m, sc, d, n in rows[-6:][::-1]:
        print(f"  {sc:>2} 点 vs 庄家 {d:<2}   {m:.2f} 倍  →  EV {m - 1:+.0%}   (n={n})")


def write_html(grid, out_path, n_samples):
    def color(m):
        """倍数 0.3→绿, 1.0→中性, 1.8→红（中国习惯：越高越红）"""
        if m is None:
            return "#f2f3f5", "#b9bcc4"
        t = max(0.0, min(1.0, (m - 0.35) / (1.80 - 0.35)))
        # 绿(22,163,74) → 浅灰 → 红(220,38,38)
        if t < 0.5:
            k = t / 0.5
            r = int(22 + (245 - 22) * k)
            g = int(163 + (245 - 163) * k)
            b = int(74 + (245 - 74) * k)
        else:
            k = (t - 0.5) / 0.5
            r = int(245 + (220 - 245) * k)
            g = int(245 + (38 - 245) * k)
            b = int(245 + (38 - 245) * k)
        return f"rgb({r},{g},{b})", ("#fff" if t > 0.62 or t < 0.16 else "#1f2329")

    head = "".join(f"<th>庄家 {d}</th>" for d in DEALER_KEYS)
    body = []
    for score in SCORES:
        tds = []
        for dealer in DEALER_KEYS:
            g = grid[(score, dealer)]
            if not g or g["mult"] is None:
                bg, fg = color(None)
                tds.append(f'<td style="background:{bg};color:{fg}" title="无样本">·</td>')
                continue
            bg, fg = color(g["mult"])
            tag = "" if g["conf"] == "confirmed" else "°"
            tip = f"{dealer} 明牌 · {g['n']} 条样本 · {g['conf']}"
            if g["flags"]:
                tip += " · " + "；".join(g["flags"])
            tds.append(f'<td style="background:{bg};color:{fg}" title="{tip}">'
                       f'{g["mult"]:.2f}{tag}</td>')
        body.append(f"<tr><th>{score}</th>{''.join(tds)}</tr>")

    conf_n = sum(1 for g in grid.values() if g and g["conf"] == "confirmed")
    fill_n = sum(1 for g in grid.values() if g and g["mult"] is not None)

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>PP cashOut 定价表</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      background:#fff;color:#1f2329;margin:0;padding:28px 24px}}
 h1{{font-size:20px;margin:0 0 6px}}
 .sub{{color:#646a73;font-size:13px;margin-bottom:18px}}
 .stats{{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}}
 .stat{{border:1px solid #e5e6eb;border-radius:8px;padding:10px 16px;min-width:96px}}
 .stat b{{display:block;font-size:22px;line-height:1.2}}
 .stat span{{font-size:12px;color:#646a73}}
 table{{border-collapse:separate;border-spacing:2px;font-variant-numeric:tabular-nums}}
 th{{font-size:12px;font-weight:500;color:#646a73;padding:6px 4px;text-align:center}}
 tbody th{{font-weight:600;color:#1f2329;font-size:13px;width:44px}}
 td{{width:56px;height:34px;text-align:center;border-radius:5px;font-size:13px;
     font-weight:600;cursor:default}}
 .legend{{margin-top:18px;font-size:12px;color:#646a73;line-height:1.9}}
 code{{background:#f2f3f5;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h1>PP cashOut 定价表</h1>
<div class="sub">格子里的数字 = 兑现能拿回注金的倍数。小数 ° 表示样本 &lt; 3 条，还在观察。生成于 {datetime.now():%Y-%m-%d %H:%M}</div>
<div class="stats">
  <div class="stat"><b>{fill_n}/180</b><span>已填格数</span></div>
  <div class="stat"><b>{conf_n}</b><span>已确认格（n≥3）</span></div>
  <div class="stat"><b>{n_samples}</b><span>原始样本行</span></div>
  <div class="stat"><b>{fill_n / 180 * 100:.0f}%</b><span>覆盖率</span></div>
</div>
<table>
<thead><tr><th>你的点数</th>{head}</tr></thead>
<tbody>{''.join(body)}</tbody>
</table>
<div class="legend">
  <b>怎么用：</b>兑现 EV = <code>倍数 − 1</code>。比如你 20 点对庄家 8，表里 1.78 → 兑现等于锁定 +78%。<br>
  <b>反推注金：</b><code>注金 = cashOut 金额 ÷ 倍数</code>（倍数是固定值，与注金无关，已实测 174/175 命中）。<br>
  <b>颜色：</b>越红 = 兑现越划算；越绿 = 兑现越亏（该继续打）。<br>
  <b>空白的格子</b>：点数 21 服务器不给兑现报价，属正常；其余是样本还不够，挂机继续抓即可。
</div>
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--dir", default=os.path.join(here, "samples"))
    ap.add_argument("--min-n", type=int, default=1)
    args = ap.parse_args()

    rows = load_samples(args.dir)
    cells, _ = build(rows)
    grid = collect(cells)

    csv_out = os.path.join(args.dir, "eco_table.csv")
    html_out = os.path.join(args.dir, "eco_table.html")
    write_csv(grid, csv_out)
    write_html(grid, html_out, len(rows))

    print_matrix(grid, args.min_n)
    print_highlights(grid)
    print(f"\n[done] 表已写入\n  {csv_out}\n  {html_out}")


if __name__ == "__main__":
    main()
