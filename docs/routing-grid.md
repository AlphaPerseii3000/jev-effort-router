# Routing grid — models, profiles, and effort mapping

Companion of SPEC.md. Every consumer reads this to know what Jev may choose, what each option means, and
how a chosen level lands on each model family's wire.

## Models offered to Jev

The Choice options are exactly these six, in this order. The option string is
`"<model-id>: <one-line profile>"`; the model id is everything before the first `:`.

Every model id here belongs to the **Ollama:Cloud** provider — this plugin routes nothing else. The
profile text is sent to Jev verbatim, **in English**, and is part of the measured payload, so treat it
as data rather than documentation: it is not a display string, and rewriting or re-translating it
changes what Jev is choosing between. The criteria were moved from French to English in this pass; the
before/after measurement is recorded in the changelog.

| # | Model id (option prefix) | Profile line sent to Jev | Context | Benchmark evidence |
|---|---|---|---|---|
| 1 | `deepseek-v4.1-flash` | generalist, excellent value for money, 1M context, the default choice | 1M | Terminal-Bench near paid flagships; young model (Sep 2026), watch in production |
| 2 | `kimi-k3` | top-tier code and agentic work, expensive, reserve it for complex development tasks | 1M | best open-weight on SWE-bench and GPQA across independent benches |
| 3 | `glm-5.3` | deep scientific and logical reasoning, expensive, for tasks with high analytical demands | — | GPQA Diamond 91.7 |
| 4 | `glm-5.3-flash` | fast and economical, excellent in real use for everyday tasks | — | highest measured Intelligence Index among flash models in real usage (41.8) |
| 5 | `minimax-m3` | good speed/agentic trade-off for tool calling and sequential actions | — | 210 tok/s, good agentic score |
| 6 | `nemotron-3-nano:30b` | very high throughput, simple tasks only, avoid it for reasoning | — | 346 TPS; avoid for reasoning |

**The id here is the wire id, verbatim — tag included.** The tier was written as
`nemotron-3-nano` while the provider's catalog names it `nemotron-3-nano:30b`; the bare name
returned `HTTP 404: model "nemotron-3-nano" not found` and killed the turn outright. Verify every
grid entry against the provider's model list (the host caches it at
`<HERMES_HOME>/ollama_cloud_models_cache.json`) rather than against memory or pricing pages — a
provider-side rename is the one drift a passing offline suite cannot see.

Pricing (reference only — the plugin does not use it for decisions): deepseek-v4.1-flash $0.15/$0.60
off-peak first-party ($0.12/$0.48 on OpenRouter, doubled during UTC 01–04h and 06–10h on weekdays);
kimi-k3 $3/$15; glm-5.3 $1/$4; glm-5.3-flash $0.15/$0.50; minimax-m3 $0.60/$2; nemotron-3-nano:30b $0.06/$0.24.

The rest of the profile's Ollama:cloud catalog (`glm-5.1`, `glm-5.2`, `deepseek-v4-flash`,
`deepseek-v4-pro`, `kimi-k2.6`, `kimi-k2.7-code`, `nemotron-3-ultra`, `nemotron-3-super`, `gemma4`,
`qwen3.5`, `minimax-m2.7`, `mistral-large-3`, `gpt-oss`) is deliberately excluded: not benchmarked in
this pass, and a longer Choice list dilutes decision quality. Adding an option is a reviewed change to
this file plus a README note, not a config-only act.

## Effort options

Second Choice question, three options, in this order: `low`, `medium`, `high`. Default when confidence is
below threshold: `medium`.

## Effort-to-wire mapping

Every route lands on the `ollama-cloud` provider profile, whose own profile code
(`plugins/model-providers/ollama-cloud/__init__.py`) already clamps to
`none | low | medium | high | max` and maps `xhigh → max`, and which emits the field only when the model
is resolved as reasoning-capable. The plugin's table exists to (a) state the family's real vocabulary
where Hermes' generic clamp would be wrong, and (b) omit the field instead of risking a 400.

| Model family | Wire parameter | Accepted levels | Mapping of `low` / `medium` / `high` | Source |
|---|---|---|---|---|
| DeepSeek (V4 / V4.1) | top-level `reasoning_effort` | low, medium, high, max | `low` → low, `medium` → medium, `high` → high | `DEEPSEEK_V4_EFFORTS`, `DEEPSEEK_V4_OVERRIDES` |
| Kimi K3 | top-level `reasoning_effort` | low, high, max (medium → **high** is the declared override; server default is high) | `low` → low, `medium` → high, `high` → high | `KIMI_K3_EFFORTS`, `KIMI_K3_OVERRIDES` |
| GLM-5.3 / GLM-5.3 Flash | top-level `reasoning_effort` | low, medium, high, max (graded, live-verified, monotonic) | passthrough | `GLM53_EFFORTS` |
| MiniMax M3 | top-level `reasoning_effort` | via the Ollama:cloud profile set (none, low, medium, high, max) | passthrough | `OLLAMA_CLOUD_EFFORTS` |
| Nemotron 3 Nano | top-level `reasoning_effort` | via the Ollama:cloud profile set | passthrough; the model is offered for simple tasks, not reasoning, so a `high` answer is recorded and honoured | `OLLAMA_CLOUD_EFFORTS` |

Rules that follow from the table:

1. **Never escalate.** When the requested level is not accepted, clamp to the nearest *weaker* accepted
   level; only send the weakest accepted level when nothing weaker exists. Hermes' `clamp_effort` already
   implements exactly this and is the reference implementation for the plugin's own table.
2. **Never invent a level.** A level outside `low | medium | high` (and the family's override targets) is
   dropped, and the request goes out with no `reasoning_effort` field at all rather than a 400.
3. **`none` is a disable, not a rung.** The plugin never routes to a reasoning-off state.
4. **`max` is not reachable by Jev's three-level question.** It is a valid target only as an override
   destination (`medium → high` for Kimi K3 is; `xhigh → max` is not reachable at all).

This table is the record of what has been verified. A family added later gets its row filled in from a
live probe before it joins the grid; until then the generic clamp applies and the audit record marks the
effort as `clamped`.
