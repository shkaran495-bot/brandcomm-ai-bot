# ==============================
# BRANDCOMM AI BOT (Drive + Upload + GPT)
# ==============================

import os
import re
import io
import json
from pathlib import Path
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
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
TOKEN_PATH = Path("/etc/secrets/token.json")

# подпапки сделки (8 штук) — названия можешь поменять как тебе надо
SUBFOLDERS = [
    "01_КП_для_клиента",
    "02_Сметы_и_расчеты",
    "03_Договоры_и_счета",
    "04_ТЗ_и_брифы",
    "05_Макеты_и_векторы",
    "06_Производство_и_логистика",
    "07_Оплаты_и_закрывашки",
    "08_Фото_и_материалы",
]

# быстрые алиасы для команды /to
TO_MAP = {
    "01": "01_КП_для_клиента",
    "02": "02_Сметы_и_расчеты",
    "03": "03_Договоры_и_счета",
    "04": "04_ТЗ_и_брифы",
    "05": "05_Макеты_и_векторы",
    "06": "06_Производство_и_логистика",
    "07": "07_Оплаты_и_закрывашки",
    "08": "08_Фото_и_материалы",
}

app = FastAPI()

# chat_id -> ctx
# ctx = {"deal_id": "...", "deal_link": "...", "subfolder_ids": {name:id}, "target": "08_Фото_и_материалы"}
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
    raise RuntimeError("OAuth not completed: token.json not found in /etc/secrets and TOKEN_JSON is empty.")

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
    authorization_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return RedirectResponse(authorization_url)

@app.get("/oauth2callback")
async def oauth2callback(request: Request):
    try:
        flow = Flow.from_client_config(_google_client_config(), scopes=SCOPES)
        flow.redirect_uri = GOOGLE_REDIRECT_URI
        flow.fetch_token(authorization_response=str(request.url))
        creds = flow.credentials
        return {"token_json": json.loads(creds.to_json())}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": "oauth2callback_failed", "details": str(e)})

# =========================
# DRIVE HELPERS
# =========================

def drive_get_or_create_folder(service, name: str, parent_id: str):
    q = (
        "mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false and '{parent_id}' in parents and name='{name}'"
    )
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

def drive_upload_bytes(service, parent_id: str, filename: str, data: bytes):
    media = MediaIoBaseUpload(io.BytesIO(data), resumable=False)
    f = service.files().create(
        body={"name": filename, "parents": [parent_id]},
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()
    return f["webViewLink"]

# =========================
# TELEGRAM HELPERS
# =========================

async def tg_send(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})

async def tg_get_file_path(file_id: str) -> str:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params={"file_id": file_id})
        r.raise_for_status()
        return r.json()["result"]["file_path"]

async def tg_download(file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content

# =========================
# GPT (optional)
# =========================

async def ask_gpt(user_text: str) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в Render. Добавь переменную OPENAI_API_KEY и сделай deploy."

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {"model": "gpt-5.2", "input": user_text}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if r.status_code >= 400:
            return f"Ошибка OpenAI: {r.status_code} {r.text}"
        data = r.json()
        return data.get("output_text", "").strip() or "Пустой ответ модели."

# =========================
# ROUTES
# =========================

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/")
def root():
    return {"status": "Bot is running"}

# =========================
# WEBHOOK
# =========================

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message", {}) or update.get("edited_message", {})
    if not message:
        return {"ok": True}

    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return {"ok": True}

    text = message.get("text") or ""

    # 1) /start
    if text.strip() == "/start":
        await tg_send(
            chat_id,
            "1) Создай сделку:\n"
            "Клиент: РЖД; Сделка: куртки 300\n\n"
            "2) Выбери подпапку (опционально):\n"
            "/to 05  (макеты)\n"
            "/to 03  (договоры)\n"
            "/to 01  (КП)\n\n"
            "3) Отправь файл (фото/документ) — я загружу в Drive.\n"
            "4) Пиши обычным текстом — отвечу как GPT (если включён ключ)."
        )
        return {"ok": True}

    # 2) /where
    if text.strip() == "/where":
        ctx = CHAT_CTX.get(chat_id)
        if not ctx:
            await tg_send(chat_id, "Активной сделки нет. Сначала создай: Клиент: ...; Сделка: ...")
            return {"ok": True}
        await tg_send(
            chat_id,
            f"Активная сделка:\n{ctx.get('deal_link','')}\n"
            f"Текущая подпапка: {ctx.get('target','08_Фото_и_материалы')}"
        )
        return {"ok": True}

    # 3) /to XX
    m_to = re.match(r"^/to\s+(\d{2})\s*$", text.strip())
    if m_to:
        code = m_to.group(1)
        ctx = CHAT_CTX.get(chat_id)
        if not ctx:
            await tg_send(chat_id, "Сначала создай сделку: Клиент: ...; Сделка: ...")
            return {"ok": True}
        folder_name = TO_MAP.get(code)
        if not folder_name:
            await tg_send(chat_id, "Не понял код. Используй /to 01..08")
            return {"ok": True}
        ctx["target"] = folder_name
        await tg_send(chat_id, f"Ок. Следующие файлы загружу в: {folder_name}")
        return {"ok": True}

    # 4) Создание сделки (и 8 подпапок)
    if text and text.startswith("Клиент:"):
        try:
            m_client = re.search(r"Клиент:\s*(.+?);", text)
            m_deal = re.search(r"Сделка:\s*(.+)$", text)
            if not m_client or not m_deal:
                await tg_send(chat_id, "Формат такой: Клиент: РЖД; Сделка: куртки 300")
                return {"ok": True}

            client_name = m_client.group(1).strip()
            deal_name = m_deal.group(1).strip()

            service = _drive_service()

            year_name = str(datetime.utcnow().year)  # 2026 и далее автоматом
            year_folder = drive_get_or_create_folder(service, year_name, GDRIVE_ROOT_FOLDER_ID)
            client_folder = drive_get_or_create_folder(service, client_name, year_folder["id"])
            deal_folder = drive_get_or_create_folder(service, deal_name, client_folder["id"])

            # создаём 8 подпапок
            sub_ids = {}
            for sf in SUBFOLDERS:
                f = drive_get_or_create_folder(service, sf, deal_folder["id"])
                sub_ids[sf] = f["id"]

            CHAT_CTX[chat_id] = {
                "deal_id": deal_folder["id"],
                "deal_link": deal_folder.get("webViewLink", ""),
                "subfolder_ids": sub_ids,
                "target": "08_Фото_и_материалы",
            }

            await tg_send(chat_id, f"Сделка создана ✅\n{deal_folder.get('webViewLink','')}\nПодпапки: 8 шт.")
            return {"ok": True}

        except Exception as e:
            await tg_send(chat_id, f"Ошибка создания сделки: {e}")
            return {"ok": True}

    # 5) Загрузка файлов (document/photo)
    if ("document" in message) or ("photo" in message):
        ctx = CHAT_CTX.get(chat_id)
        if not ctx:
            await tg_send(chat_id, "Сначала создай сделку: Клиент: ...; Сделка: ...")
            return {"ok": True}

        try:
            service = _drive_service()

            # определяем файл
            if "document" in message:
                file_id = message["document"]["file_id"]
                filename = message["document"].get("file_name", "file")
            else:
                # берём самое большое фото
                file_id = message["photo"][-1]["file_id"]
                filename = "photo.jpg"

            # куда грузим
            target_name = ctx.get("target", "08_Фото_и_материалы")
            target_id = ctx.get("subfolder_ids", {}).get(target_name, ctx["deal_id"])

            file_path = await tg_get_file_path(file_id)
            data = await tg_download(file_path)

            link = drive_upload_bytes(service, target_id, filename, data)
            await tg_send(chat_id, f"Файл загружен ✅\n{target_name}\n{link}")
            return {"ok": True}

        except Exception as e:
            await tg_send(chat_id, f"Ошибка загрузки файла: {e}")
            return {"ok": True}

    # 6) GPT чат (если это обычный текст)
    if text.strip():
        reply = await ask_gpt(text.strip())
        await tg_send(chat_id, reply)
        return {"ok": True}

    return {"ok": True}
