#!/usr/bin/env python3
"""Movie & TV finder: query the local Movie Vector catalogue and keep a taste profile.

Lists print one title per line:
    [m949] Heat (1995) · film · Michael Mann · English · Crime/Drama/Action · 2h50 · IMDb 8.3 (824k) · score 8.5
followed by a short overview (omit it with --brief). Add --json for structured output.
A REF is a catalogue key (m949 is a film, t169 a TV show), a TMDB film id (949), a title, a title
with a year ("Heat 1995"), or a title with its type ("tv:The Office", "movie:Heat").
Run `movies.py <command> -h` for each command's options. `ask` needs the project's .venv
(it re-launches itself there); everything else runs on the system Python.
"""
import argparse
import datetime
import html
import json
import math
import os
import random
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote_plus

SKILL_DIR = Path(__file__).resolve().parent.parent
# The project checkout holding data/ and .venv/: the skill sits in its skills/ folder (the agents'
# skill folders link to it), or MOVIE_FINDER_HOME names it.
PROJECT_DIRS = [Path(p).expanduser() for p in [os.environ.get("MOVIE_FINDER_HOME")] if p] + [SKILL_DIR.parent.parent]
THIS_YEAR = datetime.date.today().year
EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B"
# Measured on scripts/evaluate.py's 80-theme benchmark: +6-10% nDCG over the generic web-search
# instruction the model ships with.
QUERY_PROMPT = ("Instruct: Given a theme, mood or kind of story someone wants to watch, retrieve films and "
                "TV series whose story and themes match it\nQuery:")
AUTO_THEME_MIN = 0.6  # a query names a crowd tag only when their vectors are this close
SERVER_IDLE = 1800  # seconds the embedding server stays up without requests

LANGS = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German", "it": "Italian",
    "ja": "Japanese", "ko": "Korean", "zh": "Mandarin", "cn": "Cantonese", "hi": "Hindi",
    "ru": "Russian", "pt": "Portuguese", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "ar": "Arabic",
    "th": "Thai", "cs": "Czech", "hu": "Hungarian", "el": "Greek", "he": "Hebrew",
    "fa": "Persian", "id": "Indonesian", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam",
    "ro": "Romanian", "uk": "Ukrainian", "vi": "Vietnamese", "tl": "Tagalog", "kn": "Kannada",
    "bn": "Bengali", "mr": "Marathi", "sr": "Serbian", "hr": "Croatian", "is": "Icelandic",
    "et": "Estonian", "ka": "Georgian", "xx": "No language",
}
LANG_CODES = {name.lower(): code for code, name in LANGS.items()}
LANG_CODES.update({"chinese": "zh", "farsi": "fa", "brazilian": "pt"})
GENRE_ALIASES = {
    "sci-fi": "science fiction", "scifi": "science fiction", "sf": "science fiction",
    "doc": "documentary", "docs": "documentary", "animated": "animation", "anime": "animation",
    "kids": "family", "children": "family", "romcom": "romance", "rom-com": "romance",
}
KINDS = {"movie": "movie", "movies": "movie", "film": "movie", "films": "movie",
         "tv": "tv", "show": "tv", "shows": "tv", "series": "tv"}
STOPWORDS = set(
    "a an and the of to in on for with about at by from is are was be it its that this who "
    "whose what where when movie movies film films show shows series something some i me my want "
    "watch kind type sort really very good great".split()
)
# Tags and keywords worth a heads-up in a pitch.
CAUTION_TAGS = {
    "gore", "gory", "violence", "violent", "graphic violence", "torture", "rape", "brutal", "brutality", "bloody",
    "disturbing", "depressing", "bleak", "sad", "tearjerker", "slow", "slow paced", "nudity",
    "nudity (topless)", "nudity (full frontal)", "suicide", "animal cruelty", "splatter",
    "gratuitous violence", "sexualized violence",
}
# What --gentle keeps out, besides the Horror genre.
VIOLENT_TAGS = ("gore", "gory", "gratuitous violence", "sexualized violence", "violence", "violent", "brutal",
                "brutality", "torture", "bloody", "splatter", "disturbing", "rape", "graphic violence")
VIOLENT_KEYWORDS = ("gore", "violence", "graphic violence", "extreme violence", "torture", "brutality", "rape",
                    "splatter", "body horror", "dismemberment", "decapitation", "cannibalism")
# Tags too broad to count as something two titles "share".
GENERIC_TAGS = {"life", "story", "drama", "catastrophe", "pg-13", "original", "interesting", "very interesting"}
BETS = ("safe", "deeper", "wildcard")
BORING_KEYWORDS = {
    "based on novel or book", "woman director", "duringcreditsstinger", "aftercreditsstinger",
    "sequel", "remake", "short film", "3d animation", "independent film", "biography",
    "based on true story", "based on comic", "based on manga", "based on play or musical",
    "based on tv series", "based on video game", "amused", "admiring", "affectation", "aggressive",
}
# Profile lists. Everything but the watchlist and the pitch log is hidden from results.
LISTS = ("liked", "disliked", "seen", "watching", "dropped", "dismissed", "watchlist", "recommended")
HIDDEN = ("liked", "disliked", "seen", "watching", "dropped", "dismissed")


# ---------- helpers ----------

def fold(text):
    if text is None:
        return None
    norm = unicodedata.normalize("NFKD", text)
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def lang_name(code):
    return LANGS.get(code, code or "?")


def norm_lang(value):
    v = value.strip().lower()
    return LANG_CODES.get(v, v)


def norm_genre(value):
    v = value.strip().lower()
    return GENRE_ALIASES.get(v, v)


def marks(values):
    return ", ".join("?" for _ in values)


def snippet(text, n=160):
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "…"


def note(msg):
    print(f"# {msg}", file=sys.stderr)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def locate_db(arg):
    candidates = [arg, os.environ.get("MOVIE_FINDER_DB")]
    candidates += [str(p / "data" / "movies.db") for p in PROJECT_DIRS]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c).resolve()
    die("movies.db not found. Build it with ./install.sh in the watch-next checkout, or point "
        "MOVIE_FINDER_HOME at that checkout (or MOVIE_FINDER_DB at the database).")


def has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def connect(db):
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.create_function("fold", 1, fold, deterministic=True)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'catalog'").fetchone():
        die("the database has no catalog table yet; finish the build with ./install.sh in the project")
    return con


def fmt_votes(v):
    if not v:
        return "0"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1000:
        return f"{v / 1000:.0f}k"
    return str(v)


def fmt_runtime(minutes, kind):
    if not minutes:
        return None
    if kind == "tv":
        return f"{minutes} min episodes"
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"


def fmt_years(d):
    if d["kind"] != "tv":
        return str(d["year"] or "?")
    if d.get("status") == "Ended":
        end = d.get("end_year")
        return f"{d['year'] or '?'}–{end}" if end and end != d["year"] else str(d["year"] or "?")
    return f"{d['year'] or '?'}–"


# ---------- taste profile ----------

def profile_path(db):
    return Path(os.environ.get("MOVIE_FINDER_PROFILE") or db.parent / "profile.json")


def load_profile(db):
    path = profile_path(db)
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data.setdefault("country", None)
    data.setdefault("languages", [])
    data.setdefault("services", [])
    data.setdefault("notes", [])
    for name in LISTS:
        data.setdefault(name, [])
        for e in data[name]:  # entries from before TV support used tmdb_id
            if "key" not in e and "tmdb_id" in e:
                e["key"] = f"m{e['tmdb_id']}"
    return data


def save_profile(db, data):
    path = profile_path(db)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def keys_in(profile, *names):
    return {e["key"] for n in names for e in profile[n]}


def mine(profile, key):
    for name, label in (("liked", "liked"), ("disliked", "disliked"), ("dismissed", "not interested"),
                        ("dropped", "dropped"), ("watching", "watching"), ("seen", "seen"),
                        ("watchlist", "on watchlist")):
        if key in keys_in(profile, name):
            return label
    pitched = [e["added"] for e in profile["recommended"] if e["key"] == key]
    return f"pitched {pitched[-1]}" if pitched else None


# ---------- title lookup ----------

BASE = """SELECT c.rid, c.key, c.kind, c.id, c.imdb_id, c.title, c.year, c.end_year, c.status, c.language,
  c.country, c.network, c.people, c.genres, c.overview, c.runtime, c.rating, c.rating_source, c.votes,
  c.popularity, c.score, c.poster_url, c.url, c.cluster, c.x, c.y
FROM catalog c"""


def split_year(text):
    m = re.match(r"^(.*?)[\s,]*\(?((?:18|19|20)\d\d)\)?\s*$", text)
    if m and m.group(1).strip():
        return m.group(1).strip(), int(m.group(2))
    return text.strip(), None


def split_kind(text):
    m = re.match(r"^(movie|film|tv|show|series|title)\s*:\s*(.+)$", text.strip(), re.I)
    if m:
        return KINDS.get(m.group(1).lower()), m.group(2)
    return None, text


def title_candidates(con, text, kind=None, year=None, limit=10):
    kind2, text = split_kind(text)
    kind = kind or kind2
    title, parsed_year = split_year(text)
    year = year or parsed_year
    ft = fold(title)
    tokens = re.findall(r"\w+", ft)
    kind_sql, kind_args = (" AND c.kind = ?", [kind]) if kind else ("", [])
    aka_sql = " OR c.rid IN (SELECT rid FROM title_aka WHERE fold(aka) LIKE ?)" if has_table(con, "title_aka") else ""
    rows = con.execute(BASE + " WHERE (fold(c.title) LIKE ?" + aka_sql + ")" + kind_sql + " ORDER BY c.votes DESC LIMIT 80",
                       [f"%{ft}%"] * (2 if aka_sql else 1) + kind_args).fetchall()
    for joiner in (" AND ", " OR "):
        if rows or not tokens:
            break
        q = joiner.join(f'title:"{t}"' for t in tokens)
        rows = con.execute(BASE + " JOIN catalog_fts f ON f.rowid = c.rid WHERE catalog_fts MATCH ?" + kind_sql
                           + " ORDER BY bm25(catalog_fts) LIMIT 80", [q] + kind_args).fetchall()
    akas = other_titles(con, [r["rid"] for r in rows])

    def key(r):
        if year and r["year"] == year:
            year_rank = 0
        elif year and r["year"] and abs(r["year"] - year) <= 1:
            year_rank = 1
        else:
            year_rank = 2 if year else 0
        names = [fold(x) for x in [r["title"]] + akas[r["rid"]]]
        match_rank = 0 if ft in names else (1 if any(set(re.findall(r"\w+", x)).issuperset(tokens) for x in names)
                                            else 2)
        return (year_rank, match_rank, -(r["votes"] or 0), -(r["popularity"] or 0))

    ranked = sorted(rows, key=key)
    if any(key(r)[1] < 2 for r in ranked):
        ranked = [r for r in ranked if key(r)[1] < 2]
    return ranked[:limit]


def other_titles(con, rids):
    """IMDb's other titles (international and original) for these titles, by rid."""
    out = defaultdict(list)
    if rids and has_table(con, "title_aka"):
        for rid, aka in con.execute(f"SELECT rid, aka FROM title_aka WHERE rid IN ({marks(rids)})", list(rids)):
            out[rid].append(aka)
    return out


def warn_inexact(con, text, row):
    _, text = split_kind(text)
    title, _ = split_year(text)
    tokens = set(re.findall(r"\w+", fold(title)))
    names = [row["title"]] + other_titles(con, [row["rid"]])[row["rid"]]
    if not any(tokens.issubset(set(re.findall(r"\w+", fold(x)))) for x in names):
        note(f"no exact title match for {title!r}; the closest is {row['title']} ({row['year']}). "
             "It may not be in the catalogue.")


def by_key(con, key):
    return con.execute(BASE + " WHERE c.key = ?", (key,)).fetchone()


def by_rid(con, rid):
    return con.execute(BASE + " WHERE c.rid = ?", (rid,)).fetchone()


def resolve(con, ref, kind=None):
    ref = str(ref).strip()
    if re.fullmatch(r"[mt]\d+", ref):
        row = by_key(con, ref)
        if row:
            return row
    elif ref.isdigit() and kind != "tv":
        row = by_key(con, f"m{ref}")
        if row:
            return row
    if ref.lower().startswith("title:"):
        ref = ref[6:].strip()
    cands = title_candidates(con, ref, kind)
    if not cands:
        die(f"nothing matches {ref!r}. Try `find` with fewer words.")
    best = cands[0]
    warn_inexact(con, ref, best)
    others = [c for c in cands[1:4] if fold(c["title"]) == fold(best["title"])]
    msg = f"{ref!r} → {best['title']} ({best['year']}, {'series' if best['kind'] == 'tv' else 'film'}) [{best['key']}]"
    if others:
        alts = ", ".join(f"{c['title']} ({c['year']}, {'series' if c['kind'] == 'tv' else 'film'}) [{c['key']}]"
                         for c in others)
        msg += f"; also: {alts}. Pass the key to choose."
    note(msg)
    return best


# ---------- tags ----------

def title_tags(con, row, limit=12):
    """Crowd tags (MovieLens Tag Genome) for films that have them, else tags predicted from the plot."""
    if row["kind"] == "movie":
        rows = con.execute("SELECT tag, score, kind FROM crowd_tags WHERE tmdb_id = ? ORDER BY z DESC",
                           (row["id"],)).fetchall()
        if rows:
            themes = [r["tag"] for r in rows if r["kind"] == "theme"][:limit]
            opinions = [r["tag"] for r in rows if r["kind"] == "opinion"][:6]
            return themes, opinions, "crowd"
    rows = con.execute("SELECT tag FROM predicted_tags WHERE rid = ? ORDER BY z DESC LIMIT ?",
                       (row["rid"], limit)).fetchall()
    return [r["tag"] for r in rows], [], "predicted"


def tag_set(con, row):
    themes, opinions, _ = title_tags(con, row, limit=25)
    kws = []
    if row["kind"] == "movie":
        kws = [r[0] for r in con.execute("SELECT keyword FROM keywords WHERE tmdb_id = ?", (row["id"],))]
    return set(themes) | set(opinions) | {k for k in kws if k not in BORING_KEYWORDS}


def cautions(con, row, profile):
    out = []
    themes, opinions, source = title_tags(con, row, limit=25)
    tags = set(themes) | set(opinions)
    if row["kind"] == "movie":
        tags |= {r[0] for r in con.execute("SELECT keyword FROM keywords WHERE tmdb_id = ?", (row["id"],))}
    flagged = sorted(t for t in tags if t in CAUTION_TAGS)
    if flagged:
        out.append(", ".join(flagged) + (" (predicted)" if source == "predicted" else ""))
    if row["kind"] == "movie" and row["runtime"] and row["runtime"] >= 150:
        out.append(f"long ({fmt_runtime(row['runtime'], 'movie')})")
    if row["kind"] == "tv":
        if row["status"] == "Running":
            out.append("still running")
        elif row["year"] and row["end_year"] and row["end_year"] - row["year"] >= 5:
            out.append(f"long commitment ({row['year']}–{row['end_year']})")
    langs = profile.get("languages") or []
    if langs and row["language"] and row["language"] not in langs:
        out.append(f"subtitles ({lang_name(row['language'])})")
    return out


# ---------- filters ----------

def add_filters(p, limit=15):
    g = p.add_argument_group("filters")
    g.add_argument("--type", help="movie or tv (default: both)")
    g.add_argument("--genre", action="append", default=[], help="required genre (repeat: all required)")
    g.add_argument("--any-genre", action="append", default=[], help="at least one of these genres")
    g.add_argument("--not-genre", action="append", default=[])
    g.add_argument("--tag", action="append", default=[], help="crowd or predicted tag, e.g. 'atmospheric' (all required)")
    g.add_argument("--any-tag", action="append", default=[])
    g.add_argument("--not-tag", action="append", default=[])
    g.add_argument("--keyword", action="append", default=[], help="TMDB keyword, films only (all required)")
    g.add_argument("--any-keyword", action="append", default=[])
    g.add_argument("--not-keyword", action="append", default=[])
    g.add_argument("--lang", action="append", default=[], help="original language: code or name (any of)")
    g.add_argument("--not-lang", action="append", default=[])
    g.add_argument("--from", dest="year_from", type=int, help="earliest year")
    g.add_argument("--to", dest="year_to", type=int, help="latest year")
    g.add_argument("--person", help="director, writer or top-billed actor (accents ignored)")
    g.add_argument("--director", help="film director, or series writer")
    g.add_argument("--actor", help="top-5 billed actor (films)")
    g.add_argument("--network", help="TV network or streamer, e.g. HBO, Netflix")
    g.add_argument("--country", help="TV network country code, e.g. GB, KR")
    g.add_argument("--status", choices=["running", "ended"], help="TV shows only")
    g.add_argument("--min-runtime", type=int)
    g.add_argument("--max-runtime", type=int, help="minutes (films), or episode minutes (TV)")
    g.add_argument("--min-votes", type=int, help="IMDb votes: 10000 = known, 100000 = famous")
    g.add_argument("--max-votes", type=int, help="for hidden gems")
    g.add_argument("--min-rating", type=float)
    g.add_argument("--min-score", type=float, help="quality score (IMDb-weighted, about 4 to 9.5)")
    g.add_argument("--vibe", help="vibe cluster or sub-cluster id, films and series (see `vibes`)")
    g.add_argument("--cluster", type=int, help="the website's map cluster number, films only (see `clusters`)")
    g.add_argument("--on", action="append", default=[], help="streaming on this service in your country (profile "
                   "country, default US), e.g. --on Netflix; repeat for any of several")
    g.add_argument("--gentle", action="store_true", help="no horror, gore or graphic violence (crowd tags, predicted tags, keywords)")
    g.add_argument("--include-seen", action="store_true", help="keep titles the profile marks seen, liked, dropped…")
    g.add_argument("--adult", action="store_true", help="include softcore/erotic films (hidden by default)")
    g.add_argument("--shorts", action="store_true", help="include films under 40 minutes (hidden by default)")
    g.add_argument("--upcoming", action="store_true", help=f"include undated titles and those after {THIS_YEAR}")
    g.add_argument("--limit", type=int, default=limit)


def build_filters(a, profile):
    where, params = [], []

    def exists(sql, values, negate=False):
        where.append(("NOT " if negate else "") + f"EXISTS ({sql})")
        params.extend(values)

    def genre_sql(values):
        return (f"SELECT 1 FROM catalog_genres x WHERE x.rid = c.rid AND lower(x.genre) IN ({marks(values)})",
                [norm_genre(v) for v in values])

    def tag_sql(values):
        vals = [v.lower() for v in values]
        return (f"SELECT 1 FROM crowd_tags t WHERE c.kind = 'movie' AND t.tmdb_id = c.id AND t.tag IN ({marks(vals)}) "
                f"AND t.score >= 0.6 UNION ALL SELECT 1 FROM predicted_tags p WHERE p.rid = c.rid "
                f"AND p.tag IN ({marks(vals)})", vals + vals)

    def keyword_sql(values):
        vals = [v.lower() for v in values]
        return (f"SELECT 1 FROM keywords k WHERE c.kind = 'movie' AND k.tmdb_id = c.id "
                f"AND lower(k.keyword) IN ({marks(vals)})", vals)

    if a.type:
        kind = KINDS.get(a.type.lower())
        if not kind:
            die("--type is movie or tv")
        where.append("c.kind = ?")
        params.append(kind)
    for make, all_, any_, not_ in ((genre_sql, a.genre, a.any_genre, a.not_genre),
                                   (tag_sql, a.tag, a.any_tag, a.not_tag),
                                   (keyword_sql, a.keyword, a.any_keyword, a.not_keyword)):
        for v in all_:
            exists(*make([v]))
        if any_:
            exists(*make(any_))
        if not_:
            exists(*make(not_), negate=True)
    if a.on:
        region = (profile.get("country") or "US").upper()
        likes = " OR ".join("fold(p.provider) LIKE ?" for _ in a.on)
        where.append(f"EXISTS (SELECT 1 FROM providers p WHERE p.rid = c.rid AND p.region = ? "
                     f"AND p.type IN ('flatrate', 'free', 'ads') AND ({likes}))")
        params += [region] + [f"%{fold(x)}%" for x in a.on]
    if a.gentle:
        tags, kws = list(VIOLENT_TAGS), list(VIOLENT_KEYWORDS)
        where.append("NOT EXISTS (SELECT 1 FROM catalog_genres x WHERE x.rid = c.rid AND x.genre = 'Horror')")
        where.append(f"NOT EXISTS (SELECT 1 FROM crowd_tags t WHERE c.kind = 'movie' AND t.tmdb_id = c.id "
                     f"AND t.tag IN ({marks(tags)}) AND t.score >= 0.5)")
        where.append(f"NOT EXISTS (SELECT 1 FROM predicted_tags p WHERE p.rid = c.rid AND p.tag IN ({marks(tags)}))")
        where.append(f"NOT EXISTS (SELECT 1 FROM keywords k WHERE c.kind = 'movie' AND k.tmdb_id = c.id "
                     f"AND k.keyword IN ({marks(kws)}))")
        params += tags + tags + kws
    if not a.adult:
        where.append("c.adult = 0")
    if not a.shorts:
        where.append("c.short = 0")
    if a.lang:
        where.append(f"c.language IN ({marks(a.lang)})")
        params.extend(norm_lang(v) for v in a.lang)
    if a.not_lang:
        where.append(f"c.language NOT IN ({marks(a.not_lang)})")
        params.extend(norm_lang(v) for v in a.not_lang)
    if a.year_from:
        where.append("c.year >= ?")
        params.append(a.year_from)
    if a.year_to:
        where.append("c.year <= ?")
        params.append(a.year_to)
    if not a.upcoming:
        where.append("c.year <= ?")
        params.append(THIS_YEAR)
    if a.director:
        where.append("fold(c.people) LIKE ?")
        params.append(f"%{fold(a.director)}%")
    if a.actor:
        where.append("EXISTS (SELECT 1 FROM cast_members x WHERE c.kind = 'movie' AND x.tmdb_id = c.id "
                     "AND fold(x.actor) LIKE ?)")
        params.append(f"%{fold(a.actor)}%")
    if a.person:
        like = f"%{fold(a.person)}%"
        where.append("(fold(c.people) LIKE ? OR EXISTS (SELECT 1 FROM cast_members x WHERE c.kind = 'movie' "
                     "AND x.tmdb_id = c.id AND fold(x.actor) LIKE ?) OR EXISTS (SELECT 1 FROM credits r "
                     "WHERE r.imdb_id = c.imdb_id AND fold(r.name) LIKE ?))")
        params += [like, like, like]
    if a.network:
        where.append("fold(c.network) LIKE ?")
        params.append(f"%{fold(a.network)}%")
    if a.country:
        where.append("c.country = ?")
        params.append(a.country.upper())
    if a.status:
        where.append("c.status = ?")
        params.append("Running" if a.status == "running" else "Ended")
    for col, op, val in (("c.runtime", ">=", a.min_runtime), ("c.runtime", "<=", a.max_runtime),
                         ("c.votes", ">=", a.min_votes), ("c.votes", "<=", a.max_votes),
                         ("c.rating", ">=", a.min_rating), ("c.score", ">=", a.min_score)):
        if val is not None:
            where.append(f"{col} {op} ?")
            params.append(val)
    if a.cluster is not None:
        where.append("c.cluster = ?")
        params.append(a.cluster)
    if a.vibe:
        where.append("EXISTS (SELECT 1 FROM title_vibes v WHERE v.rid = c.rid AND (v.vibe = ? OR v.subvibe = ?))")
        params += [a.vibe, a.vibe]
    if not a.include_seen:
        hidden = sorted(keys_in(profile, *HIDDEN))
        if hidden:
            where.append(f"c.key NOT IN ({marks(hidden)})")
            params.extend(hidden)
    return where, params


def where_sql(where):
    return (" WHERE " + " AND ".join(where)) if where else ""


def fetch_rids(con, rids, where, params):
    rows, rids = [], list(rids)
    for i in range(0, len(rids), 500):
        chunk = rids[i:i + 500]
        rows += con.execute(BASE + where_sql(where + [f"c.rid IN ({marks(chunk)})"]), params + chunk).fetchall()
    return rows


# ---------- output ----------

FIELDS = ("key", "kind", "title", "year", "end_year", "status", "language", "network", "people", "genres",
          "runtime", "rating", "rating_source", "votes", "score", "overview", "poster_url", "url")


def record(r, profile, **extra):
    d = {k: r[k] for k in FIELDS}
    d["mine"] = mine(profile, r["key"])
    d.update(extra)
    return d


def line(d):
    bits = [f"{d['title']} ({fmt_years(d)})", "series" if d["kind"] == "tv" else "film"]
    if d["kind"] == "tv" and d.get("network"):
        bits.append(d["network"])
    elif d.get("people"):
        bits.append(d["people"])
    bits.append(lang_name(d["language"]))
    if d.get("genres"):
        bits.append(d["genres"].replace(", ", "/"))
    rt = fmt_runtime(d.get("runtime"), d["kind"])
    if rt:
        bits.append(rt)
    if d.get("rating"):
        src = d.get("rating_source")
        bits.append(f"IMDb {d['rating']:.1f} ({fmt_votes(d['votes'])})" if src == "IMDb" else f"{src} ★{d['rating']:.1f}")
    bits.append(f"score {d['score']:.1f}")
    if d.get("mine"):
        bits.append(f"[{d['mine']}]")
    return f"[{d['key']}] " + " · ".join(bits)


def emit(a, items, header=None):
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return
    if header:
        print(header)
    if not items:
        print("(nothing matches; loosen a filter)")
    for d in items:
        prefix = f"{d['rank']:>2}. " if "rank" in d else ""
        print(prefix + line(d))
        if d.get("why"):
            print(f"      why: {d['why']}")
        if d.get("shares"):
            print(f"      shares: {', '.join(d['shares'])}")
        if d.get("cautions"):
            print(f"      heads-up: {'; '.join(d['cautions'])}")
        if not a.brief and d.get("overview"):
            print(f"      {snippet(d['overview'])}")


LINK_LABELS = {"page": "Page", "imdb": "IMDb", "where_to_watch": "Where to watch", "letterboxd": "Letterboxd",
               "trailer_search": "Trailer search", "trailer": "Trailer"}


def links(r, country=None):
    out = {"page": r["url"]}
    if r["imdb_id"]:
        out["imdb"] = f"https://www.imdb.com/title/{r['imdb_id']}/"
    if r["kind"] == "movie":
        out["where_to_watch"] = (f"https://www.themoviedb.org/movie/{r['id']}/watch"
                                 + (f"?locale={country.upper()}" if country else ""))
        out["letterboxd"] = f"https://letterboxd.com/tmdb/{r['id']}"
    else:  # the catalogue has no TMDB id for shows: search TMDB, whose Watch tab is fed by JustWatch
        out["where_to_watch"] = f"https://www.themoviedb.org/search/tv?query={quote_plus(r['title'])}"
    q = quote_plus(" ".join(str(x) for x in (r["title"], r["year"], "series" if r["kind"] == "tv" else None, "trailer") if x))
    out["trailer_search"] = f"https://www.youtube.com/results?search_query={q}"
    return out


# ---------- commands: lookup ----------

def cmd_find(a, con, db, profile):
    kind = KINDS.get(a.type.lower()) if a.type else None
    rows = title_candidates(con, " ".join(a.title), kind, a.year, a.limit)
    if rows:
        warn_inexact(con, " ".join(a.title), rows[0])
    emit(a, [record(r, profile) for r in rows])


def cmd_show(a, con, db, profile):
    out = []
    for ref in a.refs:
        r = resolve(con, ref)
        themes, opinions, source = title_tags(con, r)
        d = record(r, profile, tags=themes, tag_source=source, viewer_opinions=opinions,
                   links=links(r, profile["country"]), cautions=cautions(con, r, profile))
        d["also_known_as"] = other_titles(con, [r["rid"]])[r["rid"]]
        if r["kind"] == "tv" and has_table(con, "crowd_series_stats"):
            st = con.execute("SELECT ml_ratings, ml_mean FROM crowd_series_stats WHERE rid = ?", (r["rid"],)).fetchone()
            d["movielens"] = {"ratings": st["ml_ratings"], "mean_stars": st["ml_mean"]} if st else None
        if r["kind"] == "movie":
            t = r["id"]
            d["cast"] = [x[0] for x in con.execute("SELECT actor FROM cast_members WHERE tmdb_id = ? ORDER BY billing", (t,))]
            d["keywords"] = [x[0] for x in con.execute("SELECT keyword FROM keywords WHERE tmdb_id = ?", (t,))]
            ph = con.execute("SELECT phrases, reviews FROM viewer_phrases WHERE tmdb_id = ?", (t,)).fetchone()
            d["viewer_phrases"] = ph["phrases"] if ph else None
            st = con.execute("SELECT ml_ratings, ml_mean FROM crowd_stats WHERE tmdb_id = ?", (t,)).fetchone()
            d["movielens"] = {"ratings": st["ml_ratings"], "mean_stars": st["ml_mean"]} if st else None
            cname = con.execute("SELECT name FROM clusters WHERE cluster = ?", (r["cluster"],)).fetchone()
            d["cluster_name"] = cname[0] if cname else None
        if has_table(con, "title_extra"):
            x = con.execute("SELECT * FROM title_extra WHERE rid = ?", (r["rid"],)).fetchone()
            if x:
                d["age_rating"] = {k: x[f"cert_{k.lower()}"] for k in ("US", "GB", "FR") if x[f"cert_{k.lower()}"]}
                d["seasons"] = int(x["seasons"]) if x["seasons"] is not None else None
                d["episodes"] = int(x["episodes"]) if x["episodes"] is not None else None
                d["created_by"] = x["created_by"]
                d["tagline"], d["collection"] = x["tagline"], x["collection"]
                if x["trailer"]:
                    d["links"]["trailer"] = f"https://www.youtube.com/watch?v={x['trailer']}"
            region = (profile.get("country") or "US").upper()
            prov = con.execute("SELECT type, group_concat(provider, ', ') FROM providers WHERE rid = ? AND region = ? "
                               "GROUP BY type", (r["rid"], region)).fetchall()
            d["streaming"] = {"region": region, **{t: v for t, v in prov}} if prov else None
        vibe = con.execute("SELECT p.id, p.label, s.id, s.label FROM title_vibes v JOIN vibe_clusters p ON p.id = v.vibe "
                           "JOIN vibe_clusters s ON s.id = v.subvibe WHERE v.rid = ?", (r["rid"],)).fetchone()
        d["vibe"] = f"{vibe[0]} {vibe[1]} › {vibe[2]} {vibe[3]}" if vibe else None
        sims = con.execute("SELECT c.key, c.title, c.year, c.kind FROM hybrid_similar h JOIN catalog c ON c.rid = h.other "
                           "WHERE h.rid = ? AND (h.cross = 0 AND h.rank <= 8 OR h.cross = 1 AND h.rank <= 3) "
                           "ORDER BY h.cross, h.rank", (r["rid"],)).fetchall()
        d["similar"] = [f"{s['title']} ({s['year']}{', series' if s['kind'] == 'tv' else ''}) [{s['key']}]" for s in sims]
        out.append(d)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    for d in out:
        print(line(d))
        if d.get("also_known_as"):
            print(f"  Also known as: {' / '.join(d['also_known_as'])}")
        if d["kind"] == "tv":
            made = f"Created by: {d['created_by']}" if d.get("created_by") else f"Writers: {d['people'] or '—'}"
            size = f" · {d['seasons']} season(s), {d['episodes']} episodes" if d.get("seasons") else ""
            print(f"  Network: {d['network'] or '—'} · Status: {d['status'] or '—'}{size} · {made}")
        else:
            print(f"  Cast: {', '.join(d['cast']) or '—'}")
        print(f"  Overview: {d['overview'] or '—'}")
        if d.get("age_rating"):
            print("  Age rating: " + ", ".join(f"{k} {v}" for k, v in d["age_rating"].items()))
        if d.get("streaming"):
            st = d["streaming"]
            parts = [f"{label}: {st[t]}" for t, label in (("flatrate", "stream"), ("free", "free"), ("ads", "with ads"),
                                                           ("rent", "rent"), ("buy", "buy")) if st.get(t)]
            print(f"  Streaming in {st['region']} (TMDB/JustWatch snapshot, may be out of date): " + " · ".join(parts))
        label = "Crowd tags" if d["tag_source"] == "crowd" else "Predicted tags (from the plot)"
        print(f"  {label}: {', '.join(d['tags']) or '—'}")
        if d["viewer_opinions"]:
            print(f"  Viewers' verdicts: {', '.join(d['viewer_opinions'])}")
        if d.get("viewer_phrases"):
            print(f"  Review vocabulary: {d['viewer_phrases']}")
        if d.get("keywords"):
            print(f"  TMDB keywords (may spoil): {', '.join(d['keywords'])}")
        if d.get("movielens"):
            print(f"  MovieLens: {d['movielens']['ratings']:,} ratings, mean {d['movielens']['mean_stars']:.2f}/5")
        if d["cautions"]:
            print(f"  Heads-up: {'; '.join(d['cautions'])}")
        if d.get("vibe"):
            print(f"  Vibe cluster: {d['vibe']}")
        print(f"  Similar: {'; '.join(d['similar'])}")
        print(f"  Poster: {d['poster_url'] or '—'}")
        for k, v in d["links"].items():
            print(f"  {LINK_LABELS[k]}: {v}")
        print()


SIGNAL_TEXT = {
    "crowd": "fans of {} also love it",
    "tags": "same feel as {}",
    "plot": "story close to {}",
    "people": "same people as {}",
    "vibes": "reviewers describe it like {}",
    "tmdb": "TMDB users pair it with {}",
    "premise": "premise like {} (Wikipedia)",
}


def explain(row, seed_title, prefix=""):
    comps = {k: row[prefix + k] for k in ("crowd", "tags", "plot", "people", "vibes", "tmdb", "premise")
             if prefix + k in row.keys() and row[prefix + k]}
    if not comps:
        return f"close to {seed_title}"
    best = max(comps, key=comps.get)
    return SIGNAL_TEXT[best].format(seed_title)


def cmd_similar(a, con, db, profile):
    r = resolve(con, a.ref)
    kind = KINDS.get(a.type.lower()) if a.type else None
    rows = con.execute(BASE.replace("FROM catalog c", ", h.rank, h.cross, h.crowd AS s_crowd, h.tags AS s_tags, "
                                    "h.plot AS s_plot, h.people AS s_people, h.vibes AS s_vibes, h.premise AS s_premise "
                                    "FROM hybrid_similar h JOIN catalog c ON c.rid = h.other")
                       + " WHERE h.rid = ?" + (" AND c.kind = ?" if kind else "") + " ORDER BY h.cross, h.rank",
                       [r["rid"]] + ([kind] if kind else [])).fetchall()
    if not kind:  # same kind first, then the best few across films and series
        rows = [x for x in rows if not x["cross"]][:a.limit] + [x for x in rows if x["cross"]][:5]
    items = [record(x, profile, rank=i, why=explain(x, r["title"], prefix="s_"))
             for i, x in enumerate(rows[:a.limit + 5], 1)]
    emit(a, items, header=f"Closest titles to {r['title']} ({r['year']}), blending crowd, tags, plot, Wikipedia premise and people:")


# ---------- commands: discovery ----------

def fts_query(text):
    if re.search(r'["*:()]|\b(AND|OR|NOT|NEAR)\b', text):
        return text
    words = [w for w in re.findall(r"\w+", text.lower()) if w not in STOPWORDS and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in words)


def fts_scores(con, text, where, params, cap=400, columns=None):
    q = fts_query(text)
    if not q:
        die("the search text has no searchable words")
    if columns:
        q = "{" + " ".join(columns) + "} : (" + q + ")"
    sql = (BASE.replace("FROM catalog c", ", bm25(catalog_fts, 4.0, 1.0, 2.0, 1.0, 0.5) AS bm "
                        "FROM catalog c JOIN catalog_fts f ON f.rowid = c.rid")
           + where_sql(["catalog_fts MATCH ?"] + where) + " ORDER BY bm LIMIT ?")
    try:
        rows = con.execute(sql, [q] + params + [cap]).fetchall()
    except sqlite3.OperationalError as e:
        die(f"bad search syntax ({e}); FTS5 query was: {q}")
    if not rows:
        return []
    best = -rows[0]["bm"]
    return [(r, (-r["bm"]) / best if best else 0.0) for r in rows]


def cmd_search(a, con, db, profile):
    where, params = build_filters(a, profile)
    scored = fts_scores(con, " ".join(a.query), where, params)
    ranked = sorted(scored, key=lambda x: x[1] * (1 + 0.12 * (x[0]["score"] - 6.5)), reverse=True)
    emit(a, [record(r, profile) for r, _ in ranked[:a.limit]])


SORTS = {
    "score": "c.score DESC", "rating": "c.rating DESC", "votes": "c.votes DESC",
    "newest": "c.year DESC, c.votes DESC", "oldest": "c.year ASC, c.votes DESC", "random": "random()",
}


def cmd_browse(a, con, db, profile):
    where, params = build_filters(a, profile)
    sql = BASE + where_sql(where) + f" ORDER BY {SORTS[a.sort]} LIMIT ?"
    emit(a, [record(r, profile) for r in con.execute(sql, params + [a.limit])])


def cmd_surprise(a, con, db, profile):
    if a.min_score is None:
        a.min_score = 6.8
    if a.min_votes is None:
        a.min_votes = 2000
    where, params = build_filters(a, profile)
    rows = con.execute(BASE + where_sql(where) + " ORDER BY c.score DESC LIMIT 3000", params).fetchall()
    rng = random.Random(a.seed)
    pool, picks = list(rows), []
    while pool and len(picks) < a.limit:
        weights = [max(r["score"] - 6.0, 0.1) ** 2 for r in pool]
        picks.append(pool.pop(rng.choices(range(len(pool)), weights=weights)[0]))
    emit(a, [record(r, profile) for r in picks])


def diversify(con, ranked, limit):
    """Greedy pick that skips near-duplicates (sequels, remakes, same-franchise entries)."""
    if not ranked:
        return ranked
    pool = ranked[:limit * 4]
    rids = [x[1]["rid"] for x in pool]
    close = defaultdict(set)
    for i in range(0, len(rids), 400):
        chunk = rids[i:i + 400]
        for rid, other in con.execute(f"SELECT rid, other FROM hybrid_similar WHERE rid IN ({marks(chunk)}) "
                                      f"AND cross = 0 AND rank <= 5 AND score >= 0.75", chunk):
            close[rid].add(other)
            close[other].add(rid)
    chosen, held = [], []
    for item in pool:
        rid = item[1]["rid"]
        if any(rid in close[c[1]["rid"]] for c in chosen):
            held.append(item)
        else:
            chosen.append(item)
        if len(chosen) == limit:
            break
    return (chosen + held)[:limit]


def cmd_recommend(a, con, db, profile):
    seeds = [resolve(con, r) for r in a.refs]
    avoid = [resolve(con, r) for r in a.avoid]
    skip = {s["rid"] for s in seeds + avoid}
    weight = defaultdict(float)
    reasons = defaultdict(list)
    hits = defaultdict(set)

    def add(rid, w, why, seed):
        if rid in skip:
            return
        weight[rid] += w
        if w > 0:
            reasons[rid].append((w, why))
            hits[rid].add(seed)

    q = ("SELECT other, rank, cross, score, crowd, tags, plot, people, vibes, premise FROM hybrid_similar "
         "WHERE rid = ? ORDER BY score DESC")
    for sign, group in ((1.0, seeds), (-0.7, avoid)):
        for s in group:
            first = con.execute(q, (s["rid"],)).fetchall()
            for row in first:
                damp = 0.65 if row["cross"] and not a.type else 1.0
                add(row["other"], sign * damp * row["score"], explain(row, s["title"]), s["rid"])
            # Lists run one way: Palm Springs lists About Time, not always the reverse.
            for row in con.execute("SELECT rid AS other, cross, score, crowd, tags, plot, people, vibes, premise FROM hybrid_similar "
                                   "WHERE other = ? ORDER BY score DESC LIMIT 50", (s["rid"],)):
                damp = 0.65 if row["cross"] and not a.type else 1.0
                add(row["other"], sign * 0.8 * damp * row["score"], explain(row, s["title"]), s["rid"])
            for row in [x for x in first if not x["cross"]][:10]:  # one more hop widens the pool
                mid = by_rid(con, row["other"])
                for row2 in con.execute(q, (row["other"],)).fetchall()[:20]:
                    add(row2["other"], sign * 0.3 * row["score"] * row2["score"],
                        f"close to {mid['title']}, itself close to {s['title']}", s["rid"])

    where, params = build_filters(a, profile)
    rows = {r["rid"]: r for r in fetch_rids(con, [c for c, w in weight.items() if w > 0], where, params)}
    if a.query:
        matches = {r["rid"]: (r, rel) for r, rel in fts_scores(con, a.query, where, params, cap=3000)}
        for rid in list(rows):
            if rid in matches:
                weight[rid] *= 1 + 0.8 * matches[rid][1]
                reasons[rid].insert(0, (9, f"matches “{a.query}”"))
            else:
                weight[rid] *= 0.7

    seed_tags = set()
    for s in seeds:
        seed_tags |= tag_set(con, s)
    ranked = []
    for rid, r in rows.items():
        w = weight[rid]
        if w <= 0:
            continue
        n = len(hits[rid])
        if n > 1:
            w *= 1 + 0.3 * (n - 1)
        top = [why for w_, why in sorted(reasons[rid], key=lambda x: x[0], reverse=True)][:2]
        if n > 1:
            top.insert(0, f"connects {n} of your titles")
        ranked.append((w, r, "; ".join(dict.fromkeys(top))))
    ranked.sort(key=lambda x: x[0], reverse=True)
    ranked = diversify(con, ranked, a.limit)
    items = []
    for i, (w, r, why) in enumerate(ranked, 1):
        shared = [t for t in title_tags(con, r, limit=25)[0] if t in seed_tags and t not in GENERIC_TAGS][:4]
        items.append(record(r, profile, rank=i, why=why, shares=shared, cautions=cautions(con, r, profile),
                            match=round(w, 3)))
    seed_txt = ", ".join(f"{s['title']} ({s['year']})" for s in seeds)
    emit(a, items, header=f"Recommendations from {seed_txt}:")


def cmd_near(a, con, db, profile):
    anchors = [resolve(con, r, kind="movie") for r in a.refs]
    if any(s["kind"] != "movie" or s["x"] is None for s in anchors):
        die("`near` works on the films' 2D map only; use `similar` or `recommend` for TV")
    a.type = "movie"
    skip = {s["rid"] for s in anchors}
    where, params = build_filters(a, profile)
    scored = []
    for r in con.execute(BASE + where_sql(where + ["c.x IS NOT NULL"]), params):
        if r["rid"] in skip:
            continue
        ds = [math.hypot(r["x"] - s["x"], r["y"] - s["y"]) for s in anchors]
        scored.append((sum(ds) / len(ds), ds, r))
    scored.sort(key=lambda x: x[0])
    items = [record(r, profile, rank=i, why="map distance " + ", ".join(f"{d:.0f} from {s['title']}"
                                                                        for d, s in zip(ds, anchors)))
             for i, (mean, ds, r) in enumerate(scored[:a.limit], 1)]
    names = " and ".join(f"{s['title']} ({s['year']})" for s in anchors)
    emit(a, items, header=f"Closest films on the map to {names} (the map spans about 1000 units):")


def find_venv_python():
    for p in PROJECT_DIRS:
        py = p / ".venv" / "bin" / "python"
        if py.exists():
            return py
    return None


def sock_path(db):
    """The embedding server's Unix socket: beside the vectors, else in a private folder whose path
    is short enough for a socket (about 100 characters at most)."""
    path = db.parent / "vectors" / "embed.sock"
    if len(str(path)) < 100:
        return path
    import hashlib
    name = f"embed-{hashlib.sha1(str(db).encode()).hexdigest()[:12]}.sock"
    for folder in (Path.home() / ".cache" / "movie-finder", Path(f"/tmp/movie-finder-{os.getuid()}")):
        if len(str(folder / name)) < 100:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            if folder.stat().st_uid == os.getuid():
                return folder / name
    die("the project's path is too long for the embedding server's socket; move it to a shorter path")


def cmd_embed_server(a, con, db, profile):
    """Keep the embedding model loaded and embed query texts sent on a Unix socket (the client
    adds the instruction). Started on demand by `ask`; exits after SERVER_IDLE idle seconds."""
    import socket
    import torch
    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(EMBED_MODEL, local_files_only=True)
    model = AutoModel.from_pretrained(EMBED_MODEL, local_files_only=True, dtype=torch.float32).eval()
    path = sock_path(db)
    if path.exists():
        path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    srv.settimeout(SERVER_IDLE)
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                break
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                vecs = []
                for text in json.loads(data)["prompted"]:
                    batch = tok([text], return_tensors="pt", truncation=True, max_length=512)
                    with torch.no_grad():
                        v = model(**batch).last_hidden_state[0, -1]
                    vecs.append(torch.nn.functional.normalize(v, dim=0).tolist())
                conn.sendall((json.dumps({"vectors": vecs}) + "\n").encode())
    finally:
        srv.close()
        if path.exists():
            path.unlink()


def embed_queries(db, texts):
    import socket
    import subprocess
    import time

    path = sock_path(db)
    payload = (json.dumps({"prompted": [QUERY_PROMPT + t for t in texts]}) + "\n").encode()

    def request():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(120)
        sock.connect(str(path))
        sock.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(1 << 20)
            if not chunk:
                break
            data += chunk
        sock.close()
        return json.loads(data)["vectors"]

    try:
        return request()
    except (OSError, ValueError, KeyError):
        pass
    env = dict(os.environ, HF_HUB_OFFLINE="1", TOKENIZERS_PARALLELISM="false", TRANSFORMERS_VERBOSITY="error")
    subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "_embed-server", "--db", str(db)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=env)
    note("starting the local embedding model (about 20 seconds, once per half hour)")
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            return request()
        except (OSError, ValueError, KeyError):
            continue
    die("the embedding server didn't start; run `movies.py _embed-server` to see the error")


def embed_query(db, text):
    return embed_queries(db, [text])[0]


def cmd_ask(a, con, db, profile):
    """Meaning-based search, as measured best by scripts/evaluate.py: each query text ranks every
    title by plot vector; those rankings are fused (RRF), then fused again with a crowd-theme
    ranking when themes are given (or one tag matches the query closely)."""
    try:
        import numpy as np
    except ImportError:
        py = find_venv_python()
        if not py or Path(sys.executable).resolve() == py.resolve():
            die("`ask` needs the project's .venv (numpy, torch, transformers); run ./install.sh in the project")
        os.execv(str(py), [str(py), "-I", str(Path(__file__).resolve())] + sys.argv[1:])

    vec = db.parent / "vectors"
    E = np.load(vec / "content.npy").astype(np.float32)
    rids = np.load(vec / "content_rids.npy")
    pos = {int(r): i for i, r in enumerate(rids)}
    n = len(rids)
    texts = [" ".join(a.query)] + list(a.also)
    if len(texts) == 1 and not a.like and len(texts[0].split()) <= 8:
        print("tip: a short, abstract description matches stories loosely. Add 3 to 6 imagined plot summaries "
              "with --also, plus --themes (see SKILL.md, \"Searching by theme\").", file=sys.stderr)

    tag_names = json.loads((vec / "tag_names.json").read_text())
    T = np.load(vec / "tags.npy").astype(np.float32)
    real = np.load(vec / "tags_real.npy")
    Z = (T - T[real].mean(0)) / (T[real].std(0) + 1e-6)
    chosen, how, qtexts = [], None, list(texts)
    if a.themes:
        wanted = [t.strip().lower() for t in ",".join(a.themes).split(",") if t.strip()]
        unknown = [t for t in wanted if t not in tag_names]
        if unknown:
            die(f"unknown crowd tag(s): {', '.join(unknown)}. Find the vocabulary with `keywords WORD`.")
        chosen, how = [tag_names.index(t) for t in wanted], "chosen"
        if a.theme_query and not a.like and len(texts) == 1:
            # The themes as one more description: a bare abstract sentence ranks fitting stories
            # around #30,000, where the soft gate below would cancel their theme credit (excellence
            # case: 0 -> 3 known fits in the top 10). With several summaries it cost one, so not then.
            qtexts.append("A story about " + ", ".join(wanted) + ".")
    Q = np.array(embed_queries(db, qtexts), dtype=np.float32)
    if not chosen and len(texts) == 1:
        tsim = np.load(vec / "tag_vectors.npy").astype(np.float32) @ Q[0]
        j = int(np.argmax(tsim))
        if tsim[j] >= AUTO_THEME_MIN:
            chosen, how = [j], "matched"
    theme_score = Z[:, chosen].mean(1) if chosen else None

    def pct(v):
        out = np.empty(len(v), dtype=np.float32)
        out[np.argsort(v)] = np.linspace(0, 1, len(v), dtype=np.float32)
        return out

    def rrf_into(score, order, w=1.0):
        score[order] += w / (60 + np.arange(len(order)))

    def ranks_of(scores):
        """Each title's rank (0 = best) under a score array."""
        out = np.empty(n, dtype=np.int32)
        out[np.argsort(-scores)] = np.arange(n, dtype=np.int32)
        return out

    final = np.zeros(n, dtype=np.float64)
    like = None
    query_ranks, theme_ranks, near_ranks = [], None, None
    if a.like:
        # "Like X but Y": X's neighbourhood by content (plot and crowd-theme profile; co-watching
        # would drag in unrelated hits), then ranked mostly by how well each title matches Y.
        like = resolve(con, a.like)
        x = pos[like["rid"]]
        Zn = Z / (np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9)
        hood = np.zeros(n)
        for scores in (E @ E[x], Zn @ Zn[x]):
            rrf_into(hood, np.argsort(-scores)[:250])
        hood[x] = 0
        idx = np.flatnonzero(hood)
        match = pct((E[idx] @ Q.T).max(1))
        if theme_score is not None:
            match = (match + 2 * pct(theme_score[idx])) / 3
        final[idx] = (1 - a.shift * 0.7) * pct(hood[idx]) + a.shift * 0.7 * match
        near_ranks = ranks_of(hood)
        query_ranks = [ranks_of(E @ q) for q in Q]
    else:
        plot = np.zeros(n)
        for q in Q:
            scores = E @ q
            query_ranks.append(ranks_of(scores))
            rrf_into(plot, np.argsort(-scores))
        if a.fusion == "best" and len(Q) > 1:
            # Each title counts its best rank over the descriptions, so one strong match in one
            # domain isn't drowned by titles moderately close to several (sum breaks ties).
            best = np.min(np.stack(query_ranks), axis=0)
            plot_order = np.lexsort((-plot, best))
        else:
            plot_order = np.argsort(-plot)
        rrf_into(final, plot_order)
        if theme_score is not None:
            theme_part = np.zeros(n)
            rrf_into(theme_part, np.argsort(-theme_score), a.theme_weight)
            if a.theme_pool:
                # Soft gate: a title's theme credit shrinks with how badly its plot matches, so a
                # famous film tagged "genius" with an unrelated story (plot rank ~20,000) keeps
                # ~5% of it, while themes can still lift a decent match for a vague query.
                plot_rank = np.empty(n)
                plot_rank[plot_order] = np.arange(n)
                theme_part *= a.theme_pool / (a.theme_pool + plot_rank)
            final += theme_part
    if theme_score is not None:
        theme_ranks = ranks_of(theme_score)
    top = np.argsort(-final)[:1500]
    fused = {int(rids[i]): float(final[i]) for i in top if final[i] > 0}

    where, params = build_filters(a, profile)
    wants_factual = re.search(r"documentar|docu|reality|true crime|nature|real story", " ".join(texts), re.I) or any(
        norm_genre(g) in ("documentary", "reality") for g in a.genre + a.any_genre)
    if not wants_factual:
        where.append("NOT EXISTS (SELECT 1 FROM catalog_genres x WHERE x.rid = c.rid "
                     "AND x.genre IN ('Documentary', 'Reality'))")
    if like is None and not chosen and len(texts) == 1 and fts_query(texts[0]) \
            and len(fts_query(texts[0]).split(" OR ")) <= 4:
        for r, (row, _) in enumerate(fts_scores(con, texts[0], where, params, cap=300, columns=("overview", "tags"))):
            fused[row["rid"]] = fused.get(row["rid"], 0.0) + 0.4 / (60 + r)
    if like is not None:
        fused.pop(like["rid"], None)
    rows = fetch_rids(con, list(fused), where, params)
    ranked = sorted(((fused[r["rid"]] * max(1 + a.quality_weight * (r["score"] - 6.5), 0.2), r, "") for r in rows),
                    key=lambda x: x[0], reverse=True)
    ranked = diversify(con, ranked, a.limit)
    theme_names = {tag_names[j] for j in chosen}

    def evidence(rid):
        """Why a title came up, in ranks among all titles, for citing (`why` / `evidence`)."""
        i = pos[rid]
        ev, parts = {}, []
        if near_ranks is not None:
            ev["near_anchor_rank"] = int(near_ranks[i]) + 1
            parts.append(f"near {like['title']} (#{ev['near_anchor_rank']:,})")
        if query_ranks:
            qr = [int(r[i]) + 1 for r in query_ranks]
            best = int(np.argmin(qr))
            ev["description_ranks"] = qr
            what = ("the themes" if best >= len(texts) else "the description" if len(texts) == 1
                    else f"description {best + 1}")
            parts.append(f"plot matches {what} (#{qr[best]:,} of {n:,})")
        if theme_ranks is not None:
            ev["theme_rank"] = int(theme_ranks[i]) + 1
            parts.append(f"themes {', '.join(tag_names[j] for j in chosen)} (#{ev['theme_rank']:,})")
        return "; ".join(parts), ev

    items = []
    for i, (_, r, _) in enumerate(ranked, 1):
        why, ev = evidence(r["rid"])
        items.append(record(r, profile, rank=i, why=why, evidence=ev,
                            shares=[t for t in title_tags(con, r, limit=25)[0] if t in theme_names][:3],
                            cautions=cautions(con, r, profile)))
    head = f"Titles matching “{texts[0]}”" + (f" and {len(texts) - 1} more description(s)" if len(texts) > 1 else "")
    head += f" near {like['title']}" if like is not None else ""
    if how == "chosen":
        head += f" (themes: {', '.join(tag_names[j] for j in chosen)})"
    elif how == "matched":
        head += f" (matched crowd tag: {tag_names[chosen[0]]}; pass --themes to choose your own)"
    else:
        head += " (plot meaning only; add --themes for crowd themes)"
    emit(a, items, header=head + ":")


def cmd_keywords(a, con, db, profile):
    like = f"%{fold(a.pattern)}%"
    kw = con.execute("SELECT keyword, count(*) AS n FROM keywords WHERE fold(keyword) LIKE ? "
                     "GROUP BY keyword ORDER BY n DESC LIMIT ?", (like, a.limit)).fetchall()
    tags = con.execute("SELECT tag, count(*) AS n FROM (SELECT tag FROM crowd_tags WHERE score >= 0.6 "
                       "UNION ALL SELECT tag FROM predicted_tags) WHERE fold(tag) LIKE ? "
                       "GROUP BY tag ORDER BY n DESC LIMIT ?", (like, a.limit)).fetchall()
    if a.json:
        print(json.dumps({"keywords": [dict(r) for r in kw], "tags": [dict(r) for r in tags]}, ensure_ascii=False))
        return
    print("Crowd tags (--tag): " + ("; ".join(f"{r['tag']} ({r['n']})" for r in tags) or "none"))
    print("TMDB keywords (--keyword, films only): " + ("; ".join(f"{r['keyword']} ({r['n']})" for r in kw) or "none"))


def cluster_summaries(con, named_only=False):
    rows = con.execute("SELECT m.cluster, c.name, count(*) AS n FROM movies m LEFT JOIN clusters c USING (cluster) "
                       "WHERE m.cluster >= 0 GROUP BY m.cluster ORDER BY n DESC").fetchall()
    tag_total = dict(con.execute("SELECT tag, count(*) FROM crowd_tags WHERE kind = 'theme' GROUP BY tag").fetchall())
    out = []
    for r in rows:
        if named_only and not r["name"]:
            continue
        top = con.execute("SELECT key, title, year FROM catalog WHERE kind = 'movie' AND cluster = ? "
                          "ORDER BY votes DESC LIMIT 3", (r["cluster"],)).fetchall()
        tags = con.execute("SELECT t.tag, count(*) FROM crowd_tags t JOIN movies m USING (tmdb_id) "
                           "WHERE m.cluster = ? AND t.kind = 'theme' GROUP BY t.tag HAVING count(*) >= 3",
                           (r["cluster"],)).fetchall()
        lift = sorted(((n * n / tag_total[t], t) for t, n in tags), reverse=True)
        out.append({"cluster": r["cluster"], "name": r["name"], "films": r["n"],
                    "examples": [f"{t['title']} ({t['year']}) [{t['key']}]" for t in top],
                    "themes": [t for _, t in lift[:4]]})
    return out


def cmd_clusters(a, con, db, profile):
    out = cluster_summaries(con)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    for c in out:
        print(f"{c['cluster']:>3} {c['name'] or '(unnamed)'} · {c['films']} films · "
              f"themes: {', '.join(c['themes']) or '—'} · e.g. {'; '.join(c['examples'])}")


def cmd_vibes(a, con, db, profile):
    """Two-level vibe clusters over films and series (Leiden communities on crowd-tag profiles).
    No id: the top level. A top-level id: its sub-clusters. A sub-cluster id: its best titles."""
    def examples(raw, n=4):
        keys = json.loads(raw)[:n]
        rows = {r["key"]: r for r in con.execute(f"SELECT key, title, year FROM catalog WHERE key IN ({marks(keys)})", keys)}
        return "; ".join(f"{rows[k]['title']} ({rows[k]['year']})" for k in keys if k in rows)

    if not a.id:
        rows = con.execute("SELECT * FROM vibe_clusters WHERE level = 1 ORDER BY CAST(id AS INTEGER)").fetchall()
    elif "." in a.id:
        r = con.execute("SELECT * FROM vibe_clusters WHERE id = ?", (a.id,)).fetchone() or die(f"no vibe {a.id}")
        print(f"{r['id']} {r['label']} · {r['titles']:,} titles. Best known (narrow with `browse --vibe {r['id']}`):")
        a.sort, a.limit = a.sort or "votes", a.limit
        top = con.execute(BASE + " JOIN title_vibes v ON v.rid = c.rid WHERE v.subvibe = ? AND c.adult = 0 "
                          f"ORDER BY {SORTS[a.sort]} LIMIT ?", (a.id, a.limit)).fetchall()
        emit(a, [record(x, profile) for x in top])
        return
    else:
        rows = con.execute("SELECT * FROM vibe_clusters WHERE parent = ? ORDER BY core_titles DESC", (a.id,)).fetchall()
        if not rows:
            die(f"no vibe {a.id}")
    if a.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
        return
    for r in rows:
        print(f"{r['id']:>5}  {r['label']} · {r['titles']:,} titles, {round(100 * r['tv_share'])}% series · "
              f"e.g. {examples(r['examples'])}")


def cmd_onboard(a, con, db, profile):
    """Contrasting vibe clusters, each as three famous titles and its moods, so a new user can
    point at what they like instead of answering questions (Chang et al., CSCW 2015)."""
    rows = con.execute("SELECT * FROM vibe_clusters WHERE level = 1 AND core_titles >= 150 "
                       "ORDER BY core_titles DESC LIMIT ?", (a.limit,)).fetchall()
    out = []
    for r in rows:
        keys = json.loads(r["examples"])[:3]
        titles = [t for (t,) in con.execute(f"SELECT title FROM catalog WHERE key IN ({marks(keys)})", keys)]
        out.append({"vibe": r["id"], "moods": r["label"], "examples": titles})
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    for i, c in enumerate(out, 1):
        print(f"{i}. {'; '.join(c['examples'])} · feels {c['moods']} (vibe {c['vibe']})")


# ---------- commands: profile ----------

def entry(r, **extra):
    d = {"key": r["key"], "title": r["title"], "year": r["year"], "kind": r["kind"],
         "added": datetime.date.today().isoformat()}
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


# ---------- history import ----------

# Netflix names episodes "Dark: Season 1: Secrets" or "The Queen's Gambit: Limited Series: Openings".
NETFLIX_EPISODE = re.compile(r":\s*(?:Season|Series|Limited Series|Miniseries|Part|Volume|Vol\.|Chapter|Book|"
                             r"Collection|Episode)\b", re.I)


def read_exports(path):
    """(file name, rows) for every CSV of an export: a .csv file, a folder or a .zip."""
    import csv
    import io
    import zipfile
    path = Path(path).expanduser()
    if path.is_dir():
        files = [(str(f.relative_to(path)), f.read_bytes()) for f in sorted(path.rglob("*.csv"))]
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            files = [(n, z.read(n)) for n in z.namelist() if n.lower().endswith(".csv")]
    elif path.is_file():
        files = [(path.name, path.read_bytes())]
    else:
        die(f"no such file or folder: {path}")
    return [(name, list(csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))))
            for name, data in files]


def match_title(con, title, kind=None, year=None, before=None):
    """The catalogue title an export names, matched on the exact title (or an IMDb other title),
    or None when unsure."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", fold(s or "")).strip()  # noqa: E731
    want = norm(title)
    if not want:
        return None
    kind_sql, kind_args = (" AND c.kind = ?", [kind]) if kind else ("", [])
    aka = " OR c.rid IN (SELECT rid FROM title_aka WHERE fold(aka) = ?)" if has_table(con, "title_aka") else ""
    rows = con.execute(BASE + " WHERE (fold(c.title) = ?" + aka + ")" + kind_sql + " ORDER BY c.votes DESC",
                       [fold(title.strip())] * (2 if aka else 1) + kind_args).fetchall()
    if not rows:  # punctuation differs: candidates by title words, then compare without it
        q = " AND ".join(f'title:"{t}"' for t in want.split())
        cands = con.execute(BASE + " JOIN catalog_fts f ON f.rowid = c.rid WHERE catalog_fts MATCH ?" + kind_sql
                            + " ORDER BY c.votes DESC LIMIT 50", [q] + kind_args).fetchall()
        akas = other_titles(con, [r["rid"] for r in cands])
        rows = [r for r in cands if want in [norm(x) for x in [r["title"]] + akas[r["rid"]]]]
    if year:
        rows = [r for r in rows if r["year"] and abs(r["year"] - year) <= 1]
    if before:
        rows = [r for r in rows if r["year"] and r["year"] <= before]
    bare = re.sub(r"\s*\((?:U\.?S\.?A?|U\.?K\.?|[A-Z][a-z]+)\)\s*$", "", title.strip())
    if not rows and bare != title.strip():  # Netflix's "The Office (U.S.)"
        return match_title(con, bare, kind, year, before)
    return rows[0] if rows else None


def import_events(con, files):
    """What each export row says: (source, title, catalogue row or None, target list, extras), in
    an order where stronger evidence comes later (watchlist, watched, liked, rated)."""
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    rated = rating_target
    order = {"watchlist": 0, "seen": 2, "liked": 3, "rated": 4}
    events, skipped = [], []
    for name, rows in files:
        fields = set(rows[0]) if rows else set()
        base = name.lower().rsplit("/", 1)[-1]
        if {"Const", "Your Rating"} <= fields:  # IMDb ratings export
            for x in rows:
                r = by_imdb(con, x["Const"]) or match_title(con, x.get("Title", ""), year=num(x.get("Year")))
                v = num(x["Your Rating"])
                if v:
                    events.append(("rated", "imdb", x.get("Title"), r, rated(v), {"rating": v}))
        elif "Const" in fields and "watchlist" in base:  # IMDb watchlist export
            for x in rows:
                r = by_imdb(con, x["Const"]) or match_title(con, x.get("Title", ""), year=num(x.get("Year")))
                events.append(("watchlist", "imdb", x.get("Title"), r, "watchlist", {}))
        elif "Letterboxd URI" in fields and base in ("ratings.csv", "watched.csv", "watchlist.csv", "films.csv"):
            if base == "films.csv" and "likes" not in name.lower():
                skipped.append(name)
                continue
            for x in rows:
                y = num(x.get("Year"))
                r = match_title(con, x["Name"], "movie", y) or match_title(con, x["Name"], None, y)
                if base == "ratings.csv" and num(x.get("Rating")):
                    v = num(x["Rating"]) * 2  # Letterboxd stars (0.5-5) on the 10-point scale
                    events.append(("rated", "letterboxd", x["Name"], r, rated(v), {"rating": v}))
                elif base == "films.csv":
                    events.append(("liked", "letterboxd", x["Name"], r, "liked", {"note": "liked on Letterboxd"}))
                elif base == "watched.csv":
                    events.append(("seen", "letterboxd", x["Name"], r, "seen", {}))
                else:
                    events.append(("watchlist", "letterboxd", x["Name"], r, "watchlist", {}))
        elif "Thumbs Value" in fields:  # Netflix ratings (from "Download your personal information")
            for x in rows:
                v = num(x.get("Thumbs Value"))
                if v in (1, 2, 3):
                    t = x.get("Title Name", "")
                    r = match_title(con, t, "tv") or match_title(con, t)
                    target = "disliked" if v == 1 else "liked"
                    events.append(("rated", "netflix", t, r, target,
                                   {"note": {1: "thumbs down", 2: "thumbs up", 3: "two thumbs up"}[int(v)] + " on Netflix"}))
        elif fields == {"Title", "Date"}:  # Netflix viewing history
            shows, films = defaultdict(list), defaultdict(list)
            prefix_count = Counter(x["Title"].split(": ")[0] for x in rows if ": " in x["Title"])
            for x in rows:
                t = x["Title"].strip()
                years = [int(n) for n in re.findall(r"\d+", x.get("Date", ""))]
                year = max((n if n > 100 else 2000 + n for n in years if n > 31 or len(years) == 3), default=None)
                m = NETFLIX_EPISODE.search(t)
                if m:
                    shows[t[:m.start()].strip()].append(year)
                elif ": " in t and prefix_count[t.split(": ")[0]] >= 2:
                    shows[t.split(": ")[0]].append(year)
                else:
                    films[t].append(year)
            for t, ys in shows.items():
                last = max((y for y in ys if y), default=None)
                r = match_title(con, t, "tv", before=last) or match_title(con, t, before=last)
                events.append(("seen", "netflix", t, r, "seen", {"note": f"Netflix: {len(ys)} episode(s) watched"}))
            for t, ys in films.items():
                last = max((y for y in ys if y), default=None)
                r = match_title(con, t, "movie", before=last) or match_title(con, t, before=last)
                if r is None and ": " in t:  # a lone episode of a series
                    r = match_title(con, t.split(": ")[0], "tv", before=last)
                events.append(("seen", "netflix", t, r, "seen", {"note": "watched on Netflix"}))
        else:
            skipped.append(name)
    return [(order[e[0]],) + e[1:] for e in events], skipped


def by_imdb(con, imdb_id):
    return con.execute(BASE + " WHERE c.imdb_id = ? ORDER BY c.votes DESC", (imdb_id,)).fetchone() if imdb_id else None


def rating_target(value):
    """The profile list a 10-point rating goes to, as with `profile rate`."""
    return "liked" if value >= 7 else "disliked" if value <= 4 else "seen"


def progress_event(source, title, r, seen, aired, assume=False):
    """What watching `seen` of a series' `aired` episodes says, when there is no rating: half or
    more (4+ episodes), or 20+ episodes, reads as a like; one or two episodes of a longer series
    as a drop; anything else as seen. Inferred, and labelled so. With `assume` (the user says the
    tracker is incomplete and what's in it was watched and liked), every tracked series is a like."""
    note = f"watched {seen} of {aired} episodes" if aired else f"watched {seen} episodes"
    if assume:
        return (2, source, title, r, "liked", {"why": f"in your {source} history ({note} tracked); you said tracked "
                                                      "shows were watched and liked", "source": "inferred"})
    if (aired and seen >= 4 and seen / aired >= 0.5) or seen >= 20:
        return (2, source, title, r, "liked", {"why": note, "source": "inferred"})
    if seen <= 2 and (aired or 0) >= 4:
        return (2, source, title, r, "dropped", {"why": note + ", then stopped", "source": "inferred",
                                                 "at": f"{seen} of {aired} episodes"})
    return (2, source, title, r, "seen", {"note": note})


def maybe_ongoing(event, last, end):
    """A series that reads as dropped but was last watched within 90 days of the export's last
    activity: tracking may simply have stopped, so it counts as seen, not dropped."""
    if event[4] != "dropped" or not last or not end or (end - last).days > 90:
        return event
    note = event[5]["why"].replace(", then stopped", "") + "; tracking ended soon after, so it may be ongoing"
    return event[:4] + ("seen", {"note": note})


def list_purpose(name):
    """What a user list is for, from its name: 'favourites', 'watchlist' or None (left out)."""
    name = fold(name or "")
    return "favourites" if re.search(r"fav|love|best|top", name) else "watchlist" if re.search(r"watch|later|queue", name) \
        else None


def favourite_events(source, title, r, watched, favourites):
    """A title in a favourites list: liked when the lists hold favourites; when they hold
    followed shows, a watchlist entry if it was never watched."""
    if favourites == "loved":
        return [(3, source, title, r, "liked", {"why": "in your favourites list"})]
    if favourites == "followed" and not watched:
        return [(0, source, title, r, "watchlist", {"note": "followed, never watched"})]
    return []


def trakt_export(path):
    """A Trakt export (a folder or .zip of its JSON files) as {file name: data}, or None."""
    import zipfile
    path = Path(path).expanduser()
    if path.is_dir():
        raw = {f.name: f.read_bytes() for f in path.glob("*.json")}
    elif path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            raw = {n.rsplit("/", 1)[-1]: z.read(n) for n in z.namelist() if n.endswith(".json")}
    else:
        return None
    if not {"watched-shows.json", "watched-movies.json"} & set(raw):
        return None
    return {n: json.loads(b.decode("utf-8")) for n, b in raw.items()}


def trakt_events(con, d, favourites, assume=False):
    """Events from a Trakt export: ratings, favourites, watched series (by episodes seen against
    episodes aired), watched films, hidden progress (drops), hidden recommendations (dismissed),
    the watchlist and lists named for favourites or watching later."""
    def item(x):
        kind = x.get("type") or ("show" if "show" in x else "movie")
        m = x.get(kind) or {}
        ids = m.get("ids") or {}
        cat_kind = "tv" if kind == "show" else "movie"
        r = by_imdb(con, ids.get("imdb")) or match_title(con, m.get("title") or "", cat_kind, m.get("year"))
        return kind, m.get("title"), r, ids.get("trakt")

    episodes = defaultdict(set)
    for name, rows in d.items():
        if name.startswith("watched-history"):
            for h in rows:
                if h.get("type") == "episode" and (h.get("episode") or {}).get("season"):
                    episodes[h["show"]["ids"]["trakt"]].add((h["episode"]["season"], h["episode"]["number"]))
    def day(stamp):
        return datetime.date.fromisoformat(stamp[:10]) if stamp else None

    events, progress = [], {}
    shows = d.get("watched-shows.json", [])
    end = max((day(x.get("last_watched_at")) for x in shows if x.get("last_watched_at")), default=None)
    for x in shows:
        _, title, r, tid = item(x)
        aired = x["show"].get("aired_episodes") or 0
        seen = len(episodes.get(tid, ())) or min(x.get("plays") or 0, aired or x.get("plays") or 0)
        events.append(maybe_ongoing(progress_event("trakt", title, r, seen, aired, assume), day(x.get("last_watched_at")),
                                    end))
        progress[tid] = events[-1][4]
    for x in d.get("watched-movies.json", []):
        _, title, r, tid = item(x)
        events.append((2, "trakt", title, r, "seen", {"note": "watched (Trakt)"}))
        progress[tid] = "seen"
    for name in ("ratings-movies.json", "ratings-shows.json"):
        for x in d.get(name, []):
            _, title, r, _ = item(x)
            if x.get("rating"):
                events.append((4, "trakt", title, r, rating_target(x["rating"]), {"rating": float(x["rating"])}))
    for x in d.get("lists-favorites.json", []):
        kind, title, r, _ = item(x)
        if kind in ("show", "movie"):
            events.append((3, "trakt", title, r, "liked", {"why": "a favourite on Trakt"}))
    for x in d.get("hidden-progress-watched.json", []):
        kind, title, r, tid = item(x)
        if kind == "show" and progress.get(tid) != "liked" and not assume:
            events.append((2, "trakt", title, r, "dropped", {"why": "hidden from your Trakt progress",
                                                               "source": "inferred"}))
    for x in d.get("hidden-recommendations.json", []):
        kind, title, r, _ = item(x)
        if kind in ("show", "movie"):  # with assume, hiding meant "already seen"
            events.append((1, "trakt", title, r, "liked", {"why": "hidden from Trakt's recommendations; you said what's "
                                                          "in your tracker was watched and liked", "source": "inferred"})
                          if assume else (1, "trakt", title, r, "dismissed", {"why": "hidden from Trakt's recommendations"}))
    lists = {str(x["ids"]["trakt"]): x["name"] for x in d.get("lists-lists.json", []) if x.get("ids")}
    for name in sorted(d):
        m = re.match(r"lists-list-(\d+)-", name)
        purpose = "watchlist" if name == "lists-watchlist.json" else list_purpose(lists.get(m.group(1), name)) if m else None
        for x in d[name] if purpose else ():
            kind, title, r, tid = item(x)
            if kind not in ("show", "movie"):
                continue
            if purpose == "watchlist":
                events.append((0, "trakt", title, r, "watchlist", {}))
            else:
                events += favourite_events("trakt", title, r, tid in progress, "loved" if assume else favourites)
    return events


def tvtime_db(path):
    """The SQLite database of a TV Time backup (.tvstbackup, a zip), copied to a temporary
    folder, or the path itself for a .db; None for anything else."""
    import tempfile
    import zipfile
    path = Path(path).expanduser()
    if path.suffix.lower() == ".db" and path.is_file():
        return path
    if path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".db")]
            if names:
                out = Path(tempfile.mkdtemp(prefix="tvtime-")) / "tvtime.db"
                out.write_bytes(z.read(names[0]))
                return out
    return None


def tvtime_events(con, path, favourites, assume=False):
    """Events from a TV Time backup, which mirrors Trakt: watched series with episodes seen and
    left, ratings, shows recommended to friends, hidden shows, the watchlist and lists."""
    t = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    tables = {n for (n,) in t.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"ShowInfo", "WatchedShowAssociation"} <= tables:
        die(f"{path} doesn't look like a TV Time backup")
    shows = {str(i): (imdb, title, year) for i, imdb, title, year in t.execute("SELECT Id, ImdbId, Title, Year FROM ShowInfo")}

    def show(sid):
        imdb, title, year = shows.get(str(sid), (None, None, None))
        return title or f"TV Time show {sid}", by_imdb(con, imdb) or (match_title(con, title, "tv", year) if title else None)

    def day(ticks):  # .NET ticks: 100 ns since 0001-01-01
        return datetime.date(1, 1, 1) + datetime.timedelta(microseconds=ticks // 10) if ticks else None

    last = {str(sid): day(ticks) for sid, ticks in t.execute(
        "SELECT ShowId, max(LastWatchedAt) FROM WatchedShowEpisodeInfo GROUP BY ShowId")}
    end = max((v for v in last.values() if v), default=None)
    events, progress = [], {}
    for sid, seen, left in t.execute("SELECT w.ShowId, h.AlreadySeenEpisode, h.EpisodeLeft FROM WatchedShowAssociation w "
                                     "LEFT JOIN InfoOnHome h ON h.Id = w.ShowId"):
        if seen is None:
            seen = t.execute("SELECT count(*) FROM WatchedShowEpisodeInfo WHERE ShowId = ? AND SeasonNumber > 0",
                             (sid,)).fetchone()[0]
        title, r = show(sid)
        aired = (seen or 0) + (left or 0) if left is not None else 0
        events.append(maybe_ongoing(progress_event("tv time", title, r, seen or 0, aired, assume), last.get(str(sid)), end))
        progress[str(sid)] = events[-1][4]
    if {"WatchedMovieAssociation", "MovieInfo"} <= tables:
        for imdb, title, year in t.execute("SELECT m.ImdbId, m.Title, m.Year FROM WatchedMovieAssociation w "
                                           "JOIN MovieInfo m ON m.Id = w.Id"):
            r = by_imdb(con, imdb) or match_title(con, title or "", "movie", year)
            events.append((2, "tv time", title, r, "seen", {"note": "watched (TV Time)"}))
    for sid, value in t.execute("SELECT ShowId, Value FROM Rating WHERE ShowId IS NOT NULL AND SeasonNumber IS NULL "
                                "AND EpisodeNumber IS NULL AND Value > 0") if "Rating" in tables else ():
        title, r = show(sid)
        events.append((4, "tv time", title, r, rating_target(float(value)), {"rating": float(value)}))
    for (sid,) in t.execute("SELECT Id FROM PersonalRecommendation") if "PersonalRecommendation" in tables else ():
        title, r = show(sid)
        events.append((3, "tv time", title, r, "liked", {"why": "you recommended it in TV Time"}))
    for sid, origin in t.execute("SELECT ShowId, Origin FROM HiddenItem WHERE Type = 'show' AND IsSeason = 0"):
        title, r = show(sid)
        if origin == 3:
            events.append((1, "tv time", title, r, "liked", {"why": "hidden from recommendations; you said what's in your "
                                                            "tracker was watched and liked", "source": "inferred"})
                          if assume else (1, "tv time", title, r, "dismissed", {"why": "hidden from recommendations"}))
        elif progress.get(str(sid)) != "liked" and not assume:
            events.append((2, "tv time", title, r, "dropped", {"why": "hidden from your progress", "source": "inferred"}))
    for (sid,) in t.execute("SELECT ShowId FROM WatchListShowAssociation"):
        title, r = show(sid)
        events.append((0, "tv time", title, r, "watchlist", {}))
    if {"UserList", "UserListItem"} <= tables:
        for lname, imdb, title, year, ltype in t.execute(
                "SELECT l.Name, i.ImdbId, i.Title, i.Year, i.UserListItemType FROM UserListItem i "
                "JOIN UserList l ON l.Id = i.ListId"):
            purpose = list_purpose(lname)
            kind = "tv" if ltype == 0 else "movie"
            r = by_imdb(con, imdb) or match_title(con, title or "", kind, year)
            if purpose == "watchlist":
                events.append((0, "tv time", title, r, "watchlist", {}))
            elif purpose == "favourites":
                watched = r is not None and any(e[3] is not None and e[3]["key"] == r["key"] for e in events if e[0] == 2)
                events += favourite_events("tv time", title, r, watched, "loved" if assume else favourites)
    t.close()
    return events


def import_history(a, con, db, profile):
    """`profile import PATH`: Trakt, TV Time, Letterboxd, IMDb or Netflix exports into the profile.
    Ratings of 7/10 or more (3.5 Letterboxd stars) become likes and 4 or less dislikes, as with
    `profile rate`; unrated series are read from episodes watched (progress_event); watched titles
    become seen; reactions already in the profile are kept."""
    if not a.args:
        die("usage: profile import PATH [PATH…] [--dry-run] [--favourites loved|followed|skip]  (a Trakt export "
            "folder or .zip, a TV Time .tvstbackup, a Letterboxd .zip or folder, IMDb ratings.csv or watchlist.csv, "
            "Netflix NetflixViewingHistory.csv or Ratings.csv)")
    events, skipped, files = [], [], []
    for p in a.args:
        trakt = trakt_export(p)
        tv = None if trakt else tvtime_db(p)
        if trakt:
            events += trakt_events(con, trakt, a.favourites, a.assume_liked)
            files.append(p)
        elif tv:
            events += tvtime_events(con, tv, a.favourites, a.assume_liked)
            files.append(p)
            if tv != Path(p).expanduser():
                import shutil
                shutil.rmtree(tv.parent, ignore_errors=True)
        else:
            found = read_exports(p)
            files += [name for name, _ in found]
            ev, sk = import_events(con, found)
            events += ev
            skipped += sk
    events.sort(key=lambda e: e[0])
    events = [e[1:] for e in events]
    keep = {e["key"] for n in ("liked", "disliked", "dropped", "dismissed") for e in profile[n]}
    plan, unmatched = {}, []
    for source, title, r, target, extra in events:
        if r is None:
            unmatched.append(f"{title} ({source})")
            continue
        if r["key"] in keep:
            continue
        prev = plan.get(r["key"])
        if prev and prev[0] in ("liked", "disliked") and target in ("seen", "watchlist", "dismissed", "dropped"):
            prev[2].update({k: v for k, v in extra.items() if k == "rating"})
            continue  # a later "watched" row or middling rating doesn't undo a like
        if prev and prev[0] in ("seen", "dropped") and target in ("watchlist", "dismissed"):
            continue
        plan[r["key"]] = (target, r, {**(prev[2] if prev else {}), **extra, "imported": source})
    clears = {"liked": ("seen", "watchlist", "watching", "dropped", "dismissed"),
              "disliked": ("seen", "watchlist", "watching", "dismissed"), "seen": ("watchlist", "dismissed"),
              "dropped": ("watchlist", "watching", "seen"), "dismissed": ("watchlist",), "watchlist": ()}
    added = Counter()
    for key, (target, r, extra) in plan.items():
        if any(e["key"] == key for e in profile[target]):
            continue
        if target == "watchlist" and any(e["key"] == key for n in HIDDEN for e in profile[n]):
            continue
        for name in clears[target]:
            profile[name] = [e for e in profile[name] if e["key"] != key]
        profile[target].append(entry(r, **extra))
        added[target] += 1
    verb = "would add" if a.dry_run else "added"
    print(f"Read {len(files)} file(s), {len(events):,} rows. {verb}: "
          + (", ".join(f"{n} {target}" for target, n in added.most_common()) or "nothing new"))
    if skipped:
        print(f"Skipped (not a history file): {', '.join(skipped)}")
    unmatched = list(dict.fromkeys(unmatched))
    if unmatched:
        print(f"Not found in the catalogue ({len(unmatched):,}): " + "; ".join(unmatched[:25])
              + (" …" if len(unmatched) > 25 else ""))
    if not a.dry_run and added:
        path = profile_path(db)
        if path.is_file():
            path.with_suffix(".json.bak").write_bytes(path.read_bytes())
        save_profile(db, profile)
        print(f"Saved. The previous profile is in {path.with_suffix('.json.bak').name}.")


def cmd_profile(a, con, db, profile):
    act = a.action
    if act == "import":
        return import_history(a, con, db, profile)
    today = datetime.date.today()
    targets = {"like": "liked", "dislike": "disliked", "seen": "seen", "watchlist": "watchlist",
               "pick": "watching", "drop": "dropped", "dismiss": "dismissed"}
    clears = {"liked": ("disliked", "seen", "watchlist", "watching", "dropped", "dismissed"),
              "disliked": ("liked", "seen", "watchlist", "watching", "dismissed"),
              "seen": ("watchlist", "watching"), "watchlist": ("dismissed",),
              "watching": ("watchlist",), "dropped": ("watching", "watchlist", "liked"),
              "dismissed": ("watchlist",)}
    if act in targets:
        target = targets[act]
        for ref in a.args:
            r = resolve(con, ref)
            for name in clears[target] + (target,):
                profile[name] = [e for e in profile[name] if e["key"] != r["key"]]
            profile[target].append(entry(r, note=a.note, why=a.why, source=a.source if a.why else None,
                                         at=a.at if act == "drop" else None,
                                         drop_kind=a.drop_kind if act == "drop" else None))
            print(f"{target}: {r['title']} ({r['year']}) [{r['key']}]")
        save_profile(db, profile)
    elif act == "rate":
        if len(a.args) != 2:
            die("usage: profile rate REF 1-10")
        r = resolve(con, a.args[0])
        value = float(a.args[1])
        target = "liked" if value >= 7 else "disliked" if value <= 4 else "seen"
        for name in LISTS:
            if name != "recommended":
                profile[name] = [e for e in profile[name] if e["key"] != r["key"]]
        profile[target].append(entry(r, rating=value, why=a.why, source=a.source if a.why else None))
        save_profile(db, profile)
        print(f"{target} ({value:g}/10): {r['title']} ({r['year']}) [{r['key']}]")
    elif act == "log":
        for ref in a.args:
            bet = a.bet
            head, _, tail = ref.rpartition(":")
            if head and tail in BETS:  # KEY:bet, e.g. m949:safe
                ref, bet = head, tail
            r = resolve(con, ref)
            profile["recommended"].append(entry(r, bet=bet, note=a.note))
            print(f"pitched: {r['title']} ({r['year']}) [{r['key']}]" + (f" as {bet}" if bet else ""))
        save_profile(db, profile)
    elif act == "checkin":
        due = [e for e in profile["watching"]
               if (today - datetime.date.fromisoformat(e["added"])).days >= 1]
        due.sort(key=lambda e: e["added"])
        if not due:
            print("Nothing to check in on.")
        for e in due:
            print(f"{e['title']} ({e['year']}) [{e['key']}], picked {e['added']}")
    elif act == "remove":
        names = [a.list] if a.list else list(LISTS)
        for ref in a.args:
            r = resolve(con, ref)
            for name in names:
                profile[name] = [e for e in profile[name] if e["key"] != r["key"]]
            print(f"removed {r['title']} ({r['year']}) from {', '.join(names)}")
        save_profile(db, profile)
    elif act == "set":
        if len(a.args) != 2 or a.args[0] not in ("country", "languages", "services"):
            die("usage: profile set country FR | languages en,fr | services 'Netflix, Prime Video'")
        key, value = a.args
        if key == "country":
            profile[key] = value.upper()
        elif key == "languages":
            profile[key] = [norm_lang(v) for v in value.split(",") if v.strip()]
        else:
            profile[key] = [v.strip() for v in value.split(",") if v.strip()]
        save_profile(db, profile)
        print(f"{key} = {profile[key]}")
    elif act == "note":
        text = " ".join(a.args).strip()
        if not text:
            die('usage: profile note "text"')
        profile["notes"].append({"text": text, "added": today.isoformat()})
        save_profile(db, profile)
        print(f"noted: {text}")
    elif act == "taste":
        summary = taste(con, profile)
        if a.json:
            print(json.dumps(summary, ensure_ascii=False, indent=1))
            return
        for side, label in (("liked", "Liked"), ("disliked", "Disliked or dropped"),
                            ("stopped_early", "Stopped after an episode or two (inferred from an import; weak)")):
            if not summary[side]:
                continue
            print(f"{label}:")
            for k, v in summary[side].items():
                if k != "titles":
                    v = ", ".join(f"{x[0]} ×{x[1]}" if isinstance(x, (list, tuple)) else str(x) for x in v)
                print(f"  {k}: {v or '—'}")
        print(f"Notes: {'; '.join(summary['notes']) or '—'}")
    else:
        if a.json:
            print(json.dumps(profile, ensure_ascii=False, indent=1))
            return
        print(f"Profile file: {profile_path(db)}")
        print(f"Country: {profile['country'] or 'not set'} · Languages: {', '.join(profile['languages']) or 'not set'}"
              f" · Services: {', '.join(profile['services']) or 'not set'}")
        for n in profile["notes"]:
            print(f"Note ({n['added']}): {n['text']}")
        for name in LISTS:
            items = profile[name]
            shown = []
            for e in items[-40:]:
                s = f"{e['title']} ({e['year']})"
                if e.get("rating") is not None:
                    s += f" {e['rating']:g}/10"
                if e.get("at"):
                    s += f" @ {e['at']}"
                if e.get("bet"):
                    s += f" [{e['bet']}]"
                if e.get("why") or e.get("note"):
                    s += f" – {e.get('why') or e.get('note')}"
                if name in ("watching", "recommended"):
                    s += f" ({e['added']})"
                shown.append(s)
            print(f"{name.capitalize()} ({len(items)}): {'; '.join(shown) or '—'}")


def taste(con, profile):
    def summarize(keys):
        if not keys:
            return {}
        rows = [r for r in (by_key(con, k) for k in keys) if r]
        genres, tags, people, langs, decades, kinds = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
        for r in rows:
            genres.update(r["genres"].split(", ") if r["genres"] else [])
            tags.update(title_tags(con, r, limit=15)[0])
            if r["people"]:
                people.update(r["people"].split(", ")[:2])
            langs[lang_name(r["language"])] += 1
            if r["year"]:
                decades[f"{r['year'] // 10 * 10}s"] += 1
            kinds["series" if r["kind"] == "tv" else "film"] += 1
        return {
            "titles": len(rows),
            "kinds": kinds.most_common(),
            "genres": genres.most_common(6),
            "tags": [t for t, n in tags.most_common(15) if n > 1],
            "people": [p for p, n in people.most_common(6) if n > 1],
            "languages": langs.most_common(5),
            "decades": decades.most_common(5),
        }
    # Drops inferred from an import (one or two episodes, then nothing) are a weak signal: kept
    # apart from dislikes and drops with a stated reason.
    early = {e["key"] for e in profile["dropped"] if e.get("source") == "inferred"}
    return {"liked": summarize(sorted(keys_in(profile, "liked"))),
            "disliked": summarize(sorted(keys_in(profile, "disliked", "dropped") - early)),
            "stopped_early": summarize(sorted(early)),
            "notes": [n["text"] for n in profile["notes"]]}


# ---------- commands: output and admin ----------

# The house style (assets/house-style.css): cream newsprint, ink and vermilion, condensed headlines.
STYLE = SKILL_DIR / "assets" / "house-style.css"
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;700;800;900&'
         'family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400;1,6..72,500&'
         'display=swap" rel="stylesheet">')


def red_last_word(text):
    """A headline with its last word in vermilion, as on the front page."""
    head, _, last = html.escape(text).rpartition(" ")
    return f"{head} <span>{last}</span>" if head else last


def cmd_board(a, con, db, profile):
    picks = [{"ref": r} for r in a.refs]
    if a.picks:
        for p in json.loads(Path(a.picks).read_text(encoding="utf-8")):
            picks.append({"ref": p.get("key") or p.get("id") or p.get("tmdb_id") or p.get("title"),
                          "note": p.get("note"), "runtime": p.get("runtime"), "age": p.get("age")})
    if not picks:
        die("give title references or --picks FILE")
    cards, has_tv = [], False
    e = html.escape
    for i, p in enumerate(picks, 1):
        r = resolve(con, p["ref"])
        tv = r["kind"] == "tv"
        has_tv |= tv
        lk = links(r, profile["country"])
        imdb = r["rating"] and r["rating_source"] == "IMDb"
        rate = f'<span class="rate">★ {r["rating"]:.1f}</span>' if imdb else ""
        img = (f'<img src="{e(r["poster_url"])}" alt="" loading="lazy">' if r["poster_url"]
               else f'<div class="blank">{e(r["title"])}</div>')
        kicker = " · ".join(x for x in ["Series" if tv else "Film", lang_name(r["language"])] if x)
        who = r["network"] if tv else (r["people"] or "").split(",")[0].strip()
        when = " · ".join(x for x in [fmt_years(dict(r)), who] if x)
        runtime = p.get("runtime") or fmt_runtime(r["runtime"], r["kind"]) or ""
        facts = " · ".join(x for x in [(r["genres"] or "").replace(", ", "/"), p.get("age"),
                                       f"{fmt_votes(r['votes'])} IMDb votes" if imdb else None] if x)
        # The note is the pitch; the overview only fills in when there is none.
        if p.get("note"):
            text_html = f'<p class="note">{e(p["note"])}</p>'
        else:
            text_html = f'<p class="ov">{e(snippet(r["overview"], 220))}</p>'
        link_html = "".join(f'<a href="{e(v)}" target="_blank" rel="noopener">'
                            f'{"Trailer" if k == "trailer_search" else LINK_LABELS[k]}</a>' for k, v in lk.items())
        cards.append(
            f'<article class="card"><div class="poster"><span class="tab">{i:02d}</span>{rate}{img}</div>'
            f'<div class="body"><p class="kicker">{e(kicker)}</p><h3>{e(r["title"])}</h3>'
            f'<p class="meta"><span>{e(when)}</span><b>{e(runtime)}</b></p>{text_html}'
            + (f'<p class="facts">{e(facts)}</p>' if facts else "")
            + f'<p class="links">{link_html}</p></div></article>')
    title = a.title or "Tonight's picks"
    out = Path(a.out) if a.out else db.parent.parent / "boards" / (
        re.sub(r"[^a-z0-9]+", "-", fold(title)).strip("-") + f"-{datetime.date.today().isoformat()}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    dateline = f"{today.day} {today:%B %Y} · {len(cards)} pick{'s' if len(cards) != 1 else ''}"
    deck = f'<p class="deck">{e(a.subtitle)}</p>' if a.subtitle else ""
    credit = "Film data from TMDB and IMDb." + (" TV data from TVmaze (CC BY-SA)." if has_tv else "")
    out.write_text(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)}</title>{FONTS}<style>{STYLE.read_text(encoding='utf-8')}</style></head>"
        f'<body class="paper"><main class="page">'
        f'<div class="masthead"><p class="flag">The Watch<span>-</span>Next</p><p class="dateline">{e(dateline)}</p></div>'
        f'<h1 class="headline">{red_last_word(title)}</h1>{deck}'
        f'<div class="label"><h2>The picks</h2>'
        f'<p class="key"><span><b>★</b>IMDb rating</span></p></div>'
        f'<div class="grid">{"".join(cards)}</div>'
        f'<footer class="colophon"><p>{credit}</p><p><b>Movie Finder</b>picked for you</p></footer>'
        f"</main></body></html>\n", encoding="utf-8")
    print(out)


def cmd_sql(a, con, db, profile):
    try:
        cur = con.execute(a.query)
    except sqlite3.Error as e:
        die(str(e))
    cols = [c[0] for c in cur.description or []]
    rows = cur.fetchmany(a.limit)
    if a.json:
        print(json.dumps([dict(zip(cols, r)) for r in rows], ensure_ascii=False, indent=1))
        return
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if v is None else str(v) for v in r))


def cmd_info(a, con, db, profile):
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    counts = dict(con.execute("SELECT kind, count(*) FROM catalog GROUP BY kind").fetchall())
    crowd = con.execute("SELECT count(DISTINCT tmdb_id) FROM crowd_similar").fetchone()[0]
    tags = con.execute("SELECT count(DISTINCT tmdb_id) FROM crowd_tags").fetchone()[0]
    print(f"Database: {db}")
    print(f"Films: {counts.get('movie', 0):,} (site snapshot {meta.get('built_on', '?')}) · "
          f"TV shows: {counts.get('tv', 0):,} (TVmaze)")
    print(f"Crowd data: {crowd:,} films with MovieLens neighbours (ratings to 2023), {tags:,} with Tag Genome tags (2021)")
    print(f"Profile: {profile_path(db)} · vectors: {db.parent / 'vectors'}")


# ---------- CLI ----------

def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="structured output")
    common.add_argument("--brief", action="store_true", help="omit overview lines")
    common.add_argument("--db", help="path to movies.db (default: auto-detect)")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("find", parents=[common], help="look up a title")
    p.add_argument("title", nargs="+")
    p.add_argument("--year", type=int)
    p.add_argument("--type", help="movie or tv")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("show", parents=[common], help="full card: tags, viewers' verdicts, cautions, links")
    p.add_argument("refs", nargs="+")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("similar", parents=[common], help="closest titles, blending crowd, tags, plot, Wikipedia premise and people")
    p.add_argument("ref")
    p.add_argument("--type", help="movie or tv")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_similar)

    p = sub.add_parser("recommend", parents=[common], help="recommend from one or more liked titles")
    p.add_argument("refs", nargs="+", help="titles the user likes")
    p.add_argument("--avoid", action="append", default=[], help="a title to steer away from (repeatable)")
    p.add_argument("--query", help="theme words to favour, e.g. 'heist'")
    add_filters(p, limit=20)
    p.set_defaults(fn=cmd_recommend)

    p = sub.add_parser("ask", parents=[common], help="meaning-based search: 'a slow film about grief on a farm'")
    p.add_argument("query", nargs="+")
    p.add_argument("--also", action="append", default=[], help="another description of what to find (repeatable); "
                   "rankings are fused")
    p.add_argument("--themes", action="append", default=[], help="crowd tags to rank by, comma-separated "
                   "(e.g. genius,obsession); soft, unlike --tag")
    p.add_argument("--theme-weight", type=float, default=1.0, help="weight of --themes against the descriptions (default 1)")
    p.add_argument("--no-theme-query", dest="theme_query", action="store_false",
                   help="don't add the --themes as one more description")
    p.add_argument("--quality-weight", type=float, default=0.25, help="how much the quality score lifts results (default 0.25)")
    p.add_argument("--fusion", choices=["sum", "best"], default="best", help="combine descriptions by best (default) or summed rank")
    p.add_argument("--theme-pool", type=int, default=1000, help="soft gate: theme credit x N/(N + plot rank) (default 1000; 0 = off)")
    p.add_argument("--like", help="anchor title: 'like X but <query>'")
    p.add_argument("--shift", type=float, default=0.8, help="with --like: weight of the query vs closeness to the anchor, 0-1 (default 0.8)")
    add_filters(p)
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("_embed-server", parents=[common], help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_embed_server)

    p = sub.add_parser("search", parents=[common], help="full-text search over titles, plots, tags, review vocabulary, people")
    p.add_argument("query", nargs="+")
    add_filters(p)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("browse", parents=[common], help="filter the catalogue")
    p.add_argument("--sort", choices=sorted(SORTS), default="score")
    add_filters(p)
    p.set_defaults(fn=cmd_browse)

    p = sub.add_parser("surprise", parents=[common], help="random well-regarded picks")
    p.add_argument("--seed", type=int)
    add_filters(p, limit=5)
    p.set_defaults(fn=cmd_surprise)

    p = sub.add_parser("near", parents=[common], help="nearest films on the website's 2D map")
    p.add_argument("refs", nargs="+")
    add_filters(p)
    p.set_defaults(fn=cmd_near)

    p = sub.add_parser("keywords", parents=[common], help="discover crowd tags and TMDB keywords containing a word")
    p.add_argument("pattern")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_keywords)

    p = sub.add_parser("clusters", parents=[common], help="the film map's regions, with themes and examples")
    p.set_defaults(fn=cmd_clusters)

    p = sub.add_parser("vibes", parents=[common], help="browse two-level vibe clusters (films and series)")
    p.add_argument("id", nargs="?", help="a vibe id to open (e.g. 21, then 21.1)")
    p.add_argument("--sort", choices=sorted(SORTS))
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_vibes)

    p = sub.add_parser("onboard", parents=[common], help="contrasting vibe clusters to pick from, for a new user")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_onboard)

    p = sub.add_parser("profile", parents=[common],
                       help="show|like|dislike|seen|watchlist|pick|drop|dismiss|rate|log|checkin|remove|set|note|taste|import")
    p.add_argument("action", nargs="?", default="show",
                   choices=["show", "like", "dislike", "seen", "watchlist", "pick", "drop", "dismiss", "rate",
                            "log", "checkin", "remove", "set", "note", "taste", "import"])
    p.add_argument("args", nargs="*")
    p.add_argument("--note", help="free note stored with the entry")
    p.add_argument("--why", help="the reason, in a few words")
    p.add_argument("--source", choices=["stated", "inferred"], default="stated", help="who gave the reason")
    p.add_argument("--at", help="for drop: where they stopped, e.g. '40 min' or 'S1E3'")
    p.add_argument("--drop-kind", choices=["friction", "disengaged", "context"], help="for drop: why it stopped")
    p.add_argument("--bet", choices=BETS, help="for log: how risky the pitch was (or write KEY:bet per title)")
    p.add_argument("--list", choices=LISTS, help="for remove: only this list")
    p.add_argument("--dry-run", action="store_true", help="for import: show what would be added, change nothing")
    p.add_argument("--favourites", choices=["loved", "followed", "skip"], default="followed",
                   help="for import: what lists named for favourites hold: loved titles (likes), followed shows "
                        "(the never-watched ones go to the watchlist; default), or neither")
    p.add_argument("--assume-liked", action="store_true",
                   help="for a Trakt or TV Time import, when the user says the tracker is incomplete and what's in it "
                        "was watched and liked: every tracked series and favourites-list title becomes an inferred like")
    p.set_defaults(fn=cmd_profile)

    p = sub.add_parser("board", parents=[common], help="write an HTML poster board of picks")
    p.add_argument("refs", nargs="*")
    p.add_argument("--picks", help='JSON file: [{"key": "m949", "note": "why it fits", "runtime": "2h50", "age": "R"}, ...]')
    p.add_argument("--title")
    p.add_argument("--subtitle")
    p.add_argument("--out", help="output .html path (default: <project>/boards/)")
    p.set_defaults(fn=cmd_board)

    p = sub.add_parser("sql", parents=[common], help="read-only SQL against the database")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(fn=cmd_sql)

    p = sub.add_parser("info", parents=[common], help="data sources, counts and paths")
    p.set_defaults(fn=cmd_info)

    a = ap.parse_args()
    db = locate_db(a.db)
    con = connect(db)
    a.fn(a, con, db, load_profile(db))


if __name__ == "__main__":
    main()
