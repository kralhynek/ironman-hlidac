#!/usr/bin/env python3
"""
Hlídač registrace IRONMAN 70.3 Hradec Králové 2027.

Slotmarket (registrační systém organizátora) má JSON API:
  https://slotmarket.pl/api/event/<id>
Ročník 2027 už v systému existuje jako akce ID 835
("IRONMAN 70.3 HRADEC KRÁLOVÉ 2027", 15. 8. 2027), zatím neaktivní,
bez ceny a s 0 sloty. Bot ji kontroluje každých ~10 minut a spustí POPLACH,
jakmile se objeví cena, volné sloty, nebo se akce zaktivní.

Pojistky:
  - kdyby organizátor ID 835 smazal, bot projde nová ID a hledá akci
    s "IRONMAN 70.3" + "HRADEC" + "2027" v názvu,
  - když API nejde přečíst ~2 hodiny v kuse, pošle varování,
  - 1× týdně tichá zpráva, že bot žije.

Notifikace: ntfy.sh (push s nejvyšší prioritou) + hovor přes CallMeBot.
"""
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).with_name("state.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) IronmanWatcher/2.0",
      "Accept": "application/json"}

API = "https://slotmarket.pl/api/event/{id}"
EVENT_PAGE = "https://slotmarket.pl/event/details/{id}/{slug}"
TARGET_ID = 835                 # IRONMAN 70.3 HRADEC KRÁLOVÉ 2027
SCAN_FROM = 842                 # první volné ID v době psaní (2026-10-17)
SCAN_WINDOW = 15
NAME_RE = re.compile(r"IRONMAN\s*70\.3.*HRADEC.*2027", re.I)

URGENT_REPEAT_RUNS = 6          # po otevření opakuj poplach 6× (~1 h)
FAIL_WARN_RUNS = 12             # varování po ~2 h nedostupného API

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
CALLMEBOT_USER = os.environ.get("CALLMEBOT_USER", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# ---------------------------------------------------------------- notifikace
def ntfy(title, msg, priority=5, click=None, tags="rotating_light"):
    if DRY_RUN or not NTFY_TOPIC:
        print(f"[ntfy p{priority}] {title}: {msg}")
        return
    headers = {"Title": title.encode("utf-8"), "Priority": str(priority), "Tags": tags}
    if click:
        headers["Click"] = click
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                      headers=headers, timeout=20)
    except Exception as e:
        print("ntfy chyba:", e)


def call():
    if DRY_RUN or not CALLMEBOT_USER:
        print("[call]")
        return
    # CallMeBot čte URL doslova: "+" musí zůstat "+", mezery jako %20
    text = "IRONMAN HRADEC KRALOVE ALERT. CHECK NTFY NOW."
    q = ("source=web&user=" + urllib.parse.quote(CALLMEBOT_USER, safe="+@")
         + "&text=" + urllib.parse.quote(text) + "&lang=en-US-Standard-B")
    try:
        r = requests.get(f"http://api.callmebot.com/start.php?{q}", timeout=120)
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "head"]):
            t.decompose()
        print("callmebot:", r.status_code, " ".join(soup.get_text(" ").split())[:600])
    except Exception as e:
        print("callmebot chyba:", e)


def alarm(title, msg, url=None):
    """Maximální poplach: 3× urgentní push + hovor."""
    for i in range(3):
        ntfy(title, msg, priority=5, click=url)
        if i < 2:
            time.sleep(20)
    call()


# ---------------------------------------------------------------- API
def get_event(event_id):
    """Vrátí dict akce, None když neexistuje (404). Výjimka při jiné chybě."""
    r = requests.get(API.format(id=event_id), headers=UA, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()["data"]["event"]


def summarize(e):
    comps = e.get("competitions") or []
    slots = sum(int(c.get("availableSlots") or 0) for c in comps)
    prices = [c["price"] for c in comps if c.get("price")]
    is_open = (bool(e.get("isAvailable")) or slots > 0 or bool(prices)
               or any(c.get("isAvailable") for c in comps))
    return {"active": bool(e.get("active")), "open": is_open,
            "slots": slots, "prices": prices}


def page_url(e):
    return EVENT_PAGE.format(id=e["id"], slug=e.get("slug", ""))


# ---------------------------------------------------------------- logika
def find_target(state):
    """ID 835 nebo náhradní akce 2027, pokud by ji organizátor přečísloval."""
    tid = state.get("target_id", TARGET_ID)
    e = get_event(tid)
    if e and NAME_RE.search(e.get("name", "")):
        return e
    start = state.get("max_seen_id", SCAN_FROM - 1) + 1
    for i in range(start, start + SCAN_WINDOW):
        try:
            cand = get_event(i)
        except Exception as ex:
            print(f"ID {i}: chyba {ex}")
            continue
        if not cand:
            continue
        state["max_seen_id"] = max(state.get("max_seen_id", 0), i)
        print(f"ID {i}: {cand.get('name')}")
        if NAME_RE.search(cand.get("name", "")):
            state["target_id"] = i
            ntfy("Ironman Hradec 2027 má nové ID",
                 f"Hlídám teď akci {i}: {cand.get('name')}", priority=4,
                 click=page_url(cand), tags="warning")
            return cand
    return None


def check(state):
    e = find_target(state)
    if not e:
        print("Akce 2027 nenalezena")
        state["missing_runs"] = state.get("missing_runs", 0) + 1
        if state["missing_runs"] == FAIL_WARN_RUNS:
            ntfy("Hlídač Ironman: akce zmizela",
                 "Akce 2027 na slotmarketu už ~2 h není k nalezení. Zkontroluj ironmanczech.com.",
                 priority=4, tags="warning")
        return
    state["missing_runs"] = 0
    s = summarize(e)
    url = page_url(e)
    print(f"ID {e['id']} {e['name']}: active={s['active']} open={s['open']} "
          f"slots={s['slots']} prices={s['prices']}")

    if s["active"] and not state.get("active_alerted"):
        state["active_alerted"] = True
        if not s["open"]:
            alarm("IRONMAN HRADEC 2027 JE ZVEŘEJNĚN",
                  "Stránka závodu je na slotmarketu aktivní, registrace bude každou chvíli!", url)

    if s["open"] and not state.get("opened"):
        state["opened"] = True
        state["opened_at"] = datetime.now(timezone.utc).isoformat()
        state["urgent_left"] = URGENT_REPEAT_RUNS - 1
        alarm("REGISTRACE IRONMAN HRADEC OTEVŘENA",
              f"Startovné {', '.join(s['prices']) or ''} v prodeji, volných míst: {s['slots']}. Přihlas se HNED!",
              url)
    elif state.get("opened") and state.get("urgent_left", 0) > 0:
        state["urgent_left"] -= 1
        alarm("REGISTRACE IRONMAN HRADEC OTEVŘENA",
              "Pořád platí – přihlas se hned!", url)


def heartbeat(state):
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    if state.get("heartbeat") != week:
        state["heartbeat"] = week
        ntfy("Hlídač Ironman běží",
             f"Vše OK, registrace zatím zavřená. Stav: {state.get('last_status', '?')}",
             priority=2, tags="white_check_mark")


def main():
    if "--test-alarm" in sys.argv:
        alarm("TEST hlídače Ironman", "Tohle je zkušební poplach. Funguje to.")
        return
    state = load_state()
    try:
        check(state)
        state["fail_runs"] = 0
        state["last_status"] = "OK " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception as ex:
        print("CHYBA API:", ex)
        state["fail_runs"] = state.get("fail_runs", 0) + 1
        state["last_status"] = f"chyba: {ex}"[:200]
        if state["fail_runs"] == FAIL_WARN_RUNS:
            ntfy("Hlídač Ironman nevidí slotmarket",
                 f"Už ~2 h se nedaří načíst API. Poslední chyba: {ex}"[:400],
                 priority=4, tags="warning")
    heartbeat(state)
    save_state(state)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text() or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
