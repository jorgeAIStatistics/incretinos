# Design captures

Full-page screenshots of every route at 1440 x 900, 834 x 1112 and 390 x 844.

- `before/`: the application at the start of Session 2 (commit `6c3f610`), 12 routes, 36 images.
- `after/`: the same routes on the merged Session 2 code after the review fixes (branch `s2/frontend-fix`), plus
  `/review`, `/quality` and `/conflicts`, plus five tab states that are not a route's default view (the design
  workbench's simulation, coverage and eligibility tools; the sources page's changes and validation tabs), 60 images.
  Every capture is taken after the network is idle and no loading placeholder is left.
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
| `/design`, population coverage tab | not captured | 0 |
| `/design`, eligibility and amendments tab | not captured | 0 |
| `/sources` | 431 px | 0 |
| all other routes | 0 | 0 |

No route or tab state overflows at 834 px or 1440 px in either set. The browser suite also checks 320 px, after the
network is idle and in every tab state (`e2e/foundation.spec.ts`, "no route scrolls sideways … in any tab"). An
earlier version of this table claimed 0 for `/safety` and `/design` at 390 px while the merged code overflowed by
23 px (`/safety`, after its risk-contrast panel loaded) and by up to 1,005 px (`/design` coverage tab, from visually
hidden cell text escaping an unpositioned scroll box); the measurement was taken too early and never opened the tabs.

| Measure | Before | After |
|---|---|---|
| Distinct document titles | 1 for 12 routes | 15 for 15 routes |
| `/gaps` page height at 1440 px | 4,235 px (52 rows, header scrolls away) | 1,255 px (25 per page, header stays) |
| `/gaps` page height at 390 px | 10,885 px | 1,619 px |
| `/trials` page height at 1440 px | 3,601 px | 1,123 px |
| Navigation below 1024 px | A clipped strip of links, no menu | Menu button and drawer |

A full-page capture shows the sticky sidebar only in the first screen of a long page. That is an artefact of
capturing the whole page in one image; in a browser the sidebar stays in view.
