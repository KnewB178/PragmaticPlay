/**
 * PP AutoBot · 余额同步到 Google 表格
 * ─────────────────────────────────────────────────────────────────────
 * ⚠ 本文件是「追加」用的 —— 不要覆盖、也不要删掉你原来的 doPost。
 *    doPost 还在服务别的平台，两条线互不干扰：
 *
 *      doPost     别的平台推过来  →  写 live 页   （A 名字 / B 余额 / C 时间）
 *      本脚本     Google 定时来拉  →  写 pp_live / pp_summary 两页
 *
 *    本脚本刻意**不碰 live 页**：那一页有别的平台在用，
 *    而本脚本是整页覆盖式刷新，写进去会把别家的记录清掉。
 *
 *    同一个 Apps Script 项目里，各个脚本文件共享全局作用域，
 *    所以本文件的变量一律带 PP_ 前缀、辅助函数带下划线后缀，避免和别的文件撞名。
 *
 * ── 安装（只做一次）─────────────────────────────────────────────────
 *   1. 打开 Google 表格 → 扩展程序 → Apps Script
 *   2. 左侧「文件」旁边的 + → 「脚本」→ 命名 sheet_sync
 *      ★ 是新建一个文件；原来的 Code.gs（doPost）一个字都不用动
 *   3. 把本文件全部内容粘进去 → 保存
 *   4. 「运行」→ 选 ppPullBalance → 首次会要授权，同意
 *        跑完表格里应多出 pp_live / pp_summary 两页
 *   5. 「运行」→ 选 ppInstallTrigger → 之后每分钟自动拉
 *
 *   想暂停     运行 ppRemoveTrigger
 *   想手动刷一次 运行 ppPullBalance
 *
 * ── 数据源 ──────────────────────────────────────────────────────────
 *   http://pp.knewb2u.com:8899/api/clients   （一次返回所有机器）
 *   请求次数只跟「多久拉一次」有关，跟 VPS 台数无关：
 *   2 台 → 1 次/分钟 ；30 台 → 还是 1 次/分钟
 * ─────────────────────────────────────────────────────────────────────
 */

// ── 配置 ─────────────────────────────────────────────────────────────
var PP_SHEET_ID   = "1WYbkQtfDYE5rmHpeg2k7D1kKOmfHK8Ht5yMwAcFNCQQ";  // 就是你现在这张表
var PP_API_URL    = "http://pp.knewb2u.com:8899/api/clients";
var PP_SHEET_MAIN = "pp_live";      // 明细页（本脚本专用，每次整页覆盖）
var PP_SHEET_SUM  = "pp_summary";   // 按币种合计页（本脚本专用）
var PP_EVERY_MIN  = 1;              // 拉取间隔（分钟）。Google 只允许 1 / 5 / 10 / 15 / 30
var PP_TZ         = "Asia/Kuala_Lumpur";   // 显式指定，不看项目设置，避免差 8 小时
var PP_NUM_FMT    = "#,##0.00";     // 金额显示格式

var PP_ALLOWED_MIN = [1, 5, 10, 15, 30];   // Google 的硬限制，别改成 2 / 3


// ══════════════════════════ 主流程 ══════════════════════════
function ppPullBalance() {
  var ss = SpreadsheetApp.openById(PP_SHEET_ID);

  // 1) 拉数据
  var resp;
  try {
    resp = UrlFetchApp.fetch(PP_API_URL, { muteHttpExceptions: true, followRedirects: true });
  } catch (e) {
    Logger.log("[sheet] 拉取失败（网络问题）: " + e);
    return;
  }
  var code = resp.getResponseCode();
  var text = resp.getContentText();
  if (code !== 200) {
    Logger.log("[sheet] 拉取失败 HTTP " + code + " → " + text.slice(0, 200));
    return;   // 不写表：保留上一版数据，别把表格刷成空的
  }
  var d;
  try {
    d = JSON.parse(text);
  } catch (e) {
    Logger.log("[sheet] 返回的不是 JSON（可能被拦了 / 服务没起）→ " + text.slice(0, 200));
    return;
  }

  writeMain_(ss, d);
  writeSummary_(ss, d);
  Logger.log("[sheet] 已更新 " + (d.clients || []).length + " 台 · 看板时间 " + d.now);
}


// ══════════════════════════ 明细页 ══════════════════════════
function writeMain_(ss, d) {
  var rows = d.clients || [];
  var out = [["名称", "余额", "币种", "当前桌", "状态", "最后更新", "距今", "在线", "备注"]];

  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    out.push([
      r.client_id || "",
      (r.balance === null || r.balance === undefined) ? "" : Number(r.balance),
      r.currency || "",
      r.table || "",
      r.status || "",
      r.last_seen || "",
      agoText_(r.age),
      r.online ? "在线" : "掉线",
      r.reason || ""
    ]);
  }

  var sh = getSheet_(ss, PP_SHEET_MAIN);
  sh.clear();
  sh.getRange(1, 1, out.length, out[0].length).setValues(out);
  if (out.length > 1) {
    sh.getRange(2, 2, out.length - 1, 1).setNumberFormat(PP_NUM_FMT);
  }
  sh.getRange(1, 1, 1, out[0].length).setFontWeight("bold");
  sh.setFrozenRows(1);
}


// ══════════════════════════ 合计页 ══════════════════════════
function writeSummary_(ss, d) {
  var rows    = d.clients || [];
  var totals  = d.totals || {};
  var pnlTot  = d.pnl_totals || {};
  var showPnl = !!d.show_pnl;

  var head = ["币种", "余额合计"];
  if (showPnl) head.push("今日盈亏合计");
  head.push("在线台数");

  var width = head.length;
  var out = [head];

  var curs = [];
  for (var k in totals) { if (totals.hasOwnProperty(k)) curs.push(k); }
  curs.sort();

  for (var i = 0; i < curs.length; i++) {
    var cur = curs[i];
    var onlineN = 0;
    for (var j = 0; j < rows.length; j++) {
      if (rows[j].online && curOf_(rows[j]) === cur) onlineN++;
    }
    var line = [cur, Number(totals[cur])];
    if (showPnl) line.push(pnlTot.hasOwnProperty(cur) ? Number(pnlTot[cur]) : "");
    line.push(onlineN);
    out.push(pad_(line, width));
  }

  out.push(pad_([], width));
  out.push(pad_(["在线 / 总数", (d.online || 0) + " / " + (d.count || 0)], width));
  out.push(pad_(["本表拉取时间", Utilities.formatDate(new Date(), PP_TZ, "yyyy-MM-dd HH:mm:ss")], width));
  out.push(pad_(["看板时间", d.now || ""], width));
  out.push(pad_(["心跳 / 掉线判定", "每 " + (d.heartbeat || "?") + " 秒心跳；超 " + (d.offline_after || "?") + " 秒未上报 = 掉线"], width));
  out.push(pad_(["数据源", PP_API_URL], width));
  if (!showPnl) {
    out.push(pad_(["提示", "今日盈亏未开启（看板 SHOW_PNL=False），故本表不显示该列"], width));
  }

  var sh = getSheet_(ss, PP_SHEET_SUM);
  sh.clear();
  sh.getRange(1, 1, out.length, out[0].length).setValues(out);
  if (curs.length > 0) {
    sh.getRange(2, 2, curs.length, 1).setNumberFormat(PP_NUM_FMT);
    if (showPnl) sh.getRange(2, 3, curs.length, 1).setNumberFormat(PP_NUM_FMT);
  }
  sh.getRange(1, 1, 1, out[0].length).setFontWeight("bold");
}


// ══════════════════════════ 定时触发器 ══════════════════════════
function ppInstallTrigger() {
  ppRemoveTrigger();
  var n = PP_EVERY_MIN;
  if (PP_ALLOWED_MIN.indexOf(n) < 0) {
    for (var i = 0; i < PP_ALLOWED_MIN.length; i++) {
      if (PP_ALLOWED_MIN[i] >= n) { n = PP_ALLOWED_MIN[i]; break; }
    }
    Logger.log("[sheet] PP_EVERY_MIN=" + PP_EVERY_MIN + " 不被 Google 支持，已改用 " + n + " 分钟");
  }
  ScriptApp.newTrigger("ppPullBalance").timeBased().everyMinutes(n).create();
  Logger.log("[sheet] 定时器已装好：每 " + n + " 分钟拉一次");
}

function ppRemoveTrigger() {
  var n = 0;
  var ts = ScriptApp.getProjectTriggers();
  for (var i = 0; i < ts.length; i++) {
    if (ts[i].getHandlerFunction() === "ppPullBalance") {
      ScriptApp.deleteTrigger(ts[i]);
      n++;
    }
  }
  Logger.log("[sheet] 已移除 " + n + " 个定时器");
}


// ══════════════════════════ 小工具 ══════════════════════════
function getSheet_(ss, name) {
  var sh = ss.getSheetByName(name);
  return sh ? sh : ss.insertSheet(name);
}

// 与看板一致：空币种当作 "—"
function curOf_(r) {
  var c = (r.currency || "").replace(/^\s+|\s+$/g, "").toUpperCase();
  return c || "—";
}

function agoText_(sec) {
  var s = Number(sec);
  if (!s || s < 0) return "";
  if (s < 60) return Math.round(s) + " 秒前";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  return Math.floor(s / 3600) + " 小时前";
}

// setValues 要求矩形：把短行补齐
function pad_(arr, width) {
  var a = arr.slice();
  while (a.length < width) a.push("");
  return a.slice(0, width);
}
