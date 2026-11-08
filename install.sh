#!/usr/bin/env bash
# Watch Next installer: builds the film and TV catalogue on this machine from public datasets,
# then links the movie-finder skill into Claude Code and Codex.
#
#   ./install.sh           build everything: 3 to 5 hours, mostly downloads; safe to stop and rerun
#   ./install.sh --check   show what is missing and which steps are done, then stop
#   ./install.sh --link    only link the skill into ~/.claude/skills and ~/.codex/skills
#
# Finished steps are skipped (markers in data/.build/) and the big downloads resume, so after a
# failure or Ctrl-C, run it again. Everything it prints also goes to data/install.log.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
RAW=data/raw
MARKS=data/.build
NEED_GB=13  # free disk for the whole build: downloads, unzipped datasets, .venv and the model
MODE=${1:-build}

say()   { printf '\n==> %s\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
stop()  { printf '\nSTOPPED: %s\n' "$1" >&2; shift; for l in "$@"; do printf '    %s\n' "$l" >&2; done; exit 1; }
done_() { [ -f "$MARKS/$1" ]; }
mark()  { mkdir -p "$MARKS"; date '+%Y-%m-%d %H:%M' > "$MARKS/$1"; }

setting() {  # NAME: from the environment, else from .env; never printed
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f .env ]; then
    v=$(sed -n "s/^$1=//p" .env | tail -n 1 | tr -d "\"' \r")
  fi
  printf '%s' "$v"
}

TMDB_HELP=(
  "A person has to do this once (it needs an account, so an agent can't):"
  "  1. Sign up for free at https://www.themoviedb.org/signup and confirm the email."
  "  2. Open https://www.themoviedb.org/settings/api and request an API key (personal use, short form)."
  "  3. Copy the \"API Read Access Token\": the long one that starts with eyJ, not the short API key."
  "  4. In this folder: cp .env.example .env, then paste the token after TMDB_READ_TOKEN= in .env."
  "Then run ./install.sh again. Don't paste the token into a chat."
)

tmdb_status() {  # 200 if the token works, 401 if TMDB refuses it, 000 if TMDB can't be reached
  printf 'header = "Authorization: Bearer %s"\n' "$1" |
    curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -K - https://api.themoviedb.org/3/authentication 2>/dev/null || true
}

free_gb() { df -Pk . | awk 'NR == 2 { print int($4 / 1048576) }'; }
ram_gb() {
  if [ "$(uname -s)" = Darwin ]; then echo $(( $(sysctl -n hw.memsize) / 1073741824 ))
  else awk '/MemTotal/ { print int($2 / 1048576) }' /proc/meminfo; fi
}

UV=$(command -v uv || true)
for p in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do  # where uv's own installer puts it
  if [ -z "$UV" ] && [ -x "$p" ]; then UV=$p; fi
done

# The database steps only count while the database exists.
[ -f data/movies.db ] || rm -f "$MARKS/site" "$MARKS/base" "$MARKS/final"

STEPS="env site imdb movielens genome tvmaze base wikipedia tmdb final"
describe() {
  case $1 in
    env)       echo "Python environment in .venv (about 1 GB; torch is most of it)" ;;
    site)      echo "Film catalogue from the Movie Vector Map site (30 MB)" ;;
    imdb)      echo "IMDb datasets: ratings, runtimes, directors and writers (630 MB)" ;;
    movielens) echo "MovieLens 32M ratings (240 MB download)" ;;
    genome)    echo "Tag Genome 2021 with IMDb reviews (1.9 GB download, 4.3 GB kept)" ;;
    tvmaze)    echo "TVmaze show index (about 15 minutes)" ;;
    base)      echo "Catalogue: IMDb links, TV shows, crowd data and tags (about 5 minutes)" ;;
    wikipedia) echo "Wikipedia introductions (1 to 3 hours: Wikipedia throttles downloads)" ;;
    tmdb)      echo "TMDB details: trailers, age ratings, seasons, streaming (about 1 hour)" ;;
    final)     echo "Plot vectors, similar titles and vibe clusters (about 1 hour with a GPU)" ;;
  esac
}

# ---------- requirements ----------

check() {  # prints each requirement; returns 1 when something blocks the build
  local ok=0 os gb ram tok code
  os=$(uname -s)
  case $os in
    Darwin|Linux) info "OK      $os" ;;
    *) info "MISSING macOS or Linux (on Windows, run this inside WSL)"; ok=1 ;;
  esac
  command -v curl >/dev/null && info "OK      curl" || { info "MISSING curl"; ok=1; }
  if [ -n "$UV" ]; then info "OK      uv ($UV)"
  else info "MISSING uv, the Python installer: curl -LsSf https://astral.sh/uv/install.sh | sh  (or: brew install uv)"; ok=1; fi
  if command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    info "OK      python3 $(python3 -c 'import platform; print(platform.python_version())') (the skill runs on it)"
  else info "WARN    python3 3.9+ not found: the skill will need it (the build itself uses .venv)"; fi
  tok=$(setting TMDB_READ_TOKEN)
  if done_ tmdb; then info "OK      TMDB details already downloaded"
  elif [ -z "$tok" ]; then info "MISSING TMDB_READ_TOKEN in .env (a free TMDB token; see below)"; ok=1
  else
    code=$(tmdb_status "$tok")
    case $code in
      200) info "OK      TMDB token (accepted by TMDB)" ;;
      401) info "MISSING a working TMDB token: TMDB refused the one in .env (use the long Read Access Token)"; ok=1 ;;
      *)   info "WARN    TMDB token found, but TMDB couldn't be reached to check it" ;;
    esac
  fi
  gb=$(free_gb)
  if done_ genome || [ "$gb" -ge "$NEED_GB" ]; then info "OK      ${gb} GB free disk"
  else info "MISSING disk space: ${gb} GB free here, the build needs about ${NEED_GB} GB"; ok=1; fi
  ram=$(ram_gb)
  [ "$ram" -ge 12 ] && info "OK      ${ram} GB memory" || info "WARN    ${ram} GB memory: 16 GB is recommended (the crowd step needs about 8 GB)"
  return $ok
}

status() {
  local s
  for s in $STEPS; do
    if done_ "$s"; then info "done    $(describe "$s")"; else info "to do   $(describe "$s")"; fi
  done
}

# ---------- steps ----------

download() {  # URL FILE: retried and resumable; FILE appears only once complete
  [ -f "$2" ] && return 0
  mkdir -p "$(dirname "$2")"
  info "downloading $1"
  curl -fsSL --retry 5 --retry-delay 10 -C - -o "$2.part" "$1"
  mv "$2.part" "$2"
}

unzip_some() {  # ZIP DEST [MEMBER…]: extract everything, or only the members named
  "$PY" -I - "$@" <<'EOF'
import sys, zipfile
src, dest, wanted = sys.argv[1], sys.argv[2], set(sys.argv[3:])
with zipfile.ZipFile(src) as z:
    for m in z.infolist():
        if not m.filename.startswith("__MACOSX") and (not wanted or m.filename in wanted):
            z.extract(m, dest)
EOF
}

step_env() {
  [ -x "$PY" ] || "$UV" venv .venv --python 3.12
  "$UV" pip install --python "$PY" -r requirements.txt
}

step_site() { "$PY" -I scripts/build_db.py; }

step_imdb() {
  local f
  for f in title.basics title.ratings title.crew name.basics; do
    rm -f "$RAW/imdb/$f.tsv.gz.part"  # IMDb republishes these daily, so never resume an old file
    download "https://datasets.imdbws.com/$f.tsv.gz" "$RAW/imdb/$f.tsv.gz"
  done
}

step_movielens() {
  download https://files.grouplens.org/datasets/movielens/ml-32m.zip "$RAW/movielens/ml-32m.zip"
  unzip_some "$RAW/movielens/ml-32m.zip" "$RAW/movielens"
  rm "$RAW/movielens/ml-32m.zip"
}

step_genome() {
  local d=movie_dataset_public_final
  download https://files.grouplens.org/datasets/tag-genome-2021/genome_2021.zip "$RAW/genome/genome_2021.zip"
  info "unzipping the three files the build reads"
  unzip_some "$RAW/genome/genome_2021.zip" "$RAW/genome" "$d/raw/metadata.json" "$d/raw/reviews.json" "$d/scores/tagdl.csv"
  rm "$RAW/genome/genome_2021.zip"
}

step_tvmaze() { "$PY" -I scripts/fetch_tvmaze.py; }
step_base()   { "$PY" -I scripts/enrich.py imdb tv crew genome reviews catalog crowd aka; }
step_final()  { "$PY" -I scripts/enrich.py tmdb vibes vectors premise tags similar clusters; }

LOOKUP_PIDS=""
stop_lookups() { [ -z "$LOOKUP_PIDS" ] || kill $LOOKUP_PIDS 2>/dev/null || true; }

running() {  # PID…: is any of them still running?
  local p
  for p in "$@"; do kill -0 "$p" 2>/dev/null && return 0; done
  return 1
}

lookups() {  # Wikipedia and TMDB side by side: both look titles up in the catalogue built so far
  local name pid pids="" names="" failed="" n=0
  for name in wikipedia tmdb; do
    if done_ "$name"; then info "done earlier: $(describe "$name")"; continue; fi
    say "$(describe "$name")"
    "$PY" -I "scripts/fetch_$name.py" > "data/$name.log" 2>&1 &
    pids="$pids $!"; names="$names $name"
  done
  [ -n "$pids" ] || return 0
  LOOKUP_PIDS=$pids
  info "progress every 5 minutes; full output in data/wikipedia.log and data/tmdb.log"
  while running $pids; do
    sleep 10
    n=$((n + 1))
    [ $((n % 30)) = 0 ] || continue
    for name in $names; do info "[$name] $(tail -n 1 "data/$name.log" 2>/dev/null)"; done
  done
  set -- $names
  for pid in $pids; do
    if wait "$pid"; then mark "$1"; else failed="$failed $1"; fi
    shift
  done
  LOOKUP_PIDS=""
  [ -z "$failed" ] || stop "stopped early:$failed (see data/<name>.log)" "Run ./install.sh again: it resumes where it stopped."
}

run() {  # STEP: run step_STEP unless it finished before
  local t0
  if done_ "$1"; then info "done earlier: $(describe "$1")"; return 0; fi
  say "$(describe "$1")"
  t0=$(date +%s)
  "step_$1"
  mark "$1"
  info "finished in $(( ($(date +%s) - t0 + 59) / 60 )) min"
}

link_skill() {
  local src="$PWD/skills/movie-finder" home dir linked=0
  for home in "$HOME/.claude" "$HOME/.codex"; do
    [ -d "$home" ] || continue
    dir="$home/skills"
    mkdir -p "$dir"
    if [ -L "$dir/movie-finder" ] || [ ! -e "$dir/movie-finder" ]; then
      ln -sfn "$src" "$dir/movie-finder"
      info "linked $dir/movie-finder -> $src"
      linked=1
    else
      info "left alone: $dir/movie-finder exists and isn't a link; remove it, then run ./install.sh --link"
    fi
  done
  [ $linked = 1 ] || info "No ~/.claude or ~/.codex folder here: point your agent at $src/SKILL.md"
}

# ---------- main ----------

case $MODE in
  --check)
    say "Requirements"
    if check; then ready=1; else ready=0; fi
    say "Steps"
    status
    [ -n "$(setting TMDB_READ_TOKEN)" ] || done_ tmdb || { echo; printf '%s\n' "${TMDB_HELP[@]}"; }
    [ $ready = 1 ] || { printf '\nnot ready yet\n'; exit 1; }
    left=0
    for s in $STEPS; do done_ "$s" || left=$((left + 1)); done
    if [ $left = 0 ]; then printf '\nbuilt: nothing left to do\n'; else printf '\nready to build (%s of 10 steps left)\n' $left; fi
    exit 0
    ;;
  --link) link_skill; exit ;;
  build) ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

mkdir -p data
exec > >(tee -a data/install.log) 2>&1
trap 'stop_lookups' EXIT
trap 'stop_lookups; exit 130' INT TERM

say "Watch Next: building the catalogue ($(date '+%Y-%m-%d %H:%M'))"
info "3 to 5 hours, mostly downloads. Safe to stop and rerun: finished steps are skipped."
if ! check; then
  [ -n "$(setting TMDB_READ_TOKEN)" ] || done_ tmdb || stop "the TMDB token is missing" "${TMDB_HELP[@]}"
  stop "fix what is marked MISSING above, then run ./install.sh again"
fi

run env
device=$("$PY" -I -c 'import torch; print("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")' 2>/dev/null || echo cpu)
[ "$device" != cpu ] || info "No GPU found: the vector steps will run on the CPU and can take many hours."
for s in site imdb movielens genome tvmaze base; do run "$s"; done
lookups
run final

say "Linking the skill"
link_skill

say "Checking the catalogue"
PY3=$(command -v python3 >/dev/null && echo python3 || echo "$PY")
"$PY3" -I skills/movie-finder/scripts/movies.py info
"$PY" -I skills/movie-finder/scripts/movies.py ask "a heist crew pulls off one last job" --limit 3 --brief

say "Done ($(date '+%Y-%m-%d %H:%M'))"
info "Ask your agent for something to watch: /movie-finder in Claude Code, \$movie-finder in Codex."
info "data/raw (about 7 GB) is only needed to rebuild; you can delete it."
