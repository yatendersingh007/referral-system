# ICP Qualification Gate (binary, no scoring, no tiers)

A competitor firm QUALIFIES only if ALL THREE conditions are true. If any fails,
the firm is DROPPED and none of its people proceed.

## Condition 1: Firm type
Must be one of:
- Real estate (owners, operators, developers, REITs)
- Family office (with a real estate allocation)
- Private equity (real estate focused, or holding a real estate portfolio)
- Property management

## Condition 2: AUM (assets under management)
- Must be between $200M and $2B.
- Below $200M: DISQUALIFIED (too small).
- Above $2B: DISQUALIFIED (too large, likely has in-house teams).
- **Floor-proxy language rule:** If the only AUM evidence is a floor statement
  ("over $2B", "north of $3B", "more than $1B", "in excess of", "at least"),
  treat the floor as the AUM. If the floor alone exceeds $2B, DISQUALIFY.
  If the floor falls within $200M–$2B, tag AUM_ESTIMATED.
  If the floor alone is below $200M, apply the asymmetric gate logic below
  (floor-type proxy → PARK, not disqualify).
  This is a deterministic rule — do not second-guess floor statements.
- If AUM cannot be found or verified after research: do NOT guess. Mark the firm
  as PARKED with reason "AUM_UNVERIFIED" and surface it for human review. Do not
  auto-qualify or auto-disqualify.

## Condition 3: Region
- Headquartered or primarily operating in North America (USA, Canada, or Mexico).
- Outside North America: DISQUALIFIED.

## AUM research source priority (search in this order)
1. SEC/RIA ADV filings — most reliable, publicly accessible.
2. Firm website "about" or investor page — direct statement.
3. Press releases with deal sizes or fundraise announcements.
4. Industry database mentions in free press (PitchBook, Preqin references in articles).
5. Proxy estimation (see AUM proxy rules below).

## AUM proxy rules — apply ONLY when no explicit AUM figure is found in sources 1–4

**A non-currency quantity is NEVER a dollar amount.** Square footage, unit
counts, property counts, employee counts, or any other count of "how many"
or "how much space" must NEVER be written into the AUM/dollar value field,
no matter how large the number looks. "Owns and manages over three million
square feet" is a SIZE, not "$3,000,000" — do not transcribe the digits
into a dollar figure just because they happen to look plausible as one.
A firm whose only size signal is a non-currency quantity like this is
NOT a portfolio-value or fund-size dollar proxy (rules c/d below) — it is
the separate Tertiary proxy case further down, with its own dedicated
field and its own downstream dollar conversion. Never estimate a dollar
figure from a non-currency quantity yourself, and never substitute the
raw count where a dollar figure is expected.

When no source explicitly labels a number as "AUM" or "assets under management,"
accept these as size proxies, in priority order:

a. "regulatory assets under management" (Form ADV / SEC filings) — treat as AUM,
   tag AUM_CONFIRMED, basis "regulatory AUM (Form ADV)".
b. "$X invested" / "$X deployed" / "$X in acquisitions" /
   "$X transaction volume since inception" — a specific, dated cumulative
   figure. This is NOT floor-proxy LANGUAGE (see Condition 2) even though
   current AUM could genuinely differ from it either way — tag
   AUM_ESTIMATED, proxy_type "non_floor", basis "cumulative invested
   capital, proxy". (FIX 8, real-mode Kairos run: this used to be grouped
   with floor-proxy LANGUAGE as proxy_type "floor" — corrected because it
   is not itself phrased as a stated lower bound the way "over $X"/"at
   least $X" is; it is treated the same as portfolio value/fund size,
   proxy rules c/d, below.)
c. "portfolio valued at $X" / "owns $X of real estate" — tag AUM_ESTIMATED,
   basis "portfolio value, proxy".
d. Fund size announcements ("closed $X fund", "raised $X") — tag AUM_ESTIMATED,
   basis "fund size, proxy".
e. Gross Asset Value ("GAV of $X", "gross asset value of $X") — report the
   RAW GAV number as the proxy value, tag AUM_ESTIMATED, basis "GAV-derived
   equity proxy (60% LTV)". GAV is a GROSS figure — it includes debt/leverage,
   so it is NEVER the same as AUM and must NEVER be compared to the $2B
   ceiling as-is (a large, leveraged real estate firm's GAV routinely exceeds
   $2B while its actual equity/AUM is well within the $200M–$2B band). A
   deterministic function converts GAV to an estimated equity/AUM figure via
   a leverage haircut before any threshold comparison — report the raw GAV
   number and let that conversion happen downstream; do not do the
   arithmetic yourself.

## Tertiary proxy — unit count or square footage ONLY, no dollar figure at all

If exhaustive research (see "escalation" below) finds NO dollar figure of
any kind — no AUM, no GAV, no invested/deployed capital, no fund size — but
a specific unit count or square footage IS disclosed (e.g. "21,794 units
across the Southeast", "1.8M square feet"), that is a last-resort tertiary
proxy: tag AUM_ESTIMATED, proxy_type "units", and leave the AUM/dollar
value field null (never populate it with the raw quantity). Report the
disclosed quantity in whichever field actually matches what was found:
- A residential/multifamily UNIT count (e.g. "21,794 units") → the unit
  count field.
- A SQUARE FOOTAGE figure (e.g. "1.8M square feet", "over three million
  square feet") → the separate square-footage field. NEVER put a
  square-footage number in the unit-count field or vice versa — a
  deterministic function converts each at a DIFFERENT per-unit-of-measure
  rate downstream (a conservative dollar value per apartment unit is not
  the same as a conservative dollar value per square foot), so conflating
  the two produces a wildly wrong estimate.
A deterministic function estimates an equity/AUM figure from whichever
quantity was reported, using a conservative rate for that specific unit of
measure and the same leverage haircut as the GAV proxy above. Do not
estimate the dollar figure yourself, and do not convert between units and
square footage yourself — report the raw quantity exactly as disclosed and
let the conversion happen downstream. If NEITHER a dollar figure NOR a
unit count NOR a square-footage figure can be found after exhaustive
search, leave the AUM/dollar value field null, tag AUM_UNVERIFIED, PARK.

Rules:
- Set aum.value_usd to the proxy number. NEVER leave value_usd null when any
  of proxies a–d appears in the sources, or when a GAV figure (proxy e)
  appears — report the raw GAV number, not a converted one.
- Do NOT invent numbers, and do NOT do currency conversion arithmetic
  yourself (GAV→equity, units→equity, square footage→equity) — that is a
  deterministic function's job, not yours. Report the raw figures you found.
- If multiple proxies conflict, use the most recent dated one; if undated, use
  the largest (conservative for a ceiling check, generous for a floor check).
- Research must go wide, not just the firm's own domain: search AUM, GAV,
  portfolio value, unit counts, square footage, and industry press/news
  coverage, not only "about us"/investor-page-style firm-site content. If an
  initial research pass finds no dollar or unit/sq-ft signal at all,
  escalate with additional, more targeted searches (within the per-firm
  search-call budget) before concluding AUM_UNVERIFIED — do not park after
  only a handful of searches while search budget remains unused.

## AUM confidence tagging
Every firm researched gets one tag:
- AUM_CONFIRMED: AUM from source priority 1–4, OR regulatory AUM from Form ADV (proxy rule a).
- AUM_ESTIMATED: AUM from proxy rules b/c/d/e, from a floor-proxy language
  statement within range, or from the unit-count/sq-ft tertiary proxy.
- AUM_UNVERIFIED: no reliable basis found (no dollar figure AND no unit/sq-ft
  count) after exhausting all sources, proxy rules, and the tertiary proxy -> PARK.

## Gate logic for AUM_ESTIMATED values (asymmetric by proxy type)

- ESTIMATED value inside $200M–$2B → **QUALIFY**. Reason must include the proxy
  basis, e.g. "QUALIFIED (est. $800M — invested capital proxy)".
- ESTIMATED value above $2B → **DISQUALIFY**. The ceiling applies regardless of
  source, EXCEPT the units/sq-ft tertiary proxy (see below, which parks
  unless it clears the ceiling by a wide margin). "DISQUALIFIED (est. $8B —
  fund size proxy, exceeds ceiling)".
- ESTIMATED value below $200M from FLOOR-proxy LANGUAGE (an explicit stated
  lower bound — "over $X", "north of $X", "at least $X"; see Condition 2) →
  **PARK**, not disqualify. The statement itself is a floor; true AUM could
  be higher. Reason: "PARKED (proxy $150M below floor — stated as a floor,
  verify manually)".
- ESTIMATED value below $200M from a non-floor proxy (invested/deployed
  capital — proxy rule b — or portfolio value/fund size — proxy rules c/d)
  → **DISQUALIFY**. None of these are stated as a lower bound the way
  floor-proxy LANGUAGE is; they are closer to actual value. (FIX 8: proxy
  rule b moved here from the floor-type row above — a cumulative
  invested-capital figure is a specific, dated number, not itself phrased
  as "at least this much".)
- ESTIMATED value outside $200M–$2B from the units/sq-ft tertiary proxy →
  **PARK**, not disqualify (FIX 21, real-mode In-Rel Properties run) — see
  the dedicated section below. This figure is MANUFACTURED by a
  deterministic function from a hardcoded rate, not disclosed by the firm,
  so it is treated even more cautiously than floor-proxy language: below
  the floor, always park; above the ceiling, park unless the estimate
  clears it by a wide margin (2x), at which point rate uncertainty alone
  couldn't explain it away.
- AUM_CONFIRMED below $200M → **DISQUALIFY** (no change from today).

## Gate logic for GAV and tertiary unit-count proxies

- **GAV (proxy e):** never gate the RAW GAV number against the $200M–$2B band.
  A deterministic function first converts it to an estimated equity/AUM
  figure via a 60% leverage haircut (estimated equity = GAV × 0.40 — the
  midpoint of typical 50–70% real estate leverage), THEN applies the normal
  AUM_ESTIMATED gate logic above to the CONVERTED figure. Example: $4.4B GAV
  → ~$1.76B estimated equity → inside $200M–$2B → QUALIFIED. A GAV of $6B →
  ~$2.4B → exceeds the ceiling → DISQUALIFIED, even though $6B alone might
  look "close" — the point of the haircut is that the raw GAV number is
  never the number being compared to the ceiling.
- **Unit-count/sq-ft tertiary proxy:** estimated the same way, but at a
  rate specific to whichever quantity was actually reported — a
  conservative per-unit gross value for a disclosed unit count, OR a
  conservative per-square-foot gross value for a disclosed square-footage
  figure (these are DIFFERENT rates; never apply the per-unit rate to a
  square-footage number or vice versa) — converted via the same 60%
  leverage haircut. This is the LAST resort, only reached when no dollar
  figure of any kind was found after exhaustive research. UNLIKE every
  other proxy type, this figure is never actually disclosed by the firm —
  it is manufactured by a deterministic function from a hardcoded rate —
  so it is gated MORE cautiously (FIX 21, real-mode In-Rel Properties run:
  a 2M sq ft firm computing to $160M — below the floor — was previously
  DISQUALIFIED outright on a number this codebase invented, deleting a
  firm plausibly well inside the band): below the $200M floor, ALWAYS
  PARK (never disqualify) — same reasoning as floor-proxy language, since
  a rate assumption this codebase chose could easily be too conservative
  for the true value to actually be below floor. Above the $2B ceiling,
  PARK too, UNLESS the estimate clears the ceiling by 2x — at that point
  no plausible rate error would bring it back under, so it is confidently
  too big and disqualifies. Example: 3,000,000 sq ft × $200/sq ft × 40%
  (post-haircut) = $240M → inside the band → QUALIFIED. 2,000,000 sq ft →
  $160M → below the floor → PARKED (not disqualified), reason states the
  rate and haircut used, e.g. "PARKED (est. $160M from 2,000,000 sq ft ×
  ~$200/sq ft, 60% leverage haircut — derived, not disclosed; verify
  manually)".
- If neither a GAV/dollar figure nor a unit/sq-ft count exists after
  exhaustive search: AUM_UNVERIFIED, PARK (unchanged from before).

## Disclosed figure vs. gating figure (FIX 22, real-mode In-Rel Properties run)

The AUM/proxy value above (aum_value_usd) exists ONLY to drive the gate
decision — it may be a RAW GAV number a deterministic function haircuts, or
be entirely absent (the units/sq-ft tertiary proxy has no dollar figure at
all). None of that is what a prospect should ever read. Separately, always
extract:

- `disclosed_figure` / `disclosed_label` — EXACTLY what the firm itself
  published, verbatim, no arithmetic, no haircut, no conversion. For a
  disclosed $4.4B GAV: disclosed_figure "$4.4B", disclosed_label "gross
  asset value" — never the ~$1.76B haircut number, even though that haircut
  figure is what actually gated the firm in. For a disclosed $254M
  invested-capital figure: disclosed_figure "$254M", disclosed_label
  "capital invested". These two fields are independent of the gate decision
  and must be filled in whenever the source text discloses SOMETHING,
  regardless of proxy type — a qualifying firm should essentially never
  have an empty disclosed_figure, since qualification itself required some
  proxy signal to be found.
- For the units/sq-ft tertiary proxy specifically, do NOT re-transcribe the
  quantity into disclosed_figure yourself — leave it empty; a deterministic
  function builds it directly from the unit_count/sqft_count fields you
  already reported, so the number a prospect reads can never drift from the
  number that gated them in. DO still supply disclosed_label — your own
  genuine, source-grounded description of what that quantity actually is,
  e.g. "of office and retail space under management" — since only you,
  having read the source text, can supply that context.

The prospect-facing deck and email read ONLY disclosed_figure/
disclosed_label going forward — never aum_value_usd, never a haircut or
derived dollar estimate, never a raw square-footage/unit count mistaken for
a dollar amount.

## Preserve the source's own precision (FIX 24, real-mode In-Rel Properties run)

In-Rel Properties' page reads "owns and manages OVER three million square
feet" — the firm claimed a FLOOR, not an exact figure. Report the
approximator word in `quantity_qualifier` whenever the source text used
one alongside the unit_count/sqft_count quantity: "over", "more_than",
"at_least", "north_of", "approximately", "about", or "roughly". A
deterministic function prepends the canonical form of whatever you report
here to the quantity in disclosed_figure ("over 3,000,000 sq ft"), so the
final text a prospect reads never implies more exactness than the firm
itself claimed. Leave `quantity_qualifier` empty if the source stated the
quantity as a plain, unqualified number ("manages 3,000,000 square feet" —
no "over"/"about" — is just "3,000,000 sq ft", no qualifier). Do not invent
a qualifier that isn't in the source text, and do not paraphrase the
source's own word into a different one yourself — report which of the
listed canonical qualifiers the source's wording matches, or leave it
empty.

## Who to target at a qualified firm
Only higher-level management. Accept titles such as:
- Chief Financial Officer (CFO)
- VP of Finance / Finance Director
- Controller
- Managing Director / Managing Partner
- Head of Finance / Head of Operations (finance-adjacent)
Drop everyone else (junior staff, unrelated departments, interns, assistants).

## Output for each firm
- icp_result: QUALIFIED, DISQUALIFIED, or PARKED
- icp_reason: one short line that ALWAYS includes the proxy basis when applicable:
  - "RE PE fund, AUM ~$450M confirmed (firm website), US-based, qualifies"
  - "AUM ~$5B, exceeds $2B ceiling, disqualified"
  - "QUALIFIED (est. $800M — invested capital proxy)"
  - "QUALIFIED (est. $1.8B — GAV-derived equity proxy (60% LTV))"
  - "DISQUALIFIED (est. $150M — invested capital proxy, below floor)"
  - "PARKED (proxy $150M below floor — stated as a floor, verify manually)"
  - "AUM not found, no dollar proxies in sources, parked"

## Notes
- This is a binary gate. There is NO numeric score and NO HOT/WARM/COLD tiering.
- A firm either qualifies (proceed to executive discovery, enrichment, drafting)
  or it does not (drop, or park if only the AUM is unverifiable).
