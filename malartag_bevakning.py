#!/usr/bin/env python3
"""
Bevakar tågtrafiken (Mälartåg) mellan Södertälje Syd och Eskilstuna
och skickar en Pushover-notis vid förseningar eller inställda tåg.

Datakälla: Trafiklab ResRobot Timetables API v2.1 (https://www.trafiklab.se)
Notiser:   Pushover (https://pushover.net)

Körs lämpligen var 10-15:e minut via cron, Task Scheduler eller
GitHub Actions (se README.md för instruktioner).
"""

import json
import os
import sys
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


def get_full_journey_stop_names(dep: dict) -> list[str]:
    """
    Hämtar ett tågs fullständiga stopplista via ResRobots journeyDetail-API.
    Detta är det enda tillförlitliga sättet att veta vilka stationer ett tåg
    faktiskt passerar, eftersom 'direction'-fältet bara visar sluthållplatsen
    - och Mälartåg-linjen fortsätter längre än så åt båda hållen (t.ex. vidare
    mot Stockholm på ena sidan, vidare mot andra städer på andra sidan).
    """
    ref = dep.get("JourneyDetailRef", {}).get("ref")
    if not ref:
        return []

    if ref.startswith("http"):
        # I vissa svar är ref redan en fullständig URL.
        url = ref
        params = {"accessId": RESROBOT_API_KEY}
    else:
        # Vanligast: ref är en kort kod som ska skickas som 'id' till
        # journeyDetail-ändpunkten (inte 'ref', trots fältnamnet).
        url = f"{RESROBOT_BASE}/journeyDetail"
        params = {"id": ref, "format": "json", "accessId": RESROBOT_API_KEY}

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        # Krascha inte hela körningen om ett enskilt tåg strular - hoppa
        # bara över det och logga vad som hände, så vi kan felsöka i lugn
        # och ro utan att missa bevakning av alla andra tåg.
        print(f"VARNING: kunde inte hämta stopplista för {dep.get('name')}: {e}")
        return []

    stops = data.get("Stops", {}).get("Stop", [])
    return [s.get("name", "") for s in stops]


def get_departures(station_id: str, other_station_name: str) -> list[dict]:
    """
    Hämtar realtidsavgångar från en station och filtrerar fram bara de
    Mälartåg som verkligen passerar även den andra stationen, genom att
    kontrollera hela tågets stopplista (inte bara slutdestinationen).
    """
    resp = requests.get(
        f"{RESROBOT_BASE}/departureBoard",
        params={
            "id": station_id,
            "format": "json",
            "accessId": RESROBOT_API_KEY,
            "duration": 180,  # kolla avgångar 3h framåt
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    departures = data.get("Departure", [])

    relevant = []
    for dep in departures:
        product = dep.get("Product", [{}])[0]
        operator = product.get("operator", "")
        name = product.get("name", "")
        is_malartag = "mälartåg" in operator.lower() or "mälartåg" in name.lower()

        print(
            f"DEBUG: sett avgång {dep.get('name')} (operator='{operator}', "
            f"produktnamn='{name}') - Mälartåg: {is_malartag}"
        )

        if not is_malartag:
            continue

        stop_names = get_full_journey_stop_names(dep)
        if any(other_station_name.lower() in s.lower() for s in stop_names):
            relevant.append(dep)

    return relevant


def compute_delay_minutes(dep: dict) -> int:
    """Räknar ut förseningen i minuter genom att jämföra planerad tid med realtid."""
    if not dep.get("rtTime"):
        return 0  # ingen realtidsdata = ingen känd försening

    from datetime import datetime

    planned = datetime.strptime(f"{dep['date']} {dep['time']}", "%Y-%m-%d %H:%M:%S")
    real = datetime.strptime(f"{dep.get('rtDate', dep['date'])} {dep['rtTime']}", "%Y-%m-%d %H:%M:%S")
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


def send_debug_notification(departures_a: list[dict], departures_b: list[dict]) -> None:
    """
    Skickar en Pushover-notis som listar alla Mälartåg som just nu hittats
    trafikera sträckan Södertälje Syd <-> Eskilstuna C, oavsett försening.
    Används för att verifiera att filtreringslogiken fungerar som tänkt.
    """
    lines = []

    for from_name, deps in [(STATION_A, departures_a), (STATION_B, departures_b)]:
        for dep in deps:
            delay = compute_delay_minutes(dep)
            status = "INSTÄLLT" if dep.get("cancelled") else (
                f"{delay} min sen" if delay > 0 else "i tid"
            )
            lines.append(f"{dep.get('name')} från {from_name} kl {dep.get('time')} – {status}")

    if not lines:
        message = (
            f"Inga Mälartåg hittades mellan {STATION_A} och {STATION_B} "
            "inom sökfönstret (3h framåt)."
        )
    else:
        message = "\n".join(lines)

    send_pushover("Debug: hittade tåg", message)


def check_and_notify(departures: list[dict], state: dict, from_name: str) -> dict:
    for dep in departures:
        train_id = f"{dep.get('name')}_{dep.get('date')}_{dep.get('time')}"
        cancelled = dep.get("cancelled", False)
        delay = compute_delay_minutes(dep)

        previous = state.get(train_id)

        if cancelled and previous != "cancelled":
            send_pushover(
                "Tåg inställt",
                f"{dep.get('name')} från {from_name} kl {dep['time']} är INSTÄLLT.",
            )
            state[train_id] = "cancelled"

        elif delay >= DELAY_THRESHOLD_MIN and previous != delay:
            send_pushover(
                "Tågförsening",
                f"{dep.get('name')} från {from_name} kl {dep['time']} "
                f"är försenat {delay} minuter.",
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

    departures_from_a = get_departures(station_a_id, other_station_name=STATION_B)
    departures_from_b = get_departures(station_b_id, other_station_name=STATION_A)

    if os.environ.get("DEBUG_NOTIFY", "false").lower() == "true":
        send_debug_notification(departures_from_a, departures_from_b)

    state = check_and_notify(departures_from_a, state, from_name=STATION_A)
    state = check_and_notify(departures_from_b, state, from_name=STATION_B)

    save_state(state)


if __name__ == "__main__":
    main()
