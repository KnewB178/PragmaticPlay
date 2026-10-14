#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_release.py —— 一条命令走完 PP_AutoBot.exe 的完整交付：

    打包(PyInstaller) → 自检(内嵌资源核对) → 上传 GitHub Release

用法（项目根目录下执行，用任意 Python 都行，脚本会自己找可用的构建环境）：

    python build_release.py                 # 打包 + 自检 + 上传（完整交付）
    python build_release.py --no-upload     # 只打包 + 自检，不上传
    python build_release.py --no-check      # 跳过自检（不推荐）
    python build_release.py --no-build      # 用 dist 里现有的 exe，只自检 + 上传

说明：
  · 构建环境不是项目里的 venv，而是独立的 ppbuild（见 BUILD_PY_CANDIDATES）。
    可用环境变量 PP_BUILD_PY 覆盖，或者先自己激活一个装了 pyinstaller+requests 的环境。
  · 上传走 GitHub REST API。token 取法（按顺序）：
      1) 环境变量 GH_TOKEN / GITHUB_TOKEN
      2) `git credential fill`（Git Credential Manager 里存的那份，即你 git push 用的凭据）
  · 上传策略：删掉 release 里同名的旧 asset → 传新的 → 回读校验。
    这样下载 URL 保持不变，PPbot.py 里的下载地址不用改。
"""

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

# ══════════════════════════ 配置 ══════════════════════════
ROOT = Path(__file__).resolve().parent          # 项目根
APP = ROOT / "AutoBot"                          # 源码目录
NAME = "PP_AutoBot"                             # exe 名（不含 .exe）
DIST = APP / "dist"                             # 产物目录
WORK = APP / "build"                            # 中间产物目录
EXE = DIST / f"{NAME}.exe"

# PyInstaller 要一起打进去的数据文件（源文件 ; 打进 exe 后的相对路径 .）
ADD_DATA = ["PP EV.py", "table_id.txt", "bot_config.ini", "pdev_local.py"]

# 自检：这几个文件必须原样出现在 exe 里
CHECK_FILES = ["bot_config.ini", "table_id.txt", "PP EV.py", "pdev_local.py"]

# GitHub Release 目标
GH_OWNER, GH_REPO = "KnewB178", "PragmaticPlay"
GH_TAG = "PP_AutoBot"                           # release 的 tag（下载 URL 用它）
GH_ASSET = f"{NAME}.exe"                        # release 上的资产名

# 构建环境候选（按顺序找第一个装了 pyinstaller + requests 的）
BUILD_PY_CANDIDATES = [
    os.environ.get("PP_BUILD_PY", ""),
    r"C:\Users\alien\.workbuddy\binaries\python\envs\ppbuild\Scripts\python.exe",
    str(APP / "venv" / "Scripts" / "python.exe"),   # 旧的项目 venv（若在你的机器上可用）
    sys.executable,
]


# ══════════════════════════ 工具 ══════════════════════════
def say(tag, msg):
    print(f"[{tag}] {msg}", flush=True)


def run(argv, **kw):
    kw.setdefault("cwd", str(ROOT))
    return subprocess.run(argv, **kw)


def find_build_py():
    """找一个装了 pyinstaller + requests 的解释器。"""
    for c in BUILD_PY_CANDIDATES:
        if not c or not Path(c).exists():
            continue
        p = subprocess.run([c, "-c", "import PyInstaller, requests"],
                           capture_output=True)
        if p.returncode == 0:
            return c
    say("FAIL", "找不到可用的构建环境（需要 pyinstaller + requests）。")
    say("FAIL", "请装好后重试，或用环境变量指定：set PP_BUILD_PY=<python.exe 路径>")
    for c in BUILD_PY_CANDIDATES:
        say("FAIL", f"  试过: {c or '(空)'}")
    sys.exit(1)


def pip_if_missing(build_py):
    """依赖缺失就补装（requirements.txt 里漏了 certifi，这里一并兜住）。"""
    need = []
    for mod, pkg in [("requests", "requests"), ("websockets", "websockets"),
                     ("colorama", "colorama"), ("numpy", "numpy"),
                     ("certifi", "certifi")]:
        p = subprocess.run([build_py, "-c", f"import {mod}"], capture_output=True)
        if p.returncode != 0:
            need.append(pkg)
    if need:
        say("deps", f"补装缺失依赖: {' '.join(need)}")
        run([build_py, "-m", "pip", "install", "-q", *need])


# ══════════════════════════ 1) 打包 ══════════════════════════
def build(build_py):
    say("build", f"构建环境 = {build_py}")
    pip_if_missing(build_py)
    try:
        EXE.unlink()
        say("build", f"删除旧产物 {EXE.name}")
    except FileNotFoundError:
        pass

    args = [
        build_py, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--console",
        "--log-level", "WARN",          # 压掉 PyInstaller 的 INFO 刷屏，只看警告/错误
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
    ]
    for f in ADD_DATA:
        args += ["--add-data", f"{APP / f};."]
    args.append(str(APP / "main.py"))

    say("build", "PyInstaller 打包中 ...")
    r = run(args)
    if r.returncode != 0 or not EXE.exists():
        say("FAIL", f"打包失败（退出码 {r.returncode}）")
        sys.exit(1)
    say("build", f"OK 产物 {EXE}  ({EXE.stat().st_size:,} bytes)")


# ══════════════════════════ 2) 自检 ══════════════════════════
def selfcheck(build_py):
    """解出 exe 里内嵌的数据文件，跟磁盘上的源文件逐字节比对。

    注意：onefile 的 exe 里数据是【压缩】存的，直接 grep 明文查不到内容 ——
          必须用 CArchiveReader 解包（这就是当初差点误判成『没打包进去』的原因）。
    """
    code = r"""
import hashlib, sys
from PyInstaller.archive.readers import CArchiveReader
exe, app = sys.argv[1], sys.argv[2]
a = CArchiveReader(exe)
bad = 0
for name in sys.argv[3:]:
    raw = a.extract(name)
    if isinstance(raw, tuple):
        raw = raw[1]
    data = bytes(raw) if isinstance(raw, (bytes, bytearray)) else raw.read()
    disk = open(app + "\\" + name, "rb").read()
    same = data == disk
    bad += 0 if same else 1
    print("  %s %-18s exe=%s disk=%s" % (
        "OK " if same else "BAD", name,
        hashlib.sha1(data).hexdigest()[:12], hashlib.sha1(disk).hexdigest()[:12]))
print("RESULT", bad)
"""
    say("check", "核对 exe 内嵌资源与磁盘源文件是否一致 ...")
    p = subprocess.run([build_py, "-c", code, str(EXE), str(APP), *CHECK_FILES],
                       capture_output=True, text=True)
    print(p.stdout.rstrip())
    if p.stderr.strip():
        print(p.stderr.rstrip())
    if p.returncode != 0 or not p.stdout.rstrip().endswith("RESULT 0"):
        say("FAIL", "自检不通过 → 不上传")
        sys.exit(1)
    say("check", "OK 内嵌资源全部一致")


# ══════════════════════════ 3) 上传 ══════════════════════════
def gh_token():
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            say("upload", f"用环境变量 {k}")
            return os.environ[k].strip()
    p = subprocess.run(["git", "credential", "fill"], cwd=str(ROOT),
                       input=b"protocol=https\nhost=github.com\n\n",
                       capture_output=True)
    for line in p.stdout.decode("utf-8", "ignore").splitlines():
        if line.lower().startswith("password="):
            say("upload", "用 git 里存的 GitHub 凭据")
            return line.split("=", 1)[1].strip()
    say("FAIL", "取不到 GitHub token（可设置 GH_TOKEN 环境变量）")
    sys.exit(1)


def upload(build_py):
    code = r"""
import os, sys, requests
owner, repo, tag, asset, exe = sys.argv[1:6]
size = os.path.getsize(exe)
tok = os.environ["_GH_TOK"]
H = {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"}
API = "https://api.github.com/repos/%s/%s" % (owner, repo)

r = requests.get(API + "/releases/tags/" + tag, headers=H, timeout=30)
if r.status_code != 200:
    print("取 release 失败:", r.status_code, r.text[:300]); sys.exit(1)
rel = r.json(); rid = rel["id"]
print("  release id=%s 现有资产=%s" % (rid, [a["name"] for a in rel.get("assets", [])]))

for a in rel.get("assets", []):
    if a["name"] == asset:
        d = requests.delete(API + "/releases/assets/%d" % a["id"], headers=H, timeout=30)
        print("  删除旧 %s -> %s" % (asset, d.status_code))

last = ""
for attempt in range(1, 4):
    with open(exe, "rb") as f:
        up = requests.post(
            "https://uploads.github.com/repos/%s/%s/releases/%d/assets" % (owner, repo, rid),
            headers=dict(H, **{"Content-Type": "application/octet-stream"}),
            params={"name": asset}, data=f, timeout=900)
    if up.status_code in (200, 201):
        print("  上传成功 size=%s state=%s" % (up.json().get("size"), up.json().get("state")))
        break
    last = "%s %s" % (up.status_code, up.text[:200])
    print("  第 %d 次上传失败: %s" % (attempt, last))
    if attempt == 3:
        print("FAIL", last); sys.exit(1)

chk = requests.get(API + "/releases/tags/" + tag, headers=H, timeout=30).json()
cur = [(a["name"], a["size"]) for a in chk.get("assets", [])]
print("  远端资产 =", cur)
if cur != [(asset, size)]:
    print("FAIL 校验不一致"); sys.exit(1)
print("OK %d" % size)
"""
    env = dict(os.environ, _GH_TOK=gh_token())
    say("upload", f"上传 {EXE.name} ({EXE.stat().st_size:,} bytes) -> release tag={GH_TAG} ...")
    p = subprocess.run([build_py, "-c", code, GH_OWNER, GH_REPO, GH_TAG, GH_ASSET, str(EXE)],
                       env=env, capture_output=True, text=True, cwd=str(ROOT))
    print(p.stdout.rstrip())
    if p.returncode != 0:
        if p.stderr.strip():
            print(p.stderr.rstrip())
        say("FAIL", "上传失败（本地 exe 还在 dist/，可重跑 --no-build 再传）")
        sys.exit(1)
    url = f"https://github.com/{GH_OWNER}/{GH_REPO}/releases/download/{GH_TAG}/{GH_ASSET}"
    say("upload", f"OK 下载地址 = {url}")


# ══════════════════════════ main ══════════════════════════
def main():
    ap = argparse.ArgumentParser(description="打包 PP_AutoBot.exe → 自检 → 上传 GitHub Release")
    ap.add_argument("--no-build", action="store_true", help="跳过打包，用 dist 里现有的 exe")
    ap.add_argument("--no-check", action="store_true", help="跳过自检")
    ap.add_argument("--no-upload", action="store_true", help="只打包+自检，不上传")
    args = ap.parse_args()

    os.chdir(ROOT)
    py = find_build_py()

    if not args.no_build:
        build(py)
    elif not EXE.exists():
        say("FAIL", f"--no-build 但 {EXE} 不存在")
        sys.exit(1)
    else:
        say("build", f"跳过打包，用现有 {EXE} ({EXE.stat().st_size:,} bytes)")

    if not args.no_check:
        selfcheck(py)

    if not args.no_upload:
        upload(py)
    else:
        say("upload", "已跳过（--no-upload）")

    say("done", "全部完成 ✅")


if __name__ == "__main__":
    main()
