// ═══════════════════════════════════════════════════════════════════════════
//  【备份】原来的 doPost —— 提，没改之前线上跑的就是这一份。
//  留着只是为了「万一想回滚」，平时不用动它。
//  （这一份的逻辑：一行一行 getRange().getValue() 找 machine_id）
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

  for (var i = 1; i <= lastRow; i++) {
    if (sheet.getRange(i, 1).getValue() === machineId) {
      sheet.getRange(i, 2).setValue(balance);
      sheet.getRange(i, 3).setValue(timestamp);
      found = true;
      break;
    }
  }

  if (!found) {
    sheet.appendRow([machineId, balance, timestamp]);
  }

  return ContentService.createTextOutput("");
}
