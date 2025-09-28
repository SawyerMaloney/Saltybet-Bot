from twitchAPI.twitch import Twitch
from twitchAPI.helper import first
from twitchAPI.oauth import UserAuthenticator
from twitchAPI.type import AuthScope
from twitchAPI.eventsub.websocket import EventSubWebsocket
from twitchAPI.object.eventsub import ChannelChatMessageEvent
from datetime import datetime
import parser
from playsound import playsound
from re import findall

from dotenv import load_dotenv
import os
import asyncio

load_dotenv() # loading secrets

my_app_id = os.getenv("ID")
my_app_secret = os.getenv("SECRET")

async def run():
    twitch = await Twitch(my_app_id, my_app_secret)

    # user auth
    target_scope = [AuthScope.USER_READ_CHAT] # need to find right scope
    auth = UserAuthenticator(twitch, target_scope, force_verify=False)
    token, refresh_token = await auth.authenticate()
    await twitch.set_user_authentication(token, target_scope, refresh_token)
    user = await first(twitch.get_users())

    eventsub = EventSubWebsocket(twitch)
    eventsub.start()
    broadcaster_user_id = "43201452" # found this somewhere
    await eventsub.listen_channel_chat_message(broadcaster_user_id, user.id, on_message)

    try:
        input("press Enter to shut down...")
    except KeyboardInterrupt:
        pass
    finally:
        await eventsub.stop()
        await twitch.close()

chars = ["", ""]
async def on_message(data: ChannelChatMessageEvent):
    data = data.to_dict()

    if data["event"]["chatter_user_id"] == "55853880":
        message = data["event"]["message"]["text"]
    
        # before we write, check if it's "Bets are OPEN" and, if so, print out the elo's of characters
        if "Bets are OPEN for " in message:
            first, second, tier = parser.get_match_info(message)
            chars[0] = first
            chars[1] = second
            # use elo.txt to find characters? Maybe have this run on a timer loop (updating elo.txt from output)
            characters, appearances = parser.data_from_elo()
            if first in characters:
                print(f"\033[0;31m{first}:\033[0m {float(characters[first]):.2f}, appearances: {appearances[first]}")
            else:
                print(f"\033[0;31m{first}\033[0m not in characters")

            if second in characters:
                print(f"\033[0;34m{second}:\033[0m {float(characters[second]):.2f}, appearances: {appearances[second]}")
            else:
                print(f"\033[0;34m{second}\033[0m not in characters")

            # if first in characters and second in characters:
                # playsound("ding.mp3")

        if "wins! Payouts to " in message:
            # win message
            match = findall(".*wins! Payouts to ", message)[0]
            winner = match[:len(match) - 18]
            if winner == chars[0]:
                print(f"\033[0;31m{chars[0]}\033[0m wins!")
            elif winner == chars[1]:
                print(f"\033[0;34m{chars[1]}\033[0m wins!")
            print("\n") # make some space in the output

        # now open file and write 
        time = datetime.now().strftime("%H:%M:%S")
        print_message = f"Writing {message} at {time}\n" 
        with open("log.txt", "a") as log:
            log.write(print_message)
        with open("output.txt", "a") as f:
            f.write(message + '\n')

        # run parser to update
        parser.parse()

asyncio.run(run())
