import os, json, secrets, requests, tempfile
from flask import Flask, redirect, request, session, send_file, jsonify, abort
from google.oauth2.service_account import Credentials
import gspread
from googleapiclient.discovery import build
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

# ─── Active Users Tracking ────────────────────────────────────────────────────
active_users = {}

@app.before_request
def track_active():
    if is_logged_in():
        user = session["discord_user"]["username"]
        active_users[user] = datetime.now()
        cutoff = datetime.now()
        inactive = [u for u, t in active_users.items() if (cutoff - t).seconds > 300]
        for u in inactive:
            del active_users[u]

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

# ─── Activity Log (in-memory, letzte 200 Einträge) ───────────────────────────
from collections import deque
activity_log = deque(maxlen=200)

def log_action(user: str, action: str, target: str, detail: str = ""):
    """Loggt eine Aktion mit Zeitstempel."""
    activity_log.appendleft({
        "time":   datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        "user":   user,
        "action": action,
        "target": target,
        "detail": detail,
    })

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
    "Senior Mitglied", "Counsel General", "Mitglied", "FIBCO Veteran", "Trainee",
]

RANG_DEFAULTS = {
    "FIB Director":          list(range(14, 28)),
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

COL = {"DN":2,"NAME":3,"ID":4,"RANG":5,"DATE":7,"URLAUB":9,"STRIKES":11,"CODENAME":12}
DATA_START = 12

# ─── Google Sheets ────────────────────────────────────────────────────────────
def get_client():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES_SHEETS)
    return gspread.authorize(creds)

def get_sheet():
    return get_client().open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

def get_db_sheet():
    return get_client().open_by_key(SPREADSHEET_ID).worksheet(DB_SHEET_NAME)

def find_row_by_name(sheet, name: str):
    """Sucht die aktuelle Zeilennummer eines Mitglieds anhand des Namens."""
    col_c = sheet.col_values(COL["NAME"])
    for i, val in enumerate(col_c, start=1):
        if val.strip().lower() == name.strip().lower() and i >= DATA_START:
            return i
    return None

def zeile_fuer_rang_srv(sheet, rang: str):
    """Findet die Einfügezeile für einen Rang in der Mitarbeiterliste."""
    rows     = sheet.get_all_values()
    rang_col = COL["RANG"] - 1
    name_col = COL["NAME"] - 1
    gruppe_ende = None
    in_gruppe   = False

    for i, row in enumerate(rows[DATA_START-1:], start=DATA_START):
        rr = row[rang_col].strip() if len(row) > rang_col else ""
        rn = row[name_col].strip() if len(row) > name_col else ""
        if rr == rang and rn:
            in_gruppe   = True
            gruppe_ende = i
        elif in_gruppe and not rn:
            return (i, True)
        elif in_gruppe and rr != rang and rn:
            return (i, True)

    if gruppe_ende:
        return (gruppe_ende + 1, True)

    # Rang nicht gefunden → ans Ende
    col_c = sheet.col_values(COL["NAME"])
    for i, v in enumerate(col_c[DATA_START-1:], start=DATA_START):
        if not v.strip():
            return (i, False)
    return (len(col_c) + 1, False)

def copy_row_format_srv(spreadsheet_id, source_row, target_row, creds):
    """Kopiert Format von source_row auf target_row via Sheets API v4."""
    try:
        service = build("sheets", "v4", credentials=creds)
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_id = None
        for s in meta["sheets"]:
            if s["properties"]["title"] == SHEET_NAME:
                sheet_id = s["properties"]["sheetId"]
                break
        if sheet_id is None:
            return
        requests = [{
            "copyPaste": {
                "source":      {"sheetId": sheet_id, "startRowIndex": source_row-1, "endRowIndex": source_row, "startColumnIndex": 0, "endColumnIndex": 30},
                "destination": {"sheetId": sheet_id, "startRowIndex": target_row-1, "endRowIndex": target_row, "startColumnIndex": 0, "endColumnIndex": 30},
                "pasteType": "PASTE_FORMAT",
                "pasteOrientation": "NORMAL"
            }
        }]
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests}
        ).execute()
    except Exception as e:
        print(f"⚠️ Format copy failed: {e}")

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
        perms     = data["perms"]
        name      = data.get("name", "")
        sheet     = get_sheet()
        # Zeilennummer live aus Sheet lesen
        row_index = find_row_by_name(sheet, name) if name else data.get("rowIndex")
        if not row_index:
            return jsonify({"error": f"Mitglied '{name}' nicht gefunden"}), 404
        # Alle 14 Berechtigungen in einem batch update → viel schneller
        updates = []
        for p in PERMISSIONS:
            val = "✓" if perms.get(p["key"]) else "✗"
            col_letter = chr(64 + p["col"]) if p["col"] <= 26 else chr(64 + (p["col"]-1)//26) + chr(65 + (p["col"]-1)%26)
            updates.append({
                "range": f"{SHEET_NAME}!{col_letter}{row_index}",
                "values": [[val]]
            })
        sheet.spreadsheet.values_batch_update({
            "valueInputOption": "RAW",
            "data": updates
        })
        user = session['discord_user']['username']
        changed_count = sum(1 for v in perms.values() if v)
        log_action(user, "Berechtigungen", name, f"{changed_count}/14 aktiv")
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {user} → Berechtigungen {name}")
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
        name      = data.get("originalName") or data.get("name", "")
        sheet     = get_sheet()
        # Zeilennummer live aus Sheet lesen
        row_index = find_row_by_name(sheet, name) if name else data.get("rowIndex")
        if not row_index:
            return jsonify({"error": f"Mitglied '{name}' nicht gefunden"}), 404

        new_rang   = data.get("rang", "")
        old_rang   = sheet.cell(row_index, COL["RANG"]).value or ""
        rang_changed = "rang" in data and new_rang != old_rang and data.get("updatePerms")

        if rang_changed:
            # ── Rang geändert → Zeile verschieben ──────────────────────────────
            # Alle aktuellen Daten der Zeile lesen
            row_data = sheet.row_values(row_index)
            while len(row_data) < 28:
                row_data.append("")

            # Alte Zeile löschen + Trennzeile danach falls leer
            sheet.delete_rows(row_index)

            # Neue Position in der Zielgruppe finden
            zeile_neu, _ = zeile_fuer_rang_srv(sheet, new_rang)

            # Neue Zeile einfügen
            sheet.insert_row([], zeile_neu)

            # Format vom letzten Mitglied der Zielgruppe kopieren
            # Suche die letzte Zeile mit dem neuen Rang als Format-Quelle
            fmt_quelle = zeile_neu - 1
            try:
                alle_rows = sheet.get_all_values()
                for i in range(zeile_neu - 2, DATA_START - 2, -1):
                    if i < len(alle_rows):
                        zeile_rang = alle_rows[i][COL["RANG"]-1].strip() if len(alle_rows[i]) > COL["RANG"]-1 else ""
                        zeile_name = alle_rows[i][COL["NAME"]-1].strip() if len(alle_rows[i]) > COL["NAME"]-1 else ""
                        if zeile_rang == new_rang and zeile_name:
                            fmt_quelle = i + 1
                            break
            except:
                pass
            if fmt_quelle >= DATA_START:
                try:
                    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES_SHEETS)
                    copy_row_format_srv(SPREADSHEET_ID, fmt_quelle, zeile_neu, creds)
                except Exception as e:
                    print(f"⚠️ Format: {e}")

            # Daten schreiben — Name, Datum, Codename, Strikes aus alter Zeile
            updates = []
            col_map = {
                COL["NAME"]:     row_data[COL["NAME"]-1],
                COL["DATE"]:     row_data[COL["DATE"]-1],
                COL["URLAUB"]:   row_data[COL["URLAUB"]-1],
                COL["STRIKES"]:  row_data[COL["STRIKES"]-1],
                COL["CODENAME"]: row_data[COL["CODENAME"]-1],
                COL["RANG"]:     new_rang,
            }
            if data.get("name"):     col_map[COL["NAME"]]     = data["name"]
            if data.get("codename"): col_map[COL["CODENAME"]] = data["codename"]
            if data.get("date"):     col_map[COL["DATE"]]      = data["date"]
            if data.get("urlaub"):   col_map[COL["URLAUB"]]    = data["urlaub"]

            for col, val in col_map.items():
                col_letter = chr(64+col) if col <= 26 else chr(64+(col-1)//26)+chr(65+(col-1)%26)
                updates.append({"range": f"{SHEET_NAME}!{col_letter}{zeile_neu}", "values": [[val]]})

            # Formeln für DN und ID
            formel_dn = f'=WENN(C{zeile_neu}=""; ""; WENNFEHLER(FILTER(Tabellenblatt37!E12:E295; Tabellenblatt37!B12:B295 = C{zeile_neu}); "/"))'
            formel_id = f'=WENN(C{zeile_neu}=""; ""; WENNFEHLER(FILTER(Tabellenblatt37!$A$1:$A$245; Tabellenblatt37!$B$1:$B$245 = C{zeile_neu}); "/"))'
            updates.append({"range": f"{SHEET_NAME}!B{zeile_neu}", "values": [[formel_dn]]})
            updates.append({"range": f"{SHEET_NAME}!D{zeile_neu}", "values": [[formel_id]]})

            sheet.spreadsheet.values_batch_update({"valueInputOption": "USER_ENTERED", "data": updates})

            # Berechtigungen je nach Up/Downrank anpassen
            default_cols = RANG_DEFAULTS.get(new_rang, [])
            old_default_cols = RANG_DEFAULTS.get(old_rang, [])
            is_uprank = RANG_ORDER.index(new_rang) < RANG_ORDER.index(old_rang) if new_rang in RANG_ORDER and old_rang in RANG_ORDER else False

            # Alte Berechtigungen aus row_data lesen
            alte_perms = {}
            for p in PERMISSIONS:
                alte_perms[p["col"]] = row_data[p["col"]-1].strip() == "✓" if len(row_data) >= p["col"] else False

            perm_updates = []
            for p in PERMISSIONS:
                hat_es = alte_perms.get(p["col"], False)
                rang_gibt_es = p["col"] in default_cols
                if is_uprank:
                    # Uprank: behalten was er hat + neue dazu
                    val = "✓" if (hat_es or rang_gibt_es) else "✗"
                else:
                    # Downrank: auf Standard des neuen Rangs setzen
                    val = "✓" if rang_gibt_es else "✗"
                col_letter = chr(64+p["col"]) if p["col"] <= 26 else chr(64+(p["col"]-1)//26)+chr(65+(p["col"]-1)%26)
                perm_updates.append({"range": f"{SHEET_NAME}!{col_letter}{zeile_neu}", "values": [[val]]})
            sheet.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": perm_updates})

            # Trennzeile danach einfügen — nur wenn danach keine leere Zeile ist
            try:
                alle_werte2 = sheet.col_values(COL["NAME"])
                naechste2   = zeile_neu + 1
                naechste_val2 = alle_werte2[naechste2 - 1].strip() if len(alle_werte2) >= naechste2 else ""
                if naechste_val2 != "":
                    sheet.insert_row([], naechste2)
                    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES_SHEETS)
                    copy_row_format_srv(SPREADSHEET_ID, 57, naechste2, creds)
            except Exception as e:
                print(f"⚠️ Trennzeile: {e}")

        else:
            # ── Kein Rang-Wechsel → nur Felder updaten ─────────────────────────
            updates = []
            col_map_f = {"name": COL["NAME"], "codename": COL["CODENAME"], "rang": COL["RANG"],
                       "date": COL["DATE"], "urlaub": COL["URLAUB"]}
            for field, col in col_map_f.items():
                if field in data:
                    col_letter = chr(64+col) if col <= 26 else chr(64+(col-1)//26)+chr(65+(col-1)%26)
                    updates.append({"range": f"{SHEET_NAME}!{col_letter}{row_index}", "values": [[data[field]]]})
            if updates:
                sheet.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": updates})

        user = session['discord_user']['username']
        if rang_changed:
            log_action(user, "Uprank/Downrank", name, f"{old_rang} → {new_rang}")
        else:
            log_action(user, "Bearbeitet", name, ", ".join([k for k in ["name","rang","urlaub","codename"] if k in data]))
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {user} → Member update {name}")
        return jsonify({"ok": True, "moved": rang_changed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── Update Strikes ───────────────────────────────────────────────────────────
@app.route("/api/member/strikes", methods=["POST"])
def api_update_strikes():
    if not is_logged_in():
        abort(401)
    try:
        data      = request.json
        strikes   = data["strikes"]
        name      = data.get("name", "")
        sheet     = get_sheet()
        row_index = find_row_by_name(sheet, name) if name else data.get("rowIndex")
        if not row_index:
            return jsonify({"error": f"Mitglied '{name}' nicht gefunden"}), 404
        sheet.update_cell(row_index, COL["STRIKES"], strikes)
        user = session['discord_user']['username']
        log_action(user, "Strike gesetzt", name, strikes)
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {user} → Strike {strikes} {name}")
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
        name      = data.get("name", "")
        sheet     = get_sheet()
        row_index = find_row_by_name(sheet, name) if name else data.get("rowIndex")
        if not row_index:
            return jsonify({"error": f"Mitglied '{name}' nicht gefunden"}), 404
        sheet.delete_rows(row_index)
        user = session['discord_user']['username']
        log_action(user, "Entfernt", name, "")
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {user} → {name} entfernt")
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

        # Format des neuen Mitglieds von der Zeile darüber kopieren
        if insert_at - 1 >= DATA_START:
            try:
                creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES_SHEETS)
                copy_row_format_srv(SPREADSHEET_ID, insert_at - 1, insert_at, creds)
            except Exception as fmt_err:
                print(f"⚠️ Format Mitglied: {fmt_err}")

        # Trennzeile einfügen — nur wenn danach keine leere Zeile ist
        try:
            alle_werte = sheet.col_values(COL["NAME"])
            naechste   = insert_at + 1
            naechste_val = alle_werte[naechste - 1].strip() if len(alle_werte) >= naechste else ""
            if naechste_val != "":
                sheet.insert_row([], naechste)
                creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES_SHEETS)
                copy_row_format_srv(SPREADSHEET_ID, 57, naechste, creds)
        except Exception as fmt_err:
            print(f"⚠️ Trennzeile: {fmt_err}")

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

        # Default permissions — ✓ wenn erlaubt, ✗ wenn nicht
        default_cols = RANG_DEFAULTS.get(rang, [])
        for p in PERMISSIONS:
            val = "✓" if p["col"] in default_cols else "✗"
            sheet.update_cell(insert_at, p["col"], val)

        perms = {p["key"]: (p["col"] in default_cols) for p in PERMISSIONS}
        user = session['discord_user']['username']
        log_action(user, "Hinzugefügt", name, rang)
        print(f"[{datetime.now():%d.%m.%Y %H:%M}] {user} → Mitglied: {name} ({rang}) Zeile {insert_at}")
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


# ─── Activity Log API ─────────────────────────────────────────────────────────
@app.route("/api/log")
def api_log():
    if not is_logged_in():
        abort(401)
    return jsonify(list(activity_log))

@app.route("/api/active-users")
def api_active_users():
    if not is_logged_in():
        abort(401)
    cutoff = datetime.now()
    online = [u for u, t in active_users.items() if (cutoff - t).seconds <= 300]
    return jsonify(online)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
