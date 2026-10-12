# Ontario Soccer Club Registration Scanner

A prototype built for a PowerUp Sports growth case study. It identifies which registration platform each Ontario soccer club uses, so non-enrolled clubs (clubs not registering on PowerUp) can be sized, segmented and targeted.

Built with Claude Code; results were checked by hand.

## What it does

1. Reads `sources.csv`: Ontario district and league websites.
2. Collects member-club website links from those pages.
3. Opens each club's site in a headless browser, follows its "Register" links, and tags the platform from where they lead (PowerUp, SportsEngine, PlayMetrics, Google Forms, etc.).
4. Writes a lead list and summary counts.

It respects `robots.txt`, pauses between requests, and collects club-level information only (no personal names or emails).

## Results (October 2026 sample)

Random sample of 122 clubs; platform identified for 52.

| | Clubs | Share |
|---|---|---|
| On PowerUp | 33 | 63% of identified |
| Not on PowerUp | 19 | 37% of identified |
| ↳ No dedicated system (forms, club website, email/paper) | 9 | 47% of non-enrolled |
| ↳ Competitor platform | 8 | 42% of non-enrolled |
| ↳ Partial (PowerUp for some programs only) | 2 | 11% of non-enrolled |

Clubs in E2E-run leagues show the same split (20 of 32 identified on PowerUp).

- `results/ontario_clubs_sample.csv`: all 122 sampled clubs, with league, district, status, segment, platform and evidence link
- `results/non_enrolled_leads.csv`: the 19 non-enrolled clubs, sorted by segment

**Limits:** percentages are directional. Five district sites blocked automated visits, and the sample skews toward clubs listed on district and league websites. A first pass counted some league websites as clubs; these were removed after checking the evidence by hand.

## Run it

Requires Python 3.

```bash
pip install playwright
playwright install chromium

# Sample run: 10 random clubs per district/league (about 15 minutes)
python3 scrape_clubs.py --limit-per-source 10

# Full run: every club found
python3 scrape_clubs.py
```

Outputs: `clubs_found.csv`, `ontario_clubs_lead_list.csv`, `summary.csv`.

## Reuse in another province

Replace the rows in `sources.csv` with that province's district and league websites (for example, BC Soccer's districts) and run it again.
