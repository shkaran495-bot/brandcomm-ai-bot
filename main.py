import os
import json
import re
import asyncio
from datetime import datetime
from typing import Optional
from pathlib import Path

import anyio
import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# -------------------------
# ENV
# -------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_TABLE_DEALS = os.getenv("AIRTABLE_TABLE_DEALS", "Deals").strip()

GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", "").strip()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "").strip()

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()

SCOPES = ["https://www.googleapis.com/auth/drive"]

CHAT_CONTEXT: dict[int, dict] = {}
YEAR_FIXED = "2026"

app = FastAPI()

# =========================
# OAuth
# =========================

@app.get("/auth")
def auth():
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
    )

    flow.redirect_uri = GOOGLE_REDIRECT_URI
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )

    return RedirectResponse(authorization_url)


@app.get("/oauth2callback")
async def oauth2callback(request: Request):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
    )

    flow.redirect_uri = GOOGLE_REDIRECT_URI
    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials

    with open("/tmp/token.json", "w") as f:
        f.write(creds.to_json())

    return {"status": "OAuth completed. Token saved."}


def _drive_service():
    token_path = Path("/tmp/token.json")
    if not token_path.exists():
        raise RuntimeError("OAuth не пройден. Открой /auth")

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# =========================
# Utils
# =========================

async def tg_send_message(chat_id: int, text: str):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})


def safe_name(s: str) -> str:
    s = (s or "").strip().replace("/", "_")
    s = re.sub(r"\s+", " ", s)
    return s[:120] if len(s) > 120 else s


def drive_get_or_create_folder(service, name: str, parent_id: str):
    name = safe_name(name)
    q = (
        "mimeType='application/vnd.google-apps.folder' and trashed=false and "
        f"'{parent_id}' in parents and name='{name}'"
    )

    res = service.files().list(
        q=q,
        fields="files(id,name)",
        supportsAllDrives=True,
    ).execute()

    files = res.get("files", [])
    if files:
        return files[0]

    file_metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    return service.files().create(
        body=file_metadata,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()


def drive_upload_file(service, local_path: Path, filename: str, parent_folder_id: str):
    file_metadata = {"name": filename, "parents": [parent_folder_id]}
    media = MediaFileUpload(str(local_path), resumable=False)

    return service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()


# =========================
# Webhook (ВАЖНОЕ)
# =========================

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    # 1) Проверяем secret_token, если он задан в ENV
    if TELEGRAM_SECRET_TOKEN:
        if (x_telegram_bot_api_secret_token or "") != TELEGRAM_SECRET_TOKEN:
            # Возвращаем 200, но ничего не делаем — защита от чужих запросов
            return {"ok": True}

    # 2) Получаем update от Telegram
    update = await request.json()

    # 3) Мини-обработчик: отвечаем пользователю, чтобы увидеть что всё работает
    try:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        text = (message.get("text") or "").strip()

        if chat_id:
            if text == "/start":
                await tg_send_message(
                    chat_id,
                    "✅ Webhook работает. Напиши: 'Клиент: РЖД; Сделка: куртки 300' — и я отвечу эхо (пока без Drive/Airtable).",
                )
            elif text:
                await tg_send_message(chat_id, f"✅ Принял: {text}")
    except Exception as e:
        # В логах Render будет видно, если что-то упало
        print("Webhook handler error:", repr(e))

    # 4) Telegramу важно получить 200 OK
    return {"ok": True}


# =========================
# Health / Root
# =========================

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return {"status": "Bot is running"}
