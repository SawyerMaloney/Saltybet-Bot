"""
    Parsing through a bunch of salty bet chat messages and extracting the right ones
"""

from re import findall

phrases = ["Bets are OPEN for ", "wins! Payouts to"]

def stripped_output():
    output = []
    with open("output.txt", "r") as f:
        lines = f.readlines()
        for line in lines:
            if any([_ in line for _ in phrases]) and "Team A" not in line and "Team B!" not in line:
                output.append(line.strip())

    return output

output = stripped_output()

with open("stripped_output.txt", "w") as f:
    for line in output:
        f.write(line + '\n')

# parse through output and get the names and tiers
i = 0
while i < len(output):
    line = output[i]
    if phrases[0] in line: # Bets are open line, has names and tier
        # use regex to match
        text = line[len(phrases[0]):] # need to chop off the beginning of the line
        matches = findall(".*vs", text)[0]
        first_character = matches[:len(matches)-3]
        
        matches = findall("vs .*! \(", text)[0]
        second_character = matches[3:len(matches)-3]

        matches = findall("\(. Tier\)", text)
        if len(matches) == 1: # we want the match to have a tier for elo reasons
            matches = matches[0]
            tier = matches[1]

            # now find the winner, look ahead one
            i += 1
            line = output[i]
            matches = findall(".*wins! Payouts to ", line) # want to be specific to avoid false matches
            if len(matches) == 1: # good, we are on the right line, continue
                matches = matches[0]
                winner = matches[:len(matches) - 18]
                print(f"first character: {first_character}, second character: {second_character}, Tier {tier}. The winner: {winner}.")
            # else:
                # incomplete log or other issue, we just will skip
        else:
            i += 1 # skipping the match result
    i += 1
