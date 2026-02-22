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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()  # optional

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_TABLE_DEALS = os.getenv("AIRTABLE_TABLE_DEALS", "Deals").strip()

# ID корневой папки Brandcomm в Drive (папка, куда ты дал доступ service account)
GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", "").strip()

# JSON сервисного аккаунта (base64) — безопасно хранить как env
GOOGLE_SA_JSON_B64 = os.getenv("GOOGLE_SA_JSON_B64", "").strip()

# Webhook URL (Render даст после деплоя). Нужен чтобы поставить webhook.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()  # например https://brandcomm-ai-bot.onrender.com


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
    payload = {"url": f"{PUBLIC_BASE_URL}/telegram/webhook"}

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
    В таблице Deals желательно поля:
      - Client (text)
      - DealName (text)
      - DriveFolderUrl (url)
      - DriveFolderId (text)
    """
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID):
        raise RuntimeError("Airtable env vars missing")

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_DEALS}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }

    fields = {
        "Client": client_name,
        "DealName": deal_name,
        "DriveFolderUrl": drive_folder_url,
        "DriveFolderId": drive_folder_id,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # Airtable принимает создание одиночной записи через {"fields": {...}}
        r = await client.post(url, headers=headers, json={"fields": fields})
        if r.status_code >= 400:
            raise RuntimeError(f"Airtable error {r.status_code}: {r.text}")
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


def safe_name(s: str) -> str:
    """
    Google Drive запрещает только '/', но мы чистим:
    - '/' -> '_'
    - лишние пробелы
    - слишком длинные имена
    """
    s = (s or "").strip().replace("/", "_")
    s = re.sub(r"\s+", " ", s)
    return s[:120] if len(s) > 120 else s


def drive_find_folder(service, name: str, parent_id: str) -> Optional[str]:
    """
    Ищет папку по имени в конкретном parent_id.
    """
    name = name.replace("'", "\\'")
    q = (
        "mimeType='application/vnd.google-apps.folder' and "
        "trashed=false and "
        f"'{parent_id}' in parents and "
        f"name='{name}'"
    )
    res = service.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def drive_get_or_create_folder(service, name: str, parent_id: str) -> dict:
    """
    Не плодит дубликаты: если папка есть — возвращает её, иначе создаёт.
    """
    name = safe_name(name)
    existing_id = drive_find_folder(service, name, parent_id)
    if existing_id:
        return {"id": existing_id, "name": name, "webViewLink": f"https://drive.google.com/drive/folders/{existing_id}"}

    file_metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=file_metadata, fields="id, webViewLink, name").execute()
    return folder


def build_deal_folders(client_name: str, deal_name: str) -> dict:
    """
    Создаёт структуру (как ты просил):
    BrandcommRoot/
      2026/
        <Клиент>/
          <Сделка>_<YYYY-MM-DD>/
            01_КП_для клиента
            02_Себестоимость
            03_Договор_и_приложение
            04_Счета_и_закрывашки
            05_Макеты_и_векторы
            06_Закрывашки
            07_Честный_знак
            08_Фото_от_клиента
    """
    service = _drive_service()

    year = str(datetime.now().year)                 # "2026"
    date_str = datetime.now().strftime("%Y-%m-%d")  # "2026-02-22"

    client_name = safe_name(client_name or "Client")
    deal_name = safe_name(deal_name or "Deal")

    # / BrandcommRoot / 2026
    year_folder = drive_get_or_create_folder(service, year, GDRIVE_ROOT_FOLDER_ID)

    # / 2026 / <Клиент>
    client_folder = drive_get_or_create_folder(service, client_name, year_folder["id"])

    # / 2026 / <Клиент> / <Сделка>_<дата>
    deal_folder_name = f"{deal_name}_{date_str}"
    deal_folder = drive_get_or_create_folder(service, deal_folder_name, client_folder["id"])

    # сабпапки внутри сделки
    subfolder_names = [
        "01_КП_для клиента",
        "02_Себестоимость",
        "03_Договор_и_приложение",
        "04_Счета_и_закрывашки",
        "05_Макеты_и_векторы",
        "06_Закрывашки",
        "07_Честный_знак",
        "08_Фото_от_клиента",
    ]

    subfolders = {}
    for sf in subfolder_names:
        subfolders[sf] = drive_get_or_create_folder(service, sf, deal_folder["id"])

    return {
        "year_folder": year_folder,
        "client_folder": client_folder,
        "deal_folder": deal_folder,
        "subfolders": subfolders,
    }


# -------------------------
# Minimal intent parser (phase 1)
# -------------------------
def parse_request(text: str) -> tuple[str, str]:
    """
    Формат:
      "Клиент: РЖД; Сделка: куртки 300"
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
        m3 = re.search(r"для\s+([A-Za-zА-Яа-я0-9\-\s]{2,60})", t, re.IGNORECASE)
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
        await tg_send_message(
            chat_id,
            "Я Brandcomm Assistant.\n"
            "Напиши: Клиент: РЖД; Сделка: куртки 300 — и я создам структуру папок + запись в Airtable."
        )
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
