# main.py
import os
import json
import re
from time import sleep
import httpx
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from apscheduler.schedulers.background import BackgroundScheduler
from googleapiclient.errors import HttpError

# ---- Разрешаем http для локальной разработки Google OAuth ----
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# ---- Google OAuth libs ----
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest
import logging

# Настраиваем вывод логов в консоль
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sync")

load_dotenv()

app = FastAPI(title="AmoCRM ↔ Google Sheets")

# ----------------- CONFIG -----------------
# Google
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.json")
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
REDIRECT_URI = "http://localhost:8000/google/oauth2/callback"

SHEET_ID = os.getenv("SHEET_ID")
SHEET_RANGE = os.getenv("SHEET_RANGE")

# AmoCRM
AMO_BASE_URL = os.getenv("AMO_BASE_URL", "").rstrip("/")
AMO_ACCESS_TOKEN = os.getenv("AMO_ACCESS_TOKEN", "")
AMO_PIPELINE_ID = int(os.getenv("AMO_PIPELINE_ID", "8237934"))
AMO_STATUS_ID = int(os.getenv("AMO_STATUS_ID", "67260282"))


# ----------------- VALIDATION -----------------
def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Переменная окружения {name} не задана в .env")
    return v


# fail fast, если что-то не заполнено
require_env("SHEET_ID")
require_env("SHEET_RANGE")
require_env("AMO_BASE_URL")
require_env("AMO_ACCESS_TOKEN")


# ----------------- GOOGLE AUTH -----------------
def flow_from_client() -> Flow:
    if not os.path.exists(CREDENTIALS_FILE):
        raise HTTPException(400, "Нет credentials.json рядом с main.py")
    flow = Flow.from_client_secrets_file(
        CREDENTIALS_FILE, scopes=GOOGLE_SCOPES, redirect_uri=REDIRECT_URI
    )
    return flow


def ensure_credentials() -> Credentials:
    if not os.path.exists(TOKEN_FILE):
        raise HTTPException(
            400, "Нет token.json. Пройди авторизацию: /google/oauth2/start"
        )
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    creds = Credentials.from_authorized_user_info(data, GOOGLE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        else:
            raise HTTPException(
                400, "Токен Google недействителен. Пройди /google/oauth2/start заново."
            )
    return creds


def commit_sheet_changes(
    updates: List[tuple], appends: List[List[Any]], chunk_size: int = 100
):
    """
    updates: список (row_index_zero_based, values[A..F])
    appends: список новых строк для добавления (каждая - [A..F])
    Делает пакетные записи, с ретраями на 429.
    """
    service = get_sheet_service()
    sheet_name = SHEET_RANGE.split("!")[0]

    # ---- UPDATE существующих строк батчами ----
    for i in range(0, len(updates), chunk_size):
        chunk = updates[i : i + chunk_size]
        data = []
        for row_idx, values in chunk:
            start_row = row_idx + 2
            rng = f"{sheet_name}!A{start_row}:F{start_row}"
            data.append({"range": rng, "values": [values]})

        attempt = 0
        while True:
            try:
                service.spreadsheets().values().batchUpdate(
                    spreadsheetId=SHEET_ID,
                    body={"valueInputOption": "RAW", "data": data},
                ).execute()
                break
            except HttpError as e:
                # Ловим лимит и немного ждём
                if "RATE_LIMIT_EXCEEDED" in str(e) and attempt < 5:
                    sleep(2**attempt)  # 1,2,4,8,16 сек
                    attempt += 1
                    continue
                raise

    # ---- APPEND новых строк одним или несколькими заходами ----
    # Google позволяет отправить сразу много строк в одном append
    for i in range(0, len(appends), 500):  # безопасный крупный батч
        values_batch = appends[i : i + 500]
        attempt = 0
        while True:
            try:
                service.spreadsheets().values().append(
                    spreadsheetId=SHEET_ID,
                    range=f"{sheet_name}!A:F",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": values_batch},
                ).execute()
                break
            except HttpError as e:
                if "RATE_LIMIT_EXCEEDED" in str(e) and attempt < 5:
                    sleep(2**attempt)
                    attempt += 1
                    continue
                raise


def get_sheet_service():
    creds = ensure_credentials()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_rows() -> List[List[str]]:
    service = get_sheet_service()
    res = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=SHEET_RANGE)
        .execute()
    )
    return res.get("values", [])


def write_deal_id(row_index_zero_based: int, deal_id: int):
    # столбец E (5-й), строка = индекс+2 (потому что A2 — это нулевая строка в нашем срезе)
    sheet_name = SHEET_RANGE.split("!")[0]
    target_range = f"{sheet_name}!E{row_index_zero_based + 2}"
    service = get_sheet_service()
    body = {"values": [[str(deal_id)]]}
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=target_range, valueInputOption="RAW", body=body
    ).execute()


# ----------------- AMOCRM (JWT) -----------------
def amo_headers() -> Dict[str, str]:
    token = require_env("AMO_ACCESS_TOKEN").strip()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def amo_find_contact(query: str) -> Optional[int]:
    if not query:
        return None
    q = normalize_phone(query) if "@" not in query else query
    url = f"{AMO_BASE_URL}/api/v4/contacts"
    r = amo_request(
        "GET",
        url,
        params={"query": q},
        headers=amo_headers() | {"Accept": "application/json"},
    )
    if r.status_code == 200:
        items = r.json().get("_embedded", {}).get("contacts", [])
        if items:
            return items[0]["id"]
    return None


def amo_create_contact(name: str, phone: str, email: str) -> int:
    url = f"{AMO_BASE_URL}/api/v4/contacts"
    cfv = []
    nphone = normalize_phone(phone)
    if nphone:
        cfv.append({"field_code": "PHONE", "values": [{"value": nphone}]})
    if email:
        cfv.append({"field_code": "EMAIL", "values": [{"value": email}]})
    data = [{"name": name or "", "custom_fields_values": cfv}]
    r = amo_request(
        "POST", url, json=data, headers=amo_headers() | {"Accept": "application/json"}
    )
    r.raise_for_status()
    return r.json()["_embedded"]["contacts"][0]["id"]


def amo_create_lead(price: int, contact_id: int) -> int:
    url = f"{AMO_BASE_URL}/api/v4/leads"
    data = [
        {
            "price": (
                int(price)
                if isinstance(price, (int, str)) and str(price).isdigit()
                else 0
            ),
            "status_id": AMO_STATUS_ID,
            "pipeline_id": AMO_PIPELINE_ID,
            "_embedded": {"contacts": [{"id": contact_id}]},
        }
    ]
    r = httpx.post(url, json=data, headers=amo_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["_embedded"]["leads"][0]["id"]


def get_status_map(pipeline_id: int) -> Dict[int, str]:
    url = f"{AMO_BASE_URL}/api/v4/leads/pipelines/{pipeline_id}/statuses"
    r = amo_request("GET", url, headers=amo_headers() | {"Accept": "application/json"})
    r.raise_for_status()
    items = r.json().get("_embedded", {}).get("statuses", [])
    return {it["id"]: it["name"] for it in items}


def fetch_leads_with_contacts(pipeline_id: int) -> List[dict]:
    """Возвращает список сделок из воронки с привязанными контактами (батчами)."""
    leads: List[dict] = []
    page = 1
    while True:
        url = f"{AMO_BASE_URL}/api/v4/leads"
        r = amo_request(
            "GET",
            url,
            params={
                "filter[pipeline_id]": pipeline_id,
                "with": "contacts",
                "page": page,
                "limit": 50,
            },
            headers=amo_headers() | {"Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("_embedded", {}).get("leads", [])
        if not batch:
            break
        leads.extend(batch)
        # пагинация по _links
        next_link = (data.get("_links") or {}).get("next", {}).get("href")
        if not next_link:
            break
        page += 1
    return leads


def fetch_contacts_by_ids(ids: List[int]) -> Dict[int, dict]:
    """Возвращает карту contact_id -> {name, phone, email}"""
    out: Dict[int, dict] = {}
    if not ids:
        return out
    # батчим по 50
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        url = f"{AMO_BASE_URL}/api/v4/contacts"
        r = amo_request(
            "GET",
            url,
            params=[("ids[]", cid) for cid in chunk],
            headers=amo_headers() | {"Accept": "application/json"},
        )
        r.raise_for_status()
        for c in r.json().get("_embedded", {}).get("contacts", []):
            name = c.get("name") or ""
            phone = ""
            email = ""
            for cf in c.get("custom_fields_values") or []:
                if cf.get("field_code") == "PHONE":
                    vals = cf.get("values") or []
                    if vals:
                        phone = normalize_phone(vals[0].get("value") or "")
                if cf.get("field_code") == "EMAIL":
                    vals = cf.get("values") or []
                    if vals:
                        email = vals[0].get("value") or ""
            out[c["id"]] = {"name": name, "phone": phone, "email": email}
    return out


def read_sheet_all() -> List[List[str]]:
    service = get_sheet_service()
    res = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=SHEET_RANGE)
        .execute()
    )
    return res.get("values", [])


def set_row_values(row_index_zero_based: int, values: List[Any]):
    """Полностью перезаписывает A..F (6 колонок) для указанной строки (от A2)."""
    sheet_name = SHEET_RANGE.split("!")[0]
    start_row = row_index_zero_based + 2
    target_range = f"{sheet_name}!A{start_row}:F{start_row}"
    service = get_sheet_service()
    body = {"values": [values]}
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=target_range, valueInputOption="RAW", body=body
    ).execute()


def append_row(values: List[Any]):
    sheet_name = SHEET_RANGE.split("!")[0]
    target_range = f"{sheet_name}!A:F"
    service = get_sheet_service()
    body = {"values": [values]}
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=target_range,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()


def sync_from_amocrm() -> dict:
    """Amo → Sheets: подтягиваем сделки из воронки, пакетно пишем в Google Sheets."""
    # индекс существующих строк по lead_id (E)
    rows = read_sheet_all()
    lead_to_rowidx: Dict[str, int] = {}
    for i, row in enumerate(rows):
        if len(row) > 4 and row[4]:
            lead_to_rowidx[str(row[4]).strip()] = i

    status_map = get_status_map(AMO_PIPELINE_ID)
    leads = fetch_leads_with_contacts(AMO_PIPELINE_ID)

    # карта lead_id -> contact_id (первый привязанный), собираем контакты
    contact_ids: List[int] = []
    lead_contact: Dict[int, Optional[int]] = {}
    for L in leads:
        cid = None
        emb = L.get("_embedded") or {}
        cs = emb.get("contacts") or []
        if cs:
            cid = cs[0].get("id")
        lead_contact[L["id"]] = cid
        if cid:
            contact_ids.append(cid)

    contacts_map = fetch_contacts_by_ids(contact_ids)

    updates: List[tuple] = []  # (row_index_zero_based, [A..F])
    appends: List[List[Any]] = []  # [A..F]
    updated, inserted = 0, 0

    for L in leads:
        lead_id = str(L["id"])
        status_id = L.get("status_id")
        status_name = status_map.get(status_id, str(status_id))
        price = L.get("price") or 0

        c = contacts_map.get(lead_contact[L["id"]], {})
        name = c.get("name", "")
        phone = c.get("phone", "")
        email = c.get("email", "")

        row_values = [name, phone, email, price, lead_id, status_name]

        if lead_id in lead_to_rowidx:
            updates.append((lead_to_rowidx[lead_id], row_values))
            updated += 1
        else:
            appends.append(row_values)
            inserted += 1

    # один пакетный коммит
    commit_sheet_changes(updates, appends)

    return {"updated": updated, "inserted": inserted, "fetched": len(leads)}


# ----------------- SYNC LOGIC -----------------


def normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    d = re.sub(r"\D+", "", raw)  # только цифры
    if d.startswith("8") and len(d) == 11:
        d = "7" + d[1:]  # 8 → 7 для РФ
    if len(d) == 10:
        d = "7" + d  # без кода страны → 7
    return d


def amo_request(method: str, url: str, **kwargs) -> httpx.Response:
    # ретраи на 429/5xx с экспоненциальной паузой
    for attempt in range(5):
        try:
            r = httpx.request(method, url, timeout=30, **kwargs)
        except httpx.HTTPError as e:
            if attempt == 4:
                raise
            sleep(2**attempt / 2)
            continue

        if r.status_code in (429,) or 500 <= r.status_code < 600:
            if attempt == 4:
                r.raise_for_status()
            sleep(2**attempt / 2)
            continue
        return r
    return r  # не должно сюда дойти


def parse_row(row: List[str]) -> Dict[str, Any]:
    name = row[0].strip() if len(row) > 0 else ""
    phone = row[1].strip() if len(row) > 1 else ""
    email = row[2].strip() if len(row) > 2 else ""
    budget = int(row[3]) if len(row) > 3 and str(row[3]).strip().isdigit() else 0
    deal_id = row[4].strip() if len(row) > 4 else ""
    return {
        "name": name,
        "phone": phone,
        "email": email,
        "budget": budget,
        "deal_id": deal_id,
    }


def process_new_rows() -> Dict[str, Any]:
    rows = read_rows()
    created: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        data = parse_row(row)
        if data["deal_id"]:
            continue  # уже обработано
        # ищем контакт по email, затем по телефону
        contact_id = None
        for q in [data["email"], data["phone"]]:
            cid = amo_find_contact(q) if q else None
            if cid:
                contact_id = cid
                break
        if not contact_id:
            contact_id = amo_create_contact(data["name"], data["phone"], data["email"])
        lead_id = amo_create_lead(data["budget"], contact_id)
        write_deal_id(idx, lead_id)
        created.append({"row": idx + 2, "lead_id": lead_id, "contact_id": contact_id})
    return {"created": created, "checked_rows": len(rows)}


# ----------------- ROUTES -----------------
@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK. Google OAuth: /google/oauth2/start  |  Синк: /sync/once"


@app.get("/google/oauth2/start")
def google_oauth_start():
    flow = flow_from_client()
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    app.state.google_oauth_states = getattr(app.state, "google_oauth_states", {})
    app.state.google_oauth_states[state] = True
    return RedirectResponse(auth_url)


@app.get("/google/oauth2/callback")
def google_oauth_callback(request: Request, state: Optional[str] = None):
    states = getattr(app.state, "google_oauth_states", {})
    if not state or state not in states:
        raise HTTPException(400, "Некорректный state в OAuth колбэке")
    flow = flow_from_client()
    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return RedirectResponse(url="/google/sheets/read")


@app.get("/google/sheets/read")
def google_sheets_read():
    values = read_rows()
    return JSONResponse({"rows_preview": values[:10], "count": len(values)})


@app.get("/sync/once")
def sync_once():
    try:
        if not AMO_ACCESS_TOKEN:
            raise HTTPException(400, "AMO_ACCESS_TOKEN не задан в .env")
        return JSONResponse(process_new_rows())
    except Exception as e:
        raise HTTPException(400, f"Sync error: {e}")


@app.get("/sync/pull_amocrm")
def sync_pull_amocrm():
    try:
        return JSONResponse(sync_from_amocrm())
    except Exception as e:
        raise HTTPException(400, f"Pull error: {e}")


# ----------------- AUTOSYNC (каждые 2 минуты) -----------------
scheduler = BackgroundScheduler()


@scheduler.scheduled_job("interval", minutes=2, coalesce=True, max_instances=1)
def scheduled_sync():
    try:
        logger.info("🟢 PUSH: Проверяем Google Sheets → AmoCRM ...")
        result = process_new_rows()
        logger.info(f"✅ PUSH завершён: {result}")
    except Exception as e:
        logger.error(f"❌ PUSH ошибка: {e}")


@scheduler.scheduled_job("interval", minutes=5, coalesce=True, max_instances=1)
def scheduled_pull():
    try:
        logger.info("🔵 PULL: Проверяем AmoCRM → Google Sheets ...")
        result = sync_from_amocrm()
        logger.info(f"✅ PULL завершён: {result}")
    except Exception as e:
        logger.error(f"❌ PULL ошибка: {e}")


@app.on_event("startup")
def on_startup():
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown(wait=False)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
