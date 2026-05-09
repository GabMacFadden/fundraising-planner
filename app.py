"""
Fundraising Route Planner — Phase 1
Area selection · house detection · walkable street display.

Phase 1 uses a single direct Overpass query for everything (buildings,
addresses, walkable streets). Streamlit reruns are confined to user
*actions* — pan/zoom never trigger them.
"""

import math
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

# Required by Overpass usage policy. Without it, mirrors return 406/429.
USER_AGENT = "FundraisingRoutePlanner/1.0 (https://streamlit.io)"

# Hard cap on selectable area to prevent abuse and timeouts
MAX_AREA_KM2 = 4.0
REQUEST_TIMEOUT = 60     # seconds

# Walkable street types (excludes motorways/trunks where pedestrians don't go)
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

DEFAULTS = {
    "buildings":         [],
    "streets_geojson":   None,
    "polygon":           None,
    "fetch_done":        False,
    "map_key":           0,
    "selected_types":    [],
    "reset_polygon_wkt": None,
}


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def label(raw: str) -> str:
    return BUILDING_LABELS.get(raw, raw.replace("_", " ").capitalize())


def approximate_area_km2(polygon) -> float:
    """Latitude-corrected area in km²."""
    if polygon is None:
        return 0.0
    centroid = polygon.centroid
    lat_km = 111.32
    lon_km = 111.32 * math.cos(math.radians(centroid.y))
    return polygon.area * lat_km * lon_km


def overpass_query(polygon) -> str:
    """ONE combined query for buildings, addresses and walkable streets."""
    minx, miny, maxx, maxy = polygon.bounds
    s, w, n, e = miny, minx, maxy, maxx
    return f"""
[out:json][timeout:{REQUEST_TIMEOUT}];
(
  way["building"]({s},{w},{n},{e});
  node["addr:housenumber"]({s},{w},{n},{e});
  way["highway"~"^({WALKABLE_HIGHWAYS})$"]({s},{w},{n},{e});
);
out geom tags;
""".strip()


def fetch_overpass(polygon):
    """Try mirrors in order. Returns (data_dict, used_url, err_str)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    query = overpass_query(polygon)
    last_err = None
    for url in OVERPASS_MIRRORS:
        host = url.split("/")[2]
        try:
            r = requests.post(
                url,
                data={"data": query},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                return r.json(), url, None
            last_err = f"HTTP {r.status_code} from {host}"
        except requests.exceptions.Timeout:
            last_err = f"Timeout from {host}"
        except Exception as e:
            last_err = f"{type(e).__name__} from {host}: {e}"
    return None, None, last_err


def parse_overpass(data, polygon):
    """Parse Overpass JSON → (buildings list, streets GeoJSON FeatureCollection)."""
    if not data or "elements" not in data:
        return [], {"type": "FeatureCollection", "features": []}

    buildings = []
    seen      = set()
    streets   = []

    for el in data["elements"]:
        tags  = el.get("tags", {})
        etype = el.get("type")

        if etype == "way":
            geom = el.get("geometry", [])
            if not geom:
                continue

            if "highway" in tags:
                # Walkable street
                coords = [[n["lon"], n["lat"]] for n in geom]
                if len(coords) < 2:
                    continue
                streets.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {
                        "highway": tags.get("highway", ""),
                        "name":    tags.get("name", ""),
                    },
                })
            elif "building" in tags:
                # Building polygon → use centroid (mean of coordinates)
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

        elif etype == "node" and "addr:housenumber" in tags:
            lat, lon = el.get("lat"), el.get("lon")
            if lat is None or lon is None:
                continue
            key = (round(lat, 5), round(lon, 5))
            if key in seen or not polygon.contains(Point(lon, lat)):
                continue
            seen.add(key)
            buildings.append({
                "lat":         lat,
                "lon":         lon,
                "type":        "address_node",
                "housenumber": tags.get("addr:housenumber", ""),
                "street":      tags.get("addr:street", ""),
                "name":        "",
            })

    streets_fc = {"type": "FeatureCollection", "features": streets}
    return buildings, streets_fc


# ══════════════════════════════════════════════════════════════════════════════
# CACHED FETCH
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False, ttl=3600, max_entries=20)
def fetch_all(polygon_wkt: str):
    """Fetch buildings + streets in one shot. Cached by polygon WKT."""
    polygon = shapely_wkt.loads(polygon_wkt)
    raw, mirror, err = fetch_overpass(polygon)
    if err:
        return {"error": err}
    buildings, streets = parse_overpass(raw, polygon)
    return {
        "buildings":       buildings,
        "streets_geojson": streets,
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
</style>
""", unsafe_allow_html=True)

st.markdown('<p class="main-title">🗺️ Fundraising Route Planner</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub-title"><span class="phase-badge">Phase 1</span>'
    '&nbsp; Area selection · House detection · Street display</p>',
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
            st.session_state.selected_types  = sorted({b["type"] for b in result["buildings"]})
            st.session_state.fetch_done      = True
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

        if st.session_state.streets_geojson:
            st.metric("🛣️ Street segments",
                      len(st.session_state.streets_geojson.get("features", [])))

    # ── Roadmap ────────────────────────────────────────────────────────────
    st.divider()
    st.markdown('<p class="step-label">📍 Roadmap</p>', unsafe_allow_html=True)
    done = st.session_state.fetch_done
    poly = polygon is not None
    st.markdown(f"""
{"✅" if poly else "⏳"} Area selection  
{"✅" if done else "⏳"} House detection  
{"✅" if done else "⏳"} Street display  
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
            st.session_state.map_key           = new_key
            st.session_state.reset_polygon_wkt = old_wkt
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# MAP
# ══════════════════════════════════════════════════════════════════════════════

with map_col:

    # Initial viewport — frame the polygon if one exists, else default to Oslo
    if st.session_state.polygon:
        c           = st.session_state.polygon.centroid
        init_center = [c.y, c.x]
        # Larger area → smaller zoom
        a = approximate_area_km2(st.session_state.polygon)
        if   a > 2.0:    init_zoom = 14
        elif a > 0.5:    init_zoom = 15
        elif a > 0.1:    init_zoom = 16
        else:            init_zoom = 17
    else:
        init_center = [59.9139, 10.7522]   # Oslo
        init_zoom   = 14

    m = folium.Map(
        location=init_center,
        zoom_start=init_zoom,
        tiles="OpenStreetMap",
        prefer_canvas=True,
    )

    # If a polygon exists, ensure it's framed (overrides initial zoom on first render)
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

    # Streets — single GeoJson layer
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
            tooltip=folium.GeoJsonTooltip(fields=["highway", "name"], aliases=["Type:", "Name:"]),
        ).add_to(m)

    # Buildings — clustered, JS-side construction
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

    # Legend
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

    # ─────────────────────────────────────────────────────────────────────
    # CRITICAL: only return last_active_drawing.
    # This stops pan/zoom from causing reruns and also stops drawings from
    # getting interrupted mid-gesture.
    # ─────────────────────────────────────────────────────────────────────
    map_output = st_folium(
        m,
        key=f"map_{st.session_state.map_key}",
        use_container_width=True,
        height=620,
        returned_objects=["last_active_drawing"],
    )

    # Process the drawn polygon (if any)
    if map_output:
        drawing = map_output.get("last_active_drawing")
        if drawing:
            try:
                new_poly = shape(drawing["geometry"])
                new_wkt  = new_poly.wkt

                # Ignore if this matches what we just reset (stale state from old map)
                if new_wkt == st.session_state.reset_polygon_wkt:
                    pass
                elif new_poly != st.session_state.polygon:
                    st.session_state.polygon            = new_poly
                    st.session_state.buildings          = []
                    st.session_state.streets_geojson    = None
                    st.session_state.fetch_done         = False
                    st.session_state.selected_types     = []
                    st.session_state.reset_polygon_wkt  = None
                    st.rerun()
            except Exception:
                pass
