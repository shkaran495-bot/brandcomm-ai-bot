import os
import json
import base64
import re
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel

from google.oauth2 import service_account
from googleapiclient.discovery import build


# -------------------------
# ENV
# -------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "")  # optional
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_DEALS = os.getenv("AIRTABLE_TABLE_DEALS", "Deals")

# ID корневой папки Brandcomm в Drive (папка, куда ты дал доступ service account)
GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", "")

# JSON сервисного аккаунта (base64) — безопасно хранить как env
GOOGLE_SA_JSON_B64 = os.getenv("GOOGLE_SA_JSON_B64", "").strip()

# Webhook URL (Render даст после деплоя). Нужен чтобы поставить webhook.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # например https://brandcomm-ai-bot.onrender.com


# -------------------------
# Helpers: Telegram
# -------------------------
async def tg_send_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})


async def tg_set_webhook():
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL is empty")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    payload = {
        "url": f"{PUBLIC_BASE_URL}/telegram/webhook",
    }
    # optional secret token (Telegram will pass it in header X-Telegram-Bot-Api-Secret-Token)
    if TELEGRAM_SECRET_TOKEN:
        payload["secret_token"] = TELEGRAM_SECRET_TOKEN

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


# -------------------------
# Helpers: Airtable
# -------------------------
async def airtable_create_deal(client_name: str, deal_name: str, drive_folder_url: str, drive_folder_id: str):
    """
    Создаём запись в Airtable.
    В таблице Deals должны быть поля (создадим позже, но лучше сразу):
      - Client (single line text)
      - DealName (single line text)
      - Status (single select)  [optional]
      - DriveFolderUrl (url)
      - DriveFolderId (single line text)
      - CreatedAt (date/time)   [optional]
    """
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID):
        raise RuntimeError("Airtable env vars missing")

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_DEALS}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }
    now = datetime.utcnow().isoformat()

    fields = {
        "Client": client_name,
        "DealName": deal_name,
        "DriveFolderUrl": drive_folder_url,
        "DriveFolderId": drive_folder_id,
        "Status": "Draft",
        "CreatedAt": now,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json={"fields": fields})
        r.raise_for_status()
        return r.json()


# -------------------------
# Helpers: Google Drive
# -------------------------
def _drive_service():
    if not (GOOGLE_SA_JSON_B64 and GDRIVE_ROOT_FOLDER_ID):
        raise RuntimeError("Google Drive env vars missing")

    clean_b64 = GOOGLE_SA_JSON_B64.strip()
sa_info = json.loads(base64.b64decode(clean_b64).decode("utf-8"))
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_create_folder(name: str, parent_id: str) -> dict:
    service = _drive_service()
    file_metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=file_metadata, fields="id, webViewLink, name").execute()
    return folder


def safe_name(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[^\w\s\-\.\(\)\[\]]+", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s[:80] if len(s) > 80 else s


def build_deal_folders(client_name: str, deal_name: str) -> dict:
    """
    Создаёт структуру:
    BrandcommRoot/
      01_Deals/
        Client/
          2026/
            Deal_YYYY-MM-DD_DealName/
              01_Calc
              02_KP
              03_Contract
              04_TZ
    """
    year = datetime.now().year
    date_str = datetime.now().strftime("%Y-%m-%d")

    client_name = safe_name(client_name or "Client")
    deal_name = safe_name(deal_name or "Deal")

    # 01_Deals
    deals_root = drive_create_folder("01_Deals", GDRIVE_ROOT_FOLDER_ID)

    # Client
    client_folder = drive_create_folder(client_name, deals_root["id"])

    # Year
    year_folder = drive_create_folder(str(year), client_folder["id"])

    # Deal folder
    deal_folder_name = f"Deal_{date_str}_{deal_name}"
    deal_folder = drive_create_folder(deal_folder_name, year_folder["id"])

    # Subfolders
    sub = {}
    for subname in ["01_Calc", "02_KP", "03_Contract", "04_TZ"]:
        sub[subname] = drive_create_folder(subname, deal_folder["id"])

    return {
        "deals_root": deals_root,
        "client_folder": client_folder,
        "year_folder": year_folder,
        "deal_folder": deal_folder,
        "subfolders": sub,
    }


# -------------------------
# Minimal intent parser (phase 1)
# -------------------------
def parse_request(text: str) -> tuple[str, str]:
    """
    Очень простой разбор на старте:
    - если в тексте есть 'кп' -> считаем это сделкой для КП
    - иначе всё равно создаём сделку, потому что тестируем систему папок
    Формат:
      "Клиент: РЖД; Сделка: куртки 300"
    или просто текст — тогда возьмём первые слова.
    """
    t = (text or "").strip()
    client_name = "Client"
    deal_name = "Запрос"

    m1 = re.search(r"клиент\s*[:\-]\s*(.+?)(;|$)", t, re.IGNORECASE)
    if m1:
        client_name = m1.group(1).strip()

    m2 = re.search(r"сделка\s*[:\-]\s*(.+?)(;|$)", t, re.IGNORECASE)
    if m2:
        deal_name = m2.group(1).strip()

    if client_name == "Client":
        # попробуем выцепить первое "для <клиент>"
        m3 = re.search(r"для\s+([A-Za-zА-Яа-я0-9\-\s]{2,40})", t, re.IGNORECASE)
        if m3:
            client_name = m3.group(1).strip()

    if deal_name == "Запрос":
        deal_name = t[:60] if t else "Deal"

    return client_name, deal_name


# -------------------------
# FastAPI
# -------------------------
app = FastAPI()


class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[dict] = None


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    # Optional security: verify Telegram secret token header if you set it
    if TELEGRAM_SECRET_TOKEN:
        got = req.headers.get("x-telegram-bot-api-secret-token", "")
        if got != TELEGRAM_SECRET_TOKEN:
            return {"ok": False, "error": "bad secret token"}

    update = await req.json()
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or ""

    if not chat_id:
        return {"ok": True}

    # commands
    if text.strip().lower() in ["/start", "start"]:
        await tg_send_message(chat_id, "Я Brandcomm Assistant. Напиши: 'Клиент: РЖД; Сделка: куртки 300' — и я создам папки + запись в Airtable.")
        return {"ok": True}

    if text.strip().lower() == "/set_webhook":
        try:
            res = await tg_set_webhook()
            await tg_send_message(chat_id, f"Webhook установлен ✅\n{res}")
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка setWebhook: {e}")
        return {"ok": True}

    # Main flow: create deal + folders + airtable
    try:
        client_name, deal_name = parse_request(text)

        folders = build_deal_folders(client_name, deal_name)
        deal_folder = folders["deal_folder"]
        deal_url = deal_folder.get("webViewLink", "")
        deal_id = deal_folder.get("id", "")

        # Airtable record
        at = await airtable_create_deal(client_name, deal_name, deal_url, deal_id)

        await tg_send_message(
            chat_id,
            "Готово ✅\n"
            f"Клиент: {client_name}\n"
            f"Сделка: {deal_name}\n"
            f"Папка: {deal_url}\n"
            f"Airtable record: {at.get('id')}"
        )
    except Exception as e:
        await tg_send_message(chat_id, f"Ошибка: {e}")

    return {"ok": True}
