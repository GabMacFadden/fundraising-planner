import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw, MeasureControl
import overpy
import osmnx as ox
from shapely.geometry import shape, Point
import json

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Fundraising Route Planner",
    page_icon="🗺️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom styling ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main-header {
        font-size: 2rem;
        font-weight: 700;
        color: #1a1a2e;
        margin-bottom: 0;
    }
    .sub-header {
        color: #666;
        font-size: 0.9rem;
        margin-top: 0;
        margin-bottom: 1.5rem;
    }
    .stat-card {
        background: #f8f9fa;
        border-left: 4px solid #2980b9;
        padding: 0.75rem 1rem;
        border-radius: 4px;
        margin-bottom: 0.5rem;
    }
    .phase-badge {
        background: #2980b9;
        color: white;
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    .step-label {
        font-weight: 600;
        font-size: 0.9rem;
        color: #333;
        margin-bottom: 0.25rem;
    }
</style>
""", unsafe_allow_html=True)

# ── Session state init ─────────────────────────────────────────────────────────
defaults = {
    'buildings': [],
    'graph': None,
    'polygon': None,
    'map_center': [59.9139, 10.7522],  # Default: Oslo, Norway
    'map_zoom': 14,
    'fetch_done': False,
}
for key, val in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = val

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown('<p class="main-header">🗺️ Fundraising Route Planner</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub-header"><span class="phase-badge">Phase 1</span>'
    '&nbsp; Area selection · House detection · Street network</p>',
    unsafe_allow_html=True
)

# ── Layout ─────────────────────────────────────────────────────────────────────
map_col, ctrl_col = st.columns([3, 1])

# ── Control panel ──────────────────────────────────────────────────────────────
with ctrl_col:

    # Step 1 instructions
    st.markdown('<p class="step-label">Step 1 — Draw your area</p>', unsafe_allow_html=True)
    st.caption("Use the ◻ rectangle or polygon tool on the map. Draw around the neighbourhood your team will cover today.")

    st.divider()

    # Step 2 — show area status + fetch button
    st.markdown('<p class="step-label">Step 2 — Fetch map data</p>', unsafe_allow_html=True)

    polygon = st.session_state.polygon
    if polygon:
        # Rough area estimate in km²
        area_km2 = polygon.area * (111.32 ** 2)
        st.success(f"Area selected  (~{area_km2:.3f} km²)")
    else:
        st.info("No area drawn yet.")

    fetch_clicked = st.button(
        "🔍 Fetch Houses & Streets",
        type="primary",
        disabled=(polygon is None),
        use_container_width=True,
    )

    if fetch_clicked and polygon:
        progress = st.progress(0, "Connecting to OpenStreetMap…")
        error_occurred = False

        try:
            # Polygon bounds → Overpass bbox (south, west, north, east)
            minx, miny, maxx, maxy = polygon.bounds
            s, w, n, e = miny, minx, maxy, maxx

            # ── Buildings via Overpass API ─────────────────────────────────
            progress.progress(15, "Fetching residential buildings…")
            api = overpy.Overpass()

            query = f"""
[out:json][timeout:60];
(
  way["building"~"house|residential|detached|apartments|yes|terrace|semidetached_house|bungalow|cabin|farm|dormitory|duplex"]({s},{w},{n},{e});
  node["addr:housenumber"]({s},{w},{n},{e});
);
out center;
"""
            result = api.query(query)
            progress.progress(45, "Processing buildings…")

            buildings = []
            seen_coords = set()

            for way in result.ways:
                if way.center_lat and way.center_lon:
                    lat = float(way.center_lat)
                    lon = float(way.center_lon)
                    coord_key = (round(lat, 5), round(lon, 5))
                    if coord_key not in seen_coords and polygon.contains(Point(lon, lat)):
                        seen_coords.add(coord_key)
                        buildings.append({
                            'lat': lat,
                            'lon': lon,
                            'type': way.tags.get('building', 'building'),
                            'housenumber': way.tags.get('addr:housenumber', ''),
                            'street': way.tags.get('addr:street', ''),
                            'name': way.tags.get('name', ''),
                        })

            for node in result.nodes:
                lat = float(node.lat)
                lon = float(node.lon)
                coord_key = (round(lat, 5), round(lon, 5))
                if coord_key not in seen_coords and polygon.contains(Point(lon, lat)):
                    seen_coords.add(coord_key)
                    buildings.append({
                        'lat': lat,
                        'lon': lon,
                        'type': 'address_node',
                        'housenumber': node.tags.get('addr:housenumber', ''),
                        'street': node.tags.get('addr:street', ''),
                        'name': '',
                    })

            st.session_state.buildings = buildings

            # ── Walkable street network via OSMnx ─────────────────────────
            progress.progress(60, "Fetching walkable street network…")
            G = ox.graph_from_polygon(polygon, network_type='walk')
            st.session_state.graph = G
            st.session_state.fetch_done = True

            progress.progress(100, f"Done! Found {len(buildings)} buildings.")

        except overpy.exception.DataWrongType as e:
            st.error(f"Overpass API error: {e}. Try a smaller area.")
            error_occurred = True
        except Exception as e:
            st.error(f"Error fetching data: {e}")
            error_occurred = True

        if not error_occurred:
            st.rerun()

    # ── Stats panel ────────────────────────────────────────────────────────
    if st.session_state.fetch_done:
        st.divider()
        st.markdown('<p class="step-label">📊 Area Stats</p>', unsafe_allow_html=True)

        b_count = len(st.session_state.buildings)
        st.metric("🏠 Buildings / addresses", b_count)

        if st.session_state.graph is not None:
            G = st.session_state.graph
            st.metric("🛣️ Street segments", len(G.edges()))
            st.metric("🔀 Intersections / nodes", len(G.nodes()))

        # House type breakdown
        if st.session_state.buildings:
            types = {}
            for b in st.session_state.buildings:
                t = b['type']
                types[t] = types.get(t, 0) + 1
            with st.expander("Building types"):
                for t, count in sorted(types.items(), key=lambda x: -x[1]):
                    st.write(f"`{t}` — {count}")

        st.divider()

    # ── Roadmap ────────────────────────────────────────────────────────────
    st.markdown('<p class="step-label">📍 Roadmap</p>', unsafe_allow_html=True)
    done = st.session_state.fetch_done
    st.markdown(f"""
{"✅" if polygon else "⏳"} Area selection  
{"✅" if done else "⏳"} House detection  
{"✅" if done else "⏳"} Street network  
⏳ Route planning *(Phase 2)*  
⏳ Team splitting *(Phase 3)*  
⏳ Parking & transit *(Phase 4)*  
""")

    # Reset button
    if st.session_state.fetch_done or st.session_state.polygon:
        st.divider()
        if st.button("🔄 Reset", use_container_width=True):
            for key in ['buildings', 'graph', 'polygon', 'fetch_done']:
                st.session_state[key] = [] if key == 'buildings' else None if key != 'fetch_done' else False
            st.rerun()

# ── Map ────────────────────────────────────────────────────────────────────────
with map_col:
    center = st.session_state.map_center
    zoom = st.session_state.map_zoom

    m = folium.Map(
        location=center,
        zoom_start=zoom,
        tiles="OpenStreetMap",
        prefer_canvas=True,
    )

    # Drawing tools
    Draw(
        export=False,
        draw_options={
            'polyline': False,
            'polygon': True,
            'circle': False,
            'marker': False,
            'circlemarker': False,
            'rectangle': True,
        },
        edit_options={'edit': False, 'remove': True},
    ).add_to(m)

    MeasureControl(position='bottomleft', primary_length_unit='meters').add_to(m)

    # ── Overlay: street network ────────────────────────────────────────────
    if st.session_state.graph is not None:
        G = st.session_state.graph
        try:
            edges_gdf = ox.graph_to_gdfs(G, nodes=False)
            for _, edge in edges_gdf.iterrows():
                try:
                    coords = [(lat, lon) for lon, lat in edge.geometry.coords]
                    # Colour by highway type
                    htype = edge.get('highway', 'residential')
                    if isinstance(htype, list):
                        htype = htype[0]
                    color = {
                        'primary': '#e74c3c',
                        'secondary': '#e67e22',
                        'tertiary': '#f1c40f',
                        'residential': '#2980b9',
                        'footway': '#27ae60',
                        'path': '#27ae60',
                        'pedestrian': '#27ae60',
                        'living_street': '#8e44ad',
                    }.get(htype, '#2980b9')
                    folium.PolyLine(
                        coords,
                        color=color,
                        weight=2.5,
                        opacity=0.75,
                        tooltip=htype,
                    ).add_to(m)
                except Exception:
                    pass
        except Exception:
            pass

    # ── Overlay: buildings ─────────────────────────────────────────────────
    if st.session_state.buildings:
        for b in st.session_state.buildings:
            label_parts = []
            if b.get('street'):
                label_parts.append(b['street'])
            if b.get('housenumber'):
                label_parts.append(b['housenumber'])
            if b.get('name'):
                label_parts.append(f"({b['name']})")
            label = ' '.join(label_parts) if label_parts else b['type']

            folium.CircleMarker(
                location=[b['lat'], b['lon']],
                radius=5,
                color='#c0392b',
                fill=True,
                fill_color='#e74c3c',
                fill_opacity=0.85,
                tooltip=f"🏠 {label}",
            ).add_to(m)

    # ── Legend ─────────────────────────────────────────────────────────────
    if st.session_state.fetch_done:
        legend_html = """
        <div style="position:fixed;bottom:30px;right:10px;z-index:1000;
                    background:white;padding:10px 14px;border-radius:8px;
                    border:1px solid #ccc;font-size:12px;line-height:1.8;">
            <b>Legend</b><br>
            <span style="color:#e74c3c">●</span> Building / Address<br>
            <span style="color:#2980b9">─</span> Residential street<br>
            <span style="color:#27ae60">─</span> Footway / Path<br>
            <span style="color:#8e44ad">─</span> Living street<br>
            <span style="color:#e74c3c">─</span> Primary road<br>
        </div>
        """
        m.get_root().html.add_child(folium.Element(legend_html))

    # ── Render map and capture interactions ───────────────────────────────
    map_output = st_folium(
        m,
        use_container_width=True,
        height=600,
        returned_objects=["last_active_drawing", "center", "zoom"],
    )

    # Update map center/zoom from user navigation
    if map_output:
        if map_output.get('center'):
            c = map_output['center']
            st.session_state.map_center = [c['lat'], c['lng']]
        if map_output.get('zoom'):
            st.session_state.map_zoom = map_output['zoom']

        # Capture drawn polygon
        if map_output.get('last_active_drawing'):
            try:
                geom = map_output['last_active_drawing']['geometry']
                new_polygon = shape(geom)
                if new_polygon != st.session_state.polygon:
                    st.session_state.polygon = new_polygon
                    # Clear previous fetch results when a new area is drawn
                    st.session_state.buildings = []
                    st.session_state.graph = None
                    st.session_state.fetch_done = False
                    st.rerun()
            except Exception:
                pass
