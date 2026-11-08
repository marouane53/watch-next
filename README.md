# Watch Next

A film and TV concierge skill for Claude Code and Codex. It builds a catalogue of 78,000 titles
on your own laptop from public datasets and recommends from your watch history.

Ask your agent for "something like Carrie, but mixed with Marvel" or "a short Korean thriller
series I haven't seen". The agent searches that catalogue instead of answering from memory. It
uses what MovieLens fans of a title also loved, crowd mood tags, plots and Wikipedia premises
compared by meaning, IMDb ratings, and where each title streams in your country. Every pick says
which search found it.

The skill also:

- remembers what you liked, dropped and want to watch;
- loads your history from Trakt, TV Time, Letterboxd, IMDb or Netflix;
- draws poster boards of its picks.

The data isn't in this repository, because the sources' licences don't allow that. The installer
downloads it and builds the catalogue on your machine, for your personal use.

## Install

One command, for people and agents alike:

```bash
git clone https://github.com/marouane53/watch-next.git && cd watch-next && ./install.sh
```

The installer works in three stages:

1. It checks what's missing and says how to fix it.
2. It builds the catalogue, which takes 3 to 5 hours, mostly downloads that need no attention.
3. It links the skill into Claude Code (`~/.claude/skills`) and Codex (`~/.codex/skills`).

You can stop it at any point and run `./install.sh` again: finished steps are skipped and
downloads resume. `./install.sh --check` shows what's missing and what's done, without
starting anything.

### What you need

| Need | Why |
|---|---|
| macOS or Linux (on Windows, use WSL) | The installer is a shell script |
| [uv](https://docs.astral.sh/uv/) | It installs Python 3.12 and the build libraries in `.venv`. Install it with `curl -LsSf https://astral.sh/uv/install.sh \| sh` or `brew install uv` |
| A free TMDB token in `.env` | For trailers, age ratings, seasons and streaming. A person has to get it (see the next section) |
| About 13 GB of free disk | 3.4 GB of downloads unpack to about 7 GB. Add the database and vectors (1.5 GB), `.venv` (1 GB or more) and the embedding model (1.2 GB) |
| 16 GB of memory | The crowd step holds a 14,000 × 14,000 matrix |
| 3 to 5 hours | Most of it is waiting on Wikipedia's rate limit. The last step (vectors) took 72 minutes on an M4 Max Mac. It uses an NVIDIA or Apple GPU when there is one; on a CPU alone it takes many hours |
| python3, version 3.9 or newer | The skill's helper runs on the system Python, which macOS and most Linux systems have |
| Claude Code or Codex | Or any agent that can read `skills/movie-finder/SKILL.md` and run its commands |

### The TMDB token: the one step a person does

TMDB needs an account, so an agent can't do this for you:

1. Sign up for free at https://www.themoviedb.org/signup and confirm your email.
2. Go to https://www.themoviedb.org/settings/api and request an API key: choose personal use and
   fill in the short form.
3. Copy the **API Read Access Token**. It's the long one that starts with `eyJ`, not the short
   API key.
4. In the `watch-next` folder, run `cp .env.example .env`, then paste the token after
   `TMDB_READ_TOKEN=` in `.env`.

`.env` stays on your machine, and git ignores it. Don't paste the token into a chat.

By default, streaming offers are kept for France, the US, the UK, Morocco, Belgium, Canada,
Germany and Spain. If you live elsewhere, add your countries to `.env` as two-letter codes
before you build, for example `WATCH_REGIONS=US,GB,IN`.

## For agents installing this

If a person asks you to install Watch Next:

1. Clone it and run `./install.sh --check` in the folder. It ends with `ready to build`, with
   `built` when everything is done, or with `not ready yet` under a list where the problems are
   marked `MISSING`.
2. If the TMDB token is missing, the person has to get it. Show them the four steps above
   (`--check` prints them too) and wait until they say the token is in `.env`. Don't ask them to
   paste it to you, and don't print or read `.env`.
3. If `uv` is missing, ask before you install it, with the command from the table above.
4. Run `./install.sh`. It runs for hours, so start it in the background if you can. Check on it
   with `tail -n 20 data/install.log` or `./install.sh --check`. If it stops, run it again: it
   resumes where it stopped.
5. When it prints `Done`, tell the person to start a new session so the agent loads the skill.
   Then they can try `/movie-finder` in Claude Code or `$movie-finder` in Codex.
6. Never commit, upload or share `data/` or `.env`.

## Using it

Talk to your agent normally: the skill triggers on any film or TV request. Some things to try:

- "What should I watch tonight? Something tense, under two hours."
- "A series like Severance, but one that has ended."
- "Something like Carrie, mixed with Marvel."
- "Here's my Trakt export: ~/Downloads/trakt.zip". TV Time, Letterboxd, IMDb and Netflix exports
  work too.
- "I watched Past Lives last night and loved it."
- "Make a poster board of those picks."

The first time, it asks a question or two, or loads your history. It then keeps a taste profile
in `data/profile.json`, which stays on your machine.

You can also run the helper without an agent:

```bash
python3 skills/movie-finder/scripts/movies.py recommend "Arrival"
```

`movies.py -h` lists every command.

## How it works

| Part | What it does |
|---|---|
| `scripts/build_db.py` | Downloads the film catalogue of [Movie Vector Map](https://mikeyguinness.com/movies/): 37,000 films with TMDB data |
| `scripts/fetch_*.py` | Download TVmaze shows, Wikipedia introductions and TMDB details. Each resumes where it stopped |
| `scripts/enrich.py` | Adds IMDb ratings, runtimes and crew; 42,000 TV shows from TVmaze; "fans also loved" from MovieLens 32M; and crowd mood tags and review vocabulary from the Tag Genome 2021 |
| `scripts/enrich.py` (continued) | Builds plot and premise vectors with a local embedding model (Qwen3-Embedding-0.6B), a blended similar-titles list and vibe clusters |
| `scripts/evaluate.py` | Scores the search against human ground truth: Tag Genome themes and MovieLens neighbours. Changes to the vectors or the ranking were kept only when they measurably helped |
| `skills/movie-finder/` | The skill itself |

The skill folder holds:

- `SKILL.md`: the workflow;
- `scripts/movies.py`: one helper with every command, using only the standard library;
- references on the data, on recommending, on using the web, and on the house style of its pages.

For agents working on the code itself, the guide is [AGENTS.md](AGENTS.md).

## Updating

- **New code:** run `git pull`. The skill is a link to this folder, so agents see the change at
  once.
- **Fresh data** (new films, current streaming): run
  `rm -rf data/raw data/.build data/movies.db data/vectors`, then `./install.sh`. Your taste
  profile is kept.
- **Disk space:** `data/raw` (about 7 GB) is only used to build. Delete it if you need the space.

## Licences and credits

The code is MIT-licensed ([LICENSE](LICENSE)). The data is not part of this repository and that
licence doesn't cover it. The installer downloads it from each source for your personal,
non-commercial use, under that source's terms. Don't publish the database, the vectors or the
raw files.

| Source | Used for | Terms |
|---|---|---|
| [Movie Vector Map](https://mikeyguinness.com/movies/) | The film catalogue and its map | Public site data with no licence stated: personal use |
| [TMDB](https://www.themoviedb.org/) | Film data, via the map; trailers, age ratings, seasons, streaming | [TMDB API terms](https://www.themoviedb.org/api-terms-of-use) |
| [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/) | Ratings, votes, runtimes, crew | Personal and non-commercial use |
| [MovieLens 32M](https://grouplens.org/datasets/movielens/32m/) | "Fans also loved" | Research and non-commercial use |
| [Tag Genome 2021](https://grouplens.org/datasets/movielens/tag-genome-2021/) | Crowd mood tags, review vocabulary | CC BY-NC 3.0 |
| [TVmaze](https://www.tvmaze.com/api) | TV shows | CC BY-SA 4.0 |
| [Wikipedia](https://en.wikipedia.org/), found through [Wikidata](https://www.wikidata.org/) | Premises for similar titles | CC BY-SA 4.0 |
| [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | Plot and premise vectors | Apache 2.0 |

This product uses the TMDB API but is not endorsed or certified by TMDB. Information courtesy of
IMDb (https://www.imdb.com), used with permission. Watch Next is an independent project and is
not affiliated with any of these sources.

The datasets' papers:

- F. Maxwell Harper and Joseph A. Konstan. 2015. The MovieLens Datasets: History and Context.
  ACM TiiS 5(4). https://doi.org/10.1145/2827872
- Denis Kotkov, Alexandr Maslov and Mats Neovius. 2021. Revisiting the Tag Relevance Prediction
  Problem. SIGIR 2021. https://doi.org/10.1145/3404835.3463019
- Jesse Vig, Shilad Sen and John Riedl. 2012. The Tag Genome: Encoding Community Knowledge to
  Support Novel Interaction. ACM TiiS 2(3). https://doi.org/10.1145/2362394.2362395
