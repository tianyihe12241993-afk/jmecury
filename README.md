# J Mercury subnet

A Bittensor subnet idea, at the draft stage: **miners build phone-first websites for local service businesses,
validators score them with public rules.**

It grows out of [J Mercury](https://jmecury.org/), which builds and runs websites for landscapers, roofers, HVAC,
plumbers, dentists, remodelers, med-spas and commercial contractors.

- Design: [`docs/DESIGN.md`](docs/DESIGN.md) — the task, scoring, copy protection, roadmap, open questions.
- Judge rubric: [`docs/RUBRIC.md`](docs/RUBRIC.md) — what the LLM judge is asked, word for word in spirit.

## Status

- [x] Design draft (v1: frontend only, single page, no JavaScript)
- [x] Local scorer: brief generator, gates 1-4, rendering checks, measured quality (Lighthouse)
- [ ] Pairwise judge against a real model (code and prompt done; so far tested only with a fake client)
- [ ] Reference miner (a hand-made example site for `--seed 42` is in [`examples/seed42/`](examples/seed42/))
- [ ] Validator and miner neurons
- [ ] Testnet

Nothing is registered on chain yet. Feedback on the design is welcome — open an issue.

## Run the local scorer

Needs Python 3.10+ and, for measured quality, Node.js 22.19+.

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium   # Chromium build used for rendering and Lighthouse
npm ci --ignore-scripts                            # Lighthouse, installed in ./node_modules

.venv/bin/python -m jmercury.brief --seed 42 > brief.json
.venv/bin/python -m jmercury.score --brief brief.json --site examples/seed42/site/
.venv/bin/python -m pytest
```

`--site` takes a directory, a `.zip` or a `.b64` file. Useful flags: `--no-measure` (skip Lighthouse),
`--runs N` (Lighthouse runs, default 3), `--out DIR` (screenshots and the JSON result), `--with-text`.

A pairwise judgement (`--vs ./other-site/ --judge`) calls an OpenAI-compatible `/chat/completions` endpoint set by
`JM_JUDGE_BASE_URL`, `JM_JUDGE_MODEL` and `JM_JUDGE_API_KEY`. The judge prompt embeds
[`docs/RUBRIC.md`](docs/RUBRIC.md) verbatim.

If Chromium will not start because system libraries are missing, install them with
`.venv/bin/python -m playwright install-deps chromium` (needs root), or point `JM_EXTRA_LIB_DIR` at a directory that
holds them. On a host with no fonts, set `FONTCONFIG_FILE` to a fontconfig file that lists a font directory.

## License

MIT, see [`LICENSE`](LICENSE).
