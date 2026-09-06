# Mälartåg-bevakning: Södertälje Syd ↔ Eskilstuna

Skickar en Pushover-notis till din mobil när ett Mälartåg på sträckan
är försenat (mer än 3 minuter, går att ändra) eller inställt.

## Filer

- `malartag_bevakning.py` – huvudskriptet
- `malartag_bevakning.yml` – GitHub Actions-workflow för automatisk körning i molnet
- `state.json` – skapas automatiskt av skriptet, håller koll på vad du redan blivit notifierad om

## 1. Skaffa nycklar

1. **ResRobot (Trafiklab):** Skapa konto på trafiklab.se → nytt projekt →
   lägg till "ResRobot Timetables v2.1" → kopiera API-nyckeln.
2. **Pushover:** Skapa konto på pushover.net → installera appen på mobilen
   (User Key visas på startsidan) → skapa en ny Application → kopiera API Token.

## 2. Alternativ A – Kör i molnet med GitHub Actions (rekommenderas)

Detta är "sätt och glöm"-alternativet. Ingen egen dator behöver vara på.

1. Skapa ett nytt (privat är helt okej) repo på GitHub.
2. Lägg `malartag_bevakning.py` i rotan av repot.
3. Skapa mappen `.github/workflows/` och lägg `malartag_bevakning.yml` där.
4. Gå till repots **Settings → Secrets and variables → Actions** och lägg till
   tre secrets: `RESROBOT_API_KEY`, `PUSHOVER_TOKEN`, `PUSHOVER_USER`.
5. Committa och pusha. Gå till fliken **Actions** i repot och kör workflowen
   manuellt en gång ("Run workflow") för att testa att allt fungerar.
6. Klart – nu körs den automatiskt var 15:e minut, gratis (GitHub ger 2000
   gratis Actions-minuter/månad för privata repon, den här jobben drar väldigt lite).

## 3. Alternativ B – Kör lokalt på din egen dator

Om du hellre vill köra det själv:

```bash
pip install requests
export RESROBOT_API_KEY="din_nyckel"
export PUSHOVER_TOKEN="din_token"
export PUSHOVER_USER="din_user_key"
python malartag_bevakning.py
```

Lägg sedan till en schemalagd körning:

- **Windows:** Task Scheduler → skapa en uppgift som kör kommandot ovan var 15:e minut.
- **Mac/Linux:** lägg till en rad i crontab (`crontab -e`):
  ```
  */15 * * * * cd /sökväg/till/mappen && python3 malartag_bevakning.py
  ```

Nackdelen med lokal körning är att datorn måste vara på och ansluten till
internet för att bevakningen ska fungera.

## 4. Justera känslighet

I `malartag_bevakning.py` kan du ändra:

- `DELAY_THRESHOLD_MIN` – hur många minuters försening som ska trigga notis (default: 20)
- `duration` i `get_departures()` – hur långt fram i tiden (minuter) skriptet kollar avgångar (default: 180 = 3 timmar)

## Felsökning

- Om skriptet inte hittar några avgångar: kontrollera att `STATION_A`/`STATION_B`
  i skriptet matchar ResRobots stationsnamn (skriv ut resultatet från
  `find_station_id()` för att felsöka).
- Om operatörsnamnet i ResRobots svar inte innehåller exakt "mälartåg" kan du
  behöva justera filtreringen i `get_departures()` – testa att skriva ut
  `product` för en avgång för att se exakt vad fältet innehåller.
