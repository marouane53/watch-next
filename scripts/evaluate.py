"""Benchmark the catalogue's search against human ground truth.

  themes   For ~80 Tag Genome themes ("chess", "genius", "time travel"…), search by the theme's
           name and score the ranking of the 5,262 crowd-tagged films against their crowd
           relevance: nDCG@50 (graded) and recall@100 (films scored >= 0.8). Films' own crowd
           themes are kept out of their text, so the test can't read the answer.
  similar  Share of a film's 20 "fans also loved" neighbours (MovieLens) found among its 20
           nearest text vectors (recall@20), over 1,500 films.
  blend    The same recall@20 for the similar-titles blend (tags, plot, vibes) with and without
           the Wikipedia premise signal, with 95% bootstrap intervals.
  cross    Series -> films cross-over lists: share of the films MovieLens fans of a series also
           loved (the ~120 series rated there) found in its cross-over top 20.
  case     Ranks, among all 78,802 titles, of 30 hand-picked "excellence / extraordinary talent"
           titles, using the production vectors (data/vectors/content.npy).
  ask      The same 30 titles through the real `ask` command, called four ways: the user's words,
           with themes, with six imagined plot summaries, and with both.

Usage (project environment):
    .venv/bin/python -I scripts/evaluate.py text plot plot+wiki   # A/B text variants (themes, similar)
    .venv/bin/python -I scripts/evaluate.py case <label>           # case study on production vectors
    .venv/bin/python -I scripts/evaluate.py ask <label>            # the excellence case through `ask`
    .venv/bin/python -I scripts/evaluate.py blend                  # the similar-titles blend, ± premise
    .venv/bin/python -I scripts/evaluate.py cross                  # series -> films cross-over lists
Text variants are embedded once and cached in data/vectors/eval/; results accumulate in
data/vectors/eval/results.json.
"""
import importlib.util
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "movies.db"
VEC = ROOT / "data" / "vectors"
CACHE = VEC / "eval"
MODEL = "Qwen/Qwen3-Embedding-0.6B"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


enrich = load("enrich", ROOT / "scripts" / "enrich.py")
movies = load("movies", ROOT / "skills" / "movie-finder" / "scripts" / "movies.py")

GOLD = [
    ("movie", "Whiplash", 2014), ("movie", "Searching for Bobby Fischer", 1993), ("movie", "Amadeus", 1984),
    ("movie", "Good Will Hunting", 1997), ("movie", "Billy Elliot", 2000), ("movie", "Black Swan", 2010),
    ("movie", "Pawn Sacrifice", 2015), ("movie", "Queen of Katwe", 2016), ("movie", "Gifted", 2017),
    ("movie", "The Imitation Game", 2014), ("movie", "A Beautiful Mind", 2001), ("movie", "Little Man Tate", 1991),
    ("movie", "The Theory of Everything", 2014), ("movie", "Hidden Figures", 2016), ("movie", "Ford v Ferrari", 2019),
    ("movie", "Rush", 2013), ("movie", "Ratatouille", 2007), ("movie", "Free Solo", 2018), ("movie", "The Prestige", 2006),
    ("movie", "X+Y", 2014), ("movie", "The Man Who Knew Infinity", 2016), ("movie", "Akeelah and the Bee", 2006),
    ("tv", "Extraordinary Attorney Woo", 2022), ("tv", "Halt and Catch Fire", 2014), ("tv", "Hikaru no Go", 2001),
    ("tv", "Haikyuu!!", 2014), ("tv", "The Bear", 2022), ("tv", "The Last Dance", 2020), ("tv", "Chef's Table", 2015),
    ("movie", "The Social Network", 2010),
]
CASE_QUERIES = ["excellence and extraordinary abilities",
                "the obsessive pursuit of greatness by a genius with an extraordinary talent"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def embed_docs(variant, texts):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{variant.replace('+', '_')}_{len(texts)}.npy"
    if path.exists():
        return np.load(path).astype(np.float32)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL, device=enrich.device())
    log(f"embedding {len(texts):,} texts for variant {variant}")
    E = model.encode(texts, batch_size=32, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    np.save(path, E.astype(np.float16))
    return E.astype(np.float32)


def queries(texts):
    return np.stack([np.array(movies.embed_query(DB, t), dtype=np.float32) for t in texts])


def ndcg(gains_in_order, all_gains, k):
    disc = 1 / np.log2(np.arange(2, k + 2))
    dcg = (gains_in_order[:k] * disc).sum()
    idcg = (np.sort(all_gains)[::-1][:k] * disc).sum()
    return dcg / idcg if idcg else 0.0


def save(results):
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / "results.json"
    previous = json.loads(out.read_text()) if out.exists() else {}
    for k, v in results.items():
        previous.setdefault(k, {}).update(v)
    out.write_text(json.dumps(previous, indent=1))
    print(pd.DataFrame(previous).T.to_string(float_format=lambda v: f"{v:.3f}"))


def crowd_sample(con, tmdb_ids):
    """1,500 crowd-tagged films with their MovieLens "fans also loved" top 20 (at least 10 of
    them crowd-tagged too): the ground truth for similar titles."""
    crowd = pd.read_sql("SELECT tmdb_id, similar_id FROM crowd_similar WHERE rank <= 20", con)
    ids = set(tmdb_ids)
    crowd = crowd[crowd.tmdb_id.isin(ids) & crowd.similar_id.isin(ids)]
    by_film = crowd.groupby("tmdb_id").similar_id.apply(set)
    return by_film[by_film.map(len) >= 10].sample(1500, random_state=7)


def text_benchmarks(variants):
    con = sqlite3.connect(DB)
    meta = json.loads((VEC / "genome_targets.json").read_text())
    Y = np.load(VEC / "genome_targets.npy")
    tags = meta["tags"]
    cat = pd.read_sql("SELECT rid, kind, id FROM catalog ORDER BY rid", con)
    row_of_movie = {int(t): i for i, (k, t) in enumerate(zip(cat.kind, cat.id)) if k == "movie"}
    rows = np.array([row_of_movie[t] for t in meta["tmdb_ids"]])

    kinds = [enrich.tag_kind(t) for t in tags]
    strong = (Y >= 0.8).sum(0)
    usable = [j for j, t in enumerate(tags) if kinds[j] == "theme" and 15 <= strong[j] <= 400]
    picked = sorted(np.random.default_rng(7).choice(usable, size=min(80, len(usable)), replace=False))
    log(f"themes: {len(picked)} over {len(rows):,} crowd-tagged films")
    Q = queries([tags[j] for j in picked])

    sample = crowd_sample(con, meta["tmdb_ids"])
    idx_of = {t: i for i, t in enumerate(meta["tmdb_ids"])}

    results = {}
    for variant in variants:
        _, texts = enrich.title_texts(con, with_themes=False, wiki="wiki" in variant, years="noyear" not in variant,
                                      keywords="nokw" not in variant)
        E = embed_docs(variant, [texts[i] for i in rows])
        S = Q @ E.T
        nd, rec = [], []
        for qi, j in enumerate(picked):
            order = np.argsort(-S[qi])
            gains = np.where(Y[:, j] >= 0.5, Y[:, j], 0.0)
            nd.append(ndcg(gains[order], gains, 50))
            rel = Y[:, j] >= 0.8
            rec.append(rel[order[:100]].sum() / rel.sum())
        sims = E @ E.T
        np.fill_diagonal(sims, -1)
        hits = [len({meta["tmdb_ids"][k] for k in np.argsort(-sims[idx_of[t]])[:20]} & neigh) / len(neigh)
                for t, neigh in sample.items()]
        results[variant] = {"theme nDCG@50": float(np.mean(nd)), "theme recall@100": float(np.mean(rec)),
                            "similar recall@20": float(np.mean(hits))}
        log(f"{variant}: {results[variant]}")
    save(results)


def case_study(label):
    con = sqlite3.connect(DB)
    E = np.load(VEC / "content.npy").astype(np.float32)
    rids = np.load(VEC / "content_rids.npy")
    gold = []
    for kind, title, year in GOLD:
        r = con.execute("SELECT rid FROM catalog WHERE kind = ? AND title = ? AND year BETWEEN ? AND ?",
                        (kind, title, year - 1, year + 1)).fetchone()
        if r:
            gold.append(r[0])
    out = {}
    for qtext, q in zip(CASE_QUERIES, queries(CASE_QUERIES)):
        order = np.argsort(-(E @ q))
        rank = {int(rids[i]): k + 1 for k, i in enumerate(order)}
        ranks = sorted(rank[g] for g in gold)
        short = "words" if qtext.startswith("excellence") else "theme"
        out[f"case ({short}) top200"] = int(sum(r <= 200 for r in ranks))
        out[f"case ({short}) median rank"] = int(np.median(ranks))
    save({f"production {label}": out})


def blend_benchmark(k=200, reps=2000):
    """The similar-titles blend signal by signal, scored as step_similar scores it, over the
    crowd-tagged films: MovieLens recall@20 with and without the Wikipedia premise, once with the
    films' real crowd tags and review vibes, once as if they had predicted tags only (5-fold ridge,
    like step_tags). Crowd links are the answer, so they stay out. 95% intervals by paired
    bootstrap over films."""
    con = sqlite3.connect(DB)
    meta = json.loads((VEC / "genome_targets.json").read_text())
    ids, Y = meta["tmdb_ids"], np.load(VEC / "genome_targets.npy").astype(np.float32)
    cat, texts = enrich.title_texts(con, with_themes=False)
    films = cat.assign(pos=np.arange(len(cat)))[cat.kind == "movie"].set_index("id")
    rids = films.rid.loc[ids].values
    quality = pd.read_sql("SELECT id, score FROM catalog WHERE kind = 'movie'", con).set_index("id").score.rank(pct=True)
    quality = quality.loc[ids].values
    E = embed_docs("plot", [texts[p] for p in films.pos.loc[ids]])
    n = len(ids)
    sample = crowd_sample(con, ids)
    idx = {t: i for i, t in enumerate(ids)}
    test = np.array([idx[t] for t in sample.index])
    truth = [np.array([idx[s] for s in sample[t]]) for t in sample.index]

    def subset(name):
        """Vectors stored for some titles (data/vectors/<name>.npy), mapped onto the test films."""
        V = np.load(VEC / f"{name}.npy").astype(np.float32)
        at = {r: j for j, r in enumerate(np.load(VEC / f"{name.split('_')[0]}_rids.npy"))}
        has = np.array([r in at for r in rids])
        out = np.zeros((n, V.shape[1]), dtype=np.float32)
        out[has] = V[[at[r] for r in rids[has]]]
        return out, has

    def norm_z(T):
        Z = (T - Y.mean(0)) / (Y.std(0) + 1e-6)
        return Z / (np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9)

    folds = np.random.default_rng(1).integers(0, 5, n)
    T_pred = np.zeros_like(Y)
    X = E.astype(np.float64)
    for f in range(5):
        tr = folds != f
        W = np.linalg.solve(X[tr].T @ X[tr] + np.eye(X.shape[1]), X[tr].T @ (Y[tr] - Y[tr].mean(0)))
        T_pred[~tr] = X[~tr] @ W + Y[tr].mean(0)
    P, has_p = subset("premise")
    V, has_v = subset("vibes")
    vectors = {"plot": (E, None), "tags": (norm_z(Y), None), "predicted tags": (norm_z(np.clip(T_pred, 0, 1)), None),
               "vibes": (V, has_v), "premise": (P, has_p)}
    lists, present = {}, {}
    for name, (M, has) in vectors.items():
        S = M[test] @ M.T
        S[np.arange(len(test)), test] = -np.inf
        if has is not None:
            S[:, ~has] = -np.inf
        lists[name] = np.argsort(-S, axis=1)[:, :k]
        present[name] = np.ones(n, bool) if has is None else has

    def recall(weights):
        out = np.zeros(len(test))
        for t, row in enumerate(test):
            sc = np.zeros(n)
            for s, w in weights.items():
                if present[s][row]:
                    sc[lists[s][t]] += w * (1 - np.arange(k) / k)
            sc *= 0.6 + 0.4 * quality
            sc[row] = -1
            out[t] = np.isin(truth[t], np.argpartition(-sc, 20)[:20]).mean()
        return out

    rng = np.random.default_rng(0)
    draws = rng.integers(0, len(test), (reps, len(test)))
    results = {}
    for label, base in (("crowd-tagged", {"tags": 0.30, "plot": 0.25, "vibes": 0.25}),
                        ("predicted tags", {"predicted tags": 0.25, "plot": 0.25})):
        a = recall(base)
        b = recall({**base, "premise": 0.25})
        d = (b - a)[draws].mean(1) / a.mean()
        results[f"blend ({label})"] = {"similar recall@20": float(a.mean()), "with premise": float(b.mean()),
                                       "gain": float(b.mean() / a.mean() - 1), "gain 95% low": float(np.percentile(d, 2.5)),
                                       "gain 95% high": float(np.percentile(d, 97.5))}
        log(f"{label}: recall@20 {a.mean():.3f} -> {b.mean():.3f} with premise "
            f"({b.mean() / a.mean() - 1:+.1%}, 95% {np.percentile(d, 2.5):+.1%} to {np.percentile(d, 97.5):+.1%})")
    save(results)


def cross_benchmark(k=200, reps=2000):
    """Series -> films cross-over lists against MovieLens: for the series MovieLens users rated
    (20+ likes, 10+ catalogue films among their fans' top 20), the share of those films found in
    the cross-over blend's top 20, scored as step_similar scores it, without crowd links (they are
    the answer). Variants change one signal's weight; 95% intervals by paired bootstrap."""
    con = sqlite3.connect(DB)
    cat = pd.read_sql("SELECT rid, kind, imdb_id, score, genres FROM catalog ORDER BY rid", con)
    rids = cat.rid.values
    pos = {r: i for i, r in enumerate(rids)}
    is_tv = (cat.kind == "tv").values
    films = np.flatnonzero(~is_tv)
    truth = {}
    for rid, other in con.execute("SELECT s.rid, s.other FROM crowd_series s JOIN crowd_series_stats t ON t.rid = s.rid "
                                  "WHERE t.ml_likes >= 20 AND s.rank <= 20 ORDER BY s.rid, s.rank"):
        if is_tv[pos[rid]] and not is_tv[pos[other]]:
            truth.setdefault(pos[rid], []).append(pos[other])
    test = np.array(sorted(i for i, v in truth.items() if len(v) >= 10))
    log(f"cross-over: {len(test)} series with MovieLens fans")
    quality = cat.groupby("kind").score.rank(pct=True).values
    factual = cat.genres.fillna("").str.contains("Documentary|Reality").values

    def lists_from(V, has=None):
        cols = films if has is None else films[has[films]]
        S = V[test] @ V[cols].T
        return cols[np.argsort(-S, axis=1)[:, :k]]

    E = np.load(VEC / "content.npy").astype(np.float32)
    T = np.load(VEC / "tags.npy").astype(np.float32)
    real = np.load(VEC / "tags_real.npy")
    Z = (T - T[real].mean(0)) / (T[real].std(0) + 1e-6)
    Z /= np.linalg.norm(Z, axis=1, keepdims=True) + 1e-9
    P = np.zeros_like(E)
    has_p = np.zeros(len(rids), bool)
    prow = np.array([pos[r] for r in np.load(VEC / "premise_rids.npy")])
    P[prow], has_p[prow] = np.load(VEC / "premise.npy").astype(np.float32), True
    # Tags centred per kind: series only have predicted tags, which sit apart from films' real ones.
    Zk = (T - T[real].mean(0)) / (T[real].std(0) + 1e-6)
    for rows in (np.flatnonzero(is_tv), films):
        Zk[rows] -= Zk[rows].mean(0)
    Zk /= np.linalg.norm(Zk, axis=1, keepdims=True) + 1e-9
    lists = {"plot": lists_from(E), "tags": lists_from(Z), "tags (per kind)": lists_from(Zk),
             "premise": lists_from(P, has_p)}
    del E, T, Z, Zk, P
    people = {}
    for imdb, nconst in con.execute("SELECT imdb_id, nconst FROM credits"):
        people.setdefault(imdb, set()).add(nconst)
    by_person = {}
    for i in films:
        for p in people.get(cat.imdb_id[i], ()):
            by_person.setdefault(p, []).append(i)
    lists["people"] = []
    for i in test:
        shared = Counter(j for p in people.get(cat.imdb_id[i], ()) if len(by_person.get(p, ())) <= 150
                         for j in by_person.get(p, ()))
        lists["people"].append(np.array([j for j, _ in shared.most_common(k)], dtype=int))

    def recall(weights):
        out = np.zeros(len(test))
        for t, i in enumerate(test):
            sc = np.zeros(len(rids))
            for s, w in weights.items():
                lst = lists[s][t]
                if s == "premise" and not has_p[i]:
                    continue
                if len(lst):
                    sc[lst] += w * (1 - np.arange(len(lst)) / len(lst))
            sc *= (0.6 + 0.4 * quality) * np.where(factual != factual[i], 0.5, 1.0)
            out[t] = np.isin(truth[i], np.argsort(-sc)[:20]).mean()
        return out

    base_w = {"tags": 0.25, "plot": 0.25, "people": 0.10}
    kind_w = {"tags (per kind)": 0.25, "plot": 0.25, "people": 0.10}
    variants = {"before (tags, plot, people)": base_w, "plot only": {"plot": 0.25},
                "tags only": {"tags": 0.25}, "tags per kind only": {"tags (per kind)": 0.25},
                "premise only": {"premise": 0.25}, "people only": {"people": 0.10},
                "plot + premise": {"plot": 0.25, "premise": 0.25},
                "+ premise (current)": {**base_w, "premise": 0.25},
                "tags per kind, plot, people": kind_w,
                "tags per kind, plot, people, premise": {**kind_w, "premise": 0.25},
                "plot + premise + people": {"plot": 0.25, "premise": 0.25, "people": 0.10},
                "plot + premise + people 0.05": {"plot": 0.25, "premise": 0.25, "people": 0.05},
                "plot + premise + tags 0.10": {"plot": 0.25, "premise": 0.25, "tags": 0.10}}
    res = {name: recall(w) for name, w in variants.items()}
    a = res["before (tags, plot, people)"]
    draws = np.random.default_rng(0).integers(0, len(test), (reps, len(test)))
    results = {}
    for name, b in res.items():
        d = (b - a)[draws].mean(1) / max(a.mean(), 1e-9)
        results[f"cross {name}"] = {"cross recall@20": float(b.mean()), "gain": float(b.mean() / a.mean() - 1),
                                    "gain 95% low": float(np.percentile(d, 2.5)),
                                    "gain 95% high": float(np.percentile(d, 97.5))}
        log(f"{name}: recall@20 {b.mean():.3f} ({b.mean() / a.mean() - 1:+.1%}, 95% {np.percentile(d, 2.5):+.1%} "
            f"to {np.percentile(d, 97.5):+.1%})")
    save(results)


HYDE = [
    "An orphan girl discovers a prodigious gift for chess and rises to challenge the world's best players while battling addiction.",
    "A young musician at an elite conservatory pushes himself to the breaking point under a ruthless teacher to become one of the greats.",
    "A brilliant self-taught mathematician struggles for recognition as his genius transforms his field.",
    "A gifted athlete trains obsessively and sacrifices everything to become a champion.",
    "A talented young cook fights to prove herself in a demanding kitchen where only perfection is accepted.",
    "A child prodigy is pushed by parents and mentors toward greatness while longing for a normal childhood.",
]
THEMES = "genius,obsession,competition,mentor"
# Famous titles that carry a "genius" or "obsession" tag but aren't about excellence: noise.
OFF_THEME = ["m77", "m539", "m27205", "m550", "m278", "m15", "m63", "m105", "m13"]


def ask_study(label, extra=()):
    import subprocess
    con = sqlite3.connect(DB)
    gold = set()
    for kind, title, year in GOLD:
        r = con.execute("SELECT key FROM catalog WHERE kind = ? AND title = ? AND year BETWEEN ? AND ?",
                        (kind, title, year - 1, year + 1)).fetchone()
        if r:
            gold.add(r[0])
    also = [x for h in HYDE[1:] for x in ("--also", h)]
    calls = {
        "user's words": ["excellence and extraordinary abilities"],
        "words + themes": ["excellence and extraordinary abilities", "--themes", THEMES],
        "6 summaries": [HYDE[0]] + also,
        "6 summaries + themes": [HYDE[0]] + also + ["--themes", THEMES],
    }
    out = {}
    for name, args in calls.items():
        res = subprocess.run([sys.executable, "-I", str(ROOT / "skills/movie-finder/scripts/movies.py"), "ask"] + args
                             + list(extra) + ["--json", "--limit", "200", "--include-seen"], capture_output=True, text=True)
        keys = [d["key"] for d in json.loads(res.stdout or "[]")]
        ranks = [keys.index(k) + 1 for k in gold if k in keys]
        noise = sum(1 for k in OFF_THEME if k in keys[:30])
        for k in (10, 20, 50):
            out[f"ask {name}: top{k}"] = sum(r <= k for r in ranks)
        out[f"ask {name}: noise in top30"] = noise
        log(f"{name}: " + ", ".join(f"top{k} {sum(r <= k for r in ranks)}" for k in (10, 20, 50, 200))
            + f", off-theme famous films in top 30: {noise}")
    save({f"ask {label}": out})


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "text"
    if mode == "case":
        case_study(sys.argv[2] if len(sys.argv) > 2 else "current")
    elif mode == "ask":
        ask_study(sys.argv[2] if len(sys.argv) > 2 else "current", sys.argv[3:])
    elif mode == "blend":
        blend_benchmark()
    elif mode == "cross":
        cross_benchmark()
    else:
        text_benchmarks(sys.argv[2:] or ["plot", "plot+wiki"])
