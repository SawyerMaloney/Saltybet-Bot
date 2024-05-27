from twitchAPI.twitch import Twitch
from twitchAPI.helper import first
from twitchAPI.oauth import UserAuthenticator
from twitchAPI.type import AuthScope
from twitchAPI.eventsub.websocket import EventSubWebsocket
from twitchAPI.object.eventsub import ChannelChatMessageEvent
from datetime import datetime

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

async def on_message(data: ChannelChatMessageEvent):
    data = data.to_dict()

    if data["event"]["chatter_user_id"] == "55853880":
        message = data["event"]["message"]["text"]
        # now open file and write 
        time = datetime.now().strftime("%H:%M:%S")
        print_message = f"Writing {message} at {time}" 
        print(print_message)
        with open("output.txt", "a") as f:
            f.write(message + '\n')

asyncio.run(run())
