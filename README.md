# 🗺️ Fundraising Route Planner

A tool for team leaders doing door-to-door fundraising. Plan efficient walking routes, detect houses, and split work across teams.

---

## Current Phase: 1 — Area Selection & Data Fetching

**What it does:**
- Interactive map centred on Norway (default: Oslo)
- Draw a rectangle or polygon around your target neighbourhood
- Fetches all residential buildings and address points from OpenStreetMap
- Fetches the full walkable street network for the area
- Displays buildings (red dots) and colour-coded streets on the map
- Shows stats: building count, street segments, intersections

---

## Deploying to Streamlit Cloud (free)

### Step 1 — Push this project to GitHub
1. Create a free account at [github.com](https://github.com)
2. Create a new repository (e.g. `fundraising-planner`)
3. Upload all files from this folder (`app.py`, `requirements.txt`, `README.md`)

### Step 2 — Deploy on Streamlit Cloud
1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub
2. Click **New app**
3. Select your repository and set the main file to `app.py`
4. Click **Deploy** — your app will be live in ~2 minutes

### Step 3 — Share the link
Streamlit gives you a public URL (e.g. `https://yourname-fundraising-planner.streamlit.app`).
Share this with your team leaders — works on any device, any browser, including phones.

---

## Running locally (optional, for development)

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open `http://localhost:8501` in your browser.

---

## Planned Phases

| Phase | Feature |
|-------|---------|
| ✅ 1 | Area selection, house detection, street network |
| ⏳ 2 | Single-team route optimisation with time budget |
| ⏳ 3 | Multi-team zone splitting, left/right street assignment |
| ⏳ 4 | Free parking detection, public transport stops, break planning |

---

## Data Sources

- **Buildings & Addresses:** [OpenStreetMap](https://www.openstreetmap.org) via [Overpass API](https://overpass-api.de)
- **Street network:** OpenStreetMap via [OSMnx](https://osmnx.readthedocs.io)
- All data is free and open source. Coverage in Norwegian towns is generally excellent.
