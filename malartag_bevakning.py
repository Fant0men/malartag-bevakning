#!/usr/bin/env python3
"""
Bevakar tågtrafiken (Mälartåg) mellan Södertälje Syd och Eskilstuna
och skickar en Pushover-notis vid förseningar eller inställda tåg.

Datakälla: Trafiklab ResRobot Timetables API v2.1 (https://www.trafiklab.se),
           arrivalBoard-ändpunkten - och ENDAST den.

Varför bara arrivalBoard och inte departureBoard:
Ett tåg som är på väg mot en station syns i dess arrivalBoard hela vägen
från att resan bokas upp i tidtabellen tills det faktiskt anländer -
oavsett om tåget själv redan avgått från sin ursprungsstation eller inte.
Ett tidigare försök att kombinera departureBoard (för kommande avgångar)
med arrivalBoard (för pågående resor) blev onödigt krångligt och hade
fortfarande luckor. Genom att bara fråga "vad är på väg hit" vid båda
våra stationer täcks allt in med en enda, enklare frågetyp.

Filtreringslogik:
- Operatör/produktnamn måste innehålla "Mälartåg".
- Mälartåg kör FLERA olika linjer. Södertälje Syd trafikeras av både
  linjen genom Eskilstuna (Örebro/Arboga <-> Uppsala) OCH en annan linje
  mot Nyköping/Norrköping. Eskilstuna C trafikeras bara av förstnämnda
  linjen. Därför: vid Södertälje Syd utesluts tåg vars ursprung pekar
  mot Nyköping/Norrköping-hållet. Vid Eskilstuna C behövs ingen sådan
  extra filtrering.

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
# KONFIGURATION
# --------------------------------------------------------------------------
RESROBOT_API_KEY = os.environ.get("RESROBOT_API_KEY", "DIN_RESROBOT_NYCKEL")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "DIN_PUSHOVER_APP_TOKEN")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "DIN_PUSHOVER_USER_KEY")

STATION_A = "Södertälje Syd"
STATION_B = "Eskilstuna C"

# Ord i ursprungsfältet som identifierar FEL Mälartåg-linje vid Södertälje
# Syd (den mot Nyköping/Norrköping, som inte går via Eskilstuna).
FEL_LINJE_ORD = ["nyköping", "norrköping", "vagnhärad", "trosa", "skavsta"]

DELAY_THRESHOLD_MIN = 20

STATE_FILE = Path(__file__).parent / "state.json"
RESROBOT_BASE = "https://api.resrobot.se/v2.1"


def find_station_id(name: str) -> str:
    resp = requests.get(
        f"{RESROBOT_BASE}/location.name",
        params={"input": name, "format": "json", "accessId": RESROBOT_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    for entry in data.get("stopLocationOrCoordLocation", []):
        stop = entry.get("StopLocation")
        if stop:
            return stop["extId"]
    raise RuntimeError(f"Kunde inte hitta någon station som matchar '{name}'")


def is_malartag(product: dict) -> bool:
    operator = product.get("operator", "")
    name = product.get("name", "")
    return "mälartåg" in operator.lower() or "mälartåg" in name.lower()


def is_ratt_linje(origin: str, station_name: str) -> bool:
    if station_name != STATION_A:
        return True
    origin_lower = origin.lower()
    return not any(ord_ in origin_lower for ord_ in FEL_LINJE_ORD)


def get_arrivals(station_id: str, station_name: str) -> list[dict]:
    """Hämtar och filtrerar ankommande Mälartåg på rätt linje till en station."""
    resp = requests.get(
        f"{RESROBOT_BASE}/arrivalBoard",
        params={
            "id": station_id,
            "format": "json",
            "accessId": RESROBOT_API_KEY,
            "duration": 180,
        },
        timeout=15,
    )
    resp.raise_for_status()
    arrivals = resp.json().get("Arrival", [])

    relevant = []
    for arr in arrivals:
        product = arr.get("Product", [{}])[0]
        origin = arr.get("origin", "")
        godkant = is_malartag(product) and is_ratt_linje(origin, station_name)

        if station_name == STATION_A:
            print(
                f"DEBUG: {arr.get('name')} till {station_name}, ursprung='{origin}' "
                f"- godkänt: {godkant}"
            )

        if godkant:
            arr["_station_name"] = station_name
            relevant.append(arr)

    return relevant


def compute_delay_minutes(arr: dict) -> int:
    if not arr.get("rtTime"):
        return 0
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
        data={"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": title, "message": message},
        timeout=15,
    )


def send_debug_notification(all_arrivals: list[dict]) -> None:
    avvikande = []
    i_tid_count = 0

    for arr in all_arrivals:
        delay = compute_delay_minutes(arr)
        if arr.get("cancelled"):
            avvikande.append(
                f"{arr.get('name')} till {arr['_station_name']} kl {arr.get('time')} – INSTÄLLT"
            )
        elif delay > 0:
            avvikande.append(
                f"{arr.get('name')} till {arr['_station_name']} kl {arr.get('time')} "
                f"– {delay} min sen"
            )
        else:
            i_tid_count += 1

    if not all_arrivals:
        message = f"Inga Mälartåg (rätt linje) hittades på väg mot {STATION_A} eller {STATION_B}."
    elif avvikande:
        message = "\n".join(avvikande) + f"\n\n({i_tid_count} övriga tåg i tid)"
    else:
        message = f"Alla {i_tid_count} hittade tåg är i tid."

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
                f"{arr.get('name')} till {station_name} (skulle anlänt {arr['time']}) är INSTÄLLT.",
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
        name for name, val in [
            ("RESROBOT_API_KEY", RESROBOT_API_KEY),
            ("PUSHOVER_TOKEN", PUSHOVER_TOKEN),
            ("PUSHOVER_USER", PUSHOVER_USER),
        ] if val.startswith("DIN_")
    ]
    if missing:
        sys.exit("Saknar konfiguration för: " + ", ".join(missing))

    station_a_id = find_station_id(STATION_A)
    station_b_id = find_station_id(STATION_B)

    state = load_state()

    arrivals_a = get_arrivals(station_a_id, STATION_A)
    arrivals_b = get_arrivals(station_b_id, STATION_B)
    all_arrivals = arrivals_a + arrivals_b

    if os.environ.get("DEBUG_NOTIFY", "false").lower() == "true":
        send_debug_notification(all_arrivals)

    state = check_and_notify(all_arrivals, state)
    save_state(state)


if __name__ == "__main__":
    main()
