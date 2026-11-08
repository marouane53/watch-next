"""Download the TVmaze show index into data/raw/tvmaze/ (one JSON file per page of 250 shows).

TVmaze's public API needs no key; its data is CC BY-SA 4.0, so credit TVmaze when showing it.
Pages already on disk are skipped, so the script can resume.

Usage:
    python3 -I scripts/fetch_tvmaze.py
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "raw" / "tvmaze"
URL = "https://api.tvmaze.com/shows?page={}"
UA = {"User-Agent": "watch-next (https://github.com/marouane53/watch-next; personal use)"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    page = 0
    while True:
        path = OUT / f"shows-{page:04d}.json"
        if not path.exists():
            req = urllib.request.Request(URL.format(page), headers=UA)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:  # past the last page
                    break
                if e.code == 429:  # rate limited: wait and retry the same page
                    time.sleep(10)
                    continue
                raise
            path.write_bytes(body)
            time.sleep(0.6)  # TVmaze allows about 20 calls per 10 seconds
        page += 1
    shows = sum(len(json.loads(p.read_text(encoding="utf-8"))) for p in OUT.glob("shows-*.json"))
    print(f"{page} pages, {shows:,} shows")


if __name__ == "__main__":
    main()
