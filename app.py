"""
Fundraising Route Planner — Phase 2
Setup wizard → parking / start-point selection → area drawing → optimised routes.
"""

import math
import random
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw, MeasureControl
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
MAX_AREA_KM2    = 4.0
REQUEST_TIMEOUT = 60
WALKABLE_HIGHWAYS = (
    "footway|path|pedestrian|residential|living_street|service|"
    "tertiary|tertiary_link|secondary|secondary_link|primary|"
    "primary_link|unclassified|cycleway|steps"
)
TEAM_COLORS = [
    "#e74c3c", "#3498db", "#2ecc71", "#f39c12",
    "#9b59b6", "#1abc9c", "#e67e22", "#34495e",
]
WALKING_SPEED_KMH = 4.0
EMPTY_HOUSE_RATE  = 0.35
EMPTY_DOOR_SEC    = 20
MAX_END_DIST_M    = 1_000   # 15 min @ 4 km/h ≈ 1 000 m

DEFAULTS = {
    "stage":               "setup",   # setup | map
    "center_address":      "",
    "center_latlng":       None,      # [lat, lon]
    "transport":           "car",
    "n_people":            2,
    "shift_hours":         4.0,
    "time_per_door":       3.0,
    "same_start":          True,
    "team_starts":         [],        # [[lat, lon], ...]
    "selected_parking":    None,      # [lat, lon]
    "parking_nearby":      [],
    "polygon":             None,
    "buildings":           [],
    "transit_stops":       [],
    "fetch_done":          False,
    "routes":              [],
    "routes_done":         False,
    "map_key":             0,
    "reset_polygon_wkt":   None,
    "last_click_seen":     None,      # dedup map clicks
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
    """Nominatim → [lat, lon] or None."""
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
        except Exception as ex:
            pass
    return None, "All mirrors failed"


@st.cache_data(show_spinner=False, ttl=3600, max_entries=50)
def fetch_parking_near(lat: float, lon: float, radius_m: int = 900):
    """Free parking spots within radius_m of a point."""
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
                "lat": sum(n_["lat"] for n_ in geom) / len(geom),
                "lon": sum(n_["lon"] for n_ in geom) / len(geom),
                "name": tags.get("name", "Free parking"),
            })
    return spots


@st.cache_data(show_spinner=False, ttl=3600, max_entries=20)
def fetch_all(polygon_wkt: str) -> dict:
    """Buildings + transit stops inside polygon. Cached by WKT."""
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
);
out geom tags;
""".strip()
    data, err = _post_overpass(query)
    if err or not data:
        return {"error": err or "No data returned"}

    buildings, seen, transit = [], set(), []
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

    return {"buildings": buildings, "transit_stops": transit}


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def num_teams(n: int) -> int:
    return max(1, math.ceil(n / 2))


def team_compositions(n: int) -> list:
    t = num_teams(n)
    return [2] * (t - 1) + [max(1, n - 2 * (t - 1))]


def compute_break_minutes(shift_min: int) -> int:
    """30 min break for every full 2-hour block."""
    return (shift_min // 120) * 30


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


# ── Nearest-neighbour TSP ─────────────────────────────────────────────────────

def _nn_route(points: list, start=None) -> list:
    """Returns ordered list of indices."""
    if not points:
        return []
    unvis = list(range(len(points)))
    first = (min(unvis, key=lambda i: haversine_m(start, points[i]))
             if start else 0)
    route = [first]
    unvis.remove(first)
    while unvis:
        last    = route[-1]
        nearest = min(unvis, key=lambda i: haversine_m(points[last], points[i]))
        route.append(nearest)
        unvis.remove(nearest)
    return route


def _nn_route_near_end(points: list, start, target_end) -> list:
    """NN from start; reverse if that puts the tail closer to target_end."""
    order = _nn_route(points, start)
    if haversine_m(points[order[0]], target_end) < haversine_m(points[order[-1]], target_end):
        return list(reversed(order))
    return order


def _route_dist(wps: list) -> float:
    return sum(haversine_m(wps[i - 1], wps[i]) for i in range(1, len(wps)))


# ── Person paths (street-following, anti-backtrack) ───────────────────────────

def _cen(blds: list) -> list:
    return [
        sum(b["lat"] for b in blds) / len(blds),
        sum(b["lon"] for b in blds) / len(blds),
    ]


def _street_path(buildings: list, start=None) -> list:
    """
    Group buildings by street name, sort each group along its street axis,
    then chain groups by nearest-centroid. This makes the person walk each
    street completely before moving on, minimising double-walking.
    Dead-end branches are visited as natural detours because their buildings
    cluster together and are chained back-to-back.
    """
    if not buildings:
        return []

    groups: dict = {}
    for b in buildings:
        key = b.get("street", "").strip() or "__nostre__"
        groups.setdefault(key, []).append(b)

    for key, grp in groups.items():
        if len(grp) < 2:
            continue
        lats = [b["lat"] for b in grp]
        lons = [b["lon"] for b in grp]
        if (max(lons) - min(lons)) > (max(lats) - min(lats)):
            groups[key] = sorted(grp, key=lambda b: b["lon"])
        else:
            groups[key] = sorted(grp, key=lambda b: b["lat"])

    glist = list(groups.values())

    # Chain groups: start with the one closest to the given start point
    first = (min(range(len(glist)), key=lambda i: haversine_m(start, _cen(glist[i])))
             if start else 0)
    ordered = [glist.pop(first)]
    while glist:
        lc = _cen(ordered[-1])
        ni = min(range(len(glist)), key=lambda i: haversine_m(lc, _cen(glist[i])))
        ordered.append(glist.pop(ni))

    return [b for g in ordered for b in g]


def _split_left_right(buildings: list):
    """
    Split buildings into two halves along the cluster's primary axis.
    Wider clusters → split N/S; taller → split E/W.
    """
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


def compute_person_paths(buildings: list, team_size: int, start=None) -> list:
    """
    Returns a list of paths (one per person), each path = [[lat, lon], ...].

    1-person team  → single street-following sweep.
    2-person team  → left/right split; each person sweeps their half.
    """
    if not buildings:
        return []

    if team_size == 1 or len(buildings) < 6:
        ordered = _street_path(buildings, start)
        return [[[b["lat"], b["lon"]] for b in ordered]]

    left, right = _split_left_right(buildings)
    pa = _street_path(left,  start)
    pb = _street_path(right, start)
    return [
        [[b["lat"], b["lon"]] for b in pa],
        [[b["lat"], b["lon"]] for b in pb],
    ]


# ── Convex-hull contour ───────────────────────────────────────────────────────

def compute_contour(buildings: list):
    """[[lat, lon], ...] convex-hull polygon of the team's buildings."""
    if len(buildings) < 3:
        return None
    try:
        hull = MultiPoint([(b["lon"], b["lat"]) for b in buildings]).convex_hull
        if hull.geom_type == "Polygon":
            return [[c[1], c[0]] for c in hull.exterior.coords]
    except Exception:
        pass
    return None


# ── Master planner ────────────────────────────────────────────────────────────

def plan_routes(buildings, parking_spot, transit_stops, team_starts,
                n_people, shift_minutes, time_per_door, transport,
                coverage=0.90) -> list:

    if not buildings:
        return []

    target_n  = max(1, round(len(buildings) * coverage))
    buildings = buildings[:target_n]

    n_t   = num_teams(n_people)
    sizes = team_compositions(n_people)
    pts   = [[b["lat"], b["lon"]] for b in buildings]

    clusters = _kmeans(pts, n_t) if n_t > 1 else [list(range(len(pts)))]
    clusters = _balance(pts, clusters)

    break_min  = compute_break_minutes(shift_minutes)
    net_min    = shift_minutes - break_min
    end_target = parking_spot if transport == "car" else None

    result = []
    for ti, (idxs, size) in enumerate(zip(clusters, sizes)):
        if not idxs:
            continue

        c_pts  = [pts[i]       for i in idxs]
        c_blds = [buildings[i] for i in idxs]

        # Resolve start point for this team
        if team_starts:
            if st.session_state.same_start or len(team_starts) == 1:
                start_ll = team_starts[0]
            else:
                start_ll = team_starts[ti] if ti < len(team_starts) else team_starts[-1]
        elif parking_spot:
            start_ll = parking_spot
        else:
            start_ll = c_pts[0]

        start_label = ("🅿️ Parking" if transport == "car" else "📍 Start point")

        # Build visit order
        if end_target:
            order = _nn_route_near_end(c_pts, start_ll, end_target)
        else:
            order = _nn_route(c_pts, start_ll)

        ordered_pts  = [c_pts[i]  for i in order]
        ordered_blds = [c_blds[i] for i in order]

        # Determine end
        end_ll, end_label, end_type = _resolve_end(
            ordered_pts, transit_stops, start_ll, parking_spot, transport
        )

        # Distance (start → houses → end)
        wps = ([start_ll] if start_ll else []) + ordered_pts
        if end_ll and end_ll != (ordered_pts[-1] if ordered_pts else None):
            wps.append(end_ll)
        dist_m = _route_dist(wps)

        stats = estimate_time(len(ordered_pts), dist_m, time_per_door)
        stats["break_min"]    = break_min
        stats["net_min"]      = net_min
        stats["fits_shift"]   = stats["total_min"] <= net_min

        # End-proximity check
        if ordered_pts:
            if transport == "car" and parking_spot:
                stats["end_dist_m"] = round(haversine_m(ordered_pts[-1], parking_spot))
                stats["end_ok"]     = stats["end_dist_m"] <= MAX_END_DIST_M
            else:
                stats["end_dist_m"] = round(haversine_m(ordered_pts[-1], start_ll))
                stats["end_ok"]     = stats["end_dist_m"] <= MAX_END_DIST_M

        person_paths = compute_person_paths(ordered_blds, size, start_ll)
        contour      = compute_contour(c_blds)

        result.append({
            "team_idx":     ti,
            "size":         size,
            "color":        TEAM_COLORS[ti % len(TEAM_COLORS)],
            "houses":       ordered_pts,
            "house_data":   ordered_blds,
            "start":        start_ll,
            "start_label":  start_label,
            "end":          end_ll,
            "end_label":    end_label,
            "end_type":     end_type,
            "stats":        stats,
            "person_paths": person_paths,
            "contour":      contour,
        })

    return result


def _resolve_end(ordered_pts, transit_stops, start_ll, parking_spot, transport):
    """Pick the best end-point for a team based on transport mode."""
    if not ordered_pts:
        return None, "—", "none"

    last = ordered_pts[-1]

    if transport == "car" and parking_spot:
        d = haversine_m(last, parking_spot)
        lbl = (f"~{round(d)} m from parking"
               if d <= MAX_END_DIST_M
               else f"⚠️ {round(d/1000, 1)} km from parking")
        return parking_spot, lbl, "parking"

    # Walk mode: prefer a transit stop within 600 m of the last house
    if transit_stops:
        t = min(transit_stops, key=lambda x: haversine_m(last, [x["lat"], x["lon"]]))
        d = haversine_m(last, [t["lat"], t["lon"]])
        if d < 600:
            return [t["lat"], t["lon"]], t["name"], "transit"

    # Fall back: walk back to start
    d = haversine_m(last, start_ll) if start_ll else 9_999
    lbl = (f"~{round(d)} m back to start"
           if d <= MAX_END_DIST_M
           else f"⚠️ {round(d/1000,1)} km from start")
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
# STAGE: SETUP  (full-width, no map)
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

            # ── Location ──────────────────────────────────────────────────────
            st.subheader("📍 Starting location")
            address = st.text_input(
                "Address, neighborhood or city",
                value=st.session_state.center_address,
                placeholder="e.g. Grünerløkka, Oslo",
            )

            # ── Transport ─────────────────────────────────────────────────────
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

            # ── Team ──────────────────────────────────────────────────────────
            st.subheader("👥 Team")
            n_people = st.slider(
                "Total people in the field today",
                min_value=1, max_value=12,
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

            # ── Shift ─────────────────────────────────────────────────────────
            st.subheader("⏱️ Shift")
            shift_hours = st.slider(
                "Shift duration (hours)",
                min_value=1.0, max_value=10.0,
                value=st.session_state.shift_hours, step=0.5,
            )
            shift_min = int(shift_hours * 60)
            brk       = compute_break_minutes(shift_min)
            st.caption(
                f"→ {brk} min breaks (30 min per 2 h) · "
                f"**{shift_min - brk} min active**"
            )

            # ── Doors ─────────────────────────────────────────────────────────
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

            submitted = st.form_submit_button(
                "Continue to map →", type="primary", use_container_width=True
            )

    with right:
        st.markdown("#### How it works")
        st.markdown("""
1. **Fill in the form** on the left and click *Continue*.
2. The map opens centred on your address.
   - 🚗 **Car mode** — free parking spots appear on the map; click one to confirm your spot.
   - 🚶 **Walk mode** — click the map to drop one start-point per team (or one shared point).
3. **Draw your work area** with the rectangle or polygon tool.
4. Click **Fetch & Plan** — the app downloads OSM data and builds optimised routes.
5. Each team gets:
   - a **coloured zone** showing their coverage area
   - a **main path** through the zone
   - a **per-person path** (left/right side or solo sweep)
   - estimated walk time, talk time and break schedule
""")

    if submitted:
        if not address.strip():
            st.error("Please enter an address or area name.")
            st.stop()

        with st.spinner("Looking up address…"):
            latlng = geocode_address(address)

        if latlng is None:
            st.error("Could not find that address. Try adding a city or country.")
            st.stop()

        parking = []
        if transport == "car":
            with st.spinner("Finding free parking nearby…"):
                parking = fetch_parking_near(latlng[0], latlng[1], 900)

        # Commit to session state and move to map stage
        st.session_state.update(
            center_address   = address,
            center_latlng    = latlng,
            transport        = transport,
            n_people         = n_people,
            same_start       = same_start,
            shift_hours      = shift_hours,
            time_per_door    = time_per_door,
            parking_nearby   = parking,
            team_starts      = [],
            selected_parking = None,
            polygon          = None,
            buildings        = [],
            transit_stops    = [],
            fetch_done       = False,
            routes           = [],
            routes_done      = False,
            last_click_seen  = None,
            stage            = "map",
        )
        st.rerun()

    st.stop()   # Nothing below renders during setup


# ══════════════════════════════════════════════════════════════════════════════
# STAGE: MAP  (map + control panel)
# ══════════════════════════════════════════════════════════════════════════════

st.markdown('<p class="main-title">🗺️ Fundraising Route Planner</p>',
            unsafe_allow_html=True)
st.markdown(
    '<p class="sub-title"><span class="phase-badge">Phase 2</span>'
    '&nbsp; Route planning · Team splitting · Parking &amp; transit</p>',
    unsafe_allow_html=True,
)

map_col, ctrl_col = st.columns([3, 1])

# ── Derived convenience vars ───────────────────────────────────────────────────
transport = st.session_state.transport
n_people  = st.session_state.n_people
n_t       = num_teams(n_people)
sizes     = team_compositions(n_people)
polygon   = st.session_state.polygon
area_km2  = approximate_area_km2(polygon)


# ══════════════════════════════════════════════════════════════════════════════
# CONTROL PANEL
# ══════════════════════════════════════════════════════════════════════════════

with ctrl_col:

    # Session summary + back button
    st.caption(
        f"📍 {st.session_state.center_address} · "
        f"{'🚗' if transport == 'car' else '🚶'} · "
        f"{n_people} people · {n_t} team{'s' if n_t > 1 else ''} · "
        f"{st.session_state.shift_hours} h"
    )
    if st.button("← Edit settings", use_container_width=True):
        st.session_state.stage = "setup"
        st.rerun()

    st.divider()

    # ── STEP 1 ─────────────────────────────────────────────────────────────────

    if transport == "car":
        st.markdown('<p class="step-label">🅿️ Step 1 — Confirm parking</p>',
                    unsafe_allow_html=True)
        nearby = st.session_state.parking_nearby

        if nearby:
            st.caption(
                f"{len(nearby)} free spot(s) found nearby. "
                "Click a marker **on the map** or pick from the list."
            )
            options = {f"{p['name']} ({p['lat']:.4f}, {p['lon']:.4f})": p
                       for p in nearby}
            chosen = st.selectbox(
                "Parking spot", list(options.keys()),
                index=None, placeholder="Select…", label_visibility="collapsed",
            )
            if chosen:
                p = options[chosen]
                st.session_state.selected_parking = [p["lat"], p["lon"]]
        else:
            st.info("No tagged free parking found. Click on the map to pin your spot.")

        if st.session_state.selected_parking:
            p = st.session_state.selected_parking
            st.success(f"✅ Parking set ({p[0]:.4f}, {p[1]:.4f})")
        else:
            st.caption("_No parking selected yet._")

    else:
        n_needed = 1 if st.session_state.same_start else n_t
        n_have   = len(st.session_state.team_starts)

        st.markdown('<p class="step-label">📍 Step 1 — Set starting point(s)</p>',
                    unsafe_allow_html=True)

        if n_have < n_needed:
            who = ("all teams"
                   if st.session_state.same_start
                   else f"Team {n_have + 1}")
            st.info(f"👆 Click the map to place the start point for **{who}**.")
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
    area_ok   = polygon is not None and area_km2 <= MAX_AREA_KM2
    can_go    = starts_ok and area_ok

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
            st.session_state.fetch_done    = True

            if not result["buildings"]:
                st.warning("No buildings found. Try a larger area.")
            else:
                # Build the per-team starts list
                if transport == "car":
                    effective_starts = (
                        [st.session_state.selected_parking] * n_t
                        if st.session_state.selected_parking else []
                    )
                elif st.session_state.same_start:
                    effective_starts = (
                        st.session_state.team_starts * n_t
                        if st.session_state.team_starts else []
                    )
                else:
                    effective_starts = st.session_state.team_starts

                with st.spinner(f"Planning routes for {n_t} team(s)…"):
                    routes = plan_routes(
                        buildings      = result["buildings"],
                        parking_spot   = st.session_state.selected_parking,
                        transit_stops  = result.get("transit_stops", []),
                        team_starts    = effective_starts,
                        n_people       = n_people,
                        shift_minutes  = int(st.session_state.shift_hours * 60),
                        time_per_door  = st.session_state.time_per_door,
                        transport      = transport,
                        coverage       = 0.90,
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

        for r in st.session_state.routes:
            s     = r["stats"]
            color = r["color"]
            fits  = "✅" if s["fits_shift"] else "⚠️"
            over  = (f" (+{s['total_min'] - s['net_min']} min)"
                     if not s["fits_shift"] else "")

            end_d     = s.get("end_dist_m", "?")
            end_ok    = s.get("end_ok", True)
            end_icon  = "✅" if end_ok else "⚠️"
            end_note  = (
                f"<br>🅿️ End: {end_d} m to parking {end_icon}"
                if transport == "car"
                else f"<br>🏁 End: {end_d} m from start {end_icon}"
            )

            n_paths = len(r.get("person_paths", []))
            sub = (f"<br>↳ {n_paths} person-paths" if n_paths > 1 else "")

            st.markdown(
                f"""<div class="team-card" style="border-color:{color}">
                <b style="color:{color}">Team {r['team_idx'] + 1}</b>
                ({r['size']}p)<br>
                🏠 {len(r['houses'])} houses &nbsp;·&nbsp; 🚶 {s['distance_m']} m<br>
                ⏱️ {s['walk_min']} + {s['talk_min']} = <b>{s['total_min']} min</b>
                {fits}{over}{end_note}{sub}
                </div>""",
                unsafe_allow_html=True,
            )

        if st.session_state.transit_stops:
            st.caption(f"🚌 {len(st.session_state.transit_stops)} transit stop(s) nearby")

    # ── Stats & reset ──────────────────────────────────────────────────────────

    if st.session_state.fetch_done:
        st.divider()
        st.metric("🏠 Houses found", len(st.session_state.buildings))

    if polygon is not None or st.session_state.fetch_done:
        st.divider()
        if st.button("🔄 Reset area & routes", use_container_width=True):
            old_wkt = polygon.wkt if polygon else None
            st.session_state.update(
                polygon          = None,
                buildings        = [],
                transit_stops    = [],
                fetch_done       = False,
                routes           = [],
                routes_done      = False,
                map_key          = st.session_state.map_key + 1,
                reset_polygon_wkt = old_wkt,
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

    Draw(
        export=False,
        draw_options={
            "polyline": False, "polygon": True,
            "circle": False, "marker": False,
            "circlemarker": False, "rectangle": True,
        },
        edit_options={"edit": False, "remove": True},
    ).add_to(m)
    MeasureControl(position="bottomleft", primary_length_unit="meters").add_to(m)

    # ── Pre-route: parking / start markers ────────────────────────────────────

    if not st.session_state.routes_done:
        if transport == "car":
            sel = st.session_state.selected_parking
            for p in st.session_state.parking_nearby:
                is_sel = (sel and abs(sel[0] - p["lat"]) < 0.0002
                          and abs(sel[1] - p["lon"]) < 0.0002)
                folium.Marker(
                    location=[p["lat"], p["lon"]],
                    tooltip=f"🅿️ {p['name']} — click to select",
                    icon=folium.Icon(
                        color="green" if is_sel else "gray",
                        icon="car", prefix="fa",
                    ),
                ).add_to(m)
            if sel:
                folium.Marker(
                    location=sel,
                    tooltip="🅿️ Your parking spot",
                    icon=folium.Icon(color="green", icon="car", prefix="fa"),
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

    # ── Route layers (shown instead of raw markers once routes exist) ─────────

    if st.session_state.routes_done and st.session_state.routes:
        for r in st.session_state.routes:
            color     = r["color"]
            light     = lighten(color, 0.5)
            team_name = f"Team {r['team_idx'] + 1}"

            # Filled team-zone contour
            if r.get("contour"):
                folium.Polygon(
                    locations=r["contour"],
                    color=color, weight=1.5, opacity=0.5,
                    fill=True, fill_color=color, fill_opacity=0.07,
                    tooltip=f"{team_name} coverage zone",
                ).add_to(m)

            # Person paths
            person_paths = r.get("person_paths", [])
            path_colors  = [color, light]
            for pi, path in enumerate(person_paths):
                if len(path) < 2:
                    continue
                pc  = path_colors[pi % len(path_colors)]
                tip = (f"{team_name} – Person {pi + 1}"
                       if len(person_paths) > 1 else team_name)
                folium.PolyLine(
                    locations=path,
                    color=pc, weight=3.5 if pi == 0 else 2.5,
                    opacity=0.9 if pi == 0 else 0.65,
                    tooltip=tip,
                ).add_to(m)

            # Start marker
            if r["start"]:
                folium.Marker(
                    location=r["start"],
                    tooltip=f"{team_name} ▶ {r['start_label']}",
                    icon=folium.Icon(
                        color="green" if transport == "car" else "blue",
                        icon="car"    if transport == "car" else "play",
                        prefix="fa",
                    ),
                ).add_to(m)

            # End marker (only if distinct from start)
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

    if st.session_state.routes_done and st.session_state.routes:
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
            '<span style="color:#111;">End / transit</span><br>'
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
        returned_objects=["last_active_drawing", "last_clicked"],
    )

    # ── Process drawing ────────────────────────────────────────────────────────

    if map_out:
        drawing = map_out.get("last_active_drawing")
        if drawing:
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

        # ── Process map click ──────────────────────────────────────────────────

        clicked = map_out.get("last_clicked")
        if clicked and not st.session_state.routes_done:
            click_key = (round(clicked["lat"], 5), round(clicked["lng"], 5))

            if click_key != st.session_state.last_click_seen:
                st.session_state.last_click_seen = click_key
                click_ll = [clicked["lat"], clicked["lng"]]

                if transport == "car":
                    # Snap to the nearest parking marker within 120 m, else custom pin
                    nearby = st.session_state.parking_nearby
                    if nearby:
                        best  = min(nearby, key=lambda p: haversine_m(click_ll, [p["lat"], p["lon"]]))
                        dist  = haversine_m(click_ll, [best["lat"], best["lon"]])
                        st.session_state.selected_parking = (
                            [best["lat"], best["lon"]] if dist < 120 else click_ll
                        )
                    else:
                        st.session_state.selected_parking = click_ll
                    st.rerun()

                else:
                    n_needed = 1 if st.session_state.same_start else n_t
                    if len(st.session_state.team_starts) < n_needed:
                        st.session_state.team_starts = st.session_state.team_starts + [click_ll]
                        st.rerun()
