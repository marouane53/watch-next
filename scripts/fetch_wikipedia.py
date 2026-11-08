"""Download the English Wikipedia introduction of every catalogue title that has an IMDb id.

Wikidata maps IMDb ids to English Wikipedia articles; the MediaWiki API returns their
introductions as plain text. Results go to data/raw/wikipedia/ (one JSON line per title) and the
script resumes where it stopped. Wikipedia text is CC BY-SA 4.0.

Usage:
    python3 -I scripts/fetch_wikipedia.py
"""
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "raw" / "wikipedia"
DB = ROOT / "data" / "movies.db"
UA = {"User-Agent": "watch-next/0.1 (https://github.com/marouane53/watch-next; personal use; python urllib)"}
SPARQL = "https://query.wikidata.org/sparql"
API = "https://en.wikipedia.org/w/api.php"


def get(url, data=None, tries=10):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=dict(UA, Accept="application/json"))
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:  # 429: back off as long as the server asks
            wait = int(e.headers.get("Retry-After") or 0) or min(15 * 2 ** attempt, 300)
            print(f"retry in {wait}s after HTTP {e.code}", flush=True)
            time.sleep(wait)
        except (OSError, json.JSONDecodeError) as e:  # URLError and socket timeouts are OSErrors
            wait = 10 * (attempt + 1)
            print(f"retry in {wait}s after {e}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url[:80]}")


def articles_for(imdb_ids):
    values = " ".join(f'"{i}"' for i in imdb_ids)
    query = f"""SELECT ?imdb ?article WHERE {{
      VALUES ?imdb {{ {values} }}
      ?item wdt:P345 ?imdb .
      ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> .
    }}"""
    data = urllib.parse.urlencode({"query": query, "format": "json"}).encode()
    rows = get(SPARQL, data)["results"]["bindings"]
    out = {}
    for r in rows:
        title = urllib.parse.unquote(r["article"]["value"].rsplit("/wiki/", 1)[1]).replace("_", " ")
        out.setdefault(r["imdb"]["value"], title)
    return out


def intros(titles):
    params = {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "redirects": 1,
              "format": "json", "formatversion": 2, "titles": "|".join(titles)}
    res = get(API + "?" + urllib.parse.urlencode(params))["query"]
    renamed = {r["from"]: r["to"] for r in res.get("redirects", []) + res.get("normalized", [])}
    pages = {p["title"]: p.get("extract") for p in res.get("pages", [])}
    out = {}
    for t in titles:
        final = renamed.get(renamed.get(t, t), renamed.get(t, t))
        out[t] = pages.get(final)
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    ids = [r[0] for r in con.execute("SELECT DISTINCT imdb_id FROM catalog WHERE imdb_id IS NOT NULL ORDER BY votes DESC")]
    links_path, intro_path = OUT / "articles.jsonl", OUT / "intros.jsonl"
    links = {}
    if links_path.exists():
        for line in links_path.open(encoding="utf-8"):
            d = json.loads(line)
            links[d["imdb_id"]] = d["article"]
    done_ids = set(links)
    todo = [i for i in ids if i not in done_ids]
    print(f"{len(ids):,} titles with IMDb ids; {len(todo):,} still to map to Wikipedia", flush=True)
    with links_path.open("a", encoding="utf-8") as f:
        for start in range(0, len(todo), 400):
            batch = todo[start:start + 400]
            found = articles_for(batch)
            for i in batch:  # record misses too, so a rerun skips them
                links[i] = found.get(i)
                f.write(json.dumps({"imdb_id": i, "article": found.get(i)}) + "\n")
            print(f"mapped {start + len(batch):,} of {len(todo):,} ({sum(1 for v in found.values() if v)} found)", flush=True)
            time.sleep(1)
    have = set()
    if intro_path.exists():
        for line in intro_path.open(encoding="utf-8"):
            have.add(json.loads(line)["article"])
    # Most-voted titles first, so a partial run already covers what matters.
    articles = list(dict.fromkeys(links[i] for i in ids if links.get(i) and links[i] not in have))
    print(f"{len(articles):,} introductions to download", flush=True)
    with intro_path.open("a", encoding="utf-8") as f:
        for start in range(0, len(articles), 20):
            batch = articles[start:start + 20]
            for title, text in intros(batch).items():
                f.write(json.dumps({"article": title, "intro": text}, ensure_ascii=False) + "\n")
            if start % 2000 == 0:
                print(f"introductions {start:,} of {len(articles):,}", flush=True)
            time.sleep(1.5)  # Wikipedia throttles faster clients
    print("done", flush=True)


if __name__ == "__main__":
    main()
