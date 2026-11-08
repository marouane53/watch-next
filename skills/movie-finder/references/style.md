# House style

Every HTML page or image made for the user uses one look: an election-night front page. Cream
newsprint, ink and a single vermilion, condensed uppercase headlines (Big Shoulders Display) over
a reading serif (Newsreader). That covers `board`, share images for social media, charts,
tables, reports and one-off pages. The `board` command already uses it. For anything else, start
from `assets/house-style.css` instead of writing new CSS.

## Using it

Inline the stylesheet in a `<style>` tag (pages are single self-contained files) and load the
fonts in `<head>`; the font link is at the top of the CSS file.

- **A page people scroll** (a board, a report, a table): `<body class="paper"><main class="page">`.
  It is responsive and works at phone width.
- **A fixed image** (a share image for X or Instagram): `<body class="stage"><main class="sheet"
  style="height: …px">`, 1600 px wide. Add a script that scales `.sheet` to fit the window
  (1220 is the sheet's height here):

  ```html
  <script>
    const sheet = document.querySelector(".sheet");
    const fit = () => { sheet.style.transform = `scale(${Math.min(innerWidth / 1600, innerHeight / 1220)})`; };
    addEventListener("resize", fit); fit();
  </script>
  ```

## Components

| Class | What it is |
|---|---|
| `.masthead` > `.flag` + `.dateline` | The paper's name ("The Watch-Next", hyphen in a red `<span>`) and the date line over a double rule |
| `.headline` | Huge condensed uppercase headline; wrap the last word or the punchline in `<span>` for red |
| `.deck` | Italic standfirst under the headline; `<b>` for the key facts |
| `.kicker` | Small red uppercase label above a title ("Series · Korean") |
| `.label` > `h2` (+ `em`) + `.key` | Section heading on a rule, an italic aside, a legend on the right |
| `.grid` / `.row10` | Responsive card grid / ten fixed columns for a sheet |
| `.card` > `.poster` (`.tab`, `.rate`, `img`) + `.body` | Poster with a number tab (01, 02…) and a ★ rating tab |
| `.win` on a card | The winner: red tab, red bar, red percentage |
| `.stamp` | A rotated rubber stamp ("Elected"); needs the `#ink` SVG filter in the page |
| `.meta` | Condensed facts line: years · network or director, runtime on the right |
| `.note` / `.ov` / `.facts` | The pitch / an overview in italic / small italic details |
| `.result` > `.bar` > `i` + `b` | Hatched bar filled in ink, with its percentage |
| `.via` | Italic "via …" attribution under a card |
| `.links` | Condensed uppercase links underlined in red |
| `.colophon` | Method and data credits under a thin rule |

The `#ink` filter for stamps:

```html
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><filter id="ink">
  <feTurbulence type="fractalNoise" baseFrequency=".75" numOctaves="2" seed="7"/>
  <feDisplacementMap in="SourceGraphic" scale="2.6"/></filter></svg>
```

## Rules

- Red is for one thing per block: the winner, the punchline word, the kicker. Everything else
  is ink on paper.
- Condensed uppercase for headlines, labels, numbers and links. The serif for anything read as
  a sentence, and italic for decks, asides and credits.
- Square corners and 1.5 px ink outlines. No rounded cards, drop shadows or gradients beyond the
  paper's own vignette and grain.
- Charts: hatched bars filled in ink with the highlight in red; categories take `--c1` to `--c6`
  in order. Show seat counts as a hemicycle of dots.
- Light only: it is printed paper, so there is no dark mode.
- Credit the data in the colophon (TMDB and IMDb, plus TVmaze and Wikipedia under CC BY-SA when
  their data appears).

## Rendering an image

Embed the posters as base64 so the file stands alone. Use larger posters than the catalogue's
`poster_url`: TMDB `w780` instead of `w342`, and TVmaze `original_untouched` instead of
`medium_portrait`. Shrink them with `sips -Z 720`. Then take the screenshot at twice the size:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
  --hide-scrollbars --force-device-scale-factor=2 --virtual-time-budget=8000 \
  --window-size=1600,1220 --screenshot=out.png "file://$PWD/page.html"
sips -s format jpeg -s formatOptions 88 out.png --out out.jpg
```

That is macOS. On Linux, run `google-chrome` or `chromium` with the same flags, and use
ImageMagick instead of `sips` (`magick in.jpg -resize 720x720 out.jpg`, `magick out.png
-quality 88 out.jpg`).

Keep the JPEG under 5 MB for X. Check the PNG by eye before sending it: look for text that is
cut off, rows of uneven height and a footer that overlaps the cards.
