import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone

import aiohttp

from odoo_client import odoo_call, cfg  # adjust import path to wherever you split this out to

logger = logging.getLogger(__name__)


async def send_digest(session: aiohttp.ClientSession, admin_email: str = cfg.admin_email, hours: int = 24) -> None:
    """
    Sends one grouped email covering issues detected in the last `hours` window.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    issues = await odoo_call(
        session,
        model="x_sync_issue",
        method="search_read",
        params={
            "domain": [["x_studio_date", ">=", since]],
            "fields": ["id", "x_studio_issue_type", "x_studio_message", "x_studio_employee_id", "x_studio_date"],
        },
    )

    if not issues:
        return  # nothing recent to report, skip sending

    grouped: dict[str, list] = {}
    for issue in issues:
        grouped.setdefault(issue["x_studio_issue_type"], []).append(issue)

    sections = []
    for issue_type, items in grouped.items():
        rows = "".join(
            f"<li>{i['x_studio_employee_id'][1] if i['x_studio_employee_id'] else '-'} — "
            f"{i['x_studio_message']} ({i['x_studio_date']})</li>"
            for i in items
        )
        sections.append(f"<h4>{issue_type}</h4><ul>{rows}</ul>")

    body_html = f"<p>Přehled problémů ze synchronizace za posledních {hours} h:</p>{''.join(sections)}"

    await odoo_call(
        session,
        model="mail.mail",
        method="create",
        params={
            "vals_list": {
                "email_to": admin_email,
                "subject": f"[Sync] Denní přehled problémů ({len(issues)})",
                "body_html": body_html,
                "state": "outgoing",
                "auto_delete": False,
            }
        },
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    async with aiohttp.ClientSession() as session:
        try:
            await send_digest(session)
        except Exception:
            logger.error("send_digest() failed:\n%s", traceback.format_exc())


if __name__ == "__main__":
    asyncio.run(main())