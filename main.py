import os
import json
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from openai import OpenAI

app = FastAPI()
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
BOT_OPEN_ID = None


@app.on_event("startup")
async def startup():
    global BOT_OPEN_ID
    token = await get_tenant_token()
    async with httpx.AsyncClient() as client:
        res = await client.get(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        BOT_OPEN_ID = res.json().get("bot", {}).get("open_id")
        print("BOT_OPEN_ID:", BOT_OPEN_ID)


async def get_tenant_token() -> str:
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        return res.json()["tenant_access_token"]


async def send_message(chat_id: str, text: str):
    token = await get_tenant_token()
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )


SYSTEM_PROMPT = """你是一个Todo助手。用户会用自然语言告诉你一个待办事项。

你需要从用户输入中提取以下信息并按格式回复：
- todo内容（具体要做什么）
- due时间（什么时候截止，转换为具体日期时间）
- 重要性（根据语气和内容判断，比如"非常紧急"、"普通"、"不紧急"）

回复格式（没有的信息就省略那一行）：
新增Todo：
📝 <todo内容>
🕔 <due时间>
⚠️ <重要性>

如果用户输入的不是todo，只回复：这好像不是一个todo呢

不要添加任何其他内容。今天的日期是：{today}"""

async def process_message(chat_id: str, user_text: str):
    from datetime import date
    today = date.today().strftime("%Y年%m月%d日")
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        max_tokens=256,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(today=today)},
            {"role": "user", "content": user_text},
        ],
    )
    reply = response.choices[0].message.content
    await send_message(chat_id, reply)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    # Feishu URL verification handshake
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body["challenge"]})

    event = body.get("event", {})
    message = event.get("message", {})

    # Ignore messages sent by the bot itself
    sender = event.get("sender", {})
    sender_open_id = sender.get("sender_id", {}).get("open_id")
    if sender_open_id and sender_open_id == BOT_OPEN_ID:
        return JSONResponse({"status": "ignored"})

    if message.get("message_type") != "text":
        return JSONResponse({"status": "ignored"})

    user_text = json.loads(message["content"])["text"]
    chat_id = message["chat_id"]

    # Return 200 immediately, process in background to avoid Feishu retry
    background_tasks.add_task(process_message, chat_id, user_text)
    return JSONResponse({"status": "ok"})
