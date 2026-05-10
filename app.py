"""
Fundraising Route Planner — personal tool for volunteer door-to-door fundraising.

DISCLAIMER: This software is an independent personal project. It is not created,
endorsed, sponsored, or condoned by any employer, company, organisation, political
party, charity, or any other entity. Use is entirely at the user's own risk and
responsibility. Users must comply with all applicable laws and any rules of their
own organisation. Provided "as is" with no warranty of any kind.
"""

import heapq
import math
import random
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw, MeasureControl, Geocoder
from shapely.geometry import shape, Point, MultiPoint
from shapely import wkt as shapely_wkt

st.set_page_config(
    page_title="Fundraising Route Planner",
    page_icon="🗺️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
USER_AGENT      = "FundraisingRoutePlanner/2.0 (https://streamlit.io)"
MAX_AREA_KM2    = 10.0
REQUEST_TIMEOUT = 60
WALKABLE_HIGHWAYS = (
    "footway|path|pedestrian|residential|living_street|service|"
    "tertiary|tertiary_link|secondary|secondary_link|primary|"
    "primary_link|unclassified|cycleway|steps"
)
TEAM_COLORS = [
    "#e74c3c", "#3498db", "#2ecc71", "#f39c12",
    "#9b59b6", "#1abc9c", "#e67e22", "#34495e",
    "#e91e63", "#795548", "#00bcd4", "#8bc34a",
]
CAR_MARKER_COLORS = ["green", "blue", "purple"]

WALKING_SPEED_KMH = 4.0
EMPTY_HOUSE_RATE  = 0.35
EMPTY_DOOR_SEC    = 20
MAX_END_DIST_M    = 1_000

HIGHWAY_PRIORITY = {
    "residential": 10, "living_street": 10, "unclassified": 8,
    "tertiary": 7,     "tertiary_link": 7,  "footway": 6, "pedestrian": 6,
    "path": 5,         "service": 4,        "cycleway": 3,
    "secondary": 3,    "secondary_link": 3, "primary": 2,
    "primary_link": 2, "steps": 1,
}
HOUSE_SNAP_M = 40.0

DEFAULTS = {
    "stage":                 "setup",
    "center_latlng":         None,
    "transport":             "car",
    "n_people":              2,
    "n_cars":                1,
    "shift_hours":           4.0,
    "time_per_door":         3.0,
    "same_start":            True,
    "team_starts":           [],
    "selected_parking":      None,
    "selected_parking_id":   None,
    "car_parkings":          [],
    "car_park_ids":          [],
    "selecting_car":         0,
    "parking_spots":         [],
    "map_mode":              "area",
    "manual_end":            None,
    "map_bounds":            None,
    "street_ways":           [],
    "polygon":               None,
    "buildings":             [],
    "transit_stops":         [],
    "fetch_done":            False,
    "routes":                [],
    "routes_done":           False,
    "map_key":               0,
    "reset_polygon_wkt":     None,
    "last_click_seen":       None,
    "isolation_threshold_m": 0,
    "route_detail":          "most",
    "show_legend":           True,
    "show_parking_markers":  False,
    "drop_excess":           True,
}


# ══════════════════════════════════════════════════════════════════════════════
# PURE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def haversine_m(a, b) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def approximate_area_km2(polygon) -> float:
    if polygon is None:
        return 0.0
    c = polygon.centroid
    return polygon.area * 111.32 * (111.32 * math.cos(math.radians(c.y)))


def lighten(hex_color: str, factor: float = 0.5) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r = round(r + (255 - r) * factor)
    g = round(g + (255 - g) * factor)
    b = round(b + (255 - b) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def geocode_address(address: str):
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        d = r.json()
        if d:
            return [float(d[0]["lat"]), float(d[0]["lon"])]
    except Exception:
        pass
    return None


def dist_point_to_segment_m(pt, seg_a, seg_b) -> float:
    lat0    = (pt[0] + seg_a[0] + seg_b[0]) / 3.0
    cos_lat = math.cos(math.radians(lat0))
    def _m(p):
        return (p[1] * 111_320 * cos_lat, p[0] * 111_320)
    px, py = _m(pt); ax, ay = _m(seg_a); bx, by = _m(seg_b)
    abx, aby = bx - ax, by - ay
    ab2 = abx * abx + aby * aby
    if ab2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / ab2))
    return math.hypot(px - ax - t * abx, py - ay - t * aby)


def _bearing(p1, p2) -> float:
    lat1 = math.radians(p1[0]); lat2 = math.radians(p2[0])
    dlon = math.radians(p2[1] - p1[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _add_route_arrows(m, coords: list, color: str, step_m: float = 120.0):
    if len(coords) < 2:
        return
    accum = step_m / 2.0
    for i in range(1, len(coords)):
        seg = haversine_m(coords[i - 1], coords[i])
        if seg < 1e-6:
            continue
        while accum <= seg:
            t   = accum / seg
            lat = coords[i - 1][0] + t * (coords[i][0] - coords[i - 1][0])
            lon = coords[i - 1][1] + t * (coords[i][1] - coords[i - 1][1])
            b   = _bearing(coords[i - 1], coords[i])
            folium.Marker(
                location=[lat, lon],
                icon=folium.DivIcon(
                    html=(
                        f'<div style="width:0;height:0;'
                        f'border-left:5px solid transparent;'
                        f'border-right:5px solid transparent;'
                        f'border-bottom:11px solid {color};'
                        f'transform:rotate({b:.0f}deg);'
                        f'opacity:0.85;"></div>'
                    ),
                    icon_size=(10, 11),
                    icon_anchor=(5, 5),
                ),
            ).add_to(m)
            accum += step_m
        accum -= seg


def _douglas_peucker(coords: list, epsilon_m: float) -> list:
    if len(coords) <= 2:
        return coords
    max_d, max_i = 0.0, 0
    for i in range(1, len(coords) - 1):
        d = dist_point_to_segment_m(coords[i], coords[0], coords[-1])
        if d > max_d:
            max_d, max_i = d, i
    if max_d > epsilon_m:
        return (
            _douglas_peucker(coords[:max_i + 1], epsilon_m)[:-1]
            + _douglas_peucker(coords[max_i:], epsilon_m)
        )
    return [coords[0], coords[-1]]


DETAIL_PRIORITY_THRESHOLDS = {"all": 99, "most": 8, "major": 3}


def _filter_segments_by_priority(segments: list, threshold: int) -> list:
    if threshold >= 99:
        return [list(seg["coords"]) for seg in segments if seg.get("coords")]
    polylines = []
    cur = []
    for seg in segments:
        coords = seg.get("coords") or []
        if len(coords) < 2:
            continue
        hw   = seg.get("highway", "unclassified")
        pri  = HIGHWAY_PRIORITY.get(hw, 99)
        keep = (hw != "connector") and (pri <= threshold)
        if keep:
            if not cur:
                cur = list(coords)
            elif cur[-1] == coords[0]:
                cur.extend(coords[1:])
            else:
                polylines.append(cur)
                cur = list(coords)
        else:
            if cur:
                polylines.append(cur)
                cur = []
    if cur:
        polylines.append(cur)
    return polylines


def _filter_isolated(buildings: list, threshold_m: float) -> list:
    """Remove buildings whose nearest neighbour is farther than threshold_m."""
    if not buildings or threshold_m <= 0:
        return buildings
    pts = [[b["lat"], b["lon"]] for b in buildings]
    kept = [
        b for i, b in enumerate(buildings)
        if any(haversine_m(pts[i], pts[j]) <= threshold_m
               for j in range(len(pts)) if j != i)
    ]
    return kept if kept else buildings


# ══════════════════════════════════════════════════════════════════════════════
# OVERPASS QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def _post_overpass(query: str):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for url in OVERPASS_MIRRORS:
        try:
            r = requests.post(url, data={"data": query},
                              headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json(), None
        except Exception:
            pass
    return None, "All mirrors failed"


@st.cache_data(show_spinner=False, ttl=3600, max_entries=50)
def fetch_parking_near(lat: float, lon: float, radius_m: int = 900):
    deg = radius_m / 111_320
    s, w, n, e = lat - deg, lon - deg, lat + deg, lon + deg
    query = f"""
[out:json][timeout:30];
(
  node["amenity"="parking"]["access"!="private"]({s},{w},{n},{e});
  way["amenity"="parking"]["access"!="private"]({s},{w},{n},{e});
);
out geom tags;
""".strip()
    data, _ = _post_overpass(query)
    if not data:
        return []
    spots = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if tags.get("fee", "no").lower() in ("yes", "paid"):
            continue
        if el["type"] == "node":
            spots.append({
                "lat": el["lat"], "lon": el["lon"],
                "name": tags.get("name", "Free parking"),
            })
        elif el["type"] == "way" and el.get("geometry"):
            geom = el["geometry"]
            spots.append({
                "lat": sum(nd["lat"] for nd in geom) / len(geom),
                "lon": sum(nd["lon"] for nd in geom) / len(geom),
                "name": tags.get("name", "Free parking"),
            })
    return spots


@st.cache_data(show_spinner=False, ttl=3600, max_entries=50)
def fetch_parking_in_bounds(s: float, w: float, n: float, e: float) -> list:
    query = f"""
[out:json][timeout:30];
(
  node["amenity"="parking"]["access"!="private"]({s},{w},{n},{e});
  way["amenity"="parking"]["access"!="private"]({s},{w},{n},{e});
  node["amenity"="kindergarten"]({s},{w},{n},{e});
  way["amenity"="kindergarten"]({s},{w},{n},{e});
  node["amenity"="school"]({s},{w},{n},{e});
  way["amenity"="school"]({s},{w},{n},{e});
);
out geom tags;
""".strip()
    data, _ = _post_overpass(query)
    if not data:
        return []
    spots = []
    default_names = {"parking": "Free parking", "kindergarten": "Kindergarten", "school": "School"}
    for el in data.get("elements", []):
        tags    = el.get("tags", {})
        amenity = tags.get("amenity", "")
        if amenity == "parking" and tags.get("fee", "no").lower() in ("yes", "paid"):
            continue
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        elif el["type"] == "way" and el.get("geometry"):
            geom = el["geometry"]
            lat = sum(nd["lat"] for nd in geom) / len(geom)
            lon = sum(nd["lon"] for nd in geom) / len(geom)
        else:
            continue
        if lat is None or lon is None:
            continue
        spot_type = amenity if amenity in default_names else "parking"
        spots.append({
            "lat":  lat,
            "lon":  lon,
            "name": tags.get("name") or default_names[spot_type],
            "type": spot_type,
            "id":   f"park_{len(spots)}",
        })
    return spots


@st.cache_data(show_spinner=False, ttl=3600, max_entries=20)
def fetch_all(polygon_wkt: str) -> dict:
    polygon = shapely_wkt.loads(polygon_wkt)
    minx, miny, maxx, maxy = polygon.bounds
    s, w, n, e = miny, minx, maxy, maxx
    pad = 0.004
    sp, wp, np_, ep = s - pad, w - pad, n + pad, e + pad
    query = f"""
[out:json][timeout:{REQUEST_TIMEOUT}];
(
  way["building"]({s},{w},{n},{e});
  node["addr:housenumber"]({s},{w},{n},{e});
  node["highway"="bus_stop"]({sp},{wp},{np_},{ep});
  node["railway"~"^(tram_stop|station|halt)$"]({sp},{wp},{np_},{ep});
  node["amenity"="bus_station"]({sp},{wp},{np_},{ep});
  way["highway"~"^({WALKABLE_HIGHWAYS})$"]({s},{w},{n},{e});
);
out geom tags;
""".strip()
    data, err = _post_overpass(query)
    if err or not data:
        return {"error": err or "No data returned"}

    buildings, seen, transit, street_ways = [], set(), [], []
    for el in data.get("elements", []):
        tags  = el.get("tags", {})
        etype = el.get("type")

        if etype == "way" and "building" in tags:
            geom = el.get("geometry", [])
            if not geom:
                continue
            lat = sum(nd["lat"] for nd in geom) / len(geom)
            lon = sum(nd["lon"] for nd in geom) / len(geom)
            key = (round(lat, 5), round(lon, 5))
            if key in seen or not polygon.contains(Point(lon, lat)):
                continue
            seen.add(key)
            buildings.append({
                "lat": lat, "lon": lon,
                "type": tags.get("building") or "yes",
                "housenumber": tags.get("addr:housenumber", ""),
                "street":      tags.get("addr:street", ""),
                "name":        tags.get("name", ""),
            })

        elif etype == "way" and "highway" in tags:
            geom   = el.get("geometry", [])
            coords = [[nd["lat"], nd["lon"]] for nd in geom]
            if len(coords) < 2:
                continue
            length_m = sum(haversine_m(coords[i - 1], coords[i])
                           for i in range(1, len(coords)))
            street_ways.append({
                "coords":   coords,
                "highway":  tags.get("highway", "unclassified"),
                "name":     tags.get("name", ""),
                "length_m": max(length_m, 1.0),
            })

        elif etype == "node":
            lat, lon = el.get("lat"), el.get("lon")
            if lat is None:
                continue
            if "addr:housenumber" in tags:
                key = (round(lat, 5), round(lon, 5))
                if key not in seen and polygon.contains(Point(lon, lat)):
                    seen.add(key)
                    buildings.append({
                        "lat": lat, "lon": lon, "type": "address_node",
                        "housenumber": tags.get("addr:housenumber", ""),
                        "street":      tags.get("addr:street", ""),
                        "name":        "",
                    })
            elif (tags.get("highway") == "bus_stop"
                  or tags.get("railway") in ("tram_stop", "station", "halt")
                  or tags.get("amenity") == "bus_station"):
                stype = (tags.get("railway") or tags.get("highway")
                         or tags.get("amenity") or "stop")
                transit.append({
                    "lat": lat, "lon": lon,
                    "name": tags.get("name", stype.replace("_", " ").title()),
                    "type": stype,
                })

    return {"buildings": buildings, "transit_stops": transit, "street_ways": street_ways}


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def num_teams(n: int) -> int:
    return max(1, math.ceil(n / 2))


def team_compositions(n: int) -> list:
    t = num_teams(n)
    return [2] * (t - 1) + [max(1, n - 2 * (t - 1))]


def compute_break_minutes(shift_min: int) -> int:
    return 30


def estimate_time(n_houses: int, dist_m: float, time_per_door: float) -> dict:
    empty    = round(n_houses * EMPTY_HOUSE_RATE)
    occupied = n_houses - empty
    talk_min = occupied * time_per_door + empty * (EMPTY_DOOR_SEC / 60)
    walk_min = (dist_m / 1000) / WALKING_SPEED_KMH * 60
    return {
        "walk_min":   round(walk_min),
        "talk_min":   round(talk_min),
        "total_min":  round(talk_min + walk_min),
        "occupied":   occupied,
        "empty":      empty,
        "distance_m": round(dist_m),
    }


def _project_buildings_on_route(buildings: list, road_coords: list) -> list:
    if not buildings:
        return []
    if not road_coords or len(road_coords) < 2:
        if road_coords:
            ref = road_coords[0]
        elif buildings:
            ref = [buildings[0]["lat"], buildings[0]["lon"]]
        else:
            return [0.0] * len(buildings)
        return [haversine_m(ref, [b["lat"], b["lon"]]) for b in buildings]

    cum = [0.0]
    for i in range(1, len(road_coords)):
        cum.append(cum[-1] + haversine_m(road_coords[i - 1], road_coords[i]))

    positions = []
    for b in buildings:
        bp = [b["lat"], b["lon"]]
        best_d, best_pos = float("inf"), 0.0
        for i in range(1, len(road_coords)):
            a, c = road_coords[i - 1], road_coords[i]
            d = dist_point_to_segment_m(bp, a, c)
            if d < best_d:
                best_d = d
                seg_len = haversine_m(a, c)
                if seg_len < 1e-6:
                    best_pos = cum[i - 1]
                else:
                    lat0    = (bp[0] + a[0] + c[0]) / 3.0
                    cos_lat = math.cos(math.radians(lat0))
                    ax, ay = a[1] * 111_320 * cos_lat, a[0] * 111_320
                    cx, cy = c[1] * 111_320 * cos_lat, c[0] * 111_320
                    px, py = bp[1] * 111_320 * cos_lat, bp[0] * 111_320
                    abx, aby = cx - ax, cy - ay
                    ab2 = abx * abx + aby * aby
                    t = 0.0 if ab2 < 1e-9 else max(
                        0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / ab2))
                    best_pos = cum[i - 1] + t * seg_len
        positions.append(best_pos)
    return positions


def _trim_to_shift(c_blds: list, street_ways: list, graph: dict, start_ll,
                   time_per_door: float, net_min: float,
                   max_iters: int = 8) -> tuple:
    blds        = list(c_blds)
    dropped     = 0
    team_result = plan_team_route(blds, street_ways, graph, start_ll)
    dist_m      = team_result["dist_m"] or 0.0
    stats       = estimate_time(len(blds), dist_m, time_per_door)

    for _ in range(max_iters):
        if stats["total_min"] <= net_min or len(blds) <= 1:
            break
        excess_min = stats["total_min"] - net_min
        per_house  = stats["total_min"] / max(1, len(blds))
        n_drop     = max(1, math.ceil(excess_min / max(per_house, 0.1)))
        n_drop     = min(n_drop, len(blds) - 1)

        positions = _project_buildings_on_route(blds, team_result["road_coords"])
        order     = sorted(range(len(blds)), key=lambda i: positions[i], reverse=True)
        drop_idx  = set(order[:n_drop])
        blds      = [b for i, b in enumerate(blds) if i not in drop_idx]
        dropped  += n_drop

        team_result = plan_team_route(blds, street_ways, graph, start_ll)
        dist_m      = team_result["dist_m"] or 0.0
        stats       = estimate_time(len(blds), dist_m, time_per_door)

    return blds, team_result, stats, dropped


# ── Clustering ────────────────────────────────────────────────────────────────

def _kmeans(points: list, k: int, seed: int = 42, max_iter: int = 40) -> list:
    n = len(points)
    if k >= n:
        return [[i] for i in range(n)]
    rng = random.Random(seed)
    centroids = [list(points[i]) for i in rng.sample(range(n), k)]
    assignments = [0] * n
    for _ in range(max_iter):
        changed = False
        for i, p in enumerate(points):
            best = min(range(k), key=lambda ci: haversine_m(p, centroids[ci]))
            if best != assignments[i]:
                assignments[i] = best
                changed = True
        if not changed:
            break
        for ci in range(k):
            grp = [points[i] for i in range(n) if assignments[i] == ci]
            if grp:
                centroids[ci] = [
                    sum(p[0] for p in grp) / len(grp),
                    sum(p[1] for p in grp) / len(grp),
                ]
    clusters = [[] for _ in range(k)]
    for i, ci in enumerate(assignments):
        clusters[ci].append(i)
    return clusters


def _balance(points: list, clusters: list, max_iter: int = 25) -> list:
    k = len(clusters)
    for _ in range(max_iter):
        sizes = [len(c) for c in clusters]
        big   = max(range(k), key=lambda i: sizes[i])
        sml   = min(range(k), key=lambda i: sizes[i])
        if sizes[big] - sizes[sml] <= 1:
            break
        if not clusters[sml]:
            continue
        sc = [
            sum(points[i][0] for i in clusters[sml]) / len(clusters[sml]),
            sum(points[i][1] for i in clusters[sml]) / len(clusters[sml]),
        ]
        j = min(range(len(clusters[big])),
                key=lambda x: haversine_m(points[clusters[big][x]], sc))
        clusters[sml].append(clusters[big].pop(j))
    return clusters


def _route_dist(wps: list) -> float:
    return sum(haversine_m(wps[i - 1], wps[i]) for i in range(1, len(wps)))


def _split_left_right(buildings: list):
    lats = [b["lat"] for b in buildings]
    lons = [b["lon"] for b in buildings]
    clat = sum(lats) / len(lats)
    clon = sum(lons) / len(lons)
    if (max(lats) - min(lats)) >= (max(lons) - min(lons)):
        left  = [b for b in buildings if b["lon"] <= clon]
        right = [b for b in buildings if b["lon"] >  clon]
    else:
        left  = [b for b in buildings if b["lat"] <= clat]
        right = [b for b in buildings if b["lat"] >  clat]
    if not left or not right:
        half  = len(buildings) // 2
        left  = buildings[:half]
        right = buildings[half:]
    return left, right


def compute_contour(buildings: list):
    if len(buildings) < 3:
        return None
    try:
        hull = MultiPoint([(b["lon"], b["lat"]) for b in buildings]).convex_hull
        if hull.geom_type == "Polygon":
            return [[c[1], c[0]] for c in hull.exterior.coords]
    except Exception:
        pass
    return None


# ── Street graph (RPP) ────────────────────────────────────────────────────────

def build_street_graph(street_ways: list) -> dict:
    graph = {}
    for way in street_ways:
        coords = way["coords"]
        hw     = way.get("highway", "unclassified")
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            na = (round(a[0], 5), round(a[1], 5))
            nb = (round(b[0], 5), round(b[1], 5))
            if na == nb:
                continue
            if na not in graph:
                graph[na] = {"lat": a[0], "lon": a[1], "neighbors": {}}
            if nb not in graph:
                graph[nb] = {"lat": b[0], "lon": b[1], "neighbors": {}}
            dist = haversine_m(a, b)
            graph[na]["neighbors"][nb] = {"dist_m": dist, "coords": [a, b], "highway": hw}
            graph[nb]["neighbors"][na] = {"dist_m": dist, "coords": [b, a], "highway": hw}
    return graph


def assign_buildings_to_edges(buildings: list, street_ways: list) -> dict:
    required_edges: dict = {}
    for bld in buildings:
        bpt = [bld["lat"], bld["lon"]]
        best_dist = float("inf")
        best_edge = None
        for way in street_ways:
            coords = way["coords"]
            clat = sum(c[0] for c in coords) / len(coords)
            clon = sum(c[1] for c in coords) / len(coords)
            if haversine_m(bpt, [clat, clon]) > 500:
                continue
            for i in range(len(coords) - 1):
                d = dist_point_to_segment_m(bpt, coords[i], coords[i + 1])
                if d < best_dist:
                    best_dist = d
                    na = (round(coords[i][0], 5),     round(coords[i][1], 5))
                    nb = (round(coords[i + 1][0], 5), round(coords[i + 1][1], 5))
                    best_edge = (min(na, nb), max(na, nb))
        if best_edge is not None:
            required_edges.setdefault(best_edge, []).append(bld)
    return required_edges


def dijkstra(graph: dict, source) -> tuple:
    dist = {source: 0.0}
    prev: dict = {}
    pq = [(0.0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")):
            continue
        for v, edge in graph.get(u, {}).get("neighbors", {}).items():
            nd = d + edge["dist_m"]
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, prev


def rural_postman_path(graph: dict, required_edges: dict, start_node) -> list:
    if not required_edges:
        return [start_node] if start_node in graph else []

    def _find_components(req_edges):
        adj: dict = {}
        for (a, b) in req_edges:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        visited: set = set()
        components = []
        for s in adj:
            if s in visited:
                continue
            comp_nodes: set = set()
            stack = [s]
            while stack:
                nd = stack.pop()
                if nd in visited:
                    continue
                visited.add(nd)
                comp_nodes.add(nd)
                for nb in adj.get(nd, set()):
                    if nb not in visited:
                        stack.append(nb)
            comp_edges = [e for e in req_edges if e[0] in comp_nodes]
            components.append(comp_edges)
        return components

    def _odd_degree_nodes(comp_edges):
        deg: dict = {}
        for (a, b) in comp_edges:
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
        return [nd for nd, d in deg.items() if d % 2 == 1]

    def _reconstruct(prev, src, tgt):
        path = []
        nd = tgt
        while nd != src:
            path.append(nd)
            if nd not in prev:
                return []
            nd = prev[nd]
        path.append(src)
        return list(reversed(path))

    def _greedy_match(odd_nodes, all_d):
        remaining = list(odd_nodes)
        pairs = []
        while len(remaining) >= 2:
            bd, bi, bj = float("inf"), 0, 1
            for i in range(len(remaining)):
                for j in range(i + 1, len(remaining)):
                    d = all_d.get(remaining[i], {}).get(remaining[j], float("inf"))
                    if d < bd:
                        bd, bi, bj = d, i, j
            pairs.append((remaining[bi], remaining[bj]))
            remaining.pop(bj)
            remaining.pop(bi)
        return pairs

    def _hierholzer(adj_lists):
        if not adj_lists:
            return []
        start = next(iter(adj_lists))
        adj_copy = {nd: list(nbs) for nd, nbs in adj_lists.items()}
        stack = [start]
        circuit = []
        while stack:
            v = stack[-1]
            if adj_copy.get(v):
                u = adj_copy[v].pop()
                try:
                    adj_copy[u].remove(v)
                except ValueError:
                    pass
                stack.append(u)
            else:
                circuit.append(stack.pop())
        return list(reversed(circuit))

    components = _find_components(required_edges)
    component_walks = []

    for comp_edges in components:
        if not comp_edges:
            continue
        comp_adj: dict = {}
        for (a, b) in comp_edges:
            comp_adj.setdefault(a, []).append(b)
            comp_adj.setdefault(b, []).append(a)

        odd = _odd_degree_nodes(comp_edges)
        if odd:
            all_dist: dict = {}
            all_prev: dict = {}
            for nd in odd:
                d, p = dijkstra(graph, nd)
                all_dist[nd] = d
                all_prev[nd] = p
            pairs = _greedy_match(odd, all_dist)
            for (u, v) in pairs:
                if all_dist.get(u, {}).get(v, float("inf")) == float("inf"):
                    continue
                path = _reconstruct(all_prev[u], u, v)
                for i in range(len(path) - 1):
                    a, b = path[i], path[i + 1]
                    comp_adj.setdefault(a, []).append(b)
                    comp_adj.setdefault(b, []).append(a)

        walk = _hierholzer(comp_adj)
        if walk:
            component_walks.append(walk)

    if not component_walks:
        return []

    if len(component_walks) == 1:
        final_walk = component_walks[0]
    else:
        if start_node in graph:
            d0, _ = dijkstra(graph, start_node)
            order = sorted(range(len(component_walks)),
                           key=lambda i: d0.get(component_walks[i][0], float("inf")))
        else:
            order = list(range(len(component_walks)))

        final_walk: list = []
        for idx in order:
            walk = component_walks[idx]
            if final_walk:
                src = final_walk[-1]
                dst = walk[0]
                d, p = dijkstra(graph, src)
                connector = _reconstruct(p, src, dst)
                if len(connector) > 1:
                    final_walk.extend(connector[1:])
            final_walk.extend(walk)

    if start_node in graph and final_walk:
        d_s, _ = dijkstra(graph, start_node)
        best_i = min(range(len(final_walk)),
                     key=lambda i: d_s.get(final_walk[i], float("inf")))
        final_walk = final_walk[best_i:] + final_walk[:best_i]

    return final_walk


def route_to_coords(node_walk: list, graph: dict) -> list:
    if not node_walk:
        return []
    first = graph.get(node_walk[0], {})
    result = [[first.get("lat", 0), first.get("lon", 0)]]
    for i in range(1, len(node_walk)):
        n1, n2 = node_walk[i - 1], node_walk[i]
        edge = graph.get(n1, {}).get("neighbors", {}).get(n2)
        if edge and edge.get("coords"):
            for c in edge["coords"][1:]:
                result.append([c[0], c[1]])
        else:
            n2_nd = graph.get(n2, {})
            if n2_nd:
                result.append([n2_nd.get("lat", 0), n2_nd.get("lon", 0)])
    return result


def route_to_coords_and_segments(node_walk: list, graph: dict) -> tuple:
    if not node_walk:
        return [], []
    first = graph.get(node_walk[0], {})
    flat  = [[first.get("lat", 0), first.get("lon", 0)]]
    segments = []
    for i in range(1, len(node_walk)):
        n1, n2 = node_walk[i - 1], node_walk[i]
        edge   = graph.get(n1, {}).get("neighbors", {}).get(n2)
        if edge and edge.get("coords"):
            seg_coords = [[c[0], c[1]] for c in edge["coords"]]
            for c in seg_coords[1:]:
                flat.append([c[0], c[1]])
            segments.append({
                "coords":  seg_coords,
                "highway": edge.get("highway", "unclassified"),
            })
        else:
            n2_nd = graph.get(n2, {})
            if n2_nd:
                end_pt = [n2_nd.get("lat", 0), n2_nd.get("lon", 0)]
                start_pt = list(flat[-1])
                flat.append(end_pt)
                segments.append({
                    "coords":  [start_pt, end_pt],
                    "highway": "connector",
                })
    return flat, segments


def plan_team_route(buildings: list, street_ways: list, graph: dict,
                    start_ll) -> dict:
    empty = {
        "road_coords": [], "road_segments": [], "left_count": None,
        "right_count": None, "contour": compute_contour(buildings),
        "dist_m": 0.0,
    }
    if not buildings:
        return empty

    if not graph or not street_ways:
        pts = [[b["lat"], b["lon"]] for b in buildings]
        if start_ll:
            unvisited = list(range(len(pts)))
            cur = start_ll
            ordered = []
            while unvisited:
                j = min(unvisited, key=lambda i: haversine_m(cur, pts[i]))
                ordered.append(j)
                cur = pts[j]
                unvisited.remove(j)
            road_coords = [pts[i] for i in ordered]
        else:
            road_coords = pts
        dist_m = _route_dist(road_coords) if len(road_coords) >= 2 else 0.0
        left_count = right_count = None
        if len(buildings) >= 6:
            left, right = _split_left_right(buildings)
            left_count, right_count = len(left), len(right)
        road_segments = (
            [{"coords": road_coords, "highway": "unclassified"}]
            if len(road_coords) >= 2 else []
        )
        return {
            "road_coords": road_coords, "road_segments": road_segments,
            "left_count": left_count, "right_count": right_count,
            "contour": compute_contour(buildings), "dist_m": dist_m,
        }

    required_edges = assign_buildings_to_edges(buildings, street_ways)
    if not required_edges:
        return empty

    if start_ll:
        start_node = min(graph.keys(),
                         key=lambda nd: haversine_m(start_ll,
                                                    [graph[nd]["lat"], graph[nd]["lon"]]))
    else:
        start_node = next(iter(required_edges.keys()))[0]

    node_walk = rural_postman_path(graph, required_edges, start_node)
    road_coords, road_segments = route_to_coords_and_segments(node_walk, graph)
    dist_m    = (_route_dist(road_coords) if len(road_coords) >= 2 else 0.0)

    left_count = right_count = None
    if len(buildings) >= 6:
        left, right = _split_left_right(buildings)
        left_count, right_count = len(left), len(right)

    return {
        "road_coords":   road_coords,
        "road_segments": road_segments,
        "left_count":    left_count,
        "right_count":   right_count,
        "contour":       compute_contour(buildings),
        "dist_m":        dist_m,
    }


# ── Master planner ────────────────────────────────────────────────────────────

def plan_routes(buildings, car_parkings, transit_stops, team_starts,
                n_people, shift_minutes, time_per_door, transport,
                street_ways=None, manual_end=None, n_cars=1, coverage=0.90,
                isolation_threshold_m=0, drop_excess=False) -> list:

    if not buildings:
        return []

    target_n  = max(1, round(len(buildings) * coverage))
    buildings = buildings[:target_n]

    if isolation_threshold_m > 0:
        buildings = _filter_isolated(buildings, isolation_threshold_m)
        if not buildings:
            return []

    pts = [[b["lat"], b["lon"]] for b in buildings]

    valid_parks  = [p for p in (car_parkings or []) if p is not None]
    primary_parking = valid_parks[0] if valid_parks else None

    break_min = compute_break_minutes(shift_minutes)
    net_min   = shift_minutes - break_min
    graph     = build_street_graph(street_ways or [])

    n_valid_cars = min(n_cars, max(1, len(valid_parks))) if n_cars > 1 else 1

    if n_valid_cars > 1:
        car_clusters_raw = _kmeans(pts, n_valid_cars)
        car_clusters_raw = _balance(pts, car_clusters_raw)
        car_cluster_map  = {ci: idxs for ci, idxs in enumerate(car_clusters_raw)}
    else:
        car_cluster_map = {0: list(range(len(pts)))}

    result = []
    global_ti = 0

    for ci in range(n_valid_cars):
        bld_idxs = car_cluster_map.get(ci, [])
        if not bld_idxs:
            continue
        park_ll = valid_parks[ci] if ci < len(valid_parks) else primary_parking

        ppc       = n_people // n_valid_cars + (1 if ci < n_people % n_valid_cars else 0)
        car_sizes = team_compositions(max(1, ppc))
        car_n_t   = len(car_sizes)

        c_pts_all  = [pts[i]       for i in bld_idxs]
        c_blds_all = [buildings[i] for i in bld_idxs]

        if car_n_t > 1 and len(c_pts_all) >= car_n_t:
            sub_clusters = _kmeans(c_pts_all, car_n_t)
            sub_clusters = _balance(c_pts_all, sub_clusters)
        else:
            sub_clusters = [list(range(len(c_pts_all)))]
            car_sizes    = [max(1, ppc)]

        for local_ti, (sub_idxs, size) in enumerate(zip(sub_clusters, car_sizes)):
            if not sub_idxs:
                global_ti += 1
                continue

            c_pts  = [c_pts_all[i]  for i in sub_idxs]
            c_blds = [c_blds_all[i] for i in sub_idxs]

            if team_starts:
                if st.session_state.same_start or len(team_starts) == 1:
                    start_ll = team_starts[0]
                else:
                    start_ll = (team_starts[global_ti]
                                if global_ti < len(team_starts)
                                else team_starts[-1])
            elif park_ll:
                start_ll = park_ll
            else:
                start_ll = c_pts[0]

            start_label = ("🅿️ Parking" if transport == "car" else "📍 Start point")

            team_result = plan_team_route(c_blds, street_ways or [], graph, start_ll)

            dist_m  = team_result["dist_m"] or _route_dist([start_ll] + c_pts)
            stats   = estimate_time(len(c_blds), dist_m, time_per_door)
            dropped = 0

            if drop_excess and stats["total_min"] > net_min and len(c_blds) > 1:
                c_blds, team_result, stats, dropped = _trim_to_shift(
                    c_blds, street_ways or [], graph, start_ll,
                    time_per_door, net_min,
                )
                c_pts  = [[b["lat"], b["lon"]] for b in c_blds]
                dist_m = team_result["dist_m"] or _route_dist([start_ll] + c_pts)

            road_coords = team_result["road_coords"]

            end_ll, end_label, end_type = _resolve_end(
                c_pts, transit_stops, start_ll, park_ll, transport,
                manual_end=manual_end,
            )

            stats["break_min"]  = break_min
            stats["net_min"]    = net_min
            stats["fits_shift"] = stats["total_min"] <= net_min

            if c_pts and park_ll:
                ref = park_ll if transport == "car" else start_ll
                if ref:
                    ed = round(haversine_m(c_pts[-1], ref))
                    stats["end_dist_m"] = ed
                    stats["end_ok"]     = ed <= MAX_END_DIST_M

            contour = team_result.get("contour") or compute_contour(c_blds)

            result.append({
                "team_idx":    global_ti,
                "car_idx":     ci,
                "size":        size,
                "color":       TEAM_COLORS[global_ti % len(TEAM_COLORS)],
                "houses":      c_pts,
                "house_data":  c_blds,
                "start":       start_ll,
                "start_label": start_label,
                "end":         end_ll,
                "end_label":   end_label,
                "end_type":    end_type,
                "stats":       stats,
                "road_coords": road_coords,
                "road_segments": team_result.get("road_segments", []),
                "left_count":  team_result.get("left_count"),
                "right_count": team_result.get("right_count"),
                "contour":     contour,
                "dropped":     dropped,
            })
            global_ti += 1

    return result


def _resolve_end(ordered_pts, transit_stops, start_ll, parking_spot, transport,
                 manual_end=None):
    if not ordered_pts:
        return None, "—", "none"

    last = ordered_pts[-1]

    if manual_end is not None:
        d = haversine_m(last, manual_end) if ordered_pts else 0
        return manual_end, f"~{round(d)} m to set end point", "manual"

    if transport == "car" and parking_spot:
        d = haversine_m(last, parking_spot)
        lbl = (f"~{round(d)} m from parking"
               if d <= MAX_END_DIST_M
               else f"⚠️ {round(d / 1000, 1)} km from parking")
        return parking_spot, lbl, "parking"

    if transit_stops:
        t = min(transit_stops, key=lambda x: haversine_m(last, [x["lat"], x["lon"]]))
        d = haversine_m(last, [t["lat"], t["lon"]])
        if d < 600:
            return [t["lat"], t["lon"]], t["name"], "transit"

    d = haversine_m(last, start_ll) if start_ll else 9_999
    lbl = (f"~{round(d)} m back to start"
           if d <= MAX_END_DIST_M
           else f"⚠️ {round(d / 1000, 1)} km from start")
    return start_ll, lbl, "start"


# ══════════════════════════════════════════════════════════════════════════════
# SESSION STATE
# ══════════════════════════════════════════════════════════════════════════════

for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ══════════════════════════════════════════════════════════════════════════════
# SHARED STYLES
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<style>
  .main-title  { font-size:2rem; font-weight:700; color:#1a1a2e; margin-bottom:0; }
  .sub-title   { color:#666; font-size:.9rem; margin-top:0; margin-bottom:1.5rem; }
  .step-label  { font-weight:600; font-size:.9rem; color:#333; margin-bottom:.2rem; }
  .phase-badge { background:#2980b9; color:#fff; padding:2px 8px;
                 border-radius:12px; font-size:.75rem; font-weight:600; }
  .team-card   { border-radius:8px; padding:10px 12px; margin-bottom:8px;
                 border-left:4px solid; background:rgba(0,0,0,0.03);
                 font-size:.85rem; line-height:1.7; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# STAGE: SETUP
# ══════════════════════════════════════════════════════════════════════════════

if st.session_state.stage == "setup":

    st.markdown('<p class="main-title">🗺️ Fundraising Route Planner</p>',
                unsafe_allow_html=True)
    st.markdown(
        '<p class="sub-title"><span class="phase-badge">Setup</span>'
        '&nbsp; Configure your session — the map opens after this step.</p>',
        unsafe_allow_html=True,
    )

    left, right = st.columns([1, 1], gap="large")

    with left:
        with st.form("setup_form"):

            st.subheader("🚗 Transport")
            transport = st.radio(
                "How is your team getting there?",
                options=["car", "walk"],
                format_func=lambda x: (
                    "🚗 By car — I need a parking spot"
                    if x == "car"
                    else "🚶 On foot / public transport"
                ),
                index=0 if st.session_state.transport == "car" else 1,
                horizontal=True,
            )

            n_cars = 1
            if transport == "car":
                n_cars = st.radio(
                    "Number of cars",
                    options=[1, 2, 3],
                    format_func=lambda x: f"{x} car{'s' if x > 1 else ''}",
                    index=st.session_state.n_cars - 1,
                    horizontal=True,
                )

            st.subheader("👥 Team")
            n_people = st.slider(
                "Total people in the field today",
                min_value=1, max_value=20,
                value=st.session_state.n_people,
            )
            n_t   = num_teams(n_people)
            sizes = team_compositions(n_people)
            st.caption(
                f"→ **{n_t} team{'s' if n_t > 1 else ''}**: "
                + " + ".join(f"{s}p" for s in sizes)
                + (" — each person covers one side of the street"
                   if max(sizes) == 2
                   else " — solo coverage")
            )

            same_start = True
            if transport == "walk" and n_t > 1:
                same_start = st.checkbox(
                    "All teams start from the same location",
                    value=st.session_state.same_start,
                )

            st.subheader("⏱️ Shift")
            shift_hours = st.slider(
                "Shift duration (hours)",
                min_value=1.0, max_value=10.0,
                value=st.session_state.shift_hours, step=0.5,
            )
            shift_min = int(shift_hours * 60)
            brk       = compute_break_minutes(shift_min)
            st.caption(
                f"→ {brk} min break included · "
                f"**{shift_min - brk} min active**"
            )

            st.subheader("🚪 Doors")
            time_per_door = st.slider(
                "Average time per answered door (min)",
                min_value=1.0, max_value=15.0,
                value=st.session_state.time_per_door, step=0.5,
                help=(
                    f"~{round(EMPTY_HOUSE_RATE * 100)}% of doors go unanswered "
                    f"({EMPTY_DOOR_SEC} s each are still counted)."
                ),
            )

            with st.expander("⚙️ Advanced options", expanded=False):
                isolation_threshold_m = st.slider(
                    "Ignore isolated houses — nearest-neighbour threshold (m)",
                    min_value=0, max_value=500,
                    value=st.session_state.isolation_threshold_m,
                    step=10,
                    help=(
                        "Houses whose nearest neighbour is farther than this "
                        "distance will be excluded from route planning. "
                        "0 = include all houses."
                    ),
                )
                if isolation_threshold_m > 0:
                    st.caption(
                        f"Houses more than {isolation_threshold_m} m from any "
                        "neighbour will be skipped."
                    )

                drop_excess = st.checkbox(
                    "Drop houses that won't fit in shift",
                    value=st.session_state.get("drop_excess", True),
                    help=(
                        "When a team's planned route exceeds the active shift "
                        "time, drop the houses farthest along the route until "
                        "it fits."
                    ),
                )

            submitted = st.form_submit_button(
                "Continue to map →", type="primary", use_container_width=True
            )

    with right:
        st.markdown("#### How it works")
        st.markdown("""
1. **Fill in the form** on the left and click *Continue*.
2. The map opens. Use the **search box** (top-left 🔍) to navigate to your area.
   - 🚗 **Car mode** — set parking for each car, then draw your area.
   - 🚶 **Walk mode** — click **Set start** then drop a marker on the map.
3. **Draw your work area** with the rectangle or polygon tool.
4. Click **Fetch & Plan** — the app downloads OSM data and builds optimised road-following routes.
5. Each team gets:
   - a **coloured zone** showing their coverage area
   - a **road-following path** with **direction arrows**
   - a left/right house count (2-person teams)
   - estimated walk time, talk time and break schedule
   - an expandable **street-by-street plan** for briefing volunteers
""")
        with st.expander("⚠️ Disclaimer", expanded=False):
            st.warning(
                "**Independent personal tool — no institutional affiliation.**\n\n"
                "This software is not created, endorsed, sponsored, or condoned by "
                "any employer, company, organisation, political party, charity, or "
                "any other entity. Its use is entirely at your own risk and "
                "responsibility. You are responsible for ensuring your fundraising "
                "activities comply with all applicable laws, regulations, and the "
                "rules of any organisation you may belong to. This tool is provided "
                '"as is" with no warranty of any kind.',
                icon="⚠️",
            )

    if submitted:
        st.session_state.update(
            transport              = transport,
            n_people               = n_people,
            n_cars                 = n_cars,
            same_start             = same_start,
            shift_hours            = shift_hours,
            time_per_door          = time_per_door,
            isolation_threshold_m  = isolation_threshold_m,
            drop_excess            = drop_excess,
            car_parkings           = [],
            car_park_ids           = [],
            selecting_car          = 0,
            parking_spots          = [],
            selected_parking       = None,
            selected_parking_id    = None,
            show_parking_markers   = False,
            team_starts            = [],
            polygon                = None,
            buildings              = [],
            transit_stops          = [],
            fetch_done             = False,
            routes                 = [],
            routes_done            = False,
            last_click_seen        = None,
            map_mode               = "area",
            manual_end             = None,
            street_ways            = [],
            route_detail           = "most",
            show_legend            = True,
            stage                  = "map",
        )
        st.rerun()

    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE: MAP
# ══════════════════════════════════════════════════════════════════════════════

st.markdown('<p class="main-title">🗺️ Fundraising Route Planner</p>',
            unsafe_allow_html=True)
st.markdown(
    '<p class="sub-title"><span class="phase-badge">Phase 2</span>'
    '&nbsp; Route planning · Team splitting · Parking &amp; transit</p>',
    unsafe_allow_html=True,
)

map_col, ctrl_col = st.columns([3, 1])

transport = st.session_state.transport
n_people  = st.session_state.n_people
n_cars    = st.session_state.n_cars
n_t       = num_teams(n_people)
sizes     = team_compositions(n_people)
polygon   = st.session_state.polygon
area_km2  = approximate_area_km2(polygon)
map_mode  = st.session_state.map_mode


# ══════════════════════════════════════════════════════════════════════════════
# CONTROL PANEL
# ══════════════════════════════════════════════════════════════════════════════

with ctrl_col:

    st.caption(
        f"{'🚗' if transport == 'car' else '🚶'} · "
        f"{n_people} people · {n_t} team{'s' if n_t > 1 else ''}"
        + (f" · {n_cars} car{'s' if n_cars > 1 else ''}" if transport == "car" else "")
        + f" · {st.session_state.shift_hours} h"
    )
    st.caption("⚠️ Independent tool — not affiliated with any organisation.")
    if st.button("← Edit settings", use_container_width=True):
        st.session_state.stage = "setup"
        st.rerun()

    st.divider()

    # ── STEP 1 ─────────────────────────────────────────────────────────────────

    if transport == "car":
        st.markdown('<p class="step-label">🅿️ Step 1 — Confirm parking</p>',
                    unsafe_allow_html=True)

        if n_cars > 1:
            car_tab_objs = st.tabs([f"🚗 Car {i + 1}" for i in range(n_cars)])
        else:
            car_tab_objs = [st.container()]

        for ci, tab in enumerate(car_tab_objs):
            with tab:
                if st.session_state.selecting_car == ci:
                    _sel    = (st.session_state.car_parkings[ci]
                               if ci < len(st.session_state.car_parkings) else None)
                    _sel_id = (st.session_state.car_park_ids[ci]
                               if ci < len(st.session_state.car_park_ids) else None)
                    if st.session_state.selected_parking != _sel:
                        st.session_state.selected_parking    = _sel
                        st.session_state.selected_parking_id = _sel_id

                if n_cars > 1 and st.session_state.selecting_car != ci:
                    if st.button(f"Switch to Car {ci + 1}", key=f"sw_car_{ci}",
                                 use_container_width=True):
                        st.session_state.selecting_car       = ci
                        st.session_state.selected_parking    = (
                            st.session_state.car_parkings[ci]
                            if ci < len(st.session_state.car_parkings) else None)
                        st.session_state.selected_parking_id = (
                            st.session_state.car_park_ids[ci]
                            if ci < len(st.session_state.car_park_ids) else None)
                        st.rerun()

                bounds_ready = st.session_state.map_bounds is not None
                if st.button("🅿️ Find parking in visible area",
                             key=f"find_pk_{ci}",
                             disabled=not bounds_ready,
                             use_container_width=True):
                    with st.spinner("Searching for parking spots…"):
                        b = st.session_state.map_bounds
                        spots = fetch_parking_in_bounds(
                            b["_southWest"]["lat"], b["_southWest"]["lng"],
                            b["_northEast"]["lat"], b["_northEast"]["lng"],
                        )
                    st.session_state.parking_spots       = spots
                    st.session_state.selected_parking_id = None
                    st.session_state.selected_parking    = None
                    st.session_state.show_parking_markers = True
                    _cp = list(st.session_state.car_parkings)
                    _ci = list(st.session_state.car_park_ids)
                    while len(_cp) <= ci:
                        _cp.append(None)
                        _ci.append(None)
                    _cp[ci] = None
                    _ci[ci] = None
                    st.session_state.car_parkings = _cp
                    st.session_state.car_park_ids = _ci
                    st.session_state.selecting_car = ci
                    st.rerun()

                if not bounds_ready:
                    st.caption("Pan or zoom the map first.")

                if st.session_state.selecting_car == ci and st.session_state.parking_spots:
                    n_spots = len(st.session_state.parking_spots)

                    col_info, col_tog, col_done = st.columns([2, 1, 1])
                    with col_info:
                        st.caption(f"{n_spots} spot{'s' if n_spots != 1 else ''} found")
                    with col_tog:
                        show_m = st.session_state.show_parking_markers
                        if st.button(
                            "🗺️ Hide" if show_m else "🗺️ Show",
                            key=f"tog_map_{ci}",
                            use_container_width=True,
                            help="Show or hide parking markers on the map (to avoid accidental clicks while drawing)",
                        ):
                            st.session_state.show_parking_markers = not show_m
                            st.session_state.map_key += 1
                            st.rerun()
                    with col_done:
                        if st.button(
                            "✓ Done",
                            key=f"done_pk_{ci}",
                            use_container_width=True,
                            help="Hide the parking list and markers (your selection is kept)",
                        ):
                            st.session_state.parking_spots        = []
                            st.session_state.show_parking_markers = False
                            st.session_state.map_key += 1
                            st.rerun()

                    _icon_lbl = {"parking": "🅿️", "school": "🏫", "kindergarten": "🏡"}
                    with st.container(height=200):
                        for sp in st.session_state.parking_spots:
                            is_sel = (st.session_state.selected_parking_id == sp["id"])
                            icon   = _icon_lbl.get(sp["type"], "🅿️")
                            label  = f"{'✅ ' if is_sel else ''}{icon} {sp['name']}"
                            if st.button(label, key=f"btn_{sp['id']}", use_container_width=True):
                                _cp = list(st.session_state.car_parkings)
                                _ci = list(st.session_state.car_park_ids)
                                while len(_cp) <= ci:
                                    _cp.append(None)
                                    _ci.append(None)
                                if is_sel:
                                    _cp[ci] = None
                                    _ci[ci] = None
                                    st.session_state.selected_parking    = None
                                    st.session_state.selected_parking_id = None
                                else:
                                    _cp[ci] = [sp["lat"], sp["lon"]]
                                    _ci[ci] = sp["id"]
                                    st.session_state.selected_parking    = _cp[ci]
                                    st.session_state.selected_parking_id = _ci[ci]
                                st.session_state.car_parkings = _cp
                                st.session_state.car_park_ids = _ci
                                st.rerun()

                park_for_car = (st.session_state.car_parkings[ci]
                                if ci < len(st.session_state.car_parkings) else None)
                if park_for_car:
                    p = park_for_car
                    label = (f"✅ Parking set ({p[0]:.4f}, {p[1]:.4f})"
                             if n_cars == 1
                             else f"✅ Car {ci + 1} parking set")
                    st.success(label)
                else:
                    lbl = ("_No parking selected yet._"
                           if n_cars == 1
                           else f"_Car {ci + 1}: no parking yet._")
                    st.caption(lbl)

    else:
        st.markdown('<p class="step-label">📍 Step 1 — Set starting point(s)</p>',
                    unsafe_allow_html=True)

        col_a, col_b = st.columns(2)
        with col_a:
            active = (map_mode == "set_start")
            if st.button("📍 Set start",
                         type="primary" if active else "secondary",
                         use_container_width=True):
                st.session_state.map_mode = "set_start"
                st.session_state.map_key += 1
                st.rerun()
        with col_b:
            active = (map_mode == "set_end")
            if st.button("🏁 Set end (opt.)",
                         type="primary" if active else "secondary",
                         use_container_width=True):
                st.session_state.map_mode = "set_end"
                st.session_state.map_key += 1
                st.rerun()

        n_needed = 1 if st.session_state.same_start else n_t
        n_have   = len(st.session_state.team_starts)
        if n_have < n_needed:
            who = ("all teams"
                   if st.session_state.same_start
                   else f"Team {n_have + 1}")
            st.info(f"Drop a start marker for **{who}**.")
        else:
            st.success(
                f"✅ {'Starting point' if n_needed == 1 else f'{n_have} starting points'} set."
            )
            if st.button("↺ Reset start points", use_container_width=True):
                st.session_state.team_starts    = []
                st.session_state.last_click_seen = None
                st.rerun()

        for i, s in enumerate(st.session_state.team_starts):
            lbl = "All teams" if st.session_state.same_start else f"Team {i + 1}"
            st.caption(f"📍 {lbl}: {s[0]:.4f}, {s[1]:.4f}")

        if st.session_state.manual_end:
            e = st.session_state.manual_end
            st.caption(f"🏁 End: {e[0]:.4f}, {e[1]:.4f}")
            if st.button("✕ Clear end point", use_container_width=True):
                st.session_state.manual_end = None
                st.rerun()

    st.divider()

    # ── STEP 2 ─────────────────────────────────────────────────────────────────

    st.markdown('<p class="step-label">📐 Step 2 — Draw work area</p>',
                unsafe_allow_html=True)
    if polygon:
        if area_km2 > MAX_AREA_KM2:
            st.error(f"Area too large ({area_km2:.2f} km²). Max {MAX_AREA_KM2} km².")
        else:
            st.success(f"Area: ~{area_km2:.3f} km²")
    else:
        st.caption(f"Use the ◻ or ⬡ tool on the map. Max {MAX_AREA_KM2} km².")

    st.divider()

    # ── STEP 3 ─────────────────────────────────────────────────────────────────

    st.markdown('<p class="step-label">Step 3 — Fetch &amp; Plan</p>',
                unsafe_allow_html=True)

    starts_ok = (
        transport == "car"
        or len(st.session_state.team_starts) >= (1 if st.session_state.same_start else n_t)
    )
    area_ok = polygon is not None and area_km2 <= MAX_AREA_KM2
    can_go  = starts_ok and area_ok

    if not starts_ok:
        st.caption("Set your starting point(s) first (Step 1).")
    if not area_ok and polygon is None:
        st.caption("Draw your work area first (Step 2).")

    if st.button("🗺️ Fetch & Plan Routes", type="primary",
                 disabled=not can_go, use_container_width=True):

        with st.spinner("Fetching map data from OpenStreetMap…"):
            result = fetch_all(polygon.wkt)

        if "error" in result:
            st.error(f"Fetch failed: {result['error']}")
        else:
            st.session_state.buildings     = result["buildings"]
            st.session_state.transit_stops = result.get("transit_stops", [])
            st.session_state.street_ways   = result.get("street_ways", [])
            st.session_state.fetch_done    = True

            if not result["buildings"]:
                st.warning("No buildings found. Try a larger area.")
            else:
                if transport == "car":
                    effective_starts = []
                elif st.session_state.same_start:
                    effective_starts = (
                        st.session_state.team_starts * n_t
                        if st.session_state.team_starts else []
                    )
                else:
                    effective_starts = st.session_state.team_starts

                with st.spinner(f"Planning routes for {n_t} team(s)…"):
                    routes = plan_routes(
                        buildings              = result["buildings"],
                        car_parkings           = st.session_state.car_parkings,
                        transit_stops          = result.get("transit_stops", []),
                        team_starts            = effective_starts,
                        n_people               = n_people,
                        shift_minutes          = int(st.session_state.shift_hours * 60),
                        time_per_door          = st.session_state.time_per_door,
                        transport              = transport,
                        street_ways            = result.get("street_ways", []),
                        manual_end             = st.session_state.get("manual_end"),
                        n_cars                 = n_cars,
                        coverage               = 0.90,
                        isolation_threshold_m  = st.session_state.isolation_threshold_m,
                        drop_excess            = st.session_state.get("drop_excess", True),
                    )

                st.session_state.routes      = routes
                st.session_state.routes_done = True
                st.rerun()

    # ── Route summaries ────────────────────────────────────────────────────────

    if st.session_state.routes_done and st.session_state.routes:
        st.divider()
        total_h = sum(len(r["houses"]) for r in st.session_state.routes)
        total_b = len(st.session_state.buildings)
        st.markdown(
            f'<p class="step-label">📋 Routes — {total_h} / {total_b} houses</p>',
            unsafe_allow_html=True,
        )

        prev_car = -1
        for r in st.session_state.routes:
            s     = r["stats"]
            color = r["color"]
            ci    = r.get("car_idx", 0)

            if n_cars > 1 and ci != prev_car:
                st.markdown(f"**🚗 Car {ci + 1}**")
                prev_car = ci

            fits  = "✅" if s["fits_shift"] else "⚠️"
            over  = (f" (+{s['total_min'] - s['net_min']} min)"
                     if not s["fits_shift"] else "")

            end_d    = s.get("end_dist_m", "?")
            end_ok   = s.get("end_ok", True)
            end_icon = "✅" if end_ok else "⚠️"
            end_note = (
                f"<br>🅿️ End: {end_d} m to parking {end_icon}"
                if transport == "car"
                else f"<br>🏁 End: {end_d} m from start {end_icon}"
            )

            lr = (f"<br>↳ L: {r['left_count']} · R: {r['right_count']} houses (opposite sides)"
                  if r.get("left_count") is not None else "")

            dropped = r.get("dropped", 0)
            drop_note = (
                f'<br><span style="color:#888">↳ {dropped} house(s) dropped to fit shift</span>'
                if dropped else ""
            )

            st.markdown(
                f"""<div class="team-card" style="border-color:{color}">
                <b style="color:{color}">Team {r['team_idx'] + 1}</b>
                ({r['size']}p)<br>
                🏠 {len(r['houses'])} houses &nbsp;·&nbsp; 🚶 {s['distance_m']} m<br>
                ⏱️ {s['walk_min']} + {s['talk_min']} = <b>{s['total_min']} min</b>
                {fits}{over}{end_note}{lr}{drop_note}
                </div>""",
                unsafe_allow_html=True,
            )

            with st.expander(f"📋 Team {r['team_idx'] + 1} street plan"):
                streets: dict = {}
                for bld in r.get("house_data", []):
                    st_name = bld.get("name") or bld.get("street") or "Unknown street"
                    num = bld.get("housenumber", "")
                    streets.setdefault(st_name, []).append(num)
                if streets:
                    for st_name, nums in sorted(streets.items()):
                        nums_str = ", ".join(n for n in sorted(nums) if n) or "—"
                        st.caption(f"📍 **{st_name}**: {nums_str}")
                else:
                    st.caption("_No address details available._")

        if st.session_state.transit_stops:
            st.caption(f"🚌 {len(st.session_state.transit_stops)} transit stop(s) nearby")

        st.divider()
        st.caption("Display options")

        _detail_labels = {
            "full":   "Full roads",
            "most":   "Most roads",
            "major":  "Major roads only",
            "medium": "Simplified",
            "simple": "Waypoints only",
        }
        _detail_opts   = list(_detail_labels.keys())
        current_detail = st.session_state.get("route_detail", "most")
        if current_detail not in _detail_opts:
            current_detail = "most"
        new_detail = st.radio(
            "Route detail",
            options=_detail_opts,
            format_func=lambda x: _detail_labels[x],
            index=_detail_opts.index(current_detail),
            horizontal=True,
        )
        if new_detail != current_detail:
            st.session_state.route_detail = new_detail
            st.session_state.map_key += 1
            st.rerun()

        leg_lbl = "🗺️ Hide legend" if st.session_state.show_legend else "🗺️ Show legend"
        if st.button(leg_lbl, use_container_width=True):
            st.session_state.show_legend = not st.session_state.show_legend
            st.session_state.map_key += 1
            st.rerun()

    # ── Stats & reset ──────────────────────────────────────────────────────────

    if st.session_state.fetch_done:
        st.divider()
        st.metric("🏠 Houses found", len(st.session_state.buildings))

    if polygon is not None or st.session_state.fetch_done:
        st.divider()
        if st.button("🔄 Reset area & routes", use_container_width=True):
            old_wkt = polygon.wkt if polygon else None
            st.session_state.update(
                polygon              = None,
                buildings            = [],
                transit_stops        = [],
                fetch_done           = False,
                routes               = [],
                routes_done          = False,
                map_key              = st.session_state.map_key + 1,
                reset_polygon_wkt    = old_wkt,
                street_ways          = [],
                map_mode             = "area",
                manual_end           = None,
                car_parkings         = [],
                car_park_ids         = [],
                selecting_car        = 0,
                parking_spots        = [],
                selected_parking     = None,
                selected_parking_id  = None,
                show_parking_markers = False,
                show_legend          = True,
            )
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# MAP
# ══════════════════════════════════════════════════════════════════════════════

with map_col:

    latlng = st.session_state.center_latlng or [59.9139, 10.7522]

    if polygon:
        c           = polygon.centroid
        init_center = [c.y, c.x]
        a           = area_km2
        init_zoom   = 14 if a > 2 else 15 if a > 0.5 else 16 if a > 0.1 else 17
    else:
        init_center = latlng
        init_zoom   = 15

    m = folium.Map(location=init_center, zoom_start=init_zoom,
                   tiles="OpenStreetMap", prefer_canvas=True)

    if polygon:
        minx, miny, maxx, maxy = polygon.bounds
        m.fit_bounds([[miny, minx], [maxy, maxx]])

    if map_mode in ("set_start", "set_end"):
        draw_opts = {
            "polygon": False, "rectangle": False, "marker": True,
            "polyline": False, "circle": False, "circlemarker": False,
        }
    else:
        draw_opts = {
            "polygon": True, "rectangle": True, "marker": False,
            "polyline": False, "circle": False, "circlemarker": False,
        }
    Draw(export=False, draw_options=draw_opts,
         edit_options={"edit": False, "remove": True}).add_to(m)

    MeasureControl(position="bottomleft", primary_length_unit="meters").add_to(m)
    Geocoder(position="topleft", collapsed=True, add_marker=True).add_to(m)

    if map_mode in ("set_start", "set_end"):
        msg = ("📍 Drop a marker for the <b>start point</b>"
               if map_mode == "set_start"
               else "🏁 Drop a marker for the <b>end point</b>")
        m.get_root().html.add_child(folium.Element(f"""
        <div style="position:fixed;top:80px;left:50%;transform:translateX(-50%);
            z-index:9999;background:rgba(41,128,185,.93);color:#fff;
            padding:8px 18px;border-radius:20px;font-size:13px;font-weight:600;
            font-family:Arial,sans-serif;pointer-events:none;">
          {msg}
        </div>"""))

    # ── Pre-route markers ──────────────────────────────────────────────────────

    if not st.session_state.routes_done:
        if transport == "car":
            for ci, park_ll in enumerate(st.session_state.car_parkings):
                if park_ll:
                    folium.Marker(
                        location=park_ll,
                        tooltip=f"🚗 Car {ci + 1} parking",
                        icon=folium.Icon(
                            color=CAR_MARKER_COLORS[ci % len(CAR_MARKER_COLORS)],
                            icon="car", prefix="fa",
                        ),
                    ).add_to(m)

            if st.session_state.show_parking_markers:
                _fa_icon = {"parking": "car", "school": "graduation-cap",
                            "kindergarten": "child"}
                for sp in st.session_state.parking_spots:
                    is_sel = (st.session_state.selected_parking_id == sp["id"])
                    folium.Marker(
                        location=[sp["lat"], sp["lon"]],
                        tooltip=sp["id"],
                        icon=folium.Icon(
                            color="green" if is_sel else "gray",
                            icon=_fa_icon.get(sp["type"], "car"), prefix="fa",
                        ),
                    ).add_to(m)
        else:
            for i, s in enumerate(st.session_state.team_starts):
                lbl = ("All teams"
                       if st.session_state.same_start else f"Team {i + 1}")
                folium.Marker(
                    location=s,
                    tooltip=f"📍 {lbl} start",
                    icon=folium.Icon(color="blue", icon="play", prefix="fa"),
                ).add_to(m)
            if st.session_state.manual_end:
                folium.Marker(
                    location=st.session_state.manual_end,
                    tooltip="🏁 End point",
                    icon=folium.Icon(color="orange", icon="flag", prefix="fa"),
                ).add_to(m)

    # ── Route layers ──────────────────────────────────────────────────────────

    if st.session_state.routes_done and st.session_state.routes:
        detail = st.session_state.get("route_detail", "full")

        for r in st.session_state.routes:
            color     = r["color"]
            team_name = f"Team {r['team_idx'] + 1}"

            if r.get("contour"):
                folium.Polygon(
                    locations=r["contour"],
                    color=color, weight=1.5, opacity=0.5,
                    fill=True, fill_color=color, fill_opacity=0.07,
                    tooltip=f"{team_name} coverage zone",
                ).add_to(m)

            road_coords   = r.get("road_coords", [])
            road_segments = r.get("road_segments", [])

            if detail == "medium" and len(road_coords) > 2:
                polylines = [_douglas_peucker(road_coords, 15.0)]
            elif detail == "simple":
                pts_chain = ([r["start"]] if r.get("start") else []) + r.get("houses", [])
                polylines = [[ll for ll in pts_chain if ll]]
            elif detail in ("major", "most"):
                polylines = _filter_segments_by_priority(
                    road_segments, DETAIL_PRIORITY_THRESHOLDS[detail])
                if not polylines and road_coords:
                    polylines = [road_coords]
            else:
                polylines = [road_coords] if road_coords else []

            for pl in polylines:
                if len(pl) >= 2:
                    folium.PolyLine(
                        locations=pl,
                        color=color, weight=3.5, opacity=0.85,
                        tooltip=f"{team_name} — {len(r['houses'])} houses",
                    ).add_to(m)
                    _add_route_arrows(m, pl, color)

            if r["start"]:
                ci = r.get("car_idx", 0)
                folium.Marker(
                    location=r["start"],
                    tooltip=f"{team_name} ▶ {r['start_label']}",
                    icon=folium.Icon(
                        color=CAR_MARKER_COLORS[ci % len(CAR_MARKER_COLORS)]
                              if transport == "car" else "blue",
                        icon="car"  if transport == "car" else "play",
                        prefix="fa",
                    ),
                ).add_to(m)

            end = r.get("end")
            if end and end != r["start"]:
                e_type = r.get("end_type", "start")
                folium.Marker(
                    location=end,
                    tooltip=f"{team_name} ■ {r['end_label']}",
                    icon=folium.Icon(
                        color="red"    if e_type == "transit" else
                              "green"  if e_type == "parking" else "orange",
                        icon="bus"     if e_type == "transit" else
                             "car"     if e_type == "parking" else "flag",
                        prefix="fa",
                    ),
                ).add_to(m)

    # ── Legend ─────────────────────────────────────────────────────────────────

    if (st.session_state.routes_done
            and st.session_state.routes
            and st.session_state.show_legend):
        rows = ""
        for r in st.session_state.routes:
            c = r["color"]
            rows += (
                f'<span style="color:{c};font-size:15px;">━</span> '
                f'<span style="color:#111;">Team {r["team_idx"]+1} '
                f'({r["size"]}p) — {len(r["houses"])} houses</span><br>'
            )
        rows += (
            '<span style="color:#27ae60;">▶</span> '
            '<span style="color:#111;">Start</span>&nbsp;&nbsp;'
            '<span style="color:#e74c3c;">■</span> '
            '<span style="color:#111;">End / transit</span>&nbsp;&nbsp;'
            '<span style="color:#555;font-size:11px;">▲</span> '
            '<span style="color:#111;">Direction</span><br>'
        )
        m.get_root().html.add_child(folium.Element(f"""
        <div style="position:fixed;bottom:36px;right:10px;z-index:9999;
            background:rgba(255,255,255,.97);padding:10px 14px;border-radius:8px;
            border:1px solid #bbb;font-size:12px;line-height:1.8;
            box-shadow:2px 2px 6px rgba(0,0,0,.18);font-family:Arial,sans-serif;
            max-width:230px;">
          <b>Legend</b><br>{rows}
        </div>"""))

    # ── Render ─────────────────────────────────────────────────────────────────

    map_out = st_folium(
        m,
        key=f"map_{st.session_state.map_key}",
        use_container_width=True,
        height=640,
        returned_objects=[
            "last_active_drawing", "last_clicked",
            "last_geocoder_result", "last_object_clicked_tooltip", "bounds",
        ],
    )

    # ── Process map output ─────────────────────────────────────────────────────

    if map_out:

        if map_out.get("bounds"):
            st.session_state.map_bounds = map_out["bounds"]

        geo = map_out.get("last_geocoder_result")
        if geo:
            try:
                c   = geo.get("center") or geo
                lat = float(c.get("lat") or c.get("y"))
                lon = float(c.get("lng") or c.get("x"))
                st.session_state.center_latlng = [lat, lon]
                st.session_state.map_key += 1
                st.rerun()
            except Exception:
                pass

        tooltip_clicked = map_out.get("last_object_clicked_tooltip")
        if (tooltip_clicked
                and str(tooltip_clicked).startswith("park_")
                and not st.session_state.routes_done
                and st.session_state.show_parking_markers):
            spot_id = str(tooltip_clicked)
            ci      = st.session_state.selecting_car
            _cp = list(st.session_state.car_parkings)
            _ci = list(st.session_state.car_park_ids)
            while len(_cp) <= ci:
                _cp.append(None)
                _ci.append(None)
            if st.session_state.selected_parking_id == spot_id:
                _cp[ci] = None
                _ci[ci] = None
                st.session_state.selected_parking_id = None
                st.session_state.selected_parking    = None
            else:
                match = next((sp for sp in st.session_state.parking_spots
                              if sp["id"] == spot_id), None)
                if match:
                    _cp[ci] = [match["lat"], match["lon"]]
                    _ci[ci] = spot_id
                    st.session_state.selected_parking_id = spot_id
                    st.session_state.selected_parking    = _cp[ci]
            st.session_state.car_parkings = _cp
            st.session_state.car_park_ids = _ci
            st.rerun()

        drawing   = map_out.get("last_active_drawing")
        geom_type = drawing.get("geometry", {}).get("type", "") if drawing else ""

        if geom_type == "Point" and map_mode in ("set_start", "set_end"):
            coords = drawing["geometry"]["coordinates"]
            ll = [coords[1], coords[0]]
            if map_mode == "set_start":
                n_needed = 1 if st.session_state.same_start else n_t
                if len(st.session_state.team_starts) < n_needed:
                    st.session_state.team_starts = st.session_state.team_starts + [ll]
            else:
                st.session_state.manual_end = ll
            st.session_state.map_mode = "area"
            st.session_state.map_key += 1
            st.rerun()

        elif geom_type in ("Polygon", "MultiPolygon"):
            try:
                new_poly = shape(drawing["geometry"])
                new_wkt  = new_poly.wkt
                if (new_wkt != st.session_state.reset_polygon_wkt
                        and new_poly != st.session_state.polygon):
                    st.session_state.polygon           = new_poly
                    st.session_state.buildings         = []
                    st.session_state.transit_stops     = []
                    st.session_state.fetch_done        = False
                    st.session_state.routes            = []
                    st.session_state.routes_done       = False
                    st.session_state.reset_polygon_wkt = None
                    st.rerun()
            except Exception:
                pass
