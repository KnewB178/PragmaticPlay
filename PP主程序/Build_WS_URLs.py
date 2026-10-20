#!/usr/bin/env python3
import os, sys

# 自动切换到 venv 的 Python（双击运行时用）
_venv_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python3")
if sys.executable != _venv_py and os.path.exists(_venv_py):
    os.execv(_venv_py, [_venv_py] + sys.argv)

import re
import math
import requests

# ===================================================================================== #
#                       从 WS_Config.txt 读取所有会话 URL（一行一个）                    
# ===================================================================================== #
def load_session_urls(filename="WS_Config.txt"):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    except Exception as e:
        print(f"[ERROR] 读取 {filename} 失败：{e}")
        return []

# ===================================================================================== #
#                             从会话 URL 提取 JSESSIONID                                
# ===================================================================================== #
def get_all_jsessionids():
    return re.findall(r'JSESSIONID=([^&\s]+)', open('WS_Config.txt', encoding='utf-8').read())

# ===================================================================================== #
#                               从文件读取桌号数据                                      
# ===================================================================================== #
def get_all_table_data():
    with open('table_id.txt', 'r', encoding='utf-8') as f:
        return [
            (name.strip(), id_.strip())
            for line in f
            if '|' in line
            for name, id_ in [line.split('|', 1)]
        ]

# ===================================================================================== #
#                               WebSocket URL 生成器                                    
# ===================================================================================== #
def get_wss_url(jsessionid: str, table_id: str) -> str:
    config_url = f"https://games.pragmaticplaylive.net/api/ui/tableConfig?JSESSIONID={jsessionid}&tableId={table_id}&operatorNotification=true"
    try:
        response = requests.get(config_url, timeout=5)
        response.raise_for_status()
        data = response.json()
        ws_address = data.get("params", {}).get("ws_address")
        if not ws_address:
            print(f"[WARNING] No WebSocket address found for table ID : {table_id}")
            return None
        return f"{ws_address}?JSESSIONID={jsessionid}&tableId={table_id}&type=json&version=3"
    except Exception as e:
        print(f"[ERROR] API request failed for table ID {table_id} : {e}")
        return None

# ===================================================================================== #
#                             生成并保存所有桌 URL                                      
# 每个会话最多负责 6 桌：会话 i 对应 table_id.txt 中第 i*6 ~ i*6+5 行                    
# ===================================================================================== #
def generate_and_save_urls(table_data, jsessionids):
    total_tables = len(table_data)
    required_jsessionids = math.ceil(total_tables / 6)
    actual_jsessionids = len(jsessionids)
    usable_jsessionids = min(required_jsessionids, actual_jsessionids)
    processed_tables = min(6 * usable_jsessionids, total_tables)
    generated_urls = []

    for i in range(processed_tables):
        jsessionid_index = i // 6
        table_name, table_id = table_data[i]
        jsessionid = jsessionids[jsessionid_index]

        ws_url = get_wss_url(jsessionid, table_id)
        if ws_url:
            print(f"\n[JSESSIONID {jsessionid_index + 1}/{usable_jsessionids}]")
            print(f"[Table {i + 1}/{processed_tables}] {table_name} | {table_id}")
            print(f"URL: {ws_url}")
            generated_urls.append(ws_url)
        else:
            print(f"\n[FAILED] Could not generate URL for {table_name} | {table_id}")

    if generated_urls:
        save_urls_to_file(generated_urls)
        print(f"\nSuccessfully generated : {len(generated_urls)}")
    else:
        print("\n[WARNING] No valid URLs generated, file not updated")

# ===================================================================================== #
#                               文件写入操作                                            
# ===================================================================================== #
def save_urls_to_file(urls: list):
    with open('WS_Config.txt', 'w', encoding='utf-8') as f:
        f.writelines(f"{url}\n" for url in urls)

# ===================================================================================== #
#                               主程序执行                                              
# ===================================================================================== #
if __name__ == "__main__":
    if not load_session_urls():
        print("[ERROR] WS_Config.txt 为空或不存在，请先把所有会话 URL 写入 WS_Config.txt（一行一个）")
        input("按回车退出...")
        sys.exit(1)

    table_data = get_all_table_data()
    jsessionids = get_all_jsessionids()
    print(f"已读取 {len(jsessionids)} 个会话，共 {len(table_data)} 桌，每个会话最多生成 6 桌")

    generate_and_save_urls(table_data, jsessionids)
