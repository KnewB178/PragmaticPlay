// ═══════════════════════════════════════════════════════════════════════════
//  「live」页的 doPost —— 提速版（逻辑一字未改，只是不再「一行读一次」）
//
//  【为什么要动它】
//  原版在 for 循环里，每一行都调用一次 sheet.getRange().getValue()；
//  每一次都是一趟「脚本 ↔ 表格服务」的来回。表一长（几千行）就是几千趟，
//  跑几十秒到几分钟都回不来 —— 客户端那边就只能超时放弃，
//  表现就是「推上去了，但表格没更新」。
//
//  【实测证据 · 2026-10-13，从这个 doPost 的地址直接打】
//    · GET  /exec                            → HTTP 200，0.95s
//    · POST 空 body（JSON.parse 就抛错，不碰表） → HTTP 200，0.90s
//      （返回页面写着 TypeError: Cannot read properties of undefined
//        → 说明 doPost 确实在跑，就是这个函数）
//    · POST 合法 JSON（会走到读表那一段）      → 超过 150 秒仍不返回 ❌
//  同样的 POST 发去 httpbin / postman-echo / www.google.com 都是秒回，
//  所以不是网络问题，卡住的就是「读表」这一段。
//
//  【怎么用】
//  1. 打开那张 Google 表格 → 扩展程序 → Apps Script
//  2. 找到你原来的 doPost，把这个函数整段覆盖上去（原来的逻辑保留，只是换了读法）
//  3. 保存 → 部署 → 管理部署 → 编辑 → 版本选「新版本」→ 部署
//     （★ 一定要重新部署成新版本，否则线上跑的还是旧代码）
//  4. 不用改客户端、不用改 URL —— 地址一模一样
//
//  改完可以让我从这边再打一次 POST 复测：正常应该在 1~3 秒内返回 200。
// ═══════════════════════════════════════════════════════════════════════════

function doPost(e) {
  var data = JSON.parse(e.postData.contents);
  var machineId = data.machine_id;
  var balance = data.balance;
  var spreadsheet = SpreadsheetApp.openById("1WYbkQtfDYE5rmHpeg2k7D1kKOmfHK8Ht5yMwAcFNCQQ");
  var sheet = spreadsheet.getSheetByName("live");
  var timestamp = new Date();

  var lastRow = sheet.getLastRow();
  var found = false;

  // ★★ 唯一改动：A 列整列一次读进来（原来是 for 里一行一次 getRange().getValue()）
  if (lastRow > 0) {
    var colA = sheet.getRange(1, 1, lastRow, 1).getValues();
    for (var i = 0; i < colA.length; i++) {
      if (colA[i][0] === machineId) {
        // 命中：B、C 两列一次写完（原来是两次 setValue）
        sheet.getRange(i + 1, 2, 1, 2).setValues([[balance, timestamp]]);
        found = true;
        break;
      }
    }
  }

  // 没命中 → 追加一行（跟原来完全一样）
  if (!found) {
    sheet.appendRow([machineId, balance, timestamp]);
  }

  return ContentService.createTextOutput("");
}
