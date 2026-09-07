#!/usr/bin/env python3
"""
Bevakar tågtrafiken (Mälartåg) mellan Södertälje Syd och Eskilstuna
och skickar en Pushover-notis vid förseningar eller inställda tåg.

Datakälla: Trafiklab ResRobot Timetables API v2.1 (https://www.trafiklab.se),
           departureBoard-ändpunkten.

Filtreringslogik:
- Operatör/produktnamn måste innehålla "Mälartåg".
- Mälartåg kör FLERA olika linjer. Södertälje Syd trafikeras av både
  linjen genom Eskilstuna (Örebro/Arboga <-> Uppsala) OCH en helt annan
  linje mot Nyköping/Norrköping. Eskilstuna C trafikeras bara av den
  förstnämnda linjen. Därför: vid Södertälje Syd utesluts avgångar vars
  riktning pekar mot Nyköping/Norrköping-hållet. Vid Eskilstuna C behövs
  ingen sådan extra filtrering.

  (Tidigare försök att bygga en "giltig tågnummer-lista" via ResRobots
  reseplanerare (trip) fungerade INTE, eftersom varje enskild avgång får
  ett unikt löpnummer - det finns inget fast tågnummer per linje att
  matcha mot över tid.)

Notiser: Pushover (https://pushover.net)

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

# Ord i riktningsfältet som identifierar FEL Mälartåg-linje vid Södertälje
# Syd (den mot Nyköping/Norrköping, som inte går via Eskilstuna).
FEL_LINJE_ORD = ["nyköping", "norrköping", "vagnhärad", "trosa", "skavsta"]

# Hur många minuters försening som ska trigga en notis.
DELAY_THRESHOLD_MIN = 1

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


def is_ratt_linje(direction: str, station_name: str) -> bool:
    """
    Kollar att avgången/ankomsten hör till linjen genom Eskilstuna, inte
    Mälartågs andra linje mot Nyköping/Norrköping. Den kontrollen behövs
    bara vid Södertälje Syd, som delas mellan de två linjerna.
    """
    if station_name != STATION_A:
        return True  # Eskilstuna C trafikeras bara av rätt linje

    direction_lower = direction.lower()
    return not any(ord_ in direction_lower for ord_ in FEL_LINJE_ORD)


def get_departures(
    station_id: str,
    station_name: str,
    search_date: str | None = None,
    search_time: str | None = None,
) -> list[dict]:
    """
    Hämtar avgångar från en station och filtrerar fram Mälartåg som hör
    till rätt linje. Om search_date/search_time anges hämtas istället
    historiska avgångar - praktiskt för att verifiera att förseningar
    fångas upp korrekt, genom att kolla tåg som redan hunnit avgå/anlända.
    """
    params = {
        "id": station_id,
        "format": "json",
        "accessId": RESROBOT_API_KEY,
        "duration": 180,
    }
    if search_date:
        params["date"] = search_date
    if search_time:
        params["time"] = search_time

    resp = requests.get(f"{RESROBOT_BASE}/departureBoard", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    departures = data.get("Departure", [])

    relevant = []
    for dep in departures:
        product = dep.get("Product", [{}])[0]
        direction = dep.get("direction", "")
        malartag = is_malartag(product)
        ratt_linje = is_ratt_linje(direction, station_name)

        print(
            f"DEBUG: sett avgång {dep.get('name')} från {station_name} "
            f"mot '{direction}' (operator='{product.get('operator', '')}', "
            f"produktnamn='{product.get('name', '')}') - "
            f"Mälartåg: {malartag}, rätt linje: {ratt_linje}"
        )

        if not (malartag and ratt_linje):
            continue

        dep["_station_name"] = station_name
        relevant.append(dep)

    return relevant


def compute_delay_minutes(dep: dict) -> int:
    """Räknar ut förseningen i minuter genom att jämföra planerad tid med realtid."""
    if not dep.get("rtTime"):
        return 0  # ingen realtidsdata = ingen känd försening

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


def send_debug_notification(all_departures: list[dict]) -> None:
    lines = []
    for dep in all_departures:
        delay = compute_delay_minutes(dep)
        status = "INSTÄLLT" if dep.get("cancelled") else (
            f"{delay} min sen" if delay > 0 else "i tid"
        )
        lines.append(
            f"{dep.get('name')} från {dep['_station_name']} kl {dep.get('time')} "
            f"mot {dep.get('direction')} – {status}"
        )

    if not lines:
        message = (
            f"Inga Mälartåg (rätt linje) hittades vid {STATION_A} eller {STATION_B}."
        )
    else:
        message = "\n".join(lines)

    send_pushover("Debug: hittade tåg", message)


def cross_check_same_trains(departures_a: list[dict], departures_b: list[dict]) -> None:
    """
    Matchar ihop samma tågnummer i de två stationernas avgångslistor (ett
    och samma tåg passerar ju båda stationerna på sin resa) och loggar en
    jämförelse av förseningen vid respektive station. Detta är en oberoende
    dubbelkontroll av att rtTime-jämförelsen faktiskt fungerar korrekt -
    om ett tåg är X minuter sent vid Södertälje Syd, bör det (ungefär)
    vara i samma härad vid Eskilstuna C också, inte plötsligt "i tid".
    """
    by_name_a = {dep["name"]: dep for dep in departures_a}
    by_name_b = {dep["name"]: dep for dep in departures_b}

    common_names = set(by_name_a) & set(by_name_b)
    if not common_names:
        print("DEBUG: inga tåg hittades i båda stationernas listor samtidigt att jämföra.")
        return

    for name in sorted(common_names):
        dep_a = by_name_a[name]
        dep_b = by_name_b[name]
        delay_a = compute_delay_minutes(dep_a)
        delay_b = compute_delay_minutes(dep_b)
        print(
            f"DEBUG: korskoll {name} - vid {STATION_A} kl {dep_a.get('time')}: "
            f"{delay_a} min sen | vid {STATION_B} kl {dep_b.get('time')}: {delay_b} min sen"
        )


def check_and_notify(departures: list[dict], state: dict) -> dict:
    for dep in departures:
        station_name = dep["_station_name"]
        train_id = f"{dep.get('name')}_{station_name}_{dep.get('date')}_{dep.get('time')}"
        cancelled = dep.get("cancelled", False)
        delay = compute_delay_minutes(dep)

        previous = state.get(train_id)

        if cancelled and previous != "cancelled":
            send_pushover(
                "Tåg inställt",
                f"{dep.get('name')} från {station_name} kl {dep['time']} är INSTÄLLT.",
            )
            state[train_id] = "cancelled"

        elif delay >= DELAY_THRESHOLD_MIN and previous != delay:
            send_pushover(
                "Tågförsening",
                f"{dep.get('name')} från {station_name} kl {dep['time']} "
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

    history_hours = os.environ.get("HISTORY_HOURS_BACK", "").strip()
    search_date = search_time = None
    if history_hours:
        search_dt = datetime.now() - timedelta(hours=float(history_hours))
        search_date = search_dt.strftime("%Y-%m-%d")
        search_time = search_dt.strftime("%H:%M")
        print(f"HISTORIK-LÄGE: kollar avgångar från och med {search_date} {search_time}")

    departures_a = get_departures(station_a_id, STATION_A, search_date, search_time)
    departures_b = get_departures(station_b_id, STATION_B, search_date, search_time)
    all_departures = departures_a + departures_b

    cross_check_same_trains(departures_a, departures_b)

    if history_hours or os.environ.get("DEBUG_NOTIFY", "false").lower() == "true":
        send_debug_notification(all_departures)

    if not history_hours:
        state = check_and_notify(all_departures, state)
        save_state(state)


if __name__ == "__main__":
    main()
