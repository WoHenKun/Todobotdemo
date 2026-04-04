import os
import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import anthropic

app = FastAPI()
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]


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


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()

    # Feishu URL verification handshake
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body["challenge"]})

    event = body.get("event", {})
    message = event.get("message", {})

    if message.get("message_type") != "text":
        return JSONResponse({"status": "ignored"})

    user_text = json.loads(message["content"])["text"]
    chat_id = message["chat_id"]

    # Call Claude
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_text}],
    )
    reply = response.content[0].text

    await send_message(chat_id, reply)
    return JSONResponse({"status": "ok"})
