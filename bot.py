import os, logging, math, time, sqlite3, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ----------------------- CONFIG VIA ENV -----------------------
BOT_TOKEN  = os.getenv("BOT_TOKEN")           # set in Render
CHANNEL_ID = os.getenv("CHANNEL_ID")          # like -1001234567890
LOCAL_TZ   = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
POST_TIME  = os.getenv("POST_TIME", "09:00")  # HH:MM 24h local

# ----------------------- DATA SOURCES (FREE) ------------------
# We use The Odds API ONLY for schedules & scores endpoints (no odds).
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
# If you happen to have an ODDS_API_KEY env var, we can use scores endpoints for MLB/NFL.
ODDS_API_KEY  = os.getenv("ODDS_API_KEY", None)

SPORT_KEYS = {
    "mlb": "baseball_mlb",
    "nfl": "americanfootball_nfl",
    "ufc": "mma_mixed_martial_arts",
}

# MLB park factors (runs) — simple, normalized around 100. (Sample values; enough for effect.)
MLB_PARK_FACTORS = {
    "Coors Field": 118, "Fenway Park": 106, "Yankee Stadium": 104, "Globe Life Field": 99,
    "Dodger Stadium": 101, "Wrigley Field": 102, "Oracle Park": 96, "Tropicana Field": 97,
    "T-Mobile Park": 95, "Great American Ball Park": 104, "Minute Maid Park": 101,
    "Truist Park": 102, "Busch Stadium": 98, "Citi Field": 97, "Nationals Park": 100,
    "Guaranteed Rate Field": 103, "Target Field": 98, "Comerica Park": 98, "Progressive Field": 99,
}

# NFL outdoor stadium flags (weather relevant). Indoor/dome/roof-closed -> weather ignored.
NFL_OUTDOOR_STADIA = {
    "Lambeau Field": True, "Soldier Field": True, "Arrowhead Stadium": True, "MetLife Stadium": True,
    "Highmark Stadium": True, "Lincoln Financial Field": True, "Gillette Stadium": True,
    "Cleveland Browns Stadium": True, "M&T Bank Stadium": True, "Acrisure Stadium": True,
    "TIAA Bank Field": True, "Lumen Field": True, "Empower Field at Mile High": True,
    "Levi's Stadium": True, "Raymond James Stadium": True, "Bank of America Stadium": True,
    "SoFi Stadium": False, "U.S. Bank Stadium": False, "Mercedes-Benz Stadium": False,
    "Caesars Superdome": False, "Ford Field": False, "Allegiant Stadium": False, "NRG Stadium": False,
    "AT&T Stadium": False, "State Farm Stadium": False, "Lucas Oil Stadium": False, "Nissan Stadium": True
}

# Stadium coordinates (subset used for weather). You can add more over time.
STADIUM_COORDS = {
    # MLB (sample major parks)
    "Coors Field": (39.7559, -104.9942),
    "Fenway Park": (42.3467, -71.0972),
    "Yankee Stadium": (40.8296, -73.9262),
    "Globe Life Field": (32.7473, -97.0846),
    "Dodger Stadium": (34.0739, -118.2390),
    "Wrigley Field": (41.9484, -87.6553),
    "Oracle Park": (37.7786, -122.3893),
    "T-Mobile Park": (47.5914, -122.3325),
    # NFL (sample)
    "Lambeau Field": (44.5013, -88.0622),
    "MetLife Stadium": (40.8128, -74.0742),
    "Highmark Stadium": (42.7738, -78.7867),
    "Lincoln Financial Field": (39.9008, -75.1675),
    "Gillette Stadium": (42.0909, -71.2643),
    "Arrowhead Stadium": (39.0490, -94.4839),
    "Acrisure Stadium": (40.4468, -80.0158),
    "Levi's Stadium": (37.4030, -121.9690),
    "SoFi Stadium": (33.9535, -118.3387),
    "U.S. Bank Stadium": (44.9735, -93.2575),
}

# ----------------------- STORAGE ------------------------------
DB_FILE = "model.db"

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS elo(
        key TEXT PRIMARY KEY,
        rating REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS results(
        sport TEXT,
        item_key TEXT,
        ts INTEGER,
        UNIQUE(sport, item_key)
    )""")
    return conn

def elo_get(key: str, base=1500.0):
    with db() as conn:
        row = conn.execute("SELECT rating FROM elo WHERE key=?", (key,)).fetchone()
        if row: return row[0]
        conn.execute("INSERT OR IGNORE INTO elo(key, rating) VALUES(?,?)", (key, base))
        conn.commit()
    return base

def elo_set(key: str, rating: float):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO elo(key, rating) VALUES(?,?)", (key, rating))
        conn.commit()

def elo_update(a_key, b_key, a_score, b_score, k=20.0):
    """Binary outcome Elo update for two competitors/teams."""
    Ra = elo_get(a_key)
    Rb = elo_get(b_key)
    Ea = 1.0 / (1 + 10 ** ((Rb - Ra) / 400))
    Eb = 1.0 / (1 + 10 ** ((Ra - Rb) / 400))
    Sa = 1.0 if a_score > b_score else (0.5 if a_score == b_score else 0.0)
    Sb = 1.0 - Sa
    Ra2 = Ra + k * (Sa - Ea)
    Rb2 = Rb + k * (Sb - Eb)
    elo_set(a_key, Ra2); elo_set(b_key, Rb2)

# ----------------------- UTILS -------------------------------
def local_date_bounds():
    now = datetime.now(LOCAL_TZ)
    start = datetime(now.year, now.month, now.day, 0, 0, tzinfo=LOCAL_TZ)
    end = start + timedelta(days=1)
    return start, end

def to_local_str(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(LOCAL_TZ)
        return dt.strftime("%b %d • %H:%M")
    except Exception:
        return iso

def open_meteo_temp_wind(lat, lon, when: datetime):
    """Return (temp_c, wind_kmh, precipitation_prob%) near the given datetime."""
    # Open-Meteo: free, no key required.
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,wind_speed_10m,precipitation_probability",
        "timezone": "UTC",
        "start_hour": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00"),
        "end_hour": (when.astimezone(timezone.utc)+timedelta(hours=1)).strftime("%Y-%m-%dT%H:00"),
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        js = r.json()
        temps = js.get("hourly", {}).get("temperature_2m", [])
        winds = js.get("hourly", {}).get("wind_speed_10m", [])
        precp = js.get("hourly", {}).get("precipitation_probability", [])
        t = temps[0] if temps else None
        w = winds[0] if winds else None
        p = precp[0] if precp else None
        if w is not None:
            w = float(w) * 3.6  # m/s -> km/h
        return t, w, p
    except Exception:
        return None, None, None

# ----------------------- SCHEDULE/SCORES (NO ODDS) -----------
def odds_scores(sport_key, days_from=3):
    """Use The Odds API scores endpoint for MLB/NFL if ODDS_API_KEY present."""
    if not ODDS_API_KEY:
        return []
    url = f"{ODDS_API_BASE}/sports/{sport_key}/scores"
    params = {"apiKey": ODDS_API_KEY, "daysFrom": days_from}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def odds_upcoming(sport_key):
    """Use The Odds API odds endpoint ONLY to list upcoming events (teams & commence_time).
       We DO NOT read odds — just structure. Works without keys? No, it needs a key.
       If no key, we gracefully show message.
    """
    if not ODDS_API_KEY:
        return []
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h", "oddsFormat": "decimal"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

# ----------------------- FEATURE ENGINEERING -----------------
def mlb_features(game):
    """Return (home, away, start_dt, park_factor, temp, wind_kmh) best-effort."""
    home = game.get("home_team")
    away = game.get("away_team")
    iso  = game.get("commence_time")
    dt   = datetime.fromisoformat(iso.replace("Z","+00:00"))
    # park guess by home team name -> simple mapping to a park if known
    # In practice you'd map every team to its stadium; we keep minimal safe defaults.
    # We'll try using a small heuristic map:
    TEAM_PARK = {
        "Colorado Rockies": "Coors Field",
        "Boston Red Sox": "Fenway Park",
        "New York Yankees": "Yankee Stadium",
        "Los Angeles Dodgers": "Dodger Stadium",
        "Chicago Cubs": "Wrigley Field",
        "San Francisco Giants": "Oracle Park",
        "Seattle Mariners": "T-Mobile Park",
        "Texas Rangers": "Globe Life Field",
        "Cincinnati Reds": "Great American Ball Park",
    }
    park = TEAM_PARK.get(home, None)
    pf = MLB_PARK_FACTORS.get(park, 100)
    t, w, p = (None, None, None)
    if park and park in STADIUM_COORDS:
        lat, lon = STADIUM_COORDS[park]
        t, w, p = open_meteo_temp_wind(lat, lon, dt)
    return home, away, dt, pf, t, w

def nfl_features(game):
    home = game.get("home_team"); away = game.get("away_team")
    iso  = game.get("commence_time")
    dt   = datetime.fromisoformat(iso.replace("Z","+00:00"))
    # crude stadium name aliasing by home team (for demo)
    TEAM_STADIUM = {
        "Green Bay Packers": "Lambeau Field",
        "New York Jets": "MetLife Stadium",
        "New York Giants": "MetLife Stadium",
        "Buffalo Bills": "Highmark Stadium",
        "Philadelphia Eagles": "Lincoln Financial Field",
        "New England Patriots": "Gillette Stadium",
        "Kansas City Chiefs": "Arrowhead Stadium",
        "Pittsburgh Steelers": "Acrisure Stadium",
        "San Francisco 49ers": "Levi's Stadium",
        "Los Angeles Rams": "SoFi Stadium",
        "Los Angeles Chargers": "SoFi Stadium",
        "Minnesota Vikings": "U.S. Bank Stadium",
    }
    stadium = TEAM_STADIUM.get(home, None)
    out = NFL_OUTDOOR_STADIA.get(stadium, False)
    t, w, p = (None, None, None)
    if stadium and out and (stadium in STADIUM_COORDS):
        lat, lon = STADIUM_COORDS[stadium]
        t, w, p = open_meteo_temp_wind(lat, lon, dt)
    # rest days approximation via last result in DB (optional, simple)
    rest_home = 7; rest_away = 7  # default weekly
    return home, away, dt, stadium, out, t, w, p, rest_home, rest_away

def ufc_features(event):
    teams = event.get("teams", [])
    if len(teams) >= 2:
        a, b = teams[0], teams[1]
    else:
        a, b = (teams[0] if teams else "Fighter A", "Fighter B")
    iso = event.get("commence_time")
    dt  = datetime.fromisoformat(iso.replace("Z","+00:00"))
    return a, b, dt

# ----------------------- MODEL SCORING -----------------------
def logistic(x): return 1.0/(1.0+math.exp(-x))

def mlb_predict(home, away, pf, temp_c, wind_kmh):
    # Base Elo
    Rh = elo_get(f"MLB:{home}")
    Ra = elo_get(f"MLB:{away}")
    # Home field baseline ~ 30 Elo
    Rh += 30.0

    # Park factor adjustment: higher PF -> gives small boost to team with stronger bats (unknown), so mild to home
    Rh += (pf - 100) * 0.2

    # Weather: warm boosts offense (slightly helps home), heavy wind makes variance; approximate:
    if temp_c is not None:
        Rh += (temp_c - 20) * 0.5  # warmer than 20C small boost to home
    if wind_kmh is not None and wind_kmh > 30:
        Rh += 3  # tiny nudge

    # Convert Elo diff to win prob
    diff = Rh - Ra
    ph = 1.0 / (1.0 + 10 ** (-diff/400))
    return ph

def nfl_predict(home, away, outdoor, temp_c, wind_kmh, precip_prob, rest_home, rest_away):
    Rh = elo_get(f"NFL:{home}")
    Ra = elo_get(f"NFL:{away}")
    # Home field ~ 55 Elo, slight for domes reduced weather impact
    Rh += 55.0
    # Rest advantage
    Rh += (rest_home - rest_away) * 1.5
    # Weather penalties to passing teams unknown; apply generic if windy/cold/rain
    if outdoor:
        if wind_kmh and wind_kmh >= 32:
            Rh += 5
        if temp_c is not None and temp_c <= 5:
            Rh += 3
        if precip_prob and precip_prob >= 60:
            Rh += 2
    diff = Rh - Ra
    ph = 1.0 / (1.0 + 10 ** (-diff/400))
    return ph

def ufc_predict(fa, fb):
    # Start equal; maintain Elo as fights complete (not auto-updated here)
    Ra = elo_get(f"UFC:{fa}")
    Rb = elo_get(f"UFC:{fb}")
    diff = Ra - Rb
    pa = 1.0 / (1.0 + 10 ** (-diff/400))
    return pa

# ----------------------- RENDER SLATES -----------------------
def format_pct(p): return f"{p*100:.1f}%"

def block_mlb(today_list):
    lines = []
    for g in today_list:
        home, away, dt, pf, t, w = mlb_features(g)
        ph = mlb_predict(home, away, pf, t, w)
        pick = home if ph >= 0.5 else away
        line = f"• {to_local_str(g['commence_time'])} — {away} @ {home}  →  {home} {format_pct(ph)} | {away} {format_pct(1-ph)}  → **Pick: {pick}**"
        lines.append(line)
    return "*MLB Today*\n" + "\n".join(lines) if lines else "No MLB games today."

def block_nfl(today_list):
    lines = []
    for g in today_list:
        home, away, dt, stadium, out, t, w, p, rh, ra = nfl_features(g)
        ph = nfl_predict(home, away, out, t, w, p, rh, ra)
        pick = home if ph >= 0.5 else away
        where = f" ({stadium})" if stadium else ""
        line = f"• {to_local_str(g['commence_time'])} — {away} @ {home}{where}  →  {home} {format_pct(ph)} | {away} {format_pct(1-ph)}  → **Pick: {pick}**"
        lines.append(line)
    return "*NFL Today*\n" + "\n".join(lines) if lines else "No NFL games today."

def block_ufc(today_list):
    lines = []
    for e in today_list:
        a, b, dt = ufc_features(e)
        pa = ufc_predict(a, b)
        pick = a if pa >= 0.5 else b
        line = f"• {to_local_str(e['commence_time'])} — {a} vs {b}  →  {a} {format_pct(pa)} | {b} {format_pct(1-pa)}  → **Pick: {pick}**"
        lines.append(line)
    return "*UFC Today*\n" + "\n".join(lines) if lines else "No UFC fights today."

# ----------------------- FETCH TODAY -------------------------
def filter_today(items):
    start_l, end_l = local_date_bounds()
    start_u = start_l.astimezone(timezone.utc); end_u = end_l.astimezone(timezone.utc)
    out = []
    for it in items:
        iso = it.get("commence_time")
        if not iso: continue
        dt = datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(timezone.utc)
        if start_u <= dt < end_u:
            out.append(it)
    out.sort(key=lambda x: x.get("commence_time",""))
    return out

def get_today_by_league(lg):
    key = SPORT_KEYS[lg]
    upcoming = odds_upcoming(key) if ODDS_API_KEY else []
    return filter_today(upcoming)

# ----------------------- TELEGRAM COMMANDS -------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = (
        "🏟️ Sports Oracle Bot (no moneylines)\n\n"
        "/today all — today’s MLB/NFL/UFC slate + picks\n"
        "/today mlb — today’s MLB only\n"
        "/today nfl — today’s NFL only\n"
        "/today ufc — today’s UFC only\n"
        "/autopost HH:MM — set daily post time (local)\n"
        "/tz Area/City — set timezone (e.g., Europe/Amsterdam)\n"
    )
    await update.message.reply_text(s)

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = [a.lower() for a in (context.args or [])]
    when = datetime.now(LOCAL_TZ).strftime("%b %d")
    if not ODDS_API_KEY:
        await update.message.reply_text("Set ODDS_API_KEY env var to fetch schedules.", parse_mode=ParseMode.MARKDOWN)
        return
    if not args:
        await update.message.reply_text("Usage: /today all|mlb|nfl|ufc")
        return
    blocks = []
    if args[0] == "all":
        mlb = get_today_by_league("mlb")
        nfl = get_today_by_league("nfl")
        ufc = get_today_by_league("ufc")
        if mlb: blocks.append(block_mlb(mlb))
        if nfl: blocks.append(block_nfl(nfl))
        if ufc: blocks.append(block_ufc(ufc))
    else:
        if args[0] in SPORT_KEYS:
            items = get_today_by_league(args[0])
            if args[0] == "mlb": blocks.append(block_mlb(items))
            elif args[0] == "nfl": blocks.append(block_nfl(items))
            elif args[0] == "ufc": blocks.append(block_ufc(items))
        else:
            await update.message.reply_text("Use: all|mlb|nfl|ufc")
            return
    if not blocks:
        await update.message.reply_text(f"No events today ({when}).")
    else:
        for b in blocks:
            await update.message.reply_text(b, parse_mode=ParseMode.MARKDOWN)
            time.sleep(0.4)

async def cmd_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global POST_TIME
    if not context.args:
        await update.message.reply_text(f"Current daily time: {POST_TIME}")
        return
    POST_TIME = context.args[0]
    await update.message.reply_text(f"✅ Daily post time set to {POST_TIME}. (Make sure the service is running.)")

async def cmd_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LOCAL_TZ
    if not context.args:
        await update.message.reply_text(f"Current TZ: {LOCAL_TZ}")
        return
    try:
        LOCAL_TZ = ZoneInfo(context.args[0])
        await update.message.reply_text(f"✅ Timezone updated to {context.args[0]}")
    except Exception:
        await update.message.reply_text("Invalid timezone. Example: Europe/Amsterdam")

# ----------------------- DAILY POST --------------------------
async def post_today(app: Application):
    if not CHANNEL_ID:
        return
    if not ODDS_API_KEY:
        await app.bot.send_message(CHANNEL_ID, "Set ODDS_API_KEY to fetch schedules.")
        return
    when = datetime.now(LOCAL_TZ).strftime("%b %d")
    await app.bot.send_message(CHANNEL_ID, f"📅 Today’s slate ({when})")
    for lg in ["mlb", "nfl", "ufc"]:
        items = get_today_by_league(lg)
        if lg == "mlb":
            text = block_mlb(items)
        elif lg == "nfl":
            text = block_nfl(items)
        else:
            text = block_ufc(items)
        await app.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.MARKDOWN)
        time.sleep(0.4)

def schedule_job(app: Application, scheduler: BackgroundScheduler):
    # Parse POST_TIME "HH:MM"
    try:
        hh, mm = [int(x) for x in POST_TIME.split(":")]
    except Exception:
        hh, mm = (9, 0)
    scheduler.add_job(lambda: app.create_task(post_today(app)),
                      "cron", hour=hh, minute=mm)

# ----------------------- APP MAIN ----------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing (set it in Render).")
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start",  cmd_start))
    application.add_handler(CommandHandler("today",  cmd_today))
    application.add_handler(CommandHandler("autopost", cmd_autopost))
    application.add_handler(CommandHandler("tz", cmd_tz))

    scheduler = BackgroundScheduler()
    schedule_job(application, scheduler)
    scheduler.start()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Bot running.")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
