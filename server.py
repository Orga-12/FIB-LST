import os, json, secrets, requests, tempfile
from flask import Flask, redirect, request, session, send_file, jsonify, abort
from google.oauth2.service_account import Credentials
import gspread
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─── Google Credentials aus Env laden (Railway) ───────────────────────────────
_creds_raw = os.getenv("GOOGLE_CREDENTIALS")
if _creds_raw:
    _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _tmp.write(_creds_raw)
    _tmp.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _tmp.name
CREDENTIALS_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))

# ─── Config ───────────────────────────────────────────────────────────────────
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_GUILD_ID      = os.getenv("DISCORD_GUILD_ID")
REDIRECT_URI          = os.getenv("REDIRECT_URI")
SPREADSHEET_ID        = os.getenv("SPREADSHEET_ID")
SHEET_NAME            = os.getenv("SHEET_NAME", "Mitarbeiterliste")
DB_SHEET_NAME         = os.getenv("DB_SHEET_NAME", "Datenbank")

DISCORD_API   = "https://discord.com/api/v10"
SCOPES_SHEETS = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

PERMISSIONS = [
    {"col": 14, "key": "N",  "name": "Der FIBCO über die Schultern schauen"},
    {"col": 15, "key": "O",  "name": "Befragungen durchführen"},
    {"col": 16, "key": "P",  "name": "Akten schreiben"},
    {"col": 17, "key": "Q",  "name": "AKS"},
    {"col": 18, "key": "R",  "name": "UC"},
    {"col": 19, "key": "S",  "name": "Bodycam anfordern"},
    {"col": 20, "key": "T",  "name": "NI Zugriff"},
    {"col": 21, "key": "U",  "name": "Beschwerde Formular bearbeiten"},
    {"col": 22, "key": "V",  "name": "Akten zählen"},
    {"col": 23, "key": "W",  "name": "HB vollstrecken"},
    {"col": 24, "key": "X",  "name": "Akten Überprüfung"},
    {"col": 25, "key": "Y",  "name": "Sanktion austeilen"},
    {"col": 26, "key": "Z",  "name": "Akten schreiben (Senior)"},
    {"col": 27, "key": "AA", "name": "Einweisung bei neuen Mitgliedern"},
]

RANG_ORDER = [
    "FIB-Director", "Director of Integrity", "Curator",
    "Chief of FIBCO", "Deputy Chief of FIBCO", "Supervisor",
    "Senior Mitglied", "Mitglied", "FIBCO Veteran", "Trainee",
]

RANG_DEFAULTS = {
    "FIB-Director":          list(range(14, 28)),
    "Director of Integrity": list(range(14, 28)),
    "Curator":               list(range(14, 28)),
    "Chief of FIBCO":        list(range(14, 28)),
    "Deputy Chief of FIBCO": list(range(14, 27)),
    "Supervisor":            list(range(14, 26)),
    "Senior Mitglied":       list(range(14, 25)),
    "Mitglied":              list(range(14, 22)),
    "FIBCO Veteran":         list(range(14, 20)),
    "Trainee":               list(range(14, 17)),
}

COL = {"DN":2,"NAME":3,"ID":4,"RANG":5,"DATE":7,"URLAUB":8,"STRIKES":9,"CODENAME":12}
DATA_START = 12

# ─── Google Sheets ────────────────────────────────────────────────────────────
def get_client():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES_SHEETS)
    return gspread.authorize(creds)

def get_sheet():
    return get_client().open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

def get_db_sheet():
    return get_client().open_by_key(SPREADSHEET_ID).worksheet(DB_SHEET_NAME)

# ─── Discord OAuth2 ───────────────────────────────────────────────────────────
def is_logged_in():
    return "discord_user" in session and "access_token" in session

def get_discord_user(token):
    r = requests.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {token}"})
    return r.json() if r.ok else None

def is_in_guild(token, guild_id):
    r = requests.get(f"{DISCORD_API}/users/@me/guilds", headers={"Authorization": f"Bearer {token}"})
    if not r.ok:
        return False
    return any(str(g["id"]) == str(guild_id) for g in r.json())

# ─── Auth Routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if not is_logged_in():
        return send_file("login.html")
    return send_file("dashboard.html")

@app.route("/login")
def login():
    state = secrets.token_hex(16)
    session["oauth_state"] = state
    params = (f"client_id={DISCORD_CLIENT_ID}&redirect_uri={REDIRECT_URI}"
              f"&response_type=code&scope=identify+guilds&state={state}")
    return redirect(f"https://discord.com/oauth2/authorize?{params}")

@app.route("/callback")
def callback():
    code  = request.args.get("code")
    state = request.args.get("state")
    if state != session.pop("oauth_state", None):
        abort(403)
    r = requests.post(f"{DISCORD_API}/oauth2/token", data={
        "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if not r.ok:
        return "Token-Fehler: " + r.text, 400
    token_data   = r.json()
    access_token = token_data["access_token"]
    user = get_discord_user(access_token)
    if not user:
        return "User konnte nicht geladen werden.", 400
    if not is_in_guild(access_token, DISCORD_GUILD_ID):
        return send_file("denied.html"), 403
    session["discord_user"] = {
        "id": user["id"], "username": user["username"],
        "avatar": user.get("avatar"),
        "global_name": user.get("global_name", user["username"]),
    }
    session["access_token"] = access_token
    return redirect("/")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/api/me")
def api_me():
    if not is_logged_in():
        abort(401)
    return jsonify(session["discord_user"])

# ─── Members API ──────────────────────────────────────────────────────────────
@app.route("/api/members")
def api_members():
    if not is_logged_in():
        abort(401)
    try:
        sheet = get_sheet()
        rows  = sheet.get_all_values()
        members = []
        print(f"DEBUG: Sheet name={SHEET_NAME}, rows loaded={len(rows)}, spreadsheet_id={SPREADSHEET_ID}")
        for i, row in enumerate(rows[DATA_START - 1:], start=DATA_START):
            while len(row) < 27:
                row.append("")
            name = row[COL["NAME"]-1].strip()
            if not name:
                continue
            perms = {p["key"]: row[p["col"]-1].strip() == "✓" for p in PERMISSIONS}
            members.append({
                "rowIndex": i,
                "dn":       row[COL["DN"]-1],
                "name":     name,
                "id":       row[COL["ID"]-1],
                "rang":     row[COL["RANG"]-1],
                "date":     row[COL["DATE"]-1],
                "urlaub":   row[COL["URLAUB"]-1],
                "strikes":  row[COL["STRIKES"]-1] or "0/3",
                "codename": row[COL["CODENAME"]-1],
                "perms":    perms,
            })
        return jsonify(members)
    except Exception as e:
        import traceback
        print(f"ERROR /api/members: {traceback.format_exc()}")
        return jsonify({"error": str(e), "detail": traceback.format_exc()}), 500

# ─── Save Permissions ─────────────────────────────────────────────────────────
@app.route("/api/permissions", methods=["POST"])
def api_save_permissions():
    if not is_logged_in():
        abort(401)
    try:
        data      = request.json
        row_index = data["rowIndex"]
        perms     = data["perms"]
        sheet     = get_sheet()
        for p in PERMISSIONS:
            sheet.update_cell(row_index, p["col"], "✓" if perms.get(p["key"]) else "")
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {session['discord_user']['username']} → Berechtigungen Zeile {row_index}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Update Member Info ───────────────────────────────────────────────────────
@app.route("/api/member/update", methods=["POST"])
def api_update_member():
    if not is_logged_in():
        abort(401)
    try:
        data      = request.json
        row_index = data["rowIndex"]
        sheet     = get_sheet()

        if "name"     in data: sheet.update_cell(row_index, COL["NAME"],     data["name"])
        if "codename" in data: sheet.update_cell(row_index, COL["CODENAME"], data["codename"])
        if "rang"     in data: sheet.update_cell(row_index, COL["RANG"],     data["rang"])
        if "date"     in data: sheet.update_cell(row_index, COL["DATE"],     data["date"])
        if "urlaub"   in data: sheet.update_cell(row_index, COL["URLAUB"],   data["urlaub"])

        # If rang changed → update default permissions too
        if "rang" in data and data.get("updatePerms"):
            rang = data["rang"]
            default_cols = RANG_DEFAULTS.get(rang, [])
            for p in PERMISSIONS:
                val = "✓" if p["col"] in default_cols else ""
                sheet.update_cell(row_index, p["col"], val)

        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {session['discord_user']['username']} → Member update Zeile {row_index}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Update Strikes ───────────────────────────────────────────────────────────
@app.route("/api/member/strikes", methods=["POST"])
def api_update_strikes():
    if not is_logged_in():
        abort(401)
    try:
        data      = request.json
        row_index = data["rowIndex"]
        strikes   = data["strikes"]
        sheet     = get_sheet()
        sheet.update_cell(row_index, COL["STRIKES"], strikes)
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {session['discord_user']['username']} → Strike {strikes} Zeile {row_index}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Delete Member ────────────────────────────────────────────────────────────
@app.route("/api/member/delete", methods=["POST"])
def api_delete_member():
    if not is_logged_in():
        abort(401)
    try:
        data      = request.json
        row_index = data["rowIndex"]
        sheet     = get_sheet()
        sheet.delete_rows(row_index)
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {session['discord_user']['username']} → Zeile {row_index} gelöscht")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Stats ────────────────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    if not is_logged_in():
        abort(401)
    try:
        sheet   = get_sheet()
        rows    = sheet.get_all_values()
        members = [r for r in rows[DATA_START-1:] if len(r) > COL["NAME"]-1 and r[COL["NAME"]-1].strip()]
        strikes = [r for r in members if len(r) > COL["STRIKES"]-1 and r[COL["STRIKES"]-1] not in ("0/3", "/", "")]
        urlaub  = [r for r in members if len(r) > COL["URLAUB"]-1 and r[COL["URLAUB"]-1].strip()]
        return jsonify({
            "total":   len(members),
            "aktiv":   len(members) - len(urlaub),
            "strikes": len(strikes),
            "urlaub":  len(urlaub),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Add to Datenbank ─────────────────────────────────────────────────────────
@app.route("/api/datenbank/add", methods=["POST"])
def api_datenbank_add():
    if not is_logged_in():
        abort(401)
    try:
        data     = request.json
        name     = data.get("name","").strip()
        codename = data.get("codename","").strip()
        datum    = data.get("datum","").strip() or datetime.now().strftime("%d.%m.%Y")
        if not name or not codename:
            return jsonify({"error": "Name und Codename sind Pflichtfelder"}), 400
        db = get_db_sheet()
        records = db.get_all_records()
        for r in records:
            if str(r.get("Name","")).strip().lower() == name.lower():
                return jsonify({"error": f"{name} ist bereits in der Datenbank"}), 409
        db.append_row([name, codename, datum])
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {session['discord_user']['username']} → Datenbank: {name} hinzugefügt")
        return jsonify({"ok": True, "name": name, "codename": codename, "datum": datum})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Add to Mitarbeiterliste ──────────────────────────────────────────────────
@app.route("/api/member/add", methods=["POST"])
def api_member_add():
    if not is_logged_in():
        abort(401)
    try:
        data     = request.json
        name     = data.get("name","").strip()
        codename = data.get("codename","").strip()
        datum    = data.get("datum","").strip() or datetime.now().strftime("%d.%m.%Y")
        rang     = data.get("rang","").strip()
        if not name or not rang:
            return jsonify({"error": "Name und Rang sind Pflichtfelder"}), 400

        sheet = get_sheet()
        rows  = sheet.get_all_values()

        # Check duplicate
        for i, row in enumerate(rows[DATA_START-1:], start=DATA_START):
            if len(row) > COL["NAME"]-1 and row[COL["NAME"]-1].strip().lower() == name.lower():
                return jsonify({"error": f"{name} ist bereits in der Mitarbeiterliste"}), 409

        # Find best row for rang group
        rang_col  = COL["RANG"] - 1
        name_col  = COL["NAME"] - 1
        group_end = None
        in_group  = False
        insert_at = None
        must_insert = False

        for i, row in enumerate(rows[DATA_START-1:], start=DATA_START):
            rr = row[rang_col].strip() if len(row) > rang_col else ""
            rn = row[name_col].strip() if len(row) > name_col else ""
            if rr == rang and rn:
                in_group  = True
                group_end = i
            elif in_group and not rn:
                insert_at   = i
                must_insert = False
                break
            elif in_group and rr != rang and rn:
                insert_at   = i
                must_insert = True
                break

        if insert_at is None:
            if group_end:
                insert_at   = group_end + 1
                must_insert = True
            else:
                # Find next free row
                col_c = sheet.col_values(COL["NAME"])
                insert_at = next((i+1 for i,v in enumerate(col_c[DATA_START-1:],DATA_START) if not v.strip()), len(col_c)+1)
                must_insert = False

        if must_insert:
            sheet.insert_row([], insert_at)

        # Formulas for DN and ID
        formel_dn = f'=WENN(C{insert_at}=""; ""; WENNFEHLER(FILTER(Tabellenblatt37!E12:E295; Tabellenblatt37!B12:B295 = C{insert_at}); "/"))'
        formel_id = f'=WENN(C{insert_at}=""; ""; WENNFEHLER(FILTER(Tabellenblatt37!$A$1:$A$245; Tabellenblatt37!$B$1:$B$245 = C{insert_at}); "/"))'

        sheet.update_cell(insert_at, COL["NAME"],     name)
        sheet.update_cell(insert_at, COL["DN"],       formel_dn)
        sheet.update_cell(insert_at, COL["ID"],       formel_id)
        sheet.update_cell(insert_at, COL["RANG"],     rang)
        sheet.update_cell(insert_at, COL["DATE"],     datum)
        sheet.update_cell(insert_at, COL["STRIKES"],  "0/3")
        if codename:
            sheet.update_cell(insert_at, COL["CODENAME"], codename)

        # Default permissions
        default_cols = RANG_DEFAULTS.get(rang, [])
        for p in PERMISSIONS:
            val = "✓" if p["col"] in default_cols else ""
            sheet.update_cell(insert_at, p["col"], val)

        perms = {p["key"]: p["col"] in default_cols for p in PERMISSIONS}
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {session['discord_user']['username']} → Mitglied: {name} ({rang}) Zeile {insert_at}")
        return jsonify({
            "ok": True,
            "rowIndex": insert_at,
            "name": name, "codename": codename,
            "rang": rang, "datum": datum,
            "strikes": "0/3", "perms": perms,
            "dn": "", "id": "",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Get Datenbank entries ────────────────────────────────────────────────────
@app.route("/api/datenbank")
def api_datenbank():
    if not is_logged_in():
        abort(401)
    try:
        db      = get_db_sheet()
        records = db.get_all_records()
        return jsonify([{
            "name":     str(r.get("Name","")),
            "codename": str(r.get("Codename","")),
            "datum":    str(r.get("Date Joined","")),
        } for r in records if r.get("Name")])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
