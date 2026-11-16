# Running a validator

What exists today is the validator's scoring code and the client that asks miners; the chain loop (metagraph,
sampling miners, `set_weights`) is not wired yet. The scoring rules are in [DESIGN.md](DESIGN.md) section 3 and the
judge's rubric in [RUBRIC.md](RUBRIC.md); nothing here changes them.

## Requirements

- Python 3.10+; `pip install -e ".[validator]"` adds Playwright 1.63.0 (pinned so every validator renders with the
  same Chromium build) and html5lib (gate 2 parses HTML the way a browser does) to the base install (pydantic,
  bittensor 11.3.2).
- Chromium: `python -m playwright install chromium`, with its system libraries and fonts.
- Node.js ≥ 22.19 and Lighthouse 13.6 from the lock file: `npm ci --ignore-scripts` (installed in `./node_modules`).
- An [OpenRouter](https://openrouter.ai/) API key for the judge. No GPU.

## Environment

| Variable | Meaning |
| --- | --- |
| `OPENROUTER_API_KEY` | required for judging. Sent only in the `Authorization` header; never logged or written to a record. |
| `JM_JUDGE_MODEL` | required for judging, **no default**: the judge model is chosen on testnet by measuring how often a model agrees with itself on repeated identical comparisons ([DESIGN 3.3](DESIGN.md#33-judged-quality-llm-pairwise)). All validators must use the same slug. |
| `JM_LLM_BASE_URL` | overrides `https://openrouter.ai/api/v1` (tests). |
| `JM_CHROMIUM` | path to a Chromium binary instead of Playwright's. |
| `JM_EXTRA_LIB_DIR`, `FONTCONFIG_FILE` | for hosts without Chromium's system libraries or fonts (see the README). |

## Score sites locally

```
python -m jmercury.brief --seed 42 > brief.json
python -m jmercury.validator.score --brief brief.json --site ./site/                        # gates + measured
python -m jmercury.validator.score --brief brief.json --site ./a/ --vs ./b/ --judge         # plus one judged pair
```

Without `JM_JUDGE_MODEL` or `OPENROUTER_API_KEY`, `--judge` stops with exit code 2 and says which is missing.

## Ask miners

`jmercury/validator/client.py`:

```python
from jmercury.validator.client import query_miners
results = query_miners(validator_wallet, [(miner_url, miner_hotkey_ss58), ...], brief)
responses = {uid: (r.bundle_b64, r.response_time) for uid, r in zip(uids, results) if r.ok}
```

Each request is a btauth/1-signed `SiteTask` bound to the miner's hotkey. The whole exchange must finish in 180 s;
the answer may be at most about 2.9 MiB (a 2 MB bundle in base64 plus the echoed brief); it must be signed by the
hotkey that was asked, for this validator and this request, and must echo the brief. Every failure comes back as a
`MinerResult` with `ok=False` and one of `bad_url`, `connect_error`, `timeout`, `http_status`,
`response_too_large`, `bad_signature`, `bad_body`, `protocol_version`, `brief_mismatch`, `no_bundle`. Where the
`(url, hotkey)` pairs come from (the metagraph) is left to the chain code.

Then `jmercury.validator.score.score_round(brief, responses, judge)` gates, renders, measures and judges the round
and returns the round record.

## The judge

- `OpenRouterJudge` (`jmercury/validator/judge.py`) sends one chat call per pair and order, temperature 0, seed 0,
  at most 1,500 output tokens, with the rubric from [RUBRIC.md](RUBRIC.md) in the system prompt.
- The reply must be the JSON object alone, or exactly one fenced `json` block containing it with nothing around
  it. Anything else (no JSON, two blocks, text before or after, a wrong schema, an HTTP failure) counts as a tie for
  that order, as DESIGN 3.3 says.
- The round record lists the judge model, the number of calls, token usage and which model and provider pairs
  answered (OpenRouter can route one slug to several providers).

### Images per call

For each of the two sites, in this order:

| Image | Size (px) | Count |
| --- | --- | --- |
| phone first screen | 390 × 844 | 1 |
| phone full page, cut top to bottom | 390 × up to 1,560 per part | 1–3 (the full page is capped at 4,000 px tall) |
| desktop first screen | 1280 × 800 | 1 |

So **6 to 10 images per call**, and every image's long edge is at most 1,560 px. The full-page screenshot is cut into
parts because a single 390 × 4,000 image would be downscaled by most vision models to a width too small to read; at
1,560 px the parts fit the common 1,568 px long-edge limit without resizing. The judge is told which part is which.
This is how the full-page screenshot of DESIGN 3.3 is sent, not an extra input. The visible text is sent too, cut at
12,000 characters per site.

### Estimated input tokens per call

Estimates, not measurements: no real judge call has been made yet. Image tokens use Anthropic's published
approximation, width × height / 750 (other vendors count differently; OpenAI-style tile counting gives roughly
425, 765 and 1,105 tokens for the three sizes). Text uses about 4 characters per token.

| Part | Tokens |
| --- | --- |
| phone first screen, 390 × 844 | ≈ 440 |
| one full-page part, 390 × 1,560 | ≈ 810 |
| desktop first screen, 1280 × 800 | ≈ 1,370 |
| one site, short page (one part, 844–1,560 px) | ≈ 2,250–2,620 |
| one site, longest page (three parts, 4,000 px) | ≈ 3,900 |
| system prompt with rubric, brief, labels | ≈ 1,300 |
| visible text, per site | ≈ 300–3,000 |

For the example site (`examples/seed42`, a 2,439 px page in two parts) one call is 8 images ≈ 6,100 image tokens and
about 7,400 characters of text (≈ 1,900 tokens): **≈ 8,000 input tokens**. The range is about 6,000 to 15,000 input
tokens per call; output is at most 1,500 tokens (a verdict is usually a few hundred).

A round with K miners has K(K−1) calls (56 at K = 8), so roughly 0.35–0.85 million input tokens per round at
K = 8 (6,000–15,000 per call). Check the per-image limits and image pricing of the model you are given before running.
