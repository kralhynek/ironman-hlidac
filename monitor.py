#!/usr/bin/env python3
"""
Hlídač registrace IRONMAN 70.3 Hradec Králové 2027.

Co dělá při každém spuštění (GitHub Actions, každých ~10 min):
  1) Projde nová ID akcí na slotmarket.pl (registrační systém organizátora
     Sport Evolution). Ročník 2026 měl ID 737 -> ročník 2027 dostane nové ID.
     Jakmile se objeví stránka "IRONMAN 70.3 HRADEC KRÁLOVÉ 2027" -> POPLACH.
  2) Pak tu stránku hlídá, a když zmizí "Registration closed" a objeví se
     cena startovného -> POPLACH "REGISTRACE OTEVŘENA" (opakuje se 1 hodinu).
  3) Hlídá i další stránky (ironman.com, ironmanczech.com) a při změně
     textu kolem registrace pošle běžnou notifikaci.

Notifikace: ntfy.sh (push s nejvyšší prioritou) + volitelně hovor přes
CallMeBot (Telegram call). Viz README.md.
"""
import hashlib
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
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) IronmanWatcher/1.0",
      "Accept-Language": "en,cs;q=0.8"}

SLOT_BASE = "https://slotmarket.pl/event/details/{id}/preview"
SLOT_WINDOW = 60          # kolik dalších ID zkusit za jedno spuštění
TARGET_RE = re.compile(r"IRONMAN\s*70\.3\s*HRADEC\s*KR[ÁA]LOV[ÉE]\s*2027", re.I)
CLOSED_RE = re.compile(r"Registration closed|Rejestracja zamkni|Registrace uzav", re.I)
FEE_RE = re.compile(r"Entry fee:\s*[\d\s.,]+\s*(EUR|CZK|PLN)", re.I)

# Další stránky – při změně řádků s klíčovými slovy přijde běžná notifikace.
WATCH_PAGES = [
    "https://www.ironman.com/races/im703-hradec-kralove",
    "https://www.ironmanczech.com/",
]
KEYWORD_RE = re.compile(r"2027|regist|Register|sold out|vyprod|Notify|Coming soon", re.I)

URGENT_REPEAT_RUNS = 6    # po otevření registrace opakuj poplach 6× (~1 h)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
CALLMEBOT_USER = os.environ.get("CALLMEBOT_USER", "").strip()  # např. @hynek
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


def call(text):
    if DRY_RUN or not CALLMEBOT_USER:
        print(f"[call] {text}")
        return
    # stejný formát jako ručně ověřený odkaz (anglický hlas, text bez diakritiky)
    text = "IRONMAN HRADEC KRALOVE ALERT. CHECK NTFY NOW."
    q = urllib.parse.urlencode({"source": "web", "user": CALLMEBOT_USER,
                                "text": text, "lang": "en-US-Standard-B"})
    try:
        r = requests.get(f"http://api.callmebot.com/start.php?{q}", timeout=120)
        print("callmebot:", r.status_code, r.text[:300])
    except Exception as e:
        print("callmebot chyba:", e)


def alarm(title, msg, url=None):
    """Maximální poplach: 3× urgentní push + hovor."""
    for i in range(3):
        ntfy(title, msg, priority=5, click=url)
        if i < 2:
            time.sleep(20)
    call(f"{title}. {msg}")


# ---------------------------------------------------------------- utility
def fetch_text(url):
    r = requests.get(url, headers=UA, timeout=30)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return re.sub(r"[ \t]+", " ", soup.get_text("\n"))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")


def is_open(text):
    return bool(FEE_RE.search(text)) and not CLOSED_RE.search(text)


# ---------------------------------------------------------------- kontroly
def scan_slotmarket(state):
    """Hledá novou stránku ročníku 2027."""
    if state.get("target_url"):
        return
    start = state.get("max_seen_id", 824) + 1
    for i in range(start, start + SLOT_WINDOW):
        url = SLOT_BASE.format(id=i)
        try:
            text = fetch_text(url)
        except Exception as e:
            print(f"ID {i}: chyba {e}")
            continue
        if not text or "Event name" not in text:
            continue
        state["max_seen_id"] = max(state.get("max_seen_id", 0), i)
        name = re.search(r"Event name:\s*\n*\s*([^\n]+)", text)
        print(f"ID {i}: {name.group(1).strip() if name else '?'}")
        if TARGET_RE.search(text):
            state["target_url"] = url
            state["found_at"] = datetime.now(timezone.utc).isoformat()
            opened = is_open(text)
            alarm("IRONMAN HRADEC 2027 JE NA SLOTMARKETU",
                  "Registrace je OTEVŘENÁ, běž se přihlásit!" if opened
                  else "Stránka závodu se objevila, registrace asi startuje brzy.",
                  url)
            if opened:
                state["opened"] = True
                state["urgent_left"] = URGENT_REPEAT_RUNS - 1
            return


def watch_target(state):
    """Hlídá stránku 2027, dokud se neotevře registrace."""
    url = state.get("target_url")
    if not url:
        return
    if state.get("opened"):
        if state.get("urgent_left", 0) > 0:
            state["urgent_left"] -= 1
            alarm("REGISTRACE IRONMAN HRADEC OTEVŘENA",
                  "Pořád platí – přihlas se hned!", url)
        return
    try:
        text = fetch_text(url)
    except Exception as e:
        print("target chyba:", e)
        return
    if text and is_open(text):
        state["opened"] = True
        state["urgent_left"] = URGENT_REPEAT_RUNS - 1
        alarm("REGISTRACE IRONMAN HRADEC OTEVŘENA",
              "Startovné je v prodeji, běž se přihlásit!", url)


def watch_pages(state):
    hashes = state.setdefault("page_hashes", {})
    for url in WATCH_PAGES:
        try:
            text = fetch_text(url)
        except Exception as e:
            print(f"{url}: chyba {e}")
            continue
        if not text:
            print(f"{url}: nedostupné")
            continue
        lines = sorted({l.strip() for l in text.splitlines()
                        if l.strip() and KEYWORD_RE.search(l) and len(l) < 300})
        h = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        old = hashes.get(url)
        hashes[url] = h
        if old and old != h:
            hot = [l for l in lines if "2027" in l]
            ntfy("Změna na stránce Ironman Hradec",
                 ("\n".join(hot[:5]) or "Změnil se text kolem registrace.") + f"\n{url}",
                 priority=4, click=url, tags="warning")


def heartbeat(state):
    """1× týdně tichá zpráva, že bot žije (a commit, aby GitHub cron neusnul)."""
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    if state.get("heartbeat") != week:
        state["heartbeat"] = week
        ntfy("Hlídač Ironman běží",
             f"Vše OK, zatím nic. Poslední prohledané ID: {state.get('max_seen_id')}",
             priority=2, tags="white_check_mark")


def main():
    if "--test-alarm" in sys.argv:
        alarm("TEST hlídače Ironman", "Tohle je zkušební poplach. Funguje to.")
        return
    state = load_state()
    scan_slotmarket(state)
    watch_target(state)
    watch_pages(state)
    heartbeat(state)
    state["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_state(state)


if __name__ == "__main__":
    main()
