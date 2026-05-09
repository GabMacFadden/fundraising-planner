"""
Fundraising Route Planner — Phase 1
Area selection · house detection · walkable street network.

Performance & safety notes inline.
"""

import math
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw, MeasureControl, FastMarkerCluster
import osmnx as ox
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

# Overpass mirrors. We try them in order. Putting kumi.systems first because
# it's typically more permissive for cloud-hosted clients.
OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# A descriptive User-Agent is REQUIRED by Overpass usage policy. Without it,
# servers may return HTTP 406, 429 or block requests outright.
USER_AGENT = "FundraisingRoutePlanner/1.0 (https://streamlit.io)"

# Hard cap on selectable area to prevent abuse, runaway queries and OOM.
MAX_AREA_KM2 = 4.0
REQUEST_TIMEOUT = 60       # seconds

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
    "primary":       "#e74c3c",
    "secondary":     "#e67e22",
    "tertiary":      "#f39c12",
    "residential":   "#2980b9",
    "living_street": "#8e44ad",
    "footway":       "#27ae60",
    "path":          "#27ae60",
    "pedestrian":    "#1abc9c",
    "service":       "#95a5a6",
    "track":         "#7f8c8d",
}
DEFAULT_HIGHWAY_COLOR = "#2980b9"

DEFAULTS = {
    "buildings":             [],
    "edges_geojson":         None,
    "polygon":               None,
    "map_center":            [59.9139, 10.7522],   # Oslo
    "map_zoom":              14,
    "fetch_done":            False,
    "map_key":               0,
    "selected_types":        [],
    "reset_polygon_wkt":     None,                 # used to ignore stale drawings
}


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def label(raw: str) -> str:
    return BUILDING_LABELS.get(raw, raw.replace("_", " ").capitalize())


def approximate_area_km2(polygon) -> float:
    """Latitude-corrected area in km² (much better than the flat-Earth estimate)."""
    if polygon is None:
        return 0.0
    centroid = polygon.centroid
    lat_km = 111.32
    lon_km = 111.32 * math.cos(math.radians(centroid.y))
    return polygon.area * lat_km * lon_km


def overpass_query(polygon) -> str:
    """One combined query for buildings + address nodes (much faster than two)."""
    minx, miny, maxx, maxy = polygon.bounds
    # Overpass bbox = south, west, north, east
    s, w, n, e = miny, minx, maxy, maxx
    return f"""
[out:json][timeout:{REQUEST_TIMEOUT}];
(
  way["building"]({s},{w},{n},{e});
  node["addr:housenumber"]({s},{w},{n},{e});
);
out center tags;
""".strip()


def fetch_overpass(polygon):
    """Try each mirror until one returns 200. Returns (data, used_url, err)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    query = overpass_query(polygon)
    last_err = None
    for url in OVERPASS_MIRRORS:
        try:
            r = requests.post(
                url,
                data={"data": query},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                return r.json(), url, None
            last_err = f"HTTP {r.status_code} from {url.split('/')[2]}"
        except requests.exceptions.Timeout:
            last_err = f"Timeout from {url.split('/')[2]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return None, None, last_err


def parse_overpass(data, polygon):
    """Parse Overpass JSON → list of building dicts, filtered to polygon."""
    if not data or "elements" not in data:
        return []

    buildings, seen = [], set()

    for el in data["elements"]:
        tags = el.get("tags", {})
        etype = el.get("type")

        if etype == "way":
            center = el.get("center")
            if not center or "building" not in tags:
                continue
            lat, lon = center["lat"], center["lon"]
            btype = tags.get("building") or "yes"
        elif etype == "node":
            if "addr:housenumber" not in tags:
                continue
            lat, lon = el.get("lat"), el.get("lon")
            btype = "address_node"
        else:
            continue

        if lat is None or lon is None:
            continue

        key = (round(lat, 5), round(lon, 5))
        if key in seen or not polygon.contains(Point(lon, lat)):
            continue
        seen.add(key)

        buildings.append({
            "lat":         lat,
            "lon":         lon,
            "type":        btype,
            "housenumber": tags.get("addr:housenumber", ""),
            "street":      tags.get("addr:street", ""),
            "name":        tags.get("name", ""),
        })

    return buildings


def set_osmnx_endpoint(url: str):
    for attr in ("overpass_endpoint", "overpass_url"):
        if hasattr(ox.settings, attr):
            setattr(ox.settings, attr, url)
            return


def fetch_street_graph(polygon):
    """Walkable street graph; tries all mirrors. Returns (graph, err)."""
    last_err = None
    for url in OVERPASS_MIRRORS:
        try:
            set_osmnx_endpoint(url)
            G = ox.graph_from_polygon(polygon, network_type="walk")
            return G, None
        except Exception as e:
            last_err = f"{url.split('/')[2]}: {e}"
    return None, last_err


def graph_to_geojson(G):
    """Single GeoJSON dict → renders 10–100× faster than per-edge PolyLines."""
    edges_gdf = ox.graph_to_gdfs(G, nodes=False)
    edges_gdf = edges_gdf[["geometry", "highway"]].copy()
    edges_gdf["highway"] = edges_gdf["highway"].apply(
        lambda h: h[0] if isinstance(h, list) else h
    ).astype(str)
    return edges_gdf.__geo_interface__


# ══════════════════════════════════════════════════════════════════════════════
# CACHED FETCH (1-hour TTL)
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False, ttl=3600, max_entries=20)
def fetch_all(polygon_wkt: str):
    """Fetch buildings + streets. Cached by polygon WKT."""
    polygon = shapely_wkt.loads(polygon_wkt)

    raw, mirror, err = fetch_overpass(polygon)
    if err:
        return {"error": err}
    buildings = parse_overpass(raw, polygon)

    G, gerr = fetch_street_graph(polygon)
    edges_geojson = None
    if G is not None:
        try:
            edges_geojson = graph_to_geojson(G)
        except Exception as e:
            gerr = f"render: {e}"

    return {
        "buildings":     buildings,
        "edges_geojson": edges_geojson,
        "street_error":  gerr,
        "mirror":        mirror,
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
</style>
""", unsafe_allow_html=True)

st.markdown('<p class="main-title">🗺️ Fundraising Route Planner</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub-title"><span class="phase-badge">Phase 1</span>'
    '&nbsp; Area selection · House detection · Street network</p>',
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

    # Step 1
    st.markdown('<p class="step-label">Step 1 — Draw your area</p>', unsafe_allow_html=True)
    st.caption(f"Use ◻ rectangle or ⬡ polygon. Max area: {MAX_AREA_KM2} km².")
    st.divider()

    # Step 2
    st.markdown('<p class="step-label">Step 2 — Fetch map data</p>', unsafe_allow_html=True)

    polygon  = st.session_state.polygon
    area_km2 = approximate_area_km2(polygon)

    if polygon:
        if area_km2 > MAX_AREA_KM2:
            st.error(f"Area too large: {area_km2:.2f} km² > {MAX_AREA_KM2} km². "
                     f"Draw a smaller area.")
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
        # Single spinner — no progress bar (which would cause extra reruns)
        with st.spinner("Fetching from OpenStreetMap…"):
            result = fetch_all(polygon.wkt)

        if "error" in result:
            st.error(f"Fetch failed: {result['error']}\n\n"
                     "Try a smaller area or wait a moment.")
        else:
            st.session_state.buildings      = result["buildings"]
            st.session_state.edges_geojson  = result["edges_geojson"]
            st.session_state.selected_types = sorted({b["type"] for b in result["buildings"]})
            st.session_state.fetch_done     = True

            if result.get("street_error"):
                st.warning(f"Streets: {result['street_error']}")

            mirror = result.get("mirror") or ""
            host   = mirror.split("/")[2] if mirror else "?"
            st.success(f"Found {len(result['buildings'])} buildings · {host}")
            st.rerun()

    # ── Building type filter ──────────────────────────────────────────────
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
            st.rerun()

    # ── Stats ──────────────────────────────────────────────────────────────
    if st.session_state.fetch_done:
        st.divider()
        st.markdown('<p class="step-label">📊 Stats</p>', unsafe_allow_html=True)

        visible = [b for b in st.session_state.buildings
                   if b["type"] in st.session_state.selected_types]
        st.metric("🏠 Visible buildings", len(visible))
        st.metric("📦 Total detected",    len(st.session_state.buildings))

        if st.session_state.edges_geojson:
            features = st.session_state.edges_geojson.get("features", [])
            st.metric("🛣️ Street segments", len(features))

    # ── Roadmap ────────────────────────────────────────────────────────────
    st.divider()
    st.markdown('<p class="step-label">📍 Roadmap</p>', unsafe_allow_html=True)
    done = st.session_state.fetch_done
    poly = polygon is not None
    st.markdown(f"""
{"✅" if poly else "⏳"} Area selection  
{"✅" if done else "⏳"} House detection  
{"✅" if done else "⏳"} Street network  
⏳ Route planning *(Phase 2)*  
⏳ Team splitting *(Phase 3)*  
⏳ Parking & transit *(Phase 4)*
""")

    # ── Reset ──────────────────────────────────────────────────────────────
    if poly or done:
        st.divider()
        if st.button("🔄 Reset everything", use_container_width=True):
            old_wkt = polygon.wkt if polygon else None
            new_key = st.session_state.map_key + 1
            for k, v in DEFAULTS.items():
                st.session_state[k] = v
            st.session_state.map_key           = new_key   # forces full map remount
            st.session_state.reset_polygon_wkt = old_wkt   # ignore stale drawings
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# MAP
# ══════════════════════════════════════════════════════════════════════════════

with map_col:
    m = folium.Map(
        location=st.session_state.map_center,
        zoom_start=st.session_state.map_zoom,
        tiles="OpenStreetMap",
        prefer_canvas=True,        # canvas renderer — much faster for many features
    )

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

    # Streets in ONE GeoJson layer (huge speedup vs per-edge PolyLine loop)
    if st.session_state.edges_geojson:
        def style_fn(feature):
            htype = feature["properties"].get("highway", "")
            return {
                "color":   HIGHWAY_COLORS.get(htype, DEFAULT_HIGHWAY_COLOR),
                "weight":  2.5,
                "opacity": 0.8,
            }

        folium.GeoJson(
            st.session_state.edges_geojson,
            name="streets",
            style_function=style_fn,
            tooltip=folium.GeoJsonTooltip(fields=["highway"], aliases=["Type:"]),
        ).add_to(m)

    # Buildings via FastMarkerCluster (JS-side construction = much faster)
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

    # Legend (only after fetch)
    if st.session_state.fetch_done:
        m.get_root().html.add_child(folium.Element("""
        <div style="
            position:fixed; bottom:36px; right:10px; z-index:9999;
            background:rgba(255,255,255,0.97);
            padding:10px 14px; border-radius:8px; border:1px solid #bbb;
            font-size:12px; line-height:1.9; color:#111;
            box-shadow:2px 2px 6px rgba(0,0,0,0.18);
            font-family:Arial,sans-serif;">
          <b style="color:#111;">Legend</b><br>
          <span style="color:#e74c3c;font-size:16px;">●</span>&nbsp;<span style="color:#111;">Building / Address</span><br>
          <span style="color:#2980b9;font-size:16px;">━</span>&nbsp;<span style="color:#111;">Residential</span><br>
          <span style="color:#27ae60;font-size:16px;">━</span>&nbsp;<span style="color:#111;">Footway / Path</span><br>
          <span style="color:#1abc9c;font-size:16px;">━</span>&nbsp;<span style="color:#111;">Pedestrian</span><br>
          <span style="color:#8e44ad;font-size:16px;">━</span>&nbsp;<span style="color:#111;">Living street</span><br>
          <span style="color:#e74c3c;font-size:16px;">━</span>&nbsp;<span style="color:#111;">Primary road</span><br>
        </div>
        """))

    # Render — key change forces full remount, clearing drawn shapes after reset
    map_output = st_folium(
        m,
        key=f"map_{st.session_state.map_key}",
        use_container_width=True,
        height=620,
        returned_objects=["last_active_drawing", "center", "zoom"],
    )

    # ── Process map output ─────────────────────────────────────────────────
    if map_output:
        if map_output.get("center"):
            c = map_output["center"]
            st.session_state.map_center = [c["lat"], c["lng"]]
        if map_output.get("zoom"):
            st.session_state.map_zoom = map_output["zoom"]

        drawing = map_output.get("last_active_drawing")
        if drawing:
            try:
                new_poly = shape(drawing["geometry"])
                new_wkt  = new_poly.wkt

                # Ignore the SAME drawing we just reset (stale state from old map widget)
                if new_wkt == st.session_state.reset_polygon_wkt:
                    pass
                elif new_poly != st.session_state.polygon:
                    st.session_state.polygon            = new_poly
                    st.session_state.buildings          = []
                    st.session_state.edges_geojson      = None
                    st.session_state.fetch_done         = False
                    st.session_state.selected_types     = []
                    st.session_state.reset_polygon_wkt  = None  # clear once a new shape is drawn
                    st.rerun()
            except Exception:
                pass
