# Running a miner

A miner receives a brief for a local service business and answers, within 180 s, with a one-page website as a zip
bundle. The repository ships a **reference miner** (`jmercury/miner/`): a starting point that passes the gates, not a
tuned competitor. Any way of producing the bundle is allowed; the validator only sees the bundle.

## What is scored

Read these before changing anything; they are the specification.

- The brief and the bundle rules: [DESIGN section 2](DESIGN.md#2-the-task).
- Gates (pass/fail, a failure scores 0): [DESIGN section 3.1](DESIGN.md#31-gates-deterministic-passfail) —
  bundle limits, no JavaScript and no external resources, exact facts (name, phone, `tel:` link, hours, JSON-LD
  `LocalBusiness` with the exact `openingHoursSpecification`), no fake reviews in structured data.
- Measured quality `M` (Lighthouse and 390 px checks): [DESIGN section 3.2](DESIGN.md#32-measured-quality-deterministic-within-noise).
- Judged quality `J` (pairwise vision LLM against the other miners' sites for the same brief):
  [DESIGN section 3.3](DESIGN.md#33-judged-quality-llm-pairwise) and the judge's rubric, [RUBRIC.md](RUBRIC.md).
  Invented claims (testimonials, ratings, awards, prices, years, certifications not in the brief) confirmed by the
  judge in half of a site's pairs score 0.
- Task score `0.4 M + 0.6 J`, duplicates and copying: [DESIGN sections 3.4 and 4](DESIGN.md#34-task-score-and-weights).

## Requirements

- Python 3.10+; `pip install -e ".[miner]"`. The miner extra adds nothing to the base install (pydantic and
  bittensor): the HTTP server and the OpenRouter client use the standard library. No browser, no Node.js.
- An [OpenRouter](https://openrouter.ai/) API key.
- A registered hotkey and a reachable port (chain wiring is not part of this repository yet).
- Optional: the validator extra (`pip install -e ".[miner,validator]"` and `python -m playwright install chromium`).
  With it, the generator checks its own output with the validator's own gates and renders it at 390 px.

## Environment

| Variable | Meaning |
| --- | --- |
| `OPENROUTER_API_KEY` | required. Sent only in the `Authorization` header; never logged or written to a record. |
| `JM_MINER_MODEL` | OpenRouter model slug for generation. Default `anthropic/claude-haiku-5.5`, a starting point only: pick your own by measuring. |
| `JM_LLM_BASE_URL` | overrides `https://openrouter.ai/api/v1` (tests point it at a local fake). |
| `JM_LOG_LEVEL` | server log level, default `INFO`. |

## Generate one site

```
python -m jmercury.brief --seed 42 > brief.json
python -m jmercury.miner.generate --brief brief.json --out ./site/ [--b64 site.b64] [--model vendor/model]
python -m jmercury.validator.score --brief brief.json --site ./site/      # needs the validator extra
```

The command prints a JSON summary: the checks, every attempt, and token usage (prompt, completion, total, and
cost when OpenRouter reports it). Exit code 0 = a site was written, 1 = no gate-passing site, 2 = configuration
error (for example no API key). Flags: `--check auto|validator|static`, `--temperature` (0.7), `--max-tokens`
(16000), `--seed`, `--timeout` (170 s overall budget).

## What the reference miner does

1. **One LLM call** with the prompt below and the brief as JSON. The model writes `index.html` and optionally
   `styles.css` and `favicon.svg` between `<<<FILE name>>>` and `<<<END FILE>>>` markers (a single fenced HTML
   block or a bare document is also accepted).
2. **Deterministic fixes** for what a gate checks exactly; the LLM is not trusted with these:
   - every `<script>` (the LLM's JSON-LD included), inline event handler and `javascript:` URL is removed, also in
     `favicon.svg`;
   - a JSON-LD block is built from the brief only: the trade's schema.org type (`HomeAndConstructionBusiness` for
     lawn care, `RoofingContractor`, `HVACBusiness`, `Plumber`, `Dentist`, `GeneralContractor` for remodeling and
     commercial, `HealthAndBeautyBusiness` for aesthetics), the exact name, telephone, email, address or service
     area, `openingHoursSpecification` from the brief's hours, and `foundingDate` / `knowsLanguage` only when the
     brief's `facts` have them;
   - `tel:` links with the wrong digits are rewritten; if there is none, a high-contrast call bar with
     `tel:+1<digits>` is added at the top of the body;
   - `<!DOCTYPE html>`, `lang="en"`, `<meta charset>`, `<meta name="viewport">`, `<title>` and a meta description
     are added when missing.
3. **Self-check.** With the validator extra: the validator's gate 2 (no script, no external resources), gate 3
   (facts, using the rendered visible text), gate 4, plus the 390 px checks (no horizontal scrolling, call link
   visible on the first screen). Without it: static approximations of gates 2 and 3. Always: a heuristic honesty
   lint for phrases that are usually invented (stars, review counts, testimonials, awards, prices, free offers,
   guarantees, certifications, licensed/insured/emergency/24/7 not in the brief, founding years that differ from
   `facts.founded`).
4. **At most one retry**, with the failures fed back to the model, when a gate fails or an advisory check flags
   something, and only if at least 40 s of the budget are left. A site that still fails a gate is an error
   (`GenerationError`, exit code 1, HTTP 500 from the server); a site that passes the gates but keeps an advisory
   flag is returned with warnings.

The honesty lint is a heuristic, not the judge. The judge reads the page against the brief with
[RUBRIC.md](RUBRIC.md); the lint only catches the common phrases cheaply.

### The generator prompt

The system prompt, verbatim (`jmercury/miner/generate.py`, `SYSTEM_PROMPT`); the user message is
`BRIEF (JSON):` followed by the brief and `Build the site now.`

```
You build one-page websites for local service businesses (the J Mercury subnet). A validator renders your page in
headless Chromium on a 390 x 844 phone screen with the network off and JavaScript off, checks hard rules, measures
it with Lighthouse, and has a vision model compare it with other pages built for the same brief.

HARD RULES (a page that breaks any of them scores 0):
1. Output only these files: index.html (required), styles.css (optional), favicon.svg (optional).
2. No JavaScript: no <script> elements at all (the structured data block is added for you), no on* event
attributes, no javascript: URLs, no <iframe>, <object>, <embed> or <base>, no meta refresh, no scripts in SVG.
3. No external resources: no web fonts, stylesheets, images or icons loaded from other sites, no @import of URLs,
no data: URLs. Use a system font stack (system-ui, -apple-system, "Segoe UI", Roboto, sans-serif). There are no
photos: use CSS and inline SVG for any decoration. Links to tel:, mailto:, sms: and https: map links are fine.
4. The business name and the phone number, exactly as written in the brief, must be in the visible text, and a
call link <a href="tel:+1XXXXXXXXXX"> with the brief's digits must be visible on the first phone screen.
5. Every opening and closing time in the brief must be in the visible text, written like "8:00 AM" or "8am", for
every open day; say which days are closed.
6. Text hidden with CSS does not count as visible.

HONESTY RULE (a page that breaks it scores 0): state only facts that are in the brief. Never invent testimonials,
customer quotes, star ratings, review counts, awards, rankings, "#1" or "best in" claims, prices, discounts, free
offers, guarantees, warranties, financing, founding years or years in business, certifications, memberships,
licenses, insurance, 24/7 or same-day promises, or staff names. The entries of "facts" may be stated exactly as
given (for example the license number, the founding year, emergency service, insured, languages). General wording
such as "friendly team" or "careful work" is fine.

WHAT WINS, in order of importance (the judge's rubric):
1. Five-second clarity: on the first phone screen a visitor sees what the business does, where it works and a big,
obvious call button.
2. Facts and honesty: name, phone, hours, service area and services shown correctly and consistently.
3. Fit for the trade and its customers: urgency for a breakdown, reassurance for a nervous patient, scope and trust
for a remodel. Use the owner's notes for tone and priorities; do not quote them as text.
4. Readable and usable on a phone: base font at least 16px, strong colour contrast, tap targets at least 48px tall,
nothing cut off or overlapping, no horizontal scrolling. Include
<meta name="viewport" content="width=device-width, initial-scale=1">.
5. Looks professional and trustworthy: clean, consistent, specific to this business, no filler or lorem ipsum.

Also: <html lang="en">, a <title>, a <meta name="description">, one <h1>, headings in order, header/main/footer
landmarks, a sticky or prominent call button, compact CSS. Include every section listed in "required" (call,
services, area, hours, about). The address, if the brief gives one, may be shown with an https: map link; if the
address is null the business does not publish one: show the service area instead.

OUTPUT FORMAT: write each file as

<<<FILE index.html>>>
...file content...
<<<END FILE>>>

and nothing else before, between or after the files.
```

(Line breaks above are for reading; the prompt in the code wraps differently.)

## Serve validators

```
python -m jmercury.miner.server --wallet.name miner --wallet.hotkey default \
    --allow-hotkey <validator ss58> [--allow-hotkey ...] [--host 127.0.0.1] [--port 8091]
```

- `POST /v1/site-task` takes a `SiteTask` signed with btauth/1 ([DESIGN section 5](DESIGN.md#transport)) and
  answers with the brief echoed back, `bundle_b64` and `miner_version`, signed by the miner hotkey for the calling
  validator and bound to its request nonce. `GET /health` answers `{"ok": true, "hotkey": ..., "miner_version": ...}`.
- Only hotkeys given with `--allow-hotkey` are served. A chain check (for example "has a validator permit") can be
  plugged in through `MinerServer(permit_check=...)`; it is not wired yet.
- Limits: request body 64 KiB, 10 s request age, one task at a time per validator (`--per-caller`), four in total
  (`--max-concurrent`), 150 s generation budget (`--timeout`, must stay under the validator's 180 s).
- Status codes: 400 bad body or brief; 401 missing, malformed or invalid signature; 403 wrong receiver, stale,
  replayed, not allow-listed or no permit; 404; 411 no `Content-Length`; 413 body too large; 429 per-validator limit;
  503 global limit; 500 generation failed; 504 generation too slow. Error bodies are unsigned JSON
  `{"error": code, "detail": text}`.
- The server binds `127.0.0.1` by default; put it behind your own reverse proxy or pass `--host` explicitly.
- `--dev-hotkey-uri //Bob` signs with a public development key instead of a wallet: local testing only.

## Cost notes

Token counts, not prices (prices change; see the model's OpenRouter page). Estimated from character counts at about
4 characters per token, not measured against a real model:

- Prompt per call: the system prompt is about 3,500 characters and the brief 1,200–2,000, so roughly 1,200–1,500
  input tokens.
- Output: the hand-made example site (`examples/seed42/site/index.html`) is about 8,300 characters, roughly
  2,000–3,000 tokens; generated pages are typically in that range. `--max-tokens` caps it at 16,000.
- A retry sends the prompt, the first answer and the feedback again, so a retried brief costs roughly three times
  the input and twice the output of a clean one.
- One brief per validator per round. Every response from the generator and the server's log line report the actual
  usage, so measure it on your own model before planning.
