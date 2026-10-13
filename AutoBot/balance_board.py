#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
balance_board.py — 多客户端余额汇总看板（零依赖，只用 Python 标准库）

用途：程序会在多台 VPS 上跑，这个服务集中收各个客户端的余额，
      老板用浏览器打开就能看到每台的钱、今日盈亏、在跑哪张桌、还活着没。

命名：客户端会上报一个「机器指纹」(machine_id)；服务端按首次上报的先后自动发号
      VPS-1 / VPS-2 / VPS-3 …（同指纹永远同一个号）→ 每台 VPS 什么都不用配。
      若客户端手填了 client_id，则以其为准（旧行为，可回滚）。

跑法（在那台常开的服务器上）：
    python3 balance_board.py              # 默认 0.0.0.0:8899
    python3 balance_board.py 9000         # 换端口
    nohup python3 balance_board.py > board.log 2>&1 &     # 后台常驻

端点：
    POST /report       客户端上报（必须带 token）
    GET  /api/clients  看板数据（JSON，供页面自动刷新）
    GET  /             网页看板（自己每 3 秒刷新一次）

数据落盘在同目录 balance_state.json —— 服务重启不丢历史。
"""

import hmac
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ══════════════════════════ 配置 ══════════════════════════
# 必须与客户端 bot_config.ini 里的 report_token 完全一致，否则一律拒绝
TOKEN = "160b928a4caa227b44aab5fa7adc5928"
PORT = 8899                     # 与 relay 的 8888 错开
OFFLINE_AFTER = 60              # 超过这个秒数没收到上报 → 看板整行标红
HEARTBEAT_HINT = 30             # 客户端心跳间隔（仅用于页面提示，服务端不强制）
# 「今日盈亏」开关
#   False = 不显示（默认）—— 起点只能取「当天第一次收到上报时的余额」，
#           充值 / 提现会被当成盈亏算进去 → 数字不准，索性不显示
#   True  = 恢复显示（列 + 顶部合计）
SHOW_PNL = False
TZ = timezone(timedelta(hours=8))   # 看板按马来西亚时间分「天」（当日盈亏）

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "balance_state.json")

# ══════════════════════════ 数据 ══════════════════════════
_lock = threading.Lock()
_clients = {}    # 显示名 -> {balance, currency, table, status, reason, ts, day, open, pnl}
_machines = {}   # machine_id（机器指纹）-> 显示名「VPS-N」，按首次上报的先后发号
ORDER_LOCK = threading.Lock()


def _today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _load():
    global _clients, _machines
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if "clients" in data or "machines" in data:
                # 新格式：{clients:{…}, machines:{指纹 -> VPS-N}}
                _clients = data.get("clients") or {}
                _machines = data.get("machines") or {}
            else:
                # 旧格式（只有客户端表）：照读，指纹表从空开始
                _clients = data
            print(f"[board] 载入 {len(_clients)} 个客户端、{len(_machines)} 台机器指纹")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[board] 历史文件读取失败（忽略）: {e}")


def _save():
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"clients": _clients, "machines": _machines}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[board] 落盘失败（忽略）: {e}")


# ── 自动发号：机器指纹 → VPS-N（按首次上报的先后；同指纹永远同一个号）──
def _assign_name(mid: str) -> str:
    used = set(_machines.values())
    n = 1
    while f"VPS-{n}" in used:
        n += 1
    name = f"VPS-{n}"
    _machines[mid] = name
    print(f"[board] 新机器 {mid} → {name}")
    return name


def _resolve_name(p: dict) -> str:
    """
    决定这条上报在看板上叫什么：
      1. 手填了 client_id → 直接用（旧行为，可回滚）
      2. 没填但有 machine_id → 按指纹查号；没见过就发下一个（VPS-1 / VPS-2 …）
      3. 两者都没有 → 返回 ""（调用方按 400 拒掉）
    """
    cid = str(p.get("client_id") or "").strip()
    if cid:
        return cid
    mid = str(p.get("machine_id") or "").strip()
    if not mid:
        return ""
    with _lock:
        name = _machines.get(mid)
        if name is None:
            name = _assign_name(mid)
            _save()          # 立刻落盘，免得刚发的号因下次重启而丢
    return name


def _update(cid, p):
    """收到一条上报 → 更新该客户端记录（含当日盈亏）"""
    now = time.time()
    with _lock:
        rec = _clients.get(cid) or {}

        raw = p.get("balance")
        if raw is None or raw == "":
            bal = rec.get("balance")          # 停止上报没带余额 → 沿用上一次
        else:
            try:
                bal = float(raw)
            except (TypeError, ValueError):
                return False

        # 当日起点 / 盈亏照样记着（SHOW_PNL=True 时直接用，不用等重新累积），只是默认不上看板
        day = _today()
        if rec.get("day") != day or rec.get("open") is None:
            rec["day"] = day
            rec["open"] = bal                 # 当天第一次上报的余额 = 当日起点
        if rec.get("first_seen") is None:
            rec["first_seen"] = now

        rec.update({
            "balance": bal,
            "currency": (p.get("currency") or rec.get("currency") or "").strip(),
            "table": (p.get("table") or "").strip(),
            "status": (p.get("status") or "running").strip(),
            "reason": (p.get("reason") or "").strip(),
            "ts": now,
        })
        rec["pnl"] = None if bal is None else round(bal - float(rec["open"]), 2)
        _clients[cid] = rec
    _save()
    return True


def _snapshot():
    now = time.time()
    rows = []
    with _lock:
        items = list(_clients.items())
    for cid, r in items:
        age = now - float(r.get("ts") or 0)
        row = {
            "client_id": cid,
            "balance": r.get("balance"),
            "currency": r.get("currency") or "",
            "table": r.get("table") or "",
            "status": r.get("status") or "",
            "reason": r.get("reason") or "",
            "age": round(age, 1),
            "online": age <= OFFLINE_AFTER,
            "last_seen": datetime.fromtimestamp(float(r.get("ts") or 0), TZ)
                                 .strftime("%H:%M:%S") if r.get("ts") else "",
        }
        if SHOW_PNL:
            row["pnl"] = r.get("pnl")
        rows.append(row)
    # 自然排序：VPS-1 / VPS-2 / … / VPS-10（而不是 VPS-1、VPS-10、VPS-2）
    def _sk(r):
        m = re.match(r"^vps-(\d+)$", r["client_id"].lower())
        return (0, int(m.group(1)), "") if m else (1, 0, r["client_id"].lower())
    rows.sort(key=_sk)
    online = [x for x in rows if x["online"]]
    out = {
        "now": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "offline_after": OFFLINE_AFTER,
        "heartbeat": HEARTBEAT_HINT,
        "count": len(rows),
        "online": len(online),
        "total": round(sum(x["balance"] for x in online if x["balance"] is not None), 2),
        "currency": next((x["currency"] for x in rows if x["currency"]), ""),
        "clients": rows,
        "show_pnl": SHOW_PNL,
    }
    if SHOW_PNL:
        out["pnl_total"] = round(sum(x["pnl"] for x in online if x["pnl"] is not None), 2)
    return out


# ══════════════════════════ 网页 ══════════════════════════
PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>客户端余额看板</title>
<style>
  :root{
    --bg:#0f1216; --card:#161b22; --line:#242c37; --fg:#e6edf3; --dim:#8b949e;
    --up:#ff5d5d; --down:#3fb950; --warn:#f85149;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{max-width:1080px;margin:0 auto;padding:20px 16px 48px}
  h1{font-size:19px;margin:0 0 4px}
  .sub{color:var(--dim);font-size:12px;margin-bottom:16px}
  .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:12px 16px;min-width:150px;flex:1}
  .card .k{color:var(--dim);font-size:12px}
  .card .v{font-size:22px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
  table{width:100%;border-collapse:collapse;background:var(--card);
        border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);
        font-variant-numeric:tabular-nums;white-space:nowrap}
  th{color:var(--dim);font-weight:500;font-size:12px;background:#11161d}
  tr:last-child td{border-bottom:none}
  tr.off td{background:#3a1418}
  .num{text-align:right}
  .up{color:var(--up)} .down{color:var(--down)}
  .pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
        border:1px solid var(--line);color:var(--dim)}
  .pill.run{color:#58a6ff;border-color:#1f3d68}
  .pill.stop{color:#d29922;border-color:#5a4413}
  .pill.dead{color:var(--warn);border-color:#6e2020}
  body.hide-pnl .pnl{display:none}
  .empty{color:var(--dim);padding:28px;text-align:center}
  .err{color:var(--warn);font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>客户端余额看板</h1>
  <div class="sub" id="sub">加载中…</div>

  <div class="cards">
    <div class="card"><div class="k">在线客户端</div><div class="v" id="c_on">—</div></div>
    <div class="card"><div class="k">余额合计（仅在线）</div><div class="v" id="c_total">—</div></div>
    <div class="card pnl"><div class="k">今日盈亏合计</div><div class="v" id="c_pnl">—</div></div>
  </div>

  <table>
    <thead>
      <tr>
        <th>客户端</th><th class="num">余额</th><th>币种</th>
        <th class="num pnl">今日盈亏</th><th>当前桌</th><th>状态</th><th>最后更新</th>
      </tr>
    </thead>
    <tbody id="rows"><tr><td colspan="7" class="empty">还没有客户端上报…</td></tr></tbody>
  </table>
  <div class="err" id="err"></div>
</div>

<script>
const cid = 'clients';
function fmt(n, d){
  if(n === null || n === undefined) return '—';
  return Number(n).toLocaleString('en-US', {minimumFractionDigits: d===undefined?2:d,
                                            maximumFractionDigits: d===undefined?2:d});
}
function pnlCls(v){ if(v === null || v === undefined || v === 0) return ''; return v > 0 ? 'up' : 'down'; }
function pnlTxt(v){
  if(v === null || v === undefined) return '—';
  const s = v > 0 ? '+' : '';
  return s + fmt(v);
}
function ago(a){
  if(a < 60) return Math.round(a) + ' 秒前';
  if(a < 3600) return Math.floor(a/60) + ' 分钟前';
  return Math.floor(a/3600) + ' 小时前';
}
async function tick(){
  try{
    const r = await fetch('/api/clients', {cache:'no-store'});
    const d = await r.json();
    document.getElementById('sub').textContent =
      d.now + ' · 每 ' + d.heartbeat + ' 秒心跳，超过 ' + d.offline_after + ' 秒没上报即标红';
    document.getElementById('c_on').textContent = d.online + ' / ' + d.count;
    document.getElementById('c_total').textContent = fmt(d.total) + ' ' + (d.currency || '');
    document.body.classList.toggle('hide-pnl', !d.show_pnl);
    if(d.show_pnl){
      const pe = document.getElementById('c_pnl');
      pe.textContent = pnlTxt(d.pnl_total) + ' ' + (d.currency || '');
      pe.className = 'v ' + pnlCls(d.pnl_total);
    }

    const COLS = d.show_pnl ? 7 : 6;
    const tb = document.getElementById('rows');
    if(!d.clients.length){ tb.innerHTML = '<tr><td colspan="' + COLS + '" class="empty">还没有客户端上报…</td></tr>'; }
    else{
      tb.innerHTML = d.clients.map(c => {
        const st = c.online ? (c.status === 'stopped'
                    ? '<span class="pill stop">已停止</span>'
                    : '<span class="pill run">运行中</span>')
                  : '<span class="pill dead">掉线</span>';
        const t = c.table ? c.table : '—';
        return '<tr class="' + (c.online ? '' : 'off') + '">'
             + '<td>' + c.client_id + '</td>'
             + '<td class="num">' + fmt(c.balance) + '</td>'
             + '<td>' + (c.currency || '—') + '</td>'
             + '<td class="num pnl ' + pnlCls(c.pnl) + '">' + pnlTxt(c.pnl) + '</td>'
             + '<td>' + t + '</td>'
             + '<td>' + st + '</td>'
             + '<td>' + ago(c.age) + '</td></tr>';
      }).join('');
    }
    document.getElementById('err').textContent = '';
  }catch(e){
    document.getElementById('err').textContent = '读取失败（看板服务可能已停）: ' + e;
  }
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""


# ══════════════════════════ HTTP ══════════════════════════
class Handler(BaseHTTPRequestHandler):
    server_version = "BalanceBoard/1.0"

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/clients":
            self._json(200, _snapshot())
        elif path == "/health":
            self._json(200, {"ok": True, "clients": len(_clients)})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/report":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                raise ValueError("bad length")
            raw = self.rfile.read(n)
            p = json.loads(raw.decode("utf-8"))
            if not isinstance(p, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            self._json(400, {"ok": False, "error": f"bad request: {e}"})
            return

        # 鉴权：token 必须一致（定长比较，避免时序泄漏）
        if not hmac.compare_digest(str(p.get("token") or ""), TOKEN):
            self._json(403, {"ok": False, "error": "bad token"})
            print(f"[board] 拒绝：token 不符  from {self.client_address[0]}")
            return

        cid = _resolve_name(p)
        if not cid or len(cid) > 64:
            self._json(400, {"ok": False, "error": "bad client_id"})
            return

        if not _update(cid, p):
            self._json(400, {"ok": False, "error": "bad balance"})
            return

        rec = _clients.get(cid) or {}
        print(f"[board] {cid}: {rec.get('balance')} {rec.get('currency','')} "
              f"({rec.get('status')}, pnl {rec.get('pnl')})")
        self._json(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass          # 静音默认访问日志（本服务自己打印要点）


def main():
    port = PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"[board] 端口参数无效: {sys.argv[1]}")
            return
    _load()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"[board] 看板已启动 → http://0.0.0.0:{port}/   (状态文件 {STATE_FILE})")
    print(f"[board] 上报端点 POST /report   token {TOKEN[:6]}…{TOKEN[-4:]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[board] 停止")
    finally:
        _save()
        srv.server_close()


if __name__ == "__main__":
    main()
