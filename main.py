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


allcars = requests.get(CARAPI_URL, auth=basic).json()

for car in allcars:
    payload = {
        "lat": float(car["LastPosition"]["Latitude"]),
        "lng": float(car["LastPosition"]["Longitude"]),
        "license_plate": car["SPZ"],
    }
    print(payload)
    response = requests.post(
        WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json"}
    )
    print("Status Code", response.status_code)
    print("Response", response.text)
    
    #1AH B685