"""
    Parsing through a bunch of salty bet chat messages and extracting the right ones
"""

phrases = ["Bets are OPEN", "wins! Payouts to"]

with open("output.txt", "r") as f:
    lines = f.readlines()
    for line in lines:
        if any([_ in line for _ in phrases]):
            print(line.strip())
