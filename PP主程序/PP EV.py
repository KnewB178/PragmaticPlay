import re
import json
import sys
import numpy as np

# ----------------------------------------------------------------------------------------------------------------------------#
# 核心计算函数
# ----------------------------------------------------------------------------------------------------------------------------#
def calc_ev(all_cards, dealer_cards_list, player_cards_list, cash_out=None, bet_amount=None):
    """
    计算 EV（预期值）。

    参数：
        all_cards: list[str] — 所有已出牌
        dealer_cards_list: list[str] — 庄家手牌（第一张为明牌）
        player_cards_list: list[str] — 玩家手牌
        cash_out: float|None — 提前兑现赔付金额（可选）

    返回：
        dict {
            "best": {"decision": str, "ev": float},
            "second": {"decision": str, "ev": float},
            "insurance_ev": float|None,
            "all_decisions": [{"decision": str, "ev": float}, ...]
        }
    """

    # --- 常量与映射 ---
    valid_cards = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'T', 'J', 'Q', 'K']
    valid_cards_set = set(valid_cards)

    card_value_mapping = {
        "A": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
        "10": 10, "T": 10, "J": 10, "Q": 10, "K": 10,
    }

    mapping = {
        'A': 1, '2': 2, '3': 3, '4': 4, '5': 5,
        '6': 6, '7': 7, '8': 8, '9': 9,
        '10': 10, 'T': 10, 'J': 10, 'Q': 10, 'K': 10
    }

    decks = 8

    # --- 统计已出牌 ---
    card_counts = {card: 0 for card in valid_cards}
    for card in all_cards:
        card_upper = card.strip().upper()
        if card_upper in card_counts:
            card_counts[card_upper] += 1

    out_cards = np.zeros(12, dtype=int)
    for card, index in mapping.items():
        out_cards[index] += card_counts[card]

    # --- 庄家明牌 ---
    dealer_up = 2
    if dealer_cards_list:
        first_dealer_card = dealer_cards_list[0].strip().upper()
        dealer_up = card_value_mapping.get(first_dealer_card, 2)

    # --- 玩家手牌解析 ---
    player_total = 0
    has_ace = False
    for c in player_cards_list:
        c_upper = c.strip().upper()
        if c_upper == 'A':
            has_ace = True
        val = card_value_mapping.get(c_upper, 0)
        player_total += val

    # --- 计算剩余牌分布与概率 ---
    num_total = 0
    num_cards = np.zeros(11, dtype=int)
    p = np.zeros(31, dtype=float)

    for i in range(1, 10):
        num_cards[i] = decks * 4 - out_cards[i]
        num_total += num_cards[i]

    num_cards[10] = decks * 4 * 4 - out_cards[10]
    num_total += num_cards[10]

    for i in range(1, 11):
        p[i] = num_cards[i] / num_total

    # --- EV 表计算 ---
    Hard_Bust = np.zeros(32, dtype=float)
    Soft_Bust = np.zeros(32, dtype=float)

    for i in range(17, 22):
        Hard_Bust[i] = 0
    for i in range(22, 32):
        Hard_Bust[i] = 1
    for i in range(17, 22):
        Soft_Bust[i] = 0
    for i in range(27, 32):
        Soft_Bust[i] = 0

    for i in range(16, 1, -1):
        for k in range(1, 11):
            if k == 1:
                Hard_Bust[i] += p[k] * Soft_Bust[k + i + 10]
            else:
                Hard_Bust[i] += p[k] * Hard_Bust[k + i]
        if 11 < i < 17:
            Soft_Bust[10 + i] = Hard_Bust[i]
        if i == 6:
            for m in range(16, 11, -1):
                for k in range(1, 11):
                    Soft_Bust[m] += p[k] * Soft_Bust[k + m]

    Hard_Hand = np.zeros((32, 32), dtype=float)
    Soft_Hand = np.zeros((32, 32), dtype=float)

    for i in range(17, 22):
        for j in range(17, 22):
            if i == j:
                Hard_Hand[i, j] = 1
                Soft_Hand[i, j] = 1
                if j + 10 < Soft_Hand.shape[1]:
                    Soft_Hand[i, j + 10] = 1

    for n in range(17, 22):
        for i in range(16, 1, -1):
            for k in range(1, 11):
                if k == 1:
                    Hard_Hand[n, i] += p[k] * Soft_Hand[n, k + i + 10]
                else:
                    Hard_Hand[n, i] += p[k] * Hard_Hand[n, k + i]
            if 11 < i < 17:
                Soft_Hand[n, 10 + i] = Hard_Hand[n, i]
            if i == 6:
                for m in range(16, 11, -1):
                    for k in range(1, 11):
                        Soft_Hand[n, m] += p[k] * Soft_Hand[n, k + m]

    Dealer_Bust = np.zeros(32, dtype=float)
    Dealer_Hand = np.zeros((32, 32), dtype=float)
    Dealer_Hand_BJ = np.zeros(11, dtype=float)

    for i in range(1, 11):
        if i > 1:
            Dealer_Bust[i] = Hard_Bust[i]
        else:
            for k in range(1, 11):
                Dealer_Bust[i] += p[k] * Soft_Bust[k + i + 10]

        for k in range(17, 22):
            if i > 1:
                Dealer_Hand[k, i] = Hard_Hand[k, i]
            else:
                for m in range(1, 11):
                    Dealer_Hand[k, i] += p[m] * Soft_Hand[k, m + i + 10]

    Dealer_Hand[21, 1] -= p[10]
    Dealer_Hand[21, 10] -= p[1]
    Dealer_Hand_BJ[1] = p[10]
    Dealer_Hand_BJ[10] = p[1]

    Temp_Total = (
        Dealer_Hand[17, 1] + Dealer_Hand[18, 1] + Dealer_Hand[19, 1]
        + Dealer_Hand[20, 1] + Dealer_Hand[21, 1] + Dealer_Bust[1]
    )

    Dealer_Bust[1] /= Temp_Total
    for k in range(17, 22):
        Dealer_Hand[k, 1] /= Temp_Total

    P_Hard_S = np.zeros((32, 32), dtype=float)
    P_Soft_S = np.zeros((32, 32), dtype=float)
    P_Hard_H = np.zeros((32, 32), dtype=float)
    P_Soft_H = np.zeros((32, 32), dtype=float)
    P_Hard_HS = np.zeros((32, 32), dtype=float)
    P_Soft_HS = np.zeros((32, 32), dtype=float)
    P_Hard_D = np.zeros((32, 32), dtype=float)
    P_Soft_D = np.zeros((32, 32), dtype=float)
    P_Hard_HSD = np.zeros((32, 32), dtype=float)
    P_Soft_HSD = np.zeros((32, 32), dtype=float)
    P_Split = np.zeros((32, 32), dtype=float)
    P_Split_YN = np.zeros((32, 32), dtype=float)

    for k in range(4, 17):
        for i in range(1, 11):
            if i == 1:
                P_Hard_S[k, i] = (
                    Dealer_Bust[i] - Dealer_Hand[17, i] - Dealer_Hand[18, i]
                    - Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i]
                )
            elif i == 10:
                P_Hard_S[k, i] = (
                    Dealer_Bust[i] - Dealer_Hand[17, i] - Dealer_Hand[18, i]
                    - Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i]
                    - Dealer_Hand_BJ[10]
                )
            else:
                P_Hard_S[k, i] = (
                    Dealer_Bust[i] - Dealer_Hand[17, i] - Dealer_Hand[18, i]
                    - Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i]
                )
            if k > 11:
                P_Soft_S[k, i] = P_Hard_S[k, i]
                P_Soft_S[k + 10, i] = P_Hard_S[k, i]

    for i in range(1, 11):
        if i == 1:
            P_Hard_S[17, i] = (Dealer_Bust[i] - 0 * Dealer_Hand[17, i] - Dealer_Hand[18, i]
                               - Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i])
            P_Hard_S[18, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] - 0 * Dealer_Hand[18, i]
                               - Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i])
            P_Hard_S[19, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] + Dealer_Hand[18, i]
                               - 0 * Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i])
            P_Hard_S[20, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] + Dealer_Hand[18, i]
                               + Dealer_Hand[19, i] - 0 * Dealer_Hand[20, i] - Dealer_Hand[21, i])
            P_Hard_S[21, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] + Dealer_Hand[18, i]
                               + Dealer_Hand[19, i] + Dealer_Hand[20, i] - 0 * Dealer_Hand[21, i])
        elif i == 10:
            P_Hard_S[17, i] = (Dealer_Bust[i] - 0 * Dealer_Hand[17, i] - Dealer_Hand[18, i]
                               - Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i]
                               - Dealer_Hand_BJ[10])
            P_Hard_S[18, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] - 0 * Dealer_Hand[18, i]
                               - Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i]
                               - Dealer_Hand_BJ[10])
            P_Hard_S[19, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] + Dealer_Hand[18, i]
                               - 0 * Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i]
                               - Dealer_Hand_BJ[10])
            P_Hard_S[20, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] + Dealer_Hand[18, i]
                               + Dealer_Hand[19, i] - 0 * Dealer_Hand[20, i] - Dealer_Hand[21, i]
                               - Dealer_Hand_BJ[10])
            P_Hard_S[21, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] + Dealer_Hand[18, i]
                               + Dealer_Hand[19, i] + Dealer_Hand[20, i] - 0 * Dealer_Hand[21, i]
                               - Dealer_Hand_BJ[10])
        else:
            P_Hard_S[17, i] = (Dealer_Bust[i] - 0 * Dealer_Hand[17, i] - Dealer_Hand[18, i]
                               - Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i])
            P_Hard_S[18, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] - 0 * Dealer_Hand[18, i]
                               - Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i])
            P_Hard_S[19, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] + Dealer_Hand[18, i]
                               - 0 * Dealer_Hand[19, i] - Dealer_Hand[20, i] - Dealer_Hand[21, i])
            P_Hard_S[20, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] + Dealer_Hand[18, i]
                               + Dealer_Hand[19, i] - 0 * Dealer_Hand[20, i] - Dealer_Hand[21, i])
            P_Hard_S[21, i] = (Dealer_Bust[i] + Dealer_Hand[17, i] + Dealer_Hand[18, i]
                               + Dealer_Hand[19, i] + Dealer_Hand[20, i] - 0 * Dealer_Hand[21, i])

    for k in range(22, 32):
        for i in range(1, 11):
            P_Hard_S[k, i] = -1

    for k in range(12, 32):
        for i in range(1, 11):
            if k < 22:
                P_Soft_S[k, i] = P_Hard_S[k, i]
            else:
                P_Soft_S[k, i] = P_Hard_S[k - 10, i]

    for k in range(21, 32):
        for i in range(1, 11):
            P_Hard_H[k, i] = -1
            P_Soft_H[31, i] = -1
            P_Hard_HS[21, i] = max(P_Hard_S[21, i], P_Hard_H[21, i])
            P_Soft_HS[31, i] = max(P_Soft_S[31, i], P_Soft_H[31, i])
            if k > 21:
                P_Hard_HS[k, i] = -1

    for m in range(20, 11, -1):
        for i in range(1, 11):
            for k in range(1, 11):
                if k == 1:
                    P_Hard_H[m, i] += p[k] * P_Soft_HS[m + k + 10, i]
                else:
                    P_Hard_H[m, i] += p[k] * P_Hard_HS[m + k, i]
                P_Soft_H[m + 10, i] = P_Hard_H[m, i]
            P_Hard_HS[m, i] = max(P_Hard_S[m, i], P_Hard_H[m, i])
            P_Soft_HS[m + 10, i] = max(P_Soft_S[m + 10, i], P_Soft_H[m + 10, i])

    for m in range(21, 11, -1):
        for i in range(1, 11):
            for k in range(1, 11):
                P_Soft_H[m, i] += p[k] * P_Soft_HS[m + k, i]
            P_Soft_HS[m, i] = max(P_Soft_S[m, i], P_Soft_H[m, i])

    for m in range(11, 3, -1):
        for i in range(1, 11):
            for k in range(1, 11):
                if k == 1:
                    P_Hard_H[m, i] += p[k] * P_Soft_HS[m + k + 10, i]
                else:
                    P_Hard_H[m, i] += p[k] * P_Hard_HS[m + k, i]
            P_Hard_HS[m, i] = max(P_Hard_S[m, i], P_Hard_H[m, i])

    for k in range(21, 32):
        for i in range(1, 11):
            P_Hard_HSD[21, i] = max(P_Hard_S[21, i], P_Hard_H[21, i], P_Hard_D[21, i])
            P_Soft_HSD[31, i] = max(P_Soft_S[31, i], P_Soft_H[31, i], P_Soft_D[31, i])
            if k > 21:
                P_Hard_HSD[k, i] = -1

    for m in range(20, 11, -1):
        for i in range(1, 11):
            for k in range(1, 11):
                if k == 1:
                    P_Hard_D[m, i] += 2 * p[k] * P_Soft_S[m + k + 10, i]
                else:
                    P_Hard_D[m, i] += 2 * p[k] * P_Hard_S[m + k, i]
                P_Soft_D[m + 10, i] = P_Hard_D[m, i]
            P_Hard_HSD[m, i] = max(P_Hard_S[m, i], P_Hard_H[m, i], P_Hard_D[m, i])
            P_Soft_HSD[m + 10, i] = max(P_Soft_S[m + 10, i], P_Soft_H[m + 10, i], P_Soft_D[m + 10, i])

    for m in range(21, 11, -1):
        for i in range(1, 11):
            for k in range(1, 11):
                P_Soft_D[m, i] += 2 * p[k] * P_Soft_S[m + k, i]
            P_Soft_HSD[m, i] = max(P_Soft_S[m, i], P_Soft_H[m, i], P_Soft_D[m, i])

    for m in range(11, 3, -1):
        for i in range(1, 11):
            for k in range(1, 11):
                if k == 1:
                    P_Hard_D[m, i] += 2 * p[k] * P_Soft_S[m + k + 10, i]
                else:
                    P_Hard_D[m, i] += 2 * p[k] * P_Hard_S[m + k, i]
            P_Hard_HSD[m, i] = max(P_Hard_S[m, i], P_Hard_H[m, i], P_Hard_D[m, i])

    for m in range(10, 0, -1):
        if m == 1:
            for i in range(1, 11):
                for k in range(1, 11):
                    P_Split[m, i] += 2 * p[k] * P_Soft_S[m + k + 10, i]
                P_Split_YN[m, i] = max(P_Split[m, i], P_Soft_HSD[2 * m + 10, i])
        else:
            for i in range(1, 11):
                for k in range(1, 11):
                    if k == 1:
                        P_Split[m, i] += 2 * p[k] * P_Soft_HSD[m + k + 10, i]
                    else:
                        P_Split[m, i] += 2 * p[k] * P_Hard_HSD[m + k, i]
                P_Split_YN[m, i] = max(P_Split[m, i], P_Hard_HSD[2 * m, i])

    # --- 空手牌：仅返回保险 EV ---
    if not player_cards_list:
        ins = None
        if dealer_up == 1:
            ins = round(3 * float(p[10]) - 1, 4)
        return {"insurance_ev": ins}

    # --- 玩家手牌分析与决策 ---
    is_soft_hand = (has_ace and player_total < 11)

    # 检查是否能分牌（两张点数相同）
    can_split = False
    if len(player_cards_list) == 2:
        c0 = player_cards_list[0].strip().upper()
        c1 = player_cards_list[1].strip().upper()
        v0 = card_value_mapping.get(c0, 0)
        v1 = card_value_mapping.get(c1, 0)
        can_split = (v0 == v1 and v0 > 0)

    early_cashout_value = None
    if cash_out is not None:
        if bet_amount and float(bet_amount) > 0:
            early_cashout_value = float(cash_out) / float(bet_amount) - 1
        else:
            early_cashout_value = float(cash_out) - 1

    if is_soft_hand:
        soft_total = player_total + 10
        hit_ev = round(float(P_Soft_H[soft_total, dealer_up]), 4)
        double_ev = round(float(P_Soft_D[soft_total, dealer_up]), 4)
        stand_ev = round(float(P_Soft_S[soft_total, dealer_up]), 4)
    else:
        hit_ev = round(float(P_Hard_H[player_total, dealer_up]), 4)
        double_ev = round(float(P_Hard_D[player_total, dealer_up]), 4)
        stand_ev = round(float(P_Hard_S[player_total, dealer_up]), 4)

    all_evs = {
        "Stand": stand_ev,
        "Hit": hit_ev,
        "Double": double_ev
    }

    if can_split:
        split_ev = round(float(P_Split[card_value_mapping.get(player_cards_list[0].strip().upper(), 0), dealer_up]), 4)
        all_evs["Split"] = split_ev

    if early_cashout_value is not None:
        all_evs["CashOut"] = round(early_cashout_value, 4)

    sorted_decisions = sorted(all_evs.items(), key=lambda x: x[1], reverse=True)

    best = {"decision": sorted_decisions[0][0], "ev": sorted_decisions[0][1]}
    second = {"decision": sorted_decisions[1][0], "ev": sorted_decisions[1][1]} if len(sorted_decisions) > 1 else None

    # 保险 EV（庄家明牌为 A 时）
    insurance_ev = None
    if dealer_up == 1:
        ins_ev = round(3 * float(p[10]) - 1, 4)

    result = {
        "best": best,
        "all_decisions": [{"decision": d, "ev": e} for d, e in sorted_decisions],
    }

    if second:
        result["second"] = second

    if dealer_up == 1:
        result["insurance_ev"] = round(3 * float(p[10]) - 1, 4)

    return result


# ----------------------------------------------------------------------------------------------------------------------------#
# CLI 入口（保持原有功能）
# ----------------------------------------------------------------------------------------------------------------------------#
if __name__ == "__main__":
    if len(sys.argv) <= 1:
        sys.exit(1)

    raw_input = sys.argv[1]
    data = json.loads(raw_input)

    all_seen_cards = data.get("cards", [])
    dealer_cards = data.get("dealer", {}).get("cards", [])
    players_data = data.get("players", {})

    for player_key, player_info in players_data.items():
        player_id = f"P{player_key}"
        player_cards = player_info.get("cards", [])
        cash_out = player_info.get("cashOut")

        result = calc_ev(all_seen_cards, dealer_cards, player_cards, cash_out)

        print(f"{player_id} : {result['best']['decision']} {result['best']['ev']} | {' / '.join(d['decision'] + ' ' + str(d['ev']) for d in result['all_decisions'] if d != result['best'])}")

        if result.get("insurance_ev") is not None:
            if player_key == list(players_data.keys())[0]:
                print(f"{'Insurance EV':<13} = {result['insurance_ev']}")
