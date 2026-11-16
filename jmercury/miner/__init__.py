"""Reference miner: brief -> OpenRouter LLM -> one static page (``generate``), served over btauth/1 (``server``).

Needs only the base install (no Playwright, no Node). If the ``validator`` extra is installed too, the
generator checks its own output with the validator's gates before answering.
"""

MINER_VERSION_PREFIX = "jmercury-ref-miner"
