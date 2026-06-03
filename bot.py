import discord
from discord import app_commands
from discord.ext import commands
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Google Credentials aus Env laden (Railway) ───────────────────────────────
import json, tempfile as _tempfile
_creds_raw = os.getenv("GOOGLE_CREDENTIALS")
if _creds_raw:
    _tmp = _tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _tmp.write(_creds_raw)
    _tmp.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _tmp.name
CREDENTIALS_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", CREDENTIALS_FILE)


# ─── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID    = os.getenv("SPREADSHEET_ID")

# Sheet-Namen – passe diese an falls nötig
SHEET_MITGLIEDER  = "Mitarbeiterliste"   # Deine Haupt-Tabelle (das Bild)
SHEET_DATENBANK   = "Datenbank"          # Quell-Tabelle mit Codename + Datum

# Spalten in der Mitarbeiterliste (Buchstabe → Index, A=1)
# B=DN, C=Name, D=ID, E=Ranks, G=Date Joined, H=Urlaub, I=Strikes, L=Codename
COL_DN         = 2   # B
COL_NAME       = 3   # C
COL_ID         = 4   # D
COL_RANKS      = 5   # E
COL_DATE       = 7   # G  (Date Joined)
COL_URLAUB     = 8   # H  (Urlaub)
COL_STRIKES    = 11  # K  (Strikes)
COL_CODENAME   = 12  # L  (Codename)

# Berechtigungs-Spalten (N=14 bis AB=28)
PERMISSIONS = [
    (14, "Der FIBCO über die Schultern schauen"),
    (15, "Befragungen durchführen"),
    (16, "Akten schreiben"),
    (17, "AKS"),
    (18, "UC"),
    (19, "Bodycam anfordern"),
    (20, "NI Zugriff"),
    (21, "Beschwerde Formular bearbeiten"),
    (22, "Akten zählen"),
    (23, "HB vollstrecken"),
    (24, "Akten Überprüfung"),
    (25, "Sanktion austeilen"),
    (26, "Akten schreiben (Senior)"),
    (27, "Einweisung bei neuen Mitgliedern"),
]

# Standard-Häkchen pro Rang (Spalten-Indizes aus PERMISSIONS)
RANG_DEFAULTS = {
    "FIB-Director":          list(range(14, 28)),
    "Director of Integrity": list(range(14, 28)),
    "Curator":               list(range(14, 28)),
    "Chief of FIBCO":        list(range(14, 28)),
    "Deputy Chief of FIBCO": list(range(14, 27)),
    "Supervisor":            list(range(14, 26)),
    "Senior Mitglied":       list(range(14, 25)),
    "Counsel General":       list(range(14, 25)),
    "Mitglied":              list(range(14, 22)),
    "FIBCO Veteran":         list(range(14, 20)),
    "Trainee":               list(range(14, 17)),
}

# Zeile ab der Daten stehen (Zeile 12 laut Screenshot, Header Zeile 11)
DATA_START_ROW = 12

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Ränge genau wie auf deinem Sheet
RAENGE = [
    "FIB-Director",
    "Director of Integrity",
    "Curator",
    "Chief of FIBCO",
    "Deputy Chief of FIBCO",
    "Supervisor",
    "Senior Mitglied",
    "Counsel General",
    "Mitglied",
    "FIBCO Veteran",
    "Trainee",
]

# Rang-Farben (hex) für Embeds
RANG_FARBEN = {
    "FIB-Director":          0xE74C3C,
    "Director of Integrity": 0xE67E22,
    "Curator":               0x22D3EE,
    "Chief of FIBCO":        0xF1C40F,
    "Deputy Chief of FIBCO": 0x3498DB,
    "Supervisor":            0x95A5A6,
    "Senior Mitglied":       0x9B59B6,
    "Counsel General":       0xE74C3C,
    "Mitglied":              0x3498DB,
    "FIBCO Veteran":         0x2471A3,
    "Trainee":               0x7F8C8D,
}

# ─── Google Sheets ─────────────────────────────────────────────────────────────
def get_client():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)

def get_mitglieder_sheet():
    return get_client().open_by_key(SPREADSHEET_ID).worksheet(SHEET_MITGLIEDER)

def get_datenbank_sheet():
    return get_client().open_by_key(SPREADSHEET_ID).worksheet(SHEET_DATENBANK)

def get_datenbank_records():
    """Gibt alle Einträge aus der Datenbank zurück."""
    db = get_datenbank_sheet()
    return db.get_all_records()

def find_mitglied_row(sheet, name: str):
    """Sucht eine Zeile in Mitarbeiterliste anhand des Namens (Spalte C). Gibt Zeilennummer zurück oder None."""
    alle = sheet.col_values(COL_NAME)
    for i, val in enumerate(alle, start=1):
        if val.strip().lower() == name.strip().lower() and i >= DATA_START_ROW:
            return i
    return None

def naechste_freie_zeile(sheet):
    """Findet die nächste leere Zeile ab DATA_START_ROW in Spalte C (Name)."""
    col_c = sheet.col_values(COL_NAME)
    for i in range(DATA_START_ROW - 1, len(col_c)):
        if not col_c[i].strip():
            return i + 1
    return len(col_c) + 1

def zeile_fuer_rang(sheet, rang: str):
    """
    Findet die beste Einfügezeile für einen Rang.
    Zwischen jeder Rang-Gruppe gibt es immer eine leere Trennzeile.
    Neuer Eintrag kommt ans Ende der Gruppe, VOR der Trennzeile.
    Gibt (zeile, muss_insert) zurück.
    """
    alle = sheet.get_all_values()
    rang_col = COL_RANKS - 1
    name_col = COL_NAME - 1

    gruppe_ende = None
    in_gruppe   = False

    for i, row in enumerate(alle):
        zeile_nr = i + 1
        if zeile_nr < DATA_START_ROW:
            continue
        row_rang = row[rang_col].strip() if len(row) > rang_col else ""
        row_name = row[name_col].strip() if len(row) > name_col else ""

        if row_rang == rang and row_name:
            in_gruppe   = True
            gruppe_ende = zeile_nr
        elif in_gruppe and not row_name:
            # Leere Trennzeile gefunden → neues Mitglied kommt HIER rein
            # Die Trennzeile bleibt danach erhalten durch insert_row davor
            return (zeile_nr, True)
        elif in_gruppe and row_rang != rang and row_name:
            # Nächste Gruppe direkt dahinter → neue Zeile einfügen + Trennzeile
            return (zeile_nr, True)

    if gruppe_ende:
        # Gruppe am Ende der Tabelle → neue Zeile einfügen + Trennzeile danach
        return (gruppe_ende + 1, True)

    # Rang noch gar nicht vorhanden → ans Ende
    return (naechste_freie_zeile(sheet), False)

def copy_row_format(spreadsheet_id: str, source_row: int, target_row: int, creds):
    """Kopiert das Format von source_row auf target_row via Sheets API v4."""
    service = build("sheets", "v4", credentials=creds)
    requests = [{
        "copyPaste": {
            "source": {
                "sheetId": 0,  # wird unten dynamisch gesetzt
                "startRowIndex": source_row - 1,
                "endRowIndex": source_row,
                "startColumnIndex": 0,
                "endColumnIndex": 26
            },
            "destination": {
                "sheetId": 0,
                "startRowIndex": target_row - 1,
                "endRowIndex": target_row,
                "startColumnIndex": 0,
                "endColumnIndex": 26
            },
            "pasteType": "PASTE_FORMAT",
            "pasteOrientation": "NORMAL"
        }
    }]
    # Sheet-ID dynamisch aus Spreadsheet holen
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet_id = None
    for s in meta["sheets"]:
        if s["properties"]["title"] == SHEET_MITGLIEDER:
            sheet_id = s["properties"]["sheetId"]
            break
    if sheet_id is None:
        raise Exception(f"Sheet '{SHEET_MITGLIEDER}' nicht gefunden")
    requests[0]["copyPaste"]["source"]["sheetId"] = sheet_id
    requests[0]["copyPaste"]["destination"]["sheetId"] = sheet_id
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests}
    ).execute()

# ─── Bot ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── Autocomplete ──────────────────────────────────────────────────────────────
async def rang_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=r, value=r)
        for r in RAENGE if current.lower() in r.lower()
    ][:25]

async def datenbank_name_autocomplete(interaction: discord.Interaction, current: str):
    """Namen aus der Datenbank für Autocomplete laden."""
    try:
        records = get_datenbank_records()
        # Erster Spaltenname der Datenbank als Name-Feld — passe "Name" an falls anders
        namen = []
        for r in records:
            for key in ["Name", "name", "Codename", "codename"]:
                if key in r and r[key]:
                    namen.append(str(r[key]))
                    break
        return [
            app_commands.Choice(name=n, value=n)
            for n in namen if current.lower() in n.lower()
        ][:25]
    except Exception:
        return []

async def mitglieder_name_autocomplete(interaction: discord.Interaction, current: str):
    """Namen aus der Mitarbeiterliste für Autocomplete laden."""
    try:
        sheet = get_mitglieder_sheet()
        col_c = sheet.col_values(COL_NAME)
        namen = [v.strip() for v in col_c[DATA_START_ROW - 1:] if v.strip()]
        return [
            app_commands.Choice(name=n, value=n)
            for n in namen if current.lower() in n.lower()
        ][:25]
    except Exception:
        return []

# ─── /mitglied_hinzufuegen ─────────────────────────────────────────────────────
@tree.command(name="mitglied_hinzufuegen", description="Neues Mitglied aus der Datenbank eintragen")
@app_commands.describe(
    name="Name aus der Datenbank auswählen",
    rang="Rang des Mitglieds"
)
@app_commands.autocomplete(name=datenbank_name_autocomplete, rang=rang_autocomplete)
async def mitglied_hinzufuegen(
    interaction: discord.Interaction,
    name: str,
    rang: str
):
    if rang not in RAENGE:
        await interaction.response.send_message(
            f"❌ Ungültiger Rang! Wähle einen aus der Liste.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        # Daten aus Datenbank holen
        db_records = get_datenbank_records()
        db_eintrag = None
        for r in db_records:
            for key in ["Name", "name", "Codename", "codename"]:
                if key in r and str(r[key]).strip().lower() == name.strip().lower():
                    db_eintrag = r
                    break
            if db_eintrag:
                break

        # Datum + Codename aus Datenbank holen
        datum_beitritt = None
        codename = None
        if db_eintrag:
            for key in ["Datum", "datum", "Beitritt", "beitritt", "Date", "date", "Date Joined"]:
                if key in db_eintrag and db_eintrag[key]:
                    datum_beitritt = str(db_eintrag[key])
                    break
            for key in ["Codename", "codename"]:
                if key in db_eintrag and db_eintrag[key]:
                    codename = str(db_eintrag[key])
                    break

        if not datum_beitritt:
            datum_beitritt = datetime.now().strftime("%d.%m.%Y")

        # In Mitarbeiterliste eintragen
        sheet = get_mitglieder_sheet()

        # Prüfen ob Name schon drin ist
        if find_mitglied_row(sheet, name):
            await interaction.followup.send(f"⚠️ **{name}** ist bereits in der Mitarbeiterliste!", ephemeral=True)
            return

        zeile, muss_einfuegen = zeile_fuer_rang(sheet, rang)

        # Immer eine neue Zeile einfügen damit Trennzeilen erhalten bleiben
        sheet.insert_row([], zeile)

        # Format von der Zeile DARÜBER kopieren (letzte Zeile der gleichen Gruppe)
        format_quelle = zeile - 1
        if format_quelle >= DATA_START_ROW:
            try:
                creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
                copy_row_format(SPREADSHEET_ID, format_quelle, zeile, creds)
            except Exception as fmt_err:
                print(f"⚠️ Format konnte nicht kopiert werden: {fmt_err}")

        # Formeln für DN und ID — C-Referenz dynamisch auf die neue Zeile anpassen
        formel_dn = f'=WENN(C{zeile}=""; ""; WENNFEHLER(FILTER(Tabellenblatt37!E12:E295; Tabellenblatt37!B12:B295 = C{zeile}); "/"))'
        formel_id = f'=WENN(C{zeile}=""; ""; WENNFEHLER(FILTER(Tabellenblatt37!$A$1:$A$245; Tabellenblatt37!$B$1:$B$245 = C{zeile}); "/"))'

        # Erst Name eintragen (Spalte C), damit die Formeln sofort greifen
        sheet.update_cell(zeile, COL_NAME,  name)
        sheet.update_cell(zeile, COL_DN,    formel_dn)
        sheet.update_cell(zeile, COL_ID,    formel_id)
        sheet.update_cell(zeile, COL_RANKS,   rang)
        sheet.update_cell(zeile, COL_DATE,    datum_beitritt)
        sheet.update_cell(zeile, COL_STRIKES, "0/3")
        if codename:
            sheet.update_cell(zeile, COL_CODENAME, codename)

        # Standard-Häkchen nach Rang setzen — ✓ wenn erlaubt, ✗ wenn nicht
        default_cols = RANG_DEFAULTS.get(rang, [])
        perm_updates = []
        for col, perm_name in PERMISSIONS:
            val = "✓" if col in default_cols else "✗"
            col_letter = chr(64+col) if col <= 26 else chr(64+(col-1)//26) + chr(65+(col-1)%26)
            perm_updates.append({
                "range": f"{col_letter}{zeile}",
                "values": [[val]]
            })
        if perm_updates:
            sheet.spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": perm_updates})

        # Gesetzte Berechtigungen für Embed
        gesetzte = [name for col, name in PERMISSIONS if col in default_cols]
        perm_text = ", ".join(gesetzte) if gesetzte else "Keine"

        farbe = RANG_FARBEN.get(rang, 0x2ECC71)
        embed = discord.Embed(title="✅ Mitglied eingetragen", color=farbe)
        embed.add_field(name="Name",        value=name,                inline=True)
        embed.add_field(name="Rang",        value=rang,                inline=True)
        embed.add_field(name="Date Joined", value=datum_beitritt,      inline=True)
        embed.add_field(name="Codename",    value=codename or "—",     inline=True)
        embed.add_field(name="DN/ID",       value="Wird per Formel aus Tabellenblatt37 gezogen", inline=False)
        embed.add_field(name=f"✓ Berechtigungen ({len(gesetzte)})", value=perm_text[:1024], inline=False)
        embed.set_footer(text=f"Eingetragen in Zeile {zeile}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

# ─── /mitglied_entfernen ───────────────────────────────────────────────────────
@tree.command(name="mitglied_entfernen", description="Mitglied aus der Mitarbeiterliste entfernen")
@app_commands.describe(name="Name des Mitglieds")
@app_commands.autocomplete(name=mitglieder_name_autocomplete)
async def mitglied_entfernen(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = get_mitglieder_sheet()
        row = find_mitglied_row(sheet, name)
        if not row:
            await interaction.followup.send(f"❌ **{name}** nicht gefunden!", ephemeral=True)
            return

        sheet.delete_rows(row)
        embed = discord.Embed(
            title="🗑️ Mitglied entfernt",
            description=f"**{name}** wurde aus der Mitarbeiterliste entfernt.",
            color=0xE74C3C
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

# ─── /rang_aendern ─────────────────────────────────────────────────────────────
@tree.command(name="rang_aendern", description="Rang eines Mitglieds ändern (Uprank / Downrank)")
@app_commands.describe(
    name="Name des Mitglieds",
    neuer_rang="Der neue Rang"
)
@app_commands.autocomplete(name=mitglieder_name_autocomplete, neuer_rang=rang_autocomplete)
async def rang_aendern(interaction: discord.Interaction, name: str, neuer_rang: str):
    if neuer_rang not in RAENGE:
        await interaction.response.send_message("❌ Ungültiger Rang!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        sheet = get_mitglieder_sheet()
        row = find_mitglied_row(sheet, name)
        if not row:
            await interaction.followup.send(f"❌ **{name}** nicht gefunden!", ephemeral=True)
            return

        alter_rang = sheet.cell(row, COL_RANKS).value or "Unbekannt"
        sheet.update_cell(row, COL_RANKS, neuer_rang)

        alter_idx = RAENGE.index(alter_rang) if alter_rang in RAENGE else 99
        neuer_idx = RAENGE.index(neuer_rang)

        # Niedrigerer Index = höherer Rang in der Liste
        if neuer_idx < alter_idx:
            aktion, farbe = "⬆️ Uprank", 0x2ECC71
        elif neuer_idx > alter_idx:
            aktion, farbe = "⬇️ Downrank", 0xE74C3C
        else:
            aktion, farbe = "🔄 Rang geändert", 0x3498DB

        embed = discord.Embed(title=aktion, color=farbe)
        embed.add_field(name="Name",       value=name,      inline=True)
        embed.add_field(name="Alter Rang", value=alter_rang, inline=True)
        embed.add_field(name="Neuer Rang", value=neuer_rang, inline=True)
        embed.set_footer(text=datetime.now().strftime("%d.%m.%Y %H:%M"))
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

# ─── /name_aendern ─────────────────────────────────────────────────────────────
@tree.command(name="name_aendern", description="Name eines Mitglieds in der Liste ändern")
@app_commands.describe(
    alter_name="Aktueller Name",
    neuer_name="Neuer Name"
)
@app_commands.autocomplete(alter_name=mitglieder_name_autocomplete)
async def name_aendern(interaction: discord.Interaction, alter_name: str, neuer_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = get_mitglieder_sheet()
        row = find_mitglied_row(sheet, alter_name)
        if not row:
            await interaction.followup.send(f"❌ **{alter_name}** nicht gefunden!", ephemeral=True)
            return

        sheet.update_cell(row, COL_NAME, neuer_name)

        embed = discord.Embed(title="✏️ Name geändert", color=0x9B59B6)
        embed.add_field(name="Alter Name", value=alter_name, inline=True)
        embed.add_field(name="Neuer Name", value=neuer_name, inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

# ─── /mitglied_info ────────────────────────────────────────────────────────────
@tree.command(name="mitglied_info", description="Infos zu einem Mitglied anzeigen")
@app_commands.describe(name="Name des Mitglieds")
@app_commands.autocomplete(name=mitglieder_name_autocomplete)
async def mitglied_info(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = get_mitglieder_sheet()
        row = find_mitglied_row(sheet, name)
        if not row:
            await interaction.followup.send(f"❌ **{name}** nicht gefunden!", ephemeral=True)
            return

        zeile = sheet.row_values(row)
        # Padding falls Zeile kurz ist
        while len(zeile) < max(COL_DN, COL_NAME, COL_ID, COL_RANKS, COL_DATE, COL_URLAUB):
            zeile.append("")

        def cell(col): return zeile[col - 1] if zeile[col - 1] else "—"

        rang = cell(COL_RANKS)
        farbe = RANG_FARBEN.get(rang, 0xF39C12)

        embed = discord.Embed(title=f"🔍 {name}", color=farbe)
        embed.add_field(name="DN",          value=cell(COL_DN),       inline=True)
        embed.add_field(name="ID",          value=cell(COL_ID),       inline=True)
        embed.add_field(name="Rang",        value=rang,               inline=True)
        embed.add_field(name="Date Joined", value=cell(COL_DATE),     inline=True)
        embed.add_field(name="Strikes",     value=cell(COL_STRIKES),  inline=True)
        embed.add_field(name="Codename",    value=cell(COL_CODENAME), inline=True)
        embed.add_field(name="Urlaub",      value=cell(COL_URLAUB),   inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

# ─── /mitglieder_liste ─────────────────────────────────────────────────────────
@tree.command(name="mitglieder_liste", description="Alle Mitglieder anzeigen, optional nach Rang filtern")
@app_commands.describe(rang="Nur Mitglieder dieses Rangs (optional)")
@app_commands.autocomplete(rang=rang_autocomplete)
async def mitglieder_liste(interaction: discord.Interaction, rang: str = None):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = get_mitglieder_sheet()
        alle_zeilen = sheet.get_all_values()

        mitglieder = []
        for zeile in alle_zeilen[DATA_START_ROW - 1:]:
            while len(zeile) < COL_RANKS:
                zeile.append("")
            name_val  = zeile[COL_NAME - 1].strip()
            rang_val  = zeile[COL_RANKS - 1].strip()
            if not name_val:
                continue
            if rang and rang_val != rang:
                continue
            mitglieder.append((name_val, rang_val))

        if not mitglieder:
            await interaction.followup.send("📋 Keine Mitglieder gefunden.", ephemeral=True)
            return

        # Gruppieren nach Rang (Reihenfolge aus RAENGE)
        grouped = {}
        for n, r in mitglieder:
            grouped.setdefault(r, []).append(n)

        embed = discord.Embed(
            title=f"📋 Mitarbeiterliste{f' – {rang}' if rang else ''}",
            color=0x2C3E50
        )
        embed.set_footer(text=f"Gesamt: {len(mitglieder)} Mitglied(er) · Federal Investigation Bureau")

        for rang_name in RAENGE:
            if rang_name in grouped:
                namen = "\n".join(f"• {n}" for n in grouped[rang_name])
                embed.add_field(name=f"{rang_name} ({len(grouped[rang_name])})", value=namen, inline=False)

        # Ränge die nicht in RAENGE sind trotzdem anzeigen
        for rang_name, namen_list in grouped.items():
            if rang_name not in RAENGE:
                namen = "\n".join(f"• {n}" for n in namen_list)
                embed.add_field(name=rang_name, value=namen, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

# ─── /urlaub_setzen ────────────────────────────────────────────────────────────
@tree.command(name="urlaub_setzen", description="Urlaub für ein Mitglied eintragen oder entfernen")
@app_commands.describe(
    name="Name des Mitglieds",
    urlaub="Urlaub-Eintrag (leer lassen zum Entfernen)"
)
@app_commands.autocomplete(name=mitglieder_name_autocomplete)
async def urlaub_setzen(interaction: discord.Interaction, name: str, urlaub: str = ""):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = get_mitglieder_sheet()
        row = find_mitglied_row(sheet, name)
        if not row:
            await interaction.followup.send(f"❌ **{name}** nicht gefunden!", ephemeral=True)
            return

        sheet.update_cell(row, COL_URLAUB, urlaub)

        if urlaub:
            embed = discord.Embed(title="🏖️ Urlaub eingetragen", color=0xF39C12)
            embed.add_field(name="Mitglied", value=name,   inline=True)
            embed.add_field(name="Urlaub",   value=urlaub, inline=True)
        else:
            embed = discord.Embed(title="✅ Urlaub entfernt", color=0x2ECC71,
                                  description=f"**{name}** ist zurück.")

        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ─── /strike_setzen ────────────────────────────────────────────────────────────
@tree.command(name="strike_setzen", description="Strike für ein Mitglied setzen (0/3, 1/3, 2/3, 3/3)")
@app_commands.describe(
    name="Name des Mitglieds",
    strikes="Anzahl der Strikes"
)
@app_commands.autocomplete(name=mitglieder_name_autocomplete)
@app_commands.choices(strikes=[
    app_commands.Choice(name="0/3 – Keine Strikes",  value="0/3"),
    app_commands.Choice(name="1/3 – Ein Strike",     value="1/3"),
    app_commands.Choice(name="2/3 – Zwei Strikes",   value="2/3"),
    app_commands.Choice(name="3/3 – Drei Strikes",   value="3/3"),
])
async def strike_setzen(interaction: discord.Interaction, name: str, strikes: str):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = get_mitglieder_sheet()
        row = find_mitglied_row(sheet, name)
        if not row:
            await interaction.followup.send(f"❌ **{name}** nicht gefunden!", ephemeral=True)
            return

        alter_strike = sheet.cell(row, COL_STRIKES).value or "0/3"
        sheet.update_cell(row, COL_STRIKES, strikes)

        farben = {"0/3": 0x2ECC71, "1/3": 0xF39C12, "2/3": 0xE67E22, "3/3": 0xE74C3C}
        farbe = farben.get(strikes, 0xE74C3C)

        embed = discord.Embed(title="⚠️ Strike aktualisiert", color=farbe)
        embed.add_field(name="Mitglied",     value=name,        inline=True)
        embed.add_field(name="Alter Stand",  value=alter_strike, inline=True)
        embed.add_field(name="Neuer Stand",  value=strikes,     inline=True)
        if strikes == "3/3":
            embed.add_field(name="⛔ Achtung", value="Mitglied hat 3/3 Strikes erreicht!", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ─── /berechtigungen ───────────────────────────────────────────────────────────
@tree.command(name="berechtigungen", description="Berechtigungen eines Mitglieds anpassen")
@app_commands.describe(name="Name des Mitglieds")
@app_commands.autocomplete(name=mitglieder_name_autocomplete)
async def berechtigungen(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        sheet = get_mitglieder_sheet()
        row = find_mitglied_row(sheet, name)
        if not row:
            await interaction.followup.send(f"❌ **{name}** nicht gefunden!", ephemeral=True)
            return

        # Aktuelle Häkchen lesen
        zeile_data = sheet.row_values(row)
        while len(zeile_data) < 28:
            zeile_data.append("")

        aktuell = []
        for col, perm_name in PERMISSIONS:
            val = zeile_data[col - 1].strip()
            if val == "✓":
                aktuell.append(perm_name)

        embed = discord.Embed(
            title=f"🔐 Berechtigungen: {name}",
            color=0x3498DB
        )
        for col, perm_name in PERMISSIONS:
            val = zeile_data[col - 1].strip()
            status = "✅" if val == "✓" else "❌"
            embed.add_field(name=f"{status} {perm_name}", value="​", inline=True)

        embed.set_footer(text="Nutze das Dashboard um Berechtigungen zu ändern")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ─── /datenbank_hinzufuegen ────────────────────────────────────────────────────
@tree.command(name="datenbank_hinzufuegen", description="Neue Person in die Datenbank eintragen")
@app_commands.describe(
    name="Echter Name der Person",
    codename="Codename der Person",
    datum="Beitrittsdatum (z.B. 01.06.2026) — leer lassen für heutiges Datum"
)
async def datenbank_hinzufuegen(interaction: discord.Interaction, name: str, codename: str, datum: str = ""):
    await interaction.response.defer(ephemeral=True)
    try:
        db = get_datenbank_sheet()
        records = db.get_all_records()

        # Prüfen ob bereits vorhanden
        for r in records:
            if str(r.get("Name", "")).strip().lower() == name.strip().lower():
                await interaction.followup.send(f"⚠️ **{name}** ist bereits in der Datenbank!", ephemeral=True)
                return

        if not datum:
            datum = datetime.now().strftime("%d.%m.%Y")

        db.append_row([name, codename, datum])

        embed = discord.Embed(title="✅ In Datenbank eingetragen", color=0x22C55E)
        embed.add_field(name="Name",        value=name,     inline=True)
        embed.add_field(name="Codename",    value=codename, inline=True)
        embed.add_field(name="Date Joined", value=datum,    inline=True)
        embed.set_footer(text="Person ist jetzt per Autocomplete bei /mitglied_hinzufuegen verfügbar")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

# ─── Bot Start ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot eingeloggt als {bot.user}")
    print(f"📋 Sheet: {SHEET_MITGLIEDER} | Quelle: {SHEET_DATENBANK}")

bot.run(DISCORD_TOKEN)
