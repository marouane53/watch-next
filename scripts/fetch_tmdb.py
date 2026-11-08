"""Download TMDB details for every catalogue title into data/raw/tmdb/ (one JSON line each).

Films are fetched by their TMDB id; shows are first found by IMDb id. Each record keeps what the
catalogue lacks: keywords (shows had none), TMDB recommendations, age ratings, a trailer,
seasons and episodes, creators, and streaming providers for a few countries. Resumable.
Needs TMDB_READ_TOKEN, in the environment or the project's .env. WATCH_REGIONS (same places)
picks the countries whose streaming offers are kept, as two-letter codes: "US,GB,IN".

Usage:
    python3 -I scripts/fetch_tmdb.py
"""
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw" / "tmdb"
DB = ROOT / "data" / "movies.db"
API = "https://api.themoviedb.org/3"
RATE = 40  # requests per second; TMDB allows about 50


def setting(name, default=None):
    if os.environ.get(name):
        return os.environ[name].strip()
    env = ROOT / ".env"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip("'\"") or default
    return default


REGIONS = tuple(r.strip().upper() for r in setting("WATCH_REGIONS", "FR,US,GB,MA,BE,CA,DE,ES").split(",") if r.strip())
TOKEN = setting("TMDB_READ_TOKEN")
if not TOKEN:
    raise SystemExit("TMDB_READ_TOKEN is missing: add it to .env (see README.md)")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "accept": "application/json"}
lock = threading.Lock()
next_slot = [time.monotonic()]


def get(path):
    for attempt in range(6):
        with lock:  # simple global rate limit
            wait = next_slot[0] - time.monotonic()
            next_slot[0] = max(next_slot[0], time.monotonic()) + 1 / RATE
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(urllib.request.Request(API + path, headers=HEADERS), timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(int(e.headers.get("Retry-After") or 2 * (attempt + 1)))
        except (OSError, json.JSONDecodeError):
            time.sleep(2 * (attempt + 1))
    return None


def compact(d, kind):
    if not d:
        return None
    vids = (d.get("videos") or {}).get("results", [])
    trailer = next((v["key"] for v in vids if v.get("site") == "YouTube" and v.get("type") == "Trailer"
                    and v.get("official")), None) or next((v["key"] for v in vids if v.get("site") == "YouTube"
                                                           and v.get("type") == "Trailer"), None)
    providers = {}
    for region, p in ((d.get("watch/providers") or {}).get("results") or {}).items():
        if region in REGIONS:
            providers[region] = {t: [x["provider_name"] for x in p.get(t, [])] for t in ("flatrate", "free", "ads", "rent", "buy")
                                 if p.get(t)}
    out = {
        "tmdb_id": d["id"], "kind": kind, "overview": d.get("overview"), "tagline": d.get("tagline"),
        "vote_count": d.get("vote_count"), "vote_average": d.get("vote_average"), "popularity": d.get("popularity"),
        "keywords": [k["name"] for k in (d.get("keywords") or {}).get("keywords" if kind == "movie" else "results", [])],
        "recommendations": [r["id"] for r in (d.get("recommendations") or {}).get("results", [])],
        "trailer": trailer, "providers": providers,
    }
    if kind == "movie":
        certs = {}
        for r in (d.get("release_dates") or {}).get("results", []):
            if r["iso_3166_1"] in REGIONS:
                c = next((x["certification"] for x in r["release_dates"] if x.get("certification")), None)
                if c:
                    certs[r["iso_3166_1"]] = c
        out.update(certificates=certs, collection=(d.get("belongs_to_collection") or {}).get("name"))
    else:
        out.update(
            certificates={r["iso_3166_1"]: r["rating"] for r in (d.get("content_ratings") or {}).get("results", [])
                          if r["iso_3166_1"] in REGIONS and r.get("rating")},
            seasons=d.get("number_of_seasons"), episodes=d.get("number_of_episodes"), status=d.get("status"),
            in_production=d.get("in_production"), type=d.get("type"),
            created_by=[c["name"] for c in d.get("created_by", [])],
            last_air_date=d.get("last_air_date"))
    return out


def run(name, items, fetch):
    path = OUT / f"{name}.jsonl"
    done = set()
    if path.exists():
        for line in path.open(encoding="utf-8"):
            done.add(json.loads(line)["key"])
    todo = [it for it in items if it[0] not in done]
    print(f"{name}: {len(todo):,} to fetch ({len(done):,} done)", flush=True)
    with path.open("a", encoding="utf-8") as f, ThreadPoolExecutor(48) as ex:
        for n, (key, rec) in enumerate(ex.map(lambda it: (it[0], fetch(it)), todo), 1):
            with lock:
                f.write(json.dumps({"key": key, "data": rec}, ensure_ascii=False) + "\n")
            if n % 2000 == 0:
                print(f"{name}: {n:,} of {len(todo):,}", flush=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    films = [(k, i) for k, i in con.execute("SELECT key, id FROM catalog WHERE kind = 'movie' ORDER BY votes DESC")]
    shows = [(k, imdb) for k, imdb in con.execute(
        "SELECT key, imdb_id FROM catalog WHERE kind = 'tv' AND imdb_id IS NOT NULL ORDER BY votes DESC")]
    extra = "keywords,recommendations,videos,watch/providers"

    def film(it):
        return compact(get(f"/movie/{it[1]}?append_to_response={extra},release_dates"), "movie")

    def show(it):
        found = get(f"/find/{it[1]}?external_source=imdb_id")
        tv = (found or {}).get("tv_results") or []
        if not tv:
            return None
        return compact(get(f"/tv/{tv[0]['id']}?append_to_response={extra},content_ratings"), "tv")

    run("shows", shows, show)
    run("films", films, film)
    print("done", flush=True)


if __name__ == "__main__":
    main()
