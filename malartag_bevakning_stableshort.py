#!/usr/bin/env python3
"""
Bevakar specifika Mälartåg-nummer mellan Södertälje Syd och Eskilstuna C
via Trafikverkets officiella öppna API (api.trafikinfo.trafikverket.se) -
samma datakälla som ligger bakom trafikverket.se/trafikinformation/tag.

Fördelen mot att gissa linjer/riktningar via ResRobot: du anger exakt
vilka tågnummer som är dina vanliga resor, och vi frågar Trafikverket
rakt av om just de tågen. Ingen gissning om vilken linje ett tåg tillhör.

Två lägen, styrda av miljövariabeln MODE:
- MODE=live (standard): kollar aktuell status för alla listade tåg och
  skickar Pushover-notis direkt vid försening/inställt. Körs lämpligen
  var 10-15:e minut.
- MODE=daily_summary: sammanställer HELA dagens faktiska utfall (redan
  inträffade ankomster) för alla listade tåg och skickar en samlad
  rapport. Körs lämpligen en gång per dag, kvällstid.

Notiser: Pushover (https://pushover.net)
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------
# KONFIGURATION
# --------------------------------------------------------------------------
TRAFIKVERKET_API_KEY = os.environ.get("TRAFIKVERKET_API_KEY", "DIN_TRAFIKVERKET_NYCKEL")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "DIN_PUSHOVER_APP_TOKEN")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "DIN_PUSHOVER_USER_KEY")

STATION_A = "Södertälje Syd"
STATION_B = "Eskilstuna C"

# uppsala tåg "906", "910", "914","918","924","928","932","936","940","946","950","20954","964",
# "970","976","982","988","907","911","915","919","929","933","937","941","947","951","955","959",
# "965","971","977","983","989",
# De tågnummer du faktiskt bryr dig om.
TRAIN_NUMBERS = [
    "10900", "10902", "10904", "10906", "10922", "20958", "10901",
    "10903", "10905", "10907", "10921", "10923", "10925"
]

DELAY_THRESHOLD_MIN = 16

STATE_FILE = Path(__file__).parent / "state.json"
TRAFIKVERKET_URL = "https://api.trafikinfo.trafikverket.se/v2/data.json"
SWEDEN_TZ = ZoneInfo("Europe/Stockholm")


# --------------------------------------------------------------------------
# TRAFIKVERKET API
# --------------------------------------------------------------------------
def trafikverket_query(xml_body: str) -> dict:
    """Skickar en XML-fråga till Trafikverkets API och returnerar JSON-svaret."""
    resp = requests.post(
        TRAFIKVERKET_URL,
        data=xml_body.encode("utf-8"),
        headers={"Content-Type": "text/xml"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def find_location_signature(station_name: str) -> str:
    """
    Slår upp Trafikverkets interna 'LocationSignature' (kortkod, t.ex. 'Ec')
    för en station, genom att fråga TrainStation-objektet.
    """
    xml = f"""<REQUEST>
        <LOGIN authenticationkey="{TRAFIKVERKET_API_KEY}"/>
        <QUERY objecttype="TrainStation" schemaversion="1.4">
            <FILTER>
                <LIKE name="AdvertisedLocationName" value="{station_name}"/>
            </FILTER>
            <INCLUDE>LocationSignature</INCLUDE>
            <INCLUDE>AdvertisedLocationName</INCLUDE>
        </QUERY>
    </REQUEST>"""

    data = trafikverket_query(xml)
    results = data.get("RESPONSE", {}).get("RESULT", [{}])[0].get("TrainStation", [])

    if not results:
        raise RuntimeError(
            f"Kunde inte hitta någon station som matchar '{station_name}' hos Trafikverket."
        )

    print(f"DEBUG: sökning på '{station_name}' gav: {results}")
    # Ta den första träffen - vid flera träffar, välj den vars namn matchar exakt.
    for r in results:
        if r.get("AdvertisedLocationName", "").lower() == station_name.lower():
            return r["LocationSignature"]
    return results[0]["LocationSignature"]


def fetch_announcements(
    location_signature: str,
    train_numbers: list[str],
    date_from: datetime,
    date_to: datetime,
) -> list[dict]:
    """
    Hämtar tågannonser (ankomster) för en lista tågnummer vid en specifik
    station, inom ett tidsintervall.
    """
    train_filter = "".join(
        f'<EQ name="AdvertisedTrainIdent" value="{num}"/>' for num in train_numbers
    )
    date_from_str = date_from.astimezone(SWEDEN_TZ).strftime("%Y-%m-%dT%H:%M:%S")
    date_to_str = date_to.astimezone(SWEDEN_TZ).strftime("%Y-%m-%dT%H:%M:%S")

    xml = f"""<REQUEST>
        <LOGIN authenticationkey="{TRAFIKVERKET_API_KEY}"/>
        <QUERY objecttype="TrainAnnouncement" schemaversion="1.9">
            <FILTER>
                <AND>
                    <EQ name="ActivityType" value="Ankomst"/>
                    <EQ name="LocationSignature" value="{location_signature}"/>
                    <GT name="AdvertisedTimeAtLocation" value="{date_from_str}"/>
                    <LT name="AdvertisedTimeAtLocation" value="{date_to_str}"/>
                    <OR>{train_filter}</OR>
                </AND>
            </FILTER>
            <INCLUDE>AdvertisedTrainIdent</INCLUDE>
            <INCLUDE>AdvertisedTimeAtLocation</INCLUDE>
            <INCLUDE>EstimatedTimeAtLocation</INCLUDE>
            <INCLUDE>TimeAtLocation</INCLUDE>
            <INCLUDE>Canceled</INCLUDE>
            <INCLUDE>FromLocation</INCLUDE>
        </QUERY>
    </REQUEST>"""

    data = trafikverket_query(xml)
    return data.get("RESPONSE", {}).get("RESULT", [{}])[0].get("TrainAnnouncement", [])


def compute_delay_minutes(ann: dict) -> int:
    """
    Räknar ut förseningen i minuter. Prioriterar den FAKTISKA tiden
    (TimeAtLocation) om tåget redan passerat, annars den senaste prognosen
    (EstimatedTimeAtLocation). Ingen av delarna = ingen känd avvikelse.
    """
    advertised = ann.get("AdvertisedTimeAtLocation")
    actual = ann.get("TimeAtLocation") or ann.get("EstimatedTimeAtLocation")

    if not advertised or not actual:
        return 0

    fmt = "%Y-%m-%dT%H:%M:%S"
    planned = datetime.strptime(advertised[:19], fmt)
    real = datetime.strptime(actual[:19], fmt)
    return int((real - planned).total_seconds() // 60)


# --------------------------------------------------------------------------
# STATE / PUSHOVER
# --------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        content = STATE_FILE.read_text().strip()
        if not content:
            return {}
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def send_pushover(title: str, message: str) -> None:
    requests.post(
        "https://api.pushover.net/1/messages.json",
        data={"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": title, "message": message},
        timeout=15,
    )


# --------------------------------------------------------------------------
# LÄGE 1: LÖPANDE BEVAKNING
# --------------------------------------------------------------------------
def run_live(sig_a: str, sig_b: str) -> None:
    state = load_state()

    now = datetime.now(SWEDEN_TZ)
    window_start = now - timedelta(hours=1)
    window_end = now + timedelta(hours=3)

    announcements = []
    for sig, name in [(sig_a, STATION_A), (sig_b, STATION_B)]:
        anns = fetch_announcements(sig, TRAIN_NUMBERS, window_start, window_end)
        for a in anns:
            a["_station_name"] = name
        announcements.extend(anns)
        print(f"DEBUG: {name} ({sig}) - {len(anns)} annonser hittade")

    if os.environ.get("DEBUG_NOTIFY", "false").lower() == "true":
        avvikande = []
        i_tid = 0
        for a in announcements:
            delay = compute_delay_minutes(a)
            if a.get("Canceled"):
                avvikande.append(f"Tåg {a.get('AdvertisedTrainIdent')} till {a['_station_name']} – INSTÄLLT")
            elif delay > 0:
                avvikande.append(
                    f"Tåg {a.get('AdvertisedTrainIdent')} till {a['_station_name']} – {delay} min sen"
                )
            else:
                i_tid += 1
        msg = "\n".join(avvikande) if avvikande else "Alla hittade tåg i tid."
        msg += f"\n\n({i_tid} tåg i tid, {len(announcements)} totalt hittade)"
        send_pushover("Debug: live-koll", msg)

    for a in announcements:
        train_id = a.get("AdvertisedTrainIdent")
        station_name = a["_station_name"]
        key = f"{train_id}_{station_name}_{a.get('AdvertisedTimeAtLocation')}"
        cancelled = a.get("Canceled", False)
        delay = compute_delay_minutes(a)
        previous = state.get(key)

        if cancelled and previous != "cancelled":
            send_pushover("Tåg inställt", f"Tåg {train_id} till {station_name} är INSTÄLLT.")
            state[key] = "cancelled"
        elif delay >= DELAY_THRESHOLD_MIN and previous != delay:
            send_pushover(
                "Tågförsening",
                f"Tåg {train_id} till {station_name} är försenat {delay} minuter.",
            )
            state[key] = delay

    save_state(state)


# --------------------------------------------------------------------------
# LÄGE 2: DAGLIG SAMMANFATTNING
# --------------------------------------------------------------------------
DEFAULT_CSS = """/* ============================================================
   Mälartåg-bevakning - stilmall
   Den här filen skrivs ALDRIG över automatiskt av skriptet.
   Ändra fritt här för att byta utseende - t.ex. färger, typsnitt,
   bakgrund. Ladda upp en ny version av just den här filen till
   docs/style.css för att uppdatera sidan.
   ============================================================ */

body {
  font-family: "Comic Sans MS", "Comic Sans", cursive, sans-serif;
  max-width: 700px;
  margin: 2rem auto;
  padding: 0 1rem;
  background-color: #000033;
  background-image:
    radial-gradient(#ffffff 1px, transparent 1px),
    radial-gradient(#ffffff 1px, transparent 1px);
  background-size: 50px 50px;
  background-position: 0 0, 25px 25px;
  color: #00ffcc;
}

h1 {
  font-size: 1.8rem;
  text-align: center;
  color: #ff00ff;
  text-shadow: 2px 2px 0 #ffff00;
  border: 4px dashed #ffff00;
  padding: 0.5rem;
  background: #000000;
}

marquee, .marquee {
  display: block;
  background: #000;
  color: #00ff00;
  font-weight: bold;
  padding: 0.3rem 0;
  border-top: 2px solid #ff00ff;
  border-bottom: 2px solid #ff00ff;
}

.banner-gif {
  display: block;
  margin: 1rem auto;
  max-width: 100%;
}

table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 1rem;
  background: #000000;
  border: 3px outset #ff00ff;
}

th {
  background: #ff00ff;
  color: #000000;
  padding: 0.4rem;
}

td {
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 1px dotted #00ffcc;
}

tr.delayed { color: #ffcc00; }
tr.cancelled { color: #ff3333; font-weight: bold; }
tr.ontime { color: #33ff33; }

.meta { color: #cccccc; font-size: 0.9rem; text-align: center; }

.nav-links {
  text-align: center;
  margin: 1rem 0;
}

.nav-links a {
  color: #00ffff;
  margin: 0 0.6rem;
  text-decoration: underline;
}

.blink {
  animation: blinker 1s step-start infinite;
}
@keyframes blinker {
  50% { opacity: 0; }
}
"""


def ensure_default_stylesheet(docs_dir: Path) -> None:
    """
    Skapar docs/style.css med ett standardutseende OM filen inte redan
    finns. Rör aldrig en befintlig style.css - så dina egna ändringar
    där skrivs aldrig över av en automatisk körning.
    """
    css_path = docs_dir / "style.css"
    if not css_path.exists():
        css_path.write_text(DEFAULT_CSS, encoding="utf-8")


def build_html_report(rows: list[dict], report_date: str, css_href: str, nav_html: str) -> str:
    """Bygger en HTML-sida med ett datums fullständiga resultat."""
    table_rows = ""
    for r in rows:
        status_class = "cancelled" if r["cancelled"] else ("delayed" if r["delay"] > 0 else "ontime")
        status_text = "Inställt" if r["cancelled"] else (
            f"{r['delay']} min sen" if r["delay"] > 0 else "I tid"
        )
        table_rows += (
            f"<tr class='{status_class}'>"
            f"<td>{r['train_id']}</td><td>{r['station']}</td>"
            f"<td>{r['planned_time']}</td><td>{status_text}</td></tr>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mälartåg-bevakning – {report_date}</title>
<link rel="stylesheet" href="{css_href}">
</head>
<body>
  <h1>Mälartåg-mañana</h1>
  <p class="meta">Södertälje Syd ↔ Eskilstuna C &middot; Rapport för {report_date}</p>
  {nav_html}
  <table>
    <tr><th>Tåg</th><th>Station</th><th>Tid</th><th>Status</th></tr>
    {table_rows}
  </table>
</body>
</html>"""


def build_archive_index(archive_dir: Path) -> str:
    """Bygger en översiktssida som listar alla tidigare arkiverade dagar."""
    dates = sorted(
        (p.stem for p in archive_dir.glob("*.html") if p.stem != "index"),
        reverse=True,
    )
    links = "\n".join(f'<li><a href="{d}.html">{d}</a></li>' for d in dates)

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mälartåg-mañana – historik</title>
<link rel="stylesheet" href="../style.css">
</head>
<body>
  <h1>Historik</h1>
  <p class="nav-links"><a href="../index.html">&larr; Tillbaka till idag</a></p>
  <ul>
    {links}
  </ul>
</body>
</html>"""


def run_daily_summary(sig_a: str, sig_b: str) -> None:
    now = datetime.now(SWEDEN_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    report_date = now.strftime("%Y-%m-%d")

    rows = []
    for sig, name in [(sig_a, STATION_A), (sig_b, STATION_B)]:
        anns = fetch_announcements(sig, TRAIN_NUMBERS, day_start, day_end)
        for a in sorted(anns, key=lambda x: x.get("AdvertisedTimeAtLocation", "")):
            rows.append(
                {
                    "train_id": a.get("AdvertisedTrainIdent"),
                    "station": name,
                    "planned_time": (a.get("AdvertisedTimeAtLocation") or "")[11:16],
                    "delay": compute_delay_minutes(a),
                    "cancelled": bool(a.get("Canceled")),
                }
            )

    # Deduplicera: ett tåg som passerar båda våra stationer dyker upp en
    # gång per station. Behåll bara den kronologiskt SISTA (dvs. resans
    # slutstation för just den här sträckan) - det är den som faktiskt
    # speglar hela resans utfall, inte en mellanstation på vägen.
    latest_by_train: dict[str, dict] = {}
    for r in rows:
        train_id = r["train_id"]
        if train_id not in latest_by_train or r["planned_time"] > latest_by_train[train_id]["planned_time"]:
            latest_by_train[train_id] = r

    def _train_sort_key(r: dict):
        try:
            return int(r["train_id"])
        except (TypeError, ValueError):
            return -1

    rows = sorted(latest_by_train.values(), key=_train_sort_key, reverse=True)


    # Skriv HTML-rapporten till docs/ - GitHub Pages kan visa den publikt.
    docs_dir = Path(__file__).parent / "docs"
    archive_dir = docs_dir / "archive"
    docs_dir.mkdir(exist_ok=True)
    archive_dir.mkdir(exist_ok=True)

    ensure_default_stylesheet(docs_dir)

    today_nav = '<p class="nav-links"><a href="archive/index.html">Se historik</a></p>'
    archive_nav = (
        '<p class="nav-links"><a href="../index.html">&larr; Tillbaka till idag</a> '
        '&middot; <a href="index.html">Alla dagar</a></p>'
    )

    # Dagens sida (docs/index.html) - det du ser som standard på hemsidan.
    (docs_dir / "index.html").write_text(
        build_html_report(rows, report_date, css_href="style.css", nav_html=today_nav),
        encoding="utf-8",
    )

    # Samma rapport arkiverad under sitt datum, för historik.
    (archive_dir / f"{report_date}.html").write_text(
        build_html_report(rows, report_date, css_href="../style.css", nav_html=archive_nav),
        encoding="utf-8",
    )

    # Bygg om historik-listan så dagens datum dyker upp där också.
    (archive_dir / "index.html").write_text(build_archive_index(archive_dir), encoding="utf-8")

    # Kort Pushover-version: bara avvikelser i detalj, resten summerat.
    avvikande = [
        f"Tåg {r['train_id']} till {r['station']} kl {r['planned_time']} – "
        + ("INSTÄLLT" if r["cancelled"] else f"{r['delay']} min sen")
        for r in rows
        if r["cancelled"] or r["delay"] > 0
    ]
    ok_count = sum(1 for r in rows if not r["cancelled"] and r["delay"] <= 0)

    if not avvikande:
        message = f"Inga avvikelser idag. {ok_count} tåg i tid."
    else:
        message = "\n".join(avvikande) + f"\n\n({ok_count} övriga tåg i tid)"

    send_pushover(f"Dagens tågsammanfattning ({report_date})", message)


# --------------------------------------------------------------------------
# HUVUDPROGRAM
# --------------------------------------------------------------------------
def main() -> None:
    missing = [
        name for name, val in [
            ("TRAFIKVERKET_API_KEY", TRAFIKVERKET_API_KEY),
            ("PUSHOVER_TOKEN", PUSHOVER_TOKEN),
            ("PUSHOVER_USER", PUSHOVER_USER),
        ] if val.startswith("DIN_")
    ]
    if missing:
        sys.exit("Saknar konfiguration för: " + ", ".join(missing))

    sig_a = find_location_signature(STATION_A)
    sig_b = find_location_signature(STATION_B)
    print(f"DEBUG: platsignaturer - {STATION_A} = '{sig_a}', {STATION_B} = '{sig_b}'")

    mode = os.environ.get("MODE", "live").lower()
    if mode == "daily_summary":
        run_daily_summary(sig_a, sig_b)
    else:
        run_live(sig_a, sig_b)


if __name__ == "__main__":
    main()
