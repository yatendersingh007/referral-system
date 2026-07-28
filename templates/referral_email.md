# Referral email template — Workflow C (v2: firm-personalized)
#
# This is an LLM generation prompt, not a slot-fill template.
# The LLM receives this prompt + the Phase 2/4 research object and generates
# the full email body. Fixed sections are reproduced verbatim. Personalized
# sections are written from research.
#
# Design principle: the email must read like Harp wrote the whole thing himself
# after a phone call with Manpreet. The personalized blocks must be
# indistinguishable from the fixed blocks in tone and rhythm.
#
# Slots resolved before the LLM call:
#   {{poc_first_name}}  - from connections/Apollo
#   {{poc_email}}        - from connections/Apollo
#   {{firm_short}}       - LLM-derived common name (no legal suffix)
#   {{research_object}}  - JSON: firm_oneliner, disclosed_figure,
#                          disclosed_label, asset_class, geography,
#                          capital_stack, recent_event, hook_1, hook_2,
#                          pain_point_angle, service_fit (from Phase 2/4).
#                          disclosed_figure/disclosed_label (FIX 22) are
#                          the ONLY AUM-shaped fields — exactly what the
#                          firm published, verbatim, never a haircut or
#                          derived number.

## Subject (FIXED, never changes)
Intro via Manpreet Singh (Singh Capital)

## To
{{poc_email}}

## Email structure: LLM generates within these blocks

### BLOCK 1 - Greeting + Manpreet line (FIXED, verbatim)

Hi {{poc_first_name}},

Manpreet Singh suggested I reach out — he's the founder of Singh Capital Partners, and both a client and investor at X Analyst.

### BLOCK 2 - Firm-specific hook (LLM-GENERATED, 1-2 sentences)

One or two sentences that show we know {{firm_short}}'s actual business. The job
of this block is to make the recipient stop skimming and start reading. It sits
between the Manpreet intro and Harp's credentials, so it must feel like a
natural bridge: "Manpreet connected us, here is why I looked you up, and
here is what I do."

Reference a specific, verifiable fact from {{research_object}}: their asset
class, a recent deal or fund close, a portfolio milestone, their geographic
focus, number of units/properties, or a public event. This is NOT a compliment.
It is a fact that proves homework.

FIX 10 (real-mode Kairos Real Estate Partners run): if the research object
has a disclosed_figure value, this block MUST include it — the gate
already verified this figure before qualifying the firm; leaving it out
entirely is a real, confirmed defect this rule closes, not an option.

FIX 22 (real-mode In-Rel Properties run): disclosed_figure /
disclosed_label are the ONLY AUM-shaped fields this block may state —
exactly what the firm itself published, verbatim, no arithmetic. NEVER
state a haircut or derived number instead: a $4.4B disclosed GAV firm
gates internally at ~$1.76B (60% leverage haircut) and qualifies, but the
email states "$4.4B", never "$1.76B" — the haircut is machinery for the
gate decision, not a fact about the firm. Phrase it according to aum_tag
and proxy_type, honestly:
- aum_tag AUM_CONFIRMED: state disclosed_figure plainly — "You manage
  $450M in AUM..." It is a confirmed fact.
- aum_tag AUM_ESTIMATED and proxy_type "gav": disclosed_figure IS the
  firm's own disclosed figure (Gross Asset Value, disclosed_label reads
  something like "gross asset value") — state it plainly too, e.g. "You
  run $4.4B in GAV across...". Do not call it "estimated" and do not
  substitute the internal haircut-adjusted figure — it is a real, publicly
  disclosed number, just gross rather than net of leverage.
- aum_tag AUM_ESTIMATED, proxy_type "floor" or "non_floor": disclosed_figure
  is a proxy dollar figure, not a confirmed AUM — phrase it as one, and
  match the proxy's ACTUAL shape rather than implying it's a current asset
  base:
  - an invested/deployed-capital proxy (proxy_basis mentions "invested
    capital" or "deployed") is a CUMULATIVE figure summed over the firm's
    whole operating history, not a snapshot of what it currently manages
    (FIX 16, real-mode Kairos run) — phrase it with a "since
    <founded_year>" framing: "roughly $254M deployed since 2013". NOT
    "$254M across the portfolio" or "$254M deployed across [markets]" —
    that phrasing implies a currently-held asset base, which invested
    capital is not (matches the deck's own "Capital Invested (since
    2013)" stat-tile label — see deck_filler.py's _aum_label_for).
  - a portfolio-value/fund-size proxy IS a snapshot-style figure, so
    phrasing it as an estimate of current holdings is fine: "with an
    estimated $X portfolio value...", "roughly $X in fund size...".
  Never state a genuine proxy as flat, unqualified AUM ("You manage $254M
  in AUM" overclaims certainty the research doesn't have).
- aum_tag AUM_ESTIMATED and proxy_type "units": there is NO dollar figure
  at all for this firm — disclosed_figure holds the raw disclosed
  QUANTITY (a unit count or square footage, e.g. "3,000,000 sq ft" or
  "21,794 units"), never a dollar estimate. State the quantity itself,
  using disclosed_label for what it measures: "you manage roughly
  3,000,000 sq ft of office and retail space" or "you run about 21,794
  units across the Southeast." NEVER invent or state a dollar figure for
  this proxy type (no "~$240M", no "roughly $X in assets") — no such
  number was ever disclosed by the firm, and none should appear anywhere
  in this email.

Rules:
- Must reference something specific to THIS firm. No generic RE industry talk.
- Must be verifiable from research sources. No invented details.
- Every number (dollar figure, unit count, state count, square footage)
  must be copied VERBATIM from the research object. Never round,
  re-derive, or paraphrase a figure that already exists there — if
  research says 17 states and $4.4B, write 17 states and $4.4B, not
  "several states" and not a nearby number that sounds about right.
- No superlatives ("impressive portfolio", "remarkable growth"). State facts.
- No "I noticed that..." or "I was impressed by..." or "I came across..."
  State the fact the way you would say it on a phone call, as context for
  why you are writing.
- If research is thin (thin_personalization flag), use the firm_oneliner from
  Phase 2 as the hook. One sentence is fine.
- 1-2 sentences. 15-30 words. Tight.

Good example:
"You run about 15,000 multifamily units across Texas and the Southeast with
LP, GP, and co-GP on each deal. That is a lot of moving parts at month-end."

Another good example:
"Singh Capital and {{firm_short}} sit in the same corner of the market, so
Manpreet knows your model well. Your recent $200M fund close caught my
attention for a specific reason."

Bad example:
"I was really impressed by your remarkable portfolio and wanted to reach out
about an exciting opportunity to discuss potential synergies."

### BLOCK 3 - X Analyst credentials (FIXED, verbatim)

I built the India property and fund accounting teams for Magnolia Capital and 29th Street Capital from scratch. The full stack: property-level books and month-end close, bank reconciliations, AP/AR and tenant billing, CAM reconciliations and true-ups, budget vs. actual variance reporting, lender packages and covenant reporting, owner distributions and monthly owner reports, across Yardi, RealPage, and Entrata.

### BLOCK 4 - Pain point, tailored to firm (LLM-GENERATED, 2-3 sentences)

This block replaces Harp's original generic pain paragraph with one that hits
closer to home. The structure stays the same: "you are stuck between two bad
options, and the inertia is costing you." But instead of talking about
"mid-market firms" in general, talk about {{firm_short}} specifically.

Use {{research_object.pain_point_angle}} and {{research_object.service_fit}}
to connect the pain to their actual operations.

Rules:
- Keep the core insight: "overpaying a provider OR burning your controller's
  nights." Connect it to what we know about their fund structures, asset
  class, entity count, or reporting complexity.
- The core insight above MUST appear in the FIRST sentence, not a later one.
  The length guard drops trailing sentences first when the email runs over
  budget, so anything essential has to lead — a Block 4 that opens with pure
  scene-setting and saves the "overpaying vs. burning nights" choice for a
  second sentence risks losing that choice entirely to trimming before it
  ever ships. The closing beat below is different: it may land in a later
  sentence, and it is expendable if the email runs long — only the core
  insight is required to lead.
- End with something close to "That inertia has a price, and you're paying
  it every close." The line works. Keep the spirit.
- 2-3 sentences. 30-50 words.
- No buzzwords. See banned words list below.
- Write it like Harp would say it across a table. If it sounds like a
  consulting deck, rewrite it.
- Do NOT repeat a number or fact Block 2 already used (same unit count, same
  AUM/GAV figure, same state count). Block 4 must add something NEW to the
  picture, not restate Block 2 in different words. If the only pain point you
  can think of is "the same fact Block 2 already used, but bigger stakes,"
  find a different angle: entity count, reporting cadence, LP mix, systems in
  use — anything that isn't the number Block 2 already spent.
- Name the CONCRETE cost, not an abstract risk. "Risks breaking investor
  reporting" or "risks disrupting X" is consultant-speak, not Harp's voice —
  it describes a category of bad outcome instead of the specific person
  losing specific hours to a specific problem. Say what actually happens:
  whose night it costs, what the workaround looks like, what breaks first.
- The example numbers below ("40+ entities," "15,000 units," "six states")
  are STYLE samples, not facts about this firm. Never reuse one of them
  unless the research object independently confirms that exact number for
  THIS firm — copying an example's number is exactly the kind of invented
  figure the number-fidelity check below is built to reject.

Good example:
"With LP, GP, and co-GP structures across 40+ entities, your controller is
either paying a fund admin who does not know your waterfalls, or doing it
in-house at 11pm. Neither works. That inertia costs you every quarter-end."

Another good example:
"At 15,000 units across six states, your property accounting is either
running through an outsourcer who has never opened your Yardi instance, or
it is landing on your controller every month-end. Both cost more than they
should."

Bad example:
"Many firms in the real estate space face challenges with their accounting
operations, and we believe there is an opportunity to optimize your processes
and enhance efficiency across your portfolio."

Another bad example (abstract risk instead of a concrete cost, and repeats
Block 2's unit count instead of adding a new fact):
"Managing roughly 21,000 units for family offices and third-party LPs means
onboarding a new operations partner risks breaking investor reporting and
performance verification."

### BLOCK 5 - X Analyst value prop (FIXED, verbatim)

X Analyst removes the friction: we hire from the same talent pool the institutional shops recruit — top 0.5% CA/CPAs, pay them top of market (~$30/hour, among the best-paid finance professionals in India), build a real culture around them in our office, and deploy them as your dedicated operator. Exclusively yours, embedded in your systems and your close calendar, live in 10 days.

### BLOCK 6 - CTA (FIXED, verbatim)

Can we do a call next week? I'd bring our Head of Delivery, ex-Blackstone and Goldman Sachs, one of the first people to transition large funds' operations to India. He's seen every version of this move; thirty minutes with him is valuable even if we never work together.

### BLOCK 7 - Signature + P.S. (FIXED, verbatim)

Harp
Founder, X Analyst

P.S. Manpreet can vouch for the work.

---

## Generation rules (system-level, not shown in email)

1. LENGTH GUARD: Total email body max 260 words. The fixed blocks are about
   204 words once assembled. Block 2 adds 15-30 words. Block 4 adds 30-50
   words. If the generated blocks push past 260 total, trim Block 4 first,
   then Block 2.

2. TONE: Harp's voice. Direct, confident, zero filler, no corporate speak.
   The fixed blocks ARE the reference. Read them before generating. The
   generated blocks must sound like the same person wrote them in the same
   sitting. If you put the email in front of someone and asked "which parts
   did a machine write?" they should not be able to tell.

3. ANTI-AI WRITING RULES (mandatory, every rule is a hard constraint):

   Punctuation:
   - No em dashes in generated blocks (Block 2 and Block 4). The fixed blocks
     use them because that is Harp's real writing. The generated blocks must
     not, because doubling them across the email is the single biggest tell
     that a machine touched it. Use commas, periods, or rewrite the sentence.
   - No semicolons in generated blocks.
   - Straight quotes only ("like this"), never curly.

   Banned phrases (never appear in generated blocks):
   - "I noticed," "I came across," "I was impressed," "it's clear that"
   - "exciting opportunity," "unique opportunity," "great opportunity"
   - "I wanted to reach out," "I'd love to connect"
   - "at the intersection of"
   - "not just X, it's Y" (negative parallelism)
   - "when it comes to"
   - "in today's market"

   Banned words (never appear in generated blocks):
   - leverage, optimize, streamline, synergy, unlock, empower
   - crucial, pivotal, vital, key (as adjective), landscape (figurative)
   - robust, comprehensive, holistic, innovative, cutting-edge
   - enhance, foster, cultivate, showcase, underscore, highlight (verb)
   - delve, navigate, spearhead, bolster, garner, resonate
   - seamless, transformative, groundbreaking
   - strategic, scalable (as marketing adjective), tailored, bespoke

   Sentence structure:
   - Vary sentence length. Mix short (5-8 words) with medium (12-18 words).
     No three consecutive sentences of similar length.
   - Start sentences with the subject, not "Additionally," "Furthermore,"
     "Moreover," "Notably," or "Importantly."
   - Do not start two consecutive sentences the same way.
   - Do not end two consecutive sentences with the same word.

   Content:
   - No compliments about the recipient or their firm. Facts only.
   - No questions in Block 2 or Block 4. Block 6 CTA is the only question.
   - Every fact must come from the research object. If you cannot verify it,
     do not write it.
   - Every number must be copied verbatim from the research object, not
     re-derived or rounded. A generated block with a number that does not
     match the research object is treated as a failure and replaced with the
     generic fallback — see NUMBER FIDELITY below.
   - Block 4 must not restate a number Block 2 already used.

4. REFERRAL CLAIM DISCIPLINE (HARD RULE 8): Block 2 and Block 4 may reference
   the Manpreet connection as context ("part of Manpreet's network" is fine,
   "Singh Capital and {{firm_short}} sit in the same corner" is fine).
   They must NEVER fabricate specifics ("Manpreet mentioned your Q3 raise"
   or "Manpreet speaks highly of your work").

5. SOURCE DISCIPLINE: Every firm-specific claim in Block 2 must trace to a
   source URL in the research object.

5a. NUMBER FIDELITY (mechanically enforced, not just a prompt instruction):
   every dollar figure or count in Block 2 and Block 4 is checked against the
   research/personalization object after generation. A number that does not
   appear there is treated as ungrounded and the whole block is replaced
   with the generic fallback (see FALLBACK below) — the same block is never
   partially edited to fix one bad number, since a hand-patched sentence
   reads as obviously mutilated. Likewise, if Block 4 repeats a number
   Block 2 already used, both blocks fall back.

6. FALLBACK (thin_personalization): If research is too thin to write a
   credible firm-specific Block 2, use the firm_oneliner from Phase 2 as a
   single sentence. Block 4 reverts to Harp's original generic pain paragraph:
   "Most mid-market firms are still running this one of two ways: overpaying
   a provider whose partners never open their ledger, or burning their
   controller's nights on work a dedicated property accountant should own.
   Not because either works well, because switching feels harder than
   overpaying. That inertia has a price, and you're paying it every close."

7. FINAL SELF-CHECK: Read the full assembled email and answer three questions:
   (a) Can you tell where the template ends and the personalization begins?
       If yes, rewrite the generated blocks.
   (b) Does Block 2 make you want to keep reading, or does it feel like
       filler between the Manpreet line and the credentials? If filler, make
       the fact sharper.
   (c) Does Block 4 feel like it is about THIS firm, or could you swap in
       any firm name and it still works? If swappable, make the pain more
       specific to their operations.
