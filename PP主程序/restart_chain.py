#!/usr/bin/env python3
"""一键刷新脚本：关闭 receiver + 旧 PPBJ -> 重建 URL -> 先启 receiver -> 再启 PPBJ。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
WSS_SCRIPT = BASE_DIR / "WSS.py"
PPBJ_SCRIPT = BASE_DIR / "PPBJ.py"
RECEIVER_SCRIPT = BASE_DIR / "pp_receiver_vps.py"
LOCK_FILE = BASE_DIR / "restart_chain.lock"

STOP_WAIT = 5
START_WAIT = 5
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 8881


def _force_utf8_when_redirected() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")


_force_utf8_when_redirected()


def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def log(msg: str) -> None:
    """只打印到控制台，不再写 restart_chain.log。
    （原来会往文件里追加，越跑越大；日志只是给人看的，不影响刷新流程。）"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)


def find_pids(script: str) -> list[int]:
    if os.name == "nt":
        cmd = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -like '*{script}*' -and $_.ProcessId -ne $PID }} | "
            "ForEach-Object { $_.ProcessId }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
    out = subprocess.run(
        ["pgrep", "-f", script],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [int(pid) for pid in out.split() if pid.strip().isdigit()]


def task_pid_exists(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == str(pid):
            return True
    return False


def stop_ppbj() -> None:
    pids = [pid for pid in find_pids("PPBJ.py") if pid != os.getpid()]
    if not pids:
        log("未发现运行中的 PPBJ.py")
        return
    log(f"停止 PPBJ.py, pid={pids}")
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], check=False)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            log(f"发送停止信号失败: {exc}")

    deadline = time.time() + 15
    while time.time() < deadline:
        if not find_pids("PPBJ.py"):
            log("PPBJ.py 已退出")
            return
        time.sleep(0.5)

    log("PPBJ.py 未在 15 秒内退出，强制结束")
    for pid in find_pids("PPBJ.py"):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], check=False)
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception as exc:
            log(f"强制结束失败: {exc}")


def stop_receiver() -> None:
    """刷新前先把现有 receiver 关掉（重启后由本脚本以最新代码重新拉起）。"""
    pids = [pid for pid in find_pids("pp_receiver_vps.py") if pid != os.getpid()]
    if not pids:
        log("未发现运行中的 pp_receiver_vps.py")
        return
    log(f"停止 pp_receiver_vps.py, pid={pids}")
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], check=False)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            log(f"发送停止信号失败: {exc}")

    deadline = time.time() + 15
    while time.time() < deadline:
        if not find_pids("pp_receiver_vps.py"):
            log("pp_receiver_vps.py 已退出")
            return
        time.sleep(0.5)

    log("pp_receiver_vps.py 未在 15 秒内退出，强制结束")
    for pid in find_pids("pp_receiver_vps.py"):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], check=False)
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception as exc:
            log(f"强制结束失败: {exc}")


def start_ppbj() -> None:
    log(f"等待 {START_WAIT} 秒后启动 PPBJ.py")
    time.sleep(START_WAIT)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    # PPBJ 是脱离进程启动的，它的屏幕输出没人接收 → 直接丢弃，不再写 restart_chain.log
    proc = subprocess.Popen(
        [PYTHON, str(PPBJ_SCRIPT)],
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=child_env(),
        **kwargs,
    )
    log(f"PPBJ.py 已启动, pid={proc.pid}")


def start_receiver() -> None:
    """以脱离进程方式拉起最新代码的 receiver（先于 PPBJ，网页先恢复）。"""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [PYTHON, str(RECEIVER_SCRIPT)],
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=child_env(),
        **kwargs,
    )
    log(f"pp_receiver_vps.py 已启动, pid={proc.pid}")


def acquire_lock() -> None:
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        stale = False
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            if os.name == "nt":
                stale = not task_pid_exists(pid)
            else:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    stale = True
                except PermissionError:
                    stale = False
                except OSError:
                    stale = True
        except (ValueError, OSError):
            stale = True
        if stale:
            LOCK_FILE.unlink(missing_ok=True)
            acquire_lock()
            return
        raise SystemExit("已有刷新任务在运行")
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def mqtt_available() -> bool:
    try:
        with socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=2):
            return True
    except OSError:
        return False


def main() -> None:
    acquire_lock()
    try:
        log("=" * 30)
        log("开始刷新 PP 主程序（含 receiver）")
        if not mqtt_available():
            log(f"[WARNING] MQTT broker 未监听 {MQTT_HOST}:{MQTT_PORT}，PPBJ 可能启动失败")
        stop_receiver()          # 先关 receiver（网页会短暂断开，稍后自动重连）
        stop_ppbj()
        log(f"等待 {STOP_WAIT} 秒后运行 WSS.py")
        time.sleep(STOP_WAIT)
        log("运行 WSS.py（内部会自动执行 Build_WS_URLs.py）...")
        result = subprocess.run([PYTHON, str(WSS_SCRIPT)], cwd=BASE_DIR, env=child_env())
        log(f"WSS.py 结束, 退出码={result.returncode}")
    finally:
        try:
            stop_ppbj()
            start_receiver()     # 先起 receiver → 网页先恢复
            time.sleep(2)        # 给 receiver 两秒监听端口/连 MQTT
            start_ppbj()         # 再起 PPBJ
        finally:
            release_lock()


if __name__ == "__main__":
    main()
