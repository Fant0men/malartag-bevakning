#!/usr/bin/env python3
"""
Bevakar Mälartåg-nummer på flera linjer (rutter) via Trafikverkets
officiella öppna API (api.trafikinfo.trafikverket.se) - samma datakälla
som ligger bakom trafikverket.se/trafikinformation/tag.

Varje rutt i ROUTES nedan är helt oberoende: egna stationer, egna
tågnummerlistor, egen sida (docs/... resp. docs/stockholm/...), egen
arkiverad historik och egen "Mälardebt"-ruta. De delar bara kod, inte
data.

Två lägen, styrda av miljövariabeln MODE:
- MODE=live (standard): kollar aktuell status för alla listade tåg på
  ALLA rutter och skickar Pushover-notis direkt vid försening/inställt.
  Körs lämpligen var 10-15:e minut.
- MODE=daily_summary: sammanställer HELA dagens faktiska utfall för
  ALLA rutter och skickar en samlad rapport. Körs lämpligen en gång
  per dag, kvällstid.

Notiser: Pushover (https://pushover.net)
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SEINFELD_QUOTE_API = "https://quotes.jepcd.com/quotes?show=Seinfeld"

# --------------------------------------------------------------------------
# KONFIGURATION
# --------------------------------------------------------------------------
TRAFIKVERKET_API_KEY = os.environ.get("TRAFIKVERKET_API_KEY", "DIN_TRAFIKVERKET_NYCKEL")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "DIN_PUSHOVER_APP_TOKEN")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "DIN_PUSHOVER_USER_KEY")

# uppsala tåg "906", "910", "914","918","924","928","932","936","940","946","950","20954","964",
# "970","976","982","988","907","911","915","919","929","933","937","941","947","951","955","959",
# "965","971","977","983","989",
# De tågnummer du faktiskt bryr dig om - linjen Södertälje Syd <-> Eskilstuna C.
TRAIN_NUMBERS_ESKILSTUNA = [
    "10900", "10902", "10904", "10906", "10922", "10901",
    "10903", "10905", "10907", "10921", "10923", "10925"
]

# Tåg som går längre än 150 km på Eskilstuna-linjen - hanteras separat
# med en högre tröskel (60 min)., "977", "983", "989"
LONG_DISTANCE_TRAIN_NUMBERS_ESKILSTUNA = [
    "906", "910", "914", "918", "924", "928", "932", "936", "940", "946",
    "950", "20954", "964", "970", "976", "982", "988", "907", "911", "915",
    "919", "929", "933", "937", "941", "947", "951", "955", "959", "965",
    "971", "20958"
]

# ============================================================================
# FYLL I DINA EGNA TÅGNUMMER HÄR för linjen Södertälje Syd <-> Stockholm C.
# Samma princip som ovan: TRAIN_NUMBERS_STOCKHOLM för kortdistans (nuvarande
# tröskel DELAY_THRESHOLD_MIN), LONG_DISTANCE_TRAIN_NUMBERS_STOCKHOLM för tåg
# över 150 km (tröskel LONG_DISTANCE_DELAY_THRESHOLD_MIN). Tomma listor
# betyder att den sidan byggs men aldrig hittar några tåg än.
# ============================================================================
TRAIN_NUMBERS_STOCKHOLM: list[str] = [
    "216", "218", "200",  "220", "202", "222",  "224",  "226",  "228", "230",  
  "232", "234", "236",   "20954", "238",  "20958", "240",  "208", "242",  "244", "246", "248",
  "250", "10900", "10902", "10904", "10906", "10922", "10901",
    "10903", "10905", "10907", "10921", "10923", "10925"
]

LONG_DISTANCE_TRAIN_NUMBERS_STOCKHOLM: list[str] = [
    "118", "120", "136","138", "140","142","144", "148","906", "910", "914", "918", "924", "928", "932", "936", "940", "946",
    "4122","4124","4128", "4132","950", "20954", "964", "970", "976", "982", "988", "907", "911", "915",
    "919", "929", "933", "937", "941", "947", "951", "955", "959", "965",
    "971", "20958"
]

DELAY_THRESHOLD_MIN = 16
LONG_DISTANCE_DELAY_THRESHOLD_MIN = 60

# Ersättning per försening. Ligger separat från notiströsklarna ovan - en
# försening på 16-19 min på kortdistans ger notis men ingen ersättning.
# Gäller lika för båda rutterna.
SHORT_COMP_THRESHOLD_MIN = 20
SHORT_COMP_KR = 19
LONG_COMP_THRESHOLD_MIN = 60
LONG_COMP_KR = 37

# ----------------------------------------------------------------------------
# RUTTER - lägg till fler här i framtiden genom att lägga till ett till
# dict i listan. "docs_subdir" styr var på hemsidan sidan hamnar, "css_class"
# är kroken din style.css kan använda för att ge rutten egen grafik (se
# instruktionerna du fått separat för body.route-xxx-selektorer).
# ----------------------------------------------------------------------------
ROUTES = [
    {
        "id": "eskilstuna",
        "station_a": "Södertälje Syd",
        "station_b": "Eskilstuna C",
        "train_numbers": TRAIN_NUMBERS_ESKILSTUNA,
        "long_numbers": LONG_DISTANCE_TRAIN_NUMBERS_ESKILSTUNA,
        "docs_subdir": "docs",
        "css_class": "route-eskilstuna",
        "other_route_href": "stockholm/index.html",
        "other_route_href_from_archive": "../stockholm/index.html",
        "other_route_label": "Stockholmssidan",
        "notify": True,
    },
    {
        "id": "stockholm",
        "station_a": "Södertälje Syd",
        "station_b": "Stockholm C",
        "train_numbers": TRAIN_NUMBERS_STOCKHOLM,
        "long_numbers": LONG_DISTANCE_TRAIN_NUMBERS_STOCKHOLM,
        "docs_subdir": "docs/stockholm",
        "css_class": "route-stockholm",
        "other_route_href": "../index.html",
        "other_route_href_from_archive": "../../index.html",
        "other_route_label": "Huvudsidan (Eskilstuna)",
        "notify": False,
    },
]

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
    station, inom ett tidsintervall. Tom lista tågnummer -> tomt resultat,
    utan att ens fråga API:et (annars blir OR-filtret ogiltigt).
    """
    if not train_numbers:
        return []

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


def get_daily_quote() -> str:
    """
    Hämtar ett slumpat Seinfeld-citat. Om API:et av någon anledning inte
    svarar, används en egen (icke-upphovsrättsskyddad) reservtext istället
    för att låta hela rapporten krascha på grund av en extern tjänst som
    inte är kritisk för huvudsyftet.
    """
    try:
        resp = requests.get(SEINFELD_QUOTE_API, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("text", "").strip()
        character = data.get("character", "").strip()
        if text:
            return f'"{text}" — {character}' if character else f'"{text}"'
    except (requests.RequestException, ValueError, KeyError):
        pass
    return "Vad är det för deal med tåg som alltid är sena?"


def update_live_elements(route: dict) -> None:
    """
    Byter ut BARA citat-texten och "senast uppdaterad"-tiden i redan
    publicerade sidor för en rutt (dagens index.html och, om den finns,
    dagens arkiverade kopia) - utan att röra resten av rapporten. Används
    av run_live() så sidan känns levande även mellan de dagliga
    sammanställningarna.
    """
    quote_text = get_daily_quote()
    escaped_quote = html.escape(quote_text)
    updated_time = datetime.now(SWEDEN_TZ).strftime("%H:%M")

    quote_pattern = re.compile(r'(<div class="ticker-text">).*?(</div>)', re.DOTALL)
    time_pattern = re.compile(r'(<span class="last-updated">).*?(</span>)', re.DOTALL)

    docs_dir = Path(__file__).parent / route["docs_subdir"]
    today = datetime.now(SWEDEN_TZ).strftime("%Y-%m-%d")
    paths = [docs_dir / "index.html", docs_dir / "archive" / f"{today}.html"]

    for path in paths:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        new_content = quote_pattern.sub(rf"\1{escaped_quote}\2", content, count=1)
        new_content = time_pattern.sub(rf"\g<1>{updated_time}\g<2>", new_content, count=1)
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")
            print(f"DEBUG: [{route['id']}] uppdaterade citat/tidsstämpel i {path}")


# --------------------------------------------------------------------------
# LÄGE 1: LÖPANDE BEVAKNING
# --------------------------------------------------------------------------
def run_live_for_route(route: dict, sig_a: str, sig_b: str, state: dict) -> dict:
    if not route.get("notify", True):
        # Ingen notisbevakning för den här rutten - hoppa över Trafikverket-
        # anropen helt, men håll sidans ticker/tidsstämpel levande.
        update_live_elements(route)
        return state

    now = datetime.now(SWEDEN_TZ)
    window_start = now - timedelta(hours=1)
    window_end = now + timedelta(hours=3)

    configs = [
        ("kort", route["train_numbers"], DELAY_THRESHOLD_MIN),
        ("lång", route["long_numbers"], LONG_DISTANCE_DELAY_THRESHOLD_MIN),
    ]

    announcements = []
    for label, train_numbers, threshold in configs:
        for sig, name in [(sig_a, route["station_a"]), (sig_b, route["station_b"])]:
            anns = fetch_announcements(sig, train_numbers, window_start, window_end)
            for a in anns:
                a["_station_name"] = name
                a["_threshold"] = threshold
            announcements.extend(anns)
            print(f"DEBUG: [{route['id']}/{label}] {name} ({sig}) - {len(anns)} annonser hittade")

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
        send_pushover(f"Debug: live-koll ({route['id']})", msg)

    for a in announcements:
        train_id = a.get("AdvertisedTrainIdent")
        station_name = a["_station_name"]
        threshold = a["_threshold"]
        key = f"{route['id']}_{train_id}_{station_name}_{a.get('AdvertisedTimeAtLocation')}"
        cancelled = a.get("Canceled", False)
        delay = compute_delay_minutes(a)
        previous = state.get(key)

        if cancelled and previous != "cancelled":
            send_pushover("Tåg inställt", f"Tåg {train_id} till {station_name} är INSTÄLLT.")
            state[key] = "cancelled"
        elif delay >= threshold and previous != delay:
            send_pushover(
                "Tågförsening",
                f"Tåg {train_id} till {station_name} är försenat {delay} minuter.",
            )
            state[key] = delay

    update_live_elements(route)
    return state


def run_live() -> None:
    state = load_state()
    for route in ROUTES:
        if not route.get("notify", True):
            update_live_elements(route)
            continue
        sig_a = find_location_signature(route["station_a"])
        sig_b = find_location_signature(route["station_b"])
        state = run_live_for_route(route, sig_a, sig_b, state)
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

   Flera rutter delar den här filen. <body> får klassen "route-<id>"
   (t.ex. route-eskilstuna eller route-stockholm) - använd
   body.route-stockholm { ... } för regler som bara ska gälla en
   specifik rutt, t.ex. andra gifs.
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


def ensure_default_stylesheet(top_docs_dir: Path) -> None:
    """
    Skapar docs/style.css med ett standardutseende OM filen inte redan
    finns. Rör aldrig en befintlig style.css - så dina egna ändringar
    där skrivs aldrig över av en automatisk körning. Delas av alla rutter,
    så tar alltid emot den ÖVERSTA docs-mappen, oavsett rutt.
    """
    css_path = top_docs_dir / "style.css"
    if not css_path.exists():
        css_path.write_text(DEFAULT_CSS, encoding="utf-8")


def render_table_rows(rows: list[dict]) -> str:
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
    return table_rows


def build_html_report(
    rows: list[dict],
    report_date: str,
    css_href: str,
    nav_html: str,
    route_label: str,
    body_class: str,
    other_route_href: str,
    other_route_label: str,
    extra_sections: list[tuple[str, list[dict]]] | None = None,
    quote_text: str = "",
    show_delay_banner: bool = False,
    compensation_html: str = "",
    updated_time: str = "",
) -> str:
    """
    Bygger en HTML-sida med ett datums fullständiga resultat för en rutt.
    extra_sections kan innehålla ytterligare (rubrik, rader)-par som
    renderas som egna tabeller under huvudtabellen.
    """
    extra_html = ""
    for heading, extra_rows in (extra_sections or []):
        extra_html += f"""
  <h2>{heading}</h2>
  <table>
    <tr><th>Tåg</th><th>Station</th><th>Tid</th><th>Status</th></tr>
    {render_table_rows(extra_rows)}
  </table>"""

    delay_banner_class = "delay-banner show" if show_delay_banner else "delay-banner"

    return f"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mälartåg-bevakning – {report_date}</title>
<link rel="stylesheet" href="{css_href}">
</head>
<body class="{body_class}">
  <h1>Mälartåg-mañana</h1>
  <div class="ticker-wrap">
    <div class="ticker-text">{html.escape(quote_text)}</div>
  </div>
  <div class="{delay_banner_class}"></div>
  <div class="meta-box">
    <p class="meta">{route_label} &middot; Rapport för {report_date}</p>
    <p class="meta-updated">Senast uppdaterad: <span class="last-updated">{updated_time}</span></p>
    <p class="meta-link"><a href="{other_route_href}">{other_route_label}</a></p>
  </div>
  {compensation_html}
  {nav_html}
  <table>
    <tr><th>Tåg</th><th>Station</th><th>Tid</th><th>Status</th></tr>
    {render_table_rows(rows)}
  </table>
  {extra_html}
</body>
</html>"""


def build_archive_index(archive_dir: Path, back_href: str) -> str:
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
<title>Mälartåg-bevakning – historik</title>
<link rel="stylesheet" href="../style.css">
</head>
<body>
  <h1>Historik</h1>
  <p class="nav-links"><a href="{back_href}">&larr; Tillbaka till idag</a></p>
  <ul>
    {links}
  </ul>
</body>
</html>"""


def collect_rows(
    sig_a: str, station_a: str, sig_b: str, station_b: str,
    train_numbers: list[str], day_start: datetime, day_end: datetime,
) -> list[dict]:
    """
    Hämtar dagens ankomster för en lista tågnummer vid båda stationerna,
    deduplicerar (behåller den kronologiskt sista per tåg) och sorterar
    fallande på tågnummer.
    """
    rows = []
    for sig, name in [(sig_a, station_a), (sig_b, station_b)]:
        anns = fetch_announcements(sig, train_numbers, day_start, day_end)
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

    return sorted(latest_by_train.values(), key=_train_sort_key, reverse=True)


def rows_to_pushover_lines(rows: list[dict]) -> tuple[list[str], int]:
    """Returnerar (avvikande rader, antal tåg i tid) för Pushover-meddelandet."""
    avvikande = [
        f"Tåg {r['train_id']} till {r['station']} kl {r['planned_time']} – "
        + ("INSTÄLLT" if r["cancelled"] else f"{r['delay']} min sen")
        for r in rows
        if r["cancelled"] or r["delay"] > 0
    ]
    ok_count = sum(1 for r in rows if not r["cancelled"] and r["delay"] <= 0)
    return avvikande, ok_count


# Matchar exakt de rader render_table_rows() skriver ut.
ARCHIVE_ROW_RE = re.compile(
    r"<tr class='(?P<cls>[a-z]+)'>"
    r"<td>(?P<train>[^<]*)</td><td>[^<]*</td>"
    r"<td>(?P<time>[^<]*)</td><td>(?P<status>[^<]*)</td></tr>"
)
ARCHIVE_DELAY_RE = re.compile(r"(\d+)\s*min sen")


def rows_from_archive_html(page: str) -> list[dict]:
    """Läser tillbaka tågrader ur en arkiverad dagssida."""
    rows = []
    for m in ARCHIVE_ROW_RE.finditer(page):
        delay_match = ARCHIVE_DELAY_RE.search(m.group("status"))
        rows.append(
            {
                "train_id": m.group("train").strip(),
                "planned_time": m.group("time").strip(),
                "delay": int(delay_match.group(1)) if delay_match else 0,
                "cancelled": m.group("cls") == "cancelled",
            }
        )
    return rows


def count_compensated_delays(
    rows: list[dict], short_numbers: list[str], long_numbers: list[str]
) -> tuple[int, int]:
    """
    Returnerar (antal kortdistans-, antal långdistansförseningar) som ger
    ersättning FÖR EN GIVEN RUTTS tåglistor. Tåget klassas efter vilken
    lista numret ligger i, inte efter vilken tabell raden stod i. Ett tåg
    som står med flera rader samma dag räknas bara en gång, med den
    kronologiskt sista. Inställda tåg saknar minutsiffra och räknas alltid.
    """
    latest: dict[str, dict] = {}
    for r in rows:
        tid = str(r["train_id"])
        if tid not in latest or r["planned_time"] > latest[tid]["planned_time"]:
            latest[tid] = r

    short_n = long_n = 0
    for tid, r in latest.items():
        if tid in long_numbers:
            if r["cancelled"] or r["delay"] >= LONG_COMP_THRESHOLD_MIN:
                long_n += 1
        elif tid in short_numbers:
            if r["cancelled"] or r["delay"] >= SHORT_COMP_THRESHOLD_MIN:
                short_n += 1
    return short_n, long_n


def compensation_from_archive(
    archive_dir: Path, exclude_date: str, short_numbers: list[str], long_numbers: list[str]
) -> tuple[int, int]:
    """
    Summerar ersättningsgrundande förseningar över alla arkiverade dagar
    FÖR EN GIVEN RUTT, utom exclude_date (dagens siffror kommer direkt
    från minnet istället, så en tidigare körning samma dag inte räknas
    dubbelt).
    """
    short_total = long_total = 0
    if not archive_dir.exists():
        return 0, 0
    for path in sorted(archive_dir.glob("*.html")):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem) or path.stem == exclude_date:
            continue
        s, l = count_compensated_delays(
            rows_from_archive_html(path.read_text(encoding="utf-8")), short_numbers, long_numbers
        )
        short_total += s
        long_total += l
    return short_total, long_total


def build_compensation_html(short_n: int, long_n: int) -> str:
    """Textrutan med den samlade ersättningen."""
    total_kr = short_n * SHORT_COMP_KR + long_n * LONG_COMP_KR
    total_str = f"{total_kr:,}".replace(",", " ")
    return f"""<div class="kr-box">
    <div class="kr-title">Mälardebt</div>
    <div class="kr-amount">{total_str} kr</div>
    <div class="kr-detail">Förrädare mot kronan!?</div>
  </div>
  <div class="welcome-banner"></div>"""


def run_daily_summary_for_route(route: dict, sig_a: str, sig_b: str, top_docs_dir: Path) -> None:
    now = datetime.now(SWEDEN_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    report_date = now.strftime("%Y-%m-%d")
    updated_time = now.strftime("%H:%M")

    station_a, station_b = route["station_a"], route["station_b"]
    train_numbers, long_numbers = route["train_numbers"], route["long_numbers"]
    route_label = f"{station_a} ↔ {station_b}"

    rows = collect_rows(sig_a, station_a, sig_b, station_b, train_numbers, day_start, day_end)
    long_rows = collect_rows(sig_a, station_a, sig_b, station_b, long_numbers, day_start, day_end)

    long_heading = f"Långdistanståg (över 150 km, gräns {LONG_DISTANCE_DELAY_THRESHOLD_MIN} min)"
    extra_sections = [(long_heading, long_rows)]

    show_delay_banner = any(
        r["cancelled"] or r["delay"] >= DELAY_THRESHOLD_MIN for r in rows
    ) or any(
        r["cancelled"] or r["delay"] >= LONG_DISTANCE_DELAY_THRESHOLD_MIN for r in long_rows
    )

    quote_text = get_daily_quote()

    docs_dir = Path(__file__).parent / route["docs_subdir"]
    archive_dir = docs_dir / "archive"
    docs_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(exist_ok=True)

    ensure_default_stylesheet(top_docs_dir)

    past_short, past_long = compensation_from_archive(
        archive_dir, exclude_date=report_date, short_numbers=train_numbers, long_numbers=long_numbers
    )
    today_short, today_long = count_compensated_delays(rows + long_rows, train_numbers, long_numbers)
    comp_short, comp_long = past_short + today_short, past_long + today_long
    print(
        f"DEBUG: [{route['id']}] ersättning - historik {past_short} kort + {past_long} lång, "
        f"idag {today_short} kort + {today_long} lång, totalt "
        f"{comp_short * SHORT_COMP_KR + comp_long * LONG_COMP_KR} kr"
    )
    compensation_html = build_compensation_html(comp_short, comp_long)

    today_nav = '<p class="nav-links"><a href="archive/index.html">Se historik</a></p>'
    archive_nav = (
        '<p class="nav-links"><a href="../index.html">&larr; Tillbaka till idag</a> '
        '&middot; <a href="index.html">Alla dagar</a></p>'
    )

    # Dagens sida - det du ser som standard på rutten.
    (docs_dir / "index.html").write_text(
        build_html_report(
            rows, report_date, css_href="style.css" if route["docs_subdir"] == "docs" else "../style.css",
            nav_html=today_nav, route_label=route_label, body_class=route["css_class"],
            other_route_href=route["other_route_href"], other_route_label=route["other_route_label"],
            extra_sections=extra_sections, quote_text=quote_text,
            show_delay_banner=show_delay_banner, compensation_html=compensation_html,
            updated_time=updated_time,
        ),
        encoding="utf-8",
    )

    # Samma rapport arkiverad under sitt datum, för historik.
    archive_css_href = "../style.css" if route["docs_subdir"] == "docs" else "../../style.css"
    (archive_dir / f"{report_date}.html").write_text(
        build_html_report(
            rows, report_date, css_href=archive_css_href, nav_html=archive_nav,
            route_label=route_label, body_class=route["css_class"],
            other_route_href=route["other_route_href_from_archive"], other_route_label=route["other_route_label"],
            extra_sections=extra_sections, quote_text=quote_text,
            show_delay_banner=show_delay_banner, updated_time=updated_time,
        ),
        encoding="utf-8",
    )

    back_href = "../index.html" if route["docs_subdir"] == "docs" else "../index.html"
    (archive_dir / "index.html").write_text(build_archive_index(archive_dir, back_href), encoding="utf-8")

    avvikande, ok_count = rows_to_pushover_lines(rows)
    long_avvikande, long_ok_count = rows_to_pushover_lines(long_rows)

    parts = []
    if avvikande:
        parts.append("\n".join(avvikande) + f"\n({ok_count} övriga i tid)")
    else:
        parts.append(f"Inga avvikelser. {ok_count} tåg i tid.")

    parts.append(f"--- {long_heading} ---")
    if long_avvikande:
        parts.append("\n".join(long_avvikande) + f"\n({long_ok_count} övriga i tid)")
    else:
        parts.append(f"Inga avvikelser. {long_ok_count} tåg i tid.")

    message = "\n\n".join(parts)
    send_pushover(f"Dagens tågsammanfattning: {route_label} ({report_date})", message)


def run_daily_summary() -> None:
    top_docs_dir = Path(__file__).parent / "docs"
    for route in ROUTES:
        sig_a = find_location_signature(route["station_a"])
        sig_b = find_location_signature(route["station_b"])
        run_daily_summary_for_route(route, sig_a, sig_b, top_docs_dir)


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

    mode = os.environ.get("MODE", "live").lower()
    if mode == "daily_summary":
        run_daily_summary()
    else:
        run_live()


if __name__ == "__main__":
    main()
