import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
import traceback

from odoo_client import cfg, odoo_call
import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("stavario_sync")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# class Config:
#     base_url:                   str = os.environ["STAVARIO_BASE_URL"].rstrip("/")
#     username:                   str = os.environ["STAVARIO_USERNAME"]
#     password:                   str = os.environ["STAVARIO_PASSWORD"]
#     login_path:                 str = os.environ["STAVARIO_LOGIN_PATH"]
#     list_path:                  str = os.environ["STAVARIO_LIST_PATH"]
#     detail_path:                str = os.environ["STAVARIO_DETAIL_PATH"]
#     building_detail_path:       str = os.environ["STAVARIO_BUILDING_DETAIL_PATH"]
#     odoo_url:                   str = os.environ["ODOO_URL"].rstrip("/")
#     odoo_db:                    str = os.environ["ODOO_DB"]
#     odoo_api_key:               str = os.environ["ODOO_API_KEY"]
#     page_size:                  int = int(os.environ.get("PAGE_SIZE", 100))
#     max_concurrent:             int = int(os.environ.get("MAX_CONCURRENT", 10))



TEMPLATE_IDS = {
    "outside_deviation": 72,
    "missing_checkout": 73,
    "conflict_detected": 74,
}
 
# Fallback template for attendance-level issues with no dedicated template
RECORD_CATCHALL_TEMPLATE_ID = 75









# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class AuthManager:
    """Fetches and caches the Stavario bearer token; re-fetches on 401."""

    def __init__(self):
        self._token: str | None = None

    async def token(self, session: aiohttp.ClientSession) -> str:
        if self._token is None:
            await self._fetch(session)
        return self._token

    async def invalidate(self, session: aiohttp.ClientSession) -> str:
        log.info("Token invalidated - re-authenticating...")
        self._token = None
        return await self.token(session)

    async def _fetch(self, session: aiohttp.ClientSession) -> None:
        url = cfg.base_url + cfg.login_path
        payload = {"application": "globalecoexp","adminUsername": cfg.username, "password": cfg.password}
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        # Stavario may return the token under different keys depending on version.
        # Adjust the key name here if needed (e.g. "access_token", "token", "jwt").
        self._token = (
            data.get("token")
            or data.get("access_token")
            or data.get("jwt")
        )
        if not self._token:
            raise RuntimeError(f"Could not find token in login response: {data}")
        log.info("Auth token acquired.")


auth = AuthManager()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _auth_headers(session: aiohttp.ClientSession) -> dict:
    return {"Authorization": f"Bearer {await auth.token(session)}"}


async def post_json(session: aiohttp.ClientSession, url: str, body: dict) -> dict:
    """POST JSON body with bearer auth; retries once on 401."""
    for attempt in range(2):
        headers = await _auth_headers(session)
        async with session.post(url, json=body, headers=headers) as resp:
            if resp.status == 401 and attempt == 0:
                await auth.invalidate(session)
                continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError("Authentication failed after retry.")


async def get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None) -> dict:
    """GET with bearer auth; retries once on 401."""
    for attempt in range(2):
        headers = await _auth_headers(session)
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 401 and attempt == 0:
                await auth.invalidate(session)
                continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError("Authentication failed after retry.")




# ---------------------------------------------------------------------------
# Notofies admin of various issues of the sync 
# sends a per record email when failure occurs on the record level, or sync level email when failure occurs with the sync itself
# ---------------------------------------------------------------------------

# async def notify(
#     session: aiohttp.ClientSession,
#     error_type: str,
#     error_message: str,
#     attendance_id: int | None = None,
#     template_key: str | None = None,
#     admin_email: str = cfg.admin_email,
# ) -> None:
#     """
#     Single entry point for all sync notification emails.
 
#     - template_key given          -> explicit known scenario, uses its template
#     - attendance_id given, no key -> record-level catchall (still renders against the record)
#     - neither given                -> sync-level catchall, plain mail.mail, no record context
 
#     Never raises — logs instead, so a broken notification can't crash the sync.
#     """
#     try:
#         log.info("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
#         if attendance_id is not None:
#             template_id = TEMPLATE_IDS.get(template_key) if template_key else None
#             if template_id is None:
#                 template_id = RECORD_CATCHALL_TEMPLATE_ID
 
#             await odoo_call(
#                 session,
#                 model="mail.template",
#                 method="send_mail",
#                 params={
#                     "ids": [template_id],
#                     "res_id": attendance_id,
#                     "force_send": True,
#                     "email_values": {
#                         "email_to": admin_email,
#                         # extra context available to templates that reference it,
#                         # e.g. ${ctx.get('error_message')} if you wire it into the template
#                         "email_from": "noreply@globalee.eu",
#                     },
#                 },
#             )
#         else:
#             # No record context at all -> build mail.mail directly, no template involved
#             timestamp = datetime.now(timezone.utc).isoformat()
#             body_html = (
#                 f"<p><b>Chyba synchronizace bez přiřazeného záznamu.</b></p>"
#                 f"<ul>"
#                 f"<li><b>Čas (UTC):</b> {timestamp}</li>"
#                 f"<li><b>Typ chyby:</b> {error_type}</li>"
#                 f"<li><b>Zpráva:</b> {error_message}</li>"
#                 f"</ul>"
#             )
#             await odoo_call(
#                 session,
#                 model="mail.mail",
#                 method="create",
#                 params={
#                     "vals_list": {
#                         "email_to": admin_email,
#                         "subject": f"[Sync] Chyba: {error_type}",
#                         "body_html": body_html,
#                         "state": "outgoing",
#                         "auto_delete": False,
#                     }
#                 },
#             )

 
#     except Exception:
#         # Last resort: don't let a broken notification take down or mask the real failure
#         log.error(
#             "notify() itself failed while handling error_type=%s attendance_id=%s\n%s",
#             error_type,
#             attendance_id,
#         )


 
async def clear_certain_date_issues(session: aiohttp.ClientSession, certain_date) -> None:
    """
    Deletes today's x_sync_issue records before a fresh sync run starts logging
    new ones. Without this, repeated runs on the same day would each add their
    own copy of the same issue, and the digest would show duplicates.
 
    Call this once at the start of the sync, before any log_issue() calls.
    """
 
    existing = await odoo_call(
        session,
        model="x_sync_issue",
        method="search_read",
        params={
            "domain": [["x_studio_date", ">=", certain_date.strftime("%Y-%m-%d 00:00:00")]],
            "fields": ["id"],
        },
    )
 
    if existing:
        await odoo_call(
            session,
            model="x_sync_issue",
            method="unlink",
            params={"ids": [i["id"] for i in existing]},
        )




async def log_issue(
    session: aiohttp.ClientSession,
    issue_type: str,
    message: str,
    certaine_date,
    attendance_id: int | None = None,
) -> None:
    """Records an issue in Odoo instead of sending an email directly."""
    try:
        log.info("[DEBUG] Logged an issue")
        await odoo_call(
            session,
            model="x_sync_issue",
            method="create",
            params={
                "vals_list": {
                    "x_name": message,
                    "x_studio_issue_type": issue_type,
                    "x_studio_message": message,
                    "x_studio_attendance_id": attendance_id,
                    "x_studio_date": certaine_date.strftime("%Y-%m-%d")
                }
            },
        )
    except Exception:
        log.error("log_issue() failed for issue_type=%s\n%s", issue_type, traceback.format_exc())












# ---------------------------------------------------------------------------
# Step 1 - Fetch latest record per employee from the list endpoint
# ---------------------------------------------------------------------------

async def fetch_latest_per_employee(session: aiohttp.ClientSession) -> dict[int, dict]:
    """
    Pages through the list endpoint (POST body, sorted by id descending).
    Highest id = most recent record, so the first occurrence of each
    employeeId is guaranteed to be their latest entry.

    Stops early once N consecutive pages yield no new employees.
    Returns a dict of {employeeId: record}.
    """
    url = cfg.base_url + cfg.list_path
    latest: dict[int, dict] = {}
    page = 1

    pages_without_new = 0
    MAX_STALE_PAGES = 3

    while True:
        body = {
            "page": page,
            "pageSize": cfg.page_size,
            "sortBy": [
                {"propertyName": "id", "descending": True}
            ],
        }
        log.info(f"Fetching list page {page} (employees collected so far: {len(latest)})...")
        data = await post_json(session, url, body)

        # Response schema has both "items" and "records" - prefer "items"
        records = data.get("items") or data.get("records") or []
        if not records:
            log.info("Empty page - stopping pagination.")
            break




        new_this_page = 0
        for rec in records:
            eid = rec.get("employeeId")
            rid = rec.get("id")
            if eid is None:
                continue
            if eid not in latest:
                latest[rid] = rec
                new_this_page += 1

        if new_this_page == 0:
            pages_without_new += 1
            log.info(f"No new employees on page {page} ({pages_without_new}/{MAX_STALE_PAGES} stale pages).")
            if pages_without_new >= MAX_STALE_PAGES:
                log.info(f"Early exit - no new employees in last {MAX_STALE_PAGES} pages.")
                break
        else:
            pages_without_new = 0

        total_count = data.get("totalCount", 0)
        if total_count and page * cfg.page_size >= total_count:
            log.info("Reached last page.")
            break

        page += 1

    log.info(f"Latest records collected for {len(latest)} employees.")
    return latest


# ---------------------------------------------------------------------------
# Step 1b - Fetch record pfor employees on certain daterange from the list endpoint
# ---------------------------------------------------------------------------


async def fetch_records_bydate(session: aiohttp.ClientSession, certain_date: datetime) -> dict[int, dict]:
    """
    Pages through the list endpoint (POST body, sorted by id descending).
    Highest id = most recent record, so the first occurrence of each
    employeeId is guaranteed to be their latest entry.

    Stops early once N consecutive pages yield no new employees.
    Returns a dict of {employeeId: record}.
    """      
    url = cfg.base_url + cfg.list_path
    records_collected: dict[int, dict] = {}
    page = 1
    
    if certain_date is None:
        certain_date = datetime.now()

    pages_without_new = 0
    MAX_STALE_PAGES = 3

    while True:
           
        body = {
            "page": page,
            "pageSize": cfg.page_size,
            "sortBy": [
                {"propertyName": "datetime", "descending": True}
            ],
            
            "filters": [
                {
                "propertyName": "datetime",
                "operator": "<",
                "value": certain_date.strftime("%Y-%m-%dT23:59:59")
                },
            ]
    
        }
        log.info(f"Fetching list page {page} (employees collected so far: {len(records_collected)})...")
        data = await post_json(session, url, body)

        # Response schema has both "items" and "records" - prefer "items"
        records = data.get("records") or data.get("items") or [] 
        if not records:
            log.info("Empty page - stopping pagination.")
            break

        next_day_reached = False

        new_this_page = 0
        for rec in records:
            eid = rec.get("employeeId")
            edate = datetime.strptime(rec.get("datetime"), "%Y-%m-%dT%H:%M:%S")
            rid = rec.get("id")
            if eid is None:
                continue
            elif certain_date.day != edate.day:
                next_day_reached = True
            else: 
                records_collected[rid] = rec
                
        if next_day_reached:
            log.info(f"Reached next date on page {page}.")
            break
        else:
            pages_without_new = 0

        total_count = data.get("totalCount", 0)
        if total_count and page * cfg.page_size >= total_count:
            log.info("Late Exit - reached last page.")
            break

        page += 1

    log.info(f"Records collected for {len(records_collected)} employees for date {certain_date.strftime("%Y-%m-%d")}.")
    return records_collected


# ---------------------------------------------------------------------------
# Step 2 - Enrich records with GPS from detail endpoint (concurrent)
# ---------------------------------------------------------------------------

async def fetch_detail(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    record_id: int,
) -> dict:
    """GET <detail_path>/<id> - id is part of the URL path, no body or params."""
    url = f"{cfg.base_url}{cfg.detail_path}/{record_id}"
    async with semaphore:
        return await get_json(session, url)

async def fetch_building_detail(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    record_id: int,
) -> dict:
    """GET <detail_path>/<id> - id is part of the URL path, no body or params."""
    url = f"{cfg.base_url}{cfg.building_detail_path}/{record_id}"
    async with semaphore:
        return await get_json(session, url)


async def enrich_with_gps(
    session: aiohttp.ClientSession,
    latest: dict[int, dict],
) -> list[dict]:
    """
    Fires detail calls concurrently (up to MAX_CONCURRENT at once).
    Merges GPS fields back into each employee's latest record.
    Skips employees where GPS is unavailable (0,0 or missing).
    """
    semaphore = asyncio.Semaphore(cfg.max_concurrent)
    records_ids = list(latest.keys())


    tasks = {
        rid: asyncio.create_task(
            fetch_detail(session, semaphore, rid)
        )
        for rid in records_ids
    }

    tasks_buildings = {
        rid: asyncio.create_task(
            fetch_building_detail(session, semaphore, latest[rid]["buildingId"])
        )
        for rid in records_ids
    }

    results = []
    for rid, task in tasks.items():
        base_record = latest[rid]
        try:
            detail = await task
        except Exception as exc:
            log.warning(f"Detail fetch failed for employeeId={base_record['employeeId']} (record id={rid}): {exc}")
            continue
        try:
            building_detail = await tasks_buildings[rid]
        except:
            log.warning(f"Building detail fetch failed for employeeId={base_record['employeeId']} (record id={rid}): {exc}")

        building_stavario_code = building_detail.get("code")
        gps_x = detail.get("record").get("gpsX") or base_record.get("gpsX", 0)
        gps_y = detail.get("record").get("gpsY") or base_record.get("gpsY", 0)

        if not gps_x and not gps_y:
            log.info(f"No GPS data for employeeId={base_record['employeeId']} - skipping.")
            continue
        log.info(f"[DEBUG] Deviation info is {detail.get("record").get("deviationGps")}; {detail.get("record").get("allowedDeviationGps")}")
        results.append({
            "employeeId":           base_record.get('employeeId'),
            "employeeName":         base_record.get("employeeName"),
            "employeeGroup":        base_record.get("employeeGroup"),
            "buildingName":         base_record.get("buildingName"),
            "buildingId":           base_record.get("buildingId"),
            "datetime":             base_record.get("datetime"),
            "gpsX":                 gps_x,   # latitude
            "gpsY":                 gps_y,   # longitude
            "accuracyGps":          detail.get("record").get("accuracyGps") or base_record.get("accuracyGps"),
            "deviationGps":         detail.get("record").get("deviationGps"),
            "allowedDeviationGps":  detail.get("record").get("allowedDeviationGps"),
            "type":                 base_record.get("type"),
            "buildingCode":         building_stavario_code,

        })

    log.info(f"GPS data enriched for {len(results)}/{len(records_ids)} employees.")
    return results

# ---------------------------------------------------------------------------
# Step 3 - Odoo JSON-2 helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step 4 - Build Odoo employee lookup and write GPS data
# Deprecated.
# ---------------------------------------------------------------------------

async def build_odoo_lookup(session: aiohttp.ClientSession) -> dict[str, int]:
    """
    !!DEPRECATED!!
    Fetches all hr.employee records that have x_studio_cislo_stavario set.
    Returns {code: odoo_employee_id} where code is the 3-char Stavario identifier.
    Logs a warning for any duplicate codes (shouldn't happen, but good to know).
    """
    log.info("Fetching Odoo employee lookup table...")
    records = await odoo_call(
        session,
        model="hr.employee",
        method="search_read",
        params={
            "fields": ["id", "name", "x_studio_cislo_stavario"],
            "limit": 0,
            "domain": [["x_studio_cislo_stavario", "!=", False]]
        },
    )

    lookup: dict[str, int] = {}
    for rec in records:
        code = (rec.get("x_studio_cislo_stavario") or "").strip()
        if not code:
            continue
        if code in lookup:
            log.warning(f"Duplicate x_studio_cislo_stavario='{code}' on employee id={rec['id']} ('{rec['name']}') - skipping duplicate.")
            continue
        lookup[code] = rec["id"]

    log.info(f"Odoo lookup ready: {len(lookup)} employees with Stavario codes.")
    return lookup


async def build_odoo_building_lookup(session: aiohttp.ClientSession) -> dict[str, int]:
    """
    !!DEPRECATED!!
    Fetches all hr.employee records that have x_studio_cislo_stavario set.
    Returns {code: odoo_employee_id} where code is the 3-char Stavario identifier.
    Logs a warning for any duplicate codes (shouldn't happen, but good to know).
    """
    log.info("Fetching Odoo employee lookup table...")
    records = await odoo_call(
        session,
        model="x_construction_project",
        method="search_read",
        params={
            "fields": ["id", "x_name", "x_studio_code"],
            "limit": 0,
            "domain": [["x_studio_code", "!=", False]]
        },
    )

    lookup: dict[str, int] = {}
    for rec in records:
        code = (rec.get("x_studio_code") or "").strip()
        if not code:
            continue
        if code in lookup:
            log.warning(f"Duplicate x_studio_code='{code}' on construction proj id={rec['id']} ('{rec['name']}') - skipping duplicate.")
            continue
        lookup[code] = rec["id"]

    log.info(f"Odoo lookup ready: {len(lookup)} employees with Stavario codes.")
    return lookup

async def write_gps_to_odoo(
    session: aiohttp.ClientSession,
    enriched: list[dict],
    odoo_lookup: dict[str, int],
) -> None:
    """
    Matches each enriched Stavario record to an Odoo employee via the first
    3 characters of employeeName (= x_studio_cislo_stavario), then writes
    GPS + checkin data using JSON-RPC write().

    Fields written (adjust x_studio_* names to match your actual Studio fields):
        x_studio_gps_lat    - gpsX  (latitude)
        x_studio_gps_lng    - gpsY  (longitude)
        x_studio_building   - buildingName
        x_studio_checkin_dt - datetime of latest check-in
    """
    success = 0
    skipped = 0

    for record in enriched:
        name = record.get("employeeName") or ""
        code = name[:3].strip()

        odoo_id = odoo_lookup.get(code)
        if odoo_id is None:
            log.warning(f"No Odoo employee found for code '{code}' (Stavario employeeId={record['employeeId']}, name='{name}') - skipping.")
            skipped += 1
            continue

        values = {
            "x_studio_last_seen_lat":    record["gpsX"],
            "x_studio_last_seen_lng":    record["gpsY"],
        }

        print(f"|\n{[odoo_id]}\n|\n{values}\n|----")
        print(f"{[[odoo_id], values]}\n|")

        try:
            await odoo_call(
                session,
                model="hr.employee",
                method="write",

                params={"vals":values,"ids":odoo_id},
            )

            success += 1
            log.info(f"  ✓ Updated Odoo employee id={odoo_id} (code='{code}', name='{name}')")
        except Exception as exc:
            log.warning(f"  ✗ Failed to write Odoo employee id={odoo_id} (code='{code}'): {exc}")
            skipped += 1

    log.info(f"Odoo write complete: {success} updated, {skipped} skipped.")







# ---------------------------------------------------------------------------
# More utility,  datetime conversion
# ---------------------------------------------------------------------------
def to_odoo_dt(iso_str: str | None) -> str | None:
    """
    Stavario datetime is local time (UTC+2), Odoo stores in UTC.
    Subtract 2 hours so Odoo stores correct UTC and displays correct local time.
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str) - timedelta(hours=2)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        log.warning(f"Could not parse datetime '{iso_str}' — passing as-is.")
        return iso_str

# ---------------------------------------------------------------------------
# Step 4b - Attendance record sync
# ---------------------------------------------------------------------------

# Stavario type ids that mean the employee has arrived / is present
CHECKIN_TYPES  = {"1"}          # Arrival
# Stavario type ids that mean the employee has left
CHECKOUT_TYPES = {"3", "4"}       # Transfer, Departure  (12=Break excluded per config)


async def sync_attendance(
    session: aiohttp.ClientSession,
    enriched: list[dict],
    odoo_lookup: dict[str, int],
    building_lookup: dict[str, int], 
    certain_date: datetime
) -> None:
    """
    For each enriched record:
      - type in CHECKIN_TYPES  -> find open hr.attendance for this employee;
                                  if none exists, create one with check_in = record datetime
      - type in CHECKOUT_TYPES -> find open hr.attendance for this employee;
                                  if one exists, close it with check_out = record datetime
      - anything else          -> skip
    
    """
    success = 0
    skipped = 0
    enriched.reverse()
    
    

    for record in enriched:
        name  = record.get("employeeName") or ""
        code  = name[:3].strip()
        rtype = record.get("type")
        buildcode = record.get("buildingCode")
        new_attendance_id = int()
        
        
        if rtype not in CHECKIN_TYPES and rtype not in CHECKOUT_TYPES:
            log.info(f"  - Skipping type={rtype} for '{name}'")
            skipped += 1
            continue

        odoo_id = odoo_lookup.get(code)
        if odoo_id is None:
            log.warning(f"No Odoo employee for code '{code}' (name='{name}') - skipping.")
            skipped += 1
            continue  

        odoo_build_id = building_lookup.get(buildcode)
        open_records = await odoo_call(
            session,
            model="hr.attendance",
            method="search_read",
            params={
                "domain": [
                    ["employee_id", "=", odoo_id],
                    ["date", "=", certain_date.strftime("%Y-%m-%d")]
                ],
                "fields": ["id", "check_in"],
                "limit":  1,
            },
        )
        open_id = open_records[0]["id"] if open_records else None

        dt = to_odoo_dt(record.get("datetime"))

        vals = {"x_studio_gps_latitude": record["gpsX"], "x_studio_gps_longitude": record["gpsY"]}
        
        odoo_build_id = building_lookup.get(buildcode)
        if odoo_build_id is None:
            log.warning(f"No Odoo construction for '{code}' (employee name='{name}') - not skipping, but no construction data will be synced.")
        else:
            vals["x_studio_project"] = odoo_build_id    


        try:
            if open_id:
                log.info(f"Employee id={odoo_id} ('{name}') has an attendance for date {certain_date.strftime("%Y-%m-%dT%H:%M:%S")}, updating it")
                
                if rtype in CHECKIN_TYPES:
                    vals["check_in"] = dt
                elif rtype in CHECKOUT_TYPES:
                    vals["check_out"] = dt
                else:
                    raise Exception("Unknown record type") 
                
                await odoo_call(
                    session,
                    model="hr.attendance",
                    method="write",
                    params={"ids": open_id, "vals": vals},
                )
                success += 1
                log.info(f"  ✓ Updated attendace record #{open_id} for employee id={odoo_id} ('{name}') at {dt} [{"check out" if rtype in CHECKOUT_TYPES else "check in"}]")
                
            else:
                
                log.info(f"Employee id={odoo_id} ('{name}') has no attendance record for date {certain_date.strftime("%Y-%m-%dT%H:%M:%S")}, creating it")
                
                
                vals["employee_id"] = odoo_id
                
                if rtype in CHECKIN_TYPES:
                    vals["check_in"] = dt
                elif rtype in CHECKOUT_TYPES:
                    vals["check_out"] = dt
                else:
                    raise Exception("Unknown record type") 
                
                result = await odoo_call(
                    session,
                    model="hr.attendance",
                    method="create",
                    params={"vals_list": vals},
                )
                success += 1
                log.info(f"  ✓ Created attendance check-in for employee id={odoo_id} ('{name}') at {dt}")
                new_attendance_id = result if isinstance(result, int) else result.get("id")

            log.info(f"deviationGps: {record.get("deviationGps")}, allowedDeviationGps: {record.get("allowedDeviationGps")}")
            if record.get("deviationGps") and record.get("deviationGps") > record.get("allowedDeviationGps") * 2 and str(record.get("buildingId")) != "60055557":
                
                
                log.info(f"the id is {"open_id" if open_id else "not open_id"} it is {open_id if open_id else new_attendance_id}")
                
                await log_issue(session, issue_type="Mimo rozsah odchylky", message="Odhlášení/přihlášení bylo provedeno mimo povolený rozsah odchylek", attendance_id=(open_id if open_id else new_attendance_id), certaine_date=certain_date)


                
                
            
        except Exception as exc:
            log.warning(f"  ✗ Attendance update failed for employee id={odoo_id} ('{name}') ({record.get("datetime")}) : \n{exc}")
            skipped += 1
            
            try:
                exc_json = json.loads(str(exc)[10:])
                if '"Check Out" time cannot be earlier than "Check In" time.' in exc_json["message"]:
                    await log_issue(session=session, message="Zaměstnanec pravděpodobně zmeškal odhlášení z předchozího dne", issue_type="Chybí odhlášení", attendance_id=(open_id if open_id else new_attendance_id),certaine_date=certain_date)
                else:
                    await log_issue(session=session, message="Během pokusu o synchronizaci došlo k neznámé chybě", issue_type="Neznámá chyba", attendance_id=(open_id if open_id else new_attendance_id),certaine_date=certain_date)
            except:
                await log_issue(session=session, message="Během pokusu o synchronizaci došlo k neznámé chybě", issue_type="Neznámá chyba", attendance_id=(open_id if open_id else new_attendance_id),certaine_date=certain_date)
                
            
        

    log.info(f"Attendance sync complete: {success} updated, {skipped} skipped.")




# where things happen

async def main(certain_date: datetime) -> None:
    log.info(f"=== Stavario -> Odoo GPS sync starting -> date = {certain_date.strftime("%Y-%m-%d")} ===")
    started = datetime.now(timezone.utc)

    connector = aiohttp.TCPConnector(limit=cfg.max_concurrent + 5)
    async with aiohttp.ClientSession(connector=connector) as session:
        await auth.token(session)


        await clear_certain_date_issues(session, certain_date)

        latest = await fetch_records_bydate(session, certain_date)
        if not latest:
            log.warning("No records found - nothing to push.")
            return

        enriched = await enrich_with_gps(session, latest)
        if not enriched:
            log.warning("No GPS-enriched records - nothing to push.")
            
            return
        odoo_lookup = await build_odoo_lookup(session)
        if not odoo_lookup:
            log.error("Odoo lookup is empty - check ODOO_API_KEY and that employees have x_studio_cislo_stavario set.")
            
            return
        odoo_buildings_lookup = await build_odoo_building_lookup(session)
        if not odoo_buildings_lookup:
            log.error("Odoo buildings lookup is empty - check ODOO_API_KEY and that buildings have x_studio_code set.")
            
            return
        await sync_attendance(session, enriched, odoo_lookup, odoo_buildings_lookup, certain_date)


    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info(f"=== Done in {elapsed:.1f}s ===")


if __name__ == "__main__":
    try:
        asyncio.run(main(certain_date=(datetime.now() - timedelta(days=1))))
        asyncio.run(main(certain_date=datetime.now()))
        
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        
        log.error(f"Fatal error: {exc}", exc_info=True)
        sys.exit(1)