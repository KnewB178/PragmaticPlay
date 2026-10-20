"""自动登录 198198.io，进入 Pragmatic Play 真人桌并导出 wss URL。"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import login_198198 as m
from playwright.sync_api import sync_playwright


OUTPUT = Path(__file__).resolve().parent / "WS_Config.txt"
BACCARAT_KEYWORDS = ("baccarat", "百家乐")
PRAGMATIC_COMPANY_ID = 123


def required_session_count() -> int:
    table_file = Path(__file__).resolve().parent / "table_id.txt"
    try:
        with table_file.open(encoding="utf-8") as f:
            count = sum(1 for line in f if "|" in line)
    except OSError:
        print("[WARNING] 无法读取 table_id.txt，按至少 1 个会话继续")
        return 1
    return math.ceil(count / 6)


def pick_pragmatic_live_game(games: list[dict]) -> dict:
    fallback = None
    for game in games:
        if game.get("category") != "Live Casino":
            continue
        if game.get("gameCompanyId") != PRAGMATIC_COMPANY_ID:
            continue
        if fallback is None:
            fallback = game
        haystack = f"{game.get('code') or ''} {game.get('name') or ''}".lower()
        if any(keyword in haystack for keyword in BACCARAT_KEYWORDS):
            return game
    if fallback is None:
        raise RuntimeError("这个账号的 Pragmatic Play 游戏列表为空。")
    print(
        "没有找到百家乐，改用 Pragmatic Play 真人游戏:",
        fallback["name"],
    )
    return fallback


def is_game_url(url: str) -> bool:
    return "game?" in url and "JSESSIONID=" in url


def capture_wss_urls(load_url: str, timeout: int = 90) -> list[str]:
    urls: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.on("websocket", lambda ws: urls.append(ws.url))
        try:
            page.goto(load_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline and not any(is_game_url(url) for url in urls):
            page.wait_for_timeout(2000)
        if any(is_game_url(url) for url in urls):
            page.wait_for_timeout(4000)
        browser.close()

    unique = list(dict.fromkeys(url for url in urls if is_game_url(url)))

    def sort_key(url: str) -> int:
        if "tableId=" in url:
            return 0
        if "/BJ" in url or "BJ-" in url:
            return 1
        if "ws1.pplc001.net" in url:
            return 2
        return 3

    unique.sort(key=sort_key)
    return unique


def process_account(cfg: dict, username: str, game_keyword: str | None, timeout: int) -> list[str]:
    print(f"[{username}] 开始处理")
    account_cfg = dict(cfg)
    account_cfg["username"] = username
    session = m.make_session(account_cfg["base_url"])
    session.get(account_cfg["base_url"] + "/", timeout=30)
    user = m.login(session, account_cfg)
    game_session = m.make_game_session(account_cfg["base_url"], user["token"])
    games = m.list_games(game_session, account_cfg["base_url"])

    if game_keyword:
        matches = m.find_games(games, game_keyword)
        if len(matches) != 1:
            raise RuntimeError(f"找到 {len(matches)} 个匹配，请用更精确的名称或代码。")
        game = matches[0]
    else:
        game = pick_pragmatic_live_game(games)

    print(f"[{username}] 游戏: {game['name']} ({game['code']})")
    last_error = ""
    for attempt in range(3):
        try:
            load_url = m.play_game(game_session, account_cfg["base_url"], game)
            urls = capture_wss_urls(load_url, timeout=timeout)
            if urls:
                print(f"[{username}] 抓取完成: {len(urls)} 个 wss")
                return urls
            last_error = "没有抓到 wss 连接。"
            print(f"[{username}] 第 {attempt + 1} 次未抓到，自动重试")
        except Exception as exc:
            last_error = str(exc)
            print(f"[{username}] 第 {attempt + 1} 次失败: {exc}")
        time.sleep(2)
    raise RuntimeError(last_error)


def main() -> None:
    parser = argparse.ArgumentParser(description="获取 198198.io 真人游戏桌的 wss URL")
    parser.add_argument(
        "--game",
        metavar="CODE_OR_NAME",
        help="指定游戏代码/名称（默认自动选 Pragmatic Play 百家乐）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="等待 wss 连接的最大秒数，默认 90",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="同时处理的账号数量，默认 3",
    )
    args = parser.parse_args()

    cfg = m.load_config()
    if not cfg["usernames"]:
        raise SystemExit("请在 config.ini 中填写账号配置。")

    results: dict[str, list[str]] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_account,
                cfg,
                username,
                args.game,
                args.timeout,
            ): username
            for username in cfg["usernames"]
        }
        for future in as_completed(futures):
            username = futures[future]
            try:
                results[username] = future.result()
            except Exception as exc:
                errors[username] = str(exc)
                print(f"[{username}] 失败: {exc}")

    with OUTPUT.open("w", encoding="utf-8") as f:
        for username in cfg["usernames"]:
            if username in results:
                f.write(results[username][0] + "\n")

    print("已写入:", OUTPUT)
    failed = [username for username in cfg["usernames"] if username in errors]
    if failed:
        print(f"[WARNING] 以下账号失败: {', '.join(failed)}")

    needed = required_session_count()
    if len(results) < needed:
        raise SystemExit(
            f"只抓到 {len(results)} 个会话，低于需要的 {needed} 个，停止启动 Build_WS_URLs.py。"
        )
    if failed:
        print(f"会话数量足够（{len(results)} >= {needed}），继续下一步")

    script_dir = Path(__file__).resolve().parent
    print("正在启动 Build_WS_URLs.py ...")
    subprocess.run(
        [sys.executable, str(script_dir / "Build_WS_URLs.py")],
        cwd=script_dir,
    )


if __name__ == "__main__":
    main()
