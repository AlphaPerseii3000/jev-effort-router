# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-09-23

### Changed

- **The grid profiles are now English, which is a change of payload and therefore of behaviour.**
  The six criterion strings in `DEFAULT_GRID` are sent to Jev verbatim and are what it weighs; they
  were French, they are now English (`grid.py`, mirrored in `README.md`, `docs/routing-grid.md`,
  `docs/jev-decisions-api.md` and `tests/test_grid.py`). The ids, the order and the six-entry count are
  untouched, and the docstrings now say the criteria are sent in English and that rewording them is a
  behaviour change rather than an editorial one.
- Measured against the live endpoint, 4 calls per task before and 4 after (16 calls per variant, the
  decision is not deterministic so one call proves nothing):

  | Task | Chosen model, French → English | Mean confidence | Calls at/above the 0.5 threshold |
  |---|---|---|---|
  | trivial (`2+2`) | `glm-5.3-flash` → `glm-5.3-flash` | 0.477 → 0.350 | 1/4 → 0/4 |
  | code refactor | `kimi-k3` → `kimi-k3` | 0.520 → 0.550 | 3/4 → 4/4 |
  | demanding analysis | `glm-5.3` → `glm-5.3` | 0.893 → 0.893 | 4/4 → 4/4 |
  | sequential tool calling | `minimax-m3` → `minimax-m3` | 0.962 → 0.980 | 4/4 → 4/4 |

  All 16 choices are identical between the two variants: discrimination between the four task
  categories is unchanged. The only material difference is confidence on the trivial task, which drops
  from ~0.48 to ~0.35 — further below the 0.5 threshold than it already was. That task was already
  degraded in 3 of 4 French calls, so the applied model (the configured default, option 1
  `deepseek-v4.1-flash`) is unchanged in practice; but the English wording makes the tie between
  `glm-5.3-flash` and `nemotron-3-nano:30b` for a trivial prompt slightly harder to break (0.46/0.36
  versus 0.56/0.31). One task × 4 calls is thin evidence: this measures the mechanism and one
  category-level regression in confidence, not a general quality verdict.

### Fixed

- `jev_router_status` and `jev_router_route` now accept the arguments dict the host's tool registry
  passes positionally (`handler(args, **context)`). Both were declared `handler(recent=5)` /
  `handler(task="", context="")`, so the arguments dict was bound to the first parameter and every
  call that carried a parameter failed with
  `TypeError: int() argument must be a string, a bytes-like object or a real number, not 'dict'`.
  Reproduced through `tools/registry.py::dispatch`: the empty-arguments call worked, which is how it
  hid. Covered by tests that dispatch through a host-shaped call.
- **`provides_middleware:` is declared again — a deliberate reversal of the previous entry.** It had
  been removed because the manifest schema has no such field and the host warned about it on every
  load. That trade was wrong: `hermes plugins validate` diffs the manifest against what
  `register(ctx)` wires and fails the plugin with `undeclared middleware registered (not in
  provides_middleware): llm_request`. That check *is* the plugin-catalog admission gate
  (`plugin-catalog-ci.yml` runs `hermes plugins validate --install-deps` at the pinned sha), so
  omitting the key silently disqualified the plugin from the catalog while looking like harmless
  cleanup — the warning it "fixed" was cosmetic and the failure it caused was not. Reproduced and
  confirmed on Hermes 0.21.4; with the key restored, `hermes plugins validate` reports
  `declared middleware :: matches registrations` and the overall verdict is `ok: true`. The one
  load-time warning is accepted as the price of admission. `test_manifest_fields_are_known_to_the_host`
  now asserts exactly `["provides_middleware"]` as the single tolerated unknown key instead of
  demanding an empty set, and the README/manifest comments and `docs/integration-surface.md` state
  the trade-off rather than denying the field exists.
- Documentation states the plugin's scope explicitly: it is for Hermes running on **Ollama:Cloud**, and
  no other provider is routed (README, manifest description, SPEC non-goals). The README's grid table
  now gives the profiles as they are actually sent to Jev, plus an English gloss beside each.
- The grid entry `nemotron-3-nano` is now `nemotron-3-nano:30b`, the id the provider's own catalog
  uses. The bare name produced `HTTP 404: model "nemotron-3-nano" not found` and failed the whole
  turn — the one outcome the fail-open design exists to prevent.
- New `catalog.py` cross-checks a decision against the provider's cached model list (read from the
  host's `ollama_cloud_models_cache.json`, no credential and no socket) before the chosen model is
  put on the wire. A model the catalog proves absent is refused: the turn keeps the configured model
  and the refusal is recorded as `model_not_in_provider_catalog` with the model Jev picked. No
  catalog evidence (absent/unreadable/unexpected) means "no verdict" and routing proceeds as before.
  `jev_router_status` reports offending grid entries under `grid_unavailable`.

## [0.1.0] - 2026-09-22

First release.

### Added

- `llm_request` middleware that asks TypeSafe Jev (`typesafe/jev-1.13` over OpenRouter's Decisions API) for
  a model and a reasoning-effort level on the first provider request of every user turn, and rewrites the
  outgoing provider kwargs accordingly.
- Six-model routing grid, keyed by position, with the criteria sent to Jev as `{"1": "<model>: <profile>"}`.
- Per-family reasoning-effort translation table: never escalates, never invents a level, omits the field
  rather than risk a 400. Kimi K3's `medium` maps to `high` per its documented vocabulary.
- Confidence guard (default `0.5`): a distrusted answer degrades to the configured default model and
  effort, and the turn still goes out routed but flagged.
- One decision per user turn, memoised on `turn_id` so a turn's whole tool loop replays it without
  disturbing the prompt cache. `route_per_turn: false` routes once per session instead.
- Durable JSONL audit trail under `plugin-data/jev-router/routes.jsonl`: choice, probabilities,
  confidence, alternatives, latency, degradation reasons and turn/session identifiers. Skipped turns are
  recorded with their reason.
- Fail-open on every path: timeout, HTTP error, malformed answer, low confidence, off-grid model, unknown
  provider or API mode all leave the request byte-identical to a plugin-disabled run.
- Operator surfaces: `/jev-router` slash command, `hermes jev-router` CLI family (`status`, `route`,
  `grid`, `tail`, `reset`), and the `jev_router_status` / `jev_router_route` agent tools.
- 74 tests, driving the real middleware callback against a stub Decisions API with no network access.

[0.1.1]: https://github.com/AlphaPerseii3000/jev-router/releases/tag/v0.1.1
[0.1.0]: https://github.com/AlphaPerseii3000/jev-router/releases/tag/v0.1.0
