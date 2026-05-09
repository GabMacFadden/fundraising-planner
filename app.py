"""
Fundraising Route Planner — Phase 2
Area selection · house detection · walkable streets · route planning · team splitting
· shift timing · free parking · public transit.
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

WALKING_SPEED_KMH = 4.0   # typical door-to-door pace
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
    """Single combined query: buildings + addresses + streets + parking + transit."""
    minx, miny, maxx, maxy = polygon.bounds
    s, w, n, e = miny, minx, maxy, maxx
    # Expand bbox slightly for transit stops near (but not inside) the boundary
    pad = 0.003
    sp, wp, np_, ep = s - pad, w - pad, n + pad, e + pad
    return f"""
[out:json][timeout:{REQUEST_TIMEOUT}];
(
  way["building"]({s},{w},{n},{e});
  node["addr:housenumber"]({s},{w},{n},{e});
  way["highway"~"^({WALKABLE_HIGHWAYS})$"]({s},{w},{n},{e});
  node["amenity"="parking"]["access"!="private"]({s},{w},{n},{e});
  way["amenity"="parking"]["access"!="private"]({s},{w},{n},{e});
  node["highway"="bus_stop"]({sp},{wp},{np_},{ep});
  node["railway"~"^(tram_stop|station|halt)$"]({sp},{wp},{np_},{ep});
  node["amenity"="bus_station"]({sp},{wp},{np_},{ep});
);
out geom tags;
""".strip()


def fetch_overpass(polygon):
    """Try mirrors in order. Returns (data_dict, used_url, err_str)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    query   = overpass_query(polygon)
    last_err = None
    for url in OVERPASS_MIRRORS:
        host = url.split("/")[2]
        try:
            r = requests.post(url, data={"data": query},
                              headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json(), url, None
            last_err = f"HTTP {r.status_code} from {host}"
        except requests.exceptions.Timeout:
            last_err = f"Timeout from {host}"
        except Exception as ex:
            last_err = f"{type(ex).__name__} from {host}: {ex}"
    return None, None, last_err


def parse_overpass(data, polygon):
    """
    Parse Overpass JSON into:
      buildings list, streets GeoJSON FeatureCollection,
      parking list, transit list.
    """
    if not data or "elements" not in data:
        return [], {"type": "FeatureCollection", "features": []}, [], []

    buildings, seen, streets, parking, transit = [], set(), [], [], []

    for el in data["elements"]:
        tags  = el.get("tags", {})
        etype = el.get("type")

        # ── Ways ──────────────────────────────────────────────────────────────
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
                        "name": tags.get("name", "Parking"),
                        "fee":  fee,
                    })

        # ── Nodes ─────────────────────────────────────────────────────────────
        elif etype == "node":
            lat, lon = el.get("lat"), el.get("lon")
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
                if fee not in ("yes", "paid"):
                    parking.append({
                        "lat":  lat, "lon": lon,
                        "name": tags.get("name", "Parking"),
                        "fee":  fee,
                    })

            elif (tags.get("highway") == "bus_stop"
                  or tags.get("railway") in ("tram_stop", "station", "halt")
                  or tags.get("amenity") == "bus_station"):
                stop_type = (tags.get("railway")
                             or tags.get("highway")
                             or tags.get("amenity") or "stop")
                transit.append({
                    "lat":  lat,
                    "lon":  lon,
                    "name": tags.get("name", stop_type.replace("_", " ").title()),
                    "type": stop_type,
                })

    return buildings, {"type": "FeatureCollection", "features": streets}, parking, transit


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE PLANNING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def num_teams(n_people: int) -> int:
    """Pairs of people form a team; solo person is their own team."""
    return max(1, math.ceil(n_people / 2))


def team_compositions(n_people: int) -> list:
    """Return list of per-team headcounts. e.g. 5 people → [2, 2, 1]."""
    n_t  = num_teams(n_people)
    base = [2] * (n_t - 1)
    last = n_people - 2 * (n_t - 1)
    return base + [max(1, last)]


def compute_break_minutes(shift_minutes: int) -> int:
    """30-min break for every full 2-hour block."""
    return (shift_minutes // 120) * 30


def estimate_route_time(n_houses: int, distance_m: float,
                        time_per_door_min: float) -> dict:
    """
    Time estimate for a single team route.
    ~35% of doors are empty and take EMPTY_DOOR_SEC seconds each.
    """
    empty    = round(n_houses * EMPTY_HOUSE_RATE)
    occupied = n_houses - empty
    talk_min = occupied * time_per_door_min + empty * (EMPTY_DOOR_SEC / 60)
    walk_min = (distance_m / 1000) / WALKING_SPEED_KMH * 60
    total    = talk_min + walk_min
    return {
        "walk_min":   round(walk_min),
        "talk_min":   round(talk_min),
        "total_min":  round(total),
        "occupied":   occupied,
        "empty":      empty,
        "distance_m": round(distance_m),
    }


def _simple_kmeans(points: list, k: int, max_iter: int = 40, seed: int = 42) -> list:
    """
    K-means on [[lat, lon], ...] points.
    Returns list of k clusters, each a list of original indices.
    """
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
            pts = [points[i] for i in range(n) if assignments[i] == ci]
            if pts:
                centroids[ci] = [
                    sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts),
                ]

    clusters = [[] for _ in range(k)]
    for i, ci in enumerate(assignments):
        clusters[ci].append(i)
    return clusters


def _balance_clusters(points: list, clusters: list, max_iter: int = 20) -> list:
    """Move boundary points from over-sized to under-sized clusters."""
    k = len(clusters)
    for _ in range(max_iter):
        sizes  = [len(c) for c in clusters]
        big_ci = max(range(k), key=lambda i: sizes[i])
        sml_ci = min(range(k), key=lambda i: sizes[i])
        if sizes[big_ci] - sizes[sml_ci] <= 1:
            break
        if not clusters[sml_ci]:
            continue
        sml_centroid = [
            sum(points[i][0] for i in clusters[sml_ci]) / len(clusters[sml_ci]),
            sum(points[i][1] for i in clusters[sml_ci]) / len(clusters[sml_ci]),
        ]
        move_j = min(range(len(clusters[big_ci])),
                     key=lambda j: haversine_m(points[clusters[big_ci][j]], sml_centroid))
        moved  = clusters[big_ci].pop(move_j)
        clusters[sml_ci].append(moved)
    return clusters


def _nearest_neighbor_route(points: list, start: list = None) -> list:
    """
    Nearest-neighbor TSP approximation.
    Returns ordered list of indices into `points`.
    """
    if not points:
        return []
    unvisited = list(range(len(points)))
    first = (min(unvisited, key=lambda i: haversine_m(start, points[i]))
             if start else 0)
    route = [first]
    unvisited.remove(first)
    while unvisited:
        last    = route[-1]
        nearest = min(unvisited, key=lambda i: haversine_m(points[last], points[i]))
        route.append(nearest)
        unvisited.remove(nearest)
    return route


def _route_distance_m(waypoints: list) -> float:
    """Sum of haversine distances along an ordered list of [lat, lon] points."""
    return sum(haversine_m(waypoints[i - 1], waypoints[i])
               for i in range(1, len(waypoints)))


def _best_start(cluster_pts: list, parking: list, transit: list):
    """
    Pick the best starting point for a team cluster:
      1. Nearest free parking spot to the cluster centroid.
      2. Nearest transit stop if no parking found.
      3. First house in the cluster as fallback.
    Returns (latlng, label, type_str).
    """
    if not cluster_pts:
        return None, None, None
    centroid = [
        sum(p[0] for p in cluster_pts) / len(cluster_pts),
        sum(p[1] for p in cluster_pts) / len(cluster_pts),
    ]
    if parking:
        p = min(parking, key=lambda x: haversine_m(centroid, [x["lat"], x["lon"]]))
        return [p["lat"], p["lon"]], p.get("name", "Free parking"), "parking"
    if transit:
        t = min(transit, key=lambda x: haversine_m(centroid, [x["lat"], x["lon"]]))
        return [t["lat"], t["lon"]], t.get("name", "Transit stop"), "transit"
    return cluster_pts[0], "Start", "manual"


def _best_end(ordered_pts: list, transit: list, start_latlng: list):
    """
    Pick the best end point for a team:
      1. Nearest transit stop within 800 m of the last house.
      2. Return to start otherwise.
    Returns (latlng, label, type_str).
    """
    if not ordered_pts:
        return None, None, None
    last = ordered_pts[-1]
    if transit:
        t    = min(transit, key=lambda x: haversine_m(last, [x["lat"], x["lon"]]))
        dist = haversine_m(last, [t["lat"], t["lon"]])
        if dist < 800:
            return [t["lat"], t["lon"]], t.get("name", "Transit stop"), "transit"
    if start_latlng:
        return start_latlng, "Return to start", "start"
    return last, "End", "manual"


def plan_routes(buildings: list, parking_spots: list, transit_stops: list,
                n_people: int, shift_minutes: int, time_per_door_min: float,
                coverage: float = 0.90) -> list:
    """
    Main route planning entry point.

    Returns a list of team dicts:
    {
        team_idx, size, color,
        houses:     [[lat, lon], ...],   # visit order
        house_data: [building_dict, ...],
        start, start_label, start_type,
        end,   end_label,   end_type,
        stats: { walk_min, talk_min, total_min, occupied, empty,
                 distance_m, break_min, net_min, fits_shift },
        sub_routes: None | [ [[lat,lon],...], [[lat,lon],...] ],
    }
    """
    if not buildings:
        return []

    # Limit to coverage target
    target_n = max(1, round(len(buildings) * coverage))
    buildings = buildings[:target_n]

    n_t   = num_teams(n_people)
    sizes = team_compositions(n_people)
    pts   = [[b["lat"], b["lon"]] for b in buildings]

    clusters = _simple_kmeans(pts, n_t) if n_t > 1 else [list(range(len(pts)))]
    clusters = _balance_clusters(pts, clusters)

    break_min  = compute_break_minutes(shift_minutes)
    net_min    = shift_minutes - break_min

    result = []
    for ti, (idxs, size) in enumerate(zip(clusters, sizes)):
        if not idxs:
            continue

        c_pts  = [pts[i]       for i in idxs]
        c_blds = [buildings[i] for i in idxs]

        start_ll, start_lbl, start_type = _best_start(c_pts, parking_spots, transit_stops)

        order       = _nearest_neighbor_route(c_pts, start_ll)
        ordered_pts = [c_pts[i]  for i in order]
        ordered_bld = [c_blds[i] for i in order]

        # Distance includes travel from start and back to end
        waypoints = []
        if start_ll:
            waypoints.append(start_ll)
        waypoints.extend(ordered_pts)
        end_ll, end_lbl, end_type = _best_end(ordered_pts, transit_stops, start_ll)
        if end_ll and end_ll != start_ll:
            waypoints.append(end_ll)

        dist_m = _route_distance_m(waypoints)
        stats  = estimate_route_time(len(ordered_pts), dist_m, time_per_door_min)
        stats["break_min"]  = break_min
        stats["net_min"]    = net_min
        stats["fits_shift"] = stats["total_min"] <= net_min

        # For 2-person teams: split the route into two person-level halves
        sub_routes = None
        if size == 2 and len(ordered_pts) >= 4:
            half = len(ordered_pts) // 2
            sub_routes = [ordered_pts[:half], ordered_pts[half:]]

        result.append({
            "team_idx":    ti,
            "size":        size,
            "color":       TEAM_COLORS[ti % len(TEAM_COLORS)],
            "houses":      ordered_pts,
            "house_data":  ordered_bld,
            "start":       start_ll,
            "start_label": start_lbl,
            "start_type":  start_type,
            "end":         end_ll,
            "end_label":   end_lbl,
            "end_type":    end_type,
            "stats":       stats,
            "sub_routes":  sub_routes,
        })

    return result


# ══════════════════════════════════════════════════════════════════════════════
# CACHED FETCH
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False, ttl=3600, max_entries=20)
def fetch_all(polygon_wkt: str):
    """Fetch buildings, streets, parking, transit in one shot. Cached by polygon WKT."""
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
                 border-left:4px solid; background:rgba(0,0,0,0.03); font-size:.85rem; }
</style>
""", unsafe_allow_html=True)

st.markdown('<p class="main-title">🗺️ Fundraising Route Planner</p>',
            unsafe_allow_html=True)
st.markdown(
    '<p class="sub-title"><span class="phase-badge">Phase 2</span>'
    '&nbsp; Area · Houses · Streets · Route planning · Team splitting'
    ' · Parking &amp; transit</p>',
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
    st.markdown('<p class="step-label">Step 1 — Draw your area</p>',
                unsafe_allow_html=True)
    st.caption(f"Use ◻ rectangle or ⬡ polygon. Max area: {MAX_AREA_KM2} km².")
    st.divider()

    # ── Step 2 ────────────────────────────────────────────────────────────────
    st.markdown('<p class="step-label">Step 2 — Fetch map data</p>',
                unsafe_allow_html=True)

    polygon  = st.session_state.polygon
    area_km2 = approximate_area_km2(polygon)

    if polygon:
        if area_km2 > MAX_AREA_KM2:
            st.error(f"Area too large: {area_km2:.2f} km² > {MAX_AREA_KM2} km².")
        else:
            st.success(f"Area selected (~{area_km2:.3f} km²)")
    else:
        st.info("No area drawn yet.")

    fetch_disabled = polygon is None or area_km2 > MAX_AREA_KM2

    if st.button("🔍 Fetch Houses & Streets", type="primary",
                 disabled=fetch_disabled, use_container_width=True):
        with st.spinner("Fetching from OpenStreetMap…"):
            result = fetch_all(polygon.wkt)

        if "error" in result:
            st.error(f"Fetch failed: {result['error']}\n\nTry again or use a smaller area.")
        else:
            st.session_state.buildings       = result["buildings"]
            st.session_state.streets_geojson = result["streets_geojson"]
            st.session_state.parking_spots   = result.get("parking_spots", [])
            st.session_state.transit_stops   = result.get("transit_stops", [])
            st.session_state.selected_types  = sorted(
                {b["type"] for b in result["buildings"]}
            )
            st.session_state.fetch_done   = True
            st.session_state.routes       = []
            st.session_state.routes_done  = False
            mirror = result.get("mirror") or ""
            host   = mirror.split("/")[2] if mirror else "?"
            n_p    = len(result.get("parking_spots", []))
            n_t    = len(result.get("transit_stops", []))
            st.success(
                f"Found **{len(result['buildings'])}** buildings · "
                f"**{n_p}** parking · **{n_t}** transit stops"
            )
            st.rerun()

    # ── Building type filter ───────────────────────────────────────────────────
    if st.session_state.fetch_done and st.session_state.buildings:
        st.divider()
        st.markdown('<p class="step-label">🏠 Filter building types</p>',
                    unsafe_allow_html=True)
        all_types   = sorted({b["type"] for b in st.session_state.buildings})
        type_counts = {t: sum(1 for b in st.session_state.buildings if b["type"] == t)
                       for t in all_types}
        new_selected = []
        for t in all_types:
            if st.checkbox(f"{label(t)}  ({type_counts[t]})",
                           value=(t in st.session_state.selected_types),
                           key=f"chk_{t}"):
                new_selected.append(t)
        if new_selected != st.session_state.selected_types:
            st.session_state.selected_types = new_selected
            st.session_state.routes         = []
            st.session_state.routes_done    = False
            st.rerun()

    # ── Step 3: Route planning ─────────────────────────────────────────────────
    if st.session_state.fetch_done and st.session_state.buildings:
        st.divider()
        st.markdown('<p class="step-label">Step 3 — Plan routes</p>',
                    unsafe_allow_html=True)

        n_people = st.slider(
            "👥 Total people in field",
            min_value=1, max_value=12,
            value=st.session_state.get("ui_n_people", 2),
            step=1, key="n_people_slider",
        )
        st.session_state["ui_n_people"] = n_people

        n_t_prev = num_teams(n_people)
        sizes    = team_compositions(n_people)
        team_str = " + ".join(f"{s}p" for s in sizes)
        st.caption(
            f"→ **{n_t_prev}** team{'s' if n_t_prev > 1 else ''}: {team_str} "
            f"({'each side of the street' if max(sizes) == 2 else 'solo coverage'})"
        )

        shift_hours = st.slider(
            "⏱️ Shift duration (hours)",
            min_value=1.0, max_value=10.0,
            value=st.session_state.get("ui_shift_hours", 4.0),
            step=0.5, key="shift_hours_slider",
        )
        st.session_state["ui_shift_hours"] = shift_hours
        shift_min = int(shift_hours * 60)
        break_min = compute_break_minutes(shift_min)
        net_min   = shift_min - break_min
        st.caption(
            f"→ {shift_min} min total · **{break_min} min breaks** "
            f"· **{net_min} min active**"
        )

        time_per_door = st.slider(
            "🚪 Avg. time per answered door (min)",
            min_value=1.0, max_value=15.0,
            value=st.session_state.get("ui_time_per_door", 3.0),
            step=0.5, key="time_per_door_slider",
            help=(
                f"Time spent when someone answers. "
                f"~{round(EMPTY_HOUSE_RATE*100)}% of doors won't answer "
                f"(counted as {EMPTY_DOOR_SEC}s each)."
            ),
        )
        st.session_state["ui_time_per_door"] = time_per_door

        if st.button("🗺️ Plan Routes", type="primary", use_container_width=True):
            visible_blds = [
                b for b in st.session_state.buildings
                if b["type"] in st.session_state.selected_types
            ]
            if not visible_blds:
                st.warning("No buildings visible. Check your filters.")
            else:
                with st.spinner("Optimising routes…"):
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

    # ── Route summaries ────────────────────────────────────────────────────────
    if st.session_state.routes_done and st.session_state.routes:
        st.divider()
        st.markdown('<p class="step-label">📋 Route summaries</p>',
                    unsafe_allow_html=True)
        total_houses = sum(len(r["houses"]) for r in st.session_state.routes)
        for r in st.session_state.routes:
            color = r["color"]
            s     = r["stats"]
            fits  = "✅" if s["fits_shift"] else "⚠️"
            over  = (f" <i>(+{s['total_min'] - s['net_min']} min over)</i>"
                     if not s["fits_shift"] else "")
            sub   = (" <br>↳ Route split into 2 person-paths"
                     if r["sub_routes"] else "")
            st.markdown(
                f"""<div class="team-card" style="border-color:{color}">
                <b style="color:{color}">Team {r['team_idx']+1}</b>
                &nbsp;({r['size']} person{'s' if r['size'] > 1 else ''})<br>
                🏠 {len(r['houses'])} houses
                &nbsp;·&nbsp; 🚶 {s['distance_m']} m<br>
                ⏱️ Walk {s['walk_min']} min + Talk {s['talk_min']} min
                = <b>{s['total_min']} min</b> {fits}{over}<br>
                📍 <b>Start:</b> {r['start_label'] or '—'}<br>
                🏁 <b>End:</b> {r['end_label'] or '—'}{sub}
                </div>""",
                unsafe_allow_html=True,
            )
        st.caption(
            f"Covering **{total_houses}** of "
            f"**{len([b for b in st.session_state.buildings if b['type'] in st.session_state.selected_types])}** "
            "visible buildings (≥ 90%)"
        )

    # ── Stats ──────────────────────────────────────────────────────────────────
    if st.session_state.fetch_done:
        st.divider()
        st.markdown('<p class="step-label">📊 Stats</p>', unsafe_allow_html=True)
        visible = [b for b in st.session_state.buildings
                   if b["type"] in st.session_state.selected_types]
        st.metric("🏠 Visible buildings", len(visible))
        st.metric("📦 Total detected",    len(st.session_state.buildings))
        if st.session_state.streets_geojson:
            st.metric("🛣️ Street segments",
                      len(st.session_state.streets_geojson.get("features", [])))
        if st.session_state.parking_spots:
            st.metric("🅿️ Free parking spots", len(st.session_state.parking_spots))
        if st.session_state.transit_stops:
            st.metric("🚌 Transit stops nearby", len(st.session_state.transit_stops))

    # ── Roadmap ────────────────────────────────────────────────────────────────
    st.divider()
    st.markdown('<p class="step-label">📍 Roadmap</p>', unsafe_allow_html=True)
    done   = st.session_state.fetch_done
    poly   = polygon is not None
    routed = st.session_state.routes_done
    st.markdown(f"""
{"✅" if poly   else "⏳"} Area selection
{"✅" if done   else "⏳"} House detection
{"✅" if done   else "⏳"} Street display
{"✅" if done   else "⏳"} Parking & transit
{"✅" if routed else "⏳"} Route planning
{"✅" if routed else "⏳"} Team splitting
""")

    # ── Reset ──────────────────────────────────────────────────────────────────
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
        init_zoom = 14 if a > 2.0 else 15 if a > 0.5 else 16 if a > 0.1 else 17
    else:
        init_center = [59.9139, 10.7522]   # Oslo
        init_zoom   = 14

    m = folium.Map(location=init_center, zoom_start=init_zoom,
                   tiles="OpenStreetMap", prefer_canvas=True)

    if st.session_state.polygon:
        minx, miny, maxx, maxy = st.session_state.polygon.bounds
        m.fit_bounds([[miny, minx], [maxy, maxx]])

    Draw(
        export=False,
        draw_options={
            "polyline": False, "polygon": True, "circle": False,
            "marker": False, "circlemarker": False, "rectangle": True,
        },
        edit_options={"edit": False, "remove": True},
    ).add_to(m)

    MeasureControl(position="bottomleft", primary_length_unit="meters").add_to(m)

    # ── Streets ────────────────────────────────────────────────────────────────
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

    # ── Routes (if planned) ────────────────────────────────────────────────────
    if st.session_state.routes_done and st.session_state.routes:
        for r in st.session_state.routes:
            color     = r["color"]
            team_name = f"Team {r['team_idx'] + 1}"

            # Route polyline
            if len(r["houses"]) >= 2:
                folium.PolyLine(
                    locations=[[h[0], h[1]] for h in r["houses"]],
                    color=color, weight=3.5, opacity=0.75,
                    tooltip=f"{team_name} route ({len(r['houses'])} houses)",
                    dash_array="8 5",
                ).add_to(m)

            # Sub-routes for 2-person teams (slightly different opacity)
            if r["sub_routes"]:
                for pi, sub in enumerate(r["sub_routes"]):
                    if len(sub) >= 2:
                        folium.PolyLine(
                            locations=[[h[0], h[1]] for h in sub],
                            color=color, weight=2, opacity=0.45,
                            tooltip=f"{team_name} person {pi + 1}",
                        ).add_to(m)

            # House markers
            for i, (pt, bld) in enumerate(zip(r["houses"], r["house_data"])):
                addr   = bld.get("housenumber", "")
                street = bld.get("street", "")
                addr_s = f"{street} {addr}".strip() or label(bld.get("type", ""))
                folium.CircleMarker(
                    location=[pt[0], pt[1]],
                    radius=5,
                    color=color, fill=True, fill_color=color, fill_opacity=0.8,
                    tooltip=f"{team_name} #{i + 1}: {addr_s}",
                ).add_to(m)

            # Start marker
            if r["start"]:
                icon_color = "green"  if r["start_type"] == "parking" else "blue"
                icon_name  = "car"    if r["start_type"] == "parking" else "play"
                folium.Marker(
                    location=r["start"],
                    tooltip=f"{team_name} ▶ {r['start_label']}",
                    icon=folium.Icon(color=icon_color, icon=icon_name, prefix="fa"),
                ).add_to(m)

            # End marker (only if different from start)
            if r["end"] and r["end"] != r["start"]:
                icon_color = "red"  if r["end_type"] == "transit" else "orange"
                icon_name  = "bus"  if r["end_type"] == "transit"  else "flag"
                folium.Marker(
                    location=r["end"],
                    tooltip=f"{team_name} ■ {r['end_label']}",
                    icon=folium.Icon(color=icon_color, icon=icon_name, prefix="fa"),
                ).add_to(m)

    else:
        # No routes yet — plain clustered building display
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
                        { color:"#c0392b", fillColor:"#e74c3c",
                          fillOpacity:0.85, weight:1, radius:5 }
                    );
                }
                """
                FastMarkerCluster(
                    data=visible, callback=callback,
                    options={"maxClusterRadius": 40, "disableClusteringAtZoom": 17},
                ).add_to(m)

        # Show parking & transit when routes not yet planned
        for p in st.session_state.parking_spots:
            folium.Marker(
                location=[p["lat"], p["lon"]],
                tooltip=f"🅿️ {p.get('name', 'Free parking')}",
                icon=folium.Icon(color="green", icon="car", prefix="fa"),
            ).add_to(m)

        for t in st.session_state.transit_stops:
            folium.Marker(
                location=[t["lat"], t["lon"]],
                tooltip=f"🚌 {t.get('name', 'Transit stop')}",
                icon=folium.Icon(color="blue", icon="bus", prefix="fa"),
            ).add_to(m)

    # ── Legend ─────────────────────────────────────────────────────────────────
    if st.session_state.fetch_done:
        if st.session_state.routes_done and st.session_state.routes:
            legend_rows = ""
            for r in st.session_state.routes:
                c     = r["color"]
                sizes = r["size"]
                legend_rows += (
                    f'<span style="color:{c};font-size:16px;">●</span>'
                    f'&nbsp;<span style="color:#111;">Team {r["team_idx"]+1}'
                    f' ({sizes}p) — {len(r["houses"])} houses</span><br>'
                )
            legend_rows += (
                '<span style="color:#27ae60;font-size:12px;">▶</span>'
                '&nbsp;<span style="color:#111;">Team start (parking)</span><br>'
                '<span style="color:#3498db;font-size:12px;">▶</span>'
                '&nbsp;<span style="color:#111;">Team start (transit/manual)</span><br>'
                '<span style="color:#e74c3c;font-size:12px;">■</span>'
                '&nbsp;<span style="color:#111;">Team end (transit stop)</span><br>'
            )
        else:
            legend_rows = (
                '<span style="color:#e74c3c;font-size:16px;">●</span>'
                '&nbsp;<span style="color:#111;">Building / Address</span><br>'
                '<span style="color:#27ae60;font-size:16px;">━</span>'
                '&nbsp;<span style="color:#111;">Footway / Path</span><br>'
                '<span style="color:#2980b9;font-size:16px;">━</span>'
                '&nbsp;<span style="color:#111;">Residential street</span><br>'
                '<span style="color:#8e44ad;font-size:16px;">━</span>'
                '&nbsp;<span style="color:#111;">Living street</span><br>'
                '<span style="color:#e74c3c;font-size:16px;">━</span>'
                '&nbsp;<span style="color:#111;">Primary road</span><br>'
            )
        if st.session_state.parking_spots:
            legend_rows += (
                '<span style="color:#27ae60;font-size:12px;">◆</span>'
                '&nbsp;<span style="color:#111;">Free parking</span><br>'
            )
        if st.session_state.transit_stops:
            legend_rows += (
                '<span style="color:#2980b9;font-size:12px;">◆</span>'
                '&nbsp;<span style="color:#111;">Transit stop</span><br>'
            )

        m.get_root().html.add_child(folium.Element(f"""
        <div style="
            position:fixed; bottom:36px; right:10px; z-index:9999;
            background:rgba(255,255,255,0.97);
            padding:10px 14px; border-radius:8px; border:1px solid #bbb;
            font-size:12px; line-height:1.9; color:#111;
            box-shadow:2px 2px 6px rgba(0,0,0,0.18);
            font-family:Arial,sans-serif; max-width:230px;">
          <b style="color:#111;">Legend</b><br>
          {legend_rows}
        </div>
        """))

    # ── Render map ─────────────────────────────────────────────────────────────
    map_output = st_folium(
        m,
        key=f"map_{st.session_state.map_key}",
        use_container_width=True,
        height=620,
        returned_objects=["last_active_drawing"],
    )

    # ── Handle drawn polygon ───────────────────────────────────────────────────
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
