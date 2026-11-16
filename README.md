# J Mercury subnet

A Bittensor subnet idea, at the draft stage: **miners build phone-first websites for local service businesses,
validators score them with public rules.**

It grows out of [J Mercury](https://jmecury.org/), which builds and runs websites for landscapers, roofers, HVAC,
plumbers, dentists, remodelers, med-spas and commercial contractors.

- Design: [`docs/DESIGN.md`](docs/DESIGN.md) — the task, scoring, copy protection, roadmap, open questions.
- Judge rubric: [`docs/RUBRIC.md`](docs/RUBRIC.md) — what the LLM judge is asked, word for word in spirit.
- Miner guide: [`docs/MINER.md`](docs/MINER.md). Validator guide: [`docs/VALIDATOR.md`](docs/VALIDATOR.md).

Code layout: `jmercury/` holds what both sides share (brief, bundle, wire protocol, OpenRouter client);
`jmercury/validator/` the gates, rendering, Lighthouse measurement, judge, round scoring and the client that asks
miners; `jmercury/miner/` the reference miner (generator and HTTP server).

## Status

- [x] Design draft (v1: frontend only, single page, no JavaScript)
- [x] Validator scoring code: brief generator, gates 1-4, rendering checks, measured quality (Lighthouse)
- [ ] Pairwise judge against a real model (code and prompt done, calls go through OpenRouter; so far tested only
      with a fake transport, and no judge model is chosen yet)
- [x] Reference miner: one OpenRouter call per brief, deterministic fixes, self-check, signed HTTP server (tested
      with a fake LLM only)
- [ ] Chain wiring (metagraph, axon discovery, `set_weights`)
- [ ] Testnet

Nothing is registered on chain yet. Feedback on the design is welcome — open an issue.

## Run a miner

Needs Python 3.10+ and an [OpenRouter](https://openrouter.ai/) API key. No browser, no Node.js.

```
python3 -m venv .venv
.venv/bin/pip install -e ".[miner]"
export OPENROUTER_API_KEY=...                      # never commit it
export JM_MINER_MODEL=anthropic/claude-haiku-5.5   # optional; this default is a starting point only

.venv/bin/python -m jmercury.brief --seed 42 > brief.json
.venv/bin/python -m jmercury.miner.generate --brief brief.json --out ./site/   # one site, printed usage
.venv/bin/python -m jmercury.miner.server --wallet.name miner --wallet.hotkey default \
    --allow-hotkey <validator hotkey ss58>                                      # serve validators
```

With the validator extra installed as well (`pip install -e ".[miner,validator]"` plus Chromium), the generator
checks its own output with the validator's gates and the 390 px checks before answering. Details, the prompt and
what the reference miner fixes on its own: [`docs/MINER.md`](docs/MINER.md).

## Run a validator

Needs Python 3.10+, Chromium (installed by Playwright), Node.js 22.19+ for Lighthouse, and an OpenRouter API key
for the judge.

```
python3 -m venv .venv
.venv/bin/pip install -e ".[validator,dev]"
.venv/bin/python -m playwright install chromium   # Chromium build used for rendering and Lighthouse
npm ci --ignore-scripts                            # Lighthouse, installed in ./node_modules

.venv/bin/python -m jmercury.brief --seed 42 > brief.json
.venv/bin/python -m jmercury.validator.score --brief brief.json --site examples/seed42/site/
.venv/bin/python -m pytest
```

`--site` takes a directory, a `.zip` or a `.b64` file. Useful flags: `--no-measure` (skip Lighthouse),
`--runs N` (Lighthouse runs, default 3), `--out DIR` (screenshots and the JSON result), `--with-text`.

A pairwise judgement (`--vs ./other-site/ --judge`) calls OpenRouter with `OPENROUTER_API_KEY` and the model in
`JM_JUDGE_MODEL`, which has no default: the judge model is still to be chosen on testnet. The judge prompt embeds
[`docs/RUBRIC.md`](docs/RUBRIC.md) verbatim. More in [`docs/VALIDATOR.md`](docs/VALIDATOR.md).

If Chromium will not start because system libraries are missing, install them with
`.venv/bin/python -m playwright install-deps chromium` (needs root), or point `JM_EXTRA_LIB_DIR` at a directory that
holds them. On a host with no fonts, set `FONTCONFIG_FILE` to a fontconfig file that lists a font directory.

## License

MIT, see [`LICENSE`](LICENSE).
