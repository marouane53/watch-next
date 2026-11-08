# The data

A local catalogue of about 37,000 films and 42,000 TV shows in the project's `data/movies.db`,
with vectors in `data/vectors/`. The counts below come from a November 2026 build; `info` gives
the ones for this build. `./install.sh` builds it from files it downloads into `data/raw/`:

1. `scripts/build_db.py` downloads the Movie Vector Map site data
   (https://mikeyguinness.com/movies/) and builds the film tables.
2. `scripts/enrich.py` adds IMDb facts, TV shows, crowd data, the unified catalogue, vectors and
   blended similarity. Steps can run alone: `imdb tv crew genome reviews catalog crowd aka tmdb
   vibes vectors premise tags similar clusters`. `vectors` takes about 25 minutes on an M4 Max.
   `scripts/fetch_wikipedia.py` and `scripts/fetch_tmdb.py` run between `aka` and `tmdb`,
   because they look titles up in the catalogue.

## Sources

| Source | What it gives | Coverage | Licence |
|---|---|---|---|
| Movie Vector Map (TMDB data) | Films: plot, genres, keywords, top-5 cast, director, poster, 2D map, 20 neighbours | 36,933 films, snapshot from the build date | TMDB terms, personal use |
| IMDb datasets | Rating, vote count, runtime, directors and writers for films and shows | 26,489 films linked, most shows | Personal, non-commercial |
| MovieLens 32M | "Fans also loved" (EASE over 16M likes, 4+ stars) | 7,579 catalogue films and 126 series, ratings to 2023 | Research and non-commercial |
| Tag Genome 2021 | Crowd tag scores (1,084 tags) and IMDb review text | 5,262 catalogue films | CC BY-NC 3.0 |
| TVmaze | TV shows: summary, genres, network, status, years, episode length, poster | 41,869 shows kept | CC BY-SA 4.0: credit TVmaze |
| TMDB API | Keywords (shows had none), age ratings, seasons and episodes, creators, trailers, streaming providers, TMDB recommendations | 70,518 titles | TMDB terms, personal use; token in `.env` |
| English Wikipedia (via Wikidata) | Introductions, used only for the premise signal in similar titles | 15,876 films and 21,563 shows (99.6% of films and 96% of shows with 10k+ votes) | CC BY-SA 4.0: credit Wikipedia |
| Qwen3-Embedding-0.6B | Plot vectors for every title, tag-name vectors | All titles | Apache 2.0 |

## Tables

| Table | Contents |
|---|---|
| `catalog` | One row per title. `rid` (integer id), `key` (`m<tmdb_id>` or `t<tvmaze_id>`), `kind` (`movie`/`tv`), `id`, `imdb_id`, `title`, `year`, `end_year`, `status` (TV: Running/Ended), `language` (ISO 639-1, original language), `country` and `network` (TV), `people` (film director, or up to three series writers), `genres`, `overview`, `runtime` (minutes; episode length for TV), `rating` + `rating_source` (IMDb when it has votes, else TMDB or TVmaze), `votes` (IMDb), `popularity` (TMDB traffic for films, TVmaze weight for shows), `score`, `short`, `adult`, `poster_url`, `url`, `cluster`, `x`, `y` (films only) |
| `catalog_genres` | `rid`, `genre` |
| `catalog_fts` | FTS5 over `title`, `overview`, `tags` (TMDB keywords, crowd tags, review vocabulary), `people`, `genres`; `rowid` is `rid` |
| `hybrid_similar` | `rid`, `other`, `cross` (0: same kind, ranks 1–50; 1: films↔series cross-over, ranks 1–20), `rank`, `score`, and the signal parts `crowd`, `tags`, `plot`, `people`, `vibes`, `tmdb` (unused), `premise` (0–1, rank-based) |
| `vibe_clusters` | `id` ("21", or "21.1" for a sub-cluster), `parent`, `level`, `label` (its most distinctive crowd themes), `core_titles`, `titles`, `tv_share`, `examples` (keys) |
| `title_vibes` | `rid`, `vibe`, `subvibe`, `core` (1 when the title was clustered, 0 when assigned to the nearest sub-cluster) |
| `crowd_similar` | MovieLens EASE neighbours: `tmdb_id`, `similar_id`, `rank`, `score` |
| `crowd_stats` | `tmdb_id`, `ml_ratings`, `ml_likes`, `ml_mean` (out of 5) |
| `crowd_series`, `crowd_series_stats` | The same for the series MovieLens rates, matched by IMDb id: `rid`, `other`, `rank` (within the other title's kind), `score`; and `rid`, `ml_ratings`, `ml_likes`, `ml_mean`. Holds each series' nearest series and films, and each film's nearest series |
| `title_aka` | `rid`, `aka`: IMDb's international and original titles where they differ from the catalogue's (15,822 titles), matched by `find` and title references and added to the full-text title |
| `crowd_tags` | Tag Genome: `tmdb_id`, `tag`, `score` (0–1), `z` (how unusual the score is for that tag), `kind` (`theme` or `opinion`, e.g. "great soundtrack", "boring"). Tags with score ≥ 0.5 and z ≥ 0.5, top 25 by z |
| `predicted_tags` | `rid`, `tag`, `score`, `z`: up to 12 distinctive themes (z ≥ 1, score ≥ 0.4) predicted from plot vectors for titles without crowd tags |
| `genome_similar` | Films with similar crowd-tag profiles: `tmdb_id`, `similar_id`, `rank`, `score` |
| `viewer_phrases` | `tmdb_id`, `phrases` (distinctive words from up to 40 IMDb reviews), `reviews` |
| `movies`, `genres`, `keywords`, `cast_members`, `similar`, `clusters` | The original site data, films only, keyed by `tmdb_id` |
| `movie_facts` | `tmdb_id`, `imdb_id`, `movielens_id`, `imdb_match` (`movielens` or `title+year`), `runtime`, `imdb_rating`, `imdb_votes` |
| `shows`, `show_genres` | Raw TVmaze shows (all 76k, before filtering) |
| `credits` | IMDb directors and writers: `imdb_id`, `nconst`, `role`, `name` |
| `meta` | `source`, `built_on`, `movies` (site snapshot) |
| `title_extra` | `rid`, `tmdb_id`, `tagline`, `cert_us`, `cert_gb`, `cert_fr`, `seasons`, `episodes`, `tmdb_status`, `created_by`, `trailer` (YouTube key), `collection`, `tmdb_votes`, `tmdb_rating` |
| `tmdb_keywords` | `rid`, `keyword` (series and films) |
| `providers` | `rid`, `region` (the build's `WATCH_REGIONS`; by default FR, US, GB, MA, BE, CA, DE, ES), `type` (flatrate, free, ads, rent, buy), `provider` |
| `tmdb_recs` | `rid`, `other`, `rank`: TMDB's recommendations within the catalogue (not used in the blend, see below) |

Vectors in `data/vectors/`: `vibes.npy` and `vibes_clean.npy` (review vibe vectors for the
8,761 films with IMDb reviews) with `vibes_rids.npy`; `content.npy` (1024-d plot vectors, float16) with `content_rids.npy`;
`tags.npy` (crowd or predicted tag scores per title) with `tags_real.npy` and `tag_names.json`;
`tag_vectors.npy` (vectors of the tag names, for matching moods to tags); `premise.npy` with
`premise_rids.npy` (Wikipedia premise vectors for the 37,439 titles with an English article).

## How the signals are made

- **Fans also loved (`crowd`):** EASE (Steck 2019, λ=500) on MovieLens likes (4+ stars), users
  with 5+ likes, films with 20+ likes. Weighted by n/(n+200) likes in the blend, so thin
  evidence counts less. The 126 series MovieLens rates (matched by IMDb id) get the same: their
  fans' series in their own list, their fans' films in their cross-over list, and films' fans'
  series in the films' cross-over lists.
- **Same feel (`tags`):** cosine over z-scored Tag Genome theme tags (opinion tags excluded).
  Titles without crowd tags use tags predicted from their plot vector by ridge regression,
  trained on crowd-tagged films re-embedded without their tags (the `tags` step prints the
  held-out accuracy against an average-profile baseline).
- **Story (`plot`):** cosine between plot vectors. Each title's text is its kind and years,
  genres, plot (with the title blanked out), crowd themes or TMDB keywords, and review
  vocabulary. Titles are left out because they dominated the vectors: "Severance" sat next to
  "Sever", "Dark" next to "In the Dark".
- **Review vibes (`vibes`):** each film's IMDb reviews reduced to the 300 words most of them
  use (filler and the film's own title words removed), embedded. Blended 50/50 with plot
  vectors they recover 41% more of the films MovieLens fans co-love (recall@20 0.142 vs 0.101).
  The Flick-style cleaning (English dictionary words only; no names, nationalities or franchise
  words) scored lower for that (0.119) but is kept in `vibes_clean.npy` for clustering.
- **Wikipedia premise (`premise`):** the story sentences of a title's English Wikipedia
  introduction (the genre phrase of its first sentence, then the sentences that aren't about
  production, release or reception, title blanked), embedded on their own and compared with
  titles of the same kind. It is a separate signal rather than part of the plot text because
  in the plot text it helped similar titles but hurt theme search (see below), so `ask` never
  sees it.
- **People:** shared IMDb directors and writers, plus the top three billed actors for films;
  names attached to more than 150 titles are ignored.
- **Blend:** each signal's top 200 turned into rank scores, weighted crowd 0.35 × n/(n+200),
  tags 0.30 (0.25 if predicted), plot 0.25, review vibes 0.25, Wikipedia premise 0.25, people
  0.10, site neighbours 0.05, renormalised
  over the signals a title has, then multiplied by 0.6 + 0.4 × the candidate's quality
  percentile, and halved when one side is a documentary or reality show and the other isn't.
  Each title's 20 cross-over neighbours (films for a series, series for a film) blend plot
  0.25, Wikipedia premise 0.25 and people 0.10, plus crowd for the series MovieLens rates. Tags
  are left out there: across the two kinds they hurt (see below). Lists aren't symmetric (Palm Springs lists About Time, not the
  reverse), so `recommend` also follows links pointing at the seed.
- **Score:** IMDb-style weighted rating per kind, `(v·R + m·C)/(v + m) − C + 6.5`, with m the
  median vote count of known titles (about 10,000 for films, 600 for shows) and C their mean
  rating. Without IMDb votes, the TMDB or TVmaze rating is shrunk hard toward 6.

## Vibe clusters

`enrich.py clusters` follows Mike Guinness's "cluster the clusters": Leiden communities on a
15-nearest-neighbour graph of crowd-tag profiles for the 24,339 titles with 1,000+ IMDb votes,
then Leiden again inside each community. Two fixes from his video and from testing: places and
nationalities are removed from the features and labels (otherwise clusters split by country),
and films and series are each centred on their own mean (otherwise they split by medium, since
shows only have predicted tags). Less-known titles join the nearest sub-cluster. The result is
35 clusters and about 150 sub-clusters.

## Measuring quality

`scripts/evaluate.py` scores the search against human ground truth, and every change above was
kept only if it helped:

| Test | Ground truth | Metric |
|---|---|---|
| Theme search | Tag Genome relevance of 80 themes over 5,262 films (films' own tags hidden) | nDCG@50, recall@100 |
| Similar titles | Each film's 20 MovieLens "fans also loved" neighbours | recall@20 |
| Similar-titles blend (`blend`) | The same, for the blend of tags, plot and vibes, with and without one more signal | recall@20, 95% bootstrap interval |
| Cross-over (`cross`) | The films MovieLens fans of each of 126 series also loved | recall@20 of the series' cross-over list, 95% interval |
| Excellence case | 30 hand-picked titles for "excellence and extraordinary abilities" | rank among 78,802 |

Findings: a film-and-TV query instruction beats the model's generic one (+6–10% nDCG);
agent-chosen crowd themes beat plot vectors for theme search (0.72 vs 0.58 nDCG) but automatic
word-to-tag mapping does not; several imagined plot summaries fused by rank beat one query;
removing the year from the text hurts similar-title recall (−20%), so it stays. In `ask`, a
soft gate on themes (credit × 1000/(1000 + plot rank)) took off-theme famous films in the top 30
of the excellence case from 7 to 0 while raising known fits in the top 20 from 8 to 10, and
counting each title's best rank across descriptions (instead of the sum) kept single-domain
matches from being drowned out.

Wikipedia introductions (5,248 of the 5,262 test films covered) help similar titles and hurt
theme search when added to the plot text: recall@20 0.101 → 0.118, theme nDCG 0.560 → 0.545.
As a separate premise signal they keep the gain without touching `ask`: in the blend, recall@20
rises 15% (95% interval +13% to +17%) for films scored with predicted tags, the case of most of
the catalogue, and 4% (+2.5% to +5.4%) for films that also have real crowd tags and reviews.
Weights 0.10, 0.15 and 0.25 were tried; 0.25 was best without crowd data and tied with it.
The gain is similar for less- and more-voted films. Series and the films↔series cross-over
can't be scored this way (MovieLens has no series), and the premise only joins the same-kind
lists. `ask` reads the similar lists only to drop near-duplicates; after the rebuild its
excellence recipe found 7, 9 and 11 known fits in the top 10, 20 and 50 (10 in the top 50
before), still with no off-theme noise.

Side effect: a title without an English article can't score on the premise, so lists now lean
toward titles that have one, which are better known. Among the top 10 neighbours of titles with
an article, the share that also have one rose from 78% to 86% for films (median IMDb votes
14k → 23k) and from 72% to 82% for series (652 → 886 votes); lists of titles without an article
didn't change. The MovieLens gain isn't this tilt: nearly every film in that test has an
article. For hidden gems, filter with `--max-votes`. By eye, a quarter of a series' top 10
changed, mostly toward closer premises: The Wire gained Homicide: Life on the Street, Fleabag
gained Crashing, Chernobyl gained other disaster series (Estonia, Heweliusz). Some good fits
dropped out (Forbrydelsen from True Detective, Sisyphus from Dark).

In `data/vectors/eval/results.json`, the "production before" and "production after-tmdb" case
rows are not comparable: the query instruction changed between them, and the new instruction
(better on the 80-theme benchmark) is worse for that one bare sentence. The full `ask` recipe
was stable across the TMDB rebuild (7 of the gold titles in the top 10, 9–10 in the top 20, no
off-theme noise).

TMDB's own recommendations recover only 4% of the MovieLens neighbours (recall@20 0.040, against
0.101 for plot vectors), so they get no weight in the blend. TMDB keywords do help: removing
keywords from films' text lowers theme search from 0.560 to 0.532 nDCG, so series now include
TMDB's keywords in their text too.

Cross-over lists (`cross`), checked against the films MovieLens fans of 126 series also loved:
the old blend of tags, plot and people found 0.023 of them in the top 20 (recall@20); plot alone
0.037; tags alone 0.016, and 0.015 with tags centred per kind (series only have predicted tags,
which don't line up with films' real ones). Plot + premise + people found 0.042 (+81%, 95%
interval +48% to +117%), so that is the cross-over blend. Only series → films can be scored
(MovieLens rates too few series for films → series), and 126 series is a small sample. Adding
the premise to same-kind lists only had first made `recommend` from a series fill 45% of its top
10 with films (34% before); with the premise in cross-over lists and tags out of them, 25% for film
seeds and 20% for series seeds.

`ask` with one abstract sentence plus `--themes` found none of the 30 excellence titles: their
plot ranks (around #30,000) made the soft gate cancel their theme credit. With one description,
`ask` now also searches with the themes as a sentence ("A story about genius, obsession…"): 3 in
the top 10, 5 in the top 50, no noise. With six summaries the same sentence cost one title in
the top 10, so it's only added for a single description.

Coverage limits to know: the Tag Genome stops at 9,734 films (Whiplash, 2014, isn't in it, so it
has predicted tags only). Anime and foreign series are stored under TVmaze's names, often
romanised, but IMDb's international titles (`title_aka`) find them: "Erased", "Money Heist".

## SQL recipes

Run them with the helper's `sql` command.

```sql
-- Best-rated limited series (ended within two years) with real audiences
SELECT title, year, network, rating, votes FROM catalog
WHERE kind = 'tv' AND status = 'Ended' AND end_year - year <= 1 AND votes >= 20000
ORDER BY score DESC LIMIT 15;

-- Films whose fans most often also love two given films
SELECT c.title, c.year, count(*) AS hits FROM crowd_similar s JOIN catalog c ON c.kind = 'movie' AND c.id = s.similar_id
WHERE s.tmdb_id IN (949, 807) GROUP BY c.rid ORDER BY hits DESC, min(s.rank) LIMIT 15;

-- What viewers praise or criticise in a film
SELECT tag, score FROM crowd_tags WHERE tmdb_id = 949 AND kind = 'opinion' ORDER BY score DESC;

-- A person's films and shows together
SELECT c.kind, c.title, c.year, r.role FROM credits r JOIN catalog c ON c.imdb_id = r.imdb_id
WHERE r.name = 'David Lynch' ORDER BY c.year;
```

## Gaps and quirks

- No streaming availability, season counts, age ratings, budgets or awards.
- MovieLens stops in 2023 and the Tag Genome in 2021: newer films have no crowd signal.
- `language` is the original language only. `people` holds only the first director of
  co-directed films, and the first three IMDb writers of a series (not always its creators).
- Some shorts lack both the `short film` tag and a runtime, so they get past the short filter.
- Crowd tags and keywords can reveal twists: read them, don't quote them.
- Award tags in the Tag Genome are predictions, not facts, so they were left out. Director,
  author and studio tags ("kubrick", "roald dahl", "pixar") count toward similarity but are
  never shown, because on another title they read as false facts.
- Review text is not stored, only distinctive words; the IMDb reviews stay in `data/raw/`.
