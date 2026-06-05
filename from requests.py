import json
from requests.auth import HTTPBasicAuth
import requests
import os
from requests.auth import HTTPBasicAuth

USERNAME = os.environ["API_USERNAME"]
PASSWORD = os.environ["API_PASSWORD"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
CARAPI_URL =  os.environ["CARAPI_URL"]


basic = HTTPBasicAuth(USERNAME, PASSWORD)

# payload = env.context.get('payload', {})

# lat = payload.get('latitude')
# lng = payload.get('longitude')
# vehicle_name = payload.get('name')

# if vehicle_name and lat and lng:
#     vehicle = env['fleet.vehicle'].sudo().browse(int(vehicle_name))
#     if vehicle.exists():
#         vehicle.write({
#             'x_studio_last_seen_location': f"{lat},{lng}",
#         })



url_carlocationsapi = "https://a1.gpsguard.eu/api/v1/vehicles/group/ZOJO"
url_odoowebhook = "https://globalee.odoo.com/web/hook/65698606-7fe6-4672-9a26-6a56269ae7f7"

headers = {
    "Content-Type": "application/json",
    #"X-Odoo-WEBHOOK-TOKEN": "65698606-7fe6-4672-9a26-6a56269ae7f7",  # from the webhook URL itself
}

allcars = requests.get(CARAPI_URL, auth=basic).json()

for car in allcars:
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "lat": car["LastPosition"]["Latitude"],
            "lng": car["LastPosition"]["Longitude"],
            "license_plate": car["SPZ"],
        }
    }
    print(payload)
    response = requests.post(
        WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json"}
    )
    print("Status Code", response.status_code)
    print("Response", response.text)