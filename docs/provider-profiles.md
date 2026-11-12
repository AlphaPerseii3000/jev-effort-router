# Provider profiles — routing beyond Ollama:Cloud (RFC)

**Status: proposal. Nothing in this document is implemented.** The shipped plugin still routes
`ollama-cloud` only, and every other provider passes through untouched. This is the design we would
build, and the questions that need an answer before it is worth building.

## Why this is the next step

The plugin is in the Hermes catalog and is maintained, but its ceiling is written into its scope: one
provider. The catalog now contains effort routers (`hermes-adaptive-effort`, `effortless-hermes`,
which also has an optional model map) and model routers (`hermes-jev-routing`). The unique claim is
narrower than it was: a *measured* grid, a confidence guard, a per-surface policy and an audit trail
that reports what was actually applied. All four are provider-neutral in principle and Ollama-only
in practice.

The obvious move — "add five more grids" — is the wrong one. It multiplies the maintenance surface
by the number of providers, makes the README's benchmark table a lie, and still does not answer the
question that decides whether per-turn model switching is even a good idea on a given provider.

## What "support another provider" can mean

Three readings, only one of which is a plugin-shaped problem.

1. **Route within the provider Hermes is already configured for.** The plugin chooses among that
   provider's models and translates the effort into that provider's wire vocabulary. This is what
   the plugin already does for Ollama:Cloud, generalised. **Recommended.**
2. **Switch the provider itself, per turn.** Rejected. The provider is more than a model id: it is a
   base URL, an auth header shape, an API mode and a client. A middleware that rewrites kwargs
   cannot open a second client, and Hermes core already has a provider-fallback path with its own
   policy. `hermes-jev-routing` reaches the same conclusion in its catalog entry: a pick on another
   provider is *reported*, not sent. Cross-provider routing is a core feature, not a plugin feature.
3. **Route between models of a provider other than Ollama:Cloud, but let the operator supply the
   grid.** This is a subset of (1), and it is the honest way to ship before benchmarks exist.

So: **within-provider routing, provider-agnostic mechanism, evidence-gated defaults.**

## The core refactor: model ids become slots

Today Jev chooses a *model id* from a six-entry list, and that list is also the mapping to the wire.
Those two jobs have to be separated.

Introduce six **capability slots**. The criteria text is the one already measured and must not
change, or the 0.2.1 measurements stop applying:

| Slot | Criterion sent to Jev (unchanged) | Ollama:Cloud mapping |
|---|---|---|
| `general` | the usual choice for general work: everyday writing, explanation, summarising, ordinary coding and tool use; 1M context; cheap for its size | `deepseek-v4.1-flash` |
| `deep-code` | strongest at complex code and long agentic tasks: multi-file refactors, deep debugging, large repositories; slow and the most expensive | `kimi-k3` |
| `rigorous` | strongest at rigorous reasoning: mathematics, logic, science, quantitative and financial analysis, where a wrong answer is costly | `glm-5.3` |
| `long-text` | best reasoning-per-cost on large text: drafting, summarising, translating and structured extraction over long documents; fast | `glm-5.3-flash` |
| `tool-throughput` | fast tool calling: long sequences of API/CLI actions, repetitive automation, high throughput | `minimax-m3` |
| `trivial` | highest throughput and lowest cost: trivial single-step requests only; weak at reasoning and at long context | `nemotron-3-nano:30b` |

Jev still answers `"1".."6"`; a provider profile maps the slot to a model id. The prompt, the keys,
the order and the fallback path on Ollama:Cloud stay byte-identical, which is the regression that
matters: **the default install must not move.**

A provider profile is then a mapping, not a grid:

```yaml
# Illustrative only. These ids are examples; every one must be checked against the
# provider's own catalog and may not exist by the time you read this.
plugins:
  entries:
    jev-effort-router:
      settings:
        provider_profiles:
          openrouter:
            models:
              general: <openrouter-generalist>
              deep-code: <openrouter-code-model>
              rigorous: <openrouter-reasoning-model>
              long-text: <openrouter-long-context-model>
              tool-throughput: <openrouter-tool-model>
              trivial: <openrouter-cheap-model>
            fallback: <openrouter-cheap-model>
            model_switching: effort_only   # see below
```

No built-in profile ships for a provider we have not benchmarked. An operator can supply one today;
the plugin validates it against `provider_models_cache.json[provider].models` and records a skip
rather than sending a model the provider does not list.

## Effort translation: delegate first, duplicate only as a fallback

Effort vocabularies are the part most likely to rot. Hermes already owns them per provider profile:
`build_api_kwargs_extras()` returns `extra_body` and top-level kwargs for Ollama:Cloud, OpenRouter,
Z.AI, DeepSeek and others, and Gemini carries its own `build_extra_body()`. The plugin currently
duplicates a subset of that in `effort.py` because it writes the final wire kwargs *after* the
profile has run.

Proposed order of preference:

1. **Ask the host.** `providers.get_provider_profile(provider)` then call
   `build_api_kwargs_extras(reasoning_config={"enabled": True, "effort": level}, supports_reasoning=True,
   model=chosen_model, base_url=..., session_id=...)`; merge only the reasoning-related keys returned.
   This keeps one source of truth and inherits every future vendor quirk for free.
2. **Fall back to the static `effort.py` table** when the profile is absent, the call raises, or it
   returns nothing for a model. This is today's behaviour and is already fail-open.
3. **Omit the field** when neither path produces a level the model accepts. Never send a generic
   `reasoning_effort` to a model whose profile says something else.

The risk is that the provider-profile API is internal and can change between Hermes releases. That
is contained by (2) and by a compatibility test that skips, rather than fails, when the profile
lookup is unavailable. The alternative — a per-provider static vocabulary owned by this plugin — is
more predictable but guarantees drift with every vendor release.

`api_mode` stays a hard gate. Phase 1 is `chat_completions` only: Ollama:Cloud, OpenRouter, Z.AI,
DeepSeek and Gemini's OpenAI-compatible path. Anthropic Messages and Responses/Codex builds carry a
different payload shape (`thinking`, `reasoning.effort`) and need their own adapter, not a new line
in a table.

## The question that decides the defaults: prompt cache

Per-turn model switching is cheap on a flat-quota provider and potentially expensive on a
cache-priced one. Prompt caches are keyed per model (and per route on aggregators). Changing the
model between turns turns a cache hit into a cache miss on the entire prefix, which on a long
working set can cost more than the model delta the router was trying to save.

That is not a reason to skip providers; it is a reason to make the switching policy per provider:

| Policy | Behaviour | Default for |
|---|---|---|
| `per_turn` | today's behaviour: decide at the first request of every user turn | `ollama-cloud` (flat quota, no cache pricing) |
| `per_session` | decide once, keep the model for the session; still re-decide effort per turn if enabled | cache-priced providers, first cut |
| `effort_only` | never change the model; route only the effort | cache-priced providers, safest default until measured |

The audit trail already records `replayed`; a cache-aware follow-up can correlate `post_llm_call`'s
usage counters with the route that produced them, but **we should not claim a cache saving before
that measurement exists.** The honest first release on a cache-priced provider is `effort_only`,
which is still the half of the plugin no model router offers.

## Compatibility and configuration

- `grid` remains a full override for operators who want today's explicit model list; it is the
  current public surface and must keep working.
- `provider_profiles` is additive. Absent, the built-in Ollama:Cloud profile is used, with the same
  six entries in the same order and the same criteria. No config migration.
- The provider gate becomes "is there a profile for this provider?" instead of
  `provider == "ollama-cloud"`. Unknown provider still means skip, with the existing audit reason.
- `default_model` becomes profile-aware. On Ollama:Cloud it stays `glm-5.3-flash`; a profile declares
  its own on-grid fallback, and the parser refuses a fallback that is not one of its slots.
- Catalog check moves from `ollama_cloud_models_cache.json` to
  `provider_models_cache.json[provider].models`, with the per-provider file as a compatibility
  fallback. No cache evidence still means "trust the decision", as today.

## Tests

The suite already drives the real middleware against a stub Decisions API. The new cases:

1. **Identity regression.** With no profile configured, the Jev payload and the rewritten request
   are byte-identical to 0.3.1 for every grid entry and effort level. This is the release gate.
2. **Slot mapping.** A fake provider profile maps each slot to a distinct id; Jev's `"3"` reaches the
   wire as that profile's `rigorous` model, not as `glm-5.3`.
3. **Provider gate.** A provider with no profile produces `provider_not_routed` and an untouched
   payload; adding a profile flips it without a restart-visible code change.
4. **Effort delegation.** A fake profile returning `extra_body.reasoning` is merged; a profile that
   raises falls back to `effort.py`; a profile returning nothing omits the field.
5. **Catalog.** `provider_models_cache.json[provider]` is consulted; a model absent from it is
   refused with `model_not_in_provider_catalog`; an absent file is "no evidence".
6. **Switching policy.** `effort_only` never rewrites `model`; `per_session` decides once per
   session; `per_turn` replays within a turn's tool loop as today.
7. **Manifest.** The new config keys appear in `plugin.yaml`, and `hermes plugins validate` still
   passes the catalog admission gate.

Nothing here needs a live provider: provider profiles and transports are stub seams already.

## Rollout

| Phase | Scope | Ships? |
|---|---|---|
| 0 | This RFC + a tracking issue; collect operator profiles | docs only |
| 1 | Slots + `provider_profiles` + provider gate + catalog + `per_turn`; Ollama identity regression | yes, no behaviour change |
| 2 | Effort delegation to host profiles + `effort_only` / `per_session`; one measured profile (OpenRouter or Z.AI) | yes, opt-in |
| 3 | Anthropic Messages / Responses adapters | only if phase 2 shows demand |
| 4 | Cache-aware policy from real usage counters | only with measurement |

Phase 1 is the expensive one (roughly a day of code plus the regression matrix); phase 2 is where the
provider-specific evidence has to be earned, and where the project's existing rule applies: a model
enters a shipped profile only with benchmark evidence, not because its name fits a slot.

## Open questions

1. **Effort strategy:** delegate to `get_provider_profile()` (one source of truth, internal API) or
   keep a static per-provider table (predictable, drifts)? Recommendation: delegate with the current
   table as fallback.
2. **Default policy on cache-priced providers:** `effort_only` (safe, less differentiated) or
   `per_session` (keeps model routing, one cache miss per session)? Recommendation: `effort_only`
   until a measured comparison exists.
3. **Which provider first?** OpenRouter is the broadest and already serves the decision endpoint;
   Z.AI is the closest to the existing GLM measurements. Recommendation: OpenRouter for reach, Z.AI
   for a cheaper, measurable benchmark.
4. **Does a shipped profile need its own benchmark table**, or is "operator-supplied, validated
   against the provider catalog" enough for phase 1? Recommendation: the latter; shipped profiles
   wait for evidence.
