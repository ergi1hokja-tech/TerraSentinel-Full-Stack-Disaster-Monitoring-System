import eventlet
eventlet.monkey_patch()

import os
import sys
import requests
from datetime import datetime, timedelta, date, timezone
from flask import Flask, render_template, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from dotenv import load_dotenv, find_dotenv
import openai
from flask_apscheduler import APScheduler
from flask_mail import Mail, Message
from sqlalchemy import and_
import pycountry
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import secrets

def generate_reset_token():
    return secrets.token_urlsafe(32)

# .env
dotenv_path = find_dotenv()
print("📄 .env found at:", repr(dotenv_path), file=sys.stderr)
load_dotenv(dotenv_path, override=True)
api_key = os.getenv("OPENAI_API_KEY")
print("🔑 openai.api_key repr:", repr(api_key), file=sys.stderr)
print("🔑 openai.api_key length:", len(api_key or ""), file=sys.stderr)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")


# 1. Database
basedir = app.root_path
app.config['SQLALCHEMY_DATABASE_URI']        = 'sqlite:///' + os.path.join(basedir, 'data.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# 2. SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# 3. OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

# 4. APScheduler
class SchedulerConfig:
    SCHEDULER_API_ENABLED = True
app.config.from_object(SchedulerConfig())
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# 5. Mail
app.config.update(
    MAIL_SERVER         = os.getenv('MAIL_SERVER'),
    MAIL_PORT           = int(os.getenv('MAIL_PORT', 587)),
    MAIL_USE_TLS        = True,
    MAIL_USERNAME       = os.getenv('MAIL_USER'),
    MAIL_PASSWORD       = os.getenv('MAIL_PASS'),
    MAIL_DEFAULT_SENDER = os.getenv('MAIL_USER'),
)
mail = Mail(app)

# 6. Models
class DisasterEvent(db.Model):
    __tablename__ = 'disaster_events'
    id          = db.Column(db.Integer, primary_key=True)
    type        = db.Column(db.String(50),  nullable=False)
    location    = db.Column(db.String(100), nullable=False)
    date        = db.Column(db.Date,         nullable=False)
    severity    = db.Column(db.String(20),   nullable=False)
    latitude    = db.Column(db.Float,        nullable=False)
    longitude   = db.Column(db.Float,        nullable=False)
    description = db.Column(db.Text)

    def to_dict(self):
        return {
            'id':          self.id,
            'type':        self.type,
            'location':    self.location,
            'date':        self.date.isoformat(),
            'severity':    self.severity,
            'coords':      [self.latitude, self.longitude],
            'description': self.description or ""
        }

class RiskForecast(db.Model):
    __tablename__  = 'risk_forecasts'

    id       = db.Column(db.Integer, primary_key=True)
    user_id  = db.Column(db.Integer, nullable=False)   # ⭐ NEW
    region   = db.Column(db.String(100), nullable=False)
    timeframe= db.Column(db.String(50),  nullable=False)
    prediction = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id':         self.id,
            'region':     self.region,
            'timeframe':  self.timeframe,
            'prediction': self.prediction,
            'created_at': self.created_at.isoformat()
        }

with app.app_context():
    db.create_all()

class DigestMessage(db.Model):
    __tablename__ = 'digest_messages'
    id          = db.Column(db.Integer, primary_key=True)
    content     = db.Column(db.Text, nullable=False)
    sent_at     = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
class User(db.Model):
    __tablename__ = "users"
    id       = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email    = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    active   = db.Column(db.Boolean, default=False)

  # ✅ Add this new line
    reset_token = db.Column(db.String(200), nullable=True)
with app.app_context():
    db.create_all()

# 8. Ingestion helpers
def fetch_latest_earthquakes():
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
    resp = requests.get(url, timeout=10); resp.raise_for_status()
    for feat in resp.json().get("features", []):
        props  = feat["properties"]
        coords = feat["geometry"]["coordinates"]
        quake_date = datetime.fromtimestamp(props["time"]/1000, timezone.utc).date()
        loc        = props.get("place","Unknown")
        mag        = props.get("mag") or 0.0
        if   mag >= 7.0: sev = "Extreme"
        elif mag >= 5.0: sev = "High"
        elif mag >= 3.0: sev = "Medium"
        else:            sev = "Low"
        exists = DisasterEvent.query.filter(and_(
            DisasterEvent.type     == "earthquake",
            DisasterEvent.location == loc,
            DisasterEvent.date     == quake_date
        )).first()
        if exists: continue
        ev = DisasterEvent(
            type        = "earthquake",
            location    = loc,
            date        = quake_date,
            severity    = sev,
            latitude    = coords[1],
            longitude   = coords[0],
            description = f"USGS mag {mag}"
        )
        db.session.add(ev)
    db.session.commit()

import requests
from datetime import datetime, timedelta
from geopy.geocoders import Nominatim
import time

geolocator = Nominatim(user_agent="terrasentinel")
geo_cache = {}

def get_country_coords(country_name):
    """Automatically get latitude/longitude for a given country or region."""
    if not country_name:
        return 0.0, 0.0
    if country_name in geo_cache:
        return geo_cache[country_name]

    try:
        location = geolocator.geocode(country_name, timeout=10)
        if location:
            coords = (location.latitude, location.longitude)
            geo_cache[country_name] = coords
            return coords
    except Exception as e:
        print(f"⚠️ Geocoding failed for {country_name}: {e}")
    return 0.0, 0.0

import feedparser
from datetime import datetime, timedelta, timezone

from dateutil import parser
def test_earthquake_fetch_no_crash():
    """Ensure API fetch does not raise an exception."""
    try:
        fetch_latest_earthquakes()
        assert True
    except Exception:
        assert False
def fetch_floods():
    """Fetch the latest 250 flood events from ReliefWeb (no date filter)."""
    print(f"🌊 Fetching ReliefWeb flood data at {datetime.now(timezone.utc)}...")

    url = "https://api.reliefweb.int/v2/disasters?appname=ergi-terrasentinel-Vdf8Uj12"

    payload = {
        "limit": 250,
        "filter": {
            "conditions": [
                {"field": "type", "value": ["Flood"]}
            ]
        },
        "sort": ["date.created:desc"],
        "profile": "full"
    }

    total_new = 0

    try:
        resp = requests.post(url, json=payload, timeout=20)
        print("Status:", resp.status_code)

        if resp.status_code != 200:
            print("⚠ ReliefWeb error:", resp.status_code)
            print(resp.text)
            return

        records = resp.json().get("data", [])

        print(f"Retrieved {len(records)} flood events")

        for rec in records:
            fields = rec.get("fields", {})

            title = fields.get("name", "Flood Event")

            # date handling
            date_info = fields.get("date", {})
            original = date_info.get("original")

            try:
                event_date = datetime.fromisoformat(
                    original.replace("Z", "+00:00")
                ).date() if original else datetime.now().date()
            except:
                event_date = datetime.now().date()

            # unique ID
            unique_id = f"rwflood_{rec.get('id')}"

            # skip duplicates
            if DisasterEvent.query.filter_by(description=unique_id).first():
                continue

            # coords
            lat = lon = 0.0
            country = fields.get("primary_country", {})
            loc = country.get("location")
            if loc:
                lat = loc.get("lat", 0.0)
                lon = loc.get("lon", 0.0)

            event = DisasterEvent(
                type="flood",
                location=title,
                date=event_date,
                severity="Medium",
                latitude=lat,
                longitude=lon,
                description=unique_id
            )

            db.session.add(event)
            total_new += 1

        db.session.commit()
        print(f"✔ ReliefWeb floods added: {total_new}")

    except Exception as e:
        print("❌ Flood fetch error:", e)




import pandas as pd
from io import StringIO
import requests

def reverse_geocode(lat, lon):
    """Convert coordinates to human-readable location."""
    try:
        url = (
            f"https://nominatim.openstreetmap.org/reverse?"
            f"format=json&lat={lat}&lon={lon}&zoom=8&addressdetails=1"
        )
        r = requests.get(url, headers={"User-Agent": "TerraSentinel-Geocoder"}, timeout=10)

        if r.status_code == 200:
            data = r.json()
            addr = data.get("address", {})

            city = (
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("county")
            )
            country = addr.get("country")

            if city and country:
                return f"{city}, {country}"
            return city or country or "Unknown"

        return "Unknown"
    except:
        return "Unknown"


def fetch_fires():
    """Fetch ONLY the latest 250 wildfire hotspots globally."""
    try:
        print(f"🔥 Fetching wildfire data at {datetime.now(timezone.utc)}...", flush=True)

        fire_urls = [
            f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{os.getenv('FIRMS_API_KEY')}/VIIRS_SNPP_NRT/world/1",
            "https://firms.modaps.eosdis.nasa.gov/data/active_fire/viirs-snpp-nrt/global/viirs_snpp_nrt_global_24h.csv"
        ]

        headers = {"User-Agent": "TerraSentinel-FIRMS-Client"}
        resp = None

        # Try both sources
        for u in fire_urls:
            try:
                r = requests.get(u, headers=headers, timeout=25)
                if r.status_code == 200 and "latitude" in r.text:
                    resp = r
                    print(f"🔥 Using wildfire source: {u}")
                    break
                else:
                    print(f"⚠️ {u} returned status {r.status_code}")
            except Exception as err:
                print(f"⚠️ Failed {u}: {err}")

        if not resp:
            raise RuntimeError("All wildfire sources failed.")

        # Parse CSV into dataframe
        df = pd.read_csv(StringIO(resp.text))

        if df.empty:
            print("ℹ️ No wildfire data found.")
            return

        # Sort by date + time and take only the newest 250 fires
        df["datetime"] = pd.to_datetime(df["acq_date"] + " " + df["acq_time"].astype(str).str.zfill(4))
        df = df.sort_values("datetime", ascending=False).head(250)

        count_new = 0

        for _, row in df.iterrows():
            try:
                # Build unique ID
                fid = (
                    f"{row['acq_date']}_{row['acq_time']}_"
                    f"{row['latitude']}_{row['longitude']}_"
                    f"{row.get('bright_ti4', 0)}"
                )

                # Skip if already exists
                if DisasterEvent.query.filter_by(description=fid).first():
                    continue

                bright = float(row.get("bright_ti4", 0))
                sev = "Extreme" if bright > 380 else "High" if bright > 350 else "Medium"

                date = datetime.strptime(str(row["acq_date"]), "%Y-%m-%d").date()

                lat = float(row["latitude"])
                lon = float(row["longitude"])

                # Convert coordinates to human name
                loc_name = reverse_geocode(lat, lon)

                ev = DisasterEvent(
                    type="wildfire",
                    location=loc_name,
                    date=date,
                    severity=sev,
                    latitude=lat,
                    longitude=lon,
                    description=fid
                )

                db.session.add(ev)
                count_new += 1

            except Exception as e:
                print("⚠️ Skipped a row:", e)
                continue

        db.session.commit()
        print(f"🔥 Wildfire update complete — {count_new} new fires added.")

    except Exception as e:
        print("⚠️ Fire data fetch failed:", e)



# 🧠 AI model training for disaster risk prediction
import pandas as pd
from sklearn.tree import DecisionTreeClassifier
from sklearn.preprocessing import LabelEncoder
import joblib

MODEL_PATH = os.path.join(basedir, "risk_model.pkl")

def train_risk_model():
    """Train or update a simple AI model for disaster risk prediction."""
    with app.app_context():
        events = DisasterEvent.query.all()
        if not events:
            print("⚠️ No events found in DB to train model.")
            return
        
        data = [{
            "type": e.type,
            "month": e.date.month,
            "severity": e.severity
        } for e in events]
        df = pd.DataFrame(data)

        severity_map = {"Low": 1, "Medium": 2, "High": 3, "Extreme": 4}
        df["severity_num"] = df["severity"].map(severity_map)

        le = LabelEncoder()
        df["type_encoded"] = le.fit_transform(df["type"])

        X = df[["type_encoded", "month"]]
        y = df["severity_num"]

        model = DecisionTreeClassifier(random_state=42)
        model.fit(X, y)

        joblib.dump({"model": model, "encoder": le}, MODEL_PATH)
        print(f"✅ Risk model trained and saved to {MODEL_PATH}")

def country_flag(alpha2: str) -> str:
    """Convert a 2-letter country code to its emoji flag."""
    if not alpha2 or len(alpha2) != 2:
        return "🏳️"
    a, b = alpha2.upper()
    return chr(127397 + ord(a)) + chr(127397 + ord(b))

from flask import session

def get_current_user_email():
    user_id = session.get("user_id")
    if not user_id:
        return None

    user = User.query.get(user_id)
    if not user:
        return None

    return user.email

# 9. Scheduled monitoring
@scheduler.task('interval', id='ten_minute_digest', minutes=5)
def send_hourly_digest():
    with app.app_context():
        try:
            print(f"🌎 [Scheduler] Fetching real-time data... {datetime.now(timezone.utc)}", flush=True)
            fetch_latest_earthquakes()
            fetch_floods()
            fetch_fires()

            now = datetime.now(timezone.utc)
            since = now - timedelta(hours=1)

            evs = DisasterEvent.query \
                .filter(DisasterEvent.severity.in_(['High', 'Extreme'])) \
                .filter(DisasterEvent.date >= since.date()) \
                .order_by(DisasterEvent.date.asc()) \
                .all()

            if not evs:
                print("ℹ️ No critical events found for this hour.")
                return

            bullets = "\n".join(
                f"- {e.type.capitalize()} at {e.location} on {e.date.isoformat()} (sev={e.severity})"
                for e in evs
            )

            system_msg = (
                "You are TerraSentinel’s digest assistant. "
                "Provide concise, bullet-point summaries with no source attributions or mention of ChatGPT/OpenAI."
            )
            user_msg = (
                f"Critical events in the past hour:\n\n{bullets}\n\n"
                "Write me a human-readable email summary."
            )

            resp = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg}
                ]
            )

            clean_summary = resp.choices[0].message.content.strip()

            # ================================
            # 🚀 SEND EMAIL ONLY TO ACTIVE USER
            # ================================
            active_user = User.query.filter_by(active=True).first()

            if not active_user:
                print("⚠️ No active user — skipping hourly digest email.")
                return

            msg = Message(
                subject=f"🌍 TerraSentinel Hourly Digest — {len(evs)} Events @ {now.strftime('%H:%M UTC')}",
                recipients=[active_user.email],
                body=clean_summary
            )
            mail.send(msg)
            print(f"📨 Sent hourly digest to {active_user.email}")

            # Save digest to DB
            digest = DigestMessage(content=clean_summary)
            db.session.add(digest)
            db.session.commit()

        except Exception as e:
            print("❌ Error in send_hourly_digest:", e)


# 11. Routes & APIs
@app.route('/')
def index():      return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    cesium_token = os.getenv("CESIUM_ION_TOKEN")
    return render_template('dashboard.html', cesium_token=cesium_token)

# -------------------------
# LOGIN / LOGOUT SYSTEM
# -------------------------

from flask import session, redirect, url_for, flash, render_template
from werkzeug.security import generate_password_hash, check_password_hash

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):

            # Save session values
            session["user_id"] = user.id
            session["username"] = user.username

            # 🔹 Mark ALL users inactive
            User.query.update({User.active: False})

            # 🔹 Mark only this user active
            user.active = True

            # 🔹 ADMIN DETECTION (supports username OR email)
            admin_usernames = ["ErgiAdmin"]
            admin_emails = ["ergi1hokja@gmail.com"]

            session["is_admin"] = (
                user.username in admin_usernames
                or user.email in admin_emails
            )

            db.session.commit()

            return redirect(url_for("dashboard"))

        flash("Invalid username or password")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/api/disaster_count")
def disaster_count():
    count = DisasterEvent.query.count()
    return {"count": count}

@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username")
        email = request.form.get("email")
        password = request.form.get("password")

        # Check existing user (username OR email)
        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or Email already exists.")
            return redirect(url_for("signup"))

        # Create user
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, email=email, password=hashed_password)

        db.session.add(new_user)
        db.session.commit()

        flash("Account created successfully! Please login.")
        return redirect(url_for("login"))

    return render_template("signup.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email")
        user = User.query.filter_by(email=email).first()

        if not user:
            flash("No account found with that email.")
            return redirect(url_for("forgot_password"))

        token = generate_reset_token()
        user.reset_token = token
        db.session.commit()

        reset_link = url_for("reset_password", token=token, _external=True)

        msg = Message(
            subject="TerraSentinel — Reset Your Password",
            recipients=[user.email],
            body=f"Click here to reset your password:\n\n{reset_link}"
        )
        mail.send(msg)

        flash("Password reset link sent to your email.")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")
@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = User.query.filter_by(reset_token=token).first()

    if not user:
        return "Invalid or expired token", 404

    if request.method == "POST":
        new_pass = request.form.get("password")
        user.password = generate_password_hash(new_pass)
        user.reset_token = None  # clear token after use
        db.session.commit()

        flash("Password updated! Please log in.")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)

# PAGE ROUTE (renders alerts.html)
@app.route("/alerts")
def alerts():
    if "user_id" not in session:  
        return redirect("/login")
    return render_template("alerts.html")

@app.route('/api/alerts', methods=['GET'])
def get_recent_alerts():
    """Return recent critical events, optionally filtered by type."""
    event_type = request.args.get('type', '').strip().lower()

    query = DisasterEvent.query.filter(
        DisasterEvent.severity.in_(['High', 'Extreme'])
    )

    if event_type:
        query = query.filter(DisasterEvent.type == event_type)

    alerts = (
        query.order_by(DisasterEvent.date.desc(), DisasterEvent.id.desc())
        .limit(100)
        .all()
    )

    return jsonify([a.to_dict() for a in alerts])

@app.route('/about')
def about():      return render_template('about.html')

@app.route('/api/disasters', methods=['GET'])
def get_disasters():
    # Fetch up to 250 of each type — newest first
    earthquakes = db.session.query(DisasterEvent) \
        .filter_by(type='earthquake') \
        .order_by(DisasterEvent.date.desc(), DisasterEvent.id.desc()) \
        .limit(250).all()

    floods = db.session.query(DisasterEvent) \
        .filter_by(type='flood') \
        .order_by(DisasterEvent.date.desc(), DisasterEvent.id.desc()) \
        .limit(250).all()

    wildfires = db.session.query(DisasterEvent) \
        .filter_by(type='wildfire') \
        .order_by(DisasterEvent.date.desc(), DisasterEvent.id.desc()) \
        .limit(250).all()

    # Combine all types and sort overall by newest first
    all_disasters = earthquakes + floods + wildfires
    all_disasters.sort(key=lambda e: (e.date, e.id), reverse=True)

    return jsonify([e.to_dict() for e in all_disasters])


@app.route('/api/disasters', methods=['POST'])
def create_disaster():
    payload = request.get_json() or {}
    try:
        coords = payload.get('coords', [])
        if not (isinstance(coords, list) and len(coords)==2):
            return jsonify(error="coords must be [lat,lng]"), 400
        dt = datetime.strptime(payload['date'],"%Y-%m-%d").date()
        evt = DisasterEvent(
            type        = payload.get('type',''),
            location    = payload.get('location',''),
            date        = dt,
            severity    = payload.get('severity',''),
            latitude    = float(coords[0]),
            longitude   = float(coords[1]),
            description = payload.get('description','')
        )
        db.session.add(evt); db.session.commit()
        if evt.severity in ('High','Extreme'):
            socketio.emit('new_alert', evt.to_dict())
        return jsonify(evt.to_dict()), 201
    except Exception as ex:
        app.logger.exception("create_disaster")
        return jsonify(error=str(ex)), 500

@app.route('/inbox')
def inbox():
    digests = DigestMessage.query.order_by(DigestMessage.sent_at.desc()).all()
    return render_template('inbox.html', digests=digests)

# ✅ HYBRID PREDICT_RISK ROUTE (AI MODEL + FULL MARKDOWN FORECAST)
@app.route('/api/predict_risk', methods=['POST'])
def predict_risk():
    import numpy as np
    from datetime import datetime, timedelta

    # -----------------------------
    # ✅ REQUIRE LOGIN
    # -----------------------------
    if not session.get("user_id"):
        return jsonify(error="login_required"), 401

    # -----------------------------
    # 🔍 INPUT DATA
    # -----------------------------
    data = request.get_json() or {}
    region = data.get('region', '').strip()
    timeframe = data.get('timeframe', 'next week').strip().lower()

    if not region:
        return jsonify(error="Region required"), 400

    # -----------------------------
    # 📅 LOOKBACK WINDOW
    # -----------------------------
    lookback_days = 30
    since = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)

    # -----------------------------
    # 🗺️ TRY REGION-SPECIFIC EVENTS
    # -----------------------------
    regional_events = DisasterEvent.query.filter(
        DisasterEvent.location.ilike(f"%{region}%"),
        DisasterEvent.date >= since
    ).order_by(DisasterEvent.date.desc()).all()

    if regional_events:
        events_source = regional_events
        scope_label = f"Events in/near {region}"
    else:
        # No local data → fall back to global, but be honest about it
        events_source = DisasterEvent.query.filter(
            DisasterEvent.date >= since
        ).order_by(DisasterEvent.date.desc()).all()

        if not events_source:
            return jsonify(error="No data available for prediction."), 500

        scope_label = (
            f"No recent disasters recorded in {region} during the last "
            f"{lookback_days} days. Using global patterns instead."
        )

    # -----------------------------
    # 📊 ORGANIZE EVENTS BY TYPE
    # -----------------------------
    events_by_type = {"earthquake": [], "flood": [], "wildfire": []}
    for e in events_source:
        if e.type in events_by_type:
            events_by_type[e.type].append(e)

    # -----------------------------
    # 🤖 CALCULATE RISK SCORES
    # -----------------------------
    risk_scores = {}
    sev_values = {"Low": 1, "Medium": 2, "High": 3, "Extreme": 4}

    today = datetime.utcnow().date()
    for t, evs in events_by_type.items():
        if not evs:
            risk_scores[t] = 0.0
            continue

        # events_source is ordered desc, so evs[0] is most recent
        days_since_last = (today - evs[0].date).days
        freq = len(evs)
        avg_sev = np.mean([sev_values.get(e.severity, 1) for e in evs])

        # simple heuristic: frequency + recency + severity
        score = (freq / 20.0) + ((4 - days_since_last) / 10.0) + (avg_sev / 4.0)
        risk_scores[t] = float(max(0, min(score, 1)))

    # If *everything* is zero, just say "low overall risk"
    if all(v == 0 for v in risk_scores.values()):
        likely_type = "overall"
        score = 0.0
    else:
        likely_type = max(risk_scores, key=risk_scores.get)
        score = risk_scores[likely_type]

    # -----------------------------
    # 🧠 GPT FORECAST (REALISTIC)
    # -----------------------------
    system_msg = (
        "You are TerraSentinel, an AI analyst predicting near-future natural "
        "disasters. Be realistic and data-driven. If there are no recent "
        "events in the requested region, clearly say that local risk appears "
        "low and avoid inventing threats that aren't supported by the data."
    )

    summary_lines = [f"Data scope: {scope_label}",
                     f"Lookback window: last {lookback_days} days"]
    for t, evs in events_by_type.items():
        if evs:
            last_date = evs[0].date.isoformat()
            avg_sev = np.mean([sev_values.get(e.severity, 1) for e in evs])
            summary_lines.append(
                f"{t.title()}s: {len(evs)} events, last on {last_date}, "
                f"avg severity score {avg_sev:.1f}"
            )
        else:
            summary_lines.append(f"{t.title()}s: 0 events recorded")

    summary_text = "\n".join(summary_lines)

    user_msg = (
        f"Requested region: {region}\n"
        f"Timeframe: {timeframe}\n\n"
        f"{summary_text}\n\n"
        "Based on this data only, explain the realistic near-term disaster risk "
        f"for {region}. If there are no recent events in this region, explicitly "
        "say that local risk appears low and that any estimate is based on global "
        "patterns, not local disasters."
    )

    resp = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ],
        temperature=0.6
    )

    forecast_text = resp.choices[0].message.content.strip()

    # -----------------------------
    # 💾 SAVE FORECAST WITH USER ID
    # -----------------------------
    forecast = RiskForecast(
        user_id=session["user_id"],
        region=region,
        timeframe=timeframe,
        prediction=forecast_text
    )
    db.session.add(forecast)
    db.session.commit()

    # -----------------------------
    # 📤 RESPONSE
    # -----------------------------
    risk_label = (
        "Low overall risk" if likely_type == "overall"
        else f"{likely_type.title()} Risk"
    )

    return jsonify(
        region=region,
        timeframe=timeframe,
        risk_level=f"{risk_label} ({score:.2f})",
        prediction=forecast_text
    ), 200

@app.route('/api/global_forecast', methods=['GET'])
def global_forecast():
    import numpy as np
    from datetime import datetime, timedelta

    lookback_days = 30
    since = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)
    events = DisasterEvent.query.filter(DisasterEvent.date >= since).all()

    if not events:
        return jsonify(error="No recent events found."), 500

    # Group by region keyword (first word(s) of location)
    region_stats = {}
    for e in events:
        if not e.location:
            continue
        region_key = e.location.split(",")[0].strip().title()
        sev_val = {"Low": 1, "Medium": 2, "High": 3, "Extreme": 4}.get(e.severity, 1)
        if region_key not in region_stats:
            region_stats[region_key] = {
                "events": [],
                "last_date": e.date,
                "types": {e.type},
                "sev_sum": sev_val,
                "count": 1,
            }
        else:
            region_stats[region_key]["events"].append(e)
            region_stats[region_key]["count"] += 1
            region_stats[region_key]["sev_sum"] += sev_val
            region_stats[region_key]["last_date"] = max(region_stats[region_key]["last_date"], e.date)
            region_stats[region_key]["types"].add(e.type)

    # Compute risk scores
    scored = []
    today = datetime.now(timezone.utc).date()
    for region, data in region_stats.items():
        days_since_last = (today - data["last_date"]).days
        recency = max(0.0, 1.0 - days_since_last / lookback_days)
        freq_score = min(1.0, data["count"] / 20.0)
        avg_sev = data["sev_sum"] / data["count"]
        sev_score = avg_sev / 4.0
        score = 0.4 * freq_score + 0.3 * sev_score + 0.3 * recency

        scored.append({
            "region": region,
            "types": list(data["types"]),
            "events": data["count"],
            "avg_severity": round(avg_sev, 2),
            "last_event": data["last_date"].isoformat(),
            "score": round(score, 3)
        })

    # Sort & pick top 5
    top_regions = sorted(scored, key=lambda x: x["score"], reverse=True)[:5]

    # Create summary text for GPT reasoning
    context = "\n".join(
        f"{i+1}. {r['region']} — {r['events']} recent events ({', '.join(r['types'])}), "
        f"avg severity {r['avg_severity']}, last on {r['last_event']}, score {r['score']}"
        for i, r in enumerate(top_regions)
    )

    system_msg = (
        "You are TerraSentinel, an AI disaster risk analyst. "
        "Based on recent frequency, severity, and recency trends, generate realistic 1–2 sentence forecasts "
        "for each of the listed regions, focusing on which disaster type is most likely next week. "
        "Use measured, factual language — avoid sensational terms."
    )
    user_msg = (
        f"Recent global disaster activity:\n{context}\n\n"
        "Write concise forecasts for these regions, e.g.:\n"
        "- Japan: Increased risk of moderate earthquakes within the next week.\n"
        "- California: Possible wildfire flare-ups due to recent heat and recurring fires."
    )

    resp = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ],
        temperature=0.6
    )

    forecast_summary = resp.choices[0].message.content.strip()

    return jsonify({
        "top_regions": top_regions,
        "summary": forecast_summary
    }), 200

@app.route('/admin/test_digest')
def test_digest():
    send_hourly_digest()
    return "✅ Test hourly digest sent (if events exist)"

@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    return jsonify([e.to_dict() for e in DisasterEvent.query
                    .filter(DisasterEvent.severity.in_(['High','Extreme']))
                    .order_by(DisasterEvent.date.desc()).all()])

@app.route('/api/alerts/<int:idx>/ack', methods=['POST'])
def ack_alert(idx):
    ev = DisasterEvent.query.get_or_404(idx)
    db.session.delete(ev); db.session.commit()
    socketio.emit('alert_removed',{'id':idx})
    return jsonify(success=True), 200

@app.route('/api/forecasts', methods=['GET'])
def get_forecasts():
    if not session.get("user_id"):
        return jsonify([])

    fs = RiskForecast.query.filter_by(user_id=session["user_id"]) \
                           .order_by(RiskForecast.created_at.desc()) \
                           .all()

    return jsonify([f.to_dict() for f in fs])

@app.route('/api/forecasts', methods=['POST'])
def create_forecast():
    d = request.get_json() or {}
    f = RiskForecast(
        region     = d.get('region',''),
        timeframe  = d.get('timeframe',''),
        prediction = d.get('prediction','')
    )
    db.session.add(f)
    db.session.commit()
    return jsonify(f.to_dict()), 201

@app.route('/api/regions', methods=['GET'])
def get_regions():
    """
    Return a big, searchable list of regions:
    - All countries (from pycountry) with emoji flags
    - Distinct DB locations (as-is), marked with 📍
    """
    regions = []
    seen = set()

    # 1) All countries (primary list)
    for c in pycountry.countries:
        name = c.name
        flag = country_flag(getattr(c, "alpha_2", None))
        item = {
            "name": name,        # what the dropdown uses/returns
            "country": name,     # for display (same as name here)
            "flag": flag
        }
        if name not in seen:
            seen.add(name)
            regions.append(item)

    # 2) Distinct DB locations (optional enrichment)
    db_locations = db.session.query(DisasterEvent.location).distinct().all()
    for row in db_locations:
        loc = (row[0] or "").strip()
        if not loc or loc in seen:
            continue
        # mark DB-derived items with a pin so users can tell
        regions.append({
            "name": loc,
            "country": "From recent events",
            "flag": "📍"
        })
        seen.add(loc)

    # Sort alphabetically by name
    regions.sort(key=lambda x: x["name"])

    return jsonify(regions)

# 12. Launch
if __name__ == "__main__":
    socketio.run(app, debug=False, host='0.0.0.0')

