# Using the web

The catalogue knows what a title is, what it's close to, how it's rated and how long it runs.
The web knows where to watch it, who it suits, how many seasons it has, what critics thought,
and what came out after the snapshot. Search when the answer would change the user's decision,
not as a reflex on every title.

## Which tool

- Claude Code: `WebSearch` and `WebFetch` (load them with the tool search first if they are
  deferred).
- Codex: the built-in web search, when the session has it enabled (`codex --search`, or web
  search turned on in the config).
- No web access: give the links from `show` (where to watch, trailer, Letterboxd, IMDb, TMDB or
  TVmaze) and say plainly what you couldn't check.

Run the lookups for all final picks in one round, in parallel, rather than one film at a time.
Always include the title and year in queries so you don't land on a remake.

## What to look up, and where

| Need | When | Where |
|---|---|---|
| Where to stream, rent or buy | They're about to watch, or ask | Fetch the `Where to watch` link from `show` (TMDB's page, fed by JustWatch, set to their country): a web fetch returns the service names, though prices can show "$" for euros. JustWatch's own country page (`justwatch.com/fr/film/<slug>`) is the cleanest source, but its slug can't be guessed: search `"<title> <year>" JustWatch <country>`, trying the local title too (*Les Ailes du désir*). Ask for the country once and save it with `profile set country XX` |
| Where to stream a series | They're about to start it | The `Where to watch` link on a series card is a TMDB search: open the show, then its Watch tab. Or search `"<title>" <first year> series JustWatch <country>`. Seasons can sit on different services, so say which season you checked |
| Seasons and episodes | Pitching a series | TVmaze page (the `Page` link), Wikipedia, IMDb. The catalogue has years, status and episode length only |
| Runtime | Only when the catalogue has none | IMDb, TMDB page or Wikipedia. Other sites, Common Sense Media included, often round runtimes |
| Age rating and content | Kids, sensitive viewers, "nothing gory" | Common Sense Media (its pages fetch well and give a violence line per title), the IMDb Parents Guide, the official rating in their country. Kids-in-Mind blocks fetches |
| Critical reception | "Is it good?", obscure or high-rated-but-unknown picks | Rotten Tomatoes, Metacritic, Letterboxd average, notable reviews |
| Trailer | They want a taste | The `Trailer search` link from `show` is enough. Search only for a specific official trailer |
| New releases, cinema showtimes | "What's out now", "what's on near me" | Cinema chains and local listings (in France, AlloCiné), the distributor's site |
| Titles missing from the catalogue | Tip-of-the-tongue misses, post-snapshot releases | General web search, Wikipedia, TMDB or TVmaze search |
| Awards, background, trivia | They're curious, or it helps the pitch | Wikipedia, festival sites |

Searches for "watch <film> streaming" attract piracy and spam pages ("streaming VF complet",
look-alike JustWatch domains). Trust only justwatch.com, themoviedb.org and the services
themselves, and never pass on a link to an unofficial stream.

When the country is unknown and you can't ask, skip the availability lookups: give the
`Where to watch` links and offer to check once they say where they are.

When the user asked to *stream* and a pick is only to rent or buy, say so plainly. Offer a
streamable alternative from the candidates if there's a close one, and keep the rental pick
only if it's clearly the best fit.

## Reporting what you found

- Name the source and when you looked: "On Netflix France and to rent on Apple TV, per
  JustWatch today." Streaming catalogues change monthly, so date it.
- Keep it to what matters for the decision: one line per film, not a dump of search results.
- Don't let reviews spoil the plot, and don't present one fan review as a consensus.
- If the web contradicts the database (a different year or director), trust primary sources
  (TMDB, Wikipedia, the distributor) and mention the mismatch briefly.
