#!/usr/bin/env python3
"""
LPG Cross-Border Benchmarking Data Generator
=============================================
Pulls daily UN1075 car counts from the RailState API at 11 border sensors,
applies Canada-to-Canada transit removal, converts to BBL using weighted
average, loads CER monthly data, and outputs three JSON files for the
benchmarking dashboard.

Usage:
  python update_lpg_cross_border.py              # standard run (180-day API pull)
  python update_lpg_cross_border.py --days 30    # override to last 30 days

Outputs:
  ../data/lpg_cross_border.json      - monthly RS vs CER comparison
  ../data/lpg_xb_daily.json          - daily car counts and barrels (180 days)
  ../data/lpg_sensor_volumes.json    - per-sensor monthly car counts for map
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

# ============================================================================
# CONFIGURATION
# ============================================================================

API_BASE_URL = "https://api.railstate.com"
FLEET_AVG_BBL_PER_CAR = 700.8
LPG_DENSITY_FACTOR = 0.875  # specific gravity for propane/butane mix
GALLONS_PER_BARREL = 42
RATE_LIMIT_SLEEP = 2.0
TRANSIT_WINDOW_DAYS = 7

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
BORDER_CROSSING_DIR = Path("/Users/dandevoe/Desktop/RailState Project/Claude_Projects/Border Crossing/lpg/output")

# Historical data files
HIST_MONTHLY_CSV = BORDER_CROSSING_DIR / "lpg_railstate_monthly.csv"
HIST_DETAIL_JSON = BORDER_CROSSING_DIR / "lpg_railstate_detail.json"
HIST_CAR_SIZES_JSON = BORDER_CROSSING_DIR / "lpg_car_sizes.json"
HIST_TRANSIT_JSON = BORDER_CROSSING_DIR / "lpg_transit_removal.json"
CER_CSV = BORDER_CROSSING_DIR / "cer_lpg_by_mode.csv"

# ============================================================================
# SENSOR / CROSSING DEFINITIONS
# ============================================================================

# (sensor_display_name, counted_direction, lat, lng, fallback_sensor, fallback_direction)
BORDER_SENSORS = [
    ("Blaine, WA",       "southbound", 48.99, -122.75, None, None),
    ("Port Huron, MI",   "westbound",  42.97,  -82.43, None, None),
    ("Ste Anne, MB",     "eastbound",  49.64,  -96.57, "Devlin, ON", "eastbound"),
    ("Moyie Springs, ID","southbound", 48.73, -116.18, None, None),
    ("Mcara, SK",        "southbound", 49.00, -102.14, None, None),
    ("Kevin, MT",        "southbound", 48.75, -111.98, "Coalhurst, AB", "eastbound"),
    ("Letellier, MB",    "southbound", 49.07,  -97.25, None, None),
    ("Rouses Point, NY", "southbound", 44.99,  -73.37, None, None),
    ("Massena, NY",      "westbound",  44.93,  -74.89, None, None),
    ("Island Pond, VT",  "eastbound",  44.81,  -71.88, None, None),
    ("Windsor TFR, ON",  "westbound",  42.31,  -83.04, "Komoka, ON", "westbound"),
]

# Sensors involved in transit removal (need ALL directions fetched)
TRANSIT_SENSORS = {"Ste Anne, MB", "Devlin, ON", "Port Huron, MI"}


# ============================================================================
# API KEY LOADING
# ============================================================================

def load_api_key() -> str:
    """Load API key from env var or config.env file."""
    key = os.environ.get("RAILSTATE_API_KEY")
    if key:
        return key

    config_path = Path("/Users/dandevoe/Desktop/RailState Project/Claude_Projects/config.env")
    if config_path.exists():
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("RAILSTATE_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
                    if key:
                        return key

    print("ERROR: No API key found. Set RAILSTATE_API_KEY env var or check config.env")
    sys.exit(1)


# ============================================================================
# API CLIENT
# ============================================================================

class RailStateAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = API_BASE_URL
        self._sensor_cache: Dict[str, int] = {}
        self._request_count = 0

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _get(self, url: str, params: dict = None) -> Tuple[dict, Optional[str]]:
        """Make a GET request with rate limiting and error handling."""
        if self._request_count > 0:
            time.sleep(RATE_LIMIT_SLEEP)
        self._request_count += 1

        try:
            resp = requests.get(url, params=params, headers=self._headers(), timeout=120)
            if resp.status_code != 200:
                return {}, f"HTTP {resp.status_code}: {resp.text[:200]}"
            return resp.json(), None
        except requests.exceptions.Timeout:
            return {}, "Request timed out"
        except requests.exceptions.ConnectionError as e:
            return {}, f"Connection error: {e}"
        except Exception as e:
            return {}, f"Unexpected error: {e}"

    def load_sensors(self) -> Dict[str, int]:
        """Fetch sensor name -> sensorId mapping."""
        if self._sensor_cache:
            return self._sensor_cache

        url = urljoin(self.base_url, "/api/v3/sensors/overview")
        data, error = self._get(url)
        if error:
            print(f"  WARNING: Could not load sensors: {error}")
            return {}

        for s in data.get("sensors", []):
            name = s.get("name")
            sid = s.get("sensorId")
            if name and sid:
                self._sensor_cache[name] = sid

        print(f"  Loaded {len(self._sensor_cache)} sensors from API")
        return self._sensor_cache

    def get_sensor_id(self, name: str) -> Optional[int]:
        """Look up sensor ID by name (case-insensitive fallback)."""
        if not self._sensor_cache:
            self.load_sensors()
        if name in self._sensor_cache:
            return self._sensor_cache[name]
        for n, sid in self._sensor_cache.items():
            if n.lower() == name.lower():
                return sid
        return None

    def fetch_sightings(self, sensor_id: int, start: datetime, end: datetime,
                        direction: str = None, label: str = "") -> List[dict]:
        """Fetch all sightings for a sensor in a date range, with pagination."""
        url = urljoin(self.base_url, "/api/v3/trains/full_sightings")
        params = {
            "sensors": str(sensor_id),
            "detection_time_from": start.strftime("%Y-%m-%dT00:00:00Z"),
            "detection_time_to": end.strftime("%Y-%m-%dT23:59:59Z"),
            "response_size": 500,
        }

        all_sightings = []
        page = 0

        while True:
            page += 1
            data, error = self._get(url, params)
            if error:
                print(f"    API error (page {page}): {error}")
                break

            sightings = data.get("sightings", [])
            if not sightings:
                break

            # Filter by direction if specified
            if direction:
                sightings = [
                    s for s in sightings
                    if s.get("direction", "").lower() == direction.lower()
                ]

            all_sightings.extend(sightings)

            next_link = data.get("nextRequestLink")
            if not next_link:
                break

            # For pagination, use the full URL directly
            url = next_link
            params = None

        if label:
            print(f"    {label}: {len(all_sightings)} sightings (pages: {page})")

        return all_sightings


# ============================================================================
# CAR IDENTIFICATION
# ============================================================================

def normalize_car_id(cid) -> str:
    """Normalize car ID for matching."""
    return re.sub(r"\s+", " ", str(cid).strip().upper())


def is_un1075_car(car: dict) -> bool:
    """Check if a car has UN1075 hazmat placard (active, not empty)."""
    hazmats = car.get("hazmats") or []
    for haz in hazmats:
        placard = haz.get("placardType", "")
        if placard == "UN1075":
            return True
    return False


def get_car_bbl(car: dict) -> float:
    """Get BBL capacity for a car. Use actual gallonage if available, else fleet average."""
    try:
        params = car.get("equipmentParameters") or {}
        dims = params.get("dimensions") or {}
        gallons = dims.get("gallonageCapacity")
        if gallons and float(gallons) > 0:
            return float(gallons) * LPG_DENSITY_FACTOR / GALLONS_PER_BARREL
    except (TypeError, ValueError):
        pass
    return FLEET_AVG_BBL_PER_CAR


# ============================================================================
# EXTRACT UN1075 CARS FROM SIGHTINGS
# ============================================================================

def extract_un1075_records(sightings: List[dict]) -> List[dict]:
    """
    Extract individual UN1075 car records from a list of train sightings.
    Returns list of dicts with: car_id, date, bbl, detection_time_utc
    """
    records = []
    for sighting in sightings:
        det_time = sighting.get("detectionTimeUTC", "")
        direction = sighting.get("direction", "")
        cars = sighting.get("cars", [])
        for car in cars:
            if is_un1075_car(car):
                cid = normalize_car_id(car.get("carId", ""))
                bbl = get_car_bbl(car)
                records.append({
                    "car_id": cid,
                    "detection_time_utc": det_time,
                    "date": det_time[:10] if len(det_time) >= 10 else "",
                    "direction": direction,
                    "bbl": bbl,
                })
    return records


# ============================================================================
# TRANSIT REMOVAL
# ============================================================================

def build_transit_removal_set(
    sa_records: List[dict],
    ph_records: List[dict],
) -> Tuple[set, set]:
    """
    Identify Canada-to-Canada transit cars to remove.

    Pattern 1 (EB transit): Car seen EB at Ste Anne/Devlin, then EB at Port Huron
      within 7 days -> remove from Ste Anne EB count.
    Pattern 2 (WB transit): Car seen WB at Port Huron, then WB at Ste Anne/Devlin
      within 7 days -> remove from Port Huron WB count.

    Returns:
      sa_remove: set of (car_id, date) to remove from Ste Anne EB count
      ph_remove: set of (car_id, date) to remove from Port Huron WB count
    """
    # Build lookup: car_id -> list of (datetime, direction, date_str) at each sensor
    def parse_dt(s):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except:
            return None

    sa_by_car = defaultdict(list)
    for r in sa_records:
        dt = parse_dt(r["detection_time_utc"])
        if dt:
            sa_by_car[r["car_id"]].append((dt, r["direction"].lower(), r["date"]))

    ph_by_car = defaultdict(list)
    for r in ph_records:
        dt = parse_dt(r["detection_time_utc"])
        if dt:
            ph_by_car[r["car_id"]].append((dt, r["direction"].lower(), r["date"]))

    sa_remove = set()  # (car_id, date) tuples to remove from Ste Anne EB
    ph_remove = set()  # (car_id, date) tuples to remove from Port Huron WB

    all_car_ids = set(sa_by_car.keys()) | set(ph_by_car.keys())

    for cid in all_car_ids:
        sa_events = sorted(sa_by_car.get(cid, []), key=lambda x: x[0])
        ph_events = sorted(ph_by_car.get(cid, []), key=lambda x: x[0])

        # Pattern 1: EB at SA -> EB at PH within 7 days
        sa_eb = [(dt, d, ds) for dt, d, ds in sa_events if d == "eastbound"]
        ph_eb = [(dt, d, ds) for dt, d, ds in ph_events if d == "eastbound"]

        for sa_dt, _, sa_ds in sa_eb:
            for ph_dt, _, _ in ph_eb:
                delta = (ph_dt - sa_dt).total_seconds() / 86400
                if 0 < delta <= TRANSIT_WINDOW_DAYS:
                    sa_remove.add((cid, sa_ds))
                    break

        # Pattern 2: WB at PH -> WB at SA within 7 days
        ph_wb = [(dt, d, ds) for dt, d, ds in ph_events if d == "westbound"]
        sa_wb = [(dt, d, ds) for dt, d, ds in sa_events if d == "westbound"]

        for ph_dt, _, ph_ds in ph_wb:
            for sa_dt, _, _ in sa_wb:
                delta = (sa_dt - ph_dt).total_seconds() / 86400
                if 0 < delta <= TRANSIT_WINDOW_DAYS:
                    ph_remove.add((cid, ph_ds))
                    break

    return sa_remove, ph_remove


# ============================================================================
# HISTORICAL DATA LOADING
# ============================================================================

def load_historical_monthly_cars() -> Dict[str, Dict[str, int]]:
    """
    Load lpg_railstate_monthly.csv: month -> sensor_name -> car_count.
    Returns dict[month][sensor_display_name] = count.
    """
    result = {}
    if not HIST_MONTHLY_CSV.exists():
        print(f"  Historical monthly CSV not found: {HIST_MONTHLY_CSV}")
        return result

    with open(HIST_MONTHLY_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            month = row.get("month", "").strip()
            if not month:
                continue
            result[month] = {}
            for col, val in row.items():
                if col in ("month", "total", "total_gap_filled"):
                    continue
                # Convert column header back to display name: "Blaine_WA" -> "Blaine, WA"
                sensor_name = col.replace("_", " ", 1)
                # Fix known patterns: "Port Huron MI" -> "Port Huron, MI" etc.
                # The CSV uses underscores for comma+space
                parts = col.rsplit("_", 1)
                if len(parts) == 2:
                    sensor_name = f"{parts[0].replace('_', ' ')}, {parts[1]}"
                else:
                    sensor_name = col.replace("_", " ")
                try:
                    result[month][sensor_name] = int(val) if val else 0
                except ValueError:
                    result[month][sensor_name] = 0

    print(f"  Loaded historical monthly cars: {len(result)} months")
    return result


def load_historical_weighted_bbl() -> Dict[str, float]:
    """Load monthly_weighted_bbl from lpg_car_sizes.json."""
    result = {}
    if not HIST_CAR_SIZES_JSON.exists():
        print(f"  Historical car sizes JSON not found: {HIST_CAR_SIZES_JSON}")
        return result

    with open(HIST_CAR_SIZES_JSON) as f:
        data = json.load(f)

    result = data.get("monthly_weighted_bbl", {})
    print(f"  Loaded historical weighted BBL: {len(result)} months")
    return result


def load_historical_transit() -> Dict[str, int]:
    """Load monthly_transit_remove from lpg_transit_removal.json."""
    result = {}
    if not HIST_TRANSIT_JSON.exists():
        print(f"  Historical transit JSON not found: {HIST_TRANSIT_JSON}")
        return result

    with open(HIST_TRANSIT_JSON) as f:
        data = json.load(f)

    result = {k: int(v) for k, v in data.get("monthly_transit_remove", {}).items()}
    print(f"  Loaded historical transit removal: {len(result)} months")
    return result


def load_cer_rail_bbl() -> Dict[str, float]:
    """
    Load CER railway data (Propane + Butanes railway exports) by month.
    Returns month -> total railway BBL.
    """
    result = defaultdict(float)
    if not CER_CSV.exists():
        print(f"  CER CSV not found: {CER_CSV}")
        return dict(result)

    with open(CER_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            commodity = row.get("commodity", "").strip()
            mode = row.get("mode", "").strip()
            period = row.get("period", "").strip()
            vol_bbl = row.get("volume_bbl", "").strip()

            if mode != "Railway":
                continue
            if commodity not in ("Propane", "Butanes"):
                continue
            if not period or not vol_bbl:
                continue

            try:
                result[period] += float(vol_bbl)
            except ValueError:
                pass

    print(f"  Loaded CER railway BBL: {len(result)} months")
    return dict(result)


# ============================================================================
# DAILY DATA FETCHING (API)
# ============================================================================

def fetch_daily_data(api: RailStateAPI, days: int = 180) -> Tuple[
    Dict[str, Dict[str, List[dict]]],  # sensor_name -> direction -> records
    set, set  # sa_remove, ph_remove
]:
    """
    Fetch last N days of data from the API for all border sensors.

    Returns:
      sensor_records: sensor_name -> {counted_direction: [records], all: [records]}
      sa_remove, ph_remove: transit removal sets
    """
    end_dt = datetime.utcnow().replace(hour=23, minute=59, second=59)
    start_dt = (end_dt - timedelta(days=days)).replace(hour=0, minute=0, second=0)

    print(f"\nFetch window: {start_dt.date()} to {end_dt.date()} ({days} days)")

    api.load_sensors()

    # We need ALL-direction data for transit sensors, counted-direction for the rest
    sensor_records: Dict[str, Dict[str, List[dict]]] = {}

    # Also collect all records for transit sensors (all directions)
    transit_all_records: Dict[str, List[dict]] = defaultdict(list)

    for sensor_name, direction, lat, lng, fallback, fb_dir in BORDER_SENSORS:
        print(f"\n  {sensor_name} ({direction}):")

        sid = api.get_sensor_id(sensor_name)
        used_fallback = False

        if sid is None and fallback:
            print(f"    Primary sensor not found, trying fallback: {fallback}")
            sid = api.get_sensor_id(fallback)
            if sid:
                used_fallback = True
                direction = fb_dir or direction

        if sid is None:
            print(f"    SKIP: Sensor ID not found for {sensor_name}")
            sensor_records[sensor_name] = {"counted": [], "all": []}
            continue

        actual_name = fallback if used_fallback else sensor_name

        # For transit sensors, fetch ALL directions first
        if sensor_name in TRANSIT_SENSORS or (fallback and fallback in TRANSIT_SENSORS):
            sightings_all = api.fetch_sightings(
                sid, start_dt, end_dt, direction=None,
                label=f"{actual_name} (all directions)"
            )
            all_recs = extract_un1075_records(sightings_all)
            transit_all_records[sensor_name].extend(all_recs)

            # Filter for counted direction
            counted_recs = [r for r in all_recs if r["direction"].lower() == direction.lower()]
            print(f"    Counted ({direction}): {len(counted_recs)} UN1075 cars")
        else:
            sightings = api.fetch_sightings(
                sid, start_dt, end_dt, direction=direction,
                label=f"{actual_name} ({direction})"
            )
            counted_recs = extract_un1075_records(sightings)
            all_recs = counted_recs
            print(f"    UN1075 cars: {len(counted_recs)}")

        sensor_records[sensor_name] = {
            "counted": counted_recs,
            "all": all_recs,
        }

    # Also fetch Devlin, ON if Ste Anne didn't have it as primary
    # (Devlin is a fallback for Ste Anne and also a transit sensor)
    devlin_sid = api.get_sensor_id("Devlin, ON")
    if devlin_sid and "Devlin, ON" not in [s[0] for s in BORDER_SENSORS]:
        print(f"\n  Devlin, ON (transit supplement):")
        sightings_all = api.fetch_sightings(
            devlin_sid, start_dt, end_dt, direction=None,
            label="Devlin, ON (all directions)"
        )
        devlin_recs = extract_un1075_records(sightings_all)
        # Add Devlin records to Ste Anne transit pool
        transit_all_records["Ste Anne, MB"].extend(devlin_recs)
        print(f"    Added {len(devlin_recs)} records to Ste Anne transit pool")

    # Build transit removal sets
    print("\n  Building transit removal sets...")
    sa_all = transit_all_records.get("Ste Anne, MB", [])
    ph_all = sensor_records.get("Port Huron, MI", {}).get("all", [])
    # Also add Port Huron transit records if fetched separately
    if "Port Huron, MI" in transit_all_records:
        ph_all = transit_all_records["Port Huron, MI"]

    sa_remove, ph_remove = build_transit_removal_set(sa_all, ph_all)
    print(f"    Ste Anne EB transit cars to remove: {len(sa_remove)}")
    print(f"    Port Huron WB transit cars to remove: {len(ph_remove)}")

    return sensor_records, sa_remove, ph_remove


# ============================================================================
# AGGREGATE DAILY DATA
# ============================================================================

def aggregate_daily(
    sensor_records: Dict[str, Dict[str, List[dict]]],
    sa_remove: set,
    ph_remove: set,
    days: int = 180,
) -> Tuple[List[str], List[int], List[int], Dict[str, Dict[str, int]]]:
    """
    Aggregate daily totals across all sensors, applying transit removal.

    Returns:
      dates: sorted list of date strings
      total_cars: car count per day (after transit removal)
      total_barrels: weighted BBL per day (after transit removal)
      sensor_monthly: sensor_name -> month -> car_count (for sensor volumes)
    """
    daily_cars = defaultdict(int)
    daily_bbl = defaultdict(float)
    sensor_monthly = defaultdict(lambda: defaultdict(int))

    for sensor_name, _, _, _, _, _ in BORDER_SENSORS:
        data = sensor_records.get(sensor_name, {})
        counted = data.get("counted", [])

        for rec in counted:
            date_str = rec["date"]
            car_id = rec["car_id"]
            bbl = rec["bbl"]

            # Apply transit removal
            if sensor_name == "Ste Anne, MB" and (car_id, date_str) in sa_remove:
                continue
            if sensor_name == "Port Huron, MI" and (car_id, date_str) in ph_remove:
                continue

            daily_cars[date_str] += 1
            daily_bbl[date_str] += bbl

            month = date_str[:7]
            sensor_monthly[sensor_name][month] += 1

    # Sort by date
    all_dates = sorted(daily_cars.keys())

    dates = all_dates
    total_cars = [daily_cars[d] for d in dates]
    total_barrels = [round(daily_bbl[d]) for d in dates]

    return dates, total_cars, total_barrels, dict(sensor_monthly)


# ============================================================================
# BUILD MONTHLY COMPARISON (HISTORICAL + RECENT)
# ============================================================================

def load_existing_output() -> Tuple[Dict[str, list], Dict[str, Dict[str, Dict[str, int]]]]:
    """
    Load existing output JSON files to preserve historical data when running in CI
    (where the original historical source files are not available).

    Returns:
      existing_monthly: month -> [month, rs_bbl_k, cer_bbl_k] from lpg_cross_border.json
      existing_sensor_monthly: sensor_name -> {month: count} from lpg_sensor_volumes.json
    """
    existing_monthly = {}
    existing_sensor_monthly = {}

    # Load existing lpg_cross_border.json
    cross_border_path = DATA_DIR / "lpg_cross_border.json"
    if cross_border_path.exists():
        try:
            with open(cross_border_path) as f:
                data = json.load(f)
            for row in data.get("monthly", {}).get("rows", []):
                if len(row) >= 3:
                    existing_monthly[row[0]] = row
            print(f"  Loaded existing cross-border output: {len(existing_monthly)} months")
        except Exception as e:
            print(f"  Warning reading existing cross-border output: {e}")

    # Load existing lpg_sensor_volumes.json
    sensor_path = DATA_DIR / "lpg_sensor_volumes.json"
    if sensor_path.exists():
        try:
            with open(sensor_path) as f:
                data = json.load(f)
            for sensor in data.get("sensors", []):
                name = sensor.get("name", "")
                monthly = sensor.get("monthly", {})
                if name and monthly:
                    existing_sensor_monthly[name] = {k: int(v) for k, v in monthly.items()}
            print(f"  Loaded existing sensor output: {len(existing_sensor_monthly)} sensors")
        except Exception as e:
            print(f"  Warning reading existing sensor output: {e}")

    return existing_monthly, existing_sensor_monthly


def build_monthly_rows(
    hist_monthly_cars: Dict[str, Dict[str, int]],
    hist_weighted_bbl: Dict[str, float],
    hist_transit: Dict[str, int],
    cer_rail_bbl: Dict[str, float],
    api_sensor_monthly: Dict[str, Dict[str, int]],
    api_daily_bbl: Dict[str, float],
    api_sa_remove_monthly: Dict[str, int],
    api_ph_remove_monthly: Dict[str, int],
    existing_monthly: Dict[str, list] = None,
) -> List[list]:
    """
    Build monthly rows from 2024-03 through present.
    Uses historical files for months before the API window, and API data for recent months.
    Falls back to existing output data when historical source files are not available (CI).
    """
    if existing_monthly is None:
        existing_monthly = {}

    # Determine month range
    start_month = "2024-03"
    now = datetime.utcnow()
    end_month = now.strftime("%Y-%m")

    # Generate all months
    months = []
    y, m = int(start_month[:4]), int(start_month[5:7])
    while f"{y:04d}-{m:02d}" <= end_month:
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1

    rows = []
    for month in months:
        # Try to get RS BBL for this month
        rs_bbl = None

        # Check if we have API data for this month
        api_bbl_for_month = api_daily_bbl.get(month)

        if month in hist_weighted_bbl:
            # Use historical weighted BBL — never overwrite with API data
            raw_bbl = hist_weighted_bbl[month]
            transit_cars = hist_transit.get(month, 0)
            # The historical weighted BBL includes transit cars, so adjust:
            # Get total cars for the month from historical CSV
            total_cars_month = sum(hist_monthly_cars.get(month, {}).values())
            if total_cars_month > 0:
                # Reduce BBL proportionally based on transit removal
                adjusted_cars = total_cars_month - transit_cars
                if adjusted_cars > 0:
                    rs_bbl = raw_bbl * (adjusted_cars / total_cars_month)
                else:
                    rs_bbl = 0
            else:
                rs_bbl = raw_bbl

            # Convert to thousands, round to integer
            rs_bbl_k = round(rs_bbl / 1000) if rs_bbl is not None else None

            # CER data from source file
            cer_val = cer_rail_bbl.get(month)
            cer_bbl_k = round(cer_val / 1000) if cer_val is not None else None

            rows.append([month, rs_bbl_k, cer_bbl_k])

        elif month in existing_monthly and existing_monthly[month][1] is not None:
            # Preserve existing output data — this is curated historical data
            # Only update if API has data for this month AND existing has no RS value
            existing_row = existing_monthly[month]
            rows.append(list(existing_row))

        elif api_bbl_for_month is not None:
            # Only use API data for months not covered by historical or existing data
            rs_bbl = api_bbl_for_month
            rs_bbl_k = round(rs_bbl / 1000) if rs_bbl is not None else None

            # CER data
            cer_val = cer_rail_bbl.get(month)
            cer_bbl_k = round(cer_val / 1000) if cer_val is not None else None

            # Check if existing had CER data we should preserve
            if cer_bbl_k is None and month in existing_monthly and existing_monthly[month][2] is not None:
                cer_bbl_k = existing_monthly[month][2]

            rows.append([month, rs_bbl_k, cer_bbl_k])

        else:
            # No data from any source — check existing for CER-only rows
            if month in existing_monthly:
                rows.append(list(existing_monthly[month]))
            else:
                # CER data only
                cer_val = cer_rail_bbl.get(month)
                cer_bbl_k = round(cer_val / 1000) if cer_val is not None else None
                rows.append([month, None, cer_bbl_k])

    return rows


# ============================================================================
# BUILD SENSOR VOLUMES FOR MAP
# ============================================================================

def build_sensor_volumes(
    hist_monthly_cars: Dict[str, Dict[str, int]],
    hist_transit: Dict[str, int],
    api_sensor_monthly: Dict[str, Dict[str, int]],
    existing_sensor_monthly: Dict[str, Dict[str, int]] = None,
) -> List[dict]:
    """Build per-sensor monthly car counts for map visualization."""
    if existing_sensor_monthly is None:
        existing_sensor_monthly = {}

    sensors_out = []

    for sensor_name, direction, lat, lng, _, _ in BORDER_SENSORS:
        monthly = {}

        # Start with existing output data (preserves historical values from prior runs)
        if sensor_name in existing_sensor_monthly:
            monthly.update(existing_sensor_monthly[sensor_name])

        # Historical source files override existing output (more authoritative)
        for month, sensors in hist_monthly_cars.items():
            count = sensors.get(sensor_name, 0)
            if count > 0:
                monthly[month] = count

        # API months — only add months not already covered
        api_months = api_sensor_monthly.get(sensor_name, {})
        for month, count in api_months.items():
            if month not in monthly:
                monthly[month] = count

        sensors_out.append({
            "name": sensor_name,
            "lat": lat,
            "lng": lng,
            "direction": direction,
            "monthly": dict(sorted(monthly.items())),
        })

    return sensors_out


# ============================================================================
# OUTPUT
# ============================================================================

def save_json(data: dict, path: Path, description: str):
    """Save JSON with pretty-printing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Saved {description}: {path.name}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="LPG Cross-Border Benchmarking Data Generator")
    parser.add_argument("--days", type=int, default=180,
                        help="Number of days to fetch from API (default: 180)")
    args = parser.parse_args()

    print("=" * 70)
    print("LPG CROSS-BORDER BENCHMARKING DATA GENERATOR")
    print(f"Run time: {datetime.utcnow().isoformat()}Z")
    print("=" * 70)

    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    # ---- Load API key ----
    api_key = load_api_key()
    print(f"API key loaded (length: {len(api_key)})")

    # ---- Load historical data ----
    print("\n--- Loading Historical Data ---")
    hist_monthly_cars = load_historical_monthly_cars()
    hist_weighted_bbl = load_historical_weighted_bbl()
    hist_transit = load_historical_transit()
    cer_rail_bbl = load_cer_rail_bbl()

    # ---- Fetch daily data from API ----
    print("\n--- Fetching API Data ---")
    api = RailStateAPI(api_key)

    sensor_records, sa_remove, ph_remove = fetch_daily_data(api, days=args.days)

    # ---- Aggregate daily data ----
    print("\n--- Aggregating Daily Data ---")
    dates, total_cars, total_barrels, api_sensor_monthly = aggregate_daily(
        sensor_records, sa_remove, ph_remove, days=args.days
    )
    print(f"  Daily data: {len(dates)} days")
    if dates:
        print(f"  Date range: {dates[0]} to {dates[-1]}")
        print(f"  Total cars: {sum(total_cars):,}")
        print(f"  Total BBL: {sum(total_barrels):,}")

    # ---- Compute API monthly BBL (for months within API window) ----
    # Aggregate daily BBL by month for overlap with historical
    api_monthly_bbl: Dict[str, float] = defaultdict(float)
    for d, bbl in zip(dates, total_barrels):
        month = d[:7]
        api_monthly_bbl[month] += bbl

    # Compute transit removal counts per month from API data
    api_sa_remove_monthly: Dict[str, int] = defaultdict(int)
    api_ph_remove_monthly: Dict[str, int] = defaultdict(int)
    for car_id, date_str in sa_remove:
        api_sa_remove_monthly[date_str[:7]] += 1
    for car_id, date_str in ph_remove:
        api_ph_remove_monthly[date_str[:7]] += 1

    # ---- Load existing output (fallback for CI where historical source files don't exist) ----
    print("\n--- Loading Existing Output (for historical preservation) ---")
    existing_monthly, existing_sensor_monthly = load_existing_output()

    # ---- Build monthly comparison rows ----
    print("\n--- Building Monthly Comparison ---")
    monthly_rows = build_monthly_rows(
        hist_monthly_cars, hist_weighted_bbl, hist_transit, cer_rail_bbl,
        api_sensor_monthly, dict(api_monthly_bbl),
        dict(api_sa_remove_monthly), dict(api_ph_remove_monthly),
        existing_monthly,
    )
    print(f"  Monthly rows: {len(monthly_rows)} months")

    # Print sample
    print(f"\n  {'Month':<10} {'RS (k BBL)':>12} {'CER (k BBL)':>12}")
    print(f"  {'-'*10} {'-'*12} {'-'*12}")
    for row in monthly_rows[-6:]:
        rs = f"{row[1]:,}" if row[1] is not None else "n/a"
        cer = f"{row[2]:,}" if row[2] is not None else "n/a"
        print(f"  {row[0]:<10} {rs:>12} {cer:>12}")

    # ---- Build sensor volumes ----
    print("\n--- Building Sensor Volumes ---")
    sensor_volumes = build_sensor_volumes(
        hist_monthly_cars, hist_transit, api_sensor_monthly, existing_sensor_monthly
    )
    print(f"  Sensors: {len(sensor_volumes)}")

    # ---- Save output files ----
    print("\n--- Saving Output Files ---")

    # Output 1: lpg_cross_border.json
    cross_border_json = {
        "commodity": "lpg_cross_border",
        "display_name": "LPG Cross-Border",
        "subtitle": "Canada \u2192 US \u00b7 Propane & Butane \u00b7 Rail Exports",
        "unit": "barrels (k)",
        "gov_source_label": "CER",
        "gov_lag_label": "8\u201310 wks",
        "last_updated": today_str,
        "monthly": {
            "description": (
                "Monthly totals in thousands of BBL. railstate = weighted BBL "
                "from sensor data after transit removal. gov = CER Railway export volumes."
            ),
            "columns": ["month", "railstate", "gov"],
            "rows": monthly_rows,
        },
    }
    save_json(cross_border_json, DATA_DIR / "lpg_cross_border.json", "monthly comparison")

    # Output 2: lpg_xb_daily.json
    daily_json = {
        "description": "LPG cross-border daily volumes",
        "last_updated": today_str,
        "dates": dates,
        "total_cars": total_cars,
        "total_barrels": total_barrels,
    }
    save_json(daily_json, DATA_DIR / "lpg_xb_daily.json", "daily volumes")

    # Output 3: lpg_sensor_volumes.json
    sensor_json = {
        "description": "LPG cross-border volumes by sensor location for map visualization",
        "last_updated": today_str,
        "sensors": sensor_volumes,
    }
    save_json(sensor_json, DATA_DIR / "lpg_sensor_volumes.json", "sensor volumes")

    # ---- Summary ----
    print("\n" + "=" * 70)
    print("COMPLETE")
    print("=" * 70)
    print(f"  Output directory: {DATA_DIR}")
    print(f"  Files written:")
    print(f"    - lpg_cross_border.json  (monthly RS vs CER, {len(monthly_rows)} months)")
    print(f"    - lpg_xb_daily.json      (daily, {len(dates)} days)")
    print(f"    - lpg_sensor_volumes.json ({len(sensor_volumes)} sensors)")

    if dates:
        # Show last 7 days
        print(f"\n  Last 7 days:")
        print(f"  {'Date':<12} {'Cars':>6} {'BBL':>8}")
        for i in range(max(0, len(dates) - 7), len(dates)):
            print(f"  {dates[i]:<12} {total_cars[i]:>6,} {total_barrels[i]:>8,}")

    print()


if __name__ == "__main__":
    main()
