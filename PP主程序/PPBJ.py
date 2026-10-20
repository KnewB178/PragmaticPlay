import os, sys
import re
import sys
import time
import json
import asyncio
import warnings
import websockets
from datetime import datetime
import paho.mqtt.client as mqtt
from colorama import init, Fore, Style
from paho.mqtt.client import CallbackAPIVersion
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import signal

# ── PDEV 本地计算引擎（pdev_local.py，与 EVO 逐位一致）─────────────────────────────
# 引擎缺失/导入失败只退化：面板不显示 PDEV，绝不让主程序崩
try:
    from pdev_local import compute_pdev as _compute_pdev
except Exception:
    _compute_pdev = None

init(autoreset=True)
if sys.platform == "win32":
    os.system("title Pragmatic Play Blackjack")
else:
    sys.stdout.write("\x1b]0;Pragmatic Play Blackjack\x07")
warnings.filterwarnings("ignore", category=DeprecationWarning)

voip_state = {}
last_output = {}
issued_cards = {}
shoe_counter = {}
seat_players = {} 
seat_bets = {} 
game_counters = {}
mqtt_client = None
ws_clients = set()
loop = None

# ===================================================================================== #
#                             启动 MQTT 客户端（适用于 PP 与 EVO）                        
# ===================================================================================== #
def start_mqtt_client():
    global mqtt_client
    mqtt_client = mqtt.Client(protocol=mqtt.MQTTv311)  # ✅ 使用旧写法但屏蔽警告
    mqtt_client.username_pw_set("MQTT@@##admin", "MQTT@@##admin")
    mqtt_client.connect("127.0.0.1", 8881, 60)
    mqtt_client.loop_start()

# ===================================================================================== #
#                             加载 ws_config.txt 中所有完整链接                            
# ===================================================================================== #
def load_ws_urls(filename="ws_config.txt"):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            seen_tables = set()
            urls = []
            for line in f:
                url = line.strip()
                if not url:
                    continue
                table_id = parse_qs(urlparse(url).query).get("tableId", [""])[0]
                if table_id in seen_tables:
                    print(f"⚠️ 跳过重复桌号 {table_id}（同一桌只保留一条连接）")
                    continue
                seen_tables.add(table_id)
                urls.append(url)
            return urls
    except Exception as e:
        print(f"❌ Failed to read WebSocket config file {filename}: {e}")
        return []

# ===================================================================================== #
#                           加载 WebSocket 原始链接（纯文本）                                
# ===================================================================================== #
def load_base_url(filename="ws_config.txt"):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        print(f"❌ Failed to read WebSocket config file {filename}: {e}")
        return ""

# ===================================================================================== #
#                               替换 WebSocket 中的桌号参数                                  
# ===================================================================================== #
def modify_url(original_url, new_table_id):
    parsed = urlparse(original_url)
    query = parse_qs(parsed.query)
    query["tableId"] = [new_table_id]
    new_query = urlencode(query, doseq=True)
    new_parsed = parsed._replace(query=new_query)
    return urlunparse(new_parsed)

# ===================================================================================== #
#                        从 Table_ID.txt 加载 tableId → 桌名 映射表                         
# ===================================================================================== #
def load_table_name_map(filename="Table_ID.txt"):
    table_name_map = {}
    try:
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                parts = [x.strip() for x in line.strip().split("|")]
                if len(parts) >= 2:
                    name, table_id = parts[0], parts[1]
                    table_name_map[table_id] = name
    except Exception as e:
        print(f"❌ Failed to load Table_ID.txt: {e}")
    return table_name_map

# ===================================================================================== #
#                          向指定频道周期性发送 WebSocket Ping 心跳                         
# ===================================================================================== #
async def send_heartbeat(websocket, channel, table_name):
    try:
        while True:
            timestamp = str(int(time.time() * 1000))
            ping_str = f'<ping channel="{channel}" time="{timestamp}"/>'
            await websocket.send(ping_str)
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        return
    except Exception as e:
        print(f"[❌ Heartbeat Error] {table_name}: {e}")

# ===================================================================================== #
#                                控制台输出更新（覆盖模式）                              
# ===================================================================================== #
def console_print(table_name, message, row):
    global last_output
    key = f"{table_name}_{row}"

    if last_output.get(key) == message:
        return

    last_output[key] = message
    output = f"{table_name} {message}"

    pad = " " * max(0, 100 - len(output))  # 防止残影
    sys.stdout.write(f"\033[{row};0H{output}{pad}")
    sys.stdout.flush()

# ===================================================================================== #
#                                计算 True Count（真实点数）                            
# ===================================================================================== #
def get_true_count(all_cards):
    count_values = {
        "2": 1, "3": 1, "4": 1, "5": 1, "6": 1,
        "7": 0, "8": 0, "9": 0,
        "10": -1, "J": -1, "Q": -1, "K": -1, "A": -1
    }

    rc = sum(count_values.get(card, 0) for card in all_cards)
    decks_remaining = max(1, (416 - len(all_cards)) / 52)
    true_count = rc / decks_remaining
    return round(true_count, 2)

# ===================================================================================== #
#                        计算 PDEV（下一手期望值，跟 EVO 同一套口径）                     
# ===================================================================================== #
def get_pdev(all_cards):
    """PDEV（不含 eco）。已出牌为空 = 满靴值 [-0.0064769]。引擎缺失或算错都返回 None。"""
    if _compute_pdev is None:
        return None
    try:
        return _compute_pdev(all_cards or [])["pdev"]
    except Exception:
        return None

# ===================================================================================== #
#                         导出所有局数据到 TXT（含 True Count）                         
# ===================================================================================== #
EXPORT_TXT_ENABLED = False  # 暂时关闭 TXT 导出；改回 True 即恢复

def export_all_rounds_to_txt(table_name):
    if not EXPORT_TXT_ENABLED:
        return
    table = issued_cards.get(table_name, {})
    all_cards = table.setdefault("all_cards", [])

    game_keys = sorted(
        [k for k in table if k != "all_cards"],
        key=lambda k: int(k.split("_")[1])
    )
    if not game_keys:
        return

    folder_path = os.path.join("Table Data", table_name)
    os.makedirs(folder_path, exist_ok=True)

    existing_files = os.listdir(folder_path)
    pattern = re.compile(rf"Shoe (\d+)", re.IGNORECASE)
    existing_shoes = [
        int(match.group(1)) for f in existing_files
        if (match := pattern.search(f))
    ]
    shoe_number = max(existing_shoes, default=0) + 1

    timestamp_str = datetime.now().strftime('%Y-%m-%d_%H-%M')
    filename = f"{table_name} - Shoe {shoe_number} {timestamp_str}.txt"
    filepath = os.path.join(folder_path, filename)

    lines = []

    for key in game_keys:
        game_id, round_num = key.split("_")
        game = table[key]

        dealer_score, dealer_cards = game["dealer"]
        total_cards = len(dealer_cards)

        for player_data in game["players"].values():
            for hand in player_data.values():
                total_cards += len(hand[1])

        timestamp = game.get("timestamp", "")

        lines.append(f"Game ID : {game_id}")
        lines.append(f"Round   : {round_num} ( {total_cards} )")
        lines.append(f"Time    : {timestamp}")
        lines.append(f"Dealer  : {dealer_score} [ {','.join(dealer_cards)} ]")

        for seat_key in sorted(game["players"].keys(), key=str):
            player_data = game["players"][seat_key]
            for hand_key in sorted(player_data.keys()):
                score, cards = player_data[hand_key]
                lines.append(f"Player {seat_key} {hand_key}: {score} [ {','.join(cards)} ]")

        lines.append("------------------------------")
        lines.append("")

    for i in range(0, len(all_cards), 10):
        prefix = "Issued Card :" if i == 0 else "              "
        lines.append(f"{prefix} {','.join(all_cards[i:i+10])}")

    lines.append(f"\nTotal Card : {len(all_cards)}")
    tc = get_true_count(all_cards)
    lines.append(f"True Count : {tc}")

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"[❌ Export Error] {table_name}: {e}")

# ===================================================================================== #
#                                  MQTT 推送 PP 牌桌更新                                 
# ===================================================================================== #
# ★ 决策状态缓存：桌名 → { "座位_h0": {"canDecide": True, "cashOut": 12.5}, ... }
#   为什么需要它：
#     服务器一次只推一个座位的 playerSeat 帧，如果每帧重建 decisions，
#     同一帧里另一个座位就「没有任何决策信息」→ 网络端那手永远不算 EV。
#   这里改成「整局累积」，新一局（betsopen）才清空 → 每次推送都带上全部座位的决策状态。
decision_cache = {}


def clear_decision_cache(table_name):
    decision_cache[table_name] = {}


def _now_turn(ps_data):
    """本帧「轮到谁做决策」→ 返回 {"座位_h0", …}

    PP 是顺序轮流：带 decisionOffer 的那一手就是现在轮到的手。
    没有 offer 的帧返回空集合（不改变现状，不清也不点）。
    """
    raw_seat = ps_data.get("seatNumber")
    if raw_seat is None:
        return set()
    mapped = "0" if raw_seat == -1 else str(raw_seat + 1)
    out = set()
    for hk, hv in ps_data.items():
        if not (hk.startswith("h") and isinstance(hv, dict)):
            continue
        if isinstance(hv.get("decisionOffer"), dict):
            out.add(f"{mapped}_{hk}")
    return out


def publish_pp_update(table_name, tc_val, cards, decisions=None):
    if mqtt_client is None:
        return

    if not voip_state.get(table_name, {}).get("mqtt_enabled"):
        return

    dealer_data = {"score": None, "cards": []}
    players_data = {}
    player_names = seat_players.get(table_name, {})

    table = issued_cards.get(table_name, {})
    game_keys = [k for k in table if k not in ("all_cards", "player_names")]

    if game_keys:
        latest_key = sorted(game_keys, key=lambda k: int(k.split("_")[1]))[-1]
        game = table[latest_key]

        dealer_data = {
            "score": game["dealer"][0],
            "cards": game["dealer"][1]
        }

        for seat_key, hand_map in game["players"].items():
            for hand_key, (score, card_list) in hand_map.items():
                flat_key = f"{seat_key}_{hand_key}"
                entry = {
                    "score": score,
                    "cards": card_list,
                    "bet": seat_bets.get(table_name, {}).get(seat_key, None)
                }
                # ★ 决策状态：cashOut 有就带上；canDecide=True 表示这一手轮到他决策
                #   （网络端只要看到 canDecide 就触发 EV 计算，不再依赖 cashOut 是否存在）
                dstate = (decisions or {}).get(flat_key)
                if isinstance(dstate, dict):
                    if dstate.get("cashOut") is not None:
                        entry["cashOut"] = dstate["cashOut"]
                    if dstate.get("canDecide"):
                        entry["canDecide"] = True
                elif dstate is not None:
                    entry["cashOut"] = dstate          # 兼容旧格式（直接是数字）
                players_data[flat_key] = entry

    pdev_val = get_pdev(cards)

    payload = {
        "tc": tc_val,
        "pdev": None if pdev_val is None else round(pdev_val, 6),
        "total": len(cards),
        "cards": cards,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dealer": dealer_data,
        "players": players_data,
        "player_names": player_names
    }

    topic = f"pp/table/{table_name}"
    try:
        mqtt_client.publish(topic, json.dumps(payload), qos=1)
    except Exception as e:
        print(f"[❌ MQTT Publish Error] {table_name}: {e}")

    # 同时广播给浏览器
    asyncio.run_coroutine_threadsafe(
        broadcast_to_browser(topic, "table_update", payload), loop
    )

async def broadcast_to_browser(topic, msg_type, payload):
    if not ws_clients:
        return
    message = json.dumps({"topic": topic, "type": msg_type, "payload": payload}, qos=1)
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send(message)
        except:
            dead.add(ws)
    ws_clients -= dead

async def browser_handler(websocket):
    ws_clients.add(websocket)
    try:
        async for _ in websocket:
            pass
    except:
        pass
    finally:
        ws_clients.discard(websocket)

async def ws_broadcast_server():
    global loop
    loop = asyncio.get_running_loop()
    async with websockets.serve(browser_handler, "0.0.0.0", 8765):
        await asyncio.Future()

# ===================================================================================== #
#                            拆牌逻辑修正合并入 handle_player_seat                     
# ===================================================================================== #
def handle_player_seat(seat_data, table_name):
    global issued_cards, game_counters

    game_id = seat_data.get("gameId", "")
    if not game_id:
        return None

    raw_seat = seat_data.get("seatNumber", "")
    seat_number = 0 if raw_seat == -1 else raw_seat + 1
    seat_key = str(seat_number)

    CARD_MAP = {
        "0": "10",
        "1": "A"
    }

    if table_name not in issued_cards:
        issued_cards[table_name] = {"all_cards": []}
        game_counters[table_name] = {}
    elif "all_cards" not in issued_cards[table_name]:
        issued_cards[table_name]["all_cards"] = []

    table_data = issued_cards[table_name]
    counter_data = game_counters[table_name]

    if game_id not in counter_data:
        counter_data[game_id] = len(counter_data) + 1
    game_key = f"{game_id}_{counter_data[game_id]}"

    if game_key not in table_data:
        table_data[game_key] = {
            "dealer": ["", []],
            "players": {},
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

    game_data = table_data[game_key]

    # ✅ 若检测到 h1，说明是拆牌 → 清除旧 seat_key（旧结构为 list）
    if "h1" in seat_data:
        if seat_key in game_data["players"]:
            if isinstance(game_data["players"][seat_key], list):
                del game_data["players"][seat_key]
            # 拆牌前先把 h0 的重复两张截成一张，避免后续新牌无法追加
            elif isinstance(game_data["players"][seat_key], dict):
                if "h0" in game_data["players"][seat_key]:
                    h0_cards = game_data["players"][seat_key]["h0"][1]
                    if len(h0_cards) == 2 and h0_cards[0] == h0_cards[1]:
                        game_data["players"][seat_key]["h0"][1] = h0_cards[:1]

    for key, hand in seat_data.items():
        if not key.startswith("h"):
            continue

        score = hand.get("score", "")
        cards = hand.get("cards", [])

        card_values = []
        for card in cards:
            code = card.get("scanCode", "")
            if len(code) >= 1:
                point = code[0]
                converted = CARD_MAP.get(point, point)
                card_values.append(converted)

        # ✅ 推送决策信息（若有 decisionOffer）
        if "decisionOffer" in hand:
            do = hand["decisionOffer"]
            payload = {
                "seatNumber": raw_seat,
                "hand": key,
                "score": score,
                "cards": [c.get("scanCode", "")[0] for c in cards if c.get("scanCode")],
                "cashOut": do.get("cashOut") if isinstance(do, dict) else None
            }
            topic = f"pp/decision/{table_name}"
            mqtt_client.publish(topic, json.dumps(payload), qos=1)

        if not card_values:
            continue

        if seat_number == 0:
            current_cards = game_data["dealer"][1]
            if len(card_values) > len(current_cards):
                new_cards = card_values[len(current_cards):]
                current_cards.extend(new_cards)

                if score:
                    game_data["dealer"][0] = score
                else:
                    last = card_values[-1]
                    if last == "A":
                        game_data["dealer"][0] = "1/11"
                    elif last in ["J", "Q", "K"]:
                        game_data["dealer"][0] = "10"
                    else:
                        game_data["dealer"][0] = last

                table_data["all_cards"].extend(new_cards)
        else:
            if seat_key not in game_data["players"] or not isinstance(game_data["players"][seat_key], dict):
                game_data["players"][seat_key] = {}

            if key not in game_data["players"][seat_key]:
                game_data["players"][seat_key][key] = ["", []]

            current_cards = game_data["players"][seat_key][key][1]

            if len(card_values) > len(current_cards):
                new_cards = card_values[len(current_cards):]
                current_cards.extend(new_cards)
                game_data["players"][seat_key][key][0] = score
                table_data["all_cards"].extend(new_cards)

    # ✅ 保留 all_cards，清洗结构
    cleaned = {k: v for k, v in table_data.items() if k != "all_cards"}
    cleaned["all_cards"] = table_data.get("all_cards", [])
    issued_cards[table_name] = cleaned

    return game_key

# ===================================================================================== #
#                        监听指定 WebSocket URL，仅处理连接与 voip_cc 与 playerSeat
# ===================================================================================== #
async def listen_ws(table_name, url, row, retry=0):
    global issued_cards, game_counters, voip_state, seat_players, decision_cache

    voip_state[table_name] = {
        "recording": False,
        "activated": False,
        "first_shoe_skipped": False,
        "mqtt_enabled": False,
        "just_betsopen": False,
        "voip_detected": False,
        "player_after_voip": False,
        "voip_pending_clear": False
    }

    try:
        async with websockets.connect(url) as websocket:
            subscribed = False
            heartbeat_task = None

            while True:
                msg = await websocket.recv()

                try:
                    data = json.loads(msg)

                    if not subscribed and "subscribe" in data:
                        if data["subscribe"].get("status") == "success":
                            subscribed = True
                            channel = data["subscribe"].get("channel") or table_name
                            heartbeat_task = asyncio.create_task(send_heartbeat(websocket, channel, table_name))
                            console_print("", f"{Fore.GREEN}{table_name:<25}| ✅ Connected ✅{Style.RESET_ALL}", row)

                    elif "playerSeat" in data:
                        ps_data = data["playerSeat"]
                        handle_player_seat(ps_data, table_name)

                        if voip_state[table_name]["voip_detected"]:
                            voip_state[table_name]["player_after_voip"] = True

                        for k, v in ps_data.items():
                            if k.startswith("h") and isinstance(v, dict) and ("decisionOffer" in v or "currentDecision" in v):
                                seat_num = ps_data.get("seatNumber")
                                if seat_num is not None:
                                    score = v.get("score", "")
                                    cards = [c.get("scanCode", "")[0] for c in v.get("cards", []) if c.get("scanCode")]
                                    do = v.get("decisionOffer")
                                    payload = {
                                        "seatNumber": seat_num,
                                        "hand": k,
                                        "score": score,
                                        "cards": cards,
                                        "currentDecision": v.get("currentDecision", ""),
                                        "cashOut": do.get("cashOut") if isinstance(do, dict) else None
                                    }
                                    topic = f"pp/decision/{table_name}"
                                    mqtt_client.publish(topic, json.dumps(payload), qos=1)
                        # ★ 决策状态（单座位就够）：
                        #   PP 是「顺序轮流」—— 谁收到 decisionOffer，就是轮到谁。
                        #   所以：这一帧带 offer 的座位 = 现在轮到他；
                        #         其他所有原本亮着的座位 = 已经做完了 → 立刻熄。
                        #   不依赖 activeHand（服务器大多帧不带），也不等下一局。
                        turn = _now_turn(ps_data)      # → 本帧轮到的手集合，如 {"3_h0"}
                        raw_seat = ps_data.get("seatNumber")
                        if raw_seat is not None:
                            mapped_seat = "0" if raw_seat == -1 else str(raw_seat + 1)
                            _dc = decision_cache.setdefault(table_name, {})
                            # 1) 先按本帧把「轮到的手」点亮
                            for flat_key in turn:
                                st = _dc.setdefault(flat_key, {})
                                st["canDecide"] = True
                            # 2) 本帧取到的 cashOut 挂上去
                            for hk, hv in ps_data.items():
                                if not (hk.startswith("h") and isinstance(hv, dict)):
                                    continue
                                do = hv.get("decisionOffer")
                                if isinstance(do, dict) and do.get("cashOut") is not None:
                                    st = _dc.setdefault(f"{mapped_seat}_{hk}", {})
                                    st["cashOut"] = do["cashOut"]
                            # 3) 除了本帧轮到的手，其余亮着的全部熄灭（回合已经过去了）
                            if turn:
                                for k, st in _dc.items():
                                    if k not in turn:
                                        st.pop("canDecide", None)
                                        st.pop("cashOut", None)
                            decisions = _dc

                        all_cards = issued_cards.get(table_name, {}).get("all_cards", [])
                        tc_val = get_true_count(all_cards)
                        pdev_val = get_pdev(all_cards)
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        tc_str = f"{tc_val:+6.2f}"
                        pdev_str = f"{pdev_val:+.5f}" if pdev_val is not None else "     n/a"
                        line = f"{table_name:<25}| {tc_str} | {pdev_str} | {len(all_cards):>3} | {now}"
                        color = Fore.GREEN if tc_val >= 1 else Fore.RED if tc_val < 0 else ""
                        if not voip_state[table_name]["activated"]:
                            color = Fore.LIGHTBLACK_EX
                        console_print("", f"{color}{line}{Style.RESET_ALL}", row)

                        publish_pp_update(table_name, tc_val, all_cards, decisions)

                    elif "voip_cc" in data:
                        voip_state[table_name]["voip_detected"] = True

                        if voip_state[table_name]["just_betsopen"]:
                            voip_state[table_name]["voip_pending_clear"] = True
                            voip_state[table_name]["just_betsopen"] = False

                    elif "betsopen" in data:
                        # ★ 新一局开始 → 清掉上一局累积的决策状态，避免旧 cashOut 串到本局
                        clear_decision_cache(table_name)
                        if voip_state[table_name]["voip_detected"] and voip_state[table_name]["player_after_voip"]:
                            if voip_state[table_name]["first_shoe_skipped"]:
                                export_all_rounds_to_txt(table_name)
                            else:
                                voip_state[table_name]["first_shoe_skipped"] = True

                            voip_state[table_name]["mqtt_enabled"] = True
                            publish_pp_update(table_name, 0.00, [])
                            issued_cards[table_name] = {}
                            game_counters[table_name] = {}
                            voip_state[table_name]["activated"] = True
                            console_print("", f"{Fore.MAGENTA}{table_name:<25}| 📕 Shuffle being preparation{Style.RESET_ALL}", row)

                        elif voip_state[table_name]["voip_pending_clear"]:
                            if voip_state[table_name]["first_shoe_skipped"]:
                                export_all_rounds_to_txt(table_name)
                            else:
                                voip_state[table_name]["first_shoe_skipped"] = True

                            voip_state[table_name]["mqtt_enabled"] = True
                            publish_pp_update(table_name, 0.00, [])
                            issued_cards[table_name] = {}
                            game_counters[table_name] = {}
                            voip_state[table_name]["activated"] = True
                            console_print("", f"{Fore.MAGENTA}{table_name:<25}| 📕 Shuffle being preparation{Style.RESET_ALL}", row)

                        voip_state[table_name]["just_betsopen"] = True
                        voip_state[table_name]["voip_detected"] = False
                        voip_state[table_name]["player_after_voip"] = False
                        voip_state[table_name]["voip_pending_clear"] = False
                        voip_state[table_name]["recording"] = False

                    elif "notinsured" in data:
                            topic = f"pp/offertimer/{table_name}"
                            mqtt_client.publish(topic, json.dumps(data), qos=1)
    
                    elif "seat" in data:
                        s = data["seat"]
                        name = s.get("screen_name", "")
                        num_raw = s.get("num", "")
                        try:
                            num = str(int(num_raw) + 1)
                        except:
                            num = str(num_raw)

                        if table_name not in seat_players:
                            seat_players[table_name] = {}
                        seat_players[table_name][num] = name

                        all_cards = issued_cards.get(table_name, {}).get("all_cards", [])
                        tc_val = get_true_count(all_cards) if all_cards else 0.00
                        publish_pp_update(table_name, tc_val, all_cards)

                    elif "instantBet" in data:
                        ib = data["instantBet"]
                        bet_info = ib.get("bet", {})
                        if bet_info.get("mainBet"):
                            seat_raw = bet_info.get("seat", "")
                            bet_amount = bet_info.get("betAmount", "0")
                            try:
                                seat_num = str(int(seat_raw) + 1)
                            except:
                                seat_num = str(seat_raw)
                            if table_name not in seat_bets:
                                seat_bets[table_name] = {}
                            seat_bets[table_name][seat_num] = bet_amount
                            # Debug: also update existing publish
                            all_cards = issued_cards.get(table_name, {}).get("all_cards", [])
                            tc_val = get_true_count(all_cards) if all_cards else 0.00
                            publish_pp_update(table_name, tc_val, all_cards)

                    elif "bjGameEnd" in data:
                        topic = f"pp/gameend/{table_name}"
                        mqtt_client.publish(topic, json.dumps(data), qos=1)

                except json.JSONDecodeError:
                    continue

    except Exception as e:
        console_print("", f"{Fore.RED}{table_name:<25}| [❌ Error] {e}{Style.RESET_ALL}", row)
        if retry < 3:
            await asyncio.sleep(5)
            issued_cards[table_name] = {"all_cards": []}
            game_counters[table_name] = {}
            retry += 1
            note = f"{Fore.YELLOW}{table_name:<25}| ⟳ Retrying ({retry}/3){Style.RESET_ALL}"
            console_print("", note, row)
            await listen_ws(table_name, url, row, retry)
        else:
            final = f"{Fore.RED}{table_name:<25}| ❌ Retry limit reached, stop reconnecting{Style.RESET_ALL}"
            console_print("", final, row)

# ===================================================================================== #
#                            启动多个 Blackjack WebSocket 监听任务                          
# ===================================================================================== #
async def main():
    ws_urls = load_ws_urls()
    if not ws_urls:
        return

    table_name_map = load_table_name_map()
    tasks = []
    tasks.append(ws_broadcast_server())

    for i, url in enumerate(ws_urls):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        table_id = query.get("tableId", ["Unknown"])[0]
        table_name = table_name_map.get(table_id, table_id)  # 显示桌名，如找不到就显示 tableId
        row = i + 1
        tasks.append(listen_ws(table_name, url, row))

    await asyncio.gather(*tasks)

# ===================================================================================== #
#                                     程序主入口                                        
# ===================================================================================== #
def signal_handler(sig, frame):
    print("\n[Shutdown] Received signal, stopping...")
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

if __name__ == "__main__":
    sys.stdout.write("\033[?25l")
    start_mqtt_client()
    asyncio.run(main())
    try:
        input("Press Enter to exit...")
    except:
        pass
