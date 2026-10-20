"""自动登录 198198.io 并获取指定页面的 URL。"""

from __future__ import annotations

import argparse
import base64
import configparser
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


CONFIG_PATH = Path(__file__).resolve().parent / "config.ini"

INDI_GAMES = [
    {"code": "ig-33", "name": "Teen Patti", "gameSupplierId": 7},
    {"code": "ig-34", "name": "Rummy", "gameSupplierId": 7},
    {"code": "ig-26", "name": "Hold Em Poker", "gameSupplierId": 7},
    {"code": "ig-45", "name": "7 Up Down", "gameSupplierId": 7},
    {"code": "ig-48", "name": "Jhandi Munda", "gameSupplierId": 7},
    {"code": "ig-31", "name": "Cowboy", "gameSupplierId": 7},
    {"code": "ig-32", "name": "Dragon Tiger", "gameSupplierId": 7},
    {"code": "ig-35", "name": "Andar Bahar", "gameSupplierId": 7},
    {"code": "ig-42", "name": "Bet 3 Patti", "gameSupplierId": 7},
    {"code": "ig-43", "name": "Slots", "gameSupplierId": 7},
    {"code": "ig-53", "name": "3 Patti Joker", "gameSupplierId": 7},
]


def load_config() -> dict:
    parser = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        raise SystemExit("找不到 config.ini，请先创建配置文件。")
    parser.read(CONFIG_PATH, encoding="utf-8")
    section = parser["198198"]
    username_prefix = section.get("username_prefix", "").strip()
    if username_prefix:
        try:
            start = int(section.get("username_start", "1"))
            end = int(section.get("username_end", "1"))
        except ValueError as exc:
            raise SystemExit("config.ini 的 username_start/username_end 必须是数字。") from exc
        if start < 1 or end < start:
            raise SystemExit("config.ini 的账号范围不正确。")
        usernames = [f"{username_prefix}{i:02d}" for i in range(start, end + 1)]
    else:
        usernames_raw = section.get("usernames", "").strip()
        username = section.get("username", "").strip()
        if usernames_raw:
            usernames = [
                name.strip() for name in usernames_raw.split(",") if name.strip()
            ]
        else:
            usernames = [username] if username else []
    return {
        "username": usernames[0] if usernames else "",
        "usernames": usernames,
        "password": section.get("password", ""),
        "base_url": section.get("base_url", "https://www.198198.io").rstrip("/"),
        "target_url": section.get("target_url", "").strip(),
    }


def encrypt_password(raw: str) -> str:
    key = b"89546sa547895326"
    cipher = AES.new(key, AES.MODE_ECB)
    padded = pad(raw.encode("utf-8"), AES.block_size)
    return base64.b64encode(cipher.encrypt(padded)).decode("utf-8")


def make_session(base_url: str) -> requests.Session:
    session = requests.Session()
    retry = Retry(total=4, backoff_factor=0.5, status_forcelist=[502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": base_url,
            "Referer": base_url + "/",
        }
    )
    return session


def make_game_session(base_url: str, token: str) -> requests.Session:
    """游戏接口专用会话，避免带上登录请求用的 Content-Type/Origin。"""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "language": "en-US",
            "token": token,
        }
    )
    return session


def request_data(
    session: requests.Session,
    method: str,
    base_url: str,
    path: str,
    **kwargs,
) -> dict:
    resp = session.request(method, base_url + path, timeout=30, **kwargs)
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 200:
        raise RuntimeError(f"接口 {path} 失败: {body.get('message') or body}")
    return body.get("data") or {}


def list_games(session: requests.Session, base_url: str) -> list[dict]:
    games: list[dict] = []
    seen: set[tuple] = set()
    filters = {"keyword": "", "gameBrandIdList": "-1", "gameTagIdList": "-1"}
    for category, path in (
        ("Live Casino", "/api/getLiveCainoGameList"),
        ("Slots", "/api/getSlotGameList"),
    ):
        data = request_data(session, "POST", base_url, path, params=filters)
        for group in data.get("list") or []:
            for game in group.get("gameList") or []:
                key = (
                    game.get("code"),
                    game.get("gameSupplierId"),
                    game.get("gameCompanyId"),
                )
                if key in seen:
                    continue
                seen.add(key)
                games.append(
                    {
                        "category": category,
                        "group": group.get("description"),
                        "code": game.get("code"),
                        "name": game.get("displayName") or game.get("name"),
                        "gameSupplierId": game.get("gameSupplierId"),
                        "gameCompanyId": game.get("gameCompanyId"),
                    }
                )
    for game in INDI_GAMES:
        games.append(
            {
                **game,
                "category": "IndiGames",
                "group": "IndiGames",
                "name": game["name"],
            }
        )
    return games


def find_games(games: list[dict], keyword: str) -> list[dict]:
    kw = keyword.strip().lower()
    exact = [
        g
        for g in games
        if str(g.get("code") or "").lower() == kw
    ]
    if exact:
        return exact
    return [
        g
        for g in games
        if kw in str(g.get("code") or "").lower()
        or kw in str(g.get("name") or "").lower()
    ]


def play_game(session: requests.Session, base_url: str, game: dict) -> str:
    params = {"game": game["code"], "gameSupplierId": game.get("gameSupplierId")}
    if game.get("gameCompanyId") is not None:
        params["gameCompanyId"] = game["gameCompanyId"]
    resp = session.post(base_url + "/api/playGame", params=params, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 200:
        raise RuntimeError(f"进入游戏失败: {body.get('message') or body}")
    return body.get("loadUrl")


def print_games(games: list[dict], keyword: str | None = None) -> None:
    matches = find_games(games, keyword) if keyword else games
    if not matches:
        print("没有找到匹配的游戏。")
        return
    for g in matches:
        company = ""
        if g.get("gameCompanyId") is not None:
            company = f" 公司={g['gameCompanyId']}"
        print(
            f"{g['category']:<12} {g['group'] or '':<14} "
            f"{g['code']:<12} {g['name'] or ''} "
            f"(供应商={g['gameSupplierId']}){company}"
        )


def login(session: requests.Session, cfg: dict) -> dict:
    encrypted = encrypt_password(cfg["password"])
    resp = session.post(
        cfg["base_url"] + "/api/v2/login/password",
        data={"account": cfg["username"], "password": encrypted},
        headers={"language": "en-US"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 200:
        raise RuntimeError(f"登录失败: {body.get('message') or body}")
    data = body.get("data") or {}
    if not data.get("token"):
        raise RuntimeError("登录失败: 响应中没有 token")
    session.headers["token"] = data["token"]
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="198198.io 自动登录/游戏工具")
    parser.add_argument(
        "--list-games",
        action="store_true",
        help="登录后列出可用游戏",
    )
    parser.add_argument(
        "--find",
        metavar="KEYWORD",
        nargs="+",
        help="按名称或代码过滤游戏列表",
    )
    parser.add_argument(
        "--game",
        metavar="CODE_OR_NAME",
        nargs="+",
        help="进入指定游戏并输出游戏 URL 及参数",
    )
    args = parser.parse_args()
    find_keyword = " ".join(args.find) if args.find else None
    game_keyword = " ".join(args.game) if args.game else None

    cfg = load_config()
    if not cfg["username"] or not cfg["password"]:
        raise SystemExit("请在 config.ini 中填写 username 和 password。")

    session = make_session(cfg["base_url"])
    session.get(cfg["base_url"] + "/", timeout=30)

    user = login(session, cfg)
    print("登录成功")
    print("会员:", user.get("name"))

    if args.list_games or args.game or args.find:
        game_session = make_game_session(cfg["base_url"], user["token"])
        games = list_games(game_session, cfg["base_url"])
        if game_keyword:
            matches = find_games(games, game_keyword)
            if not matches:
                raise SystemExit(f"没有找到游戏: {game_keyword}")
            if len(matches) > 1:
                print(f"找到 {len(matches)} 个匹配，请用更精确的名称或代码：")
                print_games(matches)
                raise SystemExit(1)
            game = matches[0]
            try:
                load_url = play_game(game_session, cfg["base_url"], game)
            except RuntimeError as exc:
                raise SystemExit(str(exc))
            print("游戏:", game["name"], f"({game['code']})")
            print("游戏 URL:", load_url)
            params = parse_qs(urlparse(load_url).query)
            simplified = {
                key: value[0] if len(value) == 1 else value
                for key, value in params.items()
            }
            print(
                "URL 参数:",
                json.dumps(simplified, ensure_ascii=False, indent=2),
            )
        else:
            print_games(games, find_keyword)
        return

    print("token:", user.get("token"))
    print("登录 token 已附加到会话，后续请求会带上登录态。")

    if cfg["target_url"]:
        page = session.get(cfg["target_url"], timeout=30)
        print("目标页面状态:", page.status_code)
        print("最终 URL:", page.url)
    else:
        print("未设置 target_url，跳过 URL 获取。")


if __name__ == "__main__":
    main()
