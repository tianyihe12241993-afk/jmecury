# Diagnose challenge — design (proposal)

Status: **proposal for discussion. Nothing here is implemented.** The only implemented challenge is Build
([`DESIGN.md`](DESIGN.md)). Numbers marked *(initial)* are starting values to be tuned on testnet, not promises.

## 1. What it produces

An **audit of a local business's online presence**: what is wrong, how much it matters for customer calls, and what to
fix first. It is the counterpart of Build: Diagnose finds the problems, Build produces the fixed site.

The subnet has one business-brief generator, one transport and one honesty rule for both challenges. Each challenge
keeps its own moving average per miner, and the emission is split between them (section 5.6).

## 2. Why the validator plants the problems

A real audit has no answer key, and an LLM grading free text tends to reward confident writing. So in v1 every task is
a **synthetic snapshot of a fictional business into which the validator has planted known problems**. The validator
knows exactly what is wrong, so detection is scored exactly; the judge is used only for what cannot be counted (order
of priorities, specificity, reasoning).

This also answers two questions any audit product has to face: how to evaluate AI recommendations (against a known
answer), and how to stop generic answers (precision penalties, nearly clean controls and a swap test, section 5).

## 3. The task

### 3.1 Snapshot (validator → miner)

A JSON object. Everything in it is fictional: invented business names, phone numbers only in the reserved
555-01XX range, emails on `.example`, generated reviews and competitors, real US cities.

| field | content |
|---|---|
| `task_id` | random hex |
| `trade` | one of the eight Build trades: `lawn`, `roofing`, `hvac`, `plumbing`, `dental`, `remodeling`, `aesthetics`, `commercial` |
| `owner` | what the owner told us, i.e. the truth: a Build brief ([DESIGN 2.1](DESIGN.md#21-brief-validator--miner)) with name, phone, email, address, hours, services, service area, facts and owner notes |
| `site` | the business's current website: `bundle_b64` (a zip bundle, same format as Build), `visible_text` (rendered at 390 px, CSS-hidden text excluded) and `screenshots` (phone first screen 390 × 844, phone full page in parts), rendered once by the validator |
| `listing` | a business-profile-style record: `name`, `phone`, `address` (or null), `website` (or null), `hours` (or null), `primary_category`, `categories`, `description` (or null), `rating`, `review_count` |
| `reviews` | 10–30 reviews, each `{id, rating, date, text, owner_reply}`; `owner_reply` is null when the owner did not reply |
| `competitors` | 2–3 competitors, each `{name, primary_category, rating, review_count, has_website, distance_miles}` |

Timeout *(initial)*: 120 s.

### 3.2 Audit (miner → validator)

```json
{
  "task_id": "…",
  "health_score": 46,
  "findings": [
    {
      "code": "nap.phone_mismatch",
      "evidence": [
        {"source": "site", "quote": "(937) 555-0188"},
        {"source": "owner", "field": "phone"}
      ],
      "severity": "high",
      "recommendation": "Replace the old number in the header and footer with (937) 555-0142 …",
      "expected_impact": "Callers who dial the header number reach a line the business no longer answers …"
    }
  ],
  "action_plan": [
    {"finding": 0, "action": "Fix the phone number everywhere it appears on the site."}
  ],
  "other_findings": "Free text, read only by the judge."
}
```

- `health_score`: integer 0–100.
- `findings`: at most 25 *(initial)*. `code` must come from the taxonomy (section 3.3); observations outside it go in
  `other_findings`. `severity` is the miner's own call (`high`, `medium`, `low`); detection scoring uses the
  taxonomy's severity, the judge reads the miner's.
- `action_plan`: at most 5 steps, in order, each pointing at a finding by index.
- Limits *(initial)*: `recommendation` and `expected_impact` at most 600 characters each, `other_findings` at most
  2,000, the whole answer at most 64 KB.

**Evidence.** Each finding needs at least one evidence item, in one of these forms:

| form | resolves when |
|---|---|
| `{"source": "site", "quote": "…"}` | the text occurs in the site's visible text or in a file of the bundle (whitespace-normalised) |
| `{"source": "site", "file": "img/phone.png"}` | the file exists in the bundle |
| `{"source": "site", "absent": true}` | always; used for codes about something missing (section 3.3 says which) |
| `{"source": "owner" \| "listing", "field": "hours.sat"}` | the path exists in that object (a field whose value is null exists) |
| `{"source": "competitors", "field": "[1].rating"}` | the path exists |
| `{"source": "reviews", "id": "r7"}` | the review exists; an optional `quote` must occur in that review's text |

A finding with no resolving evidence is discarded: it earns nothing and costs nothing.

### 3.3 Defect taxonomy (public)

Severity weights *(initial)*: high = 3, medium = 2, low = 1. A finding **matches** a planted defect when its code is
the defect's code and at least one evidence item hits one of the defect's anchors (the last column). Codes marked
*one per snapshot* can occur at most once, so the code alone matches, as long as the evidence resolves.

| code | sev. | the defect | anchors |
|---|---|---|---|
| **Consistency** | | | |
| `nap.phone_mismatch` | high | the site or the listing shows a phone number that is not the owner's | a site quote containing the wrong number, or `listing.phone` |
| `nap.hours_mismatch` | medium | hours on the site or the listing differ from the owner's for at least one day | a site quote with the wrong hours, or `listing.hours.<day>` |
| `nap.address_mismatch` | medium | a published address differs from the owner's | a site quote with the wrong address, or `listing.address` |
| `nap.name_mismatch` | low | the listing or the site uses another form of the name | `listing.name`, or a site quote with the wrong name |
| **Listing** | | | |
| `listing.website_missing` | medium | the listing has no website | `listing.website` *(one per snapshot)* |
| `listing.hours_missing` | medium | the listing has no hours | `listing.hours` *(one per snapshot)* |
| `listing.category_generic` | medium | the primary category is generic or wrong for the trade | `listing.primary_category` *(one per snapshot)* |
| `listing.description_missing` | low | the listing has no description | `listing.description` *(one per snapshot)* |
| **Conversion** | | | |
| `conv.no_tel_link` | high | the phone number is on the site but nothing is tappable: no `tel:` link anywhere | *(one per snapshot)* |
| `conv.call_below_fold` | high | a `tel:` link exists, but none inside the first phone screen | *(one per snapshot)* |
| `conv.phone_as_image` | medium | the phone number appears only inside an image | the image file |
| **Mobile** | | | |
| `mobile.no_viewport` | high | no viewport meta tag, so phones show a shrunken desktop page | *(one per snapshot)* |
| `mobile.horizontal_scroll` | medium | the page is wider than 390 px | *(one per snapshot)* |
| `mobile.small_text` | low | body text below 12 px at 390 px | *(one per snapshot)* |
| `mobile.small_tap_targets` | low | links or buttons smaller than 24 × 24 px | *(one per snapshot)* |
| **Content** | | | |
| `content.service_missing` | medium | a service the owner offers is not on the site (one defect per missing service) | `owner.services[i]` of that service |
| `content.area_missing` | medium | the site does not say where the business works | *(one per snapshot)* |
| `content.hours_missing` | medium | the site does not show the hours | *(one per snapshot)* |
| `content.emergency_unmentioned` | medium | the owner offers emergency service, the site does not say so | `owner.facts.emergency` *(one per snapshot)* |
| **Technical / SEO groundwork** | | | |
| `seo.noindex` | high | a robots `noindex` tells search engines not to list the page | a quote of the tag *(one per snapshot)* |
| `seo.title_missing` | low | no `<title>`, or an empty one | *(one per snapshot)* |
| `seo.meta_description_missing` | low | no meta description | *(one per snapshot)* |
| `seo.no_localbusiness_jsonld` | low | no schema.org `LocalBusiness` structured data | *(one per snapshot)* |
| `seo.broken_internal_link` | low | a link to a file that is not in the bundle | a quote of the link target |
| `seo.oversized_image` | low | an image larger than 1 MB | the image file |
| **Reputation** | | | |
| `rep.low_reply_rate` | medium | the owner replied to fewer than 20% of reviews | *(one per snapshot)* |
| `rep.recurring_complaint` | medium | the same complaint (for example "never called back") in 3 or more reviews | at least 2 of those reviews' ids |
| `rep.unanswered_negative` | medium | a 1- or 2-star review from the last 6 months with no reply | that review's id |
| `rep.below_competitors` | low | rating at least 0.5 below, or review count under half of, the competitors' median | `listing.rating` or `listing.review_count` *(one per snapshot)* |

Advertising is not in the taxonomy: a snapshot carries no ad data.

## 4. How a snapshot is made

1. A brief from the shared generator, from a fresh seed. It becomes `owner`.
2. A clean starting point: a current site from one of several deliberately ordinary templates (varied structure and
   styling, unrelated to miners' Build output), a listing, reviews and competitors, none of them with a taxonomy
   defect.
3. Planted defects: 3–8 *(initial)*, drawn across categories, each made by its own injector. About one snapshot in
   five *(initial)* is a **control** with 0–2 defects, so an audit that calls everything broken loses.
4. **The answer key is what the validator's detectors find in the finished snapshot**, not the list of injections. An
   injection that another one hid, or a defect a template caused by accident, is scored as what is actually there.
   If the detectors and the injection list disagree, the snapshot is thrown away and the case is logged: it means a
   bug in an injector or a detector.
5. A **decoy** snapshot of the same trade in the same city, with different services, reviews and defects, for the swap
   test (section 5.4).

Injectors must not leave markers (comments, class names, odd formatting) that point at the defect, and templates are
varied every round.

## 5. Scoring

### 5.1 Validity gate

The answer must be signed, echo the `task_id`, match the schema and respect the limits. Otherwise the task scores 0.

### 5.2 Detection `D` (exact)

Each finding with resolving evidence is either a **match** (to a planted defect not matched yet) or a **false
positive**. A second finding that matches an already matched defect is a false positive: repeats earn nothing.

```
TP     = sum of severity weights of the matched planted defects
FP     = sum of severity weights of the codes claimed by false positives
P      = sum of severity weights of all planted defects
recall = TP / P        precision = TP / (TP + FP)
D      = 2 · precision · recall / (precision + recall)       (0 when TP = 0)
D      = max(0, 1 − FP / 6)                                   for a control with no planted defects (initial)
```

### 5.3 Health-score calibration `H`

```
true health = max(0, 100 − 5 · P)                            (initial)
H           = 1 − |health_score − true health| / 100
```

### 5.4 Judged quality `Q`

**Pairwise judge.** All miners sampled in a round get the same snapshot. Every pair of valid audits is shown to the
judge model twice, with the order swapped; a win counts only if both orders agree, otherwise it is a tie, and a reply
that fails or does not match the format is a tie. Inputs: the snapshot (owner facts, listing, reviews, competitors,
the phone first-screen screenshot and the visible text) and both audits. Criteria, in order:

1. **Prioritisation**: does the action plan put the problems that cost the most calls first?
2. **Specificity**: would the advice fit only this business (its services, area, reviews, competitors)?
3. **Actionability**: could the owner, or someone working for them, act on it this week?
4. **ROI reasoning**: is the expected impact argued from the snapshot rather than from invented numbers?

`Jd = (wins + 0.5 × ties) / comparisons`; an audit that is the only valid one gets 0.5.

**Swap test.** The judge sees one audit's action plan, with the business's name, phone, email and address removed,
and two snapshots: the real one and the decoy. It is asked which business the plan was written for, twice, with the
snapshots in both orders. `S` = 1 if it is right both times, 0.5 if once, 0 if never. A plan that could belong to
either business is generic.

```
Q = 0.8 · Jd + 0.2 · S                                        (initial)
```

**Invented claims.** For each audit the judge lists claims about the business that the snapshot does not support:
for example a page-load time, a search ranking, a number of lost calls or a revenue figure stated as fact.
Expected impact may be argued in words, or with numbers that are clearly labelled as assumptions. As in Build, a
claim is *confirmed* in a pair when both orders flag it, and an audit confirmed in at least half of its pairs scores
0 for the task.

### 5.5 Task score

```
task score = 0                                    if invalid or invented claims are confirmed
           = 0.55 · D + 0.10 · H + 0.35 · Q       otherwise            (initial)
```

### 5.6 Weights and the split with Build

Each challenge keeps its own exponential moving average per miner (α = 0.1 *(initial)*). Build takes 100% of miner
weight until Diagnose ships; after that the split starts at 50% / 50% *(initial)* and is tuned on testnet.

### 5.7 Per-category records

The round record lists each miner's recall per category (consistency, listing, conversion, mobile, content,
technical, reputation). That makes specialties visible, and they can later become separate pools if the data
supports it.

## 6. Copying and its limits

- Snapshots are fresh every round, per validator, from a random seed.
- Duplicate audits within a round (the same normalised findings and action plan): only the earliest response keeps
  its score.
- **The detectors are open source**, so a miner can run them on a snapshot and score well on `D` for every defect a
  program can find. That is accepted: `D` is groundwork, like `M` in Build, and among miners who have it the judged
  part decides. Codes that need reading (complaint themes, category fit, content gaps) and new codes added over time
  keep `D` from being entirely mechanical.

## 7. Round flow and cost

1. Generate the snapshot and the decoy; render the current site once (the same Chromium setup as Build).
2. Sample K miners *(initial K = 8)* and send them the snapshot.
3. Collect the audits (120 s); apply the validity gate; compute `D` and `H` (no browser needed).
4. Judge: K(K−1) pairwise calls (56 at K = 8) plus 2K swap-test calls (16 at K = 8).
5. Task scores → moving averages → weights; write the round record (snapshot, answer key, every match and false
   positive, every judge verdict).

Judge calls are mostly text with one or two images; their token cost has not been measured yet.

Transport is the same as Build ([DESIGN, Transport](DESIGN.md#transport)): a btauth/1-signed POST, here to
`/v1/audit-task` *(proposed)*, and a signed answer bound to the request's nonce.

## 8. Toward real work

- **Real snapshots**, only with the owner's consent and only from what the business already publishes (site,
  listing, reviews). They have no answer key, so they would be judged only, and used at first to check that skill on
  synthetic snapshots carries over to real ones rather than to set weights.
- **Outcomes**, with consent: calls and inquiries before and after changes are the only real test of "expected
  impact". They are also data no one else has.
- Every audit that reaches a real business is reviewed by a person first.

## 9. Open questions

1. Are the severity weights and the health formula right, or should some codes weigh more for some trades?
2. Once `D` saturates, should the judged part weigh more than 0.35?
3. Should the swap test use the whole audit or only the action plan?
4. Is 72 judge calls per round at K = 8 affordable, or should audits be judged against a fixed reference audit?
5. Reviews from templates (exact answer key, but learnable) or written by an LLM (more varied, but then the answer key
   depends on the LLM following its instructions)?
