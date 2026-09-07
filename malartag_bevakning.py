#!/usr/bin/env python3
"""
Bevakar tågtrafiken (Mälartåg) mellan Södertälje Syd och Eskilstuna
och skickar en Pushover-notis vid förseningar eller inställda tåg.

Datakälla: Trafiklab ResRobot Timetables API v2.1 (https://www.trafiklab.se)

Metoden kombinerar två anrop för att lösa två problem samtidigt:

1. 'trip'-ändpunkten (originId/destId) används för att bygga en lista över
   vilka TÅGNUMMER som just nu faktiskt trafikerar sträckan Södertälje Syd
   <-> Eskilstuna C. Detta behövs eftersom Mälartåg kör FLERA olika linjer
   (bl.a. även Stockholm-Norrköping via Södertälje Syd som INTE går via
   Eskilstuna) - ett rent operatörsfilter skulle plocka upp fel tåg.

2. 'arrivalBoard'-ändpunkten vid varje station används sedan för att hitta
   ALLA ankommande tåg (både sådana som ännu inte avgått OCH sådana som
   redan är på väg men inte hunnit fram) och filtrera dem mot listan från
   steg 1. 'trip' ensamt missar pågående resor eftersom den bara söker
   framåt i tiden från och med nu.

Notiser: Pushover (https://pushover.net)

Körs lämpligen var 10-15:e minut via cron, Task Scheduler eller
GitHub Actions (se README.md för instruktioner).
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# KONFIGURATION – fyll i dina egna nycklar, antingen här direkt eller
# (rekommenderat) via miljövariabler så du slipper ha hemligheter i koden.
# --------------------------------------------------------------------------
RESROBOT_API_KEY = os.environ.get("RESROBOT_API_KEY", "DIN_RESROBOT_NYCKEL")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "DIN_PUSHOVER_APP_TOKEN")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "DIN_PUSHOVER_USER_KEY")

# Namnen som ResRobots stationssök ska matcha mot.
STATION_A = "Södertälje Syd"
STATION_B = "Eskilstuna C"

# Hur många minuters försening som ska trigga en notis.
DELAY_THRESHOLD_MIN = 20

# Hur många kommande direkta resor 'trip' hämtar per riktning, för att bygga
# listan över giltiga tågnummer för linjen. OBS: ResRobot verkar avvisa
# värden över 6 med "400 Bad Request" - håll detta på 6 eller lägre.
NUM_TRIPS_PER_RIKTNING = 6

# Hur långt fram i tiden (minuter) arrivalBoard ska kolla ankomster.
ARRIVAL_DURATION_MIN = 180

# Var vi sparar vilka förseningar vi redan har notifierat om,
# så du inte får samma notis varje gång skriptet körs.
STATE_FILE = Path(__file__).parent / "state.json"

RESROBOT_BASE = "https://api.resrobot.se/v2.1"


def find_station_id(name: str) -> str:
    """Slår upp ResRobots interna stations-id (extId) för ett stationsnamn."""
    resp = requests.get(
        f"{RESROBOT_BASE}/location.name",
        params={"input": name, "format": "json", "accessId": RESROBOT_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    stops = data.get("stopLocationOrCoordLocation", [])
    for entry in stops:
        stop = entry.get("StopLocation")
        if stop:
            return stop["extId"]
    raise RuntimeError(f"Kunde inte hitta någon station som matchar '{name}'")


def is_malartag(product: dict) -> bool:
    operator = product.get("operator", "")
    name = product.get("name", "")
    return "mälartåg" in operator.lower() or "mälartåg" in name.lower()


def get_valid_train_names(origin_id: str, dest_id: str) -> set[str]:
    """
    Frågar 'trip'-ändpunkten om direkta resor mellan de två stationerna och
    samlar ihop tågnumren (t.ex. 'Regional Tåg 919'). Detta blir vår lista
    över vilka tåg som verkligen hör till just den här linjen, så att vi
    senare kan filtrera bort tåg från andra Mälartåg-linjer (t.ex.
    Stockholm-Norrköping) som råkar dyka upp på samma stationer.
    """
    resp = requests.get(
        f"{RESROBOT_BASE}/trip",
        params={
            "originId": origin_id,
            "destId": dest_id,
            "format": "json",
            "accessId": RESROBOT_API_KEY,
            "numF": NUM_TRIPS_PER_RIKTNING,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    trips = data.get("Trip", [])

    valid_names = set()
    for trip in trips:
        legs = trip.get("LegList", {}).get("Leg", [])
        if len(legs) != 1:
            continue  # bara direkttåg räknas, inga byten
        leg = legs[0]
        product = leg.get("Product", [{}])[0]
        if is_malartag(product):
            valid_names.add(leg.get("name", ""))

    return valid_names


def get_arrivals(station_id: str, station_name: str, valid_names: set[str]) -> list[dict]:
    """
    Hämtar alla ankommande tåg till en station och behåller bara de som
    finns med i valid_names (dvs. bekräftat hör till vår linje). Detta
    fångar upp tåg oavsett om de redan avgått från sin startstation eller
    inte, eftersom vi tittar på ANKOMSTER hit - inte avgångar härifrån.
    """
    resp = requests.get(
        f"{RESROBOT_BASE}/arrivalBoard",
        params={
            "id": station_id,
            "format": "json",
            "accessId": RESROBOT_API_KEY,
            "duration": ARRIVAL_DURATION_MIN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    arrivals = data.get("Arrival", [])

    relevant = []
    for arr in arrivals:
        name = arr.get("name", "")
        product = arr.get("Product", [{}])[0]

        print(
            f"DEBUG: sett ankomst {name} till {station_name} "
            f"(operator='{product.get('operator', '')}', "
            f"produktnamn='{product.get('name', '')}') - "
            f"giltigt tågnummer: {name in valid_names}"
        )

        if name not in valid_names:
            continue

        arr["_station_name"] = station_name
        relevant.append(arr)

    return relevant


def compute_delay_minutes(arr: dict) -> int:
    """Räknar ut förseningen i minuter genom att jämföra planerad tid med realtid."""
    if not arr.get("rtTime"):
        return 0  # ingen realtidsdata = ingen känd försening

    planned = datetime.strptime(f"{arr['date']} {arr['time']}", "%Y-%m-%d %H:%M:%S")
    real = datetime.strptime(f"{arr.get('rtDate', arr['date'])} {arr['rtTime']}", "%Y-%m-%d %H:%M:%S")
    return int((real - planned).total_seconds() // 60)


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
        data={
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message,
            "priority": 0,
        },
        timeout=15,
    )


def send_debug_notification(all_arrivals: list[dict]) -> None:
    """
    Skickar en Pushover-notis som listar alla Mälartåg som just nu hittats
    på väg mot Södertälje Syd eller Eskilstuna C, oavsett försening.
    """
    lines = []
    for arr in all_arrivals:
        delay = compute_delay_minutes(arr)
        status = "INSTÄLLT" if arr.get("cancelled") else (
            f"{delay} min sen" if delay > 0 else "i tid"
        )
        lines.append(
            f"{arr.get('name')} anländer {arr['_station_name']} kl {arr.get('time')} – {status}"
        )

    if not lines:
        message = (
            f"Inga Mälartåg hittades på väg mot {STATION_A} eller {STATION_B} "
            "just nu."
        )
    else:
        message = "\n".join(lines)

    send_pushover("Debug: hittade tåg", message)


def check_and_notify(arrivals: list[dict], state: dict) -> dict:
    for arr in arrivals:
        station_name = arr["_station_name"]
        train_id = f"{arr.get('name')}_{station_name}_{arr.get('date')}_{arr.get('time')}"
        cancelled = arr.get("cancelled", False)
        delay = compute_delay_minutes(arr)

        previous = state.get(train_id)

        if cancelled and previous != "cancelled":
            send_pushover(
                "Tåg inställt",
                f"{arr.get('name')} till {station_name} (skulle anlänt {arr['time']}) "
                "är INSTÄLLT.",
            )
            state[train_id] = "cancelled"

        elif delay >= DELAY_THRESHOLD_MIN and previous != delay:
            send_pushover(
                "Tågförsening",
                f"{arr.get('name')} till {station_name} är försenat {delay} minuter "
                f"(ny beräknad ankomst: {arr.get('rtTime', arr.get('time'))}).",
            )
            state[train_id] = delay

    return state


def main() -> None:
    missing = [
        name
        for name, val in [
            ("RESROBOT_API_KEY", RESROBOT_API_KEY),
            ("PUSHOVER_TOKEN", PUSHOVER_TOKEN),
            ("PUSHOVER_USER", PUSHOVER_USER),
        ]
        if val.startswith("DIN_")
    ]
    if missing:
        sys.exit(
            "Saknar konfiguration för: "
            + ", ".join(missing)
            + ". Sätt dem som miljövariabler eller fyll i direkt i skriptet."
        )

    station_a_id = find_station_id(STATION_A)
    station_b_id = find_station_id(STATION_B)

    state = load_state()

    # Bygg listan över giltiga tågnummer för linjen (i båda riktningar).
    valid_names = get_valid_train_names(station_a_id, station_b_id)
    valid_names |= get_valid_train_names(station_b_id, station_a_id)

    # Hämta ankomster till båda stationerna, filtrerat mot giltiga tågnummer.
    arrivals_a = get_arrivals(station_a_id, STATION_A, valid_names)
    arrivals_b = get_arrivals(station_b_id, STATION_B, valid_names)
    all_arrivals = arrivals_a + arrivals_b

    if os.environ.get("DEBUG_NOTIFY", "false").lower() == "true":
        send_debug_notification(all_arrivals)

    state = check_and_notify(all_arrivals, state)

    save_state(state)


if __name__ == "__main__":
    main()
