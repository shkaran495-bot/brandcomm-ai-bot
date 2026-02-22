import os
import json
import base64
import re
from datetime import datetime
from typing import Optional
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# -------------------------
# ENV
# -------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "").strip()  # optional

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_TABLE_DEALS = os.getenv("AIRTABLE_TABLE_DEALS", "Deals").strip()

# Корневая папка/диск в Drive:
# - если Shared Drive: сюда лучше положить ID папки внутри Shared Drive (или ID самого Shared Drive — но проще папку)
# - если OAuth: может быть папка в My Drive
GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", "").strip()

# JSON сервисного аккаунта (base64) — безопасно хранить как env
GOOGLE_SA_JSON_B64 = os.getenv("GOOGLE_SA_JSON_B64", "").strip()

# Webhook URL (Render даст после деплоя). Нужен чтобы поставить webhook.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()  # например https://brandcomm-ai-bot.onrender.com

# Если используешь Shared Drive — можно (не обязательно) указать его ID.
# Тогда поиск/создание будет стабильнее. Если не знаешь — оставь пустым.
GDRIVE_SHARED_DRIVE_ID = os.getenv("GDRIVE_SHARED_DRIVE_ID", "").strip()

# -------------------------
# In-memory context (просто на старте)
# chat_id -> {"client":..., "deal":..., "deal_id":..., "deal_url":..., "subfolders": {name: id}}
# -------------------------
CHAT_CONTEXT: dict[int, dict] = {}

YEAR_FIXED = "2026"

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

    if TELEGRAM_SECRET_TOKEN:
        payload["secret_token"] = TELEGRAM_SECRET_TOKEN

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


async def tg_get_file_path(file_id: str) -> str:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params={"file_id": file_id})
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getFile not ok: {data}")
        return data["result"]["file_path"]


async def tg_download_to_tmp(file_path: str) -> Path:
    dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    tmp_path = Path("/tmp") / Path(file_path).name
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.get(dl_url)
        r.raise_for_status()
        tmp_path.write_bytes(r.content)
    return tmp_path


# -------------------------
# Helpers: Airtable
# -------------------------
async def airtable_create_deal(client_name: str, deal_name: str, drive_folder_url: str, drive_folder_id: str):
    if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID):
        raise RuntimeError("Airtable env vars missing")

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_DEALS}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

    fields = {
        "Client": client_name,
        "DealName": deal_name,
        "DriveFolderUrl": drive_folder_url,
        "DriveFolderId": drive_folder_id,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json={"fields": fields})
        if r.status_code >= 400:
            raise RuntimeError(f"Airtable error {r.status_code}: {r.text}")
        return r.json()


# -------------------------
# Helpers: Google Drive (Shared Drive friendly)
# -------------------------
def _drive_service():
    if not (GOOGLE_SA_JSON_B64 and GDRIVE_ROOT_FOLDER_ID):
        raise RuntimeError("Google Drive env vars missing (GOOGLE_SA_JSON_B64 / GDRIVE_ROOT_FOLDER_ID)")

    clean = GOOGLE_SA_JSON_B64.strip()

    # если вдруг вставили JSON напрямую
    if clean.startswith("{") and clean.endswith("}"):
        sa_info = json.loads(clean)
    else:
        clean = re.sub(r"\s+", "", clean)
        pad = (-len(clean)) % 4
        if pad:
            clean += "=" * pad
        sa_info = json.loads(base64.b64decode(clean).decode("utf-8"))

    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def safe_name(s: str) -> str:
    s = (s or "").strip().replace("/", "_")
    s = re.sub(r"\s+", " ", s)
    return s[:120] if len(s) > 120 else s


def _list_kwargs():
    # Shared Drive support
    kw = {
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "corpora": "allDrives" if not GDRIVE_SHARED_DRIVE_ID else "drive",
    }
    if GDRIVE_SHARED_DRIVE_ID:
        kw["driveId"] = GDRIVE_SHARED_DRIVE_ID
    return kw


def drive_find_folder(service, name: str, parent_id: str) -> Optional[str]:
    name = safe_name(name).replace("'", "\\'")
    q = (
        "mimeType='application/vnd.google-apps.folder' and trashed=false and "
        f"'{parent_id}' in parents and name='{name}'"
    )

    res = service.files().list(
        q=q,
        fields="files(id,name)",
        pageSize=1,
        **_list_kwargs(),
    ).execute()

    files = res.get("files", [])
    return files[0]["id"] if files else None


def drive_get_or_create_folder(service, name: str, parent_id: str) -> dict:
    name = safe_name(name)
    existing_id = drive_find_folder(service, name, parent_id)
    if existing_id:
        return {
            "id": existing_id,
            "name": name,
            "webViewLink": f"https://drive.google.com/drive/folders/{existing_id}",
        }

    file_metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    folder = service.files().create(
        body=file_metadata,
        fields="id, webViewLink, name",
        supportsAllDrives=True,
    ).execute()
    return folder


def drive_upload_file(service, local_path: Path, filename: str, parent_folder_id: str) -> dict:
    filename = safe_name(filename) or local_path.name
    file_metadata = {"name": filename, "parents": [parent_folder_id]}
    media = MediaFileUpload(str(local_path), resumable=False)

    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return uploaded


def build_deal_folders(client_name: str, deal_name: str) -> dict:
    """
    BrandcommRoot/<2026>/<Клиент>/<Сделка>_<YYYY-MM-DD>/
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

    date_str = datetime.now().strftime("%Y-%m-%d")
    client_name = safe_name(client_name or "Client")
    deal_name = safe_name(deal_name or "Deal")

    year_folder = drive_get_or_create_folder(service, YEAR_FIXED, GDRIVE_ROOT_FOLDER_ID)
    client_folder = drive_get_or_create_folder(service, client_name, year_folder["id"])
    deal_folder_name = f"{deal_name}_{date_str}"
    deal_folder = drive_get_or_create_folder(service, deal_folder_name, client_folder["id"])

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
# Routing rules: tags + smart guess
# -------------------------
TAG_TO_FOLDER = {
    "#kp": "01_КП_для клиента",
    "#cost": "02_Себестоимость",
    "#contract": "03_Договор_и_приложение",
    "#invoice": "04_Счета_и_закрывашки",
    "#design": "05_Макеты_и_векторы",
    "#close": "06_Закрывашки",
    "#cz": "07_Честный_знак",
    "#photo": "08_Фото_от_клиента",
}

KEYWORDS_TO_FOLDER = [
    (["кп", "kp", "proposal", "коммерческое"], "01_КП_для клиента"),
    (["себес", "cost", "смета", "расчет", "калькуляц"], "02_Себестоимость"),
    (["договор", "contract", "agreement"], "03_Договор_и_приложение"),
    (["счет", "invoice", "акт", "упд", "накладн"], "04_Счета_и_закрывашки"),
    (["макет", "design", "ai", "pdf", "cdr", "eps", "svg"], "05_Макеты_и_векторы"),
    (["закрываш", "closing"], "06_Закрывашки"),
    (["чз", "честн", "cz", "mark", "маркиров"], "07_Честный_знак"),
    (["фото", "photo", "jpg", "jpeg", "png", "heic"], "08_Фото_от_клиента"),
]


def pick_subfolder_name(caption_or_text: str, filename: str = "", mime_type: str = "") -> str:
    t = (caption_or_text or "").lower()

    # 1) explicit tags
    for tag, folder in TAG_TO_FOLDER.items():
        if tag in t:
            return folder

    # 2) by filename / mime
    blob = " ".join([t, (filename or "").lower(), (mime_type or "").lower()])

    for keys, folder in KEYWORDS_TO_FOLDER:
        if any(k in blob for k in keys):
            return folder

    # 3) defaults:
    if mime_type.startswith("image/"):
        return "08_Фото_от_клиента"
    return "05_Макеты_и_векторы"


# -------------------------
# Intent parser
# -------------------------
def parse_request(text: str) -> tuple[str, str]:
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
    return {"ok": True, "year": YEAR_FIXED}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    # Security header (optional)
    if TELEGRAM_SECRET_TOKEN:
        got = req.headers.get("x-telegram-bot-api-secret-token", "")
        if got != TELEGRAM_SECRET_TOKEN:
            return {"ok": False, "error": "bad secret token"}

    update = await req.json()
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}

    text = (msg.get("text") or "").strip()
    caption = (msg.get("caption") or "").strip()

    # -------------------------
    # Commands
    # -------------------------
    if text.lower() in ["/start", "start", "/help", "help"]:
        await tg_send_message(
            chat_id,
            "Я Brandcomm Assistant.\n\n"
            "1) Создай сделку:\n"
            "   Клиент: РЖД; Сделка: куртки 300\n\n"
            "2) Потом кидай файлы — я разложу по папкам.\n"
            "   Можно с тегами: #kp #contract #invoice #design #photo #cost #cz\n"
            "   Можно БЕЗ тегов — я попробую угадать по имени/типу.\n\n"
            "Команды:\n"
            "/where — покажу текущую сделку\n"
            "/set_webhook — установить webhook\n"
        )
        return {"ok": True}

    if text.lower() == "/set_webhook":
        try:
            res = await tg_set_webhook()
            await tg_send_message(chat_id, f"Webhook установлен ✅\n{res}")
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка setWebhook: {e}")
        return {"ok": True}

    if text.lower() == "/where":
        ctx = CHAT_CONTEXT.get(chat_id)
        if not ctx:
            await tg_send_message(chat_id, "Контекст сделки не выбран. Отправь: Клиент: ...; Сделка: ...")
        else:
            await tg_send_message(
                chat_id,
                "Текущая сделка ✅\n"
                f"Год: {YEAR_FIXED}\n"
                f"Клиент: {ctx.get('client')}\n"
                f"Сделка: {ctx.get('deal')}\n"
                f"Папка: {ctx.get('deal_url')}"
            )
        return {"ok": True}

    # -------------------------
    # Create deal by text
    # -------------------------
    if "клиент" in text.lower() and "сделка" in text.lower():
        try:
            client_name, deal_name = parse_request(text)
            folders = build_deal_folders(client_name, deal_name)

            deal_folder = folders["deal_folder"]
            deal_url = deal_folder.get("webViewLink", "")
            deal_id = deal_folder.get("id", "")

            at = await airtable_create_deal(client_name, deal_name, deal_url, deal_id)

            CHAT_CONTEXT[chat_id] = {
                "client": client_name,
                "deal": deal_name,
                "deal_id": deal_id,
                "deal_url": deal_url,
                "subfolders": {k: v.get("id") for k, v in folders["subfolders"].items()},
            }

            await tg_send_message(
                chat_id,
                "Сделка создана ✅\n"
                f"Год: {YEAR_FIXED}\n"
                f"Клиент: {client_name}\n"
                f"Сделка: {deal_name}\n"
                f"Папка: {deal_url}\n"
                f"Airtable record: {at.get('id')}\n\n"
                "Теперь отправляй файлы — я разложу по папкам."
            )
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка: {e}")
        return {"ok": True}

    # -------------------------
    # Files handling
    # -------------------------
    has_document = msg.get("document") is not None
    has_photo = msg.get("photo") is not None
    has_video = msg.get("video") is not None
    has_audio = msg.get("audio") is not None
    has_voice = msg.get("voice") is not None

    if has_document or has_photo or has_video or has_audio or has_voice:
        ctx = CHAT_CONTEXT.get(chat_id)
        if not ctx:
            await tg_send_message(chat_id, "Сначала выбери сделку: Клиент: ...; Сделка: ... (иначе не знаю куда класть файлы)")
            return {"ok": True}

        try:
            service = _drive_service()
            sub_map = ctx.get("subfolders", {})
            deal_id = ctx["deal_id"]

            # ---- document
            if has_document:
                doc = msg["document"]
                file_id = doc["file_id"]
                filename = doc.get("file_name") or "file"
                mime = doc.get("mime_type") or ""
                target_name = pick_subfolder_name(caption, filename=filename, mime_type=mime)
                target_folder_id = sub_map.get(target_name) or deal_id

                file_path = await tg_get_file_path(file_id)
                local_path = await tg_download_to_tmp(file_path)
                uploaded = drive_upload_file(service, local_path, filename, target_folder_id)

                await tg_send_message(
                    chat_id,
                    "Файл загружен ✅\n"
                    f"Куда: {target_name}\n"
                    f"Drive: {uploaded.get('webViewLink')}"
                )
                return {"ok": True}

            # ---- photo (biggest)
            if has_photo:
                photos = msg["photo"]
                biggest = photos[-1]
                file_id = biggest["file_id"]
                filename = "photo.jpg"
                mime = "image/jpeg"
                target_name = pick_subfolder_name(caption, filename=filename, mime_type=mime)
                target_folder_id = sub_map.get(target_name) or deal_id

                file_path = await tg_get_file_path(file_id)
                local_path = await tg_download_to_tmp(file_path)
                uploaded = drive_upload_file(service, local_path, local_path.name, target_folder_id)

                await tg_send_message(
                    chat_id,
                    "Фото загружено ✅\n"
                    f"Куда: {target_name}\n"
                    f"Drive: {uploaded.get('webViewLink')}"
                )
                return {"ok": True}

            # ---- video
            if has_video:
                vid = msg["video"]
                file_id = vid["file_id"]
                filename = vid.get("file_name") or "video.mp4"
                mime = vid.get("mime_type") or "video/mp4"
                target_name = pick_subfolder_name(caption, filename=filename, mime_type=mime)
                target_folder_id = sub_map.get(target_name) or deal_id

                file_path = await tg_get_file_path(file_id)
                local_path = await tg_download_to_tmp(file_path)
                uploaded = drive_upload_file(service, local_path, local_path.name, target_folder_id)

                await tg_send_message(
                    chat_id,
                    "Видео загружено ✅\n"
                    f"Куда: {target_name}\n"
                    f"Drive: {uploaded.get('webViewLink')}"
                )
                return {"ok": True}

            # ---- audio
            if has_audio:
                aud = msg["audio"]
                file_id = aud["file_id"]
                filename = aud.get("file_name") or "audio.mp3"
                mime = aud.get("mime_type") or "audio/mpeg"
                target_name = pick_subfolder_name(caption, filename=filename, mime_type=mime)
                target_folder_id = sub_map.get(target_name) or deal_id

                file_path = await tg_get_file_path(file_id)
                local_path = await tg_download_to_tmp(file_path)
                uploaded = drive_upload_file(service, local_path, filename, target_folder_id)

                await tg_send_message(
                    chat_id,
                    "Аудио загружено ✅\n"
                    f"Куда: {target_name}\n"
                    f"Drive: {uploaded.get('webViewLink')}"
                )
                return {"ok": True}

            # ---- voice
            if has_voice:
                v = msg["voice"]
                file_id = v["file_id"]
                filename = "voice.ogg"
                mime = v.get("mime_type") or "audio/ogg"
                target_name = pick_subfolder_name(caption, filename=filename, mime_type=mime)
                target_folder_id = sub_map.get(target_name) or deal_id

                file_path = await tg_get_file_path(file_id)
                local_path = await tg_download_to_tmp(file_path)
                uploaded = drive_upload_file(service, local_path, filename, target_folder_id)

                await tg_send_message(
                    chat_id,
                    "Voice загружен ✅\n"
                    f"Куда: {target_name}\n"
                    f"Drive: {uploaded.get('webViewLink')}"
                )
                return {"ok": True}

        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка загрузки файла: {e}")
            return {"ok": True}

    # -------------------------
    # Fallback
    # -------------------------
    await tg_send_message(
        chat_id,
        "Не вижу команды.\n"
        "Создай сделку: Клиент: ...; Сделка: ...\n"
        "или отправь файл (можно с тегом #kp #contract #invoice #design #photo …)."
    )
    return {"ok": True}
