# DrivingSimulator - Immersive Street View Driving Simulator

<p align="center">
  <a href="README.md">中文</a> | <b>English</b>
</p>

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Three.js](https://img.shields.io/badge/Three.js-r128-black.svg)](https://threejs.org/)
[![Leaflet](https://img.shields.io/badge/Leaflet-1.9.4-199900.svg)](https://leafletjs.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**DrivingSimulator** is an immersive virtual driving simulation system powered by real-world 360° panoramic street view imagery. By combining high-precision route planning, automated panorama tile fetching and stitching, a Three.js-driven 3D spherical rendering pipeline, realistic vehicle dynamics, and a sleek cockpit HUD, DrivingSimulator enables users to pick arbitrary origin and destination points anywhere in the world and cruise through real city streets right in their browser.

---

## 🌟 Key Features

### 1. Interactive Route Planning & Map Layers
- **Multi-layer Base Maps & Coverage Grid**: Toggle between Google HD Satellite Imagery (hybrid with road networks/labels), clean satellite maps, standard street vector maps, and real-time Street View coverage lines (blue lines overlay).
- **Intelligent Routing via OSRM**: Plan accurate road trips using the open-source OSRM routing engine with bearing-angle constraints to prevent unnecessary U-turns and lane mismatches.
- **Reverse Geocoding**: Automatically resolves start/end addresses, road names, and cities using OpenStreetMap Nominatim.

### 2. High-Performance Asynchronous Pipeline
- **Concurrent Resumable Fetching**: Parallelized panorama fetching and tile assembly using `streetlevel` and `aiohttp`. Download tasks can be cancelled or resumed cleanly at any time.
- **Heading Alignment & Lane Verification**: Automatically filters out inverse-direction capture points based on the route's forward vector, keeping the camera facing forward along the lane.
- **Global Asset Deduplication & Caching**: Reuses cached panoramic frames keyed by `pano_id`, eliminating duplicate downloads on overlapping segments.
- **Seamless Route Extension**: When reaching the destination, pick a new endpoint to seamlessly append additional legs to your journey without resetting.

### 3. Immersive 3D Cockpit (Three.js)
- **360° Spherical Panoramic Viewport**: Freely look around using mouse drag; adjust FOV smoothly between 30° and 100° with the scroll wheel.
- **Realistic Vehicle Dynamics**: Features throttle acceleration, progressive braking, inertial damping/coasting, Cruise Control, and Instant Speed Lock.
- **Heading Trim**: Micro-adjust the vehicle's heading offset via hotkeys to keep the car perfectly aligned with the lane center.
- **Modern Cockpit HUD & Mini-Map**:
  - Digital speedometer (km/h) and driving mode indicators (Accel, Brake, Cruise, Lock).
  - Odometer tracking (current distance vs. total trip distance).
  - Capture date watermark (Year / Month).
  - Picture-in-picture mini-map showing real-time GPS coordinates and vehicle orientation needle.

### 4. Route Book Persistence (SQLite)
- Automatically or manually saves planned routes and configuration to an SQLite database (`routes.db`).
- Easily load previous road trips or manage your favorite travel routes.

---

## 🛠️ Architecture & Tech Stack

- **Backend Core**: `FastAPI` + `Uvicorn` + `Asyncio` + `aiohttp` + `SQLite3`
- **Panorama Processing**: `streetlevel` + `Pillow`
- **Frontend & Rendering**: `Three.js (r128)` + `Leaflet.js` + Native HTML5 / CSS3 (Dark Glassmorphism UI)
- **Upstream Data Services**: Google Street View / Tiles, OSRM Routing Engine, OpenStreetMap Nominatim

---

## 📁 Repository Structure

```text
DrivingSimulator/
├── server.py                   # FastAPI backend server (routing, tile proxy, panorama assembly, SQLite API)
├── clean_port.py               # Utility to detect and terminate conflicting processes on target ports
├── download_route_streetview.py # Standalone CLI panorama batch downloader
├── index.html                  # Main interactive frontend (Leaflet planning + Three.js driving cockpit)
├── index2.html                 # Alternate / lightweight frontend layout
├── start.bat                   # One-click Windows launch script (port check, auto pip install, web launch)
├── .env.example                # Template for environment configuration
├── LICENSE                     # MIT License
├── README.md                   # Chinese documentation
└── README_EN.md                # English documentation
```

---

## 🚀 Quick Start

### Prerequisites
- Python 3.9+
- Modern Web Browser (Chrome, Edge, Firefox, Safari)
- Network environment: Access to Google Maps / Street View tiles is required. If operating behind a network proxy, configure `PROXY_URL` in `.env`.

### Option A: One-Click Launch (Windows)
Double-click **`start.bat`** in the project root:
1. Automatically releases the port if previously occupied.
2. Checks and installs required Python packages if missing.
3. Starts the server and opens `http://127.0.0.1:3000` in your default browser.

### Option B: Manual Setup

1. **Install Dependencies**
   ```bash
   pip install streetlevel aiohttp pillow fastapi uvicorn python-dotenv
   ```

2. **Configure Environment**
   Copy `.env.example` to `.env` and adjust settings as needed:
   ```ini
   HOST=0.0.0.0
   PORT=3000
   
   # Proxy URL (e.g. http://127.0.0.1:7890). Leave blank if direct access is available.
   PROXY_URL=http://127.0.0.1:7890
   
   # Directory for panorama assets and tile cache
   DATA_DIR=streetview_route
   
   # SQLite database path
   DB_PATH=routes.db
   ```

3. **Run Server**
   ```bash
   python server.py
   ```
   Open `http://127.0.0.1:3000` in your browser.

---

## 🎮 Driving Controls

| Key / Action | Description |
| :--- | :--- |
| <kbd>W</kbd> or <kbd>↑</kbd> | **Throttle / Accelerate** (Hold to accelerate smoothly) |
| <kbd>S</kbd> or <kbd>↓</kbd> | **Brake / Reverse** |
| <kbd>Space</kbd> | **Toggle Cruise Control** (Maintains constant driving speed) |
| <kbd>U</kbd> | **Toggle Speed Lock** (Locks current speed; releasing throttle won't slow down) |
| <kbd>[</kbd> / <kbd>]</kbd> | **Heading Trim** (Adjust vehicle heading by -5° / +5° to align with road) |
| <kbd>P</kbd> | **Reset Heading Trim** (Restores original calculated bearing) |
| <kbd>R</kbd> | **Recenter Camera** (Resets view angle to front center) |
| <kbd>B</kbd> | **Restart Route** (Teleports vehicle back to the route origin) |
| **Mouse Drag** | **360° Free Look** (Pan viewport horizontally & vertically) |
| **Mouse Wheel** | **FOV Zoom** (Zoom in/out smoothly between wide-angle and telephoto) |

---

## ⚙️ Configuration & Tuning

Key parameters can be adjusted via the UI or `server.py`:
- **Sampling Step (`step_meters`)**: Defaults to 20m. Lowering this to 8–10m yields denser, smoother playback at the expense of more downloads.
- **Image Quality (`zoom_level`)**: Defaults to 3 (stitched resolution: 4096×2048), providing the best balance between fidelity and download speed.
- **Concurrency (`Semaphore`)**: Default limit is set to 5–6 concurrent requests to prevent throttling from upstream tile servers.

---

## 📄 License & Legal Disclaimer

### License
This project is licensed under the [MIT License](LICENSE).

### Legal Disclaimer
1. **Research & Prototype Purpose**: This project is developed as an experimental **technical prototype** focusing on WebGL 3D rendering, polyline distance interpolation, and first-person virtual perspective simulation. It is intended strictly for personal research, educational study, and technical exploration.
2. **No Media Asset Distribution**: This repository **does not contain, host, or distribute any copyrighted panoramic imagery, satellite tiles, or proprietary map data**. All map and street view data are fetched on-demand at runtime directly to the user's local machine and are excluded by default via `.gitignore`.
3. **Third-Party Terms of Service Compliance**: Trademarks and assets belonging to third parties (including Google Street View, Google Maps, OSRM, and OpenStreetMap) remain the intellectual property of their respective owners. Users are solely responsible for ensuring that their usage complies with all applicable Terms of Service and robot policies of the relevant third-party providers.
4. **Non-Commercial Use**: This project is strictly prohibited from being used for high-frequency bulk scraping, commercial exploitation, or unauthorized public exhibitions. The maintainers assume no liability for any direct or indirect legal disputes, data loss, or damages resulting from non-compliant usage by third parties.
