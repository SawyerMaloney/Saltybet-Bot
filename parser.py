"""
    Parsing through a bunch of salty bet chat messages and extracting the right ones
"""

from re import findall
from elo import onevsone
import math
import networkx as nx
import matplotlib.pyplot as plt

phrases = ["Bets are OPEN for ", "wins! Payouts to"]

def stripped_output():
    output = []
    with open("output.txt", "r") as f:
        lines = f.readlines()
        for line in lines:
            if any([_ in line for _ in phrases]) and "Team A" not in line and "Team B!" not in line:
                output.append(line.strip())

    return output

def get_match_info(line):
    text = line[len(phrases[0]):]
    first = findall(".*vs ", text)[0]
    first = first[:len(first)-4]

    second = findall(" vs .*! \(", text)[0]
    second = second[4:len(second) - 3]

    tier = findall("\(. Tier\)", text)

    return (first, second, tier)

def data_from_elo():
    characters = {}
    appearances = {}
    with open("elo.txt", "r") as elo:
        lines = elo.readlines()
        for line in lines:
            line = line.strip().split(",")
            character = line[0]
            character_elo = line[1]
            character_appearances = line[2]

            characters[character] = character_elo
            appearances[character] = character_appearances

    return (characters, appearances)
            

def parse():
    red_wins = 0
    blue_wins = 0


    output = stripped_output()

    with open("stripped_output.txt", "w") as f:
        for line in output:
            f.write(line + '\n')

# tiers = {"P": 500, "B": 750, "A": 1000, "S": 1250, "X": 1500} # default elo of a character in this tier
    tiers = {"P": 100, "B": 200, "A": 300, "S": 400, "X": 500} 

    characters = {} # all the character data!
    appearances = {} # counting how many times we've seen them
    edges = [] # list of [first, second], which is all matches played


# parse through output and get the names and tiers
    i = 0
    while i < len(output):
        line = output[i]
        if phrases[0] in line: # Bets are open line, has names and tier
            # use regex to match
            text = line[len(phrases[0]):] # need to chop off the beginning of the line
            first_character, second_character, tier = get_match_info(line)

            if len(tier) == 1: 
                # use tier to initialize characters. 
                tier = tier[0][1] # get the letter
                 
                missing_one = False # for accuracy testing later
                if first_character not in characters:
                    characters[first_character] = tiers[tier]
                    appearances[first_character] = 0 # gets added later
                    missing_one = True
                if second_character not in characters:
                    characters[second_character] = tiers[tier]
                    appearances[second_character] = 0
                    missing_one = True

            # now find the winner, look ahead one
            i += 1
            if i < len(output):
                line = output[i]
                matches = findall(".*wins! Payouts to ", line) # want to be specific to avoid false matches
                if len(matches) == 1: # good, we are on the right line, continue
                    matches = matches[0]
                    winner = matches[:len(matches) - 18]

                    # need to check that both characters have records, since we are counting exhibs now
                    if first_character not in characters or second_character not in characters:
                        i += 1
                        continue

                    # update appearances count
                    appearances[first_character] += 1
                    appearances[second_character] += 1

                    p1, p2 = onevsone(characters[first_character], characters[second_character])
                    # update elo based on who won
                    if winner == first_character:
                        red_wins += 1
                        characters[first_character] = p1[0]
                        characters[second_character] = p2[1]
                    elif winner == second_character:
                        blue_wins += 1
                        characters[first_character] = p1[1]
                        characters[second_character] = p2[0]
                    edges.append([first_character, second_character])

                # else:
                    # incomplete log or other issue, we just will skip
        i += 1

# For accuracy testing, we want to use the elo that we have for them AT THE END
    num_correct = 0
    total = 0 
    i = 0
    while i < len(output):
        line = output[i]
        if phrases[0] in line: # Bets are open line, has names and tier
            first_character, second_character, tier = get_match_info(line)
            
            i += 1
            if i >= len(output):
                continue
            line = output[i]
            matches = findall(".*wins! Payouts to ", line) # want to be specific to avoid false matches
            winner = ""
            if len(matches) == 1: # good, we are on the right line, continue
                matches = matches[0]
                winner = matches[:len(matches) - 18]

            if winner == "":
                i += 1
                continue

            if first_character not in characters or second_character not in characters:
                continue # an exhibition match where we don't see the character ever again. May want to update this later

            # we have first, second, and winner. Now predict who won and find who actually one
            chars = [first_character, second_character]
            pred_winner = 0 if characters[first_character] > characters[second_character] else 1
            if winner == chars[pred_winner]:
                num_correct += 1
            total += 1
        i += 1

    # write to elo.txt LOL
    with open("elo.txt", "w") as f:
        for char in characters.keys():
            write_string = f"{char}, {characters[char]}, {appearances[char]}\n"
            f.write(write_string)

    return (num_correct, total, characters, appearances, edges, red_wins, blue_wins)

def are_unvisited(v):
    for _ in v.keys():
        if v[_][1] == 0:
            return True
    return False

def find_source(v):
    for _ in v.keys():
        if v[_][1] == 0:
            return _

if __name__ == "__main__":
    num_correct, total, characters, appearances, edges, red_wins, blue_wins = parse()
    print(f"Total correct: {num_correct}; total: {total}; Percentage: {num_correct / total}")
    max_app = 0
    max_char = ""
    for char in appearances:
        if appearances[char] > max_app:
            max_app = appearances[char]
            max_char = char
    print(f"Max appearances: {max_app} on character {max_char}")
    print(f"Number of characters: {len(characters.keys())}")

    characters_with_more_than_one_appearance = 0
    for character in appearances.keys():
        if appearances[character] > 1:
            characters_with_more_than_one_appearance += 1

    print(f"Number of characters with more than one appearance: {characters_with_more_than_one_appearance}")
    print(f"Number of red wins: {red_wins}; number of blue wins: {blue_wins}")

    # calculate connected components
    v = {}
    components = []
    for key in characters.keys():
        v[key] = ([], 0)

    # set up the edges
    for edge in edges:
        v[edge[0]][0].append(edge[1])
        v[edge[1]][0].append(edge[0]) 

    while are_unvisited(v):
        # at the top of this function we are finding another connected component
        component = []
        source = find_source(v)
        queue = [source]
        component.append(source)
        v[source] = (v[source][0], 1)
        while len(queue) != 0:
            i = queue.pop(0) # pop and set as visited
            component.append(i)
            # append all connected edges
            for edge in v[i][0]:
                if v[edge][1] != 1: # hasn't already been visited
                    queue.append(edge)
                    v[edge] = (v[edge][0], 1)
        components.append(component)
    print(f"Number of connected components: {len(components)}")
    largest_component = max([len(_) for _ in components])
    print(f"The largest connected component is: {largest_component}")

    component_lengths = [len(_) for _ in components]
    component_lengths.sort(reverse=True)
    print(f"Top 10 largest components: {component_lengths[:10]}")

    G = nx.Graph()
    G.add_nodes_from(characters.keys())
    for edge in edges:
        G.add_edge(edge[0], edge[1])

    # nx.draw(G, with_labels=True, font_weight='bold') 
    # plt.show()
