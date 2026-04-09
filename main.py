import os
import json
import httpx
from collections import deque
from datetime import date
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from openai import OpenAI
from supabase import create_client

app = FastAPI()
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

processed_events: deque[str] = deque(maxlen=10)
pending_todos: dict[str, dict] = {}

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
BOT_OPEN_ID = None

SYSTEM_PROMPT = """You are a Todo assistant. The user will describe a task in natural language.

Extract the information and return ONLY the following JSON, no other content:
{{
  "is_todo": true,
  "name": "the specific task",
  "due": "ISO8601 datetime e.g. 2026-04-10T17:00:00, or null if not specified",
  "importance": "very urgent / urgent / normal / low, inferred from tone, or null if unclear"
}}

If the input is not a todo, return: {{"is_todo": false}}

Today's date is: {today}"""


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


def get_supabase_user_id(feishu_open_id: str) -> str | None:
    result = (
        supabase.table("user_identities")
        .select("user_id")
        .eq("provider", "feishu")
        .eq("provider_uid", feishu_open_id)
        .maybe_single()
        .execute()
    )
    return result.data.get("user_id") if result.data else None


def parse_todo(user_text: str) -> dict:
    today = date.today().strftime("%Y-%m-%d")
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        max_tokens=256,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(today=today)},
            {"role": "user", "content": user_text},
        ],
    )
    return json.loads(response.choices[0].message.content)


async def process_message(chat_id: str, user_text: str, user_open_id: str = None):
    # Handle confirmation flow
    if chat_id in pending_todos:
        if user_text.strip() == "1":
            todo = pending_todos.pop(chat_id)
            user_id = get_supabase_user_id(user_open_id) if user_open_id else None
            if not user_id:
                await send_message(chat_id, "Please link your Feishu account in the app first.")
                return
            supabase.table("todos").insert({
                "name": todo["name"],
                "due": todo.get("due"),
                "importance": todo.get("importance"),
                "user_id": user_id,
            }).execute()
            await send_message(chat_id, "Todo saved!")
        elif user_text.strip() == "2":
            pending_todos.pop(chat_id)
            await send_message(chat_id, "Cancelled.")
        else:
            await send_message(chat_id, "Reply 1 to confirm or 2 to cancel.")
        return

    if not user_open_id or not get_supabase_user_id(user_open_id):
        await send_message(chat_id, "Please link your Feishu account in the app first.")
        return

    todo = parse_todo(user_text)

    if not todo.get("is_todo"):
        await send_message(chat_id, "That doesn't look like a todo.")
        return

    pending_todos[chat_id] = todo
    lines = ["New Todo:", f"📝 {todo['name']}"]
    if todo.get("due"):
        lines.append(f"🕔 {todo['due']}")
    if todo.get("importance"):
        lines.append(f"⚠️ {todo['importance']}")
    lines.append("\nConfirm?\n1 Yes  2 No")
    await send_message(chat_id, "\n".join(lines))


@app.post("/auth/feishu")
async def feishu_auth(request: Request):
    body = await request.json()
    code = body["code"]
    redirect_uri = body["redirect_uri"]

    token_resp = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    app_access_token = token_resp.json()["app_access_token"]

    user_resp = httpx.post(
        "https://open.feishu.cn/open-apis/authen/v1/access_token",
        headers={"Authorization": f"Bearer {app_access_token}"},
        json={"grant_type": "authorization_code", "code": code},
    )
    data = user_resp.json()["data"]

    return {
        "open_id": data["open_id"],
        "meta": {
            "name": data.get("name"),
            "avatar_url": data.get("avatar_url"),
        },
    }


@app.get("/todos")
async def get_todos(user_id: str):
    result = supabase.table("todos").select("*").eq("user_id", user_id).execute()
    return result.data


@app.post("/todos")
async def create_todo(request: Request):
    body = await request.json()
    user_text = body.get("text", "")
    user_id = body.get("user_id")

    todo = parse_todo(user_text)

    if not todo.get("is_todo"):
        return JSONResponse(
            {"is_todo": False, "message": f"Not a todo: {user_text}"},
            status_code=422,
        )

    result = supabase.table("todos").insert({
        "name": todo["name"],
        "due": todo.get("due"),
        "importance": todo.get("importance"),
        "user_id": user_id,
    }).execute()
    return result.data[0]


@app.delete("/todos/{todo_id}")
async def delete_todo(todo_id: str):
    supabase.table("todos").delete().eq("id", todo_id).execute()
    return JSONResponse({"status": "deleted"})


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body["challenge"]})

    event_id = body.get("header", {}).get("event_id")
    if event_id:
        if event_id in processed_events:
            return JSONResponse({"status": "duplicate"})
        processed_events.append(event_id)

    event = body.get("event", {})
    message = event.get("message", {})

    sender = event.get("sender", {})
    sender_open_id = sender.get("sender_id", {}).get("open_id")
    if sender_open_id and sender_open_id == BOT_OPEN_ID:
        return JSONResponse({"status": "ignored"})

    if message.get("message_type") != "text":
        return JSONResponse({"status": "ignored"})

    user_text = json.loads(message["content"])["text"]
    chat_id = message["chat_id"]
    user_open_id = sender.get("sender_id", {}).get("open_id")

    background_tasks.add_task(process_message, chat_id, user_text, user_open_id)
    return JSONResponse({"status": "ok"})
