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

# De tågnummer du faktiskt bryr dig om.
TRAIN_NUMBERS = [
    "906", "10900", "910", "10902", "914", "10904", "918", "10906", "924",
    "928", "932", "936", "940", "946", "950", "2139", "20954", "10922",
    "20958", "2147", "964", "970", "976", "2159", "982", "988", "10901",
    "907", "10903", "911", "10905", "915", "10907", "919", "929", "933",
    "937", "941", "947", "10921", "951", "10923", "955", "10925", "959",
    "143", "965", "145", "971", "147", "977", "983", "989",
]

DELAY_THRESHOLD_MIN = 1

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
def run_daily_summary(sig_a: str, sig_b: str) -> None:
    now = datetime.now(SWEDEN_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    lines = []
    ok_count = 0
    cancelled_count = 0

    for sig, name in [(sig_a, STATION_A), (sig_b, STATION_B)]:
        anns = fetch_announcements(sig, TRAIN_NUMBERS, day_start, day_end)
        for a in sorted(anns, key=lambda x: x.get("AdvertisedTimeAtLocation", "")):
            delay = compute_delay_minutes(a)
            train_id = a.get("AdvertisedTrainIdent")
            planned_time = (a.get("AdvertisedTimeAtLocation") or "")[11:16]

            if a.get("Canceled"):
                lines.append(f"Tåg {train_id} till {name} kl {planned_time} – INSTÄLLT")
                cancelled_count += 1
            elif delay > 0:
                lines.append(f"Tåg {train_id} till {name} kl {planned_time} – {delay} min sen")
            else:
                ok_count += 1

    if not lines:
        message = f"Inga avvikelser idag. {ok_count} tåg i tid."
    else:
        message = "\n".join(lines) + f"\n\n({ok_count} övriga tåg i tid)"

    send_pushover(f"Dagens tågsammanfattning ({now.strftime('%Y-%m-%d')})", message)


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
