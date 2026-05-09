import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw, MeasureControl, MarkerCluster
import osmnx as ox
from shapely.geometry import shape, Point
import geopandas as gpd

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Fundraising Route Planner",
    page_icon="🗺️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Overpass mirrors (tried in order if one fails) ────────────────────────────
OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

def set_osmnx_endpoint(url: str):
    """Set the Overpass endpoint in osmnx (handles API differences across versions)."""
    for attr in ("overpass_endpoint", "overpass_url"):
        if hasattr(ox.settings, attr):
            setattr(ox.settings, attr, url)
            return

# ── Human-readable building type labels ───────────────────────────────────────
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

def label(raw: str) -> str:
    return BUILDING_LABELS.get(raw, raw.replace("_", " ").capitalize())

# ── Session state defaults ─────────────────────────────────────────────────────
DEFAULTS = {
    "buildings":      [],
    "graph":          None,
    "polygon":        None,
    "map_center":     [59.9139, 10.7522],  # Oslo, Norway
    "map_zoom":       14,
    "fetch_done":     False,
    "map_key":        0,                   # Increment to force full map re-render on reset
    "selected_types": [],
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── One-time "ready" toast ────────────────────────────────────────────────────
if "toasted_ready" not in st.session_state:
    st.session_state.toasted_ready = True
    st.toast("✅ App is ready to use!", icon="🗺️")

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .main-title  { font-size:2rem; font-weight:700; color:#1a1a2e; margin-bottom:0; }
  .sub-title   { color:#666; font-size:.9rem; margin-top:0; margin-bottom:1.5rem; }
  .step-label  { font-weight:600; font-size:.9rem; color:#333; margin-bottom:.2rem; }
  .phase-badge { background:#2980b9; color:#fff; padding:2px 8px;
                 border-radius:12px; font-size:.75rem; font-weight:600; }
</style>
""", unsafe_allow_html=True)

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown('<p class="main-title">🗺️ Fundraising Route Planner</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub-title"><span class="phase-badge">Phase 1</span>'
    '&nbsp; Area selection · House detection · Street network</p>',
    unsafe_allow_html=True,
)

# ── Layout ─────────────────────────────────────────────────────────────────────
map_col, ctrl_col = st.columns([3, 1])

# ══════════════════════════════════════════════════════════════════════════════
# CONTROL PANEL
# ══════════════════════════════════════════════════════════════════════════════
with ctrl_col:

    # Step 1
    st.markdown('<p class="step-label">Step 1 — Draw your area</p>', unsafe_allow_html=True)
    st.caption("Use the ◻ rectangle or ⬡ polygon tool on the map.")
    st.divider()

    # Step 2
    st.markdown('<p class="step-label">Step 2 — Fetch map data</p>', unsafe_allow_html=True)

    polygon = st.session_state.polygon
    if polygon:
        area_km2 = polygon.area * (111.32 ** 2)
        st.success(f"Area selected (~{area_km2:.3f} km²)")
    else:
        st.info("No area drawn yet.")

    fetch_clicked = st.button(
        "🔍 Fetch Houses & Streets",
        type="primary",
        disabled=(polygon is None),
        use_container_width=True,
    )

    # ── Data fetch ────────────────────────────────────────────────────────────
    if fetch_clicked and polygon:
        progress  = st.progress(0, "Connecting to OpenStreetMap…")
        buildings = []
        error_msg = None

        # Try each Overpass mirror for building data
        for mirror_url in OVERPASS_MIRRORS:
            try:
                set_osmnx_endpoint(mirror_url)
                host = mirror_url.split("/")[2]
                progress.progress(15, f"Fetching buildings via {host}…")

                # Building polygons
                raw_buildings = ox.features_from_polygon(
                    polygon,
                    tags={"building": True},
                )

                # Address nodes (may not exist in all areas)
                try:
                    raw_addresses = ox.features_from_polygon(
                        polygon,
                        tags={"addr:housenumber": True},
                    )
                except Exception:
                    raw_addresses = gpd.GeoDataFrame()

                progress.progress(45, "Processing buildings…")
                seen = set()

                for _, row in raw_buildings.iterrows():
                    geom = row.geometry
                    if geom is None:
                        continue
                    centroid  = geom.centroid
                    lat, lon  = centroid.y, centroid.x
                    key       = (round(lat, 5), round(lon, 5))
                    if key in seen or not polygon.contains(Point(lon, lat)):
                        continue
                    seen.add(key)
                    btype = str(row.get("building", "yes") or "yes")
                    buildings.append({
                        "lat":         lat,
                        "lon":         lon,
                        "type":        btype,
                        "housenumber": str(row.get("addr:housenumber", "") or ""),
                        "street":      str(row.get("addr:street", "")     or ""),
                        "name":        str(row.get("name", "")            or ""),
                    })

                if not raw_addresses.empty:
                    for _, row in raw_addresses.iterrows():
                        geom = row.geometry
                        if geom is None:
                            continue
                        centroid  = geom.centroid
                        lat, lon  = centroid.y, centroid.x
                        key       = (round(lat, 5), round(lon, 5))
                        if key in seen or not polygon.contains(Point(lon, lat)):
                            continue
                        seen.add(key)
                        buildings.append({
                            "lat":         lat,
                            "lon":         lon,
                            "type":        "address_node",
                            "housenumber": str(row.get("addr:housenumber", "") or ""),
                            "street":      str(row.get("addr:street", "")     or ""),
                            "name":        "",
                        })

                error_msg = None
                break  # success — stop trying mirrors

            except Exception as exc:
                error_msg = str(exc)
                continue

        if error_msg:
            st.error(
                f"Could not fetch building data from any Overpass mirror.\n\n"
                f"Last error: `{error_msg}`\n\n"
                "Try selecting a smaller area, or wait a moment and try again."
            )
        else:
            st.session_state.buildings      = buildings
            st.session_state.selected_types = sorted({b["type"] for b in buildings})

            # Street network (also tries mirrors)
            progress.progress(65, "Fetching walkable street network…")
            street_error = None
            for mirror_url in OVERPASS_MIRRORS:
                try:
                    set_osmnx_endpoint(mirror_url)
                    G = ox.graph_from_polygon(polygon, network_type="walk")
                    st.session_state.graph = G
                    street_error = None
                    break
                except Exception as ge:
                    street_error = str(ge)
                    continue

            if street_error:
                st.warning(f"Street network could not be loaded: {street_error}")

            st.session_state.fetch_done = True
            progress.progress(100, f"Done! Found {len(buildings)} buildings.")
            st.rerun()

    # ── Building type filter ──────────────────────────────────────────────────
    if st.session_state.fetch_done and st.session_state.buildings:
        st.divider()
        st.markdown('<p class="step-label">🏠 Filter building types</p>', unsafe_allow_html=True)
        st.caption("Uncheck types you don't want shown on the map.")

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

    # ── Stats ─────────────────────────────────────────────────────────────────
    if st.session_state.fetch_done:
        st.divider()
        st.markdown('<p class="step-label">📊 Stats</p>', unsafe_allow_html=True)

        visible = [b for b in st.session_state.buildings
                   if b["type"] in st.session_state.selected_types]
        st.metric("🏠 Visible buildings", len(visible))
        st.metric("📦 Total detected",    len(st.session_state.buildings))

        if st.session_state.graph is not None:
            G = st.session_state.graph
            st.metric("🛣️ Street segments", len(G.edges()))
            st.metric("🔀 Intersections",   len(G.nodes()))

    # ── Roadmap ───────────────────────────────────────────────────────────────
    st.divider()
    st.markdown('<p class="step-label">📍 Roadmap</p>', unsafe_allow_html=True)
    done = st.session_state.fetch_done
    poly = st.session_state.polygon is not None
    st.markdown(f"""
{"✅" if poly else "⏳"} Area selection  
{"✅" if done else "⏳"} House detection  
{"✅" if done else "⏳"} Street network  
⏳ Route planning *(Phase 2)*  
⏳ Team splitting *(Phase 3)*  
⏳ Parking & transit *(Phase 4)*  
""")

    # ── Reset ─────────────────────────────────────────────────────────────────
    if poly or done:
        st.divider()
        if st.button("🔄 Reset everything", use_container_width=True):
            new_key = st.session_state.map_key + 1
            for k, v in DEFAULTS.items():
                st.session_state[k] = v
            st.session_state.map_key = new_key  # force full map re-render
            st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# MAP
# ══════════════════════════════════════════════════════════════════════════════
with map_col:
    m = folium.Map(
        location=st.session_state.map_center,
        zoom_start=st.session_state.map_zoom,
        tiles="OpenStreetMap",
        prefer_canvas=True,
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

    # ── Street network ─────────────────────────────────────────────────────────
    if st.session_state.graph is not None:
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
        try:
            edges_gdf = ox.graph_to_gdfs(st.session_state.graph, nodes=False)
            for _, edge in edges_gdf.iterrows():
                try:
                    coords = [(lat, lon) for lon, lat in edge.geometry.coords]
                    htype  = edge.get("highway", "residential")
                    if isinstance(htype, list):
                        htype = htype[0]
                    color = HIGHWAY_COLORS.get(str(htype), "#2980b9")
                    folium.PolyLine(
                        coords, color=color, weight=2.5, opacity=0.8,
                        tooltip=str(htype),
                    ).add_to(m)
                except Exception:
                    pass
        except Exception:
            pass

    # ── Buildings (clustered to avoid clutter) ─────────────────────────────────
    if st.session_state.buildings and st.session_state.selected_types:
        cluster = MarkerCluster(
            options={
                "maxClusterRadius":        40,
                "disableClusteringAtZoom": 17,   # show individual dots when zoomed in
                "spiderfyOnMaxZoom":       True,
            },
        )
        for b in st.session_state.buildings:
            if b["type"] not in st.session_state.selected_types:
                continue
            parts = [p for p in [b["street"], b["housenumber"]] if p]
            addr  = " ".join(parts) if parts else None
            tip   = f"🏠 {label(b['type'])}" + (f" — {addr}" if addr else "")
            folium.CircleMarker(
                location=[b["lat"], b["lon"]],
                radius=5,
                color="#c0392b",
                fill=True,
                fill_color="#e74c3c",
                fill_opacity=0.85,
                tooltip=tip,
            ).add_to(cluster)
        cluster.add_to(m)

    # ── Legend ─────────────────────────────────────────────────────────────────
    if st.session_state.fetch_done:
        m.get_root().html.add_child(folium.Element("""
        <div style="
            position:fixed; bottom:36px; right:10px; z-index:9999;
            background:rgba(255,255,255,0.97);
            padding:10px 14px; border-radius:8px; border:1px solid #bbb;
            font-size:12px; line-height:2; color:#111;
            box-shadow:2px 2px 6px rgba(0,0,0,0.18);
            font-family:Arial,sans-serif;">
          <b style="color:#111;">Legend</b><br>
          <span style="color:#e74c3c;font-size:16px;">●</span>&nbsp;<span style="color:#111;">Building / Address</span><br>
          <span style="color:#2980b9;font-size:16px;">&#9644;</span>&nbsp;<span style="color:#111;">Residential</span><br>
          <span style="color:#27ae60;font-size:16px;">&#9644;</span>&nbsp;<span style="color:#111;">Footway / Path</span><br>
          <span style="color:#1abc9c;font-size:16px;">&#9644;</span>&nbsp;<span style="color:#111;">Pedestrian</span><br>
          <span style="color:#8e44ad;font-size:16px;">&#9644;</span>&nbsp;<span style="color:#111;">Living street</span><br>
          <span style="color:#e74c3c;font-size:16px;">&#9644;</span>&nbsp;<span style="color:#111;">Primary road</span><br>
        </div>
        """))

    # ── Render ─────────────────────────────────────────────────────────────────
    # The key changes on reset, which forces st_folium to fully re-mount,
    # clearing any drawn shapes from the previous session.
    map_output = st_folium(
        m,
        key=f"map_{st.session_state.map_key}",
        use_container_width=True,
        height=620,
        returned_objects=["last_active_drawing", "center", "zoom"],
    )

    # Persist pan/zoom
    if map_output:
        if map_output.get("center"):
            c = map_output["center"]
            st.session_state.map_center = [c["lat"], c["lng"]]
        if map_output.get("zoom"):
            st.session_state.map_zoom = map_output["zoom"]

        # Detect newly drawn area
        drawing = map_output.get("last_active_drawing")
        if drawing:
            try:
                new_poly = shape(drawing["geometry"])
                if new_poly != st.session_state.polygon:
                    st.session_state.polygon        = new_poly
                    st.session_state.buildings      = []
                    st.session_state.graph          = None
                    st.session_state.fetch_done     = False
                    st.session_state.selected_types = []
                    st.rerun()
            except Exception:
                pass
