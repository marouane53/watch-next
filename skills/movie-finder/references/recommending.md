# Recommending well

Read this when turning a mood, a situation or a vague request into a shortlist. The commands
below are helper subcommands: run each as `python3 -I "$SKILL/scripts/movies.py" <command>`.
To shorten it in zsh, define a function (`m() { python3 -I "$SKILL/scripts/movies.py" "$@"; }`),
not a variable, because zsh doesn't split `$M` into words.

## Contents

- Three ways in: anchors, meaning, filters
- Mood recipes
- Situations
- Curating the shortlist
- When they push back

## Three ways in: anchors, meaning, filters

- **Anchors** ("I loved Heat"): `recommend Heat`. Several anchors find titles that connect
  them (marked "connects N of your titles"). Add `--type tv` for "a show like Heat". `--query`
  matches exact words, so keep it to one or two concrete ones (`--query heist`); for a mood or
  theme, use `ask "…" --like Heat` instead.
- **Meaning** ("a slow film about grief on a farm", "cozy mystery", "like Arrival but scarier"):
  `ask "…"`, or `ask "but scarier" --like Arrival`. For abstract themes ("excellence",
  "redemption", "found family"), write several imagined plot summaries (`--also`) and add
  `--themes`: see "Searching by theme" in SKILL.md, which has the measurements. It fuses plot vectors, the closest crowd
  tags (printed in the header when they match well) and full-text matches. It understands
  paraphrase; `search` only finds exact words. With `--like`, it takes the anchor's
  neighbourhood by plot and crowd tags and ranks it by the query; `--shift 1` pushes further
  from the anchor, `--shift 0.5` stays closer. Keep hard constraints in filters, not in the
  text: "like Breaking Bad but Japanese" is `ask "…" --like "tv:Breaking Bad" --lang ja`, and
  "Paddington but darker" stays family-friendly with `--genre Family`. Likewise, when a request
  has two parts (a genre and a vibe), put the genre in filters and only the vibe in the text:
  `ask "warm and heartfelt" --genre Romance --genre "Science Fiction"`. One query vector holds
  one idea well; given "romcom with a sci-fi premise" it drifts to plain romcoms.
- **Filters** ("Korean, 2000s, under two hours"): `browse` with filters, sorted by `score`, or add
  the same filters to `recommend` or `ask`.

Crowd tags (`--tag`) are the best mood filter for films: real crowd scores for about 5,000
well-known films, predictions from the plot for the rest. TV shows only have predicted tags,
and few of them, so for a series mood use `ask` (or `ask --like <a show they love>`) rather
than tag filters. `keywords WORD` lists the tags and TMDB
keywords that contain a word. `--any-tag` matches any of the tags given; `--tag` requires each.

## Mood recipes

| Mood or ask | Try |
|---|---|
| Cozy, feel-good | `ask "cozy feel-good comforting"`, or `browse --any-tag feel-good --any-tag heartwarming --not-tag dark --not-genre Horror` |
| Make me laugh | `--any-tag funny --any-tag hilarious --any-tag witty`; drier: `--any-tag satire --any-tag "dark comedy" --any-tag mockumentary --any-tag absurd` |
| Mind-bending | `--any-tag mindfuck --any-tag "twist ending" --any-tag nonlinear --any-tag "time loop" --any-tag surreal --any-tag dreamlike` |
| Edge of the seat | `--any-tag suspense --any-tag tense --any-tag intense --any-tag claustrophobic`; heists `--any-tag heist --any-tag "con artists"` |
| A good cry | `--any-tag sad --any-tag emotional --any-tag moving --any-tag bittersweet`, or `ask "devastating and beautiful"` |
| Scary | `--any-tag scary --any-tag creepy --any-tag "haunted house" --any-tag supernatural`; scary but not gory: add `--not-tag gore --not-tag gory --not-tag violent` |
| Nothing gory or violent | `--gentle` on any command (no horror, and no gore, violence, torture or disturbing tags or keywords). Tags miss things, so check the final picks' violence on Common Sense Media |
| Romantic | `--any-tag romantic --genre Romance`; slow-burn: `ask "slow burn romance"` |
| Slow, contemplative | `--any-tag atmospheric --any-tag meditative --any-tag melancholy --any-tag understated --any-tag "visually appealing"` |
| Gritty, dark | `--any-tag gritty --any-tag dark --any-tag bleak --any-tag "neo-noir"` |
| Thought-provoking | `--any-tag thought-provoking --any-tag philosophical --any-tag "character study"` |
| Whodunit | `--any-tag mystery --any-tag detective --genre Mystery`, or `ask "clever whodunit"` |
| Big spectacle | `--any-tag epic --any-tag adventure --min-votes 100000`; space `--any-tag space --any-tag aliens` |
| Stylish, cult | `--any-tag stylish --any-tag stylized --any-tag "cult film" --any-tag noir` |
| Nostalgic, coming of age | `--any-tag nostalgic --any-tag "coming of age" --any-tag childhood --any-tag friendship` |
| True stories | `--any-tag "based on a true story" --any-tag "true story"` |

When the mood comes with an anchor ("like Paddington but for adults"), use `ask "for adults"
--like Paddington` or `recommend Paddington` plus filters.

## Situations

- **Date night.** Something both can enjoy and talk about after: a smart thriller, a warm
  comedy, a romance with bite. Avoid heavy gore and bleak endings unless asked. Check the
  runtime if the evening is short.
- **With kids or family.** Require `--genre Family`, because `Animation` alone lets in adult
  animation. For "nothing scary", add `--gentle --not-tag scary --not-tag creepy --not-genre
  Thriller`. Check the age rating on the web (Common Sense Media
  or the official rating in their country) before pitching. Almost every kids' film has some
  peril, so describe how much ("one tense chase") instead of promising none.
- **Friends, group night.** Popular, high-energy, rewatchable: `--min-votes 100000`, crowd-
  pleasing comedy, action, horror-comedy.
- **Solo, late night.** You can go weirder, longer, slower, subtitled.
- **A series to binge.** `--type tv --status ended` gives a story with an ending, mostly: some
  ended shows were cancelled mid-story, so check on the web before promising closure. Limited
  series and short episodes are an easier commitment (`--max-runtime 35` for half-hour shows).
  Running shows get a "still running" heads-up. Check season counts on the web before quoting
  them.
- **"A show like this film" and the reverse.** `recommend Heat --type tv` or
  `recommend "tv:Breaking Bad" --type movie`. Plot, tags and shared people link films and shows.
- **Comfort rewatch or background.** Familiar, light, popular; include seen titles
  (`--include-seen`) and say that's on purpose.
- **Short on time.** `--max-runtime 95` for films. Runtimes come from IMDb and cover most
  titles.
- **Hidden gems.** `browse --max-votes 20000 --min-votes 1000 --min-score 7 --to 2023`, or
  `recommend` a favourite with `--max-votes 20000`. A few thousand votes is enough to trust the
  rating, and the `--to` cut skips recent releases whose ratings haven't settled.
- **World cinema.** `browse --lang ko --min-votes 10000 --sort score` is a starter pack. For a
  first film in a new language, pick an accessible entry point, then go deeper.
- **Director, writer or actor deep dive.** `browse --person "Kaurismäki" --sort oldest
  --include-seen` (accents optional; `--person` covers films and series). Suggest an entry
  point, the peak and a deep cut, with a reason for each.
- **Double feature or marathon.** Pair titles that talk to each other: the same theme from two
  countries, an original and its spiritual successor, a contrast in tone. Give the order and a
  line on why they belong together.
- **"What's that movie where…"** `ask` with the details they remember, plus decade or language
  filters, then `search` for exact names or phrases. Confirm with `show`. If it's not in the
  catalogue, search the web.
- **"Is X worth watching?"** `show X`, then compare its tags and verdicts with their profile
  (`profile taste`). Give a straight answer and the reason, plus reception from the web if they
  want more.
- **"What next after X?"** `recommend X`, and if X belongs to a series or a director's body of
  work, mention the natural next title too.
- **New and in cinemas.** The catalogue is a snapshot (`info`). For what's playing now or just
  out, go to the web.

## Curating the shortlist

- **Mix of risk.** A sure thing, a deeper cut, and a stretch that connects to their taste from
  an unexpected direction (another country, decade, genre or format).
- **Not just the famous.** At most one title over 500k votes unless asked; there's usually a
  less obvious film that fits better.
- **Variety.** One per franchise and per director unless asked. For a series of films, start
  with the first unless they've seen it. `recommend` and `ask` already skip near-duplicates.
- **Respect the limits they stated:** languages (`--lang`, or their profile languages for no
  subtitles), no gore (`--not-tag gore`), time (`--max-runtime`).
- **Know before you pitch.** `show` each pick: overview, crowd tags, viewers' verdicts and
  heads-ups make the pitch specific and catch clashes with their notes.
- **Trust signals in combination.** A title reached by two anchors, or by both `recommend` and
  `ask`, beats one ranked first by a single signal. "Fans also love it" is the strongest single
  signal when it's present.

## When they push back

- "Seen it": `profile seen`, swap in the next candidate without fuss.
- "Not feeling it": find out why in one short question, or infer it ("too dark?"), then steer
  with `--avoid` the rejected title, `ask "… but lighter" --like <anchor>`, or new filters. If
  they never want it suggested, `profile dismiss`.
- "More like #2": `recommend` from #2, maybe with their original anchor too.
- Three rounds without a hit means the brief is off. Ask one sharper question: a recent title
  they loved, or what they want to *feel*.
