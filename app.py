"""
Fundraising Route Planner — Phase 2
Area selection · house detection · walkable street display
Route planning · team splitting · parking & transit detection.
"""

import math
import random
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw, MeasureControl, FastMarkerCluster
from shapely.geometry import shape, Point
from shapely import wkt as shapely_wkt

# ── Page config ────────────────────────────────────────────────────────────────
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

USER_AGENT = "FundraisingRoutePlanner/2.0 (https://streamlit.io)"
MAX_AREA_KM2    = 4.0
REQUEST_TIMEOUT = 60

WALKABLE_HIGHWAYS = (
    "footway|path|pedestrian|residential|living_street|service|"
    "tertiary|tertiary_link|secondary|secondary_link|primary|"
    "primary_link|unclassified|cycleway|steps"
)

BUILDING_LABELS = {
    "yes":                "Generic building",
    "house":              "House",
    "residential":        "Residential",
    "detached":           "Detached house",
    "apartments":         "Apartments",
    "terrace":            "Terraced house",
    "semidetached_house": "Semi-detached house",
    "bungalow":           "Bungalow",
    "cabin":              "Cabin",
    "farm":               "Farm",
    "dormitory":          "Dormitory",
    "duplex":             "Duplex",
    "address_node":       "Address point",
}

HIGHWAY_COLORS = {
    "primary":         "#e74c3c",
    "primary_link":    "#e74c3c",
    "secondary":       "#e67e22",
    "secondary_link":  "#e67e22",
    "tertiary":        "#f39c12",
    "tertiary_link":   "#f39c12",
    "residential":     "#2980b9",
    "unclassified":    "#2980b9",
    "living_street":   "#8e44ad",
    "footway":         "#27ae60",
    "path":            "#27ae60",
    "steps":           "#16a085",
    "pedestrian":      "#1abc9c",
    "cycleway":        "#9b59b6",
    "service":         "#95a5a6",
}
DEFAULT_HIGHWAY_COLOR = "#2980b9"

# One colour per team (up to 8 teams)
TEAM_COLORS = [
    "#e74c3c",  # red
    "#3498db",  # blue
    "#2ecc71",  # green
    "#f39c12",  # orange
    "#9b59b6",  # purple
    "#1abc9c",  # teal
    "#e67e22",  # amber
    "#34495e",  # slate
]

WALKING_SPEED_KMH = 4.0   # average door-to-door walking pace
EMPTY_HOUSE_RATE  = 0.35  # fraction of doors where nobody answers
EMPTY_DOOR_SEC    = 20    # seconds spent at an unanswered door

DEFAULTS = {
    "buildings":         [],
    "streets_geojson":   None,
    "polygon":           None,
    "fetch_done":        False,
    "map_key":           0,
    "selected_types":    [],
    "reset_polygon_wkt": None,
    "parking_spots":     [],
    "transit_stops":     [],
    "routes":            [],
    "routes_done":       False,
}


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def label(raw: str) -> str:
    return BUILDING_LABELS.get(raw, raw.replace("_", " ").capitalize())


def approximate_area_km2(polygon) -> float:
    if polygon is None:
        return 0.0
    centroid = polygon.centroid
    lat_km = 111.32
    lon_km = 111.32 * math.cos(math.radians(centroid.y))
    return polygon.area * lat_km * lon_km


def haversine_m(a, b) -> float:
    """Straight-line distance in metres between two [lat, lon] points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def overpass_query(polygon) -> str:
    """Combined query: buildings, address nodes, walkable streets, parking, transit."""
    minx, miny, maxx, maxy = polygon.bounds
    s, w, n, e = miny, minx, maxy, maxx
    return f"""
[out:json][timeout:{REQUEST_TIMEOUT}];
(
  way["building"]({s},{w},{n},{e});
  node["addr:housenumber"]({s},{w},{n},{e});
  way["highway"~"^({WALKABLE_HIGHWAYS})$"]({s},{w},{n},{e});
  node["amenity"="parking"]["access"!="private"]({s},{w},{n},{e});
  way["amenity"="parking"]["access"!="private"]({s},{w},{n},{e});
  node["highway"="bus_stop"]({s},{w},{n},{e});
  node["railway"~"^(tram_stop|station|halt)$"]({s},{w},{n},{e});
  node["amenity"="bus_station"]({s},{w},{n},{e});
);
out geom tags;
""".strip()


def fetch_overpass(polygon):
    """Try Overpass mirrors in order. Returns (data_dict, used_url, err_str)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    query   = overpass_query(polygon)
    last_err = None
    for url in OVERPASS_MIRRORS:
        host = url.split("/")[2]
        try:
            r = requests.post(
                url, data={"data": query}, headers=headers, timeout=REQUEST_TIMEOUT
            )
            if r.status_code == 200:
                return r.json(), url, None
            last_err = f"HTTP {r.status_code} from {host}"
        except requests.exceptions.Timeout:
            last_err = f"Timeout from {host}"
        except Exception as ex:
            last_err = f"{type(ex).__name__} from {host}: {ex}"
    return None, None, last_err


def parse_overpass(data, polygon):
    """Parse Overpass JSON → (buildings, streets GeoJSON, parking list, transit list)."""
    if not data or "elements" not in data:
        return [], {"type": "FeatureCollection", "features": []}, [], []

    buildings = []
    seen      = set()
    streets   = []
    parking   = []
    transit   = []

    for el in data["elements"]:
        tags  = el.get("tags", {})
        etype = el.get("type")

        # ── Way elements ──────────────────────────────────────────────────────
        if etype == "way":
            geom = el.get("geometry", [])
            if not geom:
                continue

            if "highway" in tags:
                coords = [[n["lon"], n["lat"]] for n in geom]
                if len(coords) >= 2:
                    streets.append({
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": coords},
                        "properties": {
                            "highway": tags.get("highway", ""),
                            "name":    tags.get("name", ""),
                        },
                    })

            elif "building" in tags:
                lats = [n["lat"] for n in geom]
                lons = [n["lon"] for n in geom]
                lat  = sum(lats) / len(lats)
                lon  = sum(lons) / len(lons)
                key  = (round(lat, 5), round(lon, 5))
                if key in seen or not polygon.contains(Point(lon, lat)):
                    continue
                seen.add(key)
                buildings.append({
                    "lat":         lat,
                    "lon":         lon,
                    "type":        tags.get("building") or "yes",
                    "housenumber": tags.get("addr:housenumber", ""),
                    "street":      tags.get("addr:street", ""),
                    "name":        tags.get("name", ""),
                })

            elif tags.get("amenity") == "parking":
                fee = tags.get("fee", "no").lower()
                if fee not in ("yes", "paid"):
                    lats = [n["lat"] for n in geom]
                    lons = [n["lon"] for n in geom]
                    parking.append({
                        "lat":  sum(lats) / len(lats),
                        "lon":  sum(lons) / len(lons),
                        "name": tags.get("name", "Free parking"),
                        "fee":  fee,
                    })

        # ── Node elements ─────────────────────────────────────────────────────
        elif etype == "node":
            lat = el.get("lat")
            lon = el.get("lon")
            if lat is None or lon is None:
                continue

            if "addr:housenumber" in tags:
                key = (round(lat, 5), round(lon, 5))
                if key not in seen and polygon.contains(Point(lon, lat)):
                    seen.add(key)
                    buildings.append({
                        "lat":         lat,
                        "lon":         lon,
                        "type":        "address_node",
                        "housenumber": tags.get("addr:housenumber", ""),
                        "street":      tags.get("addr:street", ""),
                        "name":        "",
                    })

            elif tags.get("amenity") == "parking":
                fee = tags.get("fee", "no").lower()
                if fee not in ("yes", "paid") and polygon.contains(Point(lon, lat)):
                    parking.append({
                        "lat":  lat,
                        "lon":  lon,
                        "name": tags.get("name", "Free parking"),
                        "fee":  fee,
                    })

            elif (
                tags.get("highway") == "bus_stop"
                or tags.get("railway") in ("tram_stop", "station", "halt")
                or tags.get("amenity") == "bus_station"
            ):
                stop_type = (
                    tags.get("railway")
                    or tags.get("highway")
                    or tags.get("amenity")
                    or "stop"
                )
                transit.append({
                    "lat":  lat,
                    "lon":  lon,
                    "name": tags.get("name", stop_type.replace("_", " ").title()),
                    "type": stop_type,
                })

    streets_fc = {"type": "FeatureCollection", "features": streets}
    return buildings, streets_fc, parking, transit


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE PLANNING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def num_teams(n_people: int) -> int:
    """Pairs round up: 1-2 → 1, 3-4 → 2, 5-6 → 3, 7-8 → 4, …"""
    return max(1, math.ceil(n_people / 2))


def team_compositions(n_people: int) -> list:
    """Return list of per-team sizes (e.g. 5 people → [2, 2, 1])."""
    n_t   = num_teams(n_people)
    extra = n_people - 2 * (n_t - 1)
    return [2] * (n_t - 1) + [max(1, extra)]


def compute_break_minutes(shift_minutes: int) -> int:
    """30-min break for every 2 hours of shift time."""
    return (shift_minutes // 120) * 30


def haversine_m_pair(a, b) -> float:
    return haversine_m(a, b)


def simple_kmeans(points: list, k: int, max_iter: int = 40, seed: int = 42) -> list:
    """
    Minimal k-means on [[lat, lon], …] points.
    Returns a list of k clusters, each a list of point indices.
    """
    n = len(points)
    if k >= n:
        return [[i] for i in range(n)]

    rng        = random.Random(seed)
    cent_idxs  = rng.sample(range(n), k)
    centroids  = [points[i][:] for i in cent_idxs]
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
            cluster_pts = [points[i] for i in range(n) if assignments[i] == ci]
            if cluster_pts:
                centroids[ci] = [
                    sum(p[0] for p in cluster_pts) / len(cluster_pts),
                    sum(p[1] for p in cluster_pts) / len(cluster_pts),
                ]

    clusters = [[] for _ in range(k)]
    for i, ci in enumerate(assignments):
        clusters[ci].append(i)
    return clusters


def _balance_clusters(pts: list, clusters: list, k: int, max_iter: int = 20) -> list:
    """Iteratively move points from the largest to the smallest cluster."""
    for _ in range(max_iter):
        sizes  = [len(c) for c in clusters]
        max_ci = max(range(k), key=lambda i: sizes[i])
        min_ci = min(range(k), key=lambda i: sizes[i])
        if sizes[max_ci] - sizes[min_ci] <= 1:
            break
        if not clusters[min_ci]:
            continue
        min_centroid = [
            sum(pts[i][0] for i in clusters[min_ci]) / len(clusters[min_ci]),
            sum(pts[i][1] for i in clusters[min_ci]) / len(clusters[min_ci]),
        ]
        move_pos = min(
            range(len(clusters[max_ci])),
            key=lambda j: haversine_m(pts[clusters[max_ci][j]], min_centroid),
        )
        clusters[min_ci].append(clusters[max_ci].pop(move_pos))
    return clusters


def nearest_neighbor_route(points: list, start_point=None) -> list:
    """
    Greedy nearest-neighbour TSP approximation.
    Returns ordered list of indices into `points`.
    """
    if not points:
        return []
    unvisited = list(range(len(points)))
    first = (
        min(unvisited, key=lambda i: haversine_m(start_point, points[i]))
        if start_point
        else 0
    )
    route = [first]
    unvisited.remove(first)
    while unvisited:
        last    = route[-1]
        nearest = min(unvisited, key=lambda i: haversine_m(points[last], points[i]))
        route.append(nearest)
        unvisited.remove(nearest)
    return route


def route_total_distance_m(ordered_points: list) -> float:
    total = 0.0
    for i in range(1, len(ordered_points)):
        total += haversine_m(ordered_points[i - 1], ordered_points[i])
    return total


def estimate_route_time(n_houses: int, distance_m: float, time_per_door_min: float) -> dict:
    occupied = round(n_houses * (1 - EMPTY_HOUSE_RATE))
    empty    = n_houses - occupied
    talk_min = occupied * time_per_door_min + empty * (EMPTY_DOOR_SEC / 60)
    walk_min = (distance_m / 1000) / WALKING_SPEED_KMH * 60
    return {
        "walk_min":   round(walk_min),
        "talk_min":   round(talk_min),
        "total_min":  round(talk_min + walk_min),
        "occupied":   occupied,
        "empty":      empty,
        "distance_m": round(distance_m),
    }


def _nearest_from_list(cluster_centroid: list, candidates: list) -> dict | None:
    if not candidates:
        return None
    return min(candidates, key=lambda c: haversine_m(cluster_centroid, [c["lat"], c["lon"]]))


def find_best_start(cluster_pts: list, parking: list, transit: list):
    if not cluster_pts:
        return None, "Start", "manual"
    centroid = [
        sum(p[0] for p in cluster_pts) / len(cluster_pts),
        sum(p[1] for p in cluster_pts) / len(cluster_pts),
    ]
    p = _nearest_from_list(centroid, parking)
    if p:
        return [p["lat"], p["lon"]], p.get("name", "Free parking"), "parking"
    t = _nearest_from_list(centroid, transit)
    if t:
        return [t["lat"], t["lon"]], t.get("name", "Transit stop"), "transit"
    return cluster_pts[0], "Start of area", "manual"


def find_best_end(ordered_pts: list, transit: list, start_latlng) -> tuple:
    if not ordered_pts:
        return None, "End", "manual"
    last = ordered_pts[-1]
    t    = _nearest_from_list(last, transit)
    if t and haversine_m(last, [t["lat"], t["lon"]]) < 800:
        return [t["lat"], t["lon"]], t.get("name", "Transit stop"), "transit"
    if start_latlng:
        return start_latlng, "Return to start", "start"
    return last, "End of route", "manual"


def plan_routes(
    buildings:         list,
    parking_spots:     list,
    transit_stops:     list,
    n_people:          int,
    shift_minutes:     int,
    time_per_door_min: float,
    coverage:          float = 0.90,
) -> list:
    """
    Plan routes for all teams. Returns a list of team dicts.
    Each dict: team_idx, size, color, houses, house_data,
               start/end info, stats dict.
    """
    if not buildings:
        return []

    target_n  = max(1, round(len(buildings) * coverage))
    buildings = buildings[:target_n]

    n_t   = num_teams(n_people)
    sizes = team_compositions(n_people)
    pts   = [[b["lat"], b["lon"]] for b in buildings]

    clusters = [list(range(len(pts)))] if n_t == 1 else simple_kmeans(pts, n_t)
    clusters = _balance_clusters(pts, clusters, n_t)

    break_min   = compute_break_minutes(shift_minutes)
    net_minutes = shift_minutes - break_min

    result = []
    for ti, (idxs, size) in enumerate(zip(clusters, sizes)):
        if not idxs:
            continue

        cluster_pts  = [pts[i]        for i in idxs]
        cluster_blds = [buildings[i]  for i in idxs]

        start_latlng, start_label, start_type = find_best_start(
            cluster_pts, parking_spots, transit_stops
        )

        route_order  = nearest_neighbor_route(cluster_pts, start_latlng)
        ordered_pts  = [cluster_pts[i]  for i in route_order]
        ordered_blds = [cluster_blds[i] for i in route_order]

        waypoints = (
            ([start_latlng] if start_latlng else [])
            + ordered_pts
        )
        end_latlng, end_label, end_type = find_best_end(
            ordered_pts, transit_stops, start_latlng
        )
        if end_latlng:
            waypoints.append(end_latlng)

        dist_m = route_total_distance_m(waypoints)
        stats  = estimate_route_time(len(ordered_pts), dist_m, time_per_door_min)
        stats.update({
            "break_min":  break_min,
            "net_min":    net_minutes,
            "fits_shift": stats["total_min"] <= net_minutes,
        })

        result.append({
            "team_idx":    ti,
            "size":        size,
            "color":       TEAM_COLORS[ti % len(TEAM_COLORS)],
            "houses":      ordered_pts,
            "house_data":  ordered_blds,
            "start":       start_latlng,
            "start_label": start_label,
            "start_type":  start_type,
            "end":         end_latlng,
            "end_label":   end_label,
            "end_type":    end_type,
            "stats":       stats,
        })

    return result


# ══════════════════════════════════════════════════════════════════════════════
# CACHED FETCH
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False, ttl=3600, max_entries=20)
def fetch_all(polygon_wkt: str):
    """Fetch buildings, streets, parking and transit in one shot. Cached by WKT."""
    polygon = shapely_wkt.loads(polygon_wkt)
    raw, mirror, err = fetch_overpass(polygon)
    if err:
        return {"error": err}
    buildings, streets, parking, transit = parse_overpass(raw, polygon)
    return {
        "buildings":       buildings,
        "streets_geojson": streets,
        "parking_spots":   parking,
        "transit_stops":   transit,
        "mirror":          mirror,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SESSION STATE
# ══════════════════════════════════════════════════════════════════════════════

for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

if "toasted_ready" not in st.session_state:
    st.session_state.toasted_ready = True
    st.toast("✅ App ready", icon="🗺️")


# ══════════════════════════════════════════════════════════════════════════════
# STYLES + HEADER
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<style>
  .main-title  { font-size:2rem; font-weight:700; color:#1a1a2e; margin-bottom:0; }
  .sub-title   { color:#666; font-size:.9rem; margin-top:0; margin-bottom:1.5rem; }
  .step-label  { font-weight:600; font-size:.9rem; color:#333; margin-bottom:.2rem; }
  .phase-badge { background:#2980b9; color:#fff; padding:2px 8px;
                 border-radius:12px; font-size:.75rem; font-weight:600; }
  .team-card   { border-radius:8px; padding:10px 12px; margin-bottom:8px;
                 border-left:4px solid; background:rgba(0,0,0,0.03); font-size:.82rem; }
</style>
""", unsafe_allow_html=True)

st.markdown('<p class="main-title">🗺️ Fundraising Route Planner</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub-title"><span class="phase-badge">Phase 2</span>'
    '&nbsp; Area · Houses · Streets · Route planning · Team splitting · Parking & transit</p>',
    unsafe_allow_html=True,
)


# ══════════════════════════════════════════════════════════════════════════════
# LAYOUT
# ══════════════════════════════════════════════════════════════════════════════

map_col, ctrl_col = st.columns([3, 1])


# ══════════════════════════════════════════════════════════════════════════════
# CONTROL PANEL
# ══════════════════════════════════════════════════════════════════════════════

with ctrl_col:

    # ── Step 1 ────────────────────────────────────────────────────────────────
    st.markdown('<p class="step-label">Step 1 — Draw your area</p>', unsafe_allow_html=True)
    st.caption(f"Use ◻ rectangle or ⬡ polygon. Max area: {MAX_AREA_KM2} km².")
    st.divider()

    # ── Step 2 ────────────────────────────────────────────────────────────────
    st.markdown('<p class="step-label">Step 2 — Fetch map data</p>', unsafe_allow_html=True)

    polygon  = st.session_state.polygon
    area_km2 = approximate_area_km2(polygon)

    if polygon:
        if area_km2 > MAX_AREA_KM2:
            st.error(f"Area too large: {area_km2:.2f} km² > {MAX_AREA_KM2} km². Draw a smaller area.")
        else:
            st.success(f"Area selected (~{area_km2:.3f} km²)")
    else:
        st.info("No area drawn yet.")

    fetch_disabled = polygon is None or area_km2 > MAX_AREA_KM2

    if st.button(
        "🔍 Fetch Houses & Streets",
        type="primary",
        disabled=fetch_disabled,
        use_container_width=True,
    ):
        with st.spinner("Fetching from OpenStreetMap…"):
            result = fetch_all(polygon.wkt)

        if "error" in result:
            st.error(f"Fetch failed: {result['error']}\n\nTry again or use a smaller area.")
        else:
            st.session_state.buildings       = result["buildings"]
            st.session_state.streets_geojson = result["streets_geojson"]
            st.session_state.parking_spots   = result.get("parking_spots", [])
            st.session_state.transit_stops   = result.get("transit_stops", [])
            st.session_state.selected_types  = sorted({b["type"] for b in result["buildings"]})
            st.session_state.fetch_done      = True
            st.session_state.routes          = []
            st.session_state.routes_done     = False
            mirror = result.get("mirror") or ""
            host   = mirror.split("/")[2] if mirror else "?"
            n_park = len(st.session_state.parking_spots)
            n_tran = len(st.session_state.transit_stops)
            st.success(
                f"Found {len(result['buildings'])} buildings · "
                f"{n_park} free parking · {n_tran} transit stops"
            )
            st.rerun()

    # ── Building type filter ──────────────────────────────────────────────────
    if st.session_state.fetch_done and st.session_state.buildings:
        st.divider()
        st.markdown('<p class="step-label">🏠 Filter building types</p>', unsafe_allow_html=True)

        all_types   = sorted({b["type"] for b in st.session_state.buildings})
        type_counts = {t: sum(1 for b in st.session_state.buildings if b["type"] == t)
                       for t in all_types}
        new_selected = []
        for t in all_types:
            if st.checkbox(
                f"{label(t)}  ({type_counts[t]})",
                value=(t in st.session_state.selected_types),
                key=f"chk_{t}",
            ):
                new_selected.append(t)

        if new_selected != st.session_state.selected_types:
            st.session_state.selected_types = new_selected
            st.session_state.routes         = []
            st.session_state.routes_done    = False
            st.rerun()

    # ── Step 3: Route planning ────────────────────────────────────────────────
    if st.session_state.fetch_done and st.session_state.buildings:
        st.divider()
        st.markdown('<p class="step-label">Step 3 — Plan routes</p>', unsafe_allow_html=True)

        n_people = st.slider(
            "👥 Total people",
            min_value=1, max_value=12,
            value=st.session_state.get("rp_n_people", 2),
            step=1,
            key="rp_n_people",
        )
        n_t_preview = num_teams(n_people)
        sizes_prev  = team_compositions(n_people)
        team_str    = " + ".join(f"{s}p" for s in sizes_prev)
        st.caption(f"→ {n_t_preview} team{'s' if n_t_preview > 1 else ''}: {team_str}")

        shift_hours = st.slider(
            "⏱️ Shift duration (hours)",
            min_value=1.0, max_value=10.0,
            value=st.session_state.get("rp_shift_hours", 4.0),
            step=0.5,
            key="rp_shift_hours",
        )
        shift_min = int(shift_hours * 60)
        brk_min   = compute_break_minutes(shift_min)
        net_min   = shift_min - brk_min
        st.caption(
            f"→ {shift_min} min total · {brk_min} min breaks "
            f"· **{net_min} min active**"
        )

        time_per_door = st.slider(
            "🚪 Avg. time per household (min)",
            min_value=1.0, max_value=15.0,
            value=st.session_state.get("rp_time_per_door", 3.0),
            step=0.5,
            key="rp_time_per_door",
            help=(
                f"Average time spent at answered doors. "
                f"~{round(EMPTY_HOUSE_RATE * 100)}% of doors will be empty "
                f"(counted separately as {EMPTY_DOOR_SEC}s each)."
            ),
        )

        if st.button("🗺️ Plan Routes", type="primary", use_container_width=True):
            visible_blds = [
                b for b in st.session_state.buildings
                if b["type"] in st.session_state.selected_types
            ]
            if not visible_blds:
                st.warning("No buildings visible with current filters.")
            else:
                with st.spinner("Planning optimal routes…"):
                    routes = plan_routes(
                        buildings         = visible_blds,
                        parking_spots     = st.session_state.parking_spots,
                        transit_stops     = st.session_state.transit_stops,
                        n_people          = n_people,
                        shift_minutes     = shift_min,
                        time_per_door_min = time_per_door,
                        coverage          = 0.90,
                    )
                st.session_state.routes      = routes
                st.session_state.routes_done = True
                st.rerun()

    # ── Route summaries ───────────────────────────────────────────────────────
    if st.session_state.routes_done and st.session_state.routes:
        st.divider()
        st.markdown('<p class="step-label">📋 Route summaries</p>', unsafe_allow_html=True)
        for r in st.session_state.routes:
            color = r["color"]
            stats = r["stats"]
            fits  = "✅" if stats["fits_shift"] else "⚠️"
            over  = "" if stats["fits_shift"] else f" (+{stats['total_min'] - stats['net_min']}min over)"
            size_label = f"{r['size']} person{'s' if r['size'] > 1 else ''}"
            st.markdown(
                f"""<div class="team-card" style="border-color:{color};">
<b style="color:{color};">Team {r['team_idx']+1}</b> &nbsp;({size_label})<br>
🏠 {len(r['houses'])} houses &nbsp;·&nbsp; 🚶 {stats['distance_m']}m<br>
⏱️ Walk {stats['walk_min']}min + Talk {stats['talk_min']}min
= <b>{stats['total_min']}min</b> {fits}{over}<br>
👤 ~{stats['occupied']} answered · ~{stats['empty']} empty<br>
📍 <b>Start:</b> {r['start_label'] or '—'}<br>
🏁 <b>End:</b> {r['end_label'] or '—'}
</div>""",
                unsafe_allow_html=True,
            )

    # ── Stats ─────────────────────────────────────────────────────────────────
    if st.session_state.fetch_done:
        st.divider()
        st.markdown('<p class="step-label">📊 Stats</p>', unsafe_allow_html=True)
        visible = [
            b for b in st.session_state.buildings
            if b["type"] in st.session_state.selected_types
        ]
        st.metric("🏠 Visible buildings",   len(visible))
        st.metric("📦 Total detected",      len(st.session_state.buildings))
        if st.session_state.streets_geojson:
            st.metric("🛣️ Street segments",
                      len(st.session_state.streets_geojson.get("features", [])))
        if st.session_state.parking_spots:
            st.metric("🅿️ Free parking spots", len(st.session_state.parking_spots))
        if st.session_state.transit_stops:
            st.metric("🚌 Transit stops",       len(st.session_state.transit_stops))

    # ── Roadmap ───────────────────────────────────────────────────────────────
    st.divider()
    st.markdown('<p class="step-label">📍 Roadmap</p>', unsafe_allow_html=True)
    poly   = polygon is not None
    done   = st.session_state.fetch_done
    routed = st.session_state.routes_done
    st.markdown(f"""
{"✅" if poly   else "⏳"} Area selection
{"✅" if done   else "⏳"} House detection
{"✅" if done   else "⏳"} Street display
{"✅" if done   else "⏳"} Parking & transit detection
{"✅" if routed else "⏳"} Route planning
{"✅" if routed else "⏳"} Team splitting
""")

    # ── Reset ─────────────────────────────────────────────────────────────────
    if poly or done:
        st.divider()
        if st.button("🔄 Reset everything", use_container_width=True):
            old_wkt = polygon.wkt if polygon else None
            new_key = st.session_state.map_key + 1
            for k, v in DEFAULTS.items():
                st.session_state[k] = v
            st.session_state.map_key           = new_key
            st.session_state.reset_polygon_wkt = old_wkt
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# MAP
# ══════════════════════════════════════════════════════════════════════════════

with map_col:

    if st.session_state.polygon:
        c           = st.session_state.polygon.centroid
        init_center = [c.y, c.x]
        a = approximate_area_km2(st.session_state.polygon)
        if   a > 2.0: init_zoom = 14
        elif a > 0.5: init_zoom = 15
        elif a > 0.1: init_zoom = 16
        else:         init_zoom = 17
    else:
        init_center = [59.9139, 10.7522]  # Oslo
        init_zoom   = 14

    m = folium.Map(
        location=init_center,
        zoom_start=init_zoom,
        tiles="OpenStreetMap",
        prefer_canvas=True,
    )

    if st.session_state.polygon:
        minx, miny, maxx, maxy = st.session_state.polygon.bounds
        m.fit_bounds([[miny, minx], [maxy, maxx]])

    Draw(
        export=False,
        draw_options={
            "polyline":     False,
            "polygon":      True,
            "circle":       False,
            "marker":       False,
            "circlemarker": False,
            "rectangle":    True,
        },
        edit_options={"edit": False, "remove": True},
    ).add_to(m)

    MeasureControl(position="bottomleft", primary_length_unit="meters").add_to(m)

    # Streets
    if st.session_state.streets_geojson and st.session_state.streets_geojson.get("features"):
        def style_fn(feature):
            htype = feature["properties"].get("highway", "")
            return {
                "color":   HIGHWAY_COLORS.get(htype, DEFAULT_HIGHWAY_COLOR),
                "weight":  2.5,
                "opacity": 0.85,
            }
        folium.GeoJson(
            st.session_state.streets_geojson,
            name="streets",
            style_function=style_fn,
            tooltip=folium.GeoJsonTooltip(
                fields=["highway", "name"], aliases=["Type:", "Name:"]
            ),
        ).add_to(m)

    # ── ROUTES MODE ───────────────────────────────────────────────────────────
    if st.session_state.routes_done and st.session_state.routes:
        for r in st.session_state.routes:
            color     = r["color"]
            team_name = f"Team {r['team_idx'] + 1}"

            # Dashed polyline through all houses
            if len(r["houses"]) >= 2:
                folium.PolyLine(
                    locations=r["houses"],
                    color=color,
                    weight=3.5,
                    opacity=0.75,
                    tooltip=f"{team_name} route ({len(r['houses'])} houses)",
                    dash_array="8 5",
                ).add_to(m)

            # House markers
            for idx, (pt, bld) in enumerate(zip(r["houses"], r["house_data"])):
                addr   = bld.get("housenumber", "")
                street = bld.get("street", "")
                label_str = f"{street} {addr}".strip() or "Building"
                folium.CircleMarker(
                    location=pt,
                    radius=5,
                    color=color,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.85,
                    weight=1,
                    tooltip=f"{team_name} #{idx + 1}: {label_str}",
                ).add_to(m)

            # Start marker
            if r["start"]:
                s_icon  = "car"   if r["start_type"] == "parking" else "play"
                s_color = "green" if r["start_type"] == "parking" else "blue"
                folium.Marker(
                    location=r["start"],
                    tooltip=f"{team_name} — Start: {r['start_label']}",
                    icon=folium.Icon(color=s_color, icon=s_icon, prefix="fa"),
                ).add_to(m)

            # End marker (only if different from start)
            if r["end"] and r["end"] != r["start"]:
                e_icon  = "bus"    if r["end_type"] == "transit" else "flag"
                e_color = "red"    if r["end_type"] == "transit" else "orange"
                folium.Marker(
                    location=r["end"],
                    tooltip=f"{team_name} — End: {r['end_label']}",
                    icon=folium.Icon(color=e_color, icon=e_icon, prefix="fa"),
                ).add_to(m)

    # ── NO-ROUTES MODE ────────────────────────────────────────────────────────
    else:
        # Plain clustered building markers
        if st.session_state.buildings and st.session_state.selected_types:
            visible = [
                [b["lat"], b["lon"]]
                for b in st.session_state.buildings
                if b["type"] in st.session_state.selected_types
            ]
            if visible:
                callback = """
                function (row) {
                    return L.circleMarker(
                        new L.LatLng(row[0], row[1]),
                        { color: "#c0392b", fillColor: "#e74c3c",
                          fillOpacity: 0.85, weight: 1, radius: 5 }
                    );
                }
                """
                FastMarkerCluster(
                    data=visible,
                    callback=callback,
                    options={"maxClusterRadius": 40, "disableClusteringAtZoom": 17},
                ).add_to(m)

        # Free parking markers
        for p in st.session_state.parking_spots:
            folium.Marker(
                location=[p["lat"], p["lon"]],
                tooltip=f"🅿️ {p.get('name', 'Free parking')}",
                icon=folium.Icon(color="green", icon="car", prefix="fa"),
            ).add_to(m)

        # Transit stop markers
        for t in st.session_state.transit_stops:
            folium.Marker(
                location=[t["lat"], t["lon"]],
                tooltip=f"🚌 {t.get('name', 'Transit stop')}",
                icon=folium.Icon(color="blue", icon="bus", prefix="fa"),
            ).add_to(m)

    # ── LEGEND ────────────────────────────────────────────────────────────────
    if st.session_state.fetch_done:
        if st.session_state.routes_done and st.session_state.routes:
            legend_rows = ""
            for r in st.session_state.routes:
                legend_rows += (
                    f'<span style="color:{r["color"]};font-size:16px;">●</span>'
                    f'&nbsp;<span style="color:#111;">'
                    f'Team {r["team_idx"]+1} ({r["size"]}p) — {len(r["houses"])} houses'
                    f'</span><br>'
                )
            legend_rows += (
                '<span style="color:#27ae60;font-size:14px;">▲</span>'
                '&nbsp;<span style="color:#111;">Start (parking)</span><br>'
                '<span style="color:#2980b9;font-size:14px;">▲</span>'
                '&nbsp;<span style="color:#111;">Start (transit)</span><br>'
                '<span style="color:#e74c3c;font-size:14px;">▲</span>'
                '&nbsp;<span style="color:#111;">End (transit)</span><br>'
            )
        else:
            legend_rows = (
                '<span style="color:#e74c3c;font-size:16px;">●</span>'
                '&nbsp;<span style="color:#111;">Building / Address</span><br>'
                '<span style="color:#2980b9;font-size:16px;">━</span>'
                '&nbsp;<span style="color:#111;">Residential</span><br>'
                '<span style="color:#27ae60;font-size:16px;">━</span>'
                '&nbsp;<span style="color:#111;">Footway / Path</span><br>'
                '<span style="color:#1abc9c;font-size:16px;">━</span>'
                '&nbsp;<span style="color:#111;">Pedestrian</span><br>'
                '<span style="color:#8e44ad;font-size:16px;">━</span>'
                '&nbsp;<span style="color:#111;">Living street</span><br>'
                '<span style="color:#e74c3c;font-size:16px;">━</span>'
                '&nbsp;<span style="color:#111;">Primary road</span><br>'
            )
            if st.session_state.parking_spots:
                legend_rows += (
                    '<span style="color:#27ae60;font-size:14px;">▲</span>'
                    '&nbsp;<span style="color:#111;">Free parking</span><br>'
                )
            if st.session_state.transit_stops:
                legend_rows += (
                    '<span style="color:#2980b9;font-size:14px;">▲</span>'
                    '&nbsp;<span style="color:#111;">Transit stop</span><br>'
                )

        m.get_root().html.add_child(folium.Element(f"""
        <div style="
            position:fixed; bottom:36px; right:10px; z-index:9999;
            background:rgba(255,255,255,0.97);
            padding:10px 14px; border-radius:8px; border:1px solid #bbb;
            font-size:12px; line-height:1.9; color:#111;
            box-shadow:2px 2px 6px rgba(0,0,0,0.18);
            font-family:Arial,sans-serif; max-width:240px;">
          <b style="color:#111;">Legend</b><br>
          {legend_rows}
        </div>
        """))

    # ── MAP INTERACTION ───────────────────────────────────────────────────────
    map_output = st_folium(
        m,
        key=f"map_{st.session_state.map_key}",
        use_container_width=True,
        height=640,
        returned_objects=["last_active_drawing"],
    )

    if map_output:
        drawing = map_output.get("last_active_drawing")
        if drawing:
            try:
                new_poly = shape(drawing["geometry"])
                new_wkt  = new_poly.wkt
                if new_wkt == st.session_state.reset_polygon_wkt:
                    pass
                elif new_poly != st.session_state.polygon:
                    st.session_state.polygon            = new_poly
                    st.session_state.buildings          = []
                    st.session_state.streets_geojson    = None
                    st.session_state.parking_spots      = []
                    st.session_state.transit_stops      = []
                    st.session_state.fetch_done         = False
                    st.session_state.selected_types     = []
                    st.session_state.routes             = []
                    st.session_state.routes_done        = False
                    st.session_state.reset_polygon_wkt  = None
                    st.rerun()
            except Exception:
                pass
