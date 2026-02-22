# ==============================
# BRANDCOMM AI BOT FULL VERSION
# ==============================

import os
import re
import io
import json
from typing import Optional, Dict, Any
from pathlib import Path
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import RedirectResponse, JSONResponse

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# =========================
# ENV
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()
GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", "").strip()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

TOKEN_JSON_ENV = os.getenv("TOKEN_JSON", "").strip()

SCOPES = ["https://www.googleapis.com/auth/drive"]
YEAR_FIXED = "2026"
TOKEN_PATH = Path("/etc/secrets/token.json")

app = FastAPI()

# chat memory
CHAT_CTX = {}

# =========================
# DRIVE AUTH
# =========================

def _google_client_config():
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }

def _load_token_info():
    if TOKEN_PATH.exists():
        return json.loads(TOKEN_PATH.read_text())
    if TOKEN_JSON_ENV:
        return json.loads(TOKEN_JSON_ENV)
    raise RuntimeError("OAuth not completed.")

def _drive_service():
    creds = Credentials.from_authorized_user_info(_load_token_info(), SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)

# =========================
# OAUTH
# =========================

@app.get("/auth")
def auth():
    flow = Flow.from_client_config(_google_client_config(), scopes=SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent"
    )
    return RedirectResponse(authorization_url)

@app.get("/oauth2callback")
async def oauth2callback(request: Request):
    flow = Flow.from_client_config(_google_client_config(), scopes=SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials
    return {
        "token_json": json.loads(creds.to_json())
    }

# =========================
# DRIVE HELPERS
# =========================

def drive_get_or_create_folder(service, name, parent_id):
    q = f"mimeType='application/vnd.google-apps.folder' and trashed=false and '{parent_id}' in parents and name='{name}'"
    res = service.files().list(
        q=q,
        fields="files(id,name,webViewLink)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files", [])
    if files:
        return files[0]

    return service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()

def drive_upload_bytes(service, parent_id, filename, data):
    media = MediaIoBaseUpload(io.BytesIO(data), resumable=False)
    file = service.files().create(
        body={"name": filename, "parents": [parent_id]},
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()
    return file["webViewLink"]

# =========================
# TELEGRAM HELPERS
# =========================

async def tg_send(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})

async def tg_get_file(file_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params={"file_id": file_id})
        return r.json()["result"]["file_path"]

async def tg_download(file_path):
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        return r.content

# =========================
# GPT
# =========================

async def ask_gpt(text):
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY not set"

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    json_data = {
        "model": "gpt-5.2",
        "input": text
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=json_data)
        data = r.json()
        return data["output_text"]

# =========================
# WEBHOOK
# =========================

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text")

    if not chat_id:
        return {"ok": True}

    if text == "/start":
        await tg_send(chat_id, "Напиши: Клиент: РЖД; Сделка: куртки 300")
        return {"ok": True}

    if text and text.startswith("Клиент:"):
        service = _drive_service()

        client = re.search(r"Клиент:\s*(.+?);", text).group(1)
        deal = re.search(r"Сделка:\s*(.+)", text).group(1)

        year = drive_get_or_create_folder(service, YEAR_FIXED, GDRIVE_ROOT_FOLDER_ID)
        client_folder = drive_get_or_create_folder(service, client, year["id"])
        deal_folder = drive_get_or_create_folder(service, deal, client_folder["id"])

        CHAT_CTX[chat_id] = {"deal_id": deal_folder["id"]}

        await tg_send(chat_id, f"Папка создана:\n{deal_folder['webViewLink']}")
        return {"ok": True}

    # file upload
    if "document" in message or "photo" in message:
        service = _drive_service()
        ctx = CHAT_CTX.get(chat_id)
        if not ctx:
            await tg_send(chat_id, "Сначала создай сделку.")
            return {"ok": True}

        if "document" in message:
            file_id = message["document"]["file_id"]
            filename = message["document"]["file_name"]
        else:
            file_id = message["photo"][-1]["file_id"]
            filename = "photo.jpg"

        path = await tg_get_file(file_id)
        data = await tg_download(path)

        link = drive_upload_bytes(service, ctx["deal_id"], filename, data)
        await tg_send(chat_id, f"Файл загружен:\n{link}")
        return {"ok": True}

    # GPT chat
    if text:
        reply = await ask_gpt(text)
        await tg_send(chat_id, reply)

    return {"ok": True}

# =========================
# HEALTH
# =========================

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/")
def root():
    return {"status": "Bot is running"}
