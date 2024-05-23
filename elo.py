"""
    Basic system to calculate elo of winner and loser based on two input elos
"""

def onevsone(player_1_elo, player_2_elo):
    Q_A = 10 ** (player_1_elo / 400)
    Q_B = 10 ** (player_2_elo / 400)

    E_A = Q_A / (Q_A + Q_B)
    E_B = Q_B / (Q_A + Q_B)

    K = 32

    # returning two tuples, win and lose for player one and win and lose for player two
    player_1 = (new_rating(player_1_elo, K, 1, E_A), new_rating(player_1_elo, K, 0, E_A))
    player_2 = (new_rating(player_2_elo, K, 1, E_B), new_rating(player_2_elo, K, 0, E_B))

    return (player_1, player_2)

def new_rating(rating, K, actual, expected):
    return rating + K * (actual - expected)
