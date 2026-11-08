# Watch Next: a local film and TV catalogue for agents

A local catalogue of about 37,000 films and 42,000 TV shows that an agent can query to find and
recommend titles for the user. It started as an offline copy of
https://mikeyguinness.com/movies/ ("Movie Vector Map") and was enriched with IMDb facts,
TVmaze shows, MovieLens crowd data and local plot vectors. README.md covers installing it.

For any movie or TV request, use the `movie-finder` skill in `skills/movie-finder/`
(`$movie-finder` in Codex, `/movie-finder` in Claude Code). Its `SKILL.md` covers the workflow,
and `references/data.md` covers the tables, signals and SQL.

## Layout

- `install.sh`: the one-command build. It checks requirements (`--check`), builds everything,
  skipping finished steps (markers in `data/.build/`, log in `data/install.log`), and links the
  skill (`--link`).
- `requirements.txt`: the build environment, at the tested versions.
- `.env.example`: the template for `.env`.
- `data/movies.db`: the SQLite catalogue.
- `data/vectors/`: plot vectors, crowd/predicted tag matrices, review vibe and Wikipedia premise
  vectors.
- `data/profile.json`: the user's taste profile (created on first use by the skill).
- `data/raw/`: downloaded sources, untouched: the site's JSON, `imdb/`, `movielens/`
  (ml-32m), `genome/` (Tag Genome 2021, including IMDb reviews), `tvmaze/`, `tmdb/`, `wikipedia/`.
- `scripts/build_db.py`: downloads the site data and builds the film tables.
- `scripts/fetch_tvmaze.py`: downloads the TVmaze show index (resumable).
- `scripts/fetch_wikipedia.py`: downloads English Wikipedia introductions (via Wikidata, by IMDb
  id) into `data/raw/wikipedia/`; resumable and throttled, most-voted titles first.
- `scripts/fetch_tmdb.py`: downloads TMDB details (keywords, age ratings, seasons, creators,
  trailers, streaming, recommendations) for every title into `data/raw/tmdb/`; resumable. Reads
  `TMDB_READ_TOKEN` and `WATCH_REGIONS` from the environment or `.env` (private: never commit,
  print or read it aloud).
- `scripts/enrich.py`: everything else. Run it with the project environment.
- `scripts/evaluate.py`: scores the search against human ground truth (Tag Genome themes,
  MovieLens neighbours for films and for the series MovieLens rates, an "excellence" case). Run
  it before and after any change to the vectors, the text or the ranking, and keep only what
  measurably helps.
- `skills/movie-finder/`: the skill, linked into `~/.codex/skills` and `~/.claude/skills`.
- `boards/`: HTML poster boards and share images the skill writes, all in the house style
  (`skills/movie-finder/assets/house-style.css`, described in its `references/style.md`).
- `.venv/`: Python 3.12 with numpy, scipy, pandas, pyarrow, torch, sentence-transformers,
  igraph, leidenalg.

## Rebuilding

`./install.sh` does all of this. By hand, in this order (Wikipedia and TMDB look titles up in
the catalogue, so they run after its first half):

```bash
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -I scripts/build_db.py
.venv/bin/python -I scripts/fetch_tvmaze.py
.venv/bin/python -I scripts/enrich.py imdb tv crew genome reviews catalog crowd aka
.venv/bin/python -I scripts/fetch_wikipedia.py
.venv/bin/python -I scripts/fetch_tmdb.py
.venv/bin/python -I scripts/enrich.py tmdb vibes vectors premise tags similar clusters
.venv/bin/python -I scripts/evaluate.py text plot plot+wiki
.venv/bin/python -I scripts/evaluate.py blend
.venv/bin/python -I scripts/evaluate.py cross
```

The other raw files come from https://datasets.imdbws.com/ (`title.basics`, `title.ratings`,
`title.crew`, `name.basics`), https://files.grouplens.org/datasets/movielens/ml-32m.zip and
https://files.grouplens.org/datasets/tag-genome-2021/genome_2021.zip, each unzipped in its own
folder under `data/raw/`. `build_db.py` recreates `movies.db`, so run `enrich.py` after it.
`enrich.py` runs the embedding model on an NVIDIA GPU, Apple's GPU or the CPU, whichever it finds.

## Licences

Everything here is for personal, non-commercial use: TMDB data via the site, IMDb datasets,
MovieLens and the Tag Genome (research, non-commercial; don't redistribute), TVmaze and
Wikipedia (CC BY-SA: credit them when showing their text). The code is MIT; the data isn't.
Don't publish the database, the vectors or the raw files, and never commit `data/` or `.env`.
