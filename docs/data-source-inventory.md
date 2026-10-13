# Data source inventory

All sources are public. Retrieval date for the committed fixtures: **2026-10-13**.

| Source | Access | What is taken | Evidence tier | Stored | Licence / terms note |
|---|---|---|---|---|---|
| **ClinicalTrials.gov** | API v2 `GET /studies/{nct}` and `GET /studies` (search) | Protocol (design, arms, eligibility, outcomes, status, dates, references) and posted results (outcome measures, analyses, baseline, participant flow, adverse events) | Registry protocol / registry-posted results | Full record, canonical JSON, SHA-256 hash, one snapshot per content change | U.S. Government work; stored in full. Results are sponsor-submitted and NLM quality-controlled, not peer reviewed. |
| **PubMed** | E-utilities `esearch` (`NCT…[si]`) and `efetch` | PMID, DOI, title, journal, date, authors, publication types, DataBank accession numbers (NCT) | Peer-reviewed publication (for journal articles) | Bibliographic metadata; **abstract text is not stored or committed** — only its SHA-256, its length and the short excerpts that anchor curated values. Full abstracts are cached locally (git-ignored) when fetched live. | NLM metadata is freely usable; abstracts may be publisher copyright. |
| **FDA labeling** | openFDA `drug/label.json` (brand + manufacturer) | Indications, dosage, boxed warning, contraindications, warnings, adverse reactions, clinical pharmacology, clinical studies, recent major changes | FDA prescribing information | Section text in full; hash over section text; versioned | U.S. Government work / CC0 via openFDA. |
| **FDA label database check** | openFDA `drug/label.json` by generic name | Whether any label exists for an investigational drug (dated negative result) | Regulatory | Query and count | As above. Used to re-check investigational status at every ingestion. |
| **FAERS** | openFDA `drug/event.json` count endpoints | Total reports per drug, reports per MedDRA preferred term, reports per quarter, whole-database totals for disproportionality | Spontaneous reports | Counts only | CC0. Unvalidated; no exposure denominator; drug matched in any role. |
| **Lilly press releases** (read from PR Newswire copies) | HTTP GET of each cited page, in memory | Topline results curated by hand with a locator quote | Sponsor press release | URL, dates, SHA-256 of page text, and the quotes that matched verbatim. **Page text is discarded.** | Third-party copyright; not redistributed. investor.lilly.com refused automated retrieval (HTTP 403), so identical releases were read from PR Newswire. |
| **Lilly pipeline page** | Read during research | Phase, indication and mechanism statements | Sponsor pipeline disclosure | URL, date, locator quotes (pending review — the page refused automated re-fetch) | Third-party copyright; not redistributed. |
| **FDA review documents and approval letters** (accessdata.fda.gov, Drugs@FDA) | Read during research | Approval date, bioequivalence basis, review statements | FDA review document | URL, date, locator quotes (PDFs are not machine-anchored → pending review) | U.S. Government work; text not stored because PDF extraction is not automated. |
| **ICH E9(R1)** | Cited by reference | Estimand framework and intercurrent-event strategies | Guidance | URL only | Not quoted; no text stored. |

## Volumes in the committed build

45 trials ingested in depth; 245 Lilly-led trials in the registry index; 106 linked publications; 4 FDA labels (Trulicity, Zepbound, Mounjaro, Foundayo); FAERS series for dulaglutide, tirzepatide and orforglipron; 41 web sources; 207 sources in total; 1,897 result rows (1,638 machine-extracted from the registry, 155 quote-anchored, 104 pending review); 724 between-arm analyses; 16,260 adverse-event rows.

## How verification is assigned

| Status | Meaning | Assigned by |
|---|---|---|
| Machine-extracted | Parsed from a structured API payload whose snapshot hash is stored | Registry adapter |
| Quote-anchored | Curated value whose locator quote was found verbatim in the source text (stored text for registry/label; excerpts verified against the live abstract or page when fixtures were recorded) | Curation loader |
| Pending human review | Curated value that could not be anchored automatically | Curation loader; a review-queue item is opened |
| Human-verified | A reviewer approved it | Only a recorded review decision — none exist in the committed build |

## Rate limits and etiquette

Keyless budgets are conservative (≈3 requests/s to ClinicalTrials.gov and openFDA, 2.5/s to NCBI; 8/s to NCBI with an API key). Requests retry with exponential backoff and jitter on 408/425/429/5xx and honour `Retry-After`. A 404 is treated as a definitive answer and is recorded so that offline runs reproduce it. openFDA rejects `count` queries with `limit=1000`; the adapter requests 500 terms.

## Provenance of curated values

Curated evidence was drafted from AI-assisted retrieval notes (`research/web-notes/*.json`), each finding carrying its source URL and a short verbatim locator quote. `backend/scripts/notes_to_curated.py` is the only path from those notes into the product; it skips registry-sourced findings (the adapter extracts those itself), keeps a number only if its digits appear in its own quote, and lists everything it rejected in `research/web-notes/conversion-report.md`. Hand-curated files exist for ACHIEVE-4 (non-inferiority analyses) and for the orforglipron arm formulations.
