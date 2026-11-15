# Decider API wires — verified wire contracts

Companion of SPEC.md. Verified against TypeSafe Jev Decisions API and OpenAI-compatible endpoints (including OpenCode `opencode-go`).

The plugin supports three pluggable decider backends configured via `decider`:
1. `jev` (default): TypeSafe Jev on the OpenRouter Decisions API.
2. `openai_compatible`: OpenAI-compatible Chat Completions endpoint (e.g. OpenCode Go).
3. `jev_console`: OpenCode Console SystemOne API (`https://opencode.ai/inference/systemone/v1/systemone`).

Wire 4 is **not** a fourth decider: it is the opt-in secondary transport `jev_console` may consult
after its own chain fails technically.

---

## Wire 1: TypeSafe Jev Decisions API (`decider: jev`)

### Endpoint

```
POST https://openrouter.ai/api/alpha/decisions
Authorization: Bearer $OPENROUTER_API_KEY
Content-Type: application/json
```

Optional ranking headers: `HTTP-Referer`, `X-Title`. Jev is **not** served by the OpenAI-compatible chat
completions endpoint; chat SDKs do not work against it. Single provider (TypeSafe): $0.042/M input, output
free, 32K context, p50 latency 0.27 s (p99 0.55 s over the last week), 100% uptime / 99.88% availability
over three days. `typesafe/jev-latest` tracks the newest Jev; this plugin pins `typesafe/jev-1.13` by
default so a silent model swap cannot change routing behaviour under the operator.

## Request shape

```json
{
  "model": "typesafe/jev-1.13",
  "state": {
    "user_message": "...",
    "recent_context": "...",
    "previous_turn": { "tool_calls": 3, "tools": ["terminal", "read_file"] },
    "last_tool_results_excerpt": "… tail of the last tool output, redacted",
    "surface": "cli",
    "provider": "ollama-cloud"
  },
  "questions": {
    "model_route": {
      "type": "choice",
      "instructions": "Which model is best suited to handle this task?",
      "criteria": {
        "1": "deepseek-v4.1-flash: the usual choice for general work: everyday writing…",
        "2": "kimi-k3: strongest at complex code and long agentic tasks…",
        "...": "..."
      }
    },
    "reasoning_effort": {
      "type": "choice",
      "instructions": "Pick the LOWEST reasoning effort that fully covers the work this turn will need…",
      "criteria": { "low": "...", "medium": "...", "high": "...", "max": "..." }
    }
  }
}
```

`state` may be a string, an object, or an array; the object form is used here because the recommended
pattern is to send only the narrow context each question needs and to reference nested values with
backticked paths.
`instructions` is the question; `criteria` is a map for `choice`, a list for `score`, a `{true, false}`
map for `noul`. Questions are evaluated independently and in parallel — a decision cannot be conditioned
on another answer at the wire level, which is why the effort question is asked independently and any
coupling is resolved in code.

When the effective grid carries a **single** model, the plugin omits `model_route` from `questions`
and sends only `reasoning_effort`: a Choice over one option has one possible answer and a
`confidence` of 1.0 by construction, so asking it buys a certainty at the cost of tokens and
latency. The model is then `grid[0]` and the audit records `model_choice: "single"`. A model answer
the endpoint volunteers for that question is ignored.

`reasoning_effort` lists only the levels the route can actually be given (`effort.offered_levels`,
the intersection of `EFFORT_LEVELS` with the accepted levels of the provider's `effort_maps` entry
for every model on the grid). `max` is offered only where a map declares it; a route with no map
keeps `low`/`medium`/`high`. The answer is validated as a level and then resolved against the
route's declaration: a level the declaration does not carry is omitted from the wire (never clamped
to a weaker rung), and the audit keeps the asked level in `effort_requested` beside the applied
`effort`. The same holds for a level the *question* did not offer — an answer naming `max` where
only `low`/`medium`/`high` were asked is recorded as asked and omitted from the wire, even though
the route's built-in policy declares `max`: the offered set is the contract, on a fresh decision and
on a memo replay alike.

The optional turn signal is the only conversation state beyond `recent_context`:

| field | content |
|---|---|
| `previous_turn` | `{tool_calls: n, tools: [≤8 distinct names]}` for the assistant turn that answered the previous message. Omitted on a first turn. |
| `last_tool_results_excerpt` | tail (≤ 1.500 chars) of that turn's tool results, after `_clean` and `redact`; omitted when there is no tool output. |

Assistant reasoning is never forwarded, and the decision is taken once per turn, so a count of the
*current* turn's tool calls would always be zero — hence the previous turn.

## Answer shape

```json
{
  "answers": {
    "model_route":      { "type": "choice", "choice": "<one criterion key>", "probabilities": {"<key>": 0.0}, "confidence": 0.87 },
    "reasoning_effort": { "type": "choice", "choice": "medium",         "probabilities": {"medium": 0.79}, "confidence": 0.79 }
  }
}
```

- `choice` is the winning criterion **key**, not the key's description text.
- `probabilities` is the full distribution over the supplied options.
- `confidence` summarises how concentrated that distribution is; it is not a safety signal.
- `noul` answers carry a `noul` probability in [0,1]; `score` answers carry `score` on the ordered rubric.
- Probabilities vary slightly between identical calls (calibrated, not deterministic), so a decision-based
  test asserts a route landed in the allowed set, never an exact string equality of confidences.

Because `choice` returns the criterion key, the plugin builds `criteria` as `{"1": "<model-id>: <profile>", ...}`
and maps the returned key to the model id at index/key lookup, so the human-readable profile text can be
rewritten without breaking the mapping. The brief's example, which put the description into the option
value and split on `":"`, is replaced by this keyed form.

## Failure modes the plugin must handle

| Wire condition | Behaviour |
|---|---|
| HTTP 4xx/5xx, timeout, DNS failure | no route; send an audit record with `fallback_reason`; return `None` |
| `answers` missing a question, or wrong `type` | no route; `fallback_reason: "malformed_answer"` |
| `confidence` below the configured threshold | no route; `fallback_reason: "low_confidence"` |
| `choice` key not in the criteria we sent | no route; `fallback_reason: "unknown_choice"` |
| chosen model not in the configured grid | no route; `fallback_reason: "model_not_in_grid"` |
| `OPENROUTER_API_KEY` unset | plugin inert; one INFO line at registration, no per-turn noise |
| 402 / insufficient credits | no route; `fallback_reason: "upstream_error"` with the status code in the log |

The error body is logged truncated and with the `Authorization` value never included. A route is always
best-effort: the turn must be byte-identical to an unrouted turn when anything in this table fires.

---

## Wire 2: OpenAI-Compatible Decider Endpoint (`decider: openai_compatible`)

### Endpoint

```
POST {decider_base_url}/chat/completions
Authorization: Bearer ${DECIDER_API_KEY_ENV}
Content-Type: application/json
User-Agent: Hermes-Agent/0.21.4 (jev-effort-router)
x-opencode-session: jev-{timestamp}   # added when base_url contains 'opencode'
```

For example, when routing via OpenCode:
- `decider_base_url: https://opencode.ai/zen/go/v1`
- `decider_model: deepseek-v4.1-flash`
- `decider_api_key_env: OPENCODE_GO_API_KEY`

### Request shape

```json
{
  "model": "deepseek-v4.1-flash",
  "messages": [
    {
      "role": "system",
      "content": "You are an expert LLM routing decider for Hermes Agent..."
    },
    {
      "role": "user",
      "content": "Available models for provider opencode-go:\n- deepseek-v4.1-flash: general coding\n- kimi-k3: complex multi-file refactoring\n\nUser prompt:\nrefactor the payment module\n\nPick the model and effort. Output JSON only."
    }
  ],
  "temperature": 0.0,
  "response_format": {"type": "json_object"}
}
```

### Answer shape

When the effective grid carries a **single** model, the plugin omits the model block from the
prompt and drops the `model` field from the required JSON: a one-option question has one possible
answer and a confidence of 1.0 by construction, so asking it buys a certainty at the cost of tokens
and latency. The model is then `grid[0]`, the audit records `model_choice: "single"`, and a model
field the model volunteers anyway is ignored. Multi-model grids keep the block, the rule and the
strict membership validation below.

The decider model must respond with valid JSON adhering to:

```json
{
  "model": "kimi-k3",
  "effort": "high",
  "confidence": 0.95
}
```

### Strict validation rules on Wire 2

Unlike unstructured chat responses, the decider's output is subjected to strict type and membership validation:

1. **JSON Object**: The payload must parse as a JSON dictionary (handling Markdown code blocks ````json ... ```` if emitted).
2. **Grid Membership**: `model` is required and **must strictly belong to the grid sent for that provider**. If the LLM proposes an unoffered model (e.g. `gpt-4o` when only `opencode-go` models were supplied), it is rejected immediately.
3. **Valid Effort**: `effort` is required and must be one of `low`, `medium`, or `high`.
4. **Finite Numeric Confidence**: `confidence` must be a finite float between `0.0` and `1.0`. String values, NaN, infinity, or values out of bounds trigger immediate validation failure without hallucinating a default confidence.
5. **Fail-Open Policy**: Any validation violation, HTTP error, timeout, or missing key immediately aborts routing (`decide` returns `(None, reason)`), keeping the original turn request 100% byte-identical.

---

## Wire 3: OpenCode Console SystemOne API (`decider: jev_console`)

### Comparison: OpenCode Console vs OpenCode Go vs OpenRouter

| Feature / Aspect | OpenCode Console (`jev_console`) | OpenCode Go (`openai_compatible`) | OpenRouter (`jev`) |
|---|---|---|---|
| **Endpoint** | `POST https://opencode.ai/inference/systemone/v1/systemone` | `POST https://opencode.ai/zen/go/v1/chat/completions` | `POST https://openrouter.ai/api/alpha/decisions` |
| **Protocol** | SystemOne typed decisions (`model`, `state`, `questions` -> `answers`) | OpenAI Chat Completions JSON mode | OpenRouter Decisions API |
| **Credential Env** | `OPENCODE_CONSOLE_API_KEY` (or `console_api_key_env`) | `OPENCODE_GO_API_KEY` (or `decider_api_key_env`) | `OPENROUTER_API_KEY` |
| **Decider Model** | `jev-1.13-free` (primary) with fallback to `jev-1.13` (paid) | `deepseek-v4.1-flash` (or custom chat model) | `typesafe/jev-1.13` |
| **Target Generation Provider** | `opencode-console` (generation model: `deepseek-v4.1-flash` only) | `opencode-go` (catalog: `deepseek-v4.1-flash`) or `commandcode` (catalog: `deepseek/deepseek-v4.1-flash`) | `ollama-cloud` (by default) |
| **Decider Role** | Steering decider only, not generation model | Steering decider only | Steering decider only |

### Endpoint

```
POST {console_endpoint:-https://opencode.ai/inference/systemone/v1/systemone}
Authorization: Bearer ${OPEN...KEY}
Content-Type: application/json
User-Agent: Hermes-Agent/0.21.4 (jev-effort-router)
```

### Request Shape

```json
{
  "model": "jev-1.13-free",
  "state": {
    "user_message": "...",
    "recent_context": "...",
    "surface": "cli",
    "provider": "opencode-console"
  },
  "questions": {
    "model_route": {
      "type": "choice",
      "instructions": "Which model is best suited to handle this task?",
      "criteria": {
        "1": "deepseek-v4.1-flash: general work, ordinary coding and tool use; 1M context"
      }
    },
    "reasoning_effort": {
      "type": "choice",
      "instructions": "Which level of reasoning effort does this message require?",
      "criteria": { "low": "...", "medium": "...", "high": "..." }
    }
  }
}
```

### Answer Shape & Fallback Model Iteration

When the effective grid carries a **single** model, `model_route` is omitted from `questions` and
only `reasoning_effort` is sent: a one-option Choice has one possible answer and a confidence of
1.0 by construction, so asking it buys a certainty at the cost of tokens and latency. The model is
then `grid[0]`, the audit records `model_choice: "single"`, and a `model_route` answer the endpoint
volunteers anyway is ignored. This is the shape of every generation catalog the plugin routes
(`opencode-console`, `opencode-go`, `commandcode`). Multi-model grids keep both questions and the
strict validation of the model answer.

The decider responds with SystemOne envelope carrying `answers`:

```json
{
  "answers": {
    "model_route": {
      "type": "choice",
      "choice": "1",
      "probabilities": {"1": 1.0},
      "confidence": 0.95
    },
    "reasoning_effort": {
      "type": "choice",
      "choice": "low",
      "probabilities": {"low": 0.90, "medium": 0.10},
      "confidence": 0.90
    }
  }
}
```

When the primary `console_jev_model` fails (HTTP error, timeout, malformed payload), the backend iterates to `console_jev_fallback_model` (e.g. standard/paid Jev on OpenCode Console). If both fail or confidence falls below threshold, the router fails open gracefully leaving the original request intact.

---

## Wire 4: CommandCode SystemOne fallback for `jev_console` (opt-in)

A second SystemOne host behind Wire 3, **off unless an operator configures all three settings**. It
covers one operational case: the console chain (free, then paid) fails *technically* — 429, 5xx, a
timeout, a dead credential — and the turn would otherwise keep the statically configured model.

Enabling it is a deliberate act with a privacy consequence, so it is never a default, never inferred
from a key being present, and never enabled for the `jev` or `openai_compatible` deciders.

### Configuration (the endpoint is the opt-in)

```yaml
decider: jev_console
console_jev_commandcode_base_url: https://api.commandcode.ai/provider/v1/systemone
```

`console_jev_commandcode_api_key_env` and `console_jev_commandcode_model` may be written out (both
values below are the defaults), but they are **not** required: once the URL names the endpoint they
complete themselves with `COMMANDCODE_API_KEY` and `typesafe/jev`, because neither is
deployment-specific. Without the URL nothing is reached, whatever the other two say.

```yaml
console_jev_commandcode_api_key_env: COMMANDCODE_API_KEY
console_jev_commandcode_model: typesafe/jev
```

| Setting | Contract |
|---|---|
| `console_jev_commandcode_base_url` | The **complete** SystemOne endpoint. No suffix is appended: the path is the provider's contract, not this plugin's to compose. Must be `https://`, and must not carry userinfo, a query or a fragment — each of those either leaks a credential into a record or sends the decision somewhere other than the documented endpoint. An unusable value disables the fallback and is not echoed anywhere. Empty disables the fallback silently: it is the consent, not a mistake. |
| `console_jev_commandcode_api_key_env` | The **name** of the variable holding the key; empty defaults to `COMMANDCODE_API_KEY` once the URL is set. The value is read from the environment at call time (`os.environ`), never from `config.yaml`, never from `auth.json`, never from a credential pool, and never written to a log or an audit record. |
| `console_jev_commandcode_model` | The decision model id asked on this host; empty defaults to `typesafe/jev` once the URL is set. CommandCode namespaces its ids, so the same Jev is `typesafe/jev` here and `typesafe/jev-1.13` on Wire 1 — they are different contracts. |

State machine:

* **empty URL** → disabled, and the deployment is indistinguishable from one that predates the
  feature: same payload bytes, same number of HTTP calls, same audit shape, same status output. A
  `model`/`api_key_env` left behind without a URL is inert and produces no warning;
* **invalid URL** → disabled. A sanitized warning names the setting at fault (never its value) and
  *only* the fallback is switched off — the console chain keeps working;
* **valid URL** → enabled for `jev_console` only, with the model and key variable defaulted.

### When the fallback is consulted

```
console primary (free) → console paid (when console_jev_fallback_model is set and differs)
                       → CommandCode, once → fail open
```

CommandCode is asked only after a **technical** failure of that chain:

| Last console outcome | Fallback consulted? |
|---|---|
| no `OPENCODE_CONSOLE_API_KEY` | yes (it is the only transport left) |
| timeout / transport exception / absent response | yes |
| HTTP non-2xx, including 400 (credit), 401/403, 404, 422, 429, 5xx | yes |
| non-JSON body, or a wire that fails SystemOne validation (`malformed_answer`) | yes |
| valid answer below the confidence threshold (`low_confidence`) | **no** |
| valid answer naming an option the grid does not carry (`unknown_choice`) | **no** |

The last two are not host failures: the console answered, the router simply could not apply the
answer, so asking a second host the same question would add latency and cost for nothing.

The outcome that matters is the **last** console attempt, not the first failure: when the paid leg
returns no response at all, the chain has failed technically even if the previous attempt's reason
was semantic. The returned reason keeps the legacy rule — a missing response has no reason of its
own, so the previous one stands — which is what keeps a deployment without the trio identical to
0.4.0.

### Request shape

The payload is Wire 3's, unchanged except for `model`:

```json
{
  "model": "typesafe/jev",
  "state": { "user_message": "...", "recent_context": "...", "surface": "cli", "provider": "opencode-console" },
  "questions": { "model_route": { "type": "choice", "criteria": { "1": "deepseek-v4.1-flash: ..." } },
                 "reasoning_effort": { "type": "choice", "criteria": { "low": "...", "medium": "...", "high": "..." } } }
}
```

Headers are built from scratch — the generation provider's `extra_headers` are **never** inherited:

```
POST {console_jev_commandcode_base_url}
Authorization: Bearer ${console_jev_commandcode_api_key_env}
Content-Type: application/json
X-Title: hermes-jev-effort-router
```

`x-cmd-zdr` is a chat-only marker and must never reach a decision request: the transport does not
send it, and the header filter strips it case-insensitively as a backstop. No `User-Agent` is
injected either — the HTTP client's own effective value is preserved, exactly as on the Wire 3 path.

### Failure classification

| Fallback outcome | Reason recorded |
|---|---|
| HTTP 4xx/5xx (credit, credential, rate limit) | `upstream_error` |
| timeout | `timeout` |
| transport exception, absent response | `upstream_error` |
| non-JSON body, invalid SystemOne wire | `malformed_answer` |
| valid answer below threshold / off-grid choice | the semantic reason (`low_confidence`, `unknown_choice`) |
| missing secondary credential | nothing is sent; the console chain's last reason stays final |

That last row matters: a missing secondary key must not turn a 429 or a timeout into `no_api_key`,
or a fallback configuration problem would hide the host failure the operator has to act on. When
*both* credentials are missing the answer is the legacy `no_api_key`.

There are no retries and no recursion: the same per-call `timeout_s`, one request, and the recorded
`latency_ms` is the cumulative cost of the whole chain.

### Audit, replay and status

* `jev_model` keeps naming the model that actually answered — `typesafe/jev` on a fallback rescue.
* `decider_fallback_reason` carries the reason of the failed attempt that preceded the successful one
  (`upstream_error`, `timeout`, `malformed_answer`, `no_api_key`). It is not `fallback_reasons`,
  which describes a degraded *answer*.
* With the trio enabled, the route record also gains two columns:
  `decider_model` (the effective model) and `decider_endpoint` (the effective URL, never carrying a
  credential). They are written for the primary, paid and fallback legs alike, and a legacy
  deployment never gains them.
* The trio is part of the memo's configuration identity: changing the endpoint, model or key
  variable — or switching the trio on/off — invalidates a replay and the call fails open instead of
  reproducing a decision the active configuration would no longer make.
* `jev_effort_router_status` reports a `commandcode_fallback` block with `configured`, `enabled`,
  `endpoint`, `model`, `api_key_env`, `api_key_present` and a sanitized `reason`. The key's *name* is
  shown; its value never is. With an empty trio the block is omitted entirely.

### Privacy and cost — read before enabling

* **No ZDR agreement.** CommandCode/SystemOne is not a zero-data-retention upstream, so enabling it
  changes where this deployment's routing context may be retained. That is why the setting is
  inert by default and why activation requires an explicit decision by the operator.
* **What leaves the machine**: exactly what Wire 3 already sends — the last user message (up to 4000
  characters), up to `context_turns` preceding turns (up to 12000 characters), the platform and
  provider names, and the grid criteria. No system prompt, no tool output, no conversation beyond
  that window, no credential belonging to another host.
* **Cost**: the host is reached on the operator's existing paid plan (GOAT) and consumes its credit.
  This plugin does not meter spend, does not top up and never purchases a plan; a credit failure
  (`400`/`402`/`429`) is reported as `upstream_error` and the turn falls open.
* **Latency**: the chain's budget is the console chain plus one more call, not a fan-out.

### Rollback

Empty the three settings and restart the gateway. No code change, no migration, and the legacy
behaviour returns byte-identical — including the absence of the two extra audit columns.
