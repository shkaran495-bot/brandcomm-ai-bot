import os
import re
from datetime import datetime
from typing import Optional
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# =========================
# ENV
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_TABLE_DEALS = os.getenv("AIRTABLE_TABLE_DEALS", "Deals").strip()

GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", "").strip()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "").strip()

SCOPES = ["https://www.googleapis.com/auth/drive"]
YEAR_FIXED = "2026"

CHAT_CONTEXT: dict[int, dict] = {}

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
    authorization_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
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


def drive_upload_file(service, local_path: Path, filename: str, parent_folder_id: str):
    media = MediaFileUpload(str(local_path), resumable=False)
    return service.files().create(
        body={"name": filename, "parents": [parent_folder_id]},
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()

# =========================
# Deal structure
# =========================

def build_deal_structure(service, client_name, deal_name):
    date_str = datetime.now().strftime("%Y-%m-%d")
    deal_folder_name = f"{deal_name}_{date_str}"

    year_folder = drive_get_or_create_folder(service, YEAR_FIXED, GDRIVE_ROOT_FOLDER_ID)
    client_folder = drive_get_or_create_folder(service, client_name, year_folder["id"])
    deal_folder = drive_get_or_create_folder(service, deal_folder_name, client_folder["id"])

    subfolders = [
        "01_КП_для клиента",
        "02_Себестоимость",
        "03_Договор_и_приложение",
        "04_Счета_и_закрывашки",
        "05_Макеты_и_векторы",
        "06_Закрывашки",
        "07_Честный_знак",
        "08_Фото_от_клиента",
    ]

    sub_map = {}
    for sf in subfolders:
        folder = drive_get_or_create_folder(service, sf, deal_folder["id"])
        sub_map[sf] = folder["id"]

    return deal_folder, sub_map

# =========================
# Webhook
# =========================

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    msg = update.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    if "клиент" in text.lower() and "сделка" in text.lower():
        service = _drive_service()
        m = re.search(r"Клиент:\s*(.+?);\s*Сделка:\s*(.+)", text)
        if not m:
            await tg_send_message(chat_id, "Формат: Клиент: XXX; Сделка: YYY")
            return {"ok": True}

        client = safe_name(m.group(1))
        deal = safe_name(m.group(2))

        deal_folder, sub_map = build_deal_structure(service, client, deal)

        CHAT_CONTEXT[chat_id] = {
            "deal_id": deal_folder["id"],
            "subfolders": sub_map,
        }

        await tg_send_message(chat_id, f"Сделка создана ✅\n{deal_folder['webViewLink']}")
        return {"ok": True}

    # FILES
    if msg.get("document"):
        ctx = CHAT_CONTEXT.get(chat_id)
        if not ctx:
            await tg_send_message(chat_id, "Сначала создай сделку.")
            return {"ok": True}

        service = _drive_service()
        doc = msg["document"]
        file_id = doc["file_id"]
        file_name = doc.get("file_name") or "file"

        # simple default
        target_folder_id = ctx["deal_id"]

        # download
        file_info = await httpx.AsyncClient().get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id},
        )
        file_path = file_info.json()["result"]["file_path"]

        file_content = await httpx.AsyncClient().get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        )

        tmp = Path("/tmp") / file_name
        tmp.write_bytes(file_content.content)

        uploaded = drive_upload_file(service, tmp, file_name, target_folder_id)

        await tg_send_message(chat_id, f"Файл загружен ✅\n{uploaded['webViewLink']}")
        return {"ok": True}

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}
