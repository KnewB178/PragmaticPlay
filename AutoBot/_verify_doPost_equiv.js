// 等价性验证：原版 doPost vs 提速版 doPost
// 用假的 Sheets 环境（记录每一次读写），对同一批输入跑两个版本，比最终表格状态。

function makeFakeSheet(rows /* 二维数组，模拟 live 页现有内容 */) {
  const calls = { reads: 0, writes: 0 };
  const grid = rows.map(r => r.slice());
  const ensure = (r, c) => {
    while (grid.length < r) grid.push([]);
    const row = grid[r - 1];
    while (row.length < c) row.push("");
  };
  return {
    grid, calls,
    getLastRow() { return grid.length; },
    getRange(row, col, numRows = 1, numCols = 1) {
      // 真实 Apps Script 里，任何 Range 都同时有 getValue/getValues/setValue/setValues
      return {
        getValue() {
          calls.reads++;
          if (numRows !== 1 || numCols !== 1) return this.getValues()[0][0];
          ensure(row, col); return grid[row - 1][col - 1];
        },
        getValues() {
          calls.reads++;
          const out = [];
          for (let r = row; r < row + numRows; r++) {
            const line = [];
            for (let c = col; c < col + numCols; c++) { ensure(r, c); line.push(grid[r - 1][c - 1]); }
            out.push(line);
          }
          return out;
        },
        setValue(v) { calls.writes++; ensure(row, col); grid[row - 1][col - 1] = v; return this; },
        setValues(vals) {
          calls.writes++;
          for (let i = 0; i < vals.length; i++)
            for (let j = 0; j < vals[i].length; j++) { ensure(row + i, col + j); grid[row + i - 1][col + j - 1] = vals[i][j]; }
          return this;
        }
      };
    },
    appendRow(arr) { calls.writes++; grid.push(arr.slice()); }
  };
}

function makeEnv(sheet) {
  return {
    SpreadsheetApp: { openById: () => ({ getSheetByName: () => sheet }) },
    ContentService: { createTextOutput: (s) => ({ s }) },
    JSON, Date
  };
}

const ORIGINAL = `function doPost(e) {
  var data = JSON.parse(e.postData.contents);
  var machineId = data.machine_id;
  var balance = data.balance;
  var spreadsheet = SpreadsheetApp.openById("X");
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
  if (!found) { sheet.appendRow([machineId, balance, timestamp]); }
  return ContentService.createTextOutput("");
}`;

const FAST = `function doPost(e) {
  var data = JSON.parse(e.postData.contents);
  var machineId = data.machine_id;
  var balance = data.balance;
  var spreadsheet = SpreadsheetApp.openById("X");
  var sheet = spreadsheet.getSheetByName("live");
  var timestamp = new Date();
  var lastRow = sheet.getLastRow();
  var found = false;
  if (lastRow > 0) {
    var colA = sheet.getRange(1, 1, lastRow, 1).getValues();
    for (var i = 0; i < colA.length; i++) {
      if (colA[i][0] === machineId) {
        sheet.getRange(i + 1, 2, 1, 2).setValues([[balance, timestamp]]);
        found = true;
        break;
      }
    }
  }
  if (!found) { sheet.appendRow([machineId, balance, timestamp]); }
  return ContentService.createTextOutput("");
}`;

function run(src, sheet, payload) {
  const env = makeEnv(sheet);
  const fn = new Function("SpreadsheetApp", "ContentService", src + "; return doPost;")(
    env.SpreadsheetApp, env.ContentService);
  fn({ postData: { contents: JSON.stringify(payload) } });
}

const cases = [
  { name: "空表 + 新名字",           rows: [],                                                            p: { machine_id: "1.2.3.4", balance: 100 } },
  { name: "已有行 + 命中第1行",       rows: [["1.2.3.4", 1, "t"]],                                        p: { machine_id: "1.2.3.4", balance: 999 } },
  { name: "已有行 + 命中第3行",       rows: [["a", 1, "t"], ["b", 2, "t"], ["1.2.3.4", 3, "t"]],          p: { machine_id: "1.2.3.4", balance: 777 } },
  { name: "已有行 + 没命中→追加",     rows: [["a", 1, "t"], ["b", 2, "t"]],                              p: { machine_id: "9.9.9.9", balance: 55 } },
  { name: "名字是数字型字符串",       rows: [[123, 1, "t"]],                                              p: { machine_id: 123, balance: 8 } },
  { name: "PP- 前缀与 EVO 纯IP共存",  rows: [["1.2.3.4", 1, "t"], ["PP-1.2.3.4", 2, "t"]],                p: { machine_id: "PP-1.2.3.4", balance: 42 } },
  { name: "中间有空格行",            rows: [["a", 1, "t"], ["", "", ""], ["PP-5.5.5.5", 3, "t"]],       p: { machine_id: "PP-5.5.5.5", balance: 11 } },
  { name: "balance 是字符串",        rows: [["x", 1, "t"]],                                              p: { machine_id: "x", balance: "88.5" } },
];

let pass = 0, fail = 0;
for (const c of cases) {
  const s1 = makeFakeSheet(c.rows), s2 = makeFakeSheet(c.rows);
  // 时间戳不同（Date 每次 new），比对时把 C 列的时间统一成 "TS"
  run(ORIGINAL, s1, c.p);
  run(FAST, s2, c.p);
  const norm = g => JSON.stringify(g.map(r => r.map((v, i) => (i === 2 && v instanceof Date) ? "TS" : v)));
  const ok = norm(s1.grid) === norm(s2.grid);
  console.log(`${ok ? "PASS" : "FAIL"}  ${c.name}`);
  if (!ok) {
    console.log("   原版:", norm(s1.grid));
    console.log("   提速:", norm(s2.grid));
    fail++;
  } else { pass++; }
  console.log(`      原版读写次数=${s1.calls.reads}/${s1.calls.writes}  提速版=${s2.calls.reads}/${s2.calls.writes}`);
}

// 大表性能对比（1000 行）
const big = []; for (let i = 0; i < 1000; i++) big.push([i === 999 ? "PP-9.9.9.9" : "host" + i, i, "t"]);
const b1 = makeFakeSheet(big), b2 = makeFakeSheet(big);
run(ORIGINAL, b1, { machine_id: "PP-9.9.9.9", balance: 1 });
run(FAST, b2, { machine_id: "PP-9.9.9.9", balance: 1 });
console.log(`\n1000 行时：原版读写 ${b1.calls.reads}/${b1.calls.writes} 次   提速版 ${b2.calls.reads}/${b2.calls.writes} 次`);
const nb = g => JSON.stringify(g.map(r => r.map((v, i) => (i === 2 && v instanceof Date) ? "TS" : v)));
console.log(`结果一致（时间戳归一化后）: ${nb(b1.grid) === nb(b2.grid)}`);
console.log(`\n总计 通过 ${pass} / 失败 ${fail}`);
