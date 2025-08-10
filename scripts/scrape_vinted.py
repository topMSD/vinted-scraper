import requests
import csv
import datetime

# Din Vinted bruger-id eller søge-URL
VINTED_URL = "https://www.vinted.dk/api/v2/catalog/items?user_id=197061816&per_page=100"

# Output CSV-fil
OUTPUT_FILE = "vinted_export.csv"

def fetch_vinted_items():
    response = requests.get(VINTED_URL)
    response.raise_for_status()
    data = response.json()
    return data.get("items", [])

def save_to_csv(items):
    with open(OUTPUT_FILE, mode="w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["title", "price", "currency", "description", "url", "photo_url", "date"])
        for item in items:
            title = item.get("title", "")
            price = item.get("price", {}).get("amount", "")
            currency = item.get("price", {}).get("currency", "")
            description = item.get("description", "")
            url = f"https://www.vinted.dk/items/{item.get('id')}"
            photo_url = item.get("photo", {}).get("url", "")
            date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow([title, price, currency, description, url, photo_url, date])

if __name__ == "__main__":
    print("Henter data fra Vinted...")
    items = fetch_vinted_items()
    print(f"Fundet {len(items)} varer")
    save_to_csv(items)
    print(f"Data gemt i {OUTPUT_FILE}")
