#!/usr/bin/env python3
"""Refresh diesel retail-price data for the European Diesel Price Monitor."""
import csv
import gzip
import io
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data.json"
EU_CSV_GZ = "https://www.fuel-prices.eu/research/data/d1-eu-weekly-bulletin/fp-d1-eu-weekly-panel.csv.gz"
UA_PAGE = "https://azs24.com.ua/dp/"
NBU_API = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"
EU_COUNTRIES = {
    "SK": ("Slovakia", "Словаччина"),
    "CZ": ("Czechia", "Чехія"),
    "AT": ("Austria", "Австрія"),
    "DE": ("Germany", "Німеччина"),
    "PL": ("Poland", "Польща"),
    "LT": ("Lithuania", "Литва"),
    "LV": ("Latvia", "Латвія"),
}
UA_NAME = "Україна"

def fetch(url, timeout=45):
    req = Request(url, headers={"User-Agent": "EuropeanDieselPriceMonitor/1.0 (GitHub Actions)"})
    with urlopen(req, timeout=timeout) as response:
        return response.read()

def load_existing():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"countries": {}}

def parse_num(value):
    value = str(value or "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group(0)) if match else None

def parse_eu(existing):
    raw = gzip.decompress(fetch(EU_CSV_GZ)).decode("utf-8-sig")
    lines = [line for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    rows = []
    for row in reader:
        code = (row.get("country_code") or "").strip().upper()
        if code not in EU_COUNTRIES:
            continue
        week = (row.get("week") or "").strip()
        price = parse_num(row.get("diesel_eur_l"))
        if not week or price is None:
            continue
        rows.append((code, week, price, parse_num(row.get("diesel_local_l")), (row.get("currency") or "EUR").strip()))
    if not rows:
        raise RuntimeError("EU weekly dataset returned no usable diesel rows")

    countries = existing.setdefault("countries", {})
    cutoff = (date.today() - timedelta(days=370)).isoformat()
    latest_by_code = {}
    history_by_code = {}
    for code, week, eur, local, currency in rows:
        history_by_code.setdefault(code, []).append({
            "date": week,
            "eur_per_litre": round(eur, 4),
            "local_price": round(local, 4) if local is not None else None,
            "currency": currency,
        })
        if code not in latest_by_code or week > latest_by_code[code][0]:
            latest_by_code[code] = (week, eur, local, currency)

    for code, (country_en, country_ua) in EU_COUNTRIES.items():
        latest = latest_by_code.get(code)
        if not latest:
            continue
        week, eur, local, currency = latest
        history = sorted(
            (point for point in history_by_code.get(code, []) if point["date"] >= cutoff),
            key=lambda point: point["date"],
        )
        countries[code] = {
            "eur_per_litre": round(eur, 4),
            "local_price": round(local, 4) if local is not None else round(eur, 4),
            "currency": currency,
            "data_date": week,
            "source_label": "Єврокомісія · Weekly Oil Bulletin",
            "source_url": "https://energy.ec.europa.eu/data-and-analysis/weekly-oil-bulletin_en",
            "history": history,
        }
    return max(item[0] for item in latest_by_code.values())

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current_table = []
        self.current_row = []
        self.cell = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table = True
            self.current_table = []
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.current_row = []
        elif self.in_row and tag in ("td", "th"):
            self.in_cell = True
            self.cell = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            text = " ".join(" ".join(self.cell).split())
            self.current_row.append(text)
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                self.current_table.append(self.current_row)
            self.in_row = False
        elif tag == "table" and self.in_table:
            self.tables.append(self.current_table)
            self.in_table = False

def parse_ua_page():
    html = fetch(UA_PAGE).decode("utf-8", errors="replace")
    parser = TableParser()
    parser.feed(html)
    candidates = []
    for table in parser.tables:
        for row in table:
            if len(row) >= 4 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row[0].strip()):
                avg = parse_num(row[2])
                if avg is not None:
                    candidates.append((row[0].strip(), avg))
    if not candidates:
        raise RuntimeError("Could not find the dated average-price table on AZS24")
    return max(candidates, key=lambda item: item[0])

def get_eur_uah_rate(rate_date):
    target = date.fromisoformat(rate_date)
    for offset in range(0, 8):
        day = target - timedelta(days=offset)
        query = urlencode({"valcode": "EUR", "date": day.strftime("%Y%m%d"), "json": ""})
        payload = fetch(NBU_API + "?" + query)
        values = json.loads(payload.decode("utf-8"))
        if values and parse_num(values[0].get("rate")):
            return float(values[0]["rate"]), day.isoformat()
    raise RuntimeError("NBU EUR exchange rate unavailable for the fuel-price date and prior 7 days")

def parse_ua(existing):
    countries = existing.setdefault("countries", {})
    old = countries.get("UA", {})
    try:
        data_date, local_price = parse_ua_page()
        eur_rate, rate_date = get_eur_uah_rate(data_date)
        eur_price = local_price / eur_rate
        history = old.get("history", [])
        history = [point for point in history if point.get("date") != data_date]
        history.append({
            "date": data_date,
            "local_price": round(local_price, 2),
            "eur_per_litre": round(eur_price, 4),
            "currency": "UAH",
        })
        history = sorted(history, key=lambda point: point["date"])[-400:]
        countries["UA"] = {
            "eur_per_litre": round(eur_price, 4),
            "local_price": round(local_price, 2),
            "currency": "UAH",
            "data_date": data_date,
            "fx_rate": round(eur_rate, 6),
            "fx_rate_date": rate_date,
            "source_label": "АЗС24 · середня по Україні",
            "source_url": "https://azs24.com.ua/dp/",
            "history": history,
        }
        print("Ukraine: {} UAH/L, {} (NBU EUR rate {} on {})".format(local_price, data_date, eur_rate, rate_date))
    except Exception as exc:
        print("WARNING: Ukraine source refresh failed: {}".format(exc), file=sys.stderr)
        if old.get("eur_per_litre") is not None:
            old["source_label"] = "АЗС24 · останні доступні дані (оновлення не вдалося)"
        else:
            countries["UA"] = {
                "source_label": "АЗС24 · очікує першого успішного оновлення",
                "source_url": "https://azs24.com.ua/dp/",
                "history": [],
            }

def main():
    existing = load_existing()
    latest_eu = parse_eu(existing)
    parse_ua(existing)
    existing["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing["data_sources"] = {
        "eu": {
            "name": "European Commission Weekly Oil Bulletin (dataset mirror checked against EC history)",
            "url": "https://energy.ec.europa.eu/data-and-analysis/weekly-oil-bulletin_en",
            "dataset_url": "https://www.fuel-prices.eu/research/",
            "licence": "CC BY 4.0",
            "frequency": "weekly",
            "latest_week": latest_eu,
        },
        "ua": {
            "name": "АЗС24, national diesel retail average; EUR conversion by National Bank of Ukraine",
            "url": "https://azs24.com.ua/dp/",
            "licence": "CC BY 4.0 for AZS24 calculated data",
            "frequency": "daily source snapshot, generally business days",
        },
    }
    DATA_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Updated data.json at {}".format(existing["generated_at"]))

if __name__ == "__main__":
    main()
