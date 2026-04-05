import os
import json
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from openai import OpenAI
from supabase import create_client

app = FastAPI()
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
from collections import deque
processed_events: deque[str] = deque(maxlen=10)
# chat_id -> {"parsed": "formatted todo string"}
pending_todos: dict[str, str] = {}

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

从用户输入中提取信息，只返回如下JSON，不要有其他内容：
{{
  "is_todo": true,
  "name": "todo的具体内容",
  "due": "ISO8601格式时间，如2026-04-10T17:00:00，没有则为null",
  "importance": "非常紧急/紧急/普通/不紧急，根据语气判断，没有则为null"
}}

如果不是todo，返回：{{"is_todo": false}}

今天的日期是：{today}"""

async def process_message(chat_id: str, user_text: str, user_open_id: str = None):
    # Handle confirmation flow
    if chat_id in pending_todos:
        if user_text.strip() == "1":
            todo = pending_todos.pop(chat_id)
            supabase.table("todos").insert({
                "name": todo["name"],
                "due": todo.get("due"),
                "importance": todo.get("importance"),
                "user_id": user_open_id,
            }).execute()
            await send_message(chat_id, "✅ Todo已录入！")
        elif user_text.strip() == "2":
            pending_todos.pop(chat_id)
            await send_message(chat_id, "已取消。")
        else:
            await send_message(chat_id, "请回复 1 确认添加 或 2 取消")
        return

    # Parse new todo
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
    todo = json.loads(response.choices[0].message.content)

    if not todo.get("is_todo"):
        await send_message(chat_id, "这好像不是一个todo呢")
        return

    # Store parsed todo and ask for confirmation
    pending_todos[chat_id] = todo
    lines = ["新增Todo：", f"📝 {todo['name']}"]
    if todo.get("due"):
        lines.append(f"🕔 {todo['due']}")
    if todo.get("importance"):
        lines.append(f"⚠️ {todo['importance']}")
    lines.append("\n确认添加吗？\n1 确认  2 取消")
    await send_message(chat_id, "\n".join(lines))


@app.get("/todos")
async def get_todos(user_id: str):
    result = supabase.table("todos").select("*").eq("user_id", user_id).execute()
    return result.data


@app.delete("/todos/{todo_id}")
async def delete_todo(todo_id: str):
    supabase.table("todos").delete().eq("id", todo_id).execute()
    return JSONResponse({"status": "deleted"})


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    # Feishu URL verification handshake
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body["challenge"]})

    # Deduplicate events - Feishu retries if no response within timeout
    event_id = body.get("header", {}).get("event_id")
    if event_id:
        if event_id in processed_events:
            return JSONResponse({"status": "duplicate"})
        processed_events.append(event_id)

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
    user_open_id = sender.get("sender_id", {}).get("open_id")

    # Return 200 immediately, process in background to avoid Feishu retry
    background_tasks.add_task(process_message, chat_id, user_text, user_open_id)
    return JSONResponse({"status": "ok"})
