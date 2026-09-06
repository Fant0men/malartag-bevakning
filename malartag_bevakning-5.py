#!/usr/bin/env python3
"""
Bevakar tågtrafiken (Mälartåg) mellan Södertälje Syd och Eskilstuna
och skickar en Pushover-notis vid förseningar eller inställda tåg.

Datakälla: Trafiklab ResRobot Timetables API v2.1 (https://www.trafiklab.se),
           reseplanerar-ändpunkten 'trip' (inte departureBoard/journeyDetail
           som tidigare versioner av skriptet använde). Fördelen med 'trip'
           är att den direkt returnerar resor MELLAN de två angivna
           stationerna - ingen gissning om vilka tåg som "passerar" behövs,
           och förseningen som rapporteras gäller specifikt sträckan mellan
           just de här två stationerna (inte hela tågets väg innan/efter).
Notiser:   Pushover (https://pushover.net)

Körs lämpligen var 10-15:e minut via cron, Task Scheduler eller
GitHub Actions (se README.md för instruktioner).
"""

import json
import os
import sys
from datetime import datetime, timedelta
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

# Hur många minuter bakåt i tiden sökningen också ska täcka, så att tåg som
# redan avgått men fortfarande är på väg (inte hunnit fram än) också fångas
# upp - annars missar vi precis de pågående resorna vi vill bevaka mest.
SOK_BAKAT_MINUTER = 60

# Hur många kommande direkta resor som hämtas per riktning och körning.
NUM_TRIPS_PER_RIKTNING = 8

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


def get_direct_trips(origin_id: str, dest_id: str, origin_name: str, dest_name: str) -> list[dict]:
    """
    Hämtar kommande direkta (byte-fria) Mälartåg-resor mellan två stationer
    via ResRobots reseplanerare ('trip'-ändpunkten). Returnerar en lista med
    förenklade dict:ar som innehåller allt vi behöver för att avgöra
    försening och skicka notiser.
    """
    search_from = datetime.now() - timedelta(minutes=SOK_BAKAT_MINUTER)

    resp = requests.get(
        f"{RESROBOT_BASE}/trip",
        params={
            "originId": origin_id,
            "destId": dest_id,
            "format": "json",
            "accessId": RESROBOT_API_KEY,
            "numF": NUM_TRIPS_PER_RIKTNING,
            "date": search_from.strftime("%Y-%m-%d"),
            "time": search_from.strftime("%H:%M"),
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    trips = data.get("Trip", [])

    results = []
    for trip in trips:
        legs = trip.get("LegList", {}).get("Leg", [])
        if len(legs) != 1:
            # Hoppa över resor som kräver byte - vi vill bara ha direkttåg.
            continue

        leg = legs[0]
        product = leg.get("Product", [{}])[0]

        print(
            f"DEBUG: sett resa {leg.get('name')} {origin_name} -> {dest_name} "
            f"(operator='{product.get('operator', '')}', "
            f"produktnamn='{product.get('name', '')}') - "
            f"Mälartåg: {is_malartag(product)}"
        )

        if not is_malartag(product):
            continue

        origin = leg.get("Origin", {})
        destination = leg.get("Destination", {})

        results.append(
            {
                "name": leg.get("name", "okänt tåg"),
                "cancelled": leg.get("cancelled", False),
                "origin_name": origin_name,
                "dest_name": dest_name,
                "dep_date": origin.get("date"),
                "dep_time": origin.get("time"),
                "dep_rt_date": origin.get("rtDate"),
                "dep_rt_time": origin.get("rtTime"),
                "arr_date": destination.get("date"),
                "arr_time": destination.get("time"),
                "arr_rt_date": destination.get("rtDate"),
                "arr_rt_time": destination.get("rtTime"),
            }
        )

    return results


def _delay_minutes(date_str, time_str, rt_date_str, rt_time_str) -> int:
    """Räknar ut förseningen i minuter genom att jämföra planerad tid med realtid."""
    if not rt_time_str:
        return 0  # ingen realtidsdata = ingen känd försening

    planned = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    real = datetime.strptime(f"{rt_date_str or date_str} {rt_time_str}", "%Y-%m-%d %H:%M:%S")
    return int((real - planned).total_seconds() // 60)


def compute_segment_delay(trip: dict) -> int:
    """
    Förseningen som är relevant för just den här sträckan är förseningen vid
    ANKOMST till slutstationen (dvs. hur mycket senare du faktiskt kommer
    fram, jämfört med planerat) - inte förseningen tåget råkade ha innan det
    ens nådde din startstation.
    """
    return _delay_minutes(
        trip["arr_date"], trip["arr_time"], trip["arr_rt_date"], trip["arr_rt_time"]
    )


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


def send_debug_notification(all_trips: list[dict]) -> None:
    """
    Skickar en Pushover-notis som listar alla Mälartåg som just nu hittats
    trafikera sträckan Södertälje Syd <-> Eskilstuna C, oavsett försening.
    Används för att verifiera att filtreringslogiken fungerar som tänkt.
    """
    lines = []
    for trip in all_trips:
        delay = compute_segment_delay(trip)
        status = "INSTÄLLT" if trip["cancelled"] else (
            f"{delay} min sen vid ankomst" if delay > 0 else "i tid"
        )
        lines.append(
            f"{trip['name']}: {trip['origin_name']} kl {trip['dep_time']} "
            f"-> {trip['dest_name']} kl {trip['arr_time']} – {status}"
        )

    if not lines:
        message = (
            f"Inga direkta Mälartåg hittades mellan {STATION_A} och {STATION_B} "
            "just nu."
        )
    else:
        message = "\n".join(lines)

    send_pushover("Debug: hittade tåg", message)


def check_and_notify(trips: list[dict], state: dict) -> dict:
    for trip in trips:
        train_id = f"{trip['name']}_{trip['origin_name']}_{trip['dep_date']}_{trip['dep_time']}"
        delay = compute_segment_delay(trip)
        previous = state.get(train_id)

        if trip["cancelled"] and previous != "cancelled":
            send_pushover(
                "Tåg inställt",
                f"{trip['name']} från {trip['origin_name']} kl {trip['dep_time']} "
                f"(mot {trip['dest_name']}) är INSTÄLLT.",
            )
            state[train_id] = "cancelled"

        elif delay >= DELAY_THRESHOLD_MIN and previous != delay:
            send_pushover(
                "Tågförsening",
                f"{trip['name']} från {trip['origin_name']} kl {trip['dep_time']} "
                f"mot {trip['dest_name']} är försenat {delay} minuter vid ankomst.",
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

    trips_a_to_b = get_direct_trips(station_a_id, station_b_id, STATION_A, STATION_B)
    trips_b_to_a = get_direct_trips(station_b_id, station_a_id, STATION_B, STATION_A)
    all_trips = trips_a_to_b + trips_b_to_a

    if os.environ.get("DEBUG_NOTIFY", "false").lower() == "true":
        send_debug_notification(all_trips)

    state = check_and_notify(all_trips, state)

    save_state(state)


if __name__ == "__main__":
    main()
