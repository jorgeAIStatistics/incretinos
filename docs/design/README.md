# Design captures

Full-page screenshots of every route at 1440 x 900, 834 x 1112 and 390 x 844.

- `before/`: the application at the start of Session 2 (commit `6c3f610`), 12 routes, 36 images.
- `after/`: the same routes after the design foundation, plus `/review`, `/quality` and `/conflicts`, 45 images.
- `metrics.jsonl` in each folder: per route and size, the document title, sideways overflow in px
  (`scrollWidth - clientWidth`), page height, number of `h1` elements and the heading outline.

How to capture again, and what the system is: [`../design-system.md`](../design-system.md), section 18.

## Measured before and after

Sideways page overflow at 390 px (from `metrics.jsonl`):

| Route | Before | After |
|---|---|---|
| `/` | 34 px | 0 |
| `/trials/NCT05869903` | 380 px | 0 |
| `/benefit-risk` | 380 px | 0 |
| `/safety` | 68 px | 0 |
| `/design` | 121 px | 0 |
| `/sources` | 431 px | 0 |
| all other routes | 0 | 0 |

No route overflows at 834 px or 1440 px in either set.

| Measure | Before | After |
|---|---|---|
| Distinct document titles | 1 for 12 routes | 15 for 15 routes |
| `/gaps` page height at 1440 px | 4,235 px (52 rows, header scrolls away) | 1,255 px (25 per page, header stays) |
| `/gaps` page height at 390 px | 10,885 px | 1,619 px |
| `/trials` page height at 1440 px | 3,601 px | 1,123 px |
| Navigation below 1024 px | A clipped strip of links, no menu | Menu button and drawer |

A full-page capture shows the sticky sidebar only in the first screen of a long page. That is an artefact of
capturing the whole page in one image; in a browser the sidebar stays in view.
