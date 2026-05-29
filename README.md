# Facial Recognition Attendance System

A lightweight offline face-attendance system built around Python, FastAPI, React, a native webview shell, SQLite, and Excel export support.

## Scalable V2 API

This repo now also includes a `v2` backend-oriented architecture for larger deployments with `1,000-10,000` workers.

Important note:
- The new architecture is the right structural direction for scale.
- The supported startup path in this repo is now `MediaPipe + classical descriptor + LSH index`.
- That stack is what the app is configured to run by default on this machine.

### V2 Design

- Precompute one or more embeddings per worker at enrollment time
- Store worker metadata in SQL
- Store embeddings separately and build a vector index
- Run recognition as `detect -> embed -> nearest-neighbor search -> threshold -> attendance mark`
- Avoid scanning every worker one-by-one during live recognition

### V2 Files

- API app: `src/api_v2.py`
- Service layer: `src/v2/service.py`
- SQL repository: `src/v2/repository.py`
- Vector index: `src/v2/index.py`
- Embedder abstraction: `src/v2/embedder.py`
- Detection helpers: `src/v2/vision.py`

### V2 Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

`mediapipe` is now included and is used as the preferred face detector for the live web app. If MediaPipe is unavailable in a given environment, the app falls back to OpenCV cascade detection.

Backend selection is environment-driven:

```bash
ATTENDANCE_EMBEDDING_BACKEND=classical
ATTENDANCE_VECTOR_INDEX_BACKEND=lsh
ATTENDANCE_ALLOW_BACKEND_FALLBACK=true
```

Optional alternative backend:

```bash
ATTENDANCE_EMBEDDING_BACKEND=lbph
ATTENDANCE_VECTOR_INDEX_BACKEND=lsh
```

If you want the API to fail fast instead of silently falling back, disable fallback:

```bash
ATTENDANCE_ALLOW_BACKEND_FALLBACK=false
```

Run the API directly:

```bash
python api.py
```

To expose the app on another device in the same network:

```bash
ATTENDANCE_WEB_HOST=0.0.0.0
ATTENDANCE_WEB_PORT=8000
python api.py
```

Open the browser app:

```text
http://127.0.0.1:8000/
```

From another device, open:

```text
http://<device-ip>:8000/
```

Open the docs:

```text
http://127.0.0.1:8000/docs
```

### V2 API Flow

1. Enroll a worker with multiple images
2. The service extracts one embedding per image and stores it
3. The service rebuilds the vector index
4. Recognition searches the index instead of looping through every worker manually

### Web App Features

- Browser-based webcam preview and live recognition loop
- Worker enrollment directly from the root page
- Live camera enrollment capture using the `Capture From Camera` button or the `S` key
- Detection check with face-box feedback in the browser UI
- Employee removal directly from the worker table in the web app
- Real-time status, worker list, and attendance table refresh
- One-screen operational UI for local demos and admin workflows

### Frontend Dev Notes

If you run the React app with `npm run dev`, the Vite dev server now proxies `/api` requests to `http://127.0.0.1:8000` by default.

If your backend is on a different host or port, set one of these before starting Vite:

```bash
VITE_PROXY_TARGET=http://<device-ip>:8000
```

or:

```bash
VITE_API_BASE_URL=http://<device-ip>:8000
```

If you use `VITE_API_BASE_URL` from a different origin, allow that origin in the backend:

```bash
ATTENDANCE_CORS_ALLOW_ORIGINS=http://localhost:5173,http://<device-ip>:5173
```

### Lightweight Offline App

The default app now starts the operator-facing React/FastAPI UI in the normal system browser so it can run on devices like the Radxa without relying on the native Python webview runtime.

Start the browser app:

```bash
python app.py
```

If you still want the embedded native shell and the machine has `pywebview` plus the required system runtime installed, use one of these explicit commands:

```bash
python app.py offline
python app.py native
python app.py desktop
python app.py react
python app.py native-react
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Radxa / Debian packages for the optional native webview shell:

```bash
sudo apt install python3-gi gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0
```

The native shell also needs a graphical desktop/X11 session to open its own window.

If you want the browser-hosted version explicitly, use:

```bash
python app.py web
```

If you want the older Tkinter admin app, use:

```bash
python app.py gui
```

### Example V2 Endpoints

- `GET /health`
- `GET /api/v2/status`
- `GET /api/v2/workers`
- `POST /api/v2/workers/enroll`
- `POST /api/v2/recognitions`
- `POST /api/v2/breath-tests/start`
- `POST /api/v2/breath-tests/complete`
- `DELETE /api/v2/breath-tests/{session_id}`
- `GET /api/v2/attendance`
- `POST /api/v2/index/rebuild`

### Live Breath Sensor Integration

The recognition screen now supports a live SPI/GPIO breath board flow:

- face is identified first
- pressing `Exhale` starts a backend breath-test session
- the UI keeps the same face-verification countdown running while the sensor capture is in progress
- when the countdown finishes, the cannabis reading plus the alcohol result are stored in `attendance_events` and shown in the UI

To enable the real sensor on the Radxa/Linux device:

```bash
pip install python-periphery
```

```bash
ATTENDANCE_BREATH_ANALYZER_MODE=spi
ATTENDANCE_BREATH_SPI_DEVICE=/dev/spidev1.0
ATTENDANCE_BREATH_BOARD_ENABLE_GPIO=257
ATTENDANCE_BREATH_SAMPLE_SECONDS=10
ATTENDANCE_BREATH_SAMPLE_INTERVAL_SECONDS=0.05
ATTENDANCE_BREATH_ADC_BITS=16
ATTENDANCE_BREATH_ADC_VREF=2.5
ATTENDANCE_BREATH_ADC_GAIN=2.0
ATTENDANCE_BREATH_ADC_TO_CANNABIS_SCALE=1.0
ATTENDANCE_BREATH_ADC_TO_CANNABIS_OFFSET=0.0
ATTENDANCE_BREATH_ALCOHOL_SOURCE=mock
ATTENDANCE_BREATH_PLACEHOLDER_ALCOHOL_MIN_PPB=0
ATTENDANCE_BREATH_PLACEHOLDER_ALCOHOL_MAX_PPB=10
```

Optional calibration controls:

- the live cannabis path now follows the board script formula `adc * (vref / (2^bits * gain))`
- `ATTENDANCE_BREATH_ADC_BITS`, `ATTENDANCE_BREATH_ADC_VREF`, and `ATTENDANCE_BREATH_ADC_GAIN` control that conversion
- `ATTENDANCE_BREATH_ADC_TO_CANNABIS_SCALE` and `ATTENDANCE_BREATH_ADC_TO_CANNABIS_OFFSET` can still be used as a final calibration step after the board conversion
- `ATTENDANCE_BREATH_CANNABIS_THRESHOLD_PPB` should be recalibrated to match the new converted cannabis range on your board
- `ATTENDANCE_BREATH_SAMPLE_AGGREGATION` can be `mean`, `peak`, or `last`
- `ATTENDANCE_BREATH_POWER_SETTLE_SECONDS`, `ATTENDANCE_BREATH_PID_SETTLE_SECONDS`, and `ATTENDANCE_BREATH_ADC_SETTLE_SECONDS` match the board timing values from the electronics script
- `ATTENDANCE_BREATH_ADC_BASELINE`, `ATTENDANCE_BREATH_ADC_TO_ALCOHOL_SCALE`, and `ATTENDANCE_BREATH_ADC_TO_ALCOHOL_OFFSET` only apply to the temporary legacy ADC-based alcohol fallback
- if you later wire a real alcohol script, replace the placeholder alcohol path instead of reusing the cannabis conversion

Note:

- the current integration converts the live SPI ADC into the cannabis reading before it enters the app flow
- alcohol is currently a placeholder value so the app and database can still show/store both fields until the real alcohol script is added

### Benchmarking

You can benchmark the current recognizer on a folder dataset without recording hundreds of people yourself.

Expected dataset layout:

```text
dataset_root/
  person_001/
    01.jpg
    02.jpg
    03.jpg
    ...
  person_002/
    01.jpg
    02.jpg
    ...
```

Run an accuracy and latency benchmark:

```bash
python benchmark_v2.py dataset --dataset-root "C:\path\to\dataset" --max-people 100 --enroll-per-person 3
```

This will:

- enroll the first few images per person into an isolated benchmark database
- test the remaining images
- report detection rate, recognition accuracy, false rejects, misidentifications, and latency

Run a pure search-speed benchmark:

```bash
python benchmark_v2.py load --workers 10000 --probes 500
```

This measures how fast the active index can search a large worker count even if you do not have a large real face dataset yet.

If you do not have a large dataset, you can generate a synthetic one from the face images already stored in `data/faces`:

```bash
python -m src.v2.synthetic_dataset --output-root "data/synthetic_dataset" --identities 100 --images-per-identity 8 --clean
```

Then benchmark it:

```bash
python benchmark_v2.py dataset --dataset-root "data/synthetic_dataset" --max-people 100 --enroll-per-person 3
```

Important note:
- this synthetic dataset is useful for pipeline, throughput, and stress testing
- it is not a substitute for real cross-person accuracy testing on 100 truly different people

### Current Supported Stack

- Detector: `MediaPipe` with OpenCV cascade fallback
- Embedder: `classical` handcrafted face descriptor
- Search index: `lsh` approximate nearest-neighbor search
- Storage: `SQLite`

### What Changed In V2

- The embedder is now pluggable through `build_embedder()` in `src/v2/embedder.py`
- The vector index is now pluggable through `build_vector_index()` in `src/v2/index.py`
- The API architecture endpoint now reports requested and active backends
- The API now defaults to the supported local backend instead of an experimental deep-model install
- Recognition now batch-searches faces from a request instead of searching one-by-one
- Recognition now suppresses repeat camera matches for a short TTL to reduce redundant work
- Enrollment can now replace old embeddings for a worker to keep the index cleaner
- The API now exposes `/api/v2/status` for operational visibility
- The current defaults prioritize a stable startup path and immediate search over experimental installs

### Next Upgrade Path

For stronger accuracy later, the cleanest route is:

- keep `MediaPipe` detection
- replace the descriptor in `src/v2/embedder.py` with a supported deep embedding backend
- replace `lsh` in `src/v2/index.py` with `FAISS` or another dedicated vector index
- move from SQLite to PostgreSQL for central multi-camera deployments

### Operational Notes

- `ATTENDANCE_RECOGNITION_CACHE_TTL_SECONDS` controls short-term duplicate suppression per camera
- `ATTENDANCE_MAX_FACES_PER_REQUEST` limits how many faces a single recognition request will process
- `ATTENDANCE_DEFAULT_LIST_LIMIT` controls default list sizes for attendance reads
- `replace_existing=true` on enrollment replaces a worker's previous embeddings for the active backend with a fresh set
- embeddings are stored per backend, so switching from `classical` to `lbph` requires re-enrollment for that backend
- ambiguous matches are rejected if the top score is too close to the second-best score

## Features

- Admin login for dashboard access
- Person enrollment from webcam captures
- Face training from saved face images using OpenCV LBPH
- Live recognition with attendance marking
- SQLite storage for people and attendance records
- Daily CSV logging plus Excel export
- Basic liveness check based on face movement across frames

## Admin Authentication Setup

Plaintext admin passwords are no longer supported.

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Generate a bcrypt password hash:

```bash
python scripts/generate_admin_password_hash.py
```

3. Set environment variables before starting the app:

```bash
ADMIN_USERNAME=your_user
ADMIN_PASSWORD_HASH=your_bcrypt_hash
```

Supported fallback environment variable names for compatibility:

```bash
ATTENDANCE_ADMIN_USERNAME=your_user
ATTENDANCE_ADMIN_PASSWORD_HASH=your_bcrypt_hash
```

Notes:

- The password is stored only as a bcrypt hash.
- The backend compares usernames with `hmac.compare_digest`.
- The app now fails fast on startup if admin auth is not configured.
- Operator sessions default to 5 minutes. Override with `ATTENDANCE_SESSION_TTL_SECONDS` if needed.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

## Run

Launch the local React app in the system browser:

```bash
python app.py
```

Build the frontend once on your main PC:

```bash
cd frontend
npm install
npm run build
```

After that, copy the repo with the built `frontend/dist` folder to the Radxa and start the same UI locally:

```bash
python app.py
```

If you want Chromium or Edge to open it in app-style kiosk mode without browser tabs, use:

```bash
python app.py kiosk
```

If you still want the embedded native shell on a machine where `pywebview` works, use:

```bash
python app.py desktop
```

Launch the legacy desktop Tkinter app:

```bash
python app.py gui
```

Notes:
- This path keeps the same React UI but removes the Rust/Tauri dependency chain from the device.
- On Linux and Radxa, installing a Chromium-based browser is enough for the default `python app.py` flow.
- Use `python app.py kiosk` if you want the browser to open in app mode with no browser tabs.
- The local UI starts the FastAPI backend on `0.0.0.0:8000` by default, so it can also be opened from another device on the same network.
- If the device does not have npm, the launcher will still run as long as `frontend/dist` is already present.
- Set `ATTENDANCE_SKIP_FRONTEND_BUILD=true` if you always want to skip auto-build checks on the device.
- Set `ATTENDANCE_DATA_DIR` if you want the app to store attendance data somewhere other than the default app or project data folder.
- `python app.py web` is still available if you want the explicit browser command.

## CLI Commands

Enroll a person:

```bash
python app.py enroll --name "Alice" --images 10
```

Train encodings:

```bash
python app.py train
```

Start recognition:

```bash
python app.py recognize
```

Export today's attendance:

```bash
python app.py export --today
```

Export a date range:

```bash
python app.py export --start-date 2026-03-01 --end-date 2026-03-31
```

## Controls

- Enrollment window:
  - `s` saves a frame
  - `q` closes the window
- Recognition window:
  - `q` closes the window

## Data

- Face images: `data/faces`
- CSV attendance logs: `data/attendance`
- Excel exports: `data/exports`
- SQLite database: `data/attendance.db`
- Face encodings: `data/encodings.pkl`

## Anti-Spoofing Note

The app includes a basic liveness gate that waits for face movement across several frames before marking attendance. This helps reduce simple photo-based spoofing, but it is not a production-grade anti-spoofing system.

## Notes

- A webcam is required for enrollment and recognition.
- `face_recognition` may require extra native build tools on some systems.
- Better lighting and multiple enrollment images improve recognition quality.
- This version uses OpenCV's built-in recognizer instead of `face_recognition/dlib`, which makes installation much easier on Windows.
