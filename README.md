# 🗺️ Fundraising Route Planner

A personal tool for team leaders doing door-to-door fundraising. Plan efficient, road-following routes, split work across teams and cars, and get per-team street-by-street briefing sheets — all from OpenStreetMap data, with no account required.

---

## ⚠️ Disclaimer

**This is an independent personal project.** It is not created, endorsed, sponsored, or condoned by any employer, company, organisation, political party, charity, or any other entity. Use is entirely at the user's own risk and responsibility. Users must ensure their fundraising activities comply with all applicable laws, regulations, and any rules of their own organisation. Provided "as is" with no warranty of any kind.

---

## Features

### Setup wizard
- Transport mode: **car** (with 1, 2 or 3 cars) or **walk / public transport**
- Team size: 1–20 people, automatically split into 2-person teams
- Shift duration with a fixed 30-minute break
- Average door time (answered/unanswered rates modelled separately)
- **Advanced:** optional isolation threshold to skip houses far from their neighbours

### Map & area selection
- Address search bar (click the 🔍 icon, top-left of the map)
- Draw your work area with the rectangle or polygon tool
- Area size limit: 10 km² maximum

### Parking (car mode)
- **Find parking in visible area** fetches free parking, schools and kindergartens from OSM
- Results shown in a scrollable list; click to confirm a spot for each car
- **Show/Hide** toggle prevents accidental parking-marker clicks while drawing
- Confirmed spots persist until explicitly reset

### Route planning
- Fetches all residential buildings and address nodes inside your drawn polygon
- Fetches the full walkable street network
- Assigns buildings to teams using two-level geographic clustering (car → team)
- Road-following routes via the **Rural Postman Problem** algorithm (Hierholzer / Dijkstra)
- Route end-points target the car's parking spot, nearest transit stop, or start point

### Map display
- Colour-coded team zones (convex-hull polygons)
- Road-following paths with **direction arrows** (~120 m spacing)
- **Route detail** control: Full roads / Simplified (Douglas–Peucker) / Waypoints only
- Legend with team colours, start/end icons — **toggleable**
- Up to 3 car-start markers in distinct colours

### Route summary
- Per-team card: houses, walking distance, talk time, break, shift fit
- Expandable **street-by-street plan** listing house numbers per street — ready to hand to volunteers
- Groups teams by car when multiple cars are used

---

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

---

## Deploying to Streamlit Cloud (free)

1. Push this repository to GitHub.
2. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. Click **New app**, select your repo, set main file to `app.py`, and click **Deploy**.

Your app will be live at a public URL in ~2 minutes. Works on any device including phones.

---

## Data sources

- **Buildings, addresses, streets, transit, parking:** [OpenStreetMap](https://www.openstreetmap.org) via [Overpass API](https://overpass-api.de)
- All data is free and open source.
