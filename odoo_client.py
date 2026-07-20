
import os

import aiohttp

# # ---------------------------------------------------------------------------
class Config:
    base_url:                   str = os.environ["STAVARIO_BASE_URL"].rstrip("/")
    username:                   str = os.environ["STAVARIO_USERNAME"]
    password:                   str = os.environ["STAVARIO_PASSWORD"]
    login_path:                 str = os.environ["STAVARIO_LOGIN_PATH"]
    list_path:                  str = os.environ["STAVARIO_LIST_PATH"]
    detail_path:                str = os.environ["STAVARIO_DETAIL_PATH"]
    building_detail_path:       str = os.environ["STAVARIO_BUILDING_DETAIL_PATH"]
    odoo_url:                   str = os.environ["ODOO_URL"].rstrip("/")
    odoo_db:                    str = os.environ["ODOO_DB"]
    odoo_api_key:               str = os.environ["ODOO_API_KEY"]
    page_size:                  int = int(os.environ.get("PAGE_SIZE", 100))
    max_concurrent:             int = int(os.environ.get("MAX_CONCURRENT", 10))
    admin_email:                str = os.environ["ADMIN_EMAIL"]


cfg = Config()


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