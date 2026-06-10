import requests
import xml.etree.ElementTree as ET

API_KEY = "Kv3egYQ5pbATb"

def lookup(company_name: str):
    resp = requests.get(
        "https://api.houjin-bangou.nta.go.jp/4/name",
        params={"id": API_KEY, "name": company_name, "type": "12"},
        timeout=20,
    )
    root = ET.fromstring(resp.content)
    results = []
    for corp in root.findall(".//corporation"):
        name = corp.findtext("name", "").strip()
        number = corp.findtext("corporateNumber", "").strip()
        address = corp.findtext("prefectureName", "") + corp.findtext("cityName", "")
        if name:
            results.append((number, name, address))
    return results

if __name__ == "__main__":
    company = input("会社名を入力: ")
    hits = lookup(company)
    if not hits:
        print("見つかりませんでした")
    else:
        for number, name, address in hits[:10]:
            print(f"{number}  {name}  {address}")
