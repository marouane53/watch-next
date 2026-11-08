"""Enrich data/movies.db with IMDb facts, TV shows, crowd data and vectors.

Run after scripts/build_db.py, with the project environment:
    .venv/bin/python -I scripts/enrich.py              # every step, in order
    .venv/bin/python -I scripts/enrich.py imdb tv      # only some steps

Inputs live in data/raw/ (see AGENTS.md for where each one comes from). Each step drops and
rebuilds its own tables, so steps can be rerun on their own.
"""
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "movies.db"
VEC = ROOT / "data" / "vectors"

MOVIE_TYPES = {"movie", "tvMovie", "video", "short", "tvSpecial", "tvShort"}
SHOW_TYPES = {"tvSeries", "tvMiniSeries"}
# TVmaze show types worth recommending; news, sports, talk and award shows are left out.
TV_KINDS = {"Scripted", "Animation", "Documentary", "Reality", "Variety", "Game Show"}
TV_GENRES = {"Science-Fiction": "Science Fiction", "Sports": "Sport", "Children": "Family",
             "Anime": "Animation", "Food": "Food", "Travel": "Travel", "Supernatural": "Fantasy"}
LANG_CODES = {
    "English": "en", "French": "fr", "Spanish": "es", "German": "de", "Italian": "it",
    "Japanese": "ja", "Korean": "ko", "Chinese": "zh", "Hindi": "hi", "Russian": "ru",
    "Portuguese": "pt", "Swedish": "sv", "Danish": "da", "Norwegian": "no", "Finnish": "fi",
    "Dutch": "nl", "Polish": "pl", "Turkish": "tr", "Arabic": "ar", "Thai": "th", "Czech": "cs",
    "Hungarian": "hu", "Greek": "el", "Hebrew": "he", "Persian": "fa", "Indonesian": "id",
    "Tamil": "ta", "Telugu": "te", "Malayalam": "ml", "Romanian": "ro", "Ukrainian": "uk",
    "Vietnamese": "vi", "Tagalog": "tl", "Kannada": "kn", "Bengali": "bn", "Marathi": "mr",
    "Icelandic": "is", "Estonian": "et", "Georgian": "ka", "Serbian": "sr", "Croatian": "hr",
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def norm_title(text):
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def strip_html(text):
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return " ".join(text.split()) or None


def read_imdb(name, usecols):
    return pd.read_csv(RAW / "imdb" / f"{name}.tsv.gz", sep="\t", usecols=usecols, dtype=str,
                       na_values="\\N", keep_default_na=False, quoting=3)


def replace_table(con, name, df, indexes=()):
    con.execute(f"DROP TABLE IF EXISTS {name}")
    df.to_sql(name, con, index=False)
    for cols in indexes:
        con.execute(f"CREATE INDEX {name}_{'_'.join(cols)} ON {name}({', '.join(cols)})")


# ---------- step: imdb ----------

def step_imdb(con):
    """Link films to IMDb (MovieLens links first, then title + year) and load IMDb facts."""
    basics = read_imdb("title.basics", ["tconst", "titleType", "primaryTitle", "originalTitle",
                                         "startYear", "endYear", "runtimeMinutes", "genres"])
    basics = basics[basics.titleType.isin(MOVIE_TYPES | SHOW_TYPES)]
    ratings = read_imdb("title.ratings", ["tconst", "averageRating", "numVotes"])
    basics = basics.merge(ratings, on="tconst", how="left")
    basics["votes"] = pd.to_numeric(basics.numVotes, errors="coerce").fillna(0).astype(int)
    basics["year"] = pd.to_numeric(basics.startYear, errors="coerce")
    log(f"IMDb titles loaded: {len(basics):,}")

    movies = pd.read_sql("SELECT tmdb_id, title, year FROM movies", con)
    links = pd.read_csv(RAW / "movielens" / "ml-32m" / "links.csv", dtype={"imdbId": str}).dropna(subset=["tmdbId"])
    links["tmdb_id"] = links.tmdbId.astype(int)
    links["imdb_id"] = "tt" + links.imdbId
    links = links.drop_duplicates("tmdb_id")
    ids = movies.merge(links[["tmdb_id", "imdb_id", "movieId"]], on="tmdb_id", how="left")
    ids["match"] = np.where(ids.imdb_id.notna(), "movielens", None)

    # Title + year matching for films MovieLens doesn't know. Same-titled films are common, so a
    # match must share a director surname when both sides name one, and be the only candidate
    # when they don't.
    films = basics[basics.titleType.isin(MOVIE_TYPES)].copy()
    keyed = pd.concat([
        films.assign(key=films.primaryTitle.map(norm_title)),
        films.assign(key=films.originalTitle.map(norm_title)),
    ]).drop_duplicates(["tconst", "key"])
    by_key = {}
    for row in keyed[["key", "tconst", "year", "votes"]].itertuples(index=False):
        by_key.setdefault(row.key, []).append(row)
    directors = pd.read_sql("SELECT tmdb_id, director FROM movies", con).set_index("tmdb_id").director
    todo = {}
    for i, row in ids[ids.imdb_id.isna()].iterrows():
        cands = [c for c in by_key.get(norm_title(row.title), [])
                 if pd.notna(c.year) and pd.notna(row.year) and abs(c.year - row.year) <= 1]
        if cands:
            todo[i] = cands
    wanted = {c.tconst for cands in todo.values() for c in cands}
    crew = read_imdb("title.crew", ["tconst", "directors"])
    crew = crew[crew.tconst.isin(wanted)].dropna()
    crew = crew.assign(nconst=crew.directors.str.split(",")).explode("nconst")
    names = read_imdb("name.basics", ["nconst", "primaryName"])
    crew = crew.merge(names[names.nconst.isin(set(crew.nconst))], on="nconst")
    imdb_dirs = crew.groupby("tconst").primaryName.apply(list).to_dict()

    def surnames(name):
        return {t for t in norm_title(name).split() if len(t) >= 3}

    taken = set(ids.imdb_id.dropna())
    for i, cands in todo.items():
        cands = [c for c in cands if c.tconst not in taken]
        ours = directors.get(ids.at[i, "tmdb_id"])
        if ours:
            ok = [c for c in cands if c.tconst in imdb_dirs
                  and any(surnames(ours) & surnames(d) for d in imdb_dirs[c.tconst])]
            unknown = [c for c in cands if c.tconst not in imdb_dirs]
            cands = ok or (unknown if len(unknown) == 1 and len(cands) == 1 else [])
        elif len(cands) != 1:
            cands = []
        if cands:
            best = max(cands, key=lambda c: (c.year == ids.at[i, "year"], c.votes))
            ids.at[i, "imdb_id"] = best.tconst
            ids.at[i, "match"] = "title+year"
            taken.add(best.tconst)
    log(f"films linked to IMDb: {ids.imdb_id.notna().sum():,} of {len(ids):,} "
        f"({(ids.match == 'movielens').sum():,} via MovieLens)")

    facts = ids.merge(basics, left_on="imdb_id", right_on="tconst", how="left")
    movie_facts = pd.DataFrame({
        "tmdb_id": facts.tmdb_id,
        "imdb_id": facts.imdb_id,
        "movielens_id": facts.movieId.astype("Int64"),
        "imdb_match": facts.match,
        "runtime": pd.to_numeric(facts.runtimeMinutes, errors="coerce").astype("Int64"),
        "imdb_rating": pd.to_numeric(facts.averageRating, errors="coerce"),
        "imdb_votes": facts.votes.fillna(0).astype(int),
        "imdb_type": facts.titleType,
    })
    replace_table(con, "movie_facts", movie_facts, [("imdb_id",)])

    # Keep IMDb series rows for the TV step.
    shows = basics[basics.titleType.isin(SHOW_TYPES)][
        ["tconst", "titleType", "primaryTitle", "startYear", "endYear", "runtimeMinutes", "genres", "averageRating", "votes"]]
    replace_table(con, "imdb_series", shows, [("tconst",)])
    con.commit()


# ---------- step: tv ----------

def step_tv(con):
    """Build the shows table from TVmaze pages, with IMDb ratings and votes."""
    rows = []
    for page in sorted((RAW / "tvmaze").glob("shows-*.json")):
        for s in json.loads(page.read_text(encoding="utf-8")):
            if s.get("type") not in TV_KINDS:
                continue
            summary = strip_html(s.get("summary"))
            imdb = (s.get("externals") or {}).get("imdb")
            if not summary and not imdb:
                continue
            channel = s.get("network") or s.get("webChannel") or {}
            country = (channel.get("country") or {}).get("code")
            premiered, ended = s.get("premiered") or "", s.get("ended") or ""
            rows.append({
                "tvmaze_id": s["id"], "imdb_id": imdb, "title": s["name"], "type": s["type"],
                "language": LANG_CODES.get(s.get("language"), (s.get("language") or "")[:2].lower() or None),
                "genres": ", ".join(TV_GENRES.get(g, g) for g in s.get("genres") or []) or None,
                "status": s.get("status"),
                "year": int(premiered[:4]) if premiered[:4].isdigit() else None,
                "end_year": int(ended[:4]) if ended[:4].isdigit() else None,
                "runtime": s.get("averageRuntime") or s.get("runtime"),
                "network": channel.get("name"), "country": country,
                "tvmaze_rating": (s.get("rating") or {}).get("average"),
                "weight": s.get("weight") or 0,
                "summary": summary,
                "image_url": (s.get("image") or {}).get("medium"),
                "url": s.get("url"),
            })
    shows = pd.DataFrame(rows)
    series = pd.read_sql("SELECT tconst, averageRating, votes, endYear FROM imdb_series", con)
    shows = shows.merge(series, left_on="imdb_id", right_on="tconst", how="left").drop(columns="tconst")
    shows = shows.rename(columns={"averageRating": "imdb_rating", "votes": "imdb_votes"})
    shows["imdb_rating"] = pd.to_numeric(shows.imdb_rating, errors="coerce")
    shows["imdb_votes"] = shows.imdb_votes.fillna(0).astype(int)
    ey = pd.to_numeric(shows.endYear, errors="coerce")
    shows["end_year"] = shows.end_year.fillna(ey).astype("Int64")
    shows = shows.drop(columns="endYear")
    for col in ("year", "runtime"):
        shows[col] = pd.to_numeric(shows[col], errors="coerce").round().astype("Int64")
    replace_table(con, "shows", shows, [("imdb_id",), ("title",)])
    genres = shows[["tvmaze_id", "genres"]].dropna()
    genres = genres.assign(genre=genres.genres.str.split(", ")).explode("genre")[["tvmaze_id", "genre"]]
    replace_table(con, "show_genres", genres, [("tvmaze_id",), ("genre",)])
    con.commit()
    log(f"shows: {len(shows):,} ({(shows.imdb_votes > 0).sum():,} with IMDb votes)")


# ---------- step: crew ----------

def step_crew(con):
    """Directors and writers from IMDb for every linked film and show, to connect people
    across movies and TV."""
    wanted = set(pd.read_sql("SELECT imdb_id FROM movie_facts WHERE imdb_id IS NOT NULL", con).imdb_id)
    wanted |= set(pd.read_sql("SELECT imdb_id FROM shows WHERE imdb_id IS NOT NULL", con).imdb_id)
    crew = read_imdb("title.crew", ["tconst", "directors", "writers"])
    crew = crew[crew.tconst.isin(wanted)]
    parts = []
    for role in ("directors", "writers"):
        part = crew[["tconst", role]].dropna()
        part = part.assign(nconst=part[role].str.split(",")).explode("nconst")
        parts.append(part[["tconst", "nconst"]].assign(role=role[:-1]))
    credits = pd.concat(parts).drop_duplicates()
    names = read_imdb("name.basics", ["nconst", "primaryName"])
    credits = credits.merge(names[names.nconst.isin(set(credits.nconst))], on="nconst", how="left")
    credits = credits.rename(columns={"tconst": "imdb_id", "primaryName": "name"})
    replace_table(con, "credits", credits, [("imdb_id",), ("nconst",), ("name",)])
    con.commit()
    log(f"credits: {len(credits):,} rows for {credits.imdb_id.nunique():,} titles")


# ---------- step: crowd ----------

def step_crowd(con, lam=500.0, min_user_likes=5, min_item_likes=20, keep=50):
    """'People who liked X also liked Y' from MovieLens 32M with EASE (Steck 2019).
    A rating of 4 stars or more counts as a like."""
    from scipy import sparse

    r = pd.read_csv(RAW / "movielens" / "ml-32m" / "ratings.csv", usecols=["userId", "movieId", "rating"],
                    dtype={"userId": np.int32, "movieId": np.int32, "rating": np.float32})
    stats = r.groupby("movieId").rating.agg(ml_ratings="size", ml_mean="mean")
    likes = r[r.rating >= 4.0][["userId", "movieId"]]
    del r
    stats["ml_likes"] = likes.movieId.value_counts()
    all_stats = stats  # by MovieLens id, before the films-only filtering below
    for _ in range(2):  # the two thresholds interact; two passes settle them
        likes = likes[likes.movieId.map(likes.movieId.value_counts()) >= min_item_likes]
        likes = likes[likes.userId.map(likes.userId.value_counts()) >= min_user_likes]
    items = np.sort(likes.movieId.unique())
    users = np.sort(likes.userId.unique())
    log(f"EASE on {len(users):,} users x {len(items):,} films ({len(likes):,} likes)")
    X = sparse.csr_matrix((np.ones(len(likes), dtype=np.float32),
                           (np.searchsorted(users, likes.userId.values), np.searchsorted(items, likes.movieId.values))),
                          shape=(len(users), len(items)))
    G = (X.T @ X).toarray().astype(np.float64)
    G[np.diag_indices_from(G)] += lam
    P = np.linalg.inv(G)
    del G
    B = P / (-np.diag(P))
    B[np.diag_indices_from(B)] = 0.0
    del P
    log("EASE solved")

    facts = pd.read_sql("SELECT tmdb_id, movielens_id FROM movie_facts WHERE movielens_id IS NOT NULL", con)
    ml_to_tmdb = dict(zip(facts.movielens_id.astype(int), facts.tmdb_id))
    in_cat = np.array([ml_to_tmdb.get(int(i)) is not None for i in items])
    cat_idx = np.flatnonzero(in_cat)
    rows = []
    for i in cat_idx:
        scores = B[i, cat_idx]
        top = np.argpartition(-scores, keep)[:keep]
        top = top[np.argsort(-scores[top])]
        src = ml_to_tmdb[int(items[i])]
        for rank, j in enumerate(top, 1):
            if scores[j] <= 0:
                break
            rows.append((src, ml_to_tmdb[int(items[cat_idx[j]])], rank, float(scores[j])))
    sim = pd.DataFrame(rows, columns=["tmdb_id", "similar_id", "rank", "score"])
    replace_table(con, "crowd_similar", sim, [("tmdb_id",), ("similar_id",)])
    stats = stats.reset_index()
    stats["tmdb_id"] = stats.movieId.map(ml_to_tmdb)
    stats = stats.dropna(subset=["tmdb_id"])
    stats["tmdb_id"] = stats.tmdb_id.astype(int)
    stats["ml_likes"] = stats.ml_likes.fillna(0).astype(int)
    stats["ml_mean"] = stats.ml_mean.round(3)
    replace_table(con, "crowd_stats", stats[["tmdb_id", "ml_ratings", "ml_likes", "ml_mean"]], [("tmdb_id",)])
    con.commit()
    log(f"crowd neighbours for {sim.tmdb_id.nunique():,} catalogue films")

    # Series rated on MovieLens (miniseries and documentaries mostly), matched by IMDb id; an item
    # that is already a catalogue film stays a film. For every catalogue item, its nearest series,
    # and for every series, its nearest films: ranks count within the target's kind.
    links = pd.read_csv(RAW / "movielens" / "ml-32m" / "links.csv", usecols=["movieId", "imdbId"], dtype={"imdbId": str})
    links["imdb_id"] = "tt" + links.imdbId.str.zfill(7)
    shows = pd.read_sql("SELECT rid, imdb_id FROM catalog WHERE kind = 'tv' AND imdb_id IS NOT NULL", con)
    ml_to_show = {int(m): int(r) for m, r in links.merge(shows, on="imdb_id")[["movieId", "rid"]].values
                  if int(m) not in ml_to_tmdb}
    film_rid = dict(con.execute("SELECT id, rid FROM catalog WHERE kind = 'movie'"))
    item_rid = np.array([film_rid.get(ml_to_tmdb.get(int(i)), -1) if int(i) in ml_to_tmdb else ml_to_show.get(int(i), -1)
                         for i in items])
    is_show = np.array([int(i) in ml_to_show for i in items])
    film_idx, show_idx = np.flatnonzero((item_rid >= 0) & ~is_show), np.flatnonzero(is_show)
    rows = []
    for i in np.flatnonzero(item_rid >= 0):
        for targets in ((show_idx, film_idx) if is_show[i] else (show_idx,)):
            cand = targets[targets != i]
            scores = B[i, cand]
            top = np.argsort(-scores)[:keep]
            for rank, j in enumerate(top[scores[top] > 0], 1):
                rows.append((int(item_rid[i]), int(item_rid[cand[j]]), rank, float(scores[j])))
    del B
    replace_table(con, "crowd_series", pd.DataFrame(rows, columns=["rid", "other", "rank", "score"]),
                  [("rid",), ("other",)])
    st = all_stats.fillna({"ml_likes": 0})
    series_stats = pd.DataFrame([(r, int(st.ml_ratings[m]), int(st.ml_likes[m]), round(float(st.ml_mean[m]), 3))
                                 for m, r in ml_to_show.items() if m in st.index],
                                columns=["rid", "ml_ratings", "ml_likes", "ml_mean"])
    replace_table(con, "crowd_series_stats", series_stats, [("rid",)])
    con.commit()
    log(f"crowd links for {len(show_idx):,} series ({len(rows):,} rows)")


# ---------- step: genome ----------

GENOME = RAW / "genome" / "movie_dataset_public_final"
PERSON_TAGS = {"amy smart", "carrie-anne moss", "chris tucker", "emma watson", "ennio morricone",
               "liv tyler", "truman capote"}
# Director, author and studio names: they describe a style, so they help similarity, but shown
# on another title they read as false facts ("Heat: francis ford copolla").
NAME_TAGS = {
    "c.s. lewis", "dr. seuss", "george orwell", "jane austen", "neil gaiman", "philip k. dick",
    "roald dahl", "robert ludlum", "depp & burton", "disney", "disney animated feature",
    "francis ford copolla", "hitchcock", "kubrick", "lynch", "miyazaki", "pixar", "pixar animation",
    "spielberg", "tarantino", "monty python",
}
OPINION_TAGS = {
    "good", "great", "very good", "excellent", "masterpiece", "overrated", "boring", "boring!",
    "stupid", "stupid as hell", "stupidity", "awesome", "classic", "great movie", "good action",
    "great acting", "good sequel", "amazing cinematography", "bad script", "good dialogue",
    "great dialogue", "sad but good", "not as good as the first", "bad plot", "great music",
    "excellent script", "bad acting", "great soundtrack", "awesome soundtrack", "amazing photography",
    "bad ending", "great cinematography", "good story", "good acting", "good romantic comedies",
    "bad sequel", "good music", "so bad it's good", "so bad it's funny", "bad science",
    "great ending", "bad cgi", "good soundtrack",
    # verdicts rather than themes
    "bad", "brilliant", "cheesy", "corny", "disappointing", "dumb", "entertaining", "fun movie",
    "funniest movies", "guilty pleasure", "highly quotable", "horrible", "interesting", "lame",
    "not funny", "original", "original plot", "perfect", "plot holes", "pointless", "predictable",
    "pretentious", "quotable", "ridiculous", "suprisingly clever", "too long", "underrated",
    "unfunny", "unintentionally funny", "unique", "unrealistic", "very interesting", "waste of time",
    "watch the credits", "best war films",
}


def tag_kind(tag):
    if tag in PERSON_TAGS:
        return "person"
    if tag in NAME_TAGS:
        return "name"
    if re.match(r"^(oscar|afi 100|saturn award|imdb top 250|criterion)", tag):
        return "award"
    if tag in OPINION_TAGS:
        return "opinion"
    return "theme"


def step_genome(con, keep=50, show=25, min_score=0.5):
    """Crowd tags (Tag Genome 2021, TagDL scores) and tag-based similarity between films."""
    meta = pd.read_json(GENOME / "raw" / "metadata.json", lines=True)[["item_id", "imdbId"]]
    meta["imdb_id"] = "tt" + meta.imdbId.astype(str).str.zfill(7)
    facts = pd.read_sql("SELECT tmdb_id, imdb_id FROM movie_facts WHERE imdb_id IS NOT NULL", con)
    item_to_tmdb = meta.merge(facts, on="imdb_id").drop_duplicates("item_id").set_index("item_id").tmdb_id
    scores = pd.read_csv(GENOME / "scores" / "tagdl.csv")
    M = scores.pivot(index="item_id", columns="tag", values="score").fillna(0.0)
    log(f"genome: {M.shape[0]:,} films x {M.shape[1]:,} tags; {M.index.isin(item_to_tmdb.index).sum():,} in the catalogue")

    kinds = pd.Series({t: tag_kind(t) for t in M.columns})
    zall = (M - M.mean()) / (M.std() + 1e-6)
    cat = M[M.index.isin(item_to_tmdb.index)]
    tmdb = item_to_tmdb.loc[cat.index].values
    keepable = kinds.isin(["theme", "opinion"])  # award and person tags are predictions, not facts
    rows = []
    for tid, (item, vals) in zip(tmdb, cat.iterrows()):
        z = zall.loc[item]
        ok = (vals >= min_score) & keepable & (z >= 0.5)
        top = z[ok].sort_values(ascending=False).head(show)
        rows += [(int(tid), t, round(float(vals[t]), 3), round(float(zv), 2), kinds[t]) for t, zv in top.items()]
    replace_table(con, "crowd_tags", pd.DataFrame(rows, columns=["tmdb_id", "tag", "score", "z", "kind"]),
                  [("tmdb_id",), ("tag",)])

    theme = kinds[kinds.isin(["theme", "name"])].index
    Z = M[theme].values.astype(np.float32)
    Z = (Z - Z.mean(0)) / (Z.std(0) + 1e-6)
    Zc = Z[M.index.isin(item_to_tmdb.index)]
    Zc /= np.linalg.norm(Zc, axis=1, keepdims=True)
    S = Zc @ Zc.T
    np.fill_diagonal(S, -1)
    rows = []
    for i, tid in enumerate(tmdb):
        top = np.argpartition(-S[i], keep)[:keep]
        top = top[np.argsort(-S[i, top])]
        rows += [(int(tid), int(tmdb[j]), r, round(float(S[i, j]), 4)) for r, j in enumerate(top, 1)]
    replace_table(con, "genome_similar", pd.DataFrame(rows, columns=["tmdb_id", "similar_id", "rank", "score"]),
                  [("tmdb_id",), ("similar_id",)])
    # Raw theme scores of catalogue films: training targets for predicting tags of other titles.
    VEC.mkdir(parents=True, exist_ok=True)
    np.save(VEC / "genome_targets.npy", M.loc[cat.index, theme].values.astype(np.float32))
    (VEC / "genome_targets.json").write_text(json.dumps({"tmdb_ids": [int(t) for t in tmdb], "tags": list(theme)}))
    con.commit()
    log(f"crowd tags for {len(tmdb):,} films ({len(theme)} theme tags used for similarity)")


# ---------- step: reviews ----------

STOP = set("""a about above after again against all am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have
having he her here hers herself him himself his how i if in into is it its itself just me more most my
myself no nor not now of off on once only or other our ours out over own same she should so some such than
that the their theirs them themselves then there these they this those through to too under until up very
was we were what when where which while who whom why will with would you your yours yourself also even
really much many one two get got gets make makes made way well still back thing things lot quite rather
see seen saw watch watched watching movie movies film films""".split())


def _review_phrases(texts):
    """Count 1-3 word phrases per review (each once) and how often each word is capitalised."""
    counts, caps, seen = {}, {}, {}
    for text in texts:
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", text)
        low = [w.lower() for w in words]
        for w, lw in zip(words, low):
            seen[lw] = seen.get(lw, 0) + 1
            if w[0].isupper():
                caps[lw] = caps.get(lw, 0) + 1
        grams = set()
        for n in (1, 2, 3):
            for i in range(len(low) - n + 1):
                g = low[i:i + n]
                if g[0] in STOP or g[-1] in STOP or (n == 1 and len(g[0]) < 4):
                    continue
                grams.add(" ".join(g))
        for g in grams:
            counts[g] = counts.get(g, 0) + 1
    counts = {g: c for g, c in counts.items() if c >= 2}
    proper = {w for w, n in seen.items() if n >= 3 and caps.get(w, 0) / n > 0.6}
    return counts, proper


def step_reviews(con, per_film=40, keep=15):
    """Distinctive phrases viewers use about each film (c-TF-IDF over IMDb reviews from the
    Tag Genome 2021 dataset). Only short phrases are stored; review text is not."""
    from multiprocessing import Pool

    meta = pd.read_json(GENOME / "raw" / "metadata.json", lines=True)[["item_id", "imdbId"]]
    meta["imdb_id"] = "tt" + meta.imdbId.astype(str).str.zfill(7)
    facts = pd.read_sql("SELECT tmdb_id, imdb_id FROM movie_facts WHERE imdb_id IS NOT NULL", con)
    item_to_tmdb = meta.merge(facts, on="imdb_id").drop_duplicates("item_id").set_index("item_id").tmdb_id.to_dict()
    texts = {}
    with open(GENOME / "raw" / "reviews.json", encoding="utf-8") as f:
        for line in f:
            item = int(line[12:line.index(",", 12)]) if line.startswith('{"item_id": ') else json.loads(line)["item_id"]
            tid = item_to_tmdb.get(item)
            if tid is None or len(texts.get(tid, ())) >= per_film:
                continue
            texts.setdefault(tid, []).append(json.loads(line)["txt"][:3000])
    log(f"reviews loaded for {len(texts):,} catalogue films")
    ids = list(texts)
    with Pool() as pool:
        results = pool.map(_review_phrases, [texts[t] for t in ids], chunksize=20)
    df = {}
    for counts, _ in results:
        for g in counts:
            df[g] = df.get(g, 0) + 1
    n_films = len(ids)
    names = {fold_text(r[0]) for r in con.execute(
        "SELECT director FROM movies WHERE director IS NOT NULL UNION SELECT actor FROM cast_members")}
    name_words = {w for n in names for w in n.split()}
    titles = dict(con.execute("SELECT tmdb_id, title FROM movies"))
    rows = []
    for tid, (counts, proper) in zip(ids, results):
        n = len(texts[tid])
        skip = proper | name_words | set(fold_text(titles.get(tid, "")).split())
        scored = []
        for g, c in counts.items():
            if c < max(3, 0.1 * n) or df[g] < 3:
                continue
            words = g.split()
            if any(w in skip or "'" in w for w in words):
                continue
            scored.append(((c / n) * np.log(n_films / df[g]) * (1 + 0.6 * (len(words) - 1)), g))
        chosen = []
        for _, g in sorted(scored, reverse=True):
            if any(g in c or c in g for c in chosen):
                continue
            chosen.append(g)
            if len(chosen) == keep:
                break
        if chosen:
            rows.append((int(tid), ", ".join(chosen), n))
    replace_table(con, "viewer_phrases", pd.DataFrame(rows, columns=["tmdb_id", "phrases", "reviews"]), [("tmdb_id",)])
    con.commit()
    log(f"viewer phrases for {len(rows):,} films")


def fold_text(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


# ---------- step: catalog ----------

ADULT = ("softcore", "pink film", "erotic movie", "sexploitation", "pornography")


def bayes(rating, votes, kind, known):
    """IMDb-style weighted rating per title type: m = median votes, C = mean rating of known titles."""
    out = pd.Series(np.nan, index=rating.index)
    for k in kind.unique():
        sel = (kind == k) & known
        m = float(votes[sel].median())
        c = float(rating[sel].mean())
        idx = (kind == k) & rating.notna()
        v = votes[idx].astype(float)
        out[idx] = (v * rating[idx] + m * c) / (v + m) - c + 6.5
        log(f"quality for {k}: m = {m:,.0f} votes, C = {c:.2f}")
    return out


def step_catalog(con, min_show_votes=50):
    """One table of movies and TV shows, with shared genres, quality score and full-text index."""
    movies = pd.read_sql("""
        SELECT m.tmdb_id AS id, m.title, m.year, m.language, m.director AS people, m.overview,
               m.rating AS tmdb_rating, m.popularity, m.poster_url, m.cluster, m.x, m.y,
               f.imdb_id, f.runtime, f.imdb_rating, f.imdb_votes,
               (SELECT group_concat(genre, ', ') FROM genres g WHERE g.tmdb_id = m.tmdb_id) AS genres,
               EXISTS (SELECT 1 FROM keywords k WHERE k.tmdb_id = m.tmdb_id AND k.keyword = 'short film') AS short_tag,
               EXISTS (SELECT 1 FROM keywords k WHERE k.tmdb_id = m.tmdb_id AND k.keyword IN %s) AS adult
        FROM movies m LEFT JOIN movie_facts f USING (tmdb_id)""" % (str(ADULT),), con)
    movies["kind"] = "movie"
    movies["key"] = "m" + movies.id.astype(str)
    movies["url"] = "https://www.themoviedb.org/movie/" + movies.id.astype(str)
    movies["short"] = (movies.short_tag == 1) | (movies.runtime.notna() & (movies.runtime < 40))
    has_imdb = movies.imdb_votes.fillna(0) >= 1
    movies["rating"] = np.where(has_imdb, movies.imdb_rating, movies.tmdb_rating.replace(0, np.nan))
    movies["rating_source"] = np.where(has_imdb, "IMDb", np.where(movies.rating.notna(), "TMDB", None))

    shows = pd.read_sql("SELECT * FROM shows", con)
    shows = shows[(shows.imdb_votes >= min_show_votes) | ((shows.weight >= 60) & shows.summary.notna())].copy()
    extra = {"Documentary": "Documentary", "Animation": "Animation", "Reality": "Reality"}
    shows["genres"] = [", ".join(dict.fromkeys(([extra[t]] if t in extra else []) + (g.split(", ") if isinstance(g, str) else [])))
                       or None for t, g in zip(shows.type, shows.genres)]
    has_imdb = shows.imdb_votes >= 1
    shows = shows.rename(columns={"tvmaze_id": "id", "summary": "overview", "image_url": "poster_url", "weight": "popularity"})
    shows["kind"] = "tv"
    shows["key"] = "t" + shows.id.astype(str)
    shows["rating"] = np.where(has_imdb, shows.imdb_rating, shows.tvmaze_rating)
    shows["rating_source"] = np.where(has_imdb, "IMDb", np.where(shows.tvmaze_rating.notna(), "TVmaze", None))
    shows["short"] = False
    shows["adult"] = False
    # Writers listed at series level on IMDb stand in for creators.
    writers = pd.read_sql("SELECT imdb_id, group_concat(name, ', ') AS people FROM "
                          "(SELECT imdb_id, name FROM credits WHERE role = 'writer' AND name IS NOT NULL) GROUP BY imdb_id", con)
    writers["people"] = writers.people.str.split(", ").str[:3].str.join(", ")
    shows = shows.merge(writers, on="imdb_id", how="left")

    cols = ["key", "kind", "id", "imdb_id", "title", "year", "end_year", "status", "language", "country", "network",
            "people", "genres", "overview", "runtime", "rating", "rating_source", "imdb_votes", "popularity",
            "short", "adult", "poster_url", "url", "cluster", "x", "y"]
    cat = pd.concat([movies.reindex(columns=cols), shows.reindex(columns=cols)], ignore_index=True)
    cat = cat.rename(columns={"imdb_votes": "votes"})
    cat["votes"] = cat.votes.fillna(0).astype(int)
    for col in ("year", "end_year", "runtime", "cluster"):
        cat[col] = pd.to_numeric(cat[col], errors="coerce").round().astype("Int64")
    cat["short"] = cat.short.astype(bool).astype(int)
    cat["adult"] = cat.adult.astype(bool).astype(int)
    known = cat.votes >= 100
    score = bayes(cat.rating.where(cat.votes >= 1), cat.votes, cat.kind, known)
    # Titles without IMDb votes: shrink the TMDB or TVmaze rating hard toward the middle.
    weak = score.isna() & cat.rating.notna()
    conf = np.log1p(cat.popularity.fillna(0)) / (np.log1p(cat.popularity.fillna(0)) + 6.0)
    conf = conf.where(cat.rating < 9.0, conf * 0.25)
    score[weak] = conf[weak] * cat.rating[weak] + (1 - conf[weak]) * 6.0
    cat["score"] = score.fillna(5.5).round(2)
    cat.insert(0, "rid", np.arange(1, len(cat) + 1))

    con.execute("DROP TABLE IF EXISTS catalog")
    con.execute("""CREATE TABLE catalog (rid INTEGER PRIMARY KEY, key TEXT UNIQUE, kind TEXT, id INTEGER,
        imdb_id TEXT, title TEXT, year INTEGER, end_year INTEGER, status TEXT, language TEXT, country TEXT,
        network TEXT, people TEXT, genres TEXT, overview TEXT, runtime INTEGER, rating REAL, rating_source TEXT,
        votes INTEGER, popularity REAL, short INTEGER, adult INTEGER, poster_url TEXT, url TEXT, cluster INTEGER,
        x REAL, y REAL, score REAL)""")
    cat.to_sql("catalog", con, if_exists="append", index=False)
    for col in ("kind", "id", "imdb_id", "year", "score", "votes"):
        con.execute(f"CREATE INDEX catalog_{col} ON catalog({col})")
    genres = cat[["rid", "genres"]].dropna()
    genres = genres.assign(genre=genres.genres.str.split(", ")).explode("genre")[["rid", "genre"]]
    replace_table(con, "catalog_genres", genres, [("rid",), ("genre",)])

    # Full-text index: plot, TMDB keywords, crowd tags, viewer phrases, people.
    tags = pd.read_sql("""
        SELECT c.rid,
          (SELECT group_concat(keyword, ' | ') FROM keywords k WHERE c.kind = 'movie' AND k.tmdb_id = c.id) AS keywords,
          (SELECT group_concat(tag, ' | ') FROM crowd_tags t WHERE c.kind = 'movie' AND t.tmdb_id = c.id) AS crowd,
          (SELECT phrases FROM viewer_phrases v WHERE c.kind = 'movie' AND v.tmdb_id = c.id) AS phrases,
          (SELECT group_concat(actor, ' | ') FROM cast_members a WHERE c.kind = 'movie' AND a.tmdb_id = c.id) AS cast_names
        FROM catalog c""", con)
    con.execute("DROP TABLE IF EXISTS catalog_fts")
    con.execute("CREATE VIRTUAL TABLE catalog_fts USING fts5(title, overview, tags, people, genres, "
                "tokenize = 'porter unicode61 remove_diacritics 2')")
    full = cat.merge(tags, on="rid")
    full["alltags"] = full[["keywords", "crowd", "phrases"]].apply(lambda r: " | ".join(x for x in r if isinstance(x, str) and x), axis=1)
    full["allpeople"] = full[["people", "cast_names"]].apply(lambda r: " | ".join(x for x in r if isinstance(x, str) and x), axis=1)
    con.executemany("INSERT INTO catalog_fts(rowid, title, overview, tags, people, genres) VALUES (?, ?, ?, ?, ?, ?)",
                    full[["rid", "title", "overview", "alltags", "allpeople", "genres"]].astype(object)
                    .where(full[["rid", "title", "overview", "alltags", "allpeople", "genres"]].notna(), None)
                    .itertuples(index=False, name=None))
    con.commit()
    log(f"catalog: {(cat.kind == 'movie').sum():,} movies and {(cat.kind == 'tv').sum():,} shows")


def step_aka(con):
    """Other titles from IMDb (its international and original titles) where they differ from the
    catalogue's: "Money Heist" for La Casa de Papel, "Erased" for Boku Dake ga Inai Machi. They
    are matched by `find` and title references, and added to the full-text title field."""
    basics = read_imdb("title.basics", ["tconst", "primaryTitle", "originalTitle"])
    cat = pd.read_sql("SELECT rid, imdb_id, title FROM catalog WHERE imdb_id IS NOT NULL", con)
    m = cat.merge(basics, left_on="imdb_id", right_on="tconst")
    rows = []
    for rid, title, primary, original in m[["rid", "title", "primaryTitle", "originalTitle"]].itertuples(index=False):
        seen = {norm_title(title)}
        for aka in (primary, original):
            if isinstance(aka, str) and norm_title(aka) and norm_title(aka) not in seen:
                seen.add(norm_title(aka))
                rows.append((int(rid), aka))
    akas = pd.DataFrame(rows, columns=["rid", "aka"])
    replace_table(con, "title_aka", akas, [("rid",)])
    titles = dict(con.execute("SELECT rid, title FROM catalog"))
    con.executemany("UPDATE catalog_fts SET title = ? WHERE rowid = ?",
                    [(" | ".join([titles[r]] + list(g.aka)), r) for r, g in akas.groupby("rid")])
    con.commit()
    log(f"other titles for {akas.rid.nunique():,} titles")


# ---------- step: tmdb ----------

def step_tmdb(con):
    """TMDB extras from scripts/fetch_tmdb.py: keywords for shows, TMDB recommendations, age
    ratings, trailers, seasons and episodes, creators, and streaming providers."""
    records = []
    for name in ("films", "shows"):
        path = RAW / "tmdb" / f"{name}.jsonl"
        if path.exists():
            for line in path.open(encoding="utf-8"):
                d = json.loads(line)
                if d["data"]:
                    records.append((d["key"], d["data"]))
    cat = pd.read_sql("SELECT rid, key, kind, id FROM catalog", con)
    rid_of = dict(zip(cat.key, cat.rid))
    by_tmdb = {("movie", int(k[1:])): r for k, r in rid_of.items() if k.startswith("m")}
    by_tmdb.update({("tv", d["tmdb_id"]): rid_of[k] for k, d in records if d["kind"] == "tv" and k in rid_of})
    extra, kws, recs, prov = [], [], [], []
    for key, d in records:
        rid = rid_of.get(key)
        if rid is None:
            continue
        c = d.get("certificates") or {}
        extra.append({
            "rid": rid, "tmdb_id": d["tmdb_id"], "tagline": d.get("tagline") or None,
            "cert_us": c.get("US"), "cert_gb": c.get("GB"), "cert_fr": c.get("FR"),
            "seasons": d.get("seasons"), "episodes": d.get("episodes"), "tmdb_status": d.get("status"),
            "created_by": ", ".join(d.get("created_by") or []) or None, "trailer": d.get("trailer"),
            "collection": d.get("collection"), "tmdb_votes": d.get("vote_count"), "tmdb_rating": d.get("vote_average"),
        })
        kws += [(rid, k.lower()) for k in d.get("keywords") or []]
        r = 0
        for other in d.get("recommendations") or []:
            o = by_tmdb.get((d["kind"], other))
            if o is not None:
                r += 1
                recs.append((rid, o, r))
        for region, types in (d.get("providers") or {}).items():
            for t, names in types.items():
                prov += [(rid, region, t, n) for n in names]
    replace_table(con, "title_extra", pd.DataFrame(extra), [("rid",)])
    replace_table(con, "tmdb_keywords", pd.DataFrame(kws, columns=["rid", "keyword"]), [("rid",), ("keyword",)])
    replace_table(con, "tmdb_recs", pd.DataFrame(recs, columns=["rid", "other", "rank"]), [("rid",), ("other",)])
    replace_table(con, "providers", pd.DataFrame(prov, columns=["rid", "region", "type", "provider"]),
                  [("rid",), ("region", "provider")])
    con.commit()
    log(f"TMDB extras for {len(extra):,} titles; {len(kws):,} keywords, {len(recs):,} recommendation links, "
        f"{len(prov):,} provider rows")


# ---------- step: vibes ----------

VIBE_STOP = set("""the and that this with was for but his her are they you have not one all film movie its from
what who there their which about out just more like when can than been also has had were very would into only them
some even well much really story good great time see make made way get could will most other being any after over
then because did does how where why these those still while through such own should""".split())
# For browsable clusters (Mike Guinness's "Flick" method): nationality, language and franchise
# words make reviews cluster by culture or series instead of by vibe.
NATIONALITY = set("""american british english french german italian spanish japanese korean chinese indian russian
mexican canadian australian irish scottish swedish danish norwegian finnish dutch polish turkish arabic greek
brazilian argentine hindi bollywood hollywood europe european asian african america britain england france germany
italy spain japan korea china india russia mexico canada australia hong kong tamil telugu persian iranian israeli
hebrew thai vietnamese filipino indonesian egyptian nigerian swiss austrian belgian portuguese czech hungarian
romanian ukrainian soviet yiddish latin dub dubbed subtitles subtitled""".split())
FRANCHISE = set("""star wars bond james marvel batman superman spider man wolverine potter harry hobbit ring rings
bourne rocky rambo alien aliens terminator matrix toy story shrek pixar disney avengers trek""".split())


def step_vibes(con, per_film=40, words=300):
    """Review "vibe" vectors: each film's IMDb reviews (Tag Genome 2021) reduced to the 300
    words most of its reviews use, then embedded. `vibes.npy` drops filler and the film's own
    title words (best for similar titles: +41% MovieLens recall@20 blended with plot vectors);
    `vibes_clean.npy` also keeps only English dictionary words and drops names, nationality and
    franchise words, which is better for browsable clusters."""
    from sentence_transformers import SentenceTransformer

    meta = pd.read_json(GENOME / "raw" / "metadata.json", lines=True)[["item_id", "imdbId"]]
    meta["imdb_id"] = "tt" + meta.imdbId.astype(str).str.zfill(7)
    facts = pd.read_sql("SELECT tmdb_id, imdb_id FROM movie_facts WHERE imdb_id IS NOT NULL", con)
    item_to_tmdb = meta.merge(facts, on="imdb_id").drop_duplicates("item_id").set_index("item_id").tmdb_id.to_dict()
    reviews = {}
    with open(GENOME / "raw" / "reviews.json", encoding="utf-8") as f:
        for line in f:
            t = item_to_tmdb.get(int(line[12:line.index(",", 12)]))
            if t is not None and len(reviews.get(t, ())) < per_film:
                reviews.setdefault(t, []).append(json.loads(line)["txt"])
    english = {w.strip() for w in open("/usr/share/dict/words") if w.strip().islower()}
    names = set()
    for (n,) in con.execute("SELECT director FROM movies WHERE director IS NOT NULL UNION SELECT actor FROM cast_members"):
        names |= set(re.findall(r"[a-z]+", n.lower()))
    titles = dict(con.execute("SELECT tmdb_id, title FROM movies"))
    rid_of = dict(con.execute("SELECT id, rid FROM catalog WHERE kind = 'movie'"))

    def summary(t, texts, strict):
        own = set(re.findall(r"[a-z]+", titles.get(t, "").lower()))
        counts = Counter()
        for text in texts:
            toks = {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in VIBE_STOP and w not in own}
            if strict:
                toks = {w for w in toks if w in english and w not in names and w not in NATIONALITY and w not in FRANCHISE}
            counts.update(toks)
        return " ".join(w for w, _ in counts.most_common(words))

    films = [t for t in reviews if t in rid_of]
    model = SentenceTransformer(MODEL, device=device())
    VEC.mkdir(parents=True, exist_ok=True)
    for strict, name in ((False, "vibes"), (True, "vibes_clean")):
        texts = [summary(t, reviews[t], strict) for t in films]
        V = model.encode(texts, batch_size=32, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        np.save(VEC / f"{name}.npy", V.astype(np.float16))
        log(f"{name}: {len(films):,} films")
    np.save(VEC / "vibes_rids.npy", np.array([rid_of[t] for t in films], dtype=np.int32))


# ---------- step: vectors ----------

MODEL = "Qwen/Qwen3-Embedding-0.6B"


def device():
    """Where to run the embedding model: an NVIDIA GPU, Apple's GPU, else the CPU (much slower)."""
    import torch
    if torch.cuda.is_available():
        return "cuda"
    return "mps" if torch.backends.mps.is_available() else "cpu"


def top_k(S, k):
    idx = np.argpartition(-S, k, axis=1)[:, :k]
    order = np.argsort(-np.take_along_axis(S, idx, axis=1), axis=1)
    return np.take_along_axis(idx, order, axis=1)


def neighbours(V, k=100, chunk=2048, rows=None, cols=None):
    """Top-k cosine neighbours of each row in `rows` among the rows in `cols` (default: all,
    self excluded), for a row-normalised matrix. Returns indices into V."""
    rows = np.arange(len(V)) if rows is None else rows
    cols = np.arange(len(V)) if cols is None else cols
    out = np.empty((len(rows), k), dtype=np.int32)
    target = V[cols].T
    for start in range(0, len(rows), chunk):
        idx = rows[start:start + chunk]
        S = V[idx] @ target
        if len(cols) == len(V):
            S[np.arange(len(idx)), idx] = -np.inf
        out[start:start + chunk] = cols[top_k(S, k)]
    return out


def load_wiki():
    """English Wikipedia introductions by IMDb id (scripts/fetch_wikipedia.py)."""
    folder = RAW / "wikipedia"
    if not (folder / "intros.jsonl").exists():
        return {}
    links = {}
    for line in (folder / "articles.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        if d["article"]:
            links[d["imdb_id"]] = d["article"]
    intros = {}
    for line in (folder / "intros.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        if d["intro"]:
            intros[d["article"]] = d["intro"]
    return {i: intros[a] for i, a in links.items() if a in intros}


# Sentences about production, release and reception say little about what a story is like.
WIKI_NOISE = re.compile(
    r"\b(produc|premier|releas|box office|gross|nominat|won |wins |award|academy|golden globe|emmy|bafta|"
    r"festival|budget|critic|acclaim|reviews?\b|rotten|metacritic|distribut|filming|principal photography|"
    r"renewed|cancel|season \d|episodes? (?:was|were|aired)|aired|broadcast|streaming|sequel|spin-?off|"
    r"box-office|commercial|rating|ranked|listed|considered one of|cult following|soundtrack|score was)", re.I)


def wiki_text(intro, title, limit=900):
    """The premise in a Wikipedia introduction: the genre phrase of its first sentence ("a 2014
    American psychological drama film") and the sentences describing the story, with production,
    release and reception sentences dropped and the title blanked out."""
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(intro.split()))
    if not sentences:
        return ""
    first = re.split(r"\b(?:written|directed|created|starring|produced|developed|based on)\b", sentences[0])[0]
    keep = [first.strip().rstrip(",")] + [x for x in sentences[1:] if not WIKI_NOISE.search(x)]
    text = " ".join(k for k in keep if k)[:limit]
    if isinstance(title, str) and len(title) >= 3:
        text = re.sub(r"\b" + re.escape(title) + r"\b", "it", text)
    return text


def title_texts(con, with_themes=True, wiki=False, years=True, keywords=True):
    """The text embedded for each catalogue title, in rid order."""
    cat = pd.read_sql("SELECT rid, kind, id, imdb_id, title, year, end_year, network, genres, overview "
                      "FROM catalog ORDER BY rid", con)
    wiki_map = load_wiki() if wiki else {}
    themes = pd.read_sql("SELECT tmdb_id AS id, group_concat(tag, ', ') AS themes FROM "
                         "(SELECT tmdb_id, tag FROM crowd_tags WHERE kind = 'theme' ORDER BY score DESC) GROUP BY tmdb_id", con)
    kws = pd.read_sql("SELECT tmdb_id AS id, group_concat(keyword, ', ') AS keywords FROM keywords GROUP BY tmdb_id", con)
    phrases = pd.read_sql("SELECT tmdb_id AS id, phrases FROM viewer_phrases", con)
    movies = cat.kind == "movie"
    extra = (cat[movies][["rid", "id"]].merge(themes, on="id", how="left").merge(kws, on="id", how="left")
             .merge(phrases, on="id", how="left").set_index("rid"))
    # Shows have no site keywords; TMDB's (step tmdb) fill the gap.
    tv_keywords = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'tmdb_keywords'").fetchone():
        tv_keywords = dict(con.execute(
            "SELECT k.rid, group_concat(k.keyword, ', ') FROM tmdb_keywords k JOIN catalog c ON c.rid = k.rid "
            "WHERE c.kind = 'tv' GROUP BY k.rid").fetchall())

    def text(r):
        # The title is left out on purpose: with it, vectors matched titles that share a word
        # ("Severance" next to "Sever", "Dark" next to "In the Dark") instead of similar stories.
        x = extra.loc[r.rid] if r.rid in extra.index else None
        year = lambda v: str(int(v)) if pd.notna(v) else ""  # noqa: E731
        span = year(r.year) + (f"-{year(r.end_year)}" if r.kind == "tv" else "")
        parts = [f"{'TV series' if r.kind == 'tv' else 'Film'}" + (f" ({span})." if years else ".")]
        if isinstance(r.genres, str):
            parts.append(f"Genres: {r.genres}.")
        if isinstance(r.overview, str):
            overview = r.overview[:1200]
            if isinstance(r.title, str) and len(r.title) >= 3:
                overview = re.sub(r"\b" + re.escape(r.title) + r"\b", "it", overview)
            parts.append(overview)
        if isinstance(r.imdb_id, str) and r.imdb_id in wiki_map:
            parts.append("Background: " + wiki_text(wiki_map[r.imdb_id], r.title))
        if x is not None:
            if with_themes and isinstance(x.themes, str):
                parts.append("Themes: " + ", ".join(x.themes.split(", ")[:12]) + ".")
            if keywords and isinstance(x.keywords, str):
                parts.append("Keywords: " + ", ".join(x.keywords.split(", ")[:12]) + ".")
            if isinstance(x.phrases, str):
                parts.append(f"Viewers mention: {x.phrases}.")
        elif r.rid in tv_keywords:
            parts.append("Keywords: " + ", ".join(tv_keywords[r.rid].split(", ")[:12]) + ".")
        return " ".join(parts)

    return cat, [text(r) for r in cat.itertuples(index=False)]


def step_vectors(con):
    """Embed every title (plot, genres, crowd themes or keywords, review vocabulary)."""
    from sentence_transformers import SentenceTransformer

    cat, docs = title_texts(con)
    model = SentenceTransformer(MODEL, device=device())
    log(f"embedding {len(docs):,} titles with {MODEL}")
    E = model.encode(docs, batch_size=64, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    VEC.mkdir(parents=True, exist_ok=True)
    np.save(VEC / "content.npy", E.astype(np.float16))
    np.save(VEC / "content_rids.npy", cat.rid.values.astype(np.int32))
    log("titles embedded")
    meta = json.loads((VEC / "genome_targets.json").read_text())
    tag_vecs = model.encode(meta["tags"], normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    np.save(VEC / "tag_vectors.npy", tag_vecs.astype(np.float16))


def step_premise(con):
    """Premise vectors: the story sentences of each title's English Wikipedia introduction
    (wiki_text), embedded on their own. They feed the similar-titles blend only: there they raised
    MovieLens recall@20 by 15% for films without crowd data and 4% for crowd-tagged ones, while
    the same text added to the plot vectors lowered theme search by 3% (scripts/evaluate.py)."""
    from sentence_transformers import SentenceTransformer

    wiki = load_wiki()
    cat = pd.read_sql("SELECT rid, imdb_id, title FROM catalog ORDER BY rid", con)
    cat = cat[cat.imdb_id.isin(list(wiki))]
    texts = [wiki_text(wiki[i], t) for i, t in zip(cat.imdb_id, cat.title)]
    keep = [k for k, x in enumerate(texts) if x]
    model = SentenceTransformer(MODEL, device=device())
    log(f"embedding {len(keep):,} Wikipedia premises")
    P = model.encode([texts[k] for k in keep], batch_size=64, normalize_embeddings=True, convert_to_numpy=True,
                     show_progress_bar=False)
    np.save(VEC / "premise.npy", P.astype(np.float16))
    np.save(VEC / "premise_rids.npy", cat.rid.values[keep].astype(np.int32))


def step_tags(con):
    """Predict crowd theme scores for titles without Tag Genome data (ridge regression on plot
    vectors). Training films are re-embedded without their own crowd themes, so the model learns
    from the same kind of text it is applied to."""
    from sentence_transformers import SentenceTransformer

    cat, docs = title_texts(con, with_themes=False)
    movies = cat.kind == "movie"
    E = np.load(VEC / "content.npy").astype(np.float32)
    Y = np.load(VEC / "genome_targets.npy")
    meta = json.loads((VEC / "genome_targets.json").read_text())
    rid_of_movie = dict(zip(cat[movies].id, cat[movies].rid))
    row_of_rid = {r: i for i, r in enumerate(cat.rid)}
    rows = np.array([row_of_rid[rid_of_movie[t]] for t in meta["tmdb_ids"]])
    model = SentenceTransformer(MODEL, device=device())
    X = model.encode([docs[i] for i in rows], batch_size=64, normalize_embeddings=True,
                     convert_to_numpy=True, show_progress_bar=False).astype(np.float64)
    log(f"re-embedded {len(rows):,} training films without their crowd themes")
    rng = np.random.default_rng(0)
    test = rng.random(len(rows)) < 0.2
    best = None
    for alpha in (0.3, 1.0, 3.0, 10.0):
        Xt = X[~test]
        W = np.linalg.solve(Xt.T @ Xt + alpha * np.eye(X.shape[1]), Xt.T @ (Y[~test] - Y[~test].mean(0)))
        P = X[test] @ W + Y[~test].mean(0)
        corr = np.mean([np.corrcoef(P[i], Y[test][i])[0, 1] for i in range(len(P))])
        base = np.mean([np.corrcoef(Y[~test].mean(0), Y[test][i])[0, 1] for i in range(test.sum())])
        log(f"tag prediction alpha={alpha}: held-out per-film correlation {corr:.3f} (average-profile baseline {base:.3f})")
        if best is None or corr > best[0]:
            best = (corr, alpha)
    alpha = best[1]
    W = np.linalg.solve(X.T @ X + alpha * np.eye(X.shape[1]), X.T @ (Y - Y.mean(0)))
    T = np.clip(E.astype(np.float64) @ W + Y.mean(0), 0, 1).astype(np.float32)
    T[rows] = Y  # real crowd scores where they exist
    has_genome = np.zeros(len(cat), dtype=bool)
    has_genome[rows] = True
    np.save(VEC / "tags.npy", T.astype(np.float16))
    np.save(VEC / "tags_real.npy", has_genome)
    (VEC / "tag_names.json").write_text(json.dumps(meta["tags"]))

    tag_rows = []
    names = meta["tags"]
    mu, sd = Y.mean(0), Y.std(0) + 1e-6
    shown = np.array([tag_kind(t) == "theme" for t in names])
    for i in np.flatnonzero(~has_genome):
        z = np.where(shown, (T[i] - mu) / sd, -np.inf)
        top = np.argsort(-z)[:12]
        tag_rows += [(int(cat.rid[i]), names[j], round(float(T[i, j]), 3), round(float(z[j]), 2))
                     for j in top if z[j] >= 1.25 and T[i, j] >= 0.25]
    replace_table(con, "predicted_tags", pd.DataFrame(tag_rows, columns=["rid", "tag", "score", "z"]),
                  [("rid",), ("tag",)])
    con.commit()
    log(f"predicted tags for {len(set(r[0] for r in tag_rows)):,} titles (alpha={alpha})")


# ---------- step: similar ----------

# Weight of TMDB's own recommendations in the blend, set from scripts/evaluate.py-style
# measurements against MovieLens fans (see references/data.md). 0 leaves them out.
TMDB_REC_WEIGHT = 0.0


def step_similar(con, k=200, keep=50):
    """Blend crowd, tag, plot, people and site signals into one similar-titles list per title."""
    cat = pd.read_sql("SELECT rid, kind, id, imdb_id, genres, score FROM catalog ORDER BY rid", con)
    n = len(cat)
    rids = cat.rid.values
    pos = {r: i for i, r in enumerate(rids)}
    movie_pos = {t: pos[r] for t, r, k_ in zip(cat.id, cat.rid, cat.kind) if k_ == "movie"}

    is_tv = (cat.kind == "tv").values
    film_rows, tv_rows = np.flatnonzero(~is_tv), np.flatnonzero(is_tv)

    def cross(V, kc):
        """For each title, its nearest titles of the other kind."""
        out = np.empty((n, kc), dtype=np.int32)
        out[film_rows] = neighbours(V, kc, rows=film_rows, cols=tv_rows)
        out[tv_rows] = neighbours(V, kc, rows=tv_rows, cols=film_rows)
        return out

    E = np.load(VEC / "content.npy").astype(np.float32)
    plot_nb, plot_x = neighbours(E, k), cross(E, k)
    del E
    log("plot neighbours done")
    T = np.load(VEC / "tags.npy").astype(np.float32)
    real = np.load(VEC / "tags_real.npy")
    Z = (T - T[real].mean(0)) / (T[real].std(0) + 1e-6)
    Z /= np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9
    tag_nb = neighbours(Z, k)
    del T, Z
    log("tag neighbours done")
    # Review vibes (films with IMDb reviews only): nearest among the films that have them.
    vibe_nb = {}
    if (VEC / "vibes.npy").exists():
        V = np.load(VEC / "vibes.npy").astype(np.float32)
        vrows = np.array([pos[r] for r in np.load(VEC / "vibes_rids.npy")])
        for i, nb in zip(vrows, neighbours(V, k)):
            vibe_nb[int(i)] = vrows[nb]
        del V
        log(f"vibe neighbours done ({len(vibe_nb):,} films)")
    # Wikipedia premises (titles with an English article): nearest among them, same kind and
    # cross-over. Both lists get it, or same-kind scores would shrink next to cross-over ones.
    premise_nb, premise_x = {}, {}
    if (VEC / "premise.npy").exists():
        P = np.load(VEC / "premise.npy").astype(np.float32)
        prows = np.array([pos[r] for r in np.load(VEC / "premise_rids.npy")])
        tv_p, film_p = np.flatnonzero(is_tv[prows]), np.flatnonzero(~is_tv[prows])
        for mine, other in ((tv_p, film_p), (film_p, tv_p)):
            for i, nb, nx in zip(prows[mine], neighbours(P[mine], k), neighbours(P, k, rows=mine, cols=other)):
                premise_nb[int(i)] = prows[mine][nb]
                premise_x[int(i)] = prows[nx]
        del P
        log(f"premise neighbours done ({len(premise_nb):,} titles)")

    def ranked(pairs):
        lists = {}
        for a, b, r in pairs:
            if a in movie_pos and b in movie_pos:
                lists.setdefault(movie_pos[a], []).append((r, movie_pos[b]))
        return {a: [b for _, b in sorted(v)] for a, v in lists.items()}

    crowd = ranked(con.execute("SELECT tmdb_id, similar_id, rank FROM crowd_similar"))
    tmdb_recs = {}
    if TMDB_REC_WEIGHT and con.execute("SELECT 1 FROM sqlite_master WHERE name = 'tmdb_recs'").fetchone():
        for rid, other, rank in con.execute("SELECT rid, other, rank FROM tmdb_recs ORDER BY rid, rank"):
            if rid in pos and other in pos:
                tmdb_recs.setdefault(pos[rid], []).append(pos[other])
    site = ranked(con.execute("SELECT tmdb_id, similar_id, rank FROM similar"))
    likes = {movie_pos[t]: n_ for t, n_ in con.execute("SELECT tmdb_id, ml_likes FROM crowd_stats") if t in movie_pos}
    # Series rated on MovieLens: their fans' other series join `crowd`; the films they loved, and
    # the series fans of a film loved, make a cross-over crowd list.
    crowd_x = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'crowd_series'").fetchone():
        for rid, other in con.execute("SELECT rid, other FROM crowd_series ORDER BY rid, rank"):
            if rid in pos and other in pos:
                i, j = pos[rid], pos[other]
                (crowd if is_tv[i] == is_tv[j] else crowd_x).setdefault(i, []).append(j)
        likes.update({pos[r]: n_ for r, n_ in con.execute("SELECT rid, ml_likes FROM crowd_series_stats") if r in pos})

    # People: shared directors and writers (IMDb), plus top-billed cast for films.
    people = {}
    for imdb, nconst, role in con.execute("SELECT imdb_id, nconst, role FROM credits"):
        people.setdefault(imdb, {})[nconst] = 1.0
    cast = {}
    for t, actor in con.execute("SELECT tmdb_id, actor FROM cast_members WHERE billing < 3"):
        if t in movie_pos:
            cast.setdefault(movie_pos[t], set()).add(actor)
    title_people = [dict() for _ in range(n)]
    for i, imdb in enumerate(cat.imdb_id):
        if isinstance(imdb, str) and imdb in people:
            title_people[i].update(people[imdb])
    for i, names in cast.items():
        for a in names:
            title_people[i]["cast:" + a] = 0.5
    index = {}
    for i, ps in enumerate(title_people):
        for p in ps:
            index.setdefault(p, []).append(i)
    index = {p: v for p, v in index.items() if len(v) <= 150}  # prolific names say little

    quality = cat.groupby("kind").score.rank(pct=True).values
    # Documentaries and reality TV only sit well next to their own kind.
    factual = cat.genres.fillna("").str.contains("Documentary|Reality").values

    def blend(i, signals, weights, keep_n, same_kind):
        total = sum(weights.values())
        parts = {}
        for name, lst in signals.items():
            lst = [j for j in lst if (is_tv[j] == is_tv[i]) == same_kind and j != i]
            L = max(len(lst), 1)
            for r, j in enumerate(lst):
                parts.setdefault(int(j), {})[name] = 1 - r / L
        scored = sorted(((sum(weights[s] * v for s, v in ps.items()) / total * (0.6 + 0.4 * quality[j])
                          * (0.5 if factual[j] != factual[i] else 1.0), j, ps)
                         for j, ps in parts.items()), key=lambda x: -x[0])
        return scored[:keep_n]

    out = []
    for i in range(n):
        signals, weights = {}, {}
        if i in crowd:
            signals["crowd"] = crowd[i]
            weights["crowd"] = 0.35 * likes.get(i, 0) / (likes.get(i, 0) + 200)
        signals["tags"] = tag_nb[i]
        weights["tags"] = 0.30 if real[i] else 0.25  # predicted tags proved as useful as plot
        signals["plot"] = plot_nb[i]
        weights["plot"] = 0.25
        if i in vibe_nb:  # +41% MovieLens recall@20 when blended with plot (scripts/evaluate.py)
            signals["vibes"] = vibe_nb[i]
            weights["vibes"] = 0.25
        if i in premise_nb:  # +15% recall@20 without crowd data, +4% with it (scripts/evaluate.py blend)
            signals["premise"] = premise_nb[i]
            weights["premise"] = 0.25
        shared = {}
        for p, w in title_people[i].items():
            for j in index.get(p, ()):
                if j != i:
                    shared[j] = shared.get(j, 0) + w
        people_list = [j for j, _ in sorted(shared.items(), key=lambda x: -x[1])]
        if people_list:
            signals["people"] = people_list[:k]
            weights["people"] = 0.10
        if i in site:
            signals["site"] = site[i]
            weights["site"] = 0.05
        if i in tmdb_recs:
            signals["tmdb"] = tmdb_recs[i]
            weights["tmdb"] = TMDB_REC_WEIGHT
        rows_same = blend(i, signals, weights, keep, True)
        # Cross-over: the blend restricted to the other kind, without tags: across films and
        # series they hurt (series -> films recall@20 0.042 without them, 0.023 with them;
        # scripts/evaluate.py cross), since series only have tags predicted from their plot.
        xs = {"plot": plot_x[i]}
        xw = {"plot": 0.25}
        other_people = [j for j in people_list if is_tv[j] != is_tv[i]]
        if other_people:
            xs["people"], xw["people"] = other_people[:k], 0.10
        if i in premise_x:
            xs["premise"], xw["premise"] = premise_x[i], 0.25
        if i in crowd_x:
            xs["crowd"], xw["crowd"] = crowd_x[i], weights.get("crowd", 0.35 * likes.get(i, 0) / (likes.get(i, 0) + 200))
        rows_cross = blend(i, xs, xw, 20, False)
        for is_cross, group in ((0, rows_same), (1, rows_cross)):
            for r, (sc, j, ps) in enumerate(group, 1):
                out.append((int(rids[i]), int(rids[j]), r, is_cross, round(sc, 4), round(ps.get("crowd", 0), 2),
                            round(ps.get("tags", 0), 2), round(ps.get("plot", 0), 2), round(ps.get("people", 0), 2),
                            round(ps.get("vibes", 0), 2), round(ps.get("tmdb", 0), 2), round(ps.get("premise", 0), 2)))
        if i % 20000 == 0:
            log(f"blended {i:,} of {n:,}")
    sim = pd.DataFrame(out, columns=["rid", "other", "rank", "cross", "score", "crowd", "tags", "plot", "people", "vibes",
                                     "tmdb", "premise"])
    replace_table(con, "hybrid_similar", sim, [("rid",), ("other",)])
    con.commit()
    log(f"hybrid neighbours: {len(sim):,} rows")


# ---------- step: clusters ----------

# Places and nationalities: left out of cluster features and labels so clusters group by vibe,
# not by country (wars and "corporate america" stay: they are subjects, not origins).
GEO_TAGS = {
    "africa", "alaska", "argentina", "australia", "berlin", "better than the american version", "bollywood",
    "boston", "brazil", "british", "canada", "chicago", "china", "cuba", "east germany", "egypt", "england",
    "france", "french", "german", "germany", "hawaii", "hong kong", "india", "iran", "ireland", "irish",
    "irish accent", "israel", "italian", "italy", "japan", "japanese", "las vegas", "latin america", "london",
    "los angeles", "mexico", "middle east", "new jersey", "new orleans", "new york", "new york city", "paris",
    "rome", "russia", "russian", "san francisco", "scotland", "south africa", "south america", "spain",
    "spanish", "swedish", "tokyo", "vienna", "vietnam",
}

def step_clusters(con, min_votes=1000, k=15, top_target=36):
    """Two-level "vibe" clusters (after Mike Guinness's "cluster the clusters"): Leiden
    communities on a k-nearest-neighbour graph of crowd-tag profiles (real or predicted), first
    over all well-known titles, then again inside each community. Labels are the themes each
    cluster has most above the whole catalogue (top level) or above its parent (sub-clusters).
    Lesser-known titles join the sub-cluster whose centre is nearest."""
    import igraph as ig
    import leidenalg as la

    cat = pd.read_sql("SELECT rid, key, kind, title, year, votes FROM catalog ORDER BY rid", con)
    T = np.load(VEC / "tags.npy").astype(np.float32)
    real = np.load(VEC / "tags_real.npy")
    names = json.loads((VEC / "tag_names.json").read_text())
    geo = np.array([t in GEO_TAGS for t in names])
    shown = np.array([tag_kind(t) == "theme" for t in names]) & ~geo
    Z = (T - T[real].mean(0)) / (T[real].std(0) + 1e-6)
    Z[:, geo] = 0
    # Shows only have predicted tags, which sit systematically apart from films' crowd tags;
    # centring each kind on its own mean keeps clusters about vibes rather than medium.
    is_tv = (cat.kind == "tv").values
    F = Z.copy()
    for mask in (is_tv, ~is_tv):
        F[mask] -= F[mask].mean(0)
    F /= np.linalg.norm(F, axis=1, keepdims=True) + 1e-9
    core = np.flatnonzero(cat.votes.values >= min_votes)
    log(f"clustering {len(core):,} titles with {min_votes}+ votes")

    def graph(members):
        nb = neighbours(F, k, rows=members, cols=members)
        local = {g: i for i, g in enumerate(members)}
        edges = {}
        for a, row in zip(members, nb):
            for b in row:
                w = float(F[a] @ F[b])
                if w > 0:
                    edges[tuple(sorted((local[a], local[int(b)])))] = w
        g = ig.Graph(n=len(members), edges=list(edges))
        g.es["weight"] = list(edges.values())
        return g

    def leiden(g, target):
        best = None
        for res in (0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0):
            part = la.find_partition(g, la.RBConfigurationVertexPartition, weights="weight",
                                     resolution_parameter=res, seed=7)
            big = sum(1 for c in part if len(c) >= max(10, g.vcount() // 200))
            if best is None or abs(big - target) < abs(best[0] - target):
                best = (big, part.membership)
        return np.array(best[1])

    def label(members, against):
        lift = np.where(shown, Z[members].mean(0) - against, -np.inf)
        return [names[j] for j in np.argsort(-lift)[:3]]

    top = leiden(graph(core), top_target)
    overall = Z[core].mean(0)
    rows, assign = [], {}
    order = sorted(set(top), key=lambda c: -(top == c).sum())
    for n1, c in enumerate(order, 1):
        members = core[top == c]
        if len(members) < 15:
            continue
        tags1 = label(members, overall)
        examples = cat.iloc[members].sort_values("votes", ascending=False)
        rows.append((str(n1), None, 1, " · ".join(tags1), len(members),
                     float((cat.kind.values[members] == "tv").mean()), json.dumps(list(examples.key[:6]))))
        target = int(np.clip(len(members) / 150, 2, 10))
        sub = leiden(graph(members), target) if len(members) >= 60 else np.zeros(len(members), dtype=int)
        parent_mean = Z[members].mean(0)
        sub_order = sorted(set(sub), key=lambda c2: -(sub == c2).sum())
        for n2, c2 in enumerate(sub_order, 1):
            sm = members[sub == c2]
            sid = f"{n1}.{n2}"
            ex = cat.iloc[sm].sort_values("votes", ascending=False)
            rows.append((sid, str(n1), 2, " · ".join(label(sm, parent_mean) if len(sub_order) > 1 else tags1),
                         len(sm), float((cat.kind.values[sm] == "tv").mean()), json.dumps(list(ex.key[:6]))))
            for g in sm:
                assign[int(g)] = (str(n1), sid, 1)
    # Lesser-known titles: nearest sub-cluster centre.
    subs = [r for r in rows if r[2] == 2]
    centres = np.stack([F[[g for g, v in assign.items() if v[1] == r[0]]].mean(0) for r in subs])
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    rest = np.array([i for i in range(len(cat)) if i not in assign])
    for start in range(0, len(rest), 5000):
        chunk = rest[start:start + 5000]
        best = np.argmax(F[chunk] @ centres.T, axis=1)
        for g, b in zip(chunk, best):
            assign[int(g)] = (subs[b][1], subs[b][0], 0)
    clusters = pd.DataFrame(rows, columns=["id", "parent", "level", "label", "core_titles", "tv_share", "examples"])
    counts = Counter(v[1] for v in assign.values())
    counts1 = Counter(v[0] for v in assign.values())
    clusters["titles"] = [counts1[i] if lvl == 1 else counts[i] for i, lvl in zip(clusters.id, clusters.level)]
    replace_table(con, "vibe_clusters", clusters, [("id",), ("parent",)])
    tv = pd.DataFrame([(int(cat.rid.values[g]), v[0], v[1], v[2]) for g, v in assign.items()],
                      columns=["rid", "vibe", "subvibe", "core"])
    replace_table(con, "title_vibes", tv, [("rid",), ("vibe",), ("subvibe",)])
    con.commit()
    log(f"{(clusters.level == 1).sum()} vibe clusters, {(clusters.level == 2).sum()} sub-clusters")


# In order: crowd links series by their catalogue ids, so it runs after catalog.
STEPS = {"imdb": step_imdb, "tv": step_tv, "crew": step_crew, "genome": step_genome, "reviews": step_reviews,
         "catalog": step_catalog, "crowd": step_crowd, "aka": step_aka, "tmdb": step_tmdb, "vibes": step_vibes, "vectors": step_vectors, "premise": step_premise, "tags": step_tags,
         "similar": step_similar, "clusters": step_clusters}


def main():
    steps = sys.argv[1:] or list(STEPS)
    con = sqlite3.connect(DB)
    for name in steps:
        if name not in STEPS:
            sys.exit(f"unknown step {name!r}; choose from {', '.join(STEPS)}")
        log(f"step {name}")
        STEPS[name](con)
    con.close()


if __name__ == "__main__":
    main()
