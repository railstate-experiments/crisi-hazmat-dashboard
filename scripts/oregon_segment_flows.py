#!/usr/bin/env python3
"""
Oregon Segment-Level Hazmat Flow Cube
=====================================
Builds a segment x commodity x month cube of hazmat car flows for the CRISI
"Hazardous Material Flows" map. Unlike the old per-sensor bubbles, this
attributes each hazmat car to the TRACK SEGMENTS it traversed, using
linked car-id matching: a car's sightings are ordered in time, and each
consecutive leg between two sensors on the same corridor is counted as a
traversal of every segment along that leg.

Source: railstate-warehouse DuckDB (read-only).
Window: calendar year 2025 (to match the existing dashboard).

Output: ../data/oregon_segment_flows.json
  meta          - generation info, window, methodology, match coverage
  months        - 12 month labels
  commodities   - filter catalog: [{un, name, hazard_class, group, color, total}]
  classes       - hazard-class rollup catalog: [{group, color, total}]
  segments      - [{id, sensor_a, sensor_b, subdivision, color,
                    coords: [[lng,lat],[lng,lat]],   # straight fallback; real geom added by geojson step
                    total, monthly[12],
                    by_commodity: {UN####: {total, monthly[12], directions:{}}},
                    by_class:     {group:  {total, monthly[12]}} }]
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb

# Reuse the curated lookups from the existing hazmat analysis script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from oregon_hazmat_analysis import (
    UN_LOOKUP,
    HAZARD_CLASS_COLORS,
    SENSOR_SUBDIVISION,
    get_hazard_class,
    get_class_group,
)

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
OUTPUT = DATA_DIR / "oregon_segment_flows.json"
WAREHOUSE = Path("/Users/dandevoe/Developer/railstate-warehouse/railstate.duckdb")

# ── Window ──
START = "2025-01-01"
END = "2026-01-01"  # exclusive

# ── Linking parameter ──
# Max time between two consecutive sightings for them to count as one leg.
# Adjacent Oregon sensors are hours-to-a-day apart; >5 days means the car
# left the corridor and came back (separate trip), so don't bridge it.
MAX_LEG_DAYS = 5

# ── Oregon sensors: warehouse id -> name ──
ORE_SENSORS = {
    268: "Bend, OR", 266: "Cold Springs, OR", 269: "Echo, OR", 264: "Eugene, OR",
    263: "Haig, OR", 265: "Irving, OR", 261: "Jefferson, OR", 259: "Modoc Point, OR",
    270: "N. Portland E, OR", 402: "N. Portland W, OR", 271: "Ontario, OR",
    258: "Salem, OR", 262: "Springfield Jct, OR", 267: "Troutdale, OR", 256: "Worden, OR",
}

# ── Corridors: ordered sensor sequences (segments = consecutive pairs) ──
CORRIDORS = {
    "UP Brooklyn Sub":      {"sensors": ["Haig, OR", "Salem, OR", "Jefferson, OR", "Irving, OR", "Eugene, OR", "Springfield Jct, OR"], "color": "#fbcb0a"},
    "UP Cascade Sub":       {"sensors": ["Springfield Jct, OR", "Modoc Point, OR", "Worden, OR"], "color": "#f59e0b"},
    "UP La Grande Sub":     {"sensors": ["Troutdale, OR", "Echo, OR", "Cold Springs, OR"], "color": "#e05d00"},
    "UP Nampa Sub":         {"sensors": ["Cold Springs, OR", "Ontario, OR"], "color": "#dc2626"},
    "UP Graham Line":       {"sensors": ["Haig, OR", "Troutdale, OR"], "color": "#0284c7"},
    "BNSF Fallbridge Sub":  {"sensors": ["N. Portland W, OR", "N. Portland E, OR"], "color": "#c75b12"},
    "BNSF Oregon Trunk Sub":{"sensors": ["N. Portland E, OR", "Bend, OR"], "color": "#7c3aed"},
}

TOP_COMMODITIES = 25  # individually-selectable; rest folded into "Other"


def seg_key(a, b):
    """Undirected segment id (sorted), stable for lookups."""
    return " || ".join(sorted([a, b]))


def build_segment_catalog():
    """All segments from corridor adjacency + a per-corridor sensor->index map."""
    segments = {}          # seg_key -> {sensor_a, sensor_b, subdivision, color}
    corridor_index = {}    # subdivision -> {sensor_name: position}
    for sub, cfg in CORRIDORS.items():
        seq = cfg["sensors"]
        corridor_index[sub] = {s: i for i, s in enumerate(seq)}
        for i in range(len(seq) - 1):
            a, b = seq[i], seq[i + 1]
            k = seg_key(a, b)
            if k not in segments:
                segments[k] = {
                    "id": k,
                    "sensor_a": a,
                    "sensor_b": b,
                    "subdivision": sub,
                    "color": cfg["color"],
                }
    return segments, corridor_index


def leg_segments(sub_index, a, b):
    """Segments traversed going from sensor a to sensor b along a corridor.
    Returns list of (seg_key, intermediate_sensor_pairs) or [] if not co-corridor."""
    for sub, idx in sub_index.items():
        if a in idx and b in idx:
            ia, ib = idx[a], idx[b]
            seq = CORRIDORS[sub]["sensors"]
            lo, hi = (ia, ib) if ia < ib else (ib, ia)
            return [seg_key(seq[i], seq[i + 1]) for i in range(lo, hi)]
    return []


def main():
    if not WAREHOUSE.exists():
        sys.exit(f"Warehouse not found: {WAREHOUSE}")

    segments, corridor_index = build_segment_catalog()
    print(f"Corridors: {len(CORRIDORS)} | unique segments: {len(segments)}")

    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    ids = tuple(ORE_SENSORS.keys())

    # Pull every 2025 Oregon hazmat car detection with a car id.
    # One row per (car, sensor sighting, placard). A car can carry >1 placard;
    # we attribute each placard independently.
    print("Querying warehouse (2025 Oregon hazmat car sightings)...")
    rows = con.execute(f"""
        SELECT c.carId            AS car_id,
               s.sensorId         AS sensor_id,
               s.detectionTimeUTC AS t,
               s.direction        AS direction,
               h.placardType      AS un
        FROM sightings s
        JOIN car_hazmats h ON h.sightingId = s.sightingId
        JOIN cars c        ON c.sightingId = h.sightingId AND c.carPosition = h.carPosition
        WHERE s.sensorId IN {ids}
          AND s.detectionTimeUTC >= TIMESTAMP '{START}'
          AND s.detectionTimeUTC <  TIMESTAMP '{END}'
          AND h.placardType LIKE 'UN%'
          AND c.carId IS NOT NULL AND c.carId <> ''
        ORDER BY c.carId, s.detectionTimeUTC
    """).fetchall()
    print(f"  fetched {len(rows):,} hazmat car-sighting-placard rows")

    # Group by (car_id, un) so each physical car+commodity is one trackable unit.
    # (A car keeps the same placard across the corridor; pairing per-commodity
    #  avoids cross-linking two different placards on the same car.)
    by_unit = defaultdict(list)  # (car_id, un) -> [(t, sensor_name, direction)]
    for car_id, sensor_id, t, direction, un in rows:
        sname = ORE_SENSORS.get(sensor_id)
        if sname:
            by_unit[(car_id, un)].append((t, sname, direction))

    # Accumulators
    def fresh_seg():
        return {
            "total": 0,
            "monthly": [0] * 12,
            "by_commodity": defaultdict(lambda: {"total": 0, "monthly": [0] * 12,
                                                 "directions": defaultdict(int)}),
            "by_class": defaultdict(lambda: {"total": 0, "monthly": [0] * 12}),
        }

    seg_data = {k: fresh_seg() for k in segments}
    commodity_total = defaultdict(int)
    class_total = defaultdict(int)

    legs_matched = 0
    legs_skipped_gap = 0
    legs_skipped_nocorridor = 0
    units_with_traversal = 0
    max_gap = MAX_LEG_DAYS * 86400

    for (car_id, un), obs in by_unit.items():
        if len(obs) < 2:
            continue
        obs.sort(key=lambda r: r[0])
        group = get_class_group(get_hazard_class(un))
        matched_any = False
        for (t0, s0, dir0), (t1, s1, _d1) in zip(obs, obs[1:]):
            if s0 == s1:
                continue
            gap = (t1 - t0).total_seconds()
            if gap < 0 or gap > max_gap:
                legs_skipped_gap += 1
                continue
            segs = leg_segments(corridor_index, s0, s1)
            if not segs:
                legs_skipped_nocorridor += 1
                continue
            month = t0.month - 1
            direction = dir0 or "Unknown"
            for k in segs:
                sd = seg_data[k]
                sd["total"] += 1
                sd["monthly"][month] += 1
                c = sd["by_commodity"][un]
                c["total"] += 1
                c["monthly"][month] += 1
                c["directions"][direction] += 1
                cl = sd["by_class"][group]
                cl["total"] += 1
                cl["monthly"][month] += 1
                commodity_total[un] += 1
                class_total[group] += 1
                legs_matched += 1
            matched_any = True
        if matched_any:
            units_with_traversal += 1

    # ── Commodity catalog (top N individually, rest -> Other) ──
    ranked = sorted(commodity_total.items(), key=lambda kv: kv[1], reverse=True)
    top_uns = {un for un, _ in ranked[:TOP_COMMODITIES]}
    commodities = []
    for un, tot in ranked:
        name, hclass = UN_LOOKUP.get(un, (un, get_hazard_class(un)))
        group = get_class_group(hclass)
        commodities.append({
            "un": un,
            "name": name,
            "hazard_class": hclass,
            "group": group,
            "color": HAZARD_CLASS_COLORS.get(group, "#888"),
            "total": tot,
            "top": un in top_uns,
        })

    classes = [
        {"group": g, "color": HAZARD_CLASS_COLORS.get(g, "#888"), "total": t}
        for g, t in sorted(class_total.items(), key=lambda kv: kv[1], reverse=True)
    ]

    # ── Serialize segments ──
    out_segments = []
    for k, meta in segments.items():
        sd = seg_data[k]
        out_segments.append({
            **meta,
            "total": sd["total"],
            "monthly": sd["monthly"],
            "by_commodity": {
                un: {"total": v["total"], "monthly": v["monthly"],
                     "directions": dict(v["directions"])}
                for un, v in sd["by_commodity"].items()
            },
            "by_class": {g: {"total": v["total"], "monthly": v["monthly"]}
                         for g, v in sd["by_class"].items()},
        })
    out_segments.sort(key=lambda s: s["total"], reverse=True)

    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    payload = {
        "meta": {
            "generated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window": {"start": START, "end": "2025-12-31"},
            "source": "railstate-warehouse (DuckDB)",
            "methodology": (
                "Linked car-id traversal: each hazmat car's sightings are ordered in "
                "time; every consecutive leg between two sensors on the same corridor "
                f"(<= {MAX_LEG_DAYS} days apart) counts as one traversal of each segment "
                "along that leg. Counts are car-traversals, attributed per commodity."
            ),
            "coverage": {
                "car_sighting_placard_rows": len(rows),
                "car_commodity_units": len(by_unit),
                "units_with_traversal": units_with_traversal,
                "legs_matched": legs_matched,
                "legs_skipped_time_gap": legs_skipped_gap,
                "legs_skipped_no_shared_corridor": legs_skipped_nocorridor,
            },
        },
        "months": month_labels,
        "commodities": commodities,
        "classes": classes,
        "segments": out_segments,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"\nWrote {OUTPUT.name}")
    print(f"  segments: {len(out_segments)} | commodities: {len(commodities)} | classes: {len(classes)}")
    print(f"  legs matched: {legs_matched:,} | skipped (gap): {legs_skipped_gap:,} | "
          f"skipped (no corridor): {legs_skipped_nocorridor:,}")
    print(f"  units with >=1 traversal: {units_with_traversal:,} / {len(by_unit):,}")


if __name__ == "__main__":
    main()
