"""
narcolepsy_tracker.py
=====================
Narcolepsy-aware smart alarm system using Garmin Vivosmart 5 data.

ARCHITECTURE — CLOUD SERVER
-----------------------------
This runs on a cloud server (EC2/Lightsail) with a VPN tunnel or port
forwarding back to your home LAN where the Hue bridge and Kasa plug live.

  [EC2/Lightsail]
    ├── Pulls Garmin data outbound (no special networking needed)
    ├── Exposes a small HTTP endpoint your phone POSTs location to
    ├── Stores SQLite DB + raw JSON (back up to S3 periodically)
    └── Sends alarm commands → VPN/tunnel → home LAN → Hue + Kasa

  [Your home LAN]
    ├── Philips Hue bridge  (192.168.x.x, only reachable via VPN)
    └── Kasa smart plug     (192.168.x.x, only reachable via VPN)

  [Your phone]
    └── POSTs GPS coordinates to your server's public endpoint
        so the server knows whether you're home before firing the alarm

NETWORKING OPTIONS (pick one):
  A. WireGuard site-to-site  — cleanest, your server gets a private IP
     on your home subnet, calls to 192.168.x.x just work
  B. Port forwarding         — expose specific ports (Hue :80, Kasa :9999)
     on your router with a static or DDNS public IP
  C. Reverse SSH tunnel      — simpler to set up than WireGuard if you
     already manage SSH, but more fragile long-term

  The VPN_MODE env var below lets you switch behaviour without code changes.

SCHEDULING ON THE CLOUD SERVER:
  Unlike a local machine, don't rely on your laptop's cron. The server
  runs 24/7 so use its own cron + systemd.

  Nightly collection (server cron):
    SSH into your instance and run: crontab -e
    Add:
      0 12 * * * /home/ubuntu/venv/bin/python /home/ubuntu/tracker/narcolepsy_tracker.py --collect

    12:00 UTC = 7:00am Indiana time (UTC-5). Garmin needs time to process
    overnight data before you pull it — don't run this at midnight UTC.
    Adjust if you're in a different timezone:
      EST (UTC-5): 12:00 UTC
      CST (UTC-6): 13:00 UTC
    To set server timezone: sudo timedatectl set-timezone America/Indiana/Indianapolis

  Geofence listener (systemd — needs to stay running 24/7):
    Create /etc/systemd/system/narcolepsy-geo.service:

      [Unit]
      Description=Narcolepsy Geofence Listener
      After=network.target

      [Service]
      User=ubuntu
      WorkingDirectory=/home/ubuntu/tracker
      EnvironmentFile=/home/ubuntu/tracker/.env
      ExecStart=/home/ubuntu/venv/bin/python narcolepsy_tracker.py --listen
      Restart=always
      RestartSec=10

      [Install]
      WantedBy=multi-user.target

    Then:
      sudo systemctl daemon-reload
      sudo systemctl enable narcolepsy-geo
      sudo systemctl start narcolepsy-geo
      sudo journalctl -u narcolepsy-geo -f   ← live logs

  Checking logs:
    Collection runs log to tracker.log in the working directory.
    Geofence listener logs to journalctl via systemd.
    To tail both: tail -f tracker.log  |  journalctl -u narcolepsy-geo -f

INTERNET DEPENDENCY WARNING:
  Unlike a Pi on your LAN, if your home internet goes down the server
  cannot reach Hue/Kasa. check_vpn_connectivity() gates all alarm calls
  so failures are logged rather than silent. Set a backup phone alarm
  at the end of your wake window as a safety net.

DB BACKUP TO S3:
  Add to server crontab:
    30 12 * * * aws s3 sync /home/ubuntu/tracker/raw/ s3://your-bucket/raw/
    35 12 * * * aws s3 cp /home/ubuntu/tracker/sleep_data.db s3://your-bucket/sleep_data_$(date +\\%F).db
  Give your EC2 instance an IAM role with s3:PutObject on that bucket.
  No boto3 needed — this uses the aws cli directly.

CREDENTIALS ON THE SERVER:
  Do NOT commit a .env file to git. On the server, either:
    A. Use an .env file with restricted permissions (chmod 600 .env)
       and make sure .gitignore includes .env
    B. Use EC2 instance environment variables (set in User Data or SSM)
    C. Use AWS Secrets Manager and pull values at startup

DEPENDENCIES:
    pip install garminconnect python-dotenv flask requests python-kasa
"""

import os
import json
import math
import sqlite3
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from garminconnect import Garmin

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

load_dotenv()   # reads .env if present — fine for dev, see notes above for prod

GARMIN_EMAIL    = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")

# Home coordinates for geofence
HOME_LAT      = float(os.getenv("HOME_LAT", 0))
HOME_LON      = float(os.getenv("HOME_LON", 0))
HOME_RADIUS_M = float(os.getenv("HOME_RADIUS_M", 150))

# Secret token your phone includes in geofence POSTs — simple auth
# Generate: python3 -c "import secrets; print(secrets.token_hex(32))"
GEO_SECRET = os.getenv("GEO_SECRET", "change_this_before_deploying")

# How the server reaches your home LAN devices
# "wireguard" — HUE_BRIDGE_IP / KASA_PLUG_IP are private LAN addresses
#               (192.168.x.x) reachable via WireGuard
# "portforward" — HUE_BRIDGE_IP is your public IP, ports are forwarded
VPN_MODE        = os.getenv("VPN_MODE", "wireguard")
HUE_BRIDGE_IP   = os.getenv("HUE_BRIDGE_IP")
HUE_BRIDGE_PORT = int(os.getenv("HUE_BRIDGE_PORT", 80))
HUE_USERNAME    = os.getenv("HUE_USERNAME")   # from Hue bridge developer setup
HUE_LIGHT_ID    = os.getenv("HUE_LIGHT_ID", "1")
KASA_PLUG_IP    = os.getenv("KASA_PLUG_IP")

# Port the geofence Flask app listens on
# Open this in your EC2 security group inbound rules
# Restrict to 0.0.0.0/0 or, better, your phone carrier's IP range
GEO_LISTEN_PORT = int(os.getenv("GEO_LISTEN_PORT", 8765))

DB_PATH  = Path(os.getenv("DB_PATH", "sleep_data.db"))
RAW_DIR  = Path(os.getenv("RAW_DIR", "raw"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("tracker.log"),  # persists on server filesystem
    ]
)
log = logging.getLogger(__name__)

# In-memory location cache updated by geofence listener POSTs.
# Single-process only — if you ever split listener and collector into
# separate processes, move this to a DB column or Redis key.
_last_known_location: dict = {
    "lat": None,
    "lon": None,
    "timestamp": None,
    "at_home": False,
}


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    """
    Create all tables if they don't already exist.

    Tables:
      sleep_sessions — one row per detected sleep session (day or night)
      hrv_raw        — 5-min window HRV/HR values tied to a session
      journal        — morning annotations used as ground truth labels
      location_log   — timestamped log of phone location POSTs
                       useful for debugging geofence issues and correlating
                       sleep quality with location over time
    """
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS sleep_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            start_time      TEXT,
            end_time        TEXT,
            duration_s      INTEGER,
            garmin_stage    TEXT,           -- Garmin's label (often wrong for you)
            avg_hrv         REAL,
            avg_hr          REAL,
            min_hr          REAL,
            onset_to_rem_s  INTEGER,        -- seconds from sleep onset to first REM
            is_sorem        INTEGER DEFAULT 0,      -- 1 if onset_to_rem_s < 1800
            confirmed_sorem INTEGER DEFAULT NULL,   -- NULL=unreviewed, 1=yes, 0=no
            raw_json_path   TEXT,
            notes           TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS hrv_raw (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL,
            timestamp   TEXT NOT NULL,
            hrv_rmssd   REAL,
            hr_bpm      REAL,
            stress      REAL,
            FOREIGN KEY (session_id) REFERENCES sleep_sessions(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS journal (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            sleep_quality   INTEGER,        -- subjective 1-10
            accidental_naps INTEGER,        -- count of unintended sleep episodes yesterday
            energy_level    INTEGER,        -- 1-10 how you feel this morning
            sorem_suspected INTEGER DEFAULT 0,
            notes           TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS location_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            lat       REAL,
            lon       REAL,
            at_home   INTEGER,   -- 1 if within HOME_RADIUS_M
            source    TEXT       -- 'phone_post' or 'manual'
        )
    """)

    conn.commit()
    log.info("Database initialized at %s", DB_PATH)


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

def get_client() -> Garmin:
    """
    Authenticate with Garmin Connect and return a logged-in client.

    Tokens are cached at ~/.garminconnect on the server filesystem.
    They're valid for ~1 year. Lock down permissions after first login:
      chmod 700 ~/.garminconnect && chmod 600 ~/.garminconnect/*

    If your EC2 instance gets terminated and recreated, you lose the token
    directory. To avoid re-authenticating after rebuilds:
      Option A — use an EBS volume mounted at a persistent path
      Option B — after first login, copy tokens to S3:
                   aws s3 cp ~/.garminconnect/ s3://your-bucket/garmin-tokens/ --recursive
                 Add a startup script (User Data) that restores them:
                   aws s3 cp s3://your-bucket/garmin-tokens/ ~/.garminconnect/ --recursive
                   chmod 700 ~/.garminconnect && chmod 600 ~/.garminconnect/*

    TODO: if you get a 401 or TokenExpiredError, SSH into the server,
          rm -rf ~/.garminconnect, and run --setup again.
    """
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    log.info("Authenticated as %s", GARMIN_EMAIL)
    return client


# ---------------------------------------------------------------------------
# DATA COLLECTION
# ---------------------------------------------------------------------------

def fetch_sleep(client: Garmin, target_date: str) -> dict:
    """
    Pull sleep data for a given date from Garmin Connect.

    Key fields in the response:
      dailySleepDTO.sleepStartTimestampGMT — Unix ms, sleep onset
      dailySleepDTO.sleepEndTimestampGMT   — Unix ms, wake time
      dailySleepDTO.averageSpO2Value       — blood oxygen
      dailySleepDTO.restlessCount          — restless event count
      sleepLevels[]                        — list of {startGMT, endGMT, activityLevel}
                                             activityLevel: 0=deep 1=light 2=rem 3=awake

    TODO: after first --collect run, SSH in and inspect the raw JSON:
          cat raw/YYYY-MM-DD_sleep.json | python3 -m json.tool | less
          Field names shift between Vivosmart firmware versions.
    """
    return client.get_sleep_data(target_date)


def fetch_hrv(client: Garmin, target_date: str) -> dict:
    """
    Pull HRV summary data for a given date.

    Key fields:
      hrvSummary.lastNight  — Garmin's overall HRV score for the night
      hrvSummary.weeklyAvg  — 7-day rolling average (your personal baseline)
      hrvReadings[]         — list of {hrv5MinHigh, hrv5MinLow, startTimestampGMT}
                              5-minute window averages — primary classifier input

    TODO: map hrvReadings timestamps against sleepLevels from fetch_sleep()
          to get HRV-per-stage features. That's the core of the classifier.
    """
    return client.get_hrv_data(target_date)


def fetch_heart_rates(client: Garmin, target_date: str) -> dict:
    """
    Pull all-day heart rate data including overnight.

    Returns heartRateValues: list of [unixTimestampMs, bpmOrNull].
    Null = watch lost skin contact. Log these as data quality gaps.

    TODO: align timestamps with sleep stage windows. A sudden HR spike
          during a sleep window is a potential arousal/SOREM marker.
    """
    return client.get_heart_rates(target_date)


def fetch_stress(client: Garmin, target_date: str) -> dict:
    """
    Pull Garmin stress score data.

    Stress is derived from HRV — low HRV = high stress score.
    During REM, stress often spikes because REM suppresses HRV.
    Useful as an indirect REM indicator alongside the stage labels.

    TODO: compare stress spikes against Garmin's sleep stages.
          Spike during reported light sleep = SOREM candidate to flag.
    """
    return client.get_stress_data(target_date)


def save_raw(data: dict, data_type: str, target_date: str) -> Path:
    """
    Save raw API response to disk as JSON before any parsing.

    Never skip this. Your understanding of the data will evolve and
    you'll want to re-parse historical sessions as the classifier improves.

    On the server, raw/ lives on the instance filesystem.
    Back it up to S3 — see cron note at top of file.
    Restore with: aws s3 sync s3://your-bucket/raw/ /home/ubuntu/tracker/raw/
    """
    RAW_DIR.mkdir(exist_ok=True)
    path = RAW_DIR / f"{target_date}_{data_type}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    log.info("Saved raw %s → %s", data_type, path)
    return path


# ---------------------------------------------------------------------------
# PARSING + STORAGE
# ---------------------------------------------------------------------------

def parse_sleep_sessions(sleep_raw: dict, hrv_raw: dict) -> list[dict]:
    """
    Parse raw Garmin sleep + HRV data into structured session dicts.

    TODO: implement after inspecting your actual raw JSON files.
          SSH in: cat raw/YYYY-MM-DD_sleep.json | python3 -m json.tool

    Key logic to build here:
      1. Extract sleepLevels, convert activityLevel ints to stage labels
      2. Find timestamp of first REM entry in sleepLevels
      3. onset_to_rem_s = first_rem_ts - sleep_start_ts
      4. is_sorem = 1 if onset_to_rem_s < 1800 (30 minutes)
      5. avg_hrv from hrv_raw['hrvReadings'] entries
      6. Align HRV reading timestamps with sleep stage windows
    """
    raise NotImplementedError("parse_sleep_sessions() not yet implemented")


def store_session(conn: sqlite3.Connection, session: dict) -> int:
    """
    Insert a parsed session into sleep_sessions. Returns new row id.

    TODO: parameterized INSERT — never use f-strings for SQL.
    """
    raise NotImplementedError("store_session() not yet implemented")


def store_hrv_readings(conn: sqlite3.Connection, session_id: int, readings: list) -> None:
    """
    Bulk insert HRV readings for a session.

    TODO: use executemany() — not a loop of individual INSERTs.
    """
    raise NotImplementedError("store_hrv_readings() not yet implemented")


# ---------------------------------------------------------------------------
# GEOFENCE
# ---------------------------------------------------------------------------

def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Return distance in meters between two lat/lon points (Haversine formula).
    Accurate enough for a 150m radius geofence.
    """
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi   = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def is_at_home(lat: float, lon: float) -> bool:
    """Return True if coordinates are within HOME_RADIUS_M of home."""
    if not HOME_LAT or not HOME_LON:
        log.warning("HOME_LAT/HOME_LON not set — geofence always returning False")
        return False
    return haversine_distance_m(lat, lon, HOME_LAT, HOME_LON) <= HOME_RADIUS_M


def update_location(lat: float, lon: float, conn: sqlite3.Connection) -> bool:
    """
    Update in-memory location cache and log to DB.

    Called by the geofence listener each time your phone POSTs.

    TODO: implement the INSERT into location_log.
    """
    at_home = is_at_home(lat, lon)
    _last_known_location.update({
        "lat": lat,
        "lon": lon,
        "timestamp": datetime.utcnow().isoformat(),
        "at_home": at_home,
    })
    log.info("Location updated: %.4f, %.4f — at_home=%s", lat, lon, at_home)

    # TODO: INSERT into location_log
    # c = conn.cursor()
    # c.execute("INSERT INTO location_log (timestamp, lat, lon, at_home, source) VALUES (?,?,?,?,?)",
    #           (datetime.utcnow().isoformat(), lat, lon, int(at_home), "phone_post"))
    # conn.commit()

    return at_home


# ---------------------------------------------------------------------------
# GEOFENCE LISTENER (Flask — runs as a systemd service)
# ---------------------------------------------------------------------------

def start_geofence_listener() -> None:
    """
    Small Flask HTTP server that receives location POSTs from your phone.

    This is the bridge between your phone GPS and the server.
    It runs 24/7 as a systemd service (see deployment notes at top of file).

    Endpoint:  POST /location
    Headers:   X-Secret: <your GEO_SECRET value>
    Body JSON: {"lat": 41.0000, "lon": -85.0000}

    Phone side options:
      Android — Tasker:
        Profile: Location → set home coordinates as entry/exit zone
        Task: HTTP POST to http://your-server-ip:8765/location
              Header: X-Secret = your GEO_SECRET
              Body: {"lat": %loc_lat, "lon": %loc_long}
        Tasker is $3.49 one-time. Most reliable option.

      Android — Termux + cron:
        Install Termux and Termux:API apps.
        Use termux-location in a cron job every 5 minutes to POST coordinates.
        Battery drain is low since location uses cell/WiFi, not true GPS.

      iOS — Shortcuts + Automation:
        Create a Shortcut automation triggered on Arrive/Leave for home address.
        Action: "Get Contents of URL" (POST to your endpoint).
        Less reliable than Tasker but works without extra apps.

    Security note:
      GEO_SECRET in the header prevents strangers from spoofing your location.
      For extra hardening: in your EC2 security group, restrict the inbound
      rule for GEO_LISTEN_PORT to your phone carrier's IP CIDR range.
      T-Mobile, AT&T, Verizon all publish their CIDR ranges.

    TODO: implement the Flask routes below. The stubs are wired up but
          update_location() needs its DB INSERT implemented first.
    """
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        log.error("Flask not installed — run: pip install flask")
        return

    app  = Flask(__name__)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(conn)

    @app.route("/location", methods=["POST"])
    def receive_location():
        if request.headers.get("X-Secret") != GEO_SECRET:
            log.warning("Rejected location POST — bad secret from %s", request.remote_addr)
            return jsonify({"error": "unauthorized"}), 401

        data = request.get_json(silent=True)
        if not data or "lat" not in data or "lon" not in data:
            return jsonify({"error": "missing lat/lon"}), 400

        at_home = update_location(float(data["lat"]), float(data["lon"]), conn)
        return jsonify({"at_home": at_home}), 200

    @app.route("/status", methods=["GET"])
    def status():
        """
        Health check — hit http://your-server:8765/status to confirm listener is up.
        Safe to leave public since it reveals nothing sensitive.
        Useful to ping from a phone shortcut to verify connectivity before sleep.
        """
        return jsonify({
            "at_home":     _last_known_location["at_home"],
            "last_update": _last_known_location["timestamp"],
            "server_time": datetime.utcnow().isoformat(),
        }), 200

    log.info("Geofence listener starting on 0.0.0.0:%d", GEO_LISTEN_PORT)

    # TODO: for production, put nginx in front of this rather than exposing
    #       Flask directly. nginx gives you TLS termination (HTTPS) so the
    #       GEO_SECRET isn't transmitted in plaintext.
    #       A self-signed cert is fine — your phone just needs to trust it.
    #       Let's Encrypt via certbot is free if you have a domain name.
    app.run(host="0.0.0.0", port=GEO_LISTEN_PORT)


# ---------------------------------------------------------------------------
# ALARM — reaches home devices through VPN tunnel or port forward
# ---------------------------------------------------------------------------

def check_vpn_connectivity() -> bool:
    """
    Verify the path to home LAN devices is alive before triggering hardware.

    If this returns False, log a warning and skip the alarm sequence.
    Without this gate, a dropped VPN at 3am means a silent alarm failure.

    TODO: implement a short-timeout TCP connect to HUE_BRIDGE_IP:HUE_BRIDGE_PORT.
          Use socket.connect_ex() — no subprocess, no elevated permissions needed.
          Timeout of 2 seconds is enough; if WireGuard is up it'll respond fast.

    Example:
      import socket
      try:
          s = socket.socket()
          s.settimeout(2)
          result = s.connect_ex((HUE_BRIDGE_IP, HUE_BRIDGE_PORT))
          s.close()
          return result == 0
      except Exception:
          return False
    """
    raise NotImplementedError("check_vpn_connectivity() not yet implemented")


def trigger_hue_sunrise(duration_minutes: int = 5) -> None:
    """
    Start a gradual sunrise on Philips Hue via the local bridge API.

    Request path: server → WireGuard tunnel → home LAN → Hue bridge

    Hue local API:
      PUT http://<bridge-ip>/api/<username>/lights/<id>/state
      Body: {"on": true, "bri": 254, "ct": 153, "transitiontime": 3000}
      transitiontime is in 100ms units — 5 min = 3000

    One-time setup (do this from inside your home LAN, not the server):
      1. Find bridge IP: http://discovery.meethue.com
      2. Press physical button on bridge, then:
           POST http://<bridge-ip>/api
           Body: {"devicetype": "narcolepsy_tracker"}
         Response contains your HUE_USERNAME — save it to .env
      3. Find light ID:
           GET http://<bridge-ip>/api/<username>/lights
         Returns all lights with their IDs

    TODO: implement PUT request using the requests library.
          Always call check_vpn_connectivity() first and return early if False.
    """
    raise NotImplementedError("trigger_hue_sunrise() not yet implemented")


def trigger_alarm_plug(on: bool = True) -> None:
    """
    Toggle the TP-Link Kasa smart plug.

    Request path: server → WireGuard tunnel → home LAN → Kasa plug

    python-kasa is async:
      import asyncio
      from kasa import SmartPlug
      asyncio.run(SmartPlug(KASA_PLUG_IP).turn_on())

    Port forward note: Kasa uses TCP 9999. If using port forwarding rather
    than WireGuard, forward external port 9999 → 192.168.x.x:9999 on router.
    WireGuard is cleaner — port forwarding exposes the plug to the internet.

    TODO:
      1. Set a static DHCP lease for the plug in your router
      2. Implement with python-kasa
      3. Add auto-off after 5 minutes — so alarm doesn't blare if you sleep through it
      4. Always call check_vpn_connectivity() first
    """
    raise NotImplementedError("trigger_alarm_plug() not yet implemented")


def trigger_wake_sequence(stage: str) -> None:
    """
    Orchestrate the full wake sequence: light ramp → audible alarm.

    Reads at_home from the in-memory location cache set by the geofence listener.
    Only fires if at_home=True AND stage is N1 or N2.

    SHADOW MODE:
      Set SHADOW_MODE=true in your .env while validating.
      Logs what WOULD have fired without touching any hardware.
      Run in shadow mode for 1-2 weeks and check against your journal
      before flipping to live. You do not want a miscalibrated classifier
      randomly blaring an alarm at 3am.
    """
    shadow  = os.getenv("SHADOW_MODE", "false").lower() == "true"
    at_home = _last_known_location.get("at_home", False)

    if not at_home:
        log.info("Not at home — alarm suppressed")
        return

    if stage not in ("N1", "N2"):
        log.info("Stage is %s — alarm suppressed to protect sleep", stage)
        return

    if shadow:
        log.info("[SHADOW] Would have triggered wake sequence from stage %s", stage)
        return

    # TODO: uncomment once all stubs below are implemented
    # if not check_vpn_connectivity():
    #     log.error("VPN/tunnel unreachable — alarm cannot fire. Check WireGuard status.")
    #     return
    # trigger_hue_sunrise(duration_minutes=5)
    # time.sleep(300)
    # trigger_alarm_plug(on=True)

    log.info("Triggering wake sequence from stage %s", stage)


# ---------------------------------------------------------------------------
# CLASSIFIER (stub — Phase 2, after 2+ weeks of data)
# ---------------------------------------------------------------------------

def compute_hrv_features(hrv_readings: list[dict]) -> dict:
    """
    Compute HRV features from 5-minute window readings.

    Returns: mean_rmssd, min_rmssd, max_rmssd, rmssd_slope, cv_rmssd

    TODO: implement with numpy after you have real data to look at.
          np.mean(), np.polyfit() for slope, np.std()/np.mean() for CV.
    """
    raise NotImplementedError("compute_hrv_features() not yet implemented")


def classify_stage(hrv_features: dict, onset_elapsed_s: int) -> str:
    """
    Classify a window as N1, N2, N3, REM, SOREM, or AWAKE.

    Starting thresholds (tune from your own data):
      SOREM  — REM-like HRV AND onset_elapsed_s < 1800
      REM    — cv_rmssd high, mean_rmssd relatively low, HR variable
      N3     — mean_rmssd highest, HR lowest, very stable
      N2     — moderate rmssd and HR
      N1     — rmssd declining, HR just starting to slow
      AWAKE  — high HR, rmssd erratic

    TODO: start with if/elif thresholds. After 4+ weeks of annotated data
          revisit with a small decision tree trained on your journal labels.
    """
    raise NotImplementedError("classify_stage() not yet implemented")


# ---------------------------------------------------------------------------
# JOURNAL CLI
# ---------------------------------------------------------------------------

def morning_journal(conn: sqlite3.Connection) -> None:
    """
    CLI to record morning observations. SSH into the server and run this,
    or expose a /journal POST endpoint on the Flask app so you can submit
    from your phone without opening a terminal.

    Fields: sleep_quality, accidental_naps, energy_level, sorem_suspected, notes.

    TODO: implement input() prompts and INSERT into journal table.
          After inserting, query last night's session and ask whether the
          classifier's is_sorem flag matched your experience.
          Store the answer in confirmed_sorem — this is your feedback loop.
    """
    raise NotImplementedError("morning_journal() not yet implemented")


# ---------------------------------------------------------------------------
# MAIN COLLECTION LOOP
# ---------------------------------------------------------------------------

def collect(days_back: int = 1) -> None:
    """
    Nightly collection routine — triggered by server cron (see top of file).

    On first deploy, run: python narcolepsy_tracker.py --collect --backfill 30
    to pull the last 30 days of history. After that, the daily cron handles it.

    Timezone reminder: the server likely runs UTC. Garmin dates are in your
    local timezone. The cron schedule at the top of this file accounts for this.
    If you change server or move timezone, update the cron accordingly.
    """
    client = get_client()
    conn   = sqlite3.connect(DB_PATH)
    init_db(conn)

    for days_ago in range(1, days_back + 1):
        target = (date.today() - timedelta(days=days_ago)).isoformat()
        log.info("Collecting data for %s", target)

        try:
            sleep  = fetch_sleep(client, target)
            hrv    = fetch_hrv(client, target)
            hr     = fetch_heart_rates(client, target)
            stress = fetch_stress(client, target)

            save_raw(sleep,  "sleep",  target)
            save_raw(hrv,    "hrv",    target)
            save_raw(hr,     "hr",     target)
            save_raw(stress, "stress", target)

            # TODO: uncomment once parse/store functions are implemented
            # sessions = parse_sleep_sessions(sleep, hrv)
            # for session in sessions:
            #     sid = store_session(conn, session)
            #     store_hrv_readings(conn, sid, session.get("hrv_readings", []))

            log.info("Done for %s — raw JSON saved, DB storage pending", target)

        except Exception as e:
            log.error("Failed to collect %s: %s", target, e)
            continue

    conn.close()


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Narcolepsy Sleep Tracker")
    parser.add_argument("--setup",    action="store_true", help="Init DB and test Garmin auth")
    parser.add_argument("--collect",  action="store_true", help="Collect yesterday's data (run via cron)")
    parser.add_argument("--backfill", type=int, default=1, help="Collect N days back (use 30 on first run)")
    parser.add_argument("--journal",  action="store_true", help="Run morning journal CLI (SSH in to use)")
    parser.add_argument("--listen",   action="store_true", help="Start geofence listener (run via systemd)")
    args = parser.parse_args()

    if args.setup:
        conn = sqlite3.connect(DB_PATH)
        init_db(conn)
        conn.close()
        client = get_client()
        log.info("Setup complete — auth OK, DB ready at %s", DB_PATH)

    elif args.collect:
        collect(days_back=args.backfill)

    elif args.journal:
        conn = sqlite3.connect(DB_PATH)
        morning_journal(conn)
        conn.close()

    elif args.listen:
        # Blocks indefinitely — systemd keeps this alive and restarts on crash
        start_geofence_listener()

    else:
        parser.print_help()
