import os
import re
import json
from typing import Optional
from pathlib import Path
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import RedirectResponse, JSONResponse

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# =========================
# ENV
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()

GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", "").strip()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "").strip()

SCOPES = ["https://www.googleapis.com/auth/drive"]
YEAR_FIXED = "2026"

# Secret File Render (read-only)
TOKEN_PATH = Path("/etc/secrets/token.json")

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

    # Показываем токен как JSON
    return JSONResponse(
        content={
            "message": "СКОПИРУЙ ЭТО И ВСТАВЬ В Secret File token.json",
            "token_json": json.loads(creds.to_json())
        }
    )


def _drive_service():
    if not TOKEN_PATH.exists():
        raise RuntimeError("OAuth не пройден. Открой /auth")

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)

# =========================
# Utils
# =========================

async def tg_send_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})


def safe_name(s: str) -> str:
    s = (s or "").strip().replace("/", "_")
    s = re.sub(r"\s+", " ", s)
    return s[:120]


def drive_get_or_create_folder(service, name: str, parent_id: str):
    q = (
        "mimeType='application/vnd.google-apps.folder' and trashed=false and "
        f"'{parent_id}' in parents and name='{name}'"
    )

    res = service.files().list(
        q=q,
        fields="files(id,name,webViewLink)",
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

# =========================
# Deal Structure (8 папок)
# =========================

def build_deal_structure(service, client_name, deal_name):
    date_str = datetime.now().strftime("%Y-%m-%d")
    deal_folder_name = f"{deal_name}_{date_str}"

    year_folder = drive_get_or_create_folder(service, YEAR_FIXED, GDRIVE_ROOT_FOLDER_ID)
    client_folder = drive_get_or_create_folder(service, client_name, year_folder["id"])
    deal_folder = drive_get_or_create_folder(service, deal_folder_name, client_folder["id"])

    subfolders = [
        "01_КП_для_клиента",
        "02_Себестоимость",
        "03_Договор_и_приложение",
        "04_Счета_и_закрывашки",
        "05_Макеты_и_векторы",
        "06_Закрывашки",
        "07_Честный_знак",
        "08_Фото_от_клиента",
    ]

    for folder in subfolders:
        drive_get_or_create_folder(service, folder, deal_folder["id"])

    return deal_folder["webViewLink"]

# =========================
# Webhook
# =========================

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    if TELEGRAM_SECRET_TOKEN:
        if (x_telegram_bot_api_secret_token or "") != TELEGRAM_SECRET_TOKEN:
            return {"ok": True}

    update = await request.json()

    try:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()

        if not chat_id or not text:
            return {"ok": True}

        if text == "/start":
            await tg_send_message(chat_id, "Отправь: Клиент: РЖД; Сделка: куртки 300")
            return {"ok": True}

        match = re.search(r"Клиент:\s*(.+?);\s*Сделка:\s*(.+)", text)
        if not match:
            await tg_send_message(chat_id, "Формат: Клиент: XXX; Сделка: YYY")
            return {"ok": True}

        client_name = safe_name(match.group(1))
        deal_name = safe_name(match.group(2))

        service = _drive_service()
        link = build_deal_structure(service, client_name, deal_name)

        await tg_send_message(chat_id, f"📁 Папка создана:\n{link}")

    except Exception as e:
        print("Drive error:", repr(e))
        await tg_send_message(chat_id, "Ошибка Drive")

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return {"status": "Bot is running"}
