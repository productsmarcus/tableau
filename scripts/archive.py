#!/usr/bin/env python3
"""Archive des ouvertures La Gerbe pour le mode « Rejouer » du tableau de bord.

Lit les messages ntfy des 12 dernières heures (sujets laressourcerie et laressourcerie-dispo),
repère les moments d'activité des profils (choix de numéro, formulaire, réservation…),
et enregistre chacun dans replays/<id>.json, plafonné à 10 minutes.
Les heures d'ouverture ne sont pas supposées connues : on part de l'activité réelle.
"""
import json, os, re, sys, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TOPICS = "laressourcerie,laressourcerie-dispo"
URL = f"https://ntfy.sh/{TOPICS}/json?poll=1&since=12h"
OUT = os.path.join(os.path.dirname(__file__), "..", "replays")
PARIS = ZoneInfo("Europe/Paris")
RACE = {"choice", "form", "book", "free", "cancel", "relay", "relayround", "relaydone"}
START = {"choice", "form"}          # un moment d'activité doit contenir au moins un choix ou un formulaire
GAP = 5 * 60                        # au-delà de 5 min sans activité, c'est un autre moment
CAP = 10 * 60                       # plafond : 10 min après le premier événement
BEFORE, AFTER = 60, 60              # marge avant / après


def tag(msg):
    m = re.search(r"#lg ([^\n]+)", msg or "")
    if not m:
        return None
    o = {}
    for kv in m.group(1).split(" "):
        if "=" in kv:
            k, v = kv.split("=", 1)
            o[k] = v
    return o


def main():
    if os.environ.get("NTFY_FILE"):            # test hors ligne
        lines = open(os.environ["NTFY_FILE"], encoding="utf-8").read().splitlines()
    else:
        with urllib.request.urlopen(URL, timeout=60) as r:
            lines = r.read().decode("utf-8").splitlines()
    msgs = []
    for l in lines:
        try:
            m = json.loads(l)
        except Exception:
            continue
        if m.get("event") == "message":
            msgs.append({k: m.get(k) for k in ("id", "time", "topic", "title", "message")})
    msgs.sort(key=lambda m: m["time"])
    ev = [m for m in msgs if m["topic"] == "laressourcerie" and (tag(m["message"]) or {}).get("e") in RACE]

    # regroupement en moments d'activité
    groups, cur = [], []
    for m in ev:
        if cur and m["time"] - cur[-1]["time"] > GAP:
            groups.append(cur); cur = []
        cur.append(m)
    if cur:
        groups.append(cur)

    os.makedirs(OUT, exist_ok=True)
    now = datetime.now(timezone.utc).timestamp()
    added = 0
    for g in groups:
        if not any((tag(m["message"]) or {}).get("e") in START for m in g):
            continue
        first, last = g[0]["time"], g[-1]["time"]
        if now - last < GAP:          # encore en cours : on attendra le prochain passage
            continue
        start, end = first - BEFORE, min(first + CAP, last + AFTER)
        loc = datetime.fromtimestamp(first, PARIS)
        rid = loc.strftime("%Y-%m-%d_%H%M")
        path = os.path.join(OUT, rid + ".json")
        # suite : annulations (ou nouvelles réservations) plus tard dans les 12 h, pour les numéros réservés pendant le moment
        booked = {(t.get("d"), t.get("n")) for t in (tag(m["message"]) or {} for m in g) if t.get("e") == "book"}
        suite = [m for m in msgs if m["topic"] == "laressourcerie" and m["time"] > end
                 and (tag(m["message"]) or {}).get("e") in ("cancel", "book")
                 and ((tag(m["message"]) or {}).get("d"), (tag(m["message"]) or {}).get("n")) in booked]
        if os.path.exists(path):      # déjà enregistré : on complète seulement la suite
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            have = {m["id"] for m in old.get("suite", [])}
            more = [m for m in suite if m["id"] not in have]
            if more:
                old["suite"] = sorted(old.get("suite", []) + more, key=lambda m: m["time"])
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(old, f, ensure_ascii=False, separators=(",", ":"))
                added += 1
                print("suite complétée", rid, len(more))
            continue
        window = [m for m in msgs if start <= m["time"] <= end]
        before = [m for m in msgs if m["topic"] == "laressourcerie-dispo" and m["time"] < start]
        if before:                    # état des créneaux juste avant
            window.insert(0, before[-1])
        days = sorted({(tag(m["message"]) or {}).get("d") for m in g} - {None})
        n_book = sum(1 for m in g if (tag(m["message"]) or {}).get("e") == "book")
        rec = {"id": rid, "start": start, "end": end, "first": first,
               "label": loc.strftime("%a %d/%m %H:%M"), "days": days,
               "events": len(g), "books": n_book, "msgs": window, "suite": suite}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
        added += 1
        print("ajouté", rid, len(window), "messages")

    # index (les plus récents d'abord)
    idx = []
    for name in sorted(os.listdir(OUT), reverse=True):
        if not name.endswith(".json") or name == "index.json":
            continue
        with open(os.path.join(OUT, name), encoding="utf-8") as f:
            r = json.load(f)
        idx.append({k: r[k] for k in ("id", "start", "end", "first", "label", "days", "events", "books")})
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=0)
    print(added, "nouveau(x) moment(s) ;", len(idx), "au total")


if __name__ == "__main__":
    sys.exit(main())
