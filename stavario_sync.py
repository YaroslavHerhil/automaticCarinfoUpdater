"""
Stavario -> Odoo GPS sync
------------------------
GitHub Actions entry point. Run as:
    python stavario_sync.py

Required environment variables (set as GitHub Actions secrets):
    STAVARIO_BASE_URL    e.g. https://api.stavario.com
    STAVARIO_USERNAME
    STAVARIO_PASSWORD
    STAVARIO_LOGIN_PATH  e.g. /api/auth/login
    STAVARIO_LIST_PATH   e.g. /api/attendance
    STAVARIO_DETAIL_PATH e.g. /api/attendance  (record id appended as /<id>)
    ODOO_URL             e.g. https://globalee.odoo.com
    ODOO_DB              your Odoo database name, sent as X-Odoo-Database header
                         (for globalee.odoo.com this is usually just "globalee")
    ODOO_API_KEY         generated in Odoo Settings -> Technical -> API Keys
                         uses Bearer token auth - no password needed

Optional:
    PAGE_SIZE            records per page (default: 100)
    MAX_CONCURRENT       max parallel detail calls (default: 10)
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

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
class Config:
    base_url:     str = os.environ["STAVARIO_BASE_URL"].rstrip("/")
    username:     str = os.environ["STAVARIO_USERNAME"]
    password:     str = os.environ["STAVARIO_PASSWORD"]
    login_path:   str = os.environ["STAVARIO_LOGIN_PATH"]
    list_path:    str = os.environ["STAVARIO_LIST_PATH"]
    detail_path:  str = os.environ["STAVARIO_DETAIL_PATH"]
    odoo_url:     str = os.environ["ODOO_URL"].rstrip("/")
    odoo_db:      str = os.environ["ODOO_DB"]
    odoo_api_key: str = os.environ["ODOO_API_KEY"]
    page_size:    int = int(os.environ.get("PAGE_SIZE", 100))
    max_concurrent: int = int(os.environ.get("MAX_CONCURRENT", 10))

cfg = Config()



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
            if eid is None:
                continue
            if eid not in latest:
                latest[eid] = rec
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
    employee_ids = list(latest.keys())

    tasks = {
        eid: asyncio.create_task(
            fetch_detail(session, semaphore, latest[eid]["id"])
        )
        for eid in employee_ids
    }

    results = []
    for eid, task in tasks.items():
        base_record = latest[eid]
        try:
            detail = await task
        except Exception as exc:
            log.warning(f"Detail fetch failed for employeeId={eid} (record id={base_record['id']}): {exc}")
            continue

        gps_x = detail.get("record").get("gpsX") or base_record.get("gpsX", 0)
        gps_y = detail.get("record").get("gpsY") or base_record.get("gpsY", 0)

        if not gps_x and not gps_y:
            log.info(f"No GPS data for employeeId={eid} - skipping.")
            continue

        results.append({
            "employeeId":    eid,
            "employeeName":  base_record.get("employeeName"),
            "employeeGroup": base_record.get("employeeGroup"),
            "buildingName":  base_record.get("buildingName"),
            "buildingId":    base_record.get("buildingId"),
            "datetime":      base_record.get("datetime"),
            "gpsX":     gps_x,   # latitude
            "gpsY":     gps_y,   # longitude
            "accuracyGps":   detail.get("accuracyGps") or base_record.get("accuracyGps"),
            "type":          base_record.get("type"),
        })

    log.info(f"GPS data enriched for {len(results)}/{len(employee_ids)} employees.")
    return results


# ---------------------------------------------------------------------------
# Step 3 - Odoo JSON-2 helpers
# ---------------------------------------------------------------------------

async def odoo_call(
    session: aiohttp.ClientSession,
    model: str,
    method: str,
    params: dict | None = None,
) -> any:
    """
    Calls an Odoo model method via the JSON-2 API.

    Endpoint: POST /{model}/{method}
    Auth:     Authorization: Bearer <api_key>
              X-Odoo-Database: <db>
    """
    url = f"{cfg.odoo_url}/{model}/{method}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.odoo_api_key}",
        "X-Odoo-Database": cfg.odoo_db,
    }
    async with session.post(url, json=params, headers=headers) as resp:

        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"Odoo {resp.status}: {text}")

        data = await resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Odoo API error: {data['error']}")
    return data


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
    Converts Stavario datetime string to Odoo's expected format.
    e.g. "2026-06-11T08:30:00.000" → "2026-06-11 08:30:00"
    """
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d %H:%M:%S")
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

    for record in enriched:
        name  = record.get("employeeName") or ""
        code  = name[:3].strip()
        rtype = record.get("type")

        if rtype not in CHECKIN_TYPES and rtype not in CHECKOUT_TYPES:
            log.info(f"  - Skipping type={rtype} for '{name}'")
            skipped += 1
            continue

        odoo_id = odoo_lookup.get(code)
        if odoo_id is None:
            log.warning(f"No Odoo employee for code '{code}' (name='{name}') - skipping.")
            skipped += 1
            continue

        open_records = await odoo_call(
            session,
            model="hr.attendance",
            method="search_read",
            params={
                "domain": [
                    ["employee_id", "=", odoo_id],
                    ["check_out",   "=", False],
                ],
                "fields": ["id", "check_in"],
                "limit":  1,
            },
        )
        open_id = open_records[0]["id"] if open_records else None

        dt = to_odoo_dt(record.get("datetime"))

        try:
            if rtype in CHECKIN_TYPES:
                if open_id:
                    log.info(f"  - Employee id={odoo_id} ('{name}') already has open attendance #{open_id} - skipping check-in.")
                    skipped += 1
                else:
                    await odoo_call(
                        session,
                        model="hr.attendance",
                        method="create",
                        params={"vals_list": {"employee_id": odoo_id, "check_in": dt, "x_studio_gps_latitude": record["gpsX"], "x_studio_gps_longitude": record["gpsY"], }},
                    )
                    success += 1
                    log.info(f"  ✓ Created attendance check-in for employee id={odoo_id} ('{name}') at {dt}")

            elif rtype in CHECKOUT_TYPES:
                if not open_id:
                    log.info(f"  - No open attendance for employee id={odoo_id} ('{name}') - nothing to close.")
                    skipped += 1
                else:
                    await odoo_call(
                        session,
                        model="hr.attendance",
                        method="write",
                        params={"ids": open_id, "vals": {"check_out": dt, "x_studio_gps_latitude": record["gpsX"], "x_studio_gps_longitude": record["gpsY"],}},
                    )
                    success += 1
                    log.info(f"  ✓ Closed attendance #{open_id} for employee id={odoo_id} ('{name}') at {dt}")

        except Exception as exc:
            log.warning(f"  ✗ Attendance update failed for employee id={odoo_id} ('{name}'): {exc}")
            skipped += 1

    log.info(f"Attendance sync complete: {success} updated, {skipped} skipped.")




# where things happen

async def main() -> None:
    log.info("=== Stavario -> Odoo GPS sync starting ===")
    started = datetime.now(timezone.utc)

    connector = aiohttp.TCPConnector(limit=cfg.max_concurrent + 5)
    async with aiohttp.ClientSession(connector=connector) as session:
        await auth.token(session)

        latest = await fetch_latest_per_employee(session)
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

        await sync_attendance(session, enriched, odoo_lookup)


    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info(f"=== Done in {elapsed:.1f}s ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        log.error(f"Fatal error: {exc}", exc_info=True)
        sys.exit(1)