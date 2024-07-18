from fastapi import FastAPI, Request
from pydantic import BaseModel
import httpx
import uvicorn

app = FastAPI()

# Naver TalkTalk API configuration
TALKTALK_API_URL = "https://gw.talk.naver.com/chatbot/v1/event"
TALKTALK_API_TOKEN = os.environ.get('TALKTALK_API_TOKEN')

# Sendbird API configuration
SENDBIRD_API_URL = os.environ.get('SENDBIRD_API_URL')
SENDBIRD_API_TOKEN = os.environ.get('SENDBIRD_API_TOKEN')
BOT_USER_ID = "project1_chatbot"

# In-memory cache for user existence
user_cache = set()
# In-memory cache to track processed events
processed_events = set()
# In-memory cache to track messages sent to Sendbird
talktalk_messages = {}

class TalkTalkEvent(BaseModel):
    event: str
    user: str
    textContent: dict = None

@app.post("/webhook")
async def handle_talktalk_webhook(event: TalkTalkEvent):
    print(f"[DEBUG] Received TalkTalk event: {event.dict()}")
    
    event_id = f"{event.user}-{event.textContent.get('text', '') if event.textContent else ''}"
    
    if event_id in processed_events:
        print(f"[DEBUG] Event {event_id} has already been processed. Skipping.")
        return {"status": "ok"}
    
    user_id = event.user
    user_message = event.textContent.get("text", "") if event.textContent else ""
    
    print(f"[DEBUG] Processing event: user_id={user_id}, message='{user_message}'")
    
    if event.event == "send" and user_message:

        await send_typing_indicator(user_id, "typingOn")

        if user_id in talktalk_messages and talktalk_messages[user_id]['message'] == user_message:
            print(f"[DEBUG] Ignoring echo message: {user_message}")
            await send_typing_indicator(user_id, "typingOff")
            return {"status": "ok"}
        
        # Ensure the user exists in Sendbird
        user = await create_sendbird_user(user_id)
        if not user:
            print(f"[ERROR] Failed to create or retrieve user {user_id} in Sendbird")
            await send_typing_indicator(user_id, "typingOff")
            return {"status": "error", "message": "Failed to create or retrieve user"}

        print(f"[DEBUG] Sending message to Sendbird: user_id={user_id}, message='{user_message}'")
        channel_url = await send_distinct_message(user_id, BOT_USER_ID, user_message)
        
        if channel_url:
            talktalk_messages[user_id] = {'message': user_message, 'channel_url': channel_url}
            processed_events.add(event_id)
            print(f"[DEBUG] Message sent to Sendbird: channel_url={channel_url}")
        else:
            print(f"[ERROR] Failed to send message to Sendbird for user {user_id}")
            await send_typing_indicator(user_id, "typingOff")
            return {"status": "error", "message": "Failed to send message to Sendbird"}
    
    elif event.event == "echo":
        print(f"[DEBUG] Received echo event, ignoring: {event.dict()}")
        return {"status": "ok"}
    
    else:
        print(f"[DEBUG] Unhandled event type: {event.event}")
    
    return {"status": "ok"}

@app.post("/sbwebhook")
async def handle_sendbird_webhook(request: Request):
    payload = await request.json()
    print(f"[DEBUG] Received Sendbird webhook payload: {payload}")
    
    if payload.get('category') == 'group_channel:message_send':
        channel_url = payload['channel']['channel_url']
        sender_id = payload['sender']['user_id']
        message = payload['payload']['message']
        print(f"[DEBUG] Received message from Sendbird: channel_url={channel_url}, sender_id={sender_id}, message='{message}'")
        
        # Only send the message to TalkTalk if it's from the bot
        if sender_id == BOT_USER_ID:
            for user_id, data in talktalk_messages.items():
                if data['channel_url'] == channel_url:
                    # Send typing indicator
                    await send_typing_indicator(user_id, "typingOn")
                    
                    print(f"[DEBUG] Sending bot response to TalkTalk: user_id={user_id}, message='{message}'")
                    await send_response_to_talktalk(user_id, message)
                    
                    # Turn off typing indicator
                    await send_typing_indicator(user_id, "typingOff")
                    break
    
    return {"status": "ok"}

async def create_sendbird_user(user_id: str):
    async with httpx.AsyncClient() as client:
        # Create the user
        create_user_url = f"{SENDBIRD_API_URL}/users"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Api-Token": SENDBIRD_API_TOKEN
        }
        payload = {
            "user_id": user_id,
            "nickname": user_id,
            "profile_url": "",  # Include profile_url, it can be empty
            "issue_access_token": True
        }
        create_response = await client.post(create_user_url, headers=headers, json=payload)
        if create_response.status_code == 200:
            print(f"User {user_id} created successfully.")
            return create_response.json()
        elif create_response.status_code == 400 and create_response.json().get("code") == 400202:
            print(f"User {user_id} already exists.")
            return {"user_id": user_id}
        else:
            print(f"Failed to create user {user_id}: {create_response.json()}")
            return None

async def send_distinct_message(sender_id: str, receiver_id: str, message: str):
    async with httpx.AsyncClient() as client:
        send_message_url = f"{SENDBIRD_API_URL}/group_channels/distinct_message"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Api-Token": SENDBIRD_API_TOKEN
        }
        payload = {
            "sender_id": sender_id,
            "receiver_ids": [receiver_id],
            "message_payload": {
                "message_type": "MESG",
                "message": message,
                "user_id": sender_id
            },
            "create_channel": True  # Ensure a channel is created if it doesn't exist
        }
        response = await client.post(send_message_url, headers=headers, json=payload)
        if response.status_code == 200:
            response_data = response.json()
            print(f"Message sent successfully: {response_data}")
            return response_data['channel_url']
        else:
            print(f"Failed to send message: {response.json()}")
            return None
        
async def send_typing_indicator(user_id: str, action: str):
    async with httpx.AsyncClient() as client:
        payload = {
            "event": "action",
            "user": user_id,
            "options": {
                "action": action
            }
        }
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": TALKTALK_API_TOKEN
        }
        response = await client.post(TALKTALK_API_URL, json=payload, headers=headers)
        print(f"[DEBUG] Sent typing indicator to TalkTalk: action={action}, status_code={response.status_code}")

async def send_response_to_talktalk(user_id: str, response: str):
    print(f"[DEBUG] Preparing to send response to TalkTalk: user_id={user_id}, message='{response}'")
    async with httpx.AsyncClient() as client:
        payload = {
            "event": "send",
            "user": user_id,
            "textContent": {
                "text": response
            }
        }
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": TALKTALK_API_TOKEN
        }
        response = await client.post(TALKTALK_API_URL, json=payload, headers=headers)
        print(f"[DEBUG] Sent response to TalkTalk: status_code={response.status_code}, response={response.json()}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
