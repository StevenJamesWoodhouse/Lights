import argparse
import hashlib
import json
import time
import urllib.parse
from pathlib import Path

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


COUNTRY_SERVERS = {
    "AU": "http://oameshcloud.magichue.net:8081/MeshClouds/",
    "AL": "http://ttmeshcloud.magichue.net:8081/MeshClouds/",
    "CN": "http://cnmeshcloud.magichue.net:8081/MeshClouds/",
    "GB": "http://eumeshcloud.magichue.net:8081/MeshClouds/",
    "ES": "http://eumeshcloud.magichue.net:8081/MeshClouds/",
    "FR": "http://eumeshcloud.magichue.net:8081/MeshClouds/",
    "DE": "http://eumeshcloud.magichue.net:8081/MeshClouds/",
    "IT": "http://eumeshcloud.magichue.net:8081/MeshClouds/",
    "JP": "http://dymeshcloud.magichue.net:8081/MeshClouds/",
    "RU": "http://eumeshcloud.magichue.net:8081/MeshClouds/",
    "US": "http://usmeshcloud.magichue.net:8081/MeshClouds/",
}
SECRET_KEY = b"0FC154F9C01DFA9656524A0EFABC994F"
DEFAULT_OUTPUT = Path("hao_deng_meshes.json")


def timestamp_checkcode():
    timestamp = str(int(time.time() * 1000))
    value = ("ZG" + timestamp).encode("ascii")
    cipher = AES.new(SECRET_KEY, AES.MODE_ECB)
    return timestamp, cipher.encrypt(pad(value, AES.block_size)).hex()


def headers(token=""):
    return {
        "User-Agent": "HaoDeng/1.5.7(ANDROID,10,en-US)",
        "Accept-Language": "en-US",
        "Accept": "application/json",
        "token": token,
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }


def checked_json(response, label):
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"{label} returned non-JSON HTTP {response.status_code}") from exc
    if response.status_code != 200 or not data.get("ok", response.status_code == 200):
        raise RuntimeError(f"{label} failed HTTP {response.status_code}: {data}")
    return data


class HaoDengCloud:
    def __init__(self, username, password, country):
        country = country.upper()
        if country not in COUNTRY_SERVERS:
            raise ValueError(f"Unsupported country {country}; choose one of {', '.join(COUNTRY_SERVERS)}")
        self.base_url = COUNTRY_SERVERS[country]
        self.username = username
        self.password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
        self.token = None
        self.user_id = None

    def login(self):
        timestamp, checkcode = timestamp_checkcode()
        payload = {
            "userID": self.username,
            "password": self.password_md5,
            "appSys": "Android",
            "timestamp": timestamp,
            "appVer": "",
            "checkcode": checkcode,
        }
        url = self.base_url + "apixp/User001/LoginForUser/ZG"
        data = checked_json(requests.post(url, headers=headers(), json=payload, timeout=20), "login")
        result = data["result"]
        self.user_id = result["userId"]
        self.token = result["auth_token"]
        return result

    def get_meshes(self):
        url = (
            self.base_url
            + "apixp/MeshData/GetMyMeshPlaceItems/ZG?userId="
            + urllib.parse.quote_plus(self.user_id)
        )
        data = checked_json(requests.get(url, headers=headers(self.token), timeout=20), "get meshes")
        return data["result"]

    def get_mesh_devices(self, place_uni_id):
        endpoint = (
            "apixp/MeshData/GetMyMeshDeviceItems/ZG?placeUniID="
            + urllib.parse.quote_plus(place_uni_id)
            + "&userId="
            + urllib.parse.quote_plus(self.user_id)
        )
        data = checked_json(
            requests.get(self.base_url + endpoint, headers=headers(self.token), timeout=20),
            "get mesh devices",
        )
        return data["result"]

    def pull_all(self):
        login = self.login()
        meshes = self.get_meshes()
        for mesh in meshes:
            mesh["devices"] = self.get_mesh_devices(mesh["placeUniID"])
        return {"login": login, "meshes": meshes}


def summarize(data):
    for index, mesh in enumerate(data["meshes"], start=1):
        print(f"\nMesh {index}: {mesh.get('displayName', '-')}")
        print(f"  placeUniID: {mesh.get('placeUniID')}")
        print(f"  meshKey: {mesh.get('meshKey')}")
        print(f"  meshPassword: {mesh.get('meshPassword')}")
        print(f"  meshLTK: {mesh.get('meshLTK')}")
        print(f"  devices: {len(mesh.get('devices') or [])}")
        for device in mesh.get("devices") or []:
            print(
                "   "
                f"mac={device.get('macAddress')} "
                f"meshAddress={device.get('meshAddress')} "
                f"type={device.get('deviceType')} "
                f"name={device.get('displayName')}"
            )


def main():
    parser = argparse.ArgumentParser(description="Pull Hao Deng/Zengge mesh credentials from cloud.")
    parser.add_argument("username")
    parser.add_argument("password")
    parser.add_argument("--country", default="GB")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    data = HaoDengCloud(args.username, args.password, args.country).pull_all()
    args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Saved: {args.output}")
    summarize(data)


if __name__ == "__main__":
    main()
