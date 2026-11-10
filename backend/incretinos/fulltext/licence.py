"""Whether a document's full text may be stored, from its licence as the source returned it.

Only three bases allow storing (and redistributing in offline fixtures) a full text: CC BY, CC0 / public-domain
dedication, and US-government works (17 U.S.C. § 105). Every NonCommercial or NoDerivatives licence, every
unknown or missing licence and every "all rights reserved" text keeps metadata, hashes, the extracted table cells
and short locator quotes only. The decision is made from the licence string and URL exactly as returned and is
stored with its reason, so a reviewer can check it.

Europe PMC's ``isOpenAccess`` flag is recorded but is not, by itself, a permission to store: an open-access
article under CC BY-NC-ND is free to read and still may not be redistributed in full here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CC_BY = "CC BY"
CC0 = "CC0"
US_GOV = "US-GOVERNMENT-WORK"


@dataclass(frozen=True)
class LicenceDecision:
    storable: bool
    normalised: str | None  # CC BY | CC BY-SA | CC0 | CC BY-NC | CC BY-NC-SA | CC BY-ND | CC BY-NC-ND | US-GOVERNMENT-WORK | None
    reason: str


def classify(licence: str | None, url: str | None = None, *, us_government_work: bool = False) -> LicenceDecision:
    """Read a licence string ("cc by", "cc by-nc-nd", "CC BY 4.0") or URL (creativecommons.org/licenses/by/4.0/)."""
    if us_government_work:
        return LicenceDecision(True, US_GOV, "US-government work (17 U.S.C. § 105): no copyright in the United States")
    text = " ".join(t for t in (licence, url) if t).lower()
    if not text.strip():
        return LicenceDecision(False, None, "no licence was returned: full text is not stored")
    if re.search(r"publicdomain/zero|\bcc0\b|cc-?zero", text):
        return LicenceDecision(True, CC0, "CC0 public-domain dedication")
    m = re.search(r"cc[\s-]?by(?:[\s-](nc|nd|sa))?(?:[\s-](nc|nd|sa))?(?:[\s-](nc|nd|sa))?", text)
    if not m:
        m2 = re.search(r"creativecommons\.org/licenses/([a-z-]+)/", text)
        parts = m2.group(1).split("-") if m2 else []
        if not parts or parts[0] != "by":
            return LicenceDecision(False, None, f"licence {licence or url!r} is not one that allows storing the full text")
        terms = parts[1:]
    else:
        terms = [t for t in m.groups() if t]
    name = "CC BY" + ("-" + "-".join(t.upper() for t in terms) if terms else "")
    if "nc" in terms or "nd" in terms:
        return LicenceDecision(False, name, f"{name}: NonCommercial / NoDerivatives terms; only metadata, hashes, extracted cells and short quotes are kept")
    if "sa" in terms:
        return LicenceDecision(False, name, f"{name}: ShareAlike terms are not among the licences this platform stores in full (CC BY, CC0, US-government works)")
    return LicenceDecision(True, name, f"{name}: redistribution with attribution is permitted")
