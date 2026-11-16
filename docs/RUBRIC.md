# Judge rubric (v1 draft)

The validator's judge model compares two sites, **A** and **B**, built for the same brief. It sees the brief, three
screenshots of each site (phone first screen 390 × 844, phone full page, desktop first screen 1280 × 800) and each
page's visible text. It answers in a fixed JSON format; a reply that does not match the format counts as a tie.

In the phone full-page screenshot, anything fixed to the screen (a sticky header or call bar) is drawn where it sits on
the first screen, so it may appear over the middle of the page. The judge is told not to count that as overlapping
content.

The judge is told to picture **a homeowner or patient on a phone who has just heard the business name from a
neighbour and looked it up.**

## Criteria, in order of importance

1. **Five-second clarity** — on the first phone screen, can they tell what the business does, where it works, and how
   to contact it? Is the call button obvious?
2. **Facts and honesty** — are the brief's facts (name, phone, hours, service area, services) shown correctly and
   consistently? Does the site claim anything the brief does not give?
3. **Fit for the trade and its customers** — does it speak to the people who hire this trade (urgency for an HVAC
   breakdown, reassurance for a dental patient, scope and trust for a remodel)? Does it use the owner's notes?
4. **Readable and usable on a phone** — text size, contrast, spacing, tap targets, nothing cut off or overlapping.
5. **Looks professional and trustworthy** — clean, consistent, not template-generic, no filler text.

## Invented claims

For each site the judge lists any claim **not supported by the brief**, including:

- testimonials, quotes from customers, star ratings, review counts;
- awards, rankings, "best in", "#1", certifications or memberships not in `facts`;
- years in business or founding dates not in `facts`;
- prices, discounts, guarantees, warranties, financing not in the brief;
- staff names or credentials not in the brief.

General, unverifiable wording ("friendly team", "quality work") is not an invented claim. A site's invented claims are
*confirmed* in a pair when its list is non-empty in both judging orders; a site confirmed in at least half of its pairs
scores 0 for the task (see `DESIGN.md` §3.3).

## Output format

```json
{
  "criteria": {
    "clarity": "A" | "B" | "tie",
    "facts": "A" | "B" | "tie",
    "fit": "A" | "B" | "tie",
    "phone": "A" | "B" | "tie",
    "professional": "A" | "B" | "tie"
  },
  "invented_claims": {"A": ["..."], "B": ["..."]},
  "winner": "A" | "B" | "tie",
  "reason": "one or two sentences"
}
```

The overall `winner` is the judge's own call, weighing the criteria in the order above.
