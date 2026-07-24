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

When no source explicitly labels a number as "AUM" or "assets under management,"
accept these as size proxies, in priority order:

a. "regulatory assets under management" (Form ADV / SEC filings) — treat as AUM,
   tag AUM_CONFIRMED, basis "regulatory AUM (Form ADV)".
b. "$X invested" / "$X deployed" / "$X in acquisitions" /
   "$X transaction volume since inception" — treat as a FLOOR estimate,
   tag AUM_ESTIMATED, basis "cumulative invested capital, proxy".
c. "portfolio valued at $X" / "owns $X of real estate" — tag AUM_ESTIMATED,
   basis "portfolio value, proxy".
d. Fund size announcements ("closed $X fund", "raised $X") — tag AUM_ESTIMATED,
   basis "fund size, proxy".

Rules:
- Set aum.value_usd to the proxy number. NEVER leave value_usd null when any
  of the above appears in the sources.
- Do NOT invent numbers. Square footage or asset counts ALONE (without a dollar
  figure) are NOT proxies — leave value_usd null, tag AUM_UNVERIFIED, PARK.
- If multiple proxies conflict, use the most recent dated one; if undated, use
  the largest (conservative for a ceiling check, generous for a floor check).

## AUM confidence tagging
Every firm researched gets one tag:
- AUM_CONFIRMED: AUM from source priority 1–4, OR regulatory AUM from Form ADV (proxy rule a).
- AUM_ESTIMATED: AUM from proxy rules b/c/d, OR from a floor-proxy language statement within range.
- AUM_UNVERIFIED: no reliable basis found after exhausting all sources and proxy rules -> PARK.

## Gate logic for AUM_ESTIMATED values (asymmetric by proxy type)

- ESTIMATED value inside $200M–$2B → **QUALIFY**. Reason must include the proxy
  basis, e.g. "QUALIFIED (est. $800M — invested capital proxy)".
- ESTIMATED value above $2B → **DISQUALIFY**. The ceiling applies regardless of
  source. "DISQUALIFIED (est. $8B — fund size proxy, exceeds ceiling)".
- ESTIMATED value below $200M from a FLOOR-type proxy (invested/deployed capital,
  proxy rule b) → **PARK**, not disqualify. Cumulative invested capital is a floor;
  true AUM could be higher. Reason: "PARKED (proxy $150M below floor — invested
  capital is a floor, verify manually)".
- ESTIMATED value below $200M from a non-floor proxy (portfolio value, fund size —
  proxy rules c/d) → **DISQUALIFY**. These are closer to actual value, not floors.
- AUM_CONFIRMED below $200M → **DISQUALIFY** (no change from today).

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
  - "PARKED (proxy $150M below floor — invested capital is a floor, verify manually)"
  - "AUM not found, no dollar proxies in sources, parked"

## Notes
- This is a binary gate. There is NO numeric score and NO HOT/WARM/COLD tiering.
- A firm either qualifies (proceed to executive discovery, enrichment, drafting)
  or it does not (drop, or park if only the AUM is unverifiable).
