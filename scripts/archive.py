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
    archive_moves(msgs)
    archive_feed(msgs)
    now = datetime.now(timezone.utc).timestamp()
    added = 0
    real = lambda m: (tag(m["message"]) or {}).get("dry") not in ("1", "true")
    kept = []
    for g in groups:
        if not any((tag(m["message"]) or {}).get("e") in START for m in g):
            continue
        if not any(real(m) for m in g):   # moment entièrement à blanc (essai) : pas de replay
            rid = datetime.fromtimestamp(g[0]["time"], PARIS).strftime("%Y-%m-%d_%H%M")
            path = os.path.join(OUT, rid + ".json")
            if os.path.exists(path):
                os.remove(path); print("essai à blanc retiré", rid)
            continue
        kept.append(g)
    for gi, g in enumerate(kept):
        nxt = kept[gi + 1][0]["time"] - BEFORE if gi + 1 < len(kept) else float("inf")
        first, last = g[0]["time"], g[-1]["time"]
        if now - last < GAP:          # encore en cours : on attendra le prochain passage
            continue
        start, end = first - BEFORE, min(first + CAP, last + AFTER)
        loc = datetime.fromtimestamp(first, PARIS)
        rid = loc.strftime("%Y-%m-%d_%H%M")
        path = os.path.join(OUT, rid + ".json")
        # suite : annulations (ou nouvelles réservations) plus tard dans les 12 h, pour les numéros réservés pendant le moment
        open_ = set()                 # numéros réservés pendant le moment et pas encore annulés à la fin
        for m in msgs:
            if m["time"] > end:
                break
            if m["time"] < first or m["topic"] != "laressourcerie":
                continue
            t = tag(m["message"]) or {}
            if t.get("e") == "book":
                open_.add((t.get("d"), t.get("n")))
            elif t.get("e") == "cancel":
                open_.discard((t.get("d"), t.get("n")))
        days_g = {(tag(m["message"]) or {}).get("d") for m in g} - {None}
        suite, seen_c = [], set()     # annulations réelles qui suivent, jusqu'au moment suivant : numéros réservés pendant le moment
        for m in msgs:                # et anciennes places remplacées (une seule par numéro)
            t = tag(m["message"]) or {}
            if m["time"] <= end or m["time"] >= nxt or m["topic"] != "laressourcerie" or t.get("e") != "cancel" or not real(m):
                continue
            k = (t.get("d"), t.get("n"))
            if (k in open_ or t.get("d") in days_g) and k not in seen_c:
                suite.append(m); seen_c.add(k); open_.discard(k)
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
        if not name.endswith(".json") or name == "index.json" or not os.path.isfile(os.path.join(OUT, name)):
            continue
        with open(os.path.join(OUT, name), encoding="utf-8") as f:
            r = json.load(f)
        idx.append({k: r[k] for k in ("id", "start", "end", "first", "label", "days", "events", "books")})
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=0)
    print(added, "nouveau(x) moment(s) ;", len(idx), "au total")


# ===== Mouvements des créneaux (relevés d'obs1), un fichier par séance : replays/moves/<date>.json =====
MVFROM = 1793538353463              # premier relevé correct (v22m.1)
MOVES = os.path.join(OUT, "moves")
MOVES_CAP = 1000                    # garde-fou par séance


def _arr(v):
    return v if isinstance(v, list) else ([] if v == "absent" else None)


def archive_moves(msgs):
    os.makedirs(MOVES, exist_ok=True)
    lastp = os.path.join(MOVES, "_last.json")
    last = None
    if os.path.exists(lastp):
        with open(lastp, encoding="utf-8") as f:
            last = json.load(f)
    rel = []
    for m in msgs:
        if m["topic"] != "laressourcerie-dispo":
            continue
        try:
            x = json.loads(m["message"])
        except Exception:
            continue
        if x.get("t", 0) < MVFROM or (last and x["t"] <= last["t"]):
            continue
        rel.append(x)
    rel.sort(key=lambda x: x["t"])
    new = {}
    prev = last
    for x in rel:
        if prev:
            pa = {z["d"]: (z.get("x") or {}) for z in prev.get("days", [])}
            pz = {z["d"]: z for z in prev.get("days", [])}
            for z in x.get("days", []):
                oa = pa.get(z["d"])
                if oa is None or not z.get("x"):
                    continue
                for h, nv in z["x"].items():
                    N, O = _arr(nv), _arr(oa.get(h))
                    if N is None or O is None:
                        continue
                    zp = pz.get(z["d"]) or {}
                    seen = None if (zp.get("va") or "v" not in zp) else (zp.get("v") or {}).get(h, [])
                    lib = [n for n in N if n not in O and (seen is None or n in seen)]   # une première apparition n'est pas un désistement
                    pri = [n for n in O if n not in N]
                    for k, ns in (("lib", lib), ("pri", pri)):
                        if ns:
                            new.setdefault(z["d"], []).append({"t": x["t"], "d": z["d"], "h": h, "k": k, "n": ns})
        # un relevé partiel (moins de jours que le précédent) ne remplace pas l'état de référence pour les jours absents
        if prev:
            keep = {z["d"]: z for z in prev.get("days", [])}
            for z in x.get("days", []):
                keep[z["d"]] = z
            today = datetime.fromtimestamp(x["t"] / 1000, PARIS).strftime("%Y-%m-%d")
            prev = {"t": x["t"], "days": sorted((z for z in keep.values() if z["d"] >= today), key=lambda z: z["d"])}
        else:
            prev = {"t": x["t"], "days": x.get("days", [])}
    changed = 0
    for d, mv in new.items():
        path = os.path.join(MOVES, d + ".json")
        old = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        keys = {(m["t"], m["h"], m["k"]) for m in old}
        mv = [m for m in mv if (m["t"], m["h"], m["k"]) not in keys]
        if mv:
            allm = sorted(old + mv, key=lambda m: m["t"])[-MOVES_CAP:]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(allm, f, ensure_ascii=False, separators=(",", ":"))
            changed += 1
            print("mouvements", d, "+", len(mv))
    if prev and (not last or prev["t"] != last["t"]):
        # on ne garde que les jours encore à venir dans l'état de référence
        with open(lastp, "w", encoding="utf-8") as f:
            json.dump(prev, f, ensure_ascii=False, separators=(",", ":"))
    return changed


# ===== Fil des messages des profils, un fichier par séance visée : replays/feed/<date>.json =====
FEED = os.path.join(OUT, "feed")
FEED_CAP = 2000                     # garde-fou par séance
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def archive_feed(msgs):
    os.makedirs(FEED, exist_ok=True)
    by = {}
    for m in msgs:
        if m["topic"] != "laressourcerie":
            continue
        d = (tag(m["message"]) or {}).get("d") or ""
        if not DATE_RE.match(d):        # message sans séance (alerte…) : rangé au jour où il a été envoyé
            d = datetime.fromtimestamp(m["time"], PARIS).strftime("%Y-%m-%d")
        by.setdefault(d, []).append({k: m[k] for k in ("id", "time", "title", "message")})
    for d, mv in by.items():
        path = os.path.join(FEED, d + ".json")
        old = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        have = {m["id"] for m in old}
        mv = [m for m in mv if m["id"] not in have]
        if mv:
            allm = sorted(old + mv, key=lambda m: m["time"])[-FEED_CAP:]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(allm, f, ensure_ascii=False, separators=(",", ":"))
            print("fil", d, "+", len(mv))
    # en glissant : une séance passée sort de l'archive
    today = datetime.now(PARIS).strftime("%Y-%m-%d")
    dates = []
    for name in sorted(os.listdir(FEED)):
        if not name.endswith(".json") or name == "index.json":
            continue
        d = name[:-5]
        if d < today:
            os.remove(os.path.join(FEED, name))
        else:
            dates.append(d)
    with open(os.path.join(FEED, "index.json"), "w", encoding="utf-8") as f:
        json.dump(dates, f)


if __name__ == "__main__":
    sys.exit(main())
