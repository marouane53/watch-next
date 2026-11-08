"""Download the Movie Vector Map data and build data/movies.db.

Source: https://mikeyguinness.com/movies/ (the page loads two static JSON files).

Usage:
    python3 -I scripts/build_db.py            # download, then build
    python3 -I scripts/build_db.py --offline  # rebuild from data/raw only
"""
import datetime
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://mikeyguinness.com/movies/"
FILES = ["movie_points.json", "cluster_labels.json"]
POSTER_BASE = "https://image.tmdb.org/t/p/w342"

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "movies.db"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE clusters (
    cluster INTEGER PRIMARY KEY,
    name TEXT,
    x REAL,
    y REAL,
    size INTEGER
);
CREATE TABLE movies (
    tmdb_id INTEGER PRIMARY KEY,
    idx INTEGER UNIQUE NOT NULL,
    title TEXT,
    year INTEGER,
    language TEXT,
    overview TEXT,
    director TEXT,
    rating REAL,
    popularity REAL,
    x REAL,
    y REAL,
    cluster INTEGER,
    color TEXT,
    poster_url TEXT,
    tmdb_url TEXT
);
CREATE TABLE genres (tmdb_id INTEGER, genre TEXT);
CREATE TABLE keywords (tmdb_id INTEGER, keyword TEXT);
CREATE TABLE cast_members (tmdb_id INTEGER, actor TEXT, billing INTEGER);
CREATE TABLE similar (tmdb_id INTEGER, similar_id INTEGER, rank INTEGER);
CREATE VIRTUAL TABLE movies_fts USING fts5(
    title, overview, keywords, cast_names, director,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE INDEX genres_genre ON genres(genre);
CREATE INDEX genres_movie ON genres(tmdb_id);
CREATE INDEX keywords_keyword ON keywords(keyword);
CREATE INDEX keywords_movie ON keywords(tmdb_id);
CREATE INDEX cast_actor ON cast_members(actor);
CREATE INDEX cast_movie ON cast_members(tmdb_id);
CREATE INDEX similar_movie ON similar(tmdb_id);
CREATE INDEX similar_target ON similar(similar_id);
CREATE INDEX movies_title ON movies(title COLLATE NOCASE);
CREATE INDEX movies_director ON movies(director);
CREATE INDEX movies_year ON movies(year);
CREATE INDEX movies_cluster ON movies(cluster);

CREATE VIEW movies_full AS
SELECT m.*, c.name AS cluster_name,
       (SELECT group_concat(genre, ', ') FROM genres g WHERE g.tmdb_id = m.tmdb_id) AS genres,
       (SELECT group_concat(actor, ', ') FROM
           (SELECT actor FROM cast_members a WHERE a.tmdb_id = m.tmdb_id ORDER BY billing)) AS cast_names
FROM movies m LEFT JOIN clusters c USING (cluster);
"""


def download():
    RAW.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        with urllib.request.urlopen(BASE_URL + name, timeout=120) as resp:
            body = resp.read()
        (RAW / name).write_bytes(body)
        print(f"downloaded {name}: {len(body):,} bytes")


def build():
    points = json.loads((RAW / "movie_points.json").read_text(encoding="utf-8"))
    labels = json.loads((RAW / "cluster_labels.json").read_text(encoding="utf-8"))

    tmp = DB.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)

    con.executemany(
        "INSERT INTO clusters VALUES (?, ?, ?, ?, ?)",
        [(c["cluster"], c["name"] or None, c["x"], c["y"], c["size"]) for c in labels],
    )

    # "similar" in the source holds positions in the points array, not TMDB ids.
    idx_to_id = [p["id"] for p in points]
    for idx, p in enumerate(points):
        tid = p["id"]
        con.execute(
            "INSERT INTO movies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tid, idx, p["title"], p["year"], p["language"], p["overview"] or None,
                p["director"] or None, p["rating"], p["popularity"], p["x"], p["y"],
                p["cluster"], p["color"],
                POSTER_BASE + p["poster"] if p["poster"] else None,
                f"https://www.themoviedb.org/movie/{tid}",
            ),
        )
        con.executemany("INSERT INTO genres VALUES (?, ?)", [(tid, g) for g in p["genres"]])
        con.executemany("INSERT INTO keywords VALUES (?, ?)", [(tid, k) for k in p["keywords"]])
        con.executemany(
            "INSERT INTO cast_members VALUES (?, ?, ?)",
            [(tid, a, i) for i, a in enumerate(p["cast"])],
        )
        con.executemany(
            "INSERT INTO similar VALUES (?, ?, ?)",
            [(tid, idx_to_id[n], r) for r, n in enumerate(p["similar"], start=1)],
        )
        con.execute(
            "INSERT INTO movies_fts(rowid, title, overview, keywords, cast_names, director) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (tid, p["title"], p["overview"], " | ".join(p["keywords"]),
             " | ".join(p["cast"]), p["director"]),
        )

    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("source", BASE_URL),
        ("built_on", datetime.date.today().isoformat()),
        ("movies", str(len(points))),
    ])
    con.commit()
    con.execute("VACUUM")
    con.close()
    tmp.replace(DB)
    print(f"built {DB.relative_to(ROOT)}: {len(points):,} movies, {len(labels)} named clusters")


if __name__ == "__main__":
    if "--offline" not in sys.argv:
        download()
    build()
