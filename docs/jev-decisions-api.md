# Jev Decisions API — verified wire contract

Companion of SPEC.md. Verified 2026-09-22 against the OpenRouter model page for `typesafe/jev-1.13`,
OpenRouter's Jev tutorial, the Decisions API reference, and TypeSafe's "How to build with System One".

## Endpoint

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
  "state": { "user_message": "...", "recent_context": "...", "surface": "cli", "provider": "ollama-cloud" },
  "questions": {
    "model_route": {
      "type": "choice",
      "instructions": "Quel modèle est le plus adapté pour traiter cette tâche ?",
      "criteria": {
        "1": "deepseek-v4.1-flash: généraliste, ...",
        "2": "kimi-k3: code et agentiques haut de gamme, coûteux",
        "...": "..."
      }
    },
    "reasoning_effort": {
      "type": "choice",
      "instructions": "Quel niveau d'effort de raisonnement ce message nécessite-t-il ?",
      "criteria": { "low": "...", "medium": "...", "high": "..." }
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
