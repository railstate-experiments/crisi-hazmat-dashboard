#!/usr/bin/env python3
"""
Oregon Hazmat Flow Geometry
===========================
Turns the segment flow cube (oregon_segment_flows.json) into a map-ready
GeoJSON where each segment LineString follows the ACTUAL rail geometry
(not a straight A->B line).

Method: build a graph from the railroad's real linework (na_rail_network.json)
cropped to an Oregon bounding box, snap each sensor to the nearest graph node,
and route the shortest path along the track between the two sensors. The
resulting polyline is the segment geometry. All flow-cube properties (total,
monthly, by_commodity, by_class) are merged into the feature so the dashboard
can filter and trend from a single file.

Output: ../data/oregon_hazmat_flow_geo.json  (GeoJSON FeatureCollection)
"""

import json
import math
import sys
from pathlib import Path

import networkx as nx
import duckdb

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
NETWORK = DATA_DIR / "na_rail_network.json"
CUBE = DATA_DIR / "oregon_segment_flows.json"
OUTPUT = DATA_DIR / "oregon_hazmat_flow_geo.json"
WAREHOUSE = Path("/Users/dandevoe/Developer/railstate-warehouse/railstate.duckdb")

# Oregon bounding box (generous) to crop the continental linework
BBOX = (-125.0, -116.0, 41.0, 47.0)  # lng0, lng1, lat0, lat1
SNAP_DECIMALS = 5  # ~1.1m node-identity rounding to bridge coincident vertices

ORE_IDS = (268, 266, 269, 264, 263, 265, 261, 259, 270, 402, 271, 258, 262, 267, 256)


def haversine(a, b):
    """Approx meters between [lng,lat] points."""
    lon1, lat1, lon2, lat2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def in_box(p):
    return BBOX[0] <= p[0] <= BBOX[1] and BBOX[2] <= p[1] <= BBOX[3]


def node_key(p):
    return (round(p[0], SNAP_DECIMALS), round(p[1], SNAP_DECIMALS))


def build_graph(multiline_coords):
    """Graph over vertices of LineStrings that touch the bbox."""
    G = nx.Graph()
    for line in multiline_coords:
        if not any(in_box(p) for p in line):
            continue
        prev = None
        for p in line:
            k = node_key(p)
            if k not in G:
                G.add_node(k, xy=[p[0], p[1]])
            if prev is not None and prev != k:
                w = haversine(G.nodes[prev]["xy"], G.nodes[k]["xy"])
                if not G.has_edge(prev, k):
                    G.add_edge(prev, k, weight=w)
            prev = k
    return G


def nearest_node(G, pt):
    best, bestd = None, float("inf")
    for n, data in G.nodes(data=True):
        d = haversine(data["xy"], pt)
        if d < bestd:
            bestd, best = d, n
    return best, bestd


def main():
    cube = json.loads(CUBE.read_text())
    net = json.loads(NETWORK.read_text())

    # Railroad linework
    rr_lines = {}
    for f in net["features"]:
        rr = f["properties"].get("rr")
        if rr in ("UP", "BNSF"):
            g = f["geometry"]
            rr_lines[rr] = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]

    graphs = {}
    for rr, lines in rr_lines.items():
        G = build_graph(lines)
        graphs[rr] = G
        print(f"  {rr} graph: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")

    # Sensor coordinates from the warehouse (authoritative)
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    coords = {name: [lng, lat] for name, lng, lat in
              con.execute(f"SELECT name, lng, lat FROM sensors WHERE sensorId IN {ORE_IDS}").fetchall()}
    con.close()

    features = []
    real_count = straight_count = 0

    for seg in cube["segments"]:
        a, b = seg["sensor_a"], seg["sensor_b"]
        ca, cb = coords.get(a), coords.get(b)
        rr = "BNSF" if seg["subdivision"].startswith("BNSF") else "UP"
        G = graphs.get(rr)

        path_coords = None
        if G is not None and ca and cb:
            na, da = nearest_node(G, ca)
            nb, db = nearest_node(G, cb)
            if na and nb and na != nb:
                try:
                    nodes = nx.shortest_path(G, na, nb, weight="weight")
                    path_coords = [G.nodes[n]["xy"] for n in nodes]
                    # prepend/append the true sensor points so the line touches the sensors
                    if path_coords[0] != ca:
                        path_coords = [ca] + path_coords
                    if path_coords[-1] != cb:
                        path_coords = path_coords + [cb]
                except nx.NetworkXNoPath:
                    path_coords = None

        if path_coords and len(path_coords) >= 2:
            real_count += 1
            geom_kind = "routed"
        else:
            path_coords = [ca, cb] if (ca and cb) else None
            straight_count += 1
            geom_kind = "straight"
        if not path_coords:
            continue

        props = {k: seg[k] for k in ("id", "sensor_a", "sensor_b", "subdivision",
                                     "color", "total", "monthly", "by_commodity", "by_class")}
        props["geom_kind"] = geom_kind
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": path_coords},
            "properties": props,
        })

    # Sensor point features (for labels/markers)
    for name, c in coords.items():
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": c},
            "properties": {"type": "sensor", "name": name},
        })

    out = {
        "type": "FeatureCollection",
        "meta": {**cube["meta"],
                 "geometry_source": "na_rail_network.json routed via shortest-path",
                 "segments_routed": real_count, "segments_straight": straight_count},
        "months": cube["months"],
        "commodities": cube["commodities"],
        "classes": cube["classes"],
        "features": features,
    }
    OUTPUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"\nWrote {OUTPUT.name}: {real_count} routed + {straight_count} straight segments, "
          f"{len(coords)} sensor points")


if __name__ == "__main__":
    main()
