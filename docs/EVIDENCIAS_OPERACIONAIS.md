# Evidências Operacionais — jev-effort-router (Fork & Decider Plugável)

Este documento registra a execução empírica, comandos reproduzíveis e evidências verificáveis de acordo com os critérios de aceite do card `t_1a091c5a` e as solicitações de revisão (Rounds 1 a 4).

---

## 1. Publicação e Sincronização de Refs

### 1.1 Resolução de `uv.lock`
O `uv.lock` foi explicitamente sincronizado com a especificação de dependência de desenvolvimento `ruff >=0.15.0` do `pyproject.toml`.

```bash
uv lock --check
# Saída: Resolved 17 packages in 1ms
```

### 1.2 Publicação dos Refs
Os commits de correção e de sincronização do lockfile foram publicados tanto na branch de trabalho (`wt/t_1a091c5a`) quanto na branch default do repositório (`main`):

```bash
git ls-remote --symref origin HEAD refs/heads/main refs/heads/wt/t_1a091c5a
```

Saída verificada:
```text
ref: refs/heads/main	HEAD
0060f7c69679439ee3e4298edafa7c08bb0dcd0d	HEAD
0060f7c69679439ee3e4298edafa7c08bb0dcd0d	refs/heads/main
0060f7c69679439ee3e4298edafa7c08bb0dcd0d	refs/heads/wt/t_1a091c5a
```

---

## 2. Instalação no Perfil de Teste (`test_jev`)

O plugin foi reinstalado do fork no GitHub sem seleção de branch, validando que o ref default (`main`) aponta para o commit correto:

```bash
hermes -p test_jev plugins install RVerdiF/jev-effort-router --force
hermes -p test_jev plugins enable jev-effort-router
```

### 2.1 Verificação de Metadados de Instalação
Leitura de `/home/rafael/hermes/home/profiles/test_jev/plugins/.install-metadata.json`:

```json
{
  "jev-effort-router": {
    "pinned": false,
    "revision": "0060f7c69679439ee3e4298edafa7c08bb0dcd0d",
    "source": "https://github.com/RVerdiF/jev-effort-router.git"
  }
}
```

---

## 3. Turno Real Roteado em Produção (`test_jev`)

### 3.1 Execução do Turno
Comando executado:
```bash
env OPENCODE_GO_API_KEY="[REDACTED]" hermes -p test_jev chat -q "What is 2+2? Answer with only the number."
```

Saída:
```text
Query: What is 2+2? Answer with only the number.
Initializing agent...
────────────────────────────────────────
╭─ ☤ Hermes ───────────────────────────────────────────────────────────────────╮
│ 4                                                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
Duration:       5s
Messages:       2 (1 user, 0 tool calls)
```

### 3.2 Registro de Roteamento no Audit (`routes.jsonl`)
Trecho registrado em `/home/rafael/hermes/home/profiles/test_jev/plugin-data/agent-plugin-jev-effort-router-ee542f23/routes.jsonl`:

```json
{
  "ts": "2026-10-15T22:25:14+00:00",
  "event": "route",
  "model": "deepseek-v4.1-flash",
  "effort": "low",
  "effort_requested": "low",
  "model_choice": "deepseek-v4.1-flash",
  "effort_choice": "low",
  "model_confidence": 0.99,
  "effort_confidence": 0.99,
  "model_probabilities": {"deepseek-v4.1-flash": 0.99},
  "effort_probabilities": {"low": 0.99},
  "alternatives": ["kimi-k3"],
  "latency_ms": 1511,
  "fallback_reasons": [],
  "jev_model": "typesafe/jev-1.13",
  "decider": "openai_compatible",
  "replayed": false,
  "turn_id": "20261015_192511_ce62fa:20261015_192511_ce62fa:995605d1",
  "session_id": "20261015_192511_ce62fa",
  "platform": "cli",
  "api_call_count": 1
}
```

---

## 4. Verificação de Fail-Open e Preservação Byte-a-Byte de Kwargs

O script `scripts/verify_fail_open.py` testa formalmente os cenários de falha do decider e valida a identidade exata dos parâmetros de chamada originais:

```bash
cd /home/rafael/hermes/workspaces/jev-effort-router
uv run python scripts/verify_fail_open.py
```

Saída:
```text
=== VERIFYING BYTE-IDENTICAL KWARGS ON FAIL-OPEN ===

--- Case 1: Missing API key ---
Router returned: None
VERIFIED: res is None -> original kwargs unchanged (byte-identical)

--- Case 2: Invalid decider base_url ---
Router returned: None
VERIFIED: res is None -> original kwargs unchanged (byte-identical)

--- Case 3: Middleware contract simulation ---
VERIFIED: outgoing payload is byte-identical to original_request

ALL FAIL-OPEN CHECKS PASSED!
```

Além disso, o registro de `upstream_error` em `routes.jsonl` comprova o registro de auditoria quando um erro ocorre:
```json
{
  "ts": "2026-10-15T20:08:57+00:00",
  "event": "skip",
  "reason": "upstream_error",
  "provider": "opencode-go",
  "configured_model": "deepseek-v4.1-flash",
  "turn_id": "20261015_170856_59a27e:d4032773-12a4-410d-a0b2-a627bab8542e:b18d3586",
  "session_id": "20261015_170856_59a27e",
  "platform": "cli"
}
```

---

## 5. Suíte de Testes e Opt-In Live Probe

### 5.1 Suíte Completa Offline
Por padrão, sem `JEV_LIVE_PROBE=1`, nenhuma chamada de rede é realizada:

```bash
cd /home/rafael/hermes/workspaces/jev-effort-router
env -u JEV_LIVE_PROBE -u OPENCODE_GO_API_KEY uv run --with pytest --with pyyaml pytest -q tests -o addopts= -rs
```

Resultado:
```text
........................................................................ [ 38%]
....................................................sssss..s............ [ 76%]
............................................                             [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/test_live_probe.py:40: Live probe opt-in requires JEV_LIVE_PROBE=1 and OPENCODE_GO_API_KEY
SKIPPED [4] tests/test_live_probe.py:71: Live probe opt-in requires JEV_LIVE_PROBE=1 and OPENCODE_GO_API_KEY
SKIPPED [1] tests/test_registration.py:100: could not import 'hermes_cli.plugins_manifest': No module named 'hermes_cli'
182 passed, 6 skipped in 1.04s
```

### 5.2 Execução Opt-in do Live Probe (`JEV_LIVE_PROBE=1`)
Comando reproduzível:
```bash
cd /home/rafael/hermes/workspaces/jev-effort-router
env JEV_LIVE_PROBE=1 OPENCODE_GO_API_KEY="[REDACTED]" uv run --with pytest --with pyyaml pytest -v tests/test_live_probe.py -o addopts=
```

Resultado:
```text
============================= test session starts ==============================
tests/test_live_probe.py::test_live_probe_skipped_offline_even_if_key_in_env PASSED [ 16%]
tests/test_live_probe.py::test_live_opencode_decider_call PASSED          [ 33%]
tests/test_live_probe.py::test_live_opencode_reasoning_effort_support[None] PASSED [ 50%]
tests/test_live_probe.py::test_live_opencode_reasoning_effort_support[low] PASSED [ 66%]
tests/test_live_probe.py::test_live_opencode_reasoning_effort_support[medium] PASSED [ 83%]
tests/test_live_probe.py::test_live_opencode_reasoning_effort_support[high] PASSED [100%]

============================== 6 passed in 10.74s ==============================
```

### 5.3 Prova Empírica dos Níveis de Reasoning Effort do OpenCode
Execução de `scripts/probe_opencode_levels.py` com o endpoint oficial `https://opencode.ai/zen/go/v1` e modelo `deepseek-v4.1-flash`:

```bash
uv run python scripts/probe_opencode_levels.py
```

Resultado:
```text
=== OPENCODE REASONING_EFFORT LEVEL PROBE ===
level=None            -> HTTP 200 (ACCEPTED)
level='low'           -> HTTP 200 (ACCEPTED)
level='medium'        -> HTTP 200 (ACCEPTED)
level='high'          -> HTTP 200 (ACCEPTED)
level='xhigh'         -> HTTP 200 (ACCEPTED)
level='max'           -> HTTP 200 (ACCEPTED)
level='invalid_level' -> HTTP 422 (REJECTED): {"error":{"type":"server_error","message":"Upstream request failed: Endpoint is unavailable."}}
```

Isso sustenta rigorosamente a declaração da seção "Effort-to-wire mapping (Ollama:Cloud built-in)" de `docs/routing-grid.md` e a política de mapeamento seguro (`accepted: [low, medium, high]`). **Atenção:** o conjunto aceito citado aqui ficou superado pelo §8 (`t_a0a4c186`) para a rota `opencode-go`, que passa a declarar `low`/`medium`/`high`/`max` com `xhigh`→`max`, o mesmo contrato que o core aplica nesse wire; o registro de `invalid_level` também não é estável entre execuções (ver §8.1).

---

## 6. Implementação OpenCode Console Jev & Catálogo de 2 Modelos (`t_6ec7a94d`)

> **Superado pela seção 7 (`t_14209379`)**: o catálogo Go passou a ter **um único modelo**, o MiMo foi removido de todas as grades/allowlists e a rota `commandcode` foi adicionada. As afirmações de 6.1–6.4 registram o estado entregue no card `t_6ec7a94d` e ficam mantidas como histórico; a verdade corrente está na seção 7. A política de reasoning do Go registrada em 6.1/6.4 também foi superada, agora pela **seção 8** (`t_a0a4c186`).

### 6.1 Resumo Normativo e Decisões de Escopo
- **Diretiva do Dono & Separação de Rotas (Console Decider / Go Geração)**: O dono do produto definiu formalmente a escolha do ID padrão `mimo-v2.6-flash` (não-free) na geração, rejeitando estritamente a variante `mimo-v2.6-flash-free` e suas condições de retenção/treinamento de dados. Conforme orientado pelo dono (Comentário 108), as fontes oficiais distinguem claramente os produtos:
  - **Decider Jev Console**: Opera via SystemOne (`https://opencode.ai/inference/systemone/v1/systemone`) com modelo primário gratuito `jev-1.13-free` e fallback automático para o Jev pago (`jev-1.13`).
  - **Geração OpenCode Go**: A geração dos modelos `deepseek-v4.1-flash` e `mimo-v2.6-flash` é atendida pelo provider `opencode-go`, onde a documentação oficial do OpenCode Go confirma explicitamente MiMo-V2.6-Flash normal e DeepSeek V4.1 Flash com tarifação por token.
  - **Isolamento e Segurança**: O MiMo normal não é enviado ao endpoint Console, e a variante `mimo-v2.6-flash-free` nunca é substituída. Ambas as rotas permanecem estritamente isoladas e verificadas offline sem chamadas pagas nem inferência live.
- **Fontes Oficiais e Mapping dos IDs Go**: Os IDs de geração (`deepseek-v4.1-flash`, `mimo-v2.6-flash`) vêm da lista oficial de modelos do OpenCode Go (`https://opencode.ai/v2/docs/console/go`, que nomeia MiMo-V2.6-Flash normal e DeepSeek V4.1 Flash com preço por token) e são distintos do id do decider SystemOne (`jev-1.13-free` / `jev-1.13`). A listagem pública `https://opencode.ai/inference/v1/models` é a do Console e não serve para confirmar os modelos Go. Nenhuma credencial foi lida para estabelecer esse mapping e nenhuma inferência paga foi executada; a verificação da documentação oficial foi registrada na revisão independente que pediu esta separação.
- **Catálogo Console (um modelo)**: `opencode-console` (e alias `opencode_console`) é regido por `OPENCODE_CONSOLE_MODELS = ("deepseek-v4.1-flash",)`. O MiMo normal **não** pertence a esse catálogo: o Console não o serve, e uma decisão que o nomeie nessa rota falha aberto, mantendo o modelo configurado pelo operador.
- **Catálogo Go de dois modelos (opt-in)**: `OPENCODE_GO_MODELS = ("deepseek-v4.1-flash", "mimo-v2.6-flash")` é aplicado a `opencode-go`/`opencode_go` **somente enquanto `decider: jev_console` está ativo**. O limite é um teto: entradas de `grids.opencode-go` fora do catálogo são descartadas, grade que fica vazia cai no catálogo embutido de dois modelos, e grade de política de superfície (`platform_overrides.<surface>.grid`) passa pelo mesmo corte. Deciders legados (`jev`, `openai_compatible`) preservam a grade Go que o operador declarou, sem alteração.
- **Onde o teto é aplicado**: `config.route_allowlist(provider, decider)` é a única fonte de verdade; `Settings.grid_for` / `EffectiveSettings` cortam a grade (choice, alternativas, fallback e replay leem dessa grade), `ModelCatalog.is_known(..., decider=...)` responde `False` para modelo fora do catálogo da rota, e `JevConsoleDecider._interpret` rejeita a escolha fora da allowlist antes de qualquer aplicação.
- **Política Explícita de Reasoning por Modelo**: Distingue explicitamente (1) o nível de esforço lógico que o Jev decide/devolve (`effort_requested`) e (2) o parâmetro de reasoning no endpoint de geração (`effort`). Sem contrato confirmado por modelo no endpoint de geração de cada rota, o parâmetro é estritamente omitido do wire nas duas rotas opt-in (`deepseek-v4.1-flash` no Console; `deepseek-v4.1-flash` e `mimo-v2.6-flash` no Go), sem inferir suporte de Ollama ou de outra rota e sem alegar que o modelo downstream recebeu o reasoning escolhido.
- **Operação Estritamente Offline**: Nenhuma chamada paga ou de rede foi realizada; nenhum perfil real foi modificado (conforme o gate do card, instalação e chamadas de rede exigem autorização operacional separada).

### 6.2 Execução da Suíte de Testes Offline

Comando executado no worktree isolado (`.worktrees/t_6ec7a94d`):
```bash
uv run --extra dev pytest
```

Saída obtida (execução atual, 2026-10-17):
```text
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/rafael/hermes/workspaces/jev-effort-router/.worktrees/t_6ec7a94d
configfile: pyproject.toml
plugins: anyio-4.15.1
collected 263 items

tests/test_audit.py ...........                                          [  4%]
tests/test_catalog.py ...................                                [ 11%]
tests/test_catalog_status.py ....                                        [ 12%]
tests/test_client.py ................................................... [ 32%]
.                                                                        [ 32%]
tests/test_config_policy.py ................................             [ 44%]
tests/test_effort.py ...................                                 [ 52%]
tests/test_grid.py ............                                          [ 56%]
tests/test_grid_coverage.py ....                                         [ 58%]
tests/test_live_probe.py .sssss                                          [ 60%]
tests/test_registration.py .ss..s.............                           [ 67%]
tests/test_router.py ................................................... [ 87%]
..................................                                       [100%]

======================== 255 passed, 8 skipped in 0.61s ========================
```

Todos os 255 testes offline passaram (evolução sobre os 246 da entrega anterior: 9 testes novos, nenhum removido sem substituto). Os 8 skips são exatamente: 5 testes de live probe (`test_live_probe.py`) que exigem `JEV_LIVE_PROBE=1` e chave real, e 3 testes em `test_registration.py` condicionados à biblioteca opcional PyYAML (`yaml = pytest.importorskip("yaml")`). Nenhum skip é justificado por socket bloqueado; a suíte inteira também roda com `socket.socket.connect` e `socket.create_connection` bloqueados.

### 6.3 Verificação de Cobertura nos Testes Adicionados
1. **`tests/test_client.py`**:
   - `test_jev_console_decider_happy_path`: valida wire SystemOne (`POST`, URL, Authorization header com `OPENCODE_CONSOLE_API_KEY`, payload com `state`/`questions`, decodificação de `answers`).
   - `test_jev_console_decider_fallback_to_paid_jev` / `test_jev_console_decider_fallback_model`: valida iteração do modelo primário (`jev-1.13-free`) para o secundário (`jev-1.13`) em caso de falha no primário.
   - `test_jev_console_decider_both_models_fail` / `test_jev_console_decider_network_failure_fail_open`: valida tratamento de erro 500/502 com fail-open sem exceção.
   - `test_jev_console_decider_timeout`: valida timeout de rede com marcação `REASON_TIMEOUT` e fail-open.
   - `test_jev_console_decider_http_error_matrix`: matriz com status 401, 403, 429, 500, 503 e verificação estrita de que segredos da API (`OPENCODE_CONSOLE_API_KEY`) nunca são expostos em logs/auditoria.
   - `test_jev_console_decider_real_non_json_body`: valida corpo HTML/texto não-JSON com `REASON_MALFORMED` e redaction de credenciais.
   - `test_jev_console_decider_response_defined_probability_domain`: valida que o domínio de probabilidades é estritamente restrito às opções enviadas na grade e rejeita chaves externas inventadas pela resposta com `REASON_MALFORMED`.
   - `test_jev_console_decider_invalid_types_and_float_choice`: valida rejeição de escolhas float, tipos inválidos e escolhas de esforço numéricas.
   - `test_jev_console_decider_low_confidence_degrades`: valida degradação graciosa (fail-open) em confiança abaixo do limiar.
2. **`tests/test_catalog.py`**:
   - `test_opencode_console_catalog_allowlist`: valida `is_known` True para `deepseek-v4.1-flash`, False para `mimo-v2.6-flash`, `mimo-v2.6-flash-free` e `kimi-k3` sob `opencode-console` e `opencode_console`.
   - `test_opencode_go_catalog_is_capped_only_for_the_console_decider`: valida que `is_known` para `opencode-go`/`opencode_go` responde True aos dois modelos autorizados, False ao MiMo free e a `kimi-k3` quando `decider="jev_console"`, e `None` (sem veto) sob deciders legados.
3. **`tests/test_config_policy.py`**:
   - `test_console_config_options_parsed_and_queried`: valida parsing das configurações `console_endpoint`, `console_jev_model`, `console_jev_fallback_model`, `console_api_key_env`, e métodos `base_url_for`, `decider_model_for`, `api_key_env_for`.
   - `test_opencode_console_grid_resolution`: grade efetiva do Console contém apenas `deepseek-v4.1-flash`.
   - `test_opencode_go_grid_is_the_two_model_catalog_under_the_console_decider`: grade efetiva de `opencode-go`/`opencode_go` é exatamente os dois modelos autorizados, sem herdar nenhum modelo Ollama.
   - `test_opencode_go_grid_cap_applies_to_operator_grids_without_widening_them`: `grids.opencode-go` com `unauthorized-model` é cortada; grade só com modelos fora do catálogo cai no catálogo embutido; grade de política de superfície também é cortada.
   - `test_opencode_go_grid_untouched_for_legacy_deciders`: decider legado mantém a grade Go configurada (e a grade global quando não há grade).
   - `test_materialized_schema_defaults`: valida os defaults materializados diretamente de `plugin.yaml` (`console_jev_model` = `jev-1.13-free`, `console_jev_fallback_model` = `jev-1.13`, decider default `jev`, routable_providers default `[ollama-cloud]`).
4. **`tests/test_router.py`**:
   - `test_jev_console_decider_routing_end_to_end`: valida roteamento completo ponta a ponta com `decider: jev_console` e `provider: opencode-console`.
   - `test_jev_console_decider_generation_route_go_uses_the_two_model_catalog`: usa a configuração efetiva (sem grade injetada) — `{decider: jev_console, routable_providers: [opencode-go]}` — e comprova: grade = dois modelos autorizados, `default_model = deepseek-v4.1-flash`, escolha 2 aplica `mimo-v2.6-flash`, chamada do decider vai para o SystemOne (nunca para o `base_url` Go), `reasoning_effort` ausente do wire, `effort_requested='high'` + `effort=None` na auditoria, alternativas restritas a `deepseek-v4.1-flash`, snapshots serializados de `request` e `original_request` idênticos (objetos independentes, um deles com `reasoning_config`), e replay no mesmo turno sem segunda chamada.
   - `test_opencode_go_route_cap_rejects_models_outside_the_two_model_catalog`: grade customizada com `unauthorized-model` é cortada antes da pergunta; resposta nomeando o modelo inventado é rejeitada nos dois Jev (free e pago) e o payload original permanece byte-idêntico (skip registrado).
   - `test_console_route_no_longer_applies_normal_mimo`: no Console, resposta apontando a posição 2 (MiMo normal) falha aberto, com a grade efetiva contendo só `deepseek-v4.1-flash`.
   - `test_opencode_go_legacy_decider_keeps_its_configured_grid`: com decider legado, a grade Go configurada é preservada.
   - `test_cross_provider_memo_replay_isolation`: valida que transições de provider não reutilizam memo incompatível.
   - `test_cross_decider_memo_replay_isolation`: valida que transições de decider não reutilizam memo incompatível.
   - `test_memo_replay_invalidated_by_confidence_threshold_elevation`: valida que elevação de limiar de confiança no mesmo turno invalida o memo anterior e falha aberto sem chamada duplicada.
   - `test_memo_replay_invalidated_by_fallback_backend_or_key_env_change`: valida que alteração de backend de fallback ou de variável de chave invalida o replay de memo.
   - `test_middleware_rejects_invented_criteria_or_outsider_choice`: valida rejeição de choice com critérios inventados ou opção fora da grade enviada, mantendo request byte-idêntico.
   - `test_middleware_zero_effort_confidence_replay_policy_and_no_effort_response`: valida que effort_confidence 0.0 é distinguida de ausência de resposta no replay (invalidada quando limiar sobe), enquanto resposta sem esforço não sofre regressão.
   - `test_middleware_fail_open_console_negative_matrix`: matriz completa offline (HTTP 401/403/429/500/503, timeout, non-JSON real, envelope, confidence bool/str/NaN/Inf/range/threshold, probabilidades nulas/tipos/domínio/soma, choice fora da grade/critérios inventados, effort malformado/critérios remapeados) invocando `on_llm_request` diretamente com dois objetos de request independentes e distinguíveis (top-level e aninhados), comprovando retorno None (fail-open), preservação exata dos snapshots serializados de ambos os payloads (`working_req` e `original_req`), geração do registro de skip na trilha de auditoria real (`routes.jsonl`), e ausência da chave secreta tanto nos logs (`caplog.text`) quanto na auditoria, com exerção real de redaction (`[REDACTED]`) em corpos de erro e payloads não-JSON com segredo embutido.
   - `test_middleware_fail_open_both_models_fail`: valida fail-open quando ambos os modelos (primário e fallback) falham, invocando `on_llm_request` com objetos independentes e distinguíveis, comprovando preservação dos snapshots serializados de ambos, escrita de skip na auditoria, redaction efetiva do segredo no log (`[REDACTED]`) e ausência da chave secreta na auditoria e nos logs.
   - `test_opencode_console_distinguishes_logical_effort_from_wire`: valida que o Jev decide esforço lógico (`effort_requested='high'`), enquanto o wire payload omite `reasoning_effort` e o log de auditoria registra `effort=null`, sem alegar execução downstream.
5. **`tests/test_effort.py`**:
   - `test_opencode_console_explicit_reasoning_policies`: valida a política explícita (`ModelReasoningPolicy`) do `deepseek-v4.1-flash` no Console, com `omitted=True`, `wire_parameter=None` e decisão registrada.
   - `test_opencode_go_explicit_reasoning_policies`: (**superado pelo §8**) valida a política explícita dos dois modelos Go (`omitted=True`, `wire_parameter=None`) e a omissão de um modelo fora do catálogo Go.
   - `test_opencode_go_effort_cannot_be_inferred_from_other_providers`: (**superado pelo §8**) comprova que o mesmo modelo honra o nível em `ollama-cloud` e continua omitido em `opencode-go`.
   - `test_opencode_console_effort_cannot_be_inferred_from_other_providers`: comprova que suporte em `ollama-cloud` ou `opencode-go` não é inferido para `opencode-console`.
   - `test_opencode_console_custom_effort_map_overrides_omission`: valida que mapeamentos explícitos em `effort_maps` pelo operador têm precedência e são honrados sem escalada.

### 6.4 Matriz da Política de Reasoning por Modelo

| Provedor | Modelo | Esforço Lógico (Jev) | Parâmetro no Wire | Níveis Aceitos | Evidência / Decisão |
|---|---|---|---|---|---|
| `opencode-console` | `deepseek-v4.1-flash` | Sim (`low`/`medium`/`high`) | `None` (omitido) | N/A | **Não confirmado**: sem evidência empírica ou contrato no endpoint de geração do console. Não se infere de `opencode-go` ou `ollama-cloud`. Omitido para prevenir HTTP 400. |
| `opencode-go` | `deepseek-v4.1-flash` | Sim (`low`/`medium`/`high`) | `reasoning_effort` | low, medium, high, max (`xhigh`→`max`) | **Superado pelo §8** (card `t_a0a4c186`): o probe direto de 2026-10-20 confirmou o parâmetro no endpoint Go; o nível decidido vai no wire no vocabulário do core e qualquer valor fora do conjunto declarado (`minimal`, `none`, desconhecido) é omitido. |
| `opencode-go` | `mimo-v2.6-flash` | Sim (`low`/`medium`/`high`) | `None` (omitido) | N/A | **Não confirmado**: modelo multimodal/agentic normal, sem knob de reasoning documentado. Omitido no wire. |
| `ollama-cloud` | `deepseek-v4.1-flash` | Sim (`low`/`medium`/`high`) | `reasoning_effort` | low, medium, high, max | **Verificado**: Contrato formal Ollama:cloud (`DEEPSEEK_V4_EFFORTS`). |
| `ollama-cloud` | Outros (Kimi, GLM, etc.) | Sim (`low`/`medium`/`high`) | `reasoning_effort` | Por família | **Verificado**: Mapeamento e clamping estrito sem escalada. |

---

## 7. Rota `commandcode` e catálogos de geração de um modelo (`t_14209379`)

### 7.1 Escopo entregue
- Rota `commandcode` registrada como provedor opt-in de geração, com catálogo de **um** modelo namespaced.
- `mimo-v2.6-flash` removido de todas as grades, allowlists e catálogos: `OPENCODE_GO_MODELS = ("deepseek-v4.1-flash",)`. Nenhum catálogo de geração cita MiMo.
- Efeito pretendido: com um modelo por rota a escolha de modelo é degenerada; o decider passa a decidir, na prática, apenas o nível de raciocínio.

### 7.2 Fonte do id do modelo `commandcode` (sem ler credencial)
- Endpoint público e **não autenticado** `GET https://api.commandcode.ai/provider/v1/models` (o próprio profile do provider o declara como público): resposta `HTTP 200`, 18.707 bytes, sem header `Authorization`; a lista contém `"id":"deepseek/deepseek-v4.1-flash"` e também `"deepseek/deepseek-v4.1-flash-fast"`.
- Profile do provider (`/home/rafael/hermes/core/plugins/model-providers/commandcode/__init__.py`): `api_mode="chat_completions"`, `base_url="https://api.commandcode.ai/provider/v1"`, aliases `("commandcode-chat",)`, env `COMMANDCODE_API_KEY`.
- **Id literal usado**: `deepseek/deepseek-v4.1-flash`. É namespaced de propósito: o id bare `deepseek-v4.1-flash` das rotas OpenCode é outro contrato e não é aceito nesta rota. A variante `-fast` não foi autorizada e não entra na allowlist.

### 7.3 Catálogos vigentes
- `OPENCODE_CONSOLE_MODELS = ("deepseek-v4.1-flash",)` (inalterado; rota sempre pinada).
- `OPENCODE_GO_MODELS = ("deepseek-v4.1-flash",)` (MiMo removido).
- `COMMANDCODE_MODELS = ("deepseek/deepseek-v4.1-flash",)` (novo; opt-in sob `decider: jev_console`, como o Go).
- Com deciders legados (`jev`, `openai_compatible`) nenhuma das rotas opt-in é capada: a grade declarada pelo operador continua valendo, byte a byte como antes.

### 7.4 Mecanismo e correção de assimetria
- O teto usa o mecanismo existente: `config.route_allowlist(provider, decider)` é a fonte única, `route_catalog` devolve `COMMANDCODE_GRID`/`OPENCODE_GO_GRID`, e `Settings.grid_for`/`EffectiveSettings` cortam choice, alternativas, fallback e replay. `ModelCatalog.is_known(..., decider=...)` responde `False` para modelo fora da allowlist e `JevConsoleDecider._interpret` rejeita a escolha fora dela antes de qualquer aplicação.
- `_GRID_ALIASES` passou a listar `commandcode` e agora também alimenta `is_provider_routable` por uma tabela única (`_provider_group`), corrigindo a assimetria anterior: `opencode-go` não casava com `opencode_go` e a rota era pulada em silêncio. `commandcode`, `commandcode-chat` e `commandcode_chat` normalizam entre si.

### 7.5 Política explícita de reasoning por rota/modelo
| Provedor | Modelo | Parâmetro no wire | Evidência / Decisão |
|---|---|---|---|
| `opencode-console` | `deepseek-v4.1-flash` | `None` (omitido) | Inalterado: contrato não confirmado no endpoint de geração do Console. |
| `opencode-go` | `deepseek-v4.1-flash` | `reasoning_effort` (`low`/`medium`/`high`/`max`, `xhigh`→`max`) | **Superado pelo §8** (card `t_a0a4c186`): o probe direto de 2026-10-20 confirmou o parâmetro no endpoint Go. Vigente: o nível decidido vai no wire no vocabulário do core; qualquer valor fora do conjunto declarado é omitido. |
| `commandcode` | `deepseek/deepseek-v4.1-flash` | `None` (o plugin omitia o seu) | **Superado pelo §9** (card `t_1906910b`): o probe direto de 2026-10-20 confirmou `reasoning_effort` no endpoint CommandCode, e o plugin passa a enviar o nível decidido no campo top-level, ao lado do `extra_body.thinking` do profile. Histórico desta rodada: **contrato de dois componentes, rastreado offline** (§7.5.1), com omissão deliberada do campo do plugin e `effort=null` no wire. |

#### 7.5.1 Rastreio do contrato `commandcode` (correção da revisão, rodada 1)

A revisão apontou que a justificativa anterior — "`reasoning_effort` não faz parte do contrato" — era falsa. Rastreio das fontes, sem importar o core, sem rede e sem ler credencial:

1. `core/plugins/model-providers/commandcode/__init__.py:42-59` — `build_api_kwargs_extras` reconhece ids `deepseek/*` e delega ao profile `deepseek` via registry, repassando `reasoning_config` e o id **sem** o prefixo `deepseek/`.
2. `core/plugins/model-providers/deepseek/__init__.py:28-43` — para ids `deepseek-v4*` chama `thinking_toggle_extras(reasoning_config, DEEPSEEK_V4_EFFORTS, DEEPSEEK_V4_OVERRIDES, always_emit_toggle=True)`.
3. `core/agent/reasoning_effort.py:197-219` — no caminho de nível aceito, a linha 218 devolve **dois** valores: `({"thinking": {"type": "enabled"}}, {"reasoning_effort": <nível>})`.
4. `core/agent/reasoning_effort.py:81-83` — vocabulário V4: `low`/`medium`/`high`/`max`, com `xhigh → max`.
5. `core/agent/transports/chat_completions.py:562-583` — os extras do profile entram em `api_kwargs` (linha 568) **antes** de `request_overrides` (linhas 579-583); um `reasoning_effort` deixado no payload roteado sobrescreveria a derivação do profile.

Execução offline das funções **reais** dessa fonte (AST: apenas os nós de `thinking_toggle_extras`, `clamp_effort`, `requested_effort` e as constantes; nenhum `import agent`, nenhuma chave), com os mesmos argumentos que o profile DeepSeek usa:

```text
$ uv run --offline python <scratch>/t_14209379_contract.py
DEEPSEEK_V4_EFFORTS=('low', 'medium', 'high', 'max')  DEEPSEEK_V4_OVERRIDES={'xhigh': 'max'}
  reasoning_config={'effort': 'high'} -> ({'thinking': {'type': 'enabled'}}, {'reasoning_effort': 'high'})
  reasoning_config={'effort': 'xhigh'} -> ({'thinking': {'type': 'enabled'}}, {'reasoning_effort': 'max'})
  reasoning_config={'effort': 'none'} -> ({'thinking': {'type': 'enabled'}}, {})
  reasoning_config={} -> ({'thinking': {'type': 'enabled'}}, {})
  reasoning_config={'enabled': False} -> ({'thinking': {'type': 'disabled'}}, {})
```

Decisão registrada em `COMMANDCODE_REASONING_POLICIES` (`effort.py`): os dois componentes pertencem ao profile; o plugin omite o seu próprio campo e remove um `reasoning_effort` top-level preexistente para não sobrepor a derivação do profile. A incerteza declarada é estritamente sobre o **endpoint CommandCode graduar o nível escolhido pelo Jev** — o profile prova só que o Hermes envia o campo. Nenhum probe pago foi executado.

> **Superado pelo §9 (`t_1906910b`)**: o probe direto mostrou que o endpoint aceita `reasoning_effort`, então o plugin passou a ser o autor do campo **top-level** com o nível do Jev, mantendo o `thinking` como propriedade do profile. O rastreio acima continua correto quanto à ordem do transporte (profile → middleware) e é o que sustenta a precedência documentada no §9.2.

Regressão offline: `test_commandcode_keeps_the_profile_native_controls_and_drops_a_pre_existing_effort` (`tests/test_router.py`) parte de um payload no formato real do profile (`extra_body.thinking` + `reasoning_effort`) e comprova a remoção deliberada, `extra_body.thinking` e `reasoning_config` intactos, `effort_requested='high'` com `effort=None`, snapshots byte-idênticos e replay estável sem segunda chamada ao decider.

### 7.6 Evidência de execução offline
Suíte canônica, no worktree `.worktrees/t_14209379`, sem grade artificial (configuração efetiva):
```text
$ uv sync --offline --extra dev      # pytest 9.1.1 + deps do cache local
$ uv run --offline --extra dev pytest
collected 272 items
...
======================= 264 passed, 8 skipped in 0.74s ========================
```
Mesma suíte com `socket.socket.connect`, `socket.socket.connect_ex` e `socket.create_connection` bloqueados (interpreter da venv + patch, sem rede):
```text
======================= 264 passed, 8 skipped in 0.71s ========================
```
Os 8 skips são os mesmos de sempre: 5 do live probe (exigem `JEV_LIVE_PROBE=1` e chave real) e 3 de `test_registration.py` condicionados a PyYAML. Nenhum skip decorre de socket bloqueado.

Testes novos/ajustados que sustentam os critérios:
- `test_commandcode_grid_is_the_single_namespaced_catalog_under_the_console_decider`, `test_commandcode_grid_cap_applies_to_operator_grids_without_widening_them`, `test_commandcode_grid_untouched_for_legacy_deciders` (`tests/test_config_policy.py`).
- `test_is_provider_routable_normalises_every_route_alias` — cobre a assimetria `opencode-go`/`opencode_go` e os aliases de `commandcode`.
- `test_commandcode_catalog_is_capped_only_for_the_console_decider` e o Go atualizado para um modelo (`tests/test_catalog.py`).
- `test_commandcode_explicit_reasoning_policy` (`tests/test_effort.py`) — agora exige os **dois** componentes do contrato no texto da política (`extra_body.thinking` + `reasoning_effort`), a remoção deliberada e a incerteza do endpoint, e rejeita a formulação antiga (campo ausente do contrato).
- `test_jev_console_decider_generation_route_commandcode_uses_the_namespaced_catalog` e `test_commandcode_route_cap_leaves_one_model_and_no_question` (`tests/test_router.py`): grade efetiva de um modelo, sem pergunta de modelo no payload, decider no SystemOne (nunca no `base_url` de geração), `reasoning_effort` aplicado com `effort_requested`/`effort` auditados, snapshots serializados de `request`/`original_request` byte-idênticos (sem mutação in-place) e replay no mesmo turno sem segunda chamada; no cap, uma escolha inventada não tem canal para o wire e o request fica no único modelo do catálogo.
- `test_commandcode_keeps_the_profile_native_controls_and_drops_a_pre_existing_effort` (`tests/test_router.py`) — regressão da rodada de revisão: parte do formato real do profile (`extra_body.thinking` + `reasoning_effort` top-level), comprova a remoção deliberada do campo preexistente, `extra_body.thinking`/`reasoning_config` intactos, snapshots byte-idênticos e replay estável.
- Go atualizado para o catálogo de um modelo: `test_jev_console_decider_generation_route_go_uses_the_single_model_catalog` e `test_opencode_go_route_cap_leaves_one_model_and_no_question`.

Fail-open continua byte-idêntico também fora do pytest: `scripts/verify_fail_open.py` executado com os mesmos sockets bloqueados imprime `ALL FAIL-OPEN CHECKS PASSED!` (3 casos, `res is None`, payload serializado idêntico ao `original_request`).

Nenhuma alteração no core Hermes, nenhum PR upstream, nenhuma inferência paga. A única chamada de rede desta entrega foi o `GET` público e não autenticado de `/models`, usado apenas para confirmar o id.

---

## 8. `reasoning_effort` aplicado no wire do `opencode-go` (`t_a0a4c186`)

Esta seção **supera** a política do Go registrada em 6.1/6.4 e em 7.5: o `opencode-go` deixa de omitir o campo e passa a enviar o nível decidido pelo Jev. Console continua omitindo; `commandcode` também omitia nesta rodada e passa a enviar no §9 (`t_1906910b`), na sua própria evidência.

### 8.1 Probe que sustenta a mudança (2026-10-20, reexecutado nesta rodada)

`POST https://opencode.ai/zen/go/v1/chat/completions` (header `x-opencode-session` obrigatório), modelo `deepseek-v4.1-flash`. Duas execuções nesta rodada: `scripts/probe_opencode_levels.py` (só status) e uma variante que também mede `usage.completion_tokens_details.reasoning_tokens` com `max_tokens=1024`.

| Campo enviado | Resultado |
|---|---|
| nenhum | HTTP 200 |
| `reasoning_effort=low` | HTTP 200 |
| `reasoning_effort=medium` | HTTP 200 |
| `reasoning_effort=high` | HTTP 200 |
| `reasoning_effort=xhigh` | HTTP 200 |
| `reasoning_effort=max` | HTTP 200 |
| `reasoning_effort=invalid_level` | HTTP 200 |
| `reasoning={"effort":"high"}` | HTTP 200 |
| `thinking=high` | HTTP 400 (`thinking must be an object ...`) |

As respostas aceitas trouxeram `reasoning_content` e `usage.completion_tokens_details.reasoning_tokens` (25–61 tokens medidos), ou seja, o modelo emite reasoning nesse endpoint. Duas observações delimitam o que o probe prova:

- **o endpoint não valida o valor:** `invalid_level` também retornou HTTP 200. O §5.3, em outra execução, registrou 422 para esse mesmo caso, mas com a mensagem `Upstream request failed: Endpoint is unavailable` — erro de upstream, não de validação. Logo o conjunto aceito é uma decisão nossa (a do core), não uma fronteira de 400.
- **200 não gradua nada:** com uma amostra por nível, `reasoning_tokens` não escalou de forma monotônica (low 61, medium 36, high 25, max 50, xhigh 60, sem campo 60).

### 8.2 Política embutida (`effort.py`)

`OPENCODE_GO_REASONING_POLICIES["deepseek-v4.1-flash"]`: `wire_parameter="reasoning_effort"`, `accepted_levels=("low","medium","high","max")`, `overrides={"xhigh":"max"}`, `omitted=False`, com `evidence` citando endpoint, data, os HTTP 200 de `low`/`medium`/`high`/`xhigh`/`max`, os campos de reasoning da resposta, o HTTP 400 de `thinking` e a referência ao contrato do core. A `decision` registra que o nível declarado vai no wire, que `xhigh` é mapeado para `max` pela mesma override declarada do core, que nada fora do conjunto declarado é enviado, e que o 200 prova aceitação do parâmetro — não profundidade calibrada.

O vocabulário não foi escolhido por probe isolado: é o contrato que o próprio core aplica nesse mesmo wire. `core/agent/reasoning_effort.py` declara `DEEPSEEK_V4_EFFORTS = ("low","medium","high","max")` e `DEEPSEEK_V4_OVERRIDES = {"xhigh": "max"}`; `core/plugins/model-providers/opencode-zen/__init__.py` reconhece `deepseek-v4.1-flash` como modelo de thinking (`_is_deepseek_thinking_model`: `deepseek-v*` que não seja `deepseek-v3*`) e chama `thinking_toggle_extras(reasoning_config, DEEPSEEK_V4_EFFORTS, DEEPSEEK_V4_OVERRIDES)`, que emite o `reasoning_effort` **top-level** com o nível clampado a esse vocabulário. O plugin passa a declarar exatamente o mesmo conjunto, em vez de uma versão mais estreita.

### 8.3 Regra do resolvedor (`resolve_effort`)

Ordem efetiva: (1) `effort_maps` do operador tem precedência e mantém o clamp existente para o nível mais fraco; (2) rotas de política embutida (`opencode-console`/`opencode_console`, `opencode-go`/`opencode_go`, `commandcode`/`commandcode-chat`/`commandcode_chat`) enviam o nível **somente** se ele for membro de `accepted_levels`, ou se uma `overrides` declarada na própria política o mapear para um nível membro — qualquer outro valor é omitido; (3) `ollama-cloud` segue intocado no `family_for` + `clamp` (Kimi `medium`→`high` continua valendo). O ramo (2) existe porque o fallback de família responderia um nível que a rota nunca declarou (`minimal` desce para `low`, `ultra` sobe para `max`), e a override só vale quando o alvo é um nível declarado: `{"xhigh": "max"}` vale, uma override para `ultra` seria ignorada.

### 8.4 Verificação offline do resolvedor e da config do perfil

```
$ uv run --offline --with pyyaml python <scratch>/t_a0a4c186_verify_r2.py
== built-in policy matrix (worktree effort.py) ==
opencode-go       deepseek-v4.1-flash   wire='reasoning_effort' accepted=('low', 'medium', 'high', 'max') omitted=False overrides={'xhigh': 'max'}
                  resolve -> {'low': 'low', 'medium': 'medium', 'high': 'high', 'max': 'max', 'xhigh': 'max', 'minimal': None, 'none': None, 'bogus': None, 'ultra': None}
opencode_go       deepseek-v4.1-flash   wire='reasoning_effort' accepted=('low', 'medium', 'high', 'max') omitted=False overrides={'xhigh': 'max'}
                  resolve -> {'low': 'low', 'medium': 'medium', 'high': 'high', 'max': 'max', 'xhigh': 'max', 'minimal': None, 'none': None, 'bogus': None, 'ultra': None}
opencode-console  deepseek-v4.1-flash   wire=None               accepted=() omitted=True overrides={}
                  resolve -> todos None
commandcode       deepseek/deepseek-v4.1-flash wire=None         accepted=() omitted=True overrides={}
                  resolve -> todos None
opencode-go       kimi-k3               wire=None               accepted=() omitted=True overrides={}
                  resolve -> todos None

== live profile config: effort_maps is parsed and honoured ==
raw effort_maps from config.yaml: {'opencode-go': {'deepseek-v4.1-flash': {'accepted': ['low', 'medium', 'high', 'max']}}}
  config path resolve_effort('low') -> 'low'
  config path resolve_effort('medium') -> 'medium'
  config path resolve_effort('high') -> 'high'
  config path resolve_effort('max') -> 'max'
  config path resolve_effort('xhigh') -> 'high'
  config path resolve_effort('minimal') -> None
  config path resolve_effort('none') -> None
  config path resolve_effort('bogus') -> None
  config path resolve_effort('ultra') -> 'max'
```

`effort_maps.opencode-go.deepseek-v4.1-flash.accepted` em `~/.hermes/profiles/code/config.yaml` passou de `[low, medium, high]` para `[low, medium, high, max]` (uma linha; permissões 600 preservadas; backup em `<scratch>/settings-backup-r2.txt`). A entrada tem duas funções: faz o plugin **hoje instalado** — que só conhece o caminho de `effort_maps` — continuar aplicando o nível, e mantém o perfil alinhado ao conjunto declarado. Depois de merge + reinstall ela é redundante para `low`/`medium`/`high`/`max`, que a política embutida já envia verbatim.

Divergências conhecidas entre os dois caminhos, ambas **inalcançáveis** pelo decider atual (`EFFORT_LEVELS = ("low","medium","high")` em `config.py` valida criteria, `default_effort` e `max_effort`), portanto sem efeito em turno real:

- `xhigh`: a política embutida mapeia para `max` (override declarada, igual ao core); o caminho `effort_maps` mantém a regra anti-escalada do caminho custom e desce para `high`.
- `ultra`: a política embutida omite; o caminho `effort_maps` desce para `max` (mesma regra: nunca sobe, mas o nível mais forte aceito é `max`).

### 8.5 Testes

Novos/ajustados:

- `test_opencode_go_explicit_reasoning_policies` (`tests/test_effort.py`) — política `omitted=False`, `wire_parameter='reasoning_effort'`, `accepted_levels=('low','medium','high','max')`, `overrides={'xhigh':'max'}`, marcadores do probe e da referência ao core na `evidence`, `low`/`medium`/`high`/`max` resolvidos verbatim nos dois aliases (`opencode-go`/`opencode_go`), `xhigh` → `max`, `minimal`/`none`/`bogus`/`ultra`/vazio → `None`, e modelo fora do catálogo Go continua omitido.
- `test_declared_override_cannot_invent_a_level_outside_the_policy` — a override só vale quando o alvo é um nível declarado: com `accepted=('low','medium')` e `overrides={'xhigh':'ultra','high':'medium'}`, `xhigh` → `None` (nada inventado) e `high` → `medium`.
- `test_operator_effort_map_takes_precedence_over_the_go_builtin_policy` — map do operador vence a política embutida e mantém o clamp (`high` → `low` com `accepted: [low]`).
- `test_unmapped_provider_defaults_to_omitting_effort` — substitui o teste antigo que afirmava omissão no Go; usa `openrouter` como provider sem política.
- `test_jev_console_decider_generation_route_go_uses_the_single_model_catalog` (`tests/test_router.py`) — exige `request["reasoning_effort"] == "high"`, `record["effort"] == "high"`, `effort_requested == "high"` e `fallback_reasons == []`, preservando uma única chamada ao SystemOne, ausência de mutação in-place e replay sem segunda chamada.
- `test_opencode_go_route_lands_every_decidable_level_on_the_wire` — parametrizado `low`/`medium`/`high` (os níveis que o decider pode produzir) com `decider: jev_console` + `routable_providers: [opencode-go]`: o campo vai no request e o audit registra o mesmo nível (nunca `null`).

Não regredidos: `test_opencode_console_*` seguem exigindo omissão explícita (o `commandcode` deixou de omitir no §9, e o teste dele foi reescrito lá), e `test_opencode_go_effort_cannot_be_inferred_from_other_providers` continua comprovando que o Go não herda a família Ollama para um modelo fora do catálogo.

### 8.6 Suíte

```text
$ cd .worktrees/t_a0a4c186
$ uv sync --offline --extra dev
$ uv run --offline --extra dev pytest -q
273 passed, 8 skipped in 0.85s

$ uv run --offline --extra dev pytest -q tests/test_effort.py tests/test_router.py -k "opencode_go or go_route or route_go or operator_effort_map" -v
9 passed, 104 deselected in 0.05s
```

Os 8 skips são os mesmos de sempre (5 do live probe sem `JEV_LIVE_PROBE=1` e 3 de `test_registration.py` condicionados a PyYAML).

### 8.7 Limitações e pendências

- **HTTP 200 não gradua nada:** nesta rodada o endpoint respondeu 200 para todos os níveis testados, inclusive `invalid_level` — o §5.3, em outra execução, registrou 422 para esse mesmo caso com a mensagem de upstream indisponível, não de validação. A medição de `reasoning_tokens` (uma amostra por nível) também não escalou de forma monotônica. Logo o 200 prova que o campo é aceito, não que o nível mude a profundidade do raciocínio; por isso o audit continua afirmando apenas "o campo foi enviado".
- **`thinking` não é usado:** o Go exige objeto (`{type: enabled|disabled}`) e rejeita escalar; o parâmetro aplicado é o top-level `reasoning_effort`.
- **`max` e `xhigh` são contrato, não caminho de produção:** o decider só oferece `low`/`medium`/`high` (`EFFORT_LEVELS`), então `max` e o mapeamento `xhigh`→`max` só se aplicam a chamadas diretas à API do plugin ou a um decider futuro. Estão cobertos por teste, não por turno real.
- **Verificação end-to-end pós-merge** (plugin reinstalado + turno real com `"effort": "<nível>"` no audit e `reasoning_effort` no request) fica para o dono, porque o merge é dele: nenhum turno real com o plugin reinstalado foi executado nesta entrega. As chamadas de rede desta rodada foram apenas os probes de aceitação do endpoint (§8.1).

---

## 9. `reasoning_effort` aplicado no wire do `commandcode` (`t_1906910b`)

Esta seção **supera** a política do `commandcode` registrada em §7.5/§7.5.1 (e a nota do §8 que dizia que a rota continuava omitindo): a rota passa a ser **roteada** e o nível decidido pelo Jev vai no `reasoning_effort` **top-level**, ao lado do `extra_body.thinking` que o profile do provider deriva. O `opencode-console` continua omitindo; o `opencode-go` não muda.

### 9.1 Probe que sustenta a mudança (2026-10-20)

`POST https://api.commandcode.ai/provider/v1/chat/completions`, modelo `deepseek/deepseek-v4.1-flash`:

| Campo enviado | Resultado |
|---|---|
| `reasoning_effort=low` | HTTP 200 |
| `reasoning_effort=medium` | HTTP 200 |
| `reasoning_effort=high` | HTTP 200 |
| `reasoning_effort=xhigh` | HTTP 200 |
| `reasoning_effort=max` | HTTP 200 |

O que o probe prova é **aceitação**, não calibração: `xhigh` também retorna 200 (o vocabulário é uma decisão nossa, a do core) e nenhuma medição de profundidade foi feita. É o mesmo limite registrado para o Go no §8.1/§8.7.

### 9.2 Quem escreve o quê no wire (a ordem do transporte)

A rota tem **dois** componentes de reasoning e eles passaram a ter **dois** autores:

1. `extra_body.thinking` — **profile do provider**. `core/plugins/model-providers/commandcode/__init__.py:42-59` delega ids `deepseek/*` ao profile `deepseek`; `deepseek/__init__.py:28-43` chama `thinking_toggle_extras(reasoning_config, DEEPSEEK_V4_EFFORTS, DEEPSEEK_V4_OVERRIDES, always_emit_toggle=True)` (`core/agent/reasoning_effort.py:197-219`), que emite o toggle a partir do `reasoning_config` da sessão.
2. `reasoning_effort` **top-level** — **plugin**, com o nível decidido pelo Jev.

A precedência vem da ordem real de montagem, rastreada em `core/agent/turn_api_request.py::build_api_request`:

- `api_kwargs = agent._build_api_kwargs(api_messages)` (linha 122/124) → o profile já colocou `extra_body.thinking` e o **seu próprio** `reasoning_effort` (via `chat_completions.py:562-568`) no payload;
- `apply_llm_request_middleware(...)` (linhas 143-151) roda **depois** e `api_kwargs = _llm_request_mw.payload` — o payload devolvido pelo middleware é o que vai para a chamada.

O plugin monta o payload roteado a partir do `original_request` (pré-middleware, que já contém `extra_body.thinking`) e reescreve apenas `model` e `EFFORT_WIRE_KEY` (`router.py::_apply`). Consequência exata:

- o `reasoning_effort` do plugin **sobrescreve** o que o profile derivaria do `reasoning_config`;
- o `extra_body.thinking` **não é tocado** (`extra_body` é preservado inteiro);
- o wire fica `thinking: {"type": "enabled"}` **+** `reasoning_effort: <nível do Jev>`.

Não foi preciso alterar `_apply` nem o core: o transport já aplica o campo do plugin **depois** do profile. Isso está fixado por teste (§9.6, `test_commandcode_lands_the_jev_level_beside_the_profile_thinking_toggle`), que parte de um payload no formato real do profile (`thinking` enabled + `reasoning_effort` derivado de `reasoning_config: {effort: low}`) e comprova o nível do Jev (`high`) no wire com o toggle e o `reasoning_config` intactos.

### 9.3 Política embutida (`effort.py`)

`COMMANDCODE_REASONING_POLICIES["deepseek/deepseek-v4.1-flash"]`: `wire_parameter="reasoning_effort"`, `accepted_levels=("low","medium","high","max")`, `overrides={"xhigh":"max"}`, `omitted=False`, com `evidence` citando o endpoint, a data, os cinco HTTP 200, a ordem `profile → middleware` e o contrato do core (`DEEPSEEK_V4_EFFORTS`/`DEEPSEEK_V4_OVERRIDES`). A `decision` registra que `low`/`medium`/`high`/`max` vão verbatim, `xhigh`→`max` pela override declarada, que nada fora do conjunto é enviado (nem clamp, nem invenção), que `extra_body.thinking`/`reasoning_config` ficam como o profile os montou, e que o 200 prova aceitação — não profundidade.

### 9.4 Auditoria: `provider` no registro de rota

O registro `event: route` não nomeava a rota (só o `skip` nomeava). Com duas rotas servindo `deepseek*/deepseek-v4.1-flash` e ambas enviando `reasoning_effort`, o registro ficou ambíguo. `router.py::_record_route` passou a gravar `"provider": where.provider`, a mesma chave que o `skip` já usava. Consumidores usam `record.get(...)`, então nenhum leitor quebrou.

### 9.5 Config do perfil `code` (aplicada nesta rodada)

`/home/rafael/hermes/home/profiles/code/config.yaml` (backup em `<scratch>/live-settings-r2.tgz`, permissões `600` preservadas):

```yaml
        routable_providers:
          - opencode-go
          - commandcode
        grids:
          opencode-go:
            - 'deepseek-v4.1-flash: general work, explanation, ordinary coding and tool use'
          commandcode:
            - 'deepseek/deepseek-v4.1-flash: general work, explanation, ordinary coding and tool use'
        effort_maps:
          opencode-go:
            deepseek-v4.1-flash:
              accepted: [low, medium, high, max]
```

**`commandcode` não tem entrada em `effort_maps`** — isso é a correção da revisão, não um esquecimento. `effort.py::resolve_effort` resolve um provider que aparece em `effort_maps` pelo caminho custom (`_resolve_custom_provider_effort`), que **precede** a política embutida (`effort.py:486-498`) e aplica a regra anti-escalada do clamp (`effort.py:125-181`). Com a entrada `accepted: [low, medium, high, max]` que a rodada anterior deixou na config, o contrato efetivo da rota era outro: `xhigh` descia para `high` (na escada `LADDER`, `max` é "mais forte" que `xhigh`, então a override declarada era rejeitada como escalada) e `ultra` descia para `max` em vez de ser omitido. A política embutida é a única autoridade do vocabulário da rota; a config só declara que a rota existe e qual modelo ela serve. O mapa do `opencode-go` fica intocado: é o vocabulário declarado daquela rota (§8), com a semântica de clamp documentada em §8.4 — esta correção não altera override de operador de nenhuma outra rota.

Leitura da config real pelo `load_settings` do worktree (`<scratch>/verify_settings.py`; imprime só chaves de roteamento/effort):

```text
$ cd .worktrees/t_1906910b && uv run --offline --with pyyaml python <scratch>/verify_settings.py
routable_providers: ('opencode-go', 'commandcode')
commandcode routable: True
commandcode grid: ['deepseek/deepseek-v4.1-flash']
effort_maps providers: ['opencode-go']
raw effort_maps: {'opencode-go': {'deepseek-v4.1-flash': {'accepted': ['low', 'medium', 'high', 'max']}}}

== policy actually in force for commandcode (with the live effort_maps) ==
  wire='reasoning_effort' accepted=('low', 'medium', 'high', 'max') overrides={'xhigh': 'max'} omitted=False
  commandcode/'low'    -> 'low'
  commandcode/'medium' -> 'medium'
  commandcode/'high'   -> 'high'
  commandcode/'max'    -> 'max'
  commandcode/'xhigh'  -> 'max'
  commandcode/'ultra'  -> None
  commandcode/'minimal' -> None
  commandcode/'none'   -> None
  commandcode/'bogus'  -> None
  commandcode/''       -> None

== sem regressão nas outras rotas ==
  opencode-go/'low'    -> 'low'
  opencode-go/'medium' -> 'medium'
  opencode-go/'high'   -> 'high'
  opencode-go/'max'    -> 'max'
  opencode-go/'xhigh'  -> 'high'
  opencode-go/'ultra'  -> 'max'
  opencode-console/'low'    -> None
  opencode-console/'high'   -> None
```

Ou seja: com a config **e** o código desta branch, `resolve_effort("deepseek/deepseek-v4.1-flash", nível, provider="commandcode")` devolve `low`/`medium`/`high`/`max` verbatim, `xhigh`→`max`, e omite `ultra`/`minimal`/`none`/string desconhecida/vazio. Rollback: restaurar `<scratch>/live-settings-r2.tgz` sobre o arquivo (as permissões `600` viajam no tar).

### 9.6 Testes

Novos/reescritos:

- `test_commandcode_explicit_reasoning_policy` (`tests/test_effort.py`) — agora exige `wire_parameter='reasoning_effort'`, `accepted_levels=('low','medium','high','max')`, `overrides={'xhigh':'max'}`, `omitted=False`, marcadores do probe e da ordem do transporte (`turn_api_request`, `extra_body.thinking`) na `evidence`, `low`/`medium`/`high`/`max` verbatim nos três nomes de provider, `xhigh`→`max`, `minimal`/`none`/`bogus`/`ultra`/vazio → `None`, e modelo fora do catálogo continua omitido.
- `test_jev_console_decider_generation_route_commandcode_uses_the_namespaced_catalog` (`tests/test_router.py`) — passa a exigir `request["reasoning_effort"] == "high"` e `record["effort"] == "high"` (antes `None`), mantendo grade de um modelo, pergunta de um critério, decider no SystemOne, ausência de mutação in-place e replay sem segunda chamada.
- `test_commandcode_lands_the_jev_level_beside_the_profile_thinking_toggle` — a regressão do §9.2: payload no formato real do profile, nível do Jev vencendo o derivado (`low`→`high`), `extra_body.thinking` e `reasoning_config` intactos, snapshots byte-idênticos, replay estável.
- `test_commandcode_route_lands_every_decidable_level_on_the_wire` — parametrizado `low`/`medium`/`high`: o campo vai no request e o audit registra `event=route`, `provider=commandcode`, `decider=jev_console`, `effort` igual ao escolhido.
- `test_the_delivered_code_profile_config_holds_the_commandcode_contract` (`tests/test_effort.py`) — a regressão que a rodada 1 da revisão exigiu: monta o bloco de settings que o perfil `code` roda (`routable_providers` + `grids`, `effort_maps` só com `opencode-go`), passa pelo `load_settings` e resolve **através desses `effort_maps`**. Exige `low`/`medium`/`high`/`max` verbatim, `xhigh`→`max`, `minimal`/`none`/`bogus`/`ultra`/vazio → omitido, `commandcode` ausente do mapa (nenhum caminho custom sombreando a política), a grade da rota com o id namespaced, e — na mesma config — `opencode-go` preservando a semântica do mapa de operador (`max` verbatim, `xhigh`→`high`). Non-vacuidade verificada: reintroduzindo a entrada `commandcode` no dicionário de settings, o teste falha em `assert "commandcode" not in maps`.
- `test_the_live_code_profile_config_holds_the_same_commandcode_contract` (`tests/test_effort.py`) — mesma verificação lida do arquivo real do perfil, via `JEV_PROFILE_CONFIG=/caminho/config.yaml` (skip quando não apontado, para a suíte continuar hermética). Executado nesta rodada com `uv run --offline --extra dev --with pyyaml pytest tests/test_effort.py -k "delivered_code_profile or live_code_profile"` → `2 passed`.

### 9.7 Suíte

```text
$ cd .worktrees/t_1906910b
$ uv sync --offline --extra dev
$ uv run --offline --extra dev pytest -q
277 passed, 9 skipped in 0.87s
```

Os 9 skips são os 8 de sempre (5 do live probe sem `JEV_LIVE_PROBE=1` e 3 de `test_registration.py` condicionados a PyYAML) mais o novo check opt-in do config vivo (`JEV_PROFILE_CONFIG`). A linha de base desta branch era `273 passed, 8 skipped`; o delta são os três casos parametrizados da entrega original mais a regressão de config (+1 passed, +1 skip).

### 9.8 Verificação end-to-end com o plugin reinstalado

O plugin da instalação viva (`/home/rafael/hermes/home/profiles/code/plugins/jev-effort-router`, pin `375d180`) foi apontado para o commit desta branch sem rede (`git fetch <worktree> wt/t_1906910b`, `git checkout -B check-t_1906910b FETCH_HEAD`) e o turno real do card foi executado:

```text
$ hermes -p code --provider commandcode -m deepseek/deepseek-v4.1-flash chat -q "Reply with only: ok"
ok
Session:        20261020_100410_2be7c9
Duration:       7s
```

Registro de audit correspondente (campos integrais, mensagem livre de conteúdo):

```json
{"ts": "2026-10-20T13:04:14+00:00", "event": "route", "model": "deepseek/deepseek-v4.1-flash",
 "effort": "low", "effort_requested": "low", "model_choice": "1", "provider": "commandcode",
 "effort_choice": "low", "model_confidence": 1.0, "effort_confidence": 1.0,
 "model_probabilities": {"1": 1.0}, "effort_probabilities": {"low": 1.0, "medium": 0.0, "high": 0.0},
 "latency_ms": 1213, "fallback_reasons": [], "jev_model": "jev-1.13-free",
 "decider_fallback_reason": null, "decider": "jev_console", "replayed": false,
 "turn_id": "20261020_100410_2be7c9:20261020_100410_2be7c9:d4df5a48",
 "platform": "cli", "api_call_count": 1}
```

`event: route` (não `skip`), `provider: commandcode`, `decider: jev_console`, `effort: "low"` não-nulo — os quatro critérios do card no mesmo registro. O `skip`/`provider_not_routed` que motivou o card está no mesmo arquivo (61 ocorrências, a última em 2026-10-20T12:50:32Z, `platform: cli`).

Segundo turno com `HERMES_DUMP_REQUESTS=1` para capturar o corpo realmente enviado (o dump mascara o header de autorização e o script abaixo imprime só as chaves de reasoning):

```text
$ HERMES_DUMP_REQUESTS=1 hermes -p code --provider commandcode -m deepseek/deepseek-v4.1-flash chat -q "Reply with only: ok"
ok
$ uv run --offline python <scratch>/t_1906910b_dump_fields.py <session>/request_dump_20261020_100451_fa17c1_...json
dump: request_dump_20261020_100451_fa17c1_20261020_100454_703584.json
url: https://api.commandcode.ai/provider/v1/chat/completions
model: deepseek/deepseek-v4.1-flash
reasoning_effort: low
extra_body: {"thinking": {"type": "enabled"}}
reasoning_config present: False
top-level keys: ['extra_body', 'model', 'reasoning_effort']
```

Esse é o critério "`thinking` + `reasoning_effort` coexistem no wire sem 400" verificado no corpo real, não no duble de teste: o endpoint respondeu 200 com os dois campos presentes, e o nível no wire é o do Jev (`low`) — a sessão do perfil `code` tem `agent.reasoning_effort: xhigh`, que o profile traduziria para um nível mais alto, então o valor observado comprova a precedência do §9.2 em produção. `reasoning_config` não vaza para o wire.

Depois da verificação o clone instalado voltou ao pin (`git checkout main`, HEAD `375d180`, árvore limpa), porque o merge ainda é do dono.

#### 9.8.1 Janela pré-merge (config corrigida, plugin ainda no pin)

Com a entrada `effort_maps.commandcode` removida (§9.5), o plugin **instalado** (`main`, `375d180`) ainda aplica a política antiga da rota, que a omite (`omitted=True`); e `router.py::_apply` remove o `reasoning_effort` do payload quando a decisão não traz nível. Medido, não inferido — turno real com a config nova e o plugin velho:

```text
$ hermes -p code --provider commandcode -m deepseek/deepseek-v4.1-flash chat -q "Reply with only: ok"
ok  (session 20261020_101510_4401ee)
```

```json
{"ts": "2026-10-20T13:15:13+00:00", "event": "route", "model": "deepseek/deepseek-v4.1-flash",
 "effort": null, "effort_requested": "low", "decider": "jev_console", "replayed": false,
 "turn_id": "20261020_101510_4401ee:20261020_101510_4401ee:5466ae08",
 "platform": "cli", "api_call_count": 1}
```

Corpo enviado (`HERMES_DUMP_REQUESTS=1`, session `20261020_101541_42895e`):

```text
url: https://api.commandcode.ai/provider/v1/chat/completions
model: deepseek/deepseek-v4.1-flash
reasoning_effort: <absent>
extra_body: {"thinking": {"type": "enabled"}}
reasoning_config present: False
top-level keys: ['extra_body', 'model']
```

O turno continua válido (HTTP 200, `thinking` presente), mas sem nível no wire: é o custo explícito de retirar o mapa que sombreava o contrato, e ele só existe enquanto o merge não acontece. A janela fecha com o merge + `hermes plugin update`; o rollback (§9.5) restaura o tar e, com ele, o nível `low` pelo caminho custom.

#### 9.8.2 Com o código da branch instalado e a config corrigida

O mesmo clone foi apontado para o commit da branch (`git fetch <worktree> wt/t_1906910b`, `git checkout -B check-t_1906910b FETCH_HEAD`, head `34132ad`) e o turno repetido:

```text
$ HERMES_DUMP_REQUESTS=1 hermes -p code --provider commandcode -m deepseek/deepseek-v4.1-flash chat -q "Reply with only: ok"
ok  (session 20261020_101657_2a77f0)
```

Audit (registro integral, conteúdo de mensagem ausente por padrão):

```json
{"ts": "2026-10-20T13:17:01+00:00", "event": "route", "model": "deepseek/deepseek-v4.1-flash",
 "effort": "low", "effort_requested": "low", "model_choice": "1", "provider": "commandcode",
 "effort_choice": "low", "model_confidence": 1.0, "effort_confidence": 1.0,
 "model_probabilities": {"1": 1.0}, "effort_probabilities": {"low": 1.0, "medium": 0.0, "high": 0.0},
 "alternatives": null, "latency_ms": 666, "fallback_reasons": [], "jev_model": "jev-1.13-free",
 "decider_fallback_reason": null, "decider": "jev_console", "replayed": false,
 "turn_id": "20261020_101657_2a77f0:20261020_101657_2a77f0:a3a333d6",
 "session_id": "20261020_101657_2a77f0",
 "api_request_id": "20261020_101657_2a77f0:20261020_101657_2a77f0:a3a333d6:api:1",
 "platform": "cli", "api_call_count": 1}
```

Corpo de fato enviado:

```text
url: https://api.commandcode.ai/provider/v1/chat/completions
model: deepseek/deepseek-v4.1-flash
reasoning_effort: low
extra_body: {"thinking": {"type": "enabled"}}
reasoning_config present: False
top-level keys: ['extra_body', 'model', 'reasoning_effort']
```

Com a config corrigida o nível vem da **política embutida** — não existe mais mapa desta rota no caminho custom —, continua sendo o nível do Jev (`low`, e não o derivado da sessão, que tem `agent.reasoning_effort: xhigh`) e coexiste com o `thinking` do profile. O clone voltou ao pin (`git checkout main`, `375d180`, árvore limpa; a branch temporária saiu com `git update-ref -d refs/heads/check-t_1906910b`).

### 9.9 Limitações e pendências

- **HTTP 200 não gradua nada:** o probe prova que o campo é aceito, não que o nível mude a profundidade do raciocínio. O audit segue afirmando apenas "o campo foi enviado".
- **`thinking` explícito desabilitado pelo usuário não é reinterpretado:** quando o `reasoning_config` da sessão tem `enabled: false`, o profile emite `thinking: {"type": "disabled"}` e nenhum `reasoning_effort`; o plugin ainda aplica o nível do Jev nesse caso, exatamente como já fazia no `opencode-go` desde o §8 (o plugin é dono do nível, o profile é dono do toggle). Nenhum turno real dessa forma foi executado; o comportamento é o mesmo das duas rotas, não uma divergência nova.
- **`max` e `xhigh` são contrato, não caminho de produção:** o decider só oferece `low`/`medium`/`high`.
- **A rota `commandcode` não tem — e não deve ganhar — entrada em `effort_maps`:** qualquer entrada para o provider faz `resolve_effort` resolver pelo caminho custom, cujo clamp anti-escalada responde `xhigh` com `high` e `ultra` com `max`. O vocabulário da rota é a política embutida; a config só declara `routable_providers` e `grids`. A regressão `test_the_delivered_code_profile_config_holds_the_commandcode_contract` falha se a entrada voltar.
- **A validação estritamente pós-merge/pós-update continua pendente do dono:** o merge e o `hermes plugin update` são dele. O §9.8.2 foi medido com o clone instalado apontado para a branch; repetir o turno depois do update é a única etapa que falta, e até lá o estado vivo é o do §9.8.1 (rota válida, sem nível no wire).
- **Sessões de cron com `provider: commandcode` deixam de ser puladas:** as 61 ocorrências de `provider_not_routed` no audit do perfil `code` incluem turnos de `platform: cron`, que passam a ser roteados por esta config. É o efeito pedido pelo card (a rota precisava estar em `routable_providers`), registrado aqui porque o alcance não é só o CLI.

## 10. Rota `openai-codex` (Codex OAuth / Responses API) (`t_9087219e`)

A rota `openai-codex` não é um chat-completions: o profile fala `api_mode="codex_responses"`
(`core/plugins/model-providers/openai-codex/__init__.py`), e as duas metades do router assumiam o
formato antigo — o portão de `api_mode` pulava a rota, o estado era lido de `messages` (o payload
Responses carrega `input`) e o effort era escrito como `reasoning_effort` top-level, que **não é
campo da Responses API**. Por isso a rota antiga só aparecia como `provider_not_routed` no audit
(3 registros antes da instalação desta branch, no perfil `job-search`).

### 10.1 O contrato do wire (capturado, não inferido)

O transporte consome o `reasoning_config` params-level e escreve o objeto `reasoning` **enquanto
monta o corpo** (`core/agent/transports/codex.py::_reasoning_fields`, chamado de `build_kwargs`), e o
middleware `llm_request` roda **depois** dessa montagem (`core/agent/turn_api_request.py::build_api_request`).
Logo o payload que o plugin vê já é o corpo da Responses API. Dump real (`HERMES_DUMP_REQUESTS=1`,
`--provider openai-codex -m gpt-6-luna`, 2026-10-20) — os campos abaixo são os kwargs da chamada
`responses.create(...)`, que o SDK serializa no corpo com os mesmos nomes:

```text
url: https://chatgpt.com/backend-api/codex/responses   (POST)
model: gpt-6-luna
reasoning: {"effort": "high", "summary": "auto"}
include: ["reasoning.encrypted_content"]
store: false
reasoning_effort (top-level): ausente
```

Consequência de projeto: o plugin reescreve **`reasoning.effort`** dentro de um objeto `reasoning` que
já existe. `summary` e `include` continuam do transporte, e uma rota que ele julgou sem campo de
reasoning (objeto ausente) não ganha um — inventar o objeto é exatamente a classe de 400 que aquele
guard evita. Nesse caso o audit registra `effort: null` e mantém `effort_requested`, porque um `effort`
nulo é uma afirmação sobre o wire, nunca sobre o modelo.

### 10.2 Vocabulário: duas famílias, dois conjuntos

Declarados pelo core (`core/agent/reasoning_effort.py`), não pelo plugin:

| Modelo | Conjunto aceito | Constante do core |
|---|---|---|
| `gpt-6-luna`, `gpt-6-luna-900k` | `none, low, medium, high, xhigh, max` | `CODEX_GPT56_EFFORTS` |
| `gpt-6.1-sol` | `low, medium, high, xhigh, max` (**sem `none`**) | `CODEX_ASTRA_EFFORTS` via `NO_DISABLE_TIER_PREFIXES` |

Um pedido de `none` na família Sol é **omitido**, nunca enviado: aquela rota 400 num disable, e o campo
omitido reabilita o effort default do modelo em vez de desligá-lo. `minimal`, `ultra` e string
desconhecida também são omitidos (sem clamp), como nas demais rotas opt-in.

### 10.3 `-900k` é alias de picker, não id do backend

`strip_codex_context_variant_suffix` (`core/agent/model_metadata.py`) remove o sufixo antes do wire, e a
janela grande é resolvida de `agent.model` na construção da sessão — **um roteamento no middleware não
consegue concedê-la**. Dois turnos reais, `-m gpt-6-luna` e `-m gpt-6-luna-900k`, produzem corpos
idênticos no que importa (`model: gpt-6-luna`, mesmo `reasoning`); só mudam `session_id`,
`x-client-request-id`/`prompt_cache_key` e a linha `Model:` do system prompt.

Por isso `Entry` ganhou `wire_id`: o catálogo registra o id que o endpoint realmente serve e o router
escreve **esse**. A primeira versão lia o `wire_id` da entrada da grade efetiva — e `grids.<provider>`
vindo da config carrega só descrição, então o alias ia inteiro para o wire. O turno real pegou o erro:
audit `event: route, model: gpt-6-luna-900k, effort: low` e, um segundo depois,
`⚠️ Model fallback: gpt-6-luna-900k via openai-codex unavailable (model entitlement)`. `wire_model_for()`
resolve agora contra `route_catalog(provider)` (o único lugar que sabe o que cada id significa no wire),
com fallback para a entrada resolvida em providers sem catálogo embutido.

Desde a rodada de revisão, todo registro `event: route` carrega também `wire_model` (o id enviado) ao
lado de `model` (o id escolhido): um registro de `gpt-6-luna-900k` mostra
`model: gpt-6-luna-900k, wire_model: gpt-6-luna` e não pode ser lido como rota com janela de 900k.

### 10.4 `gpt-6.1-sol-900k` não existe neste backend

O card lista `gpt-6.1-sol-900k` como disponível no `provider_models_cache.json` de `openai-codex`. Não
está: o cache do perfil lista `gpt-6.1-sol` **sem** variante `-900k` (`gpt-6-astra-900k`, `gpt-6-sol-900k`,
`gpt-6-luna-900k` e os `gpt-5.6-*` estão lá; `gpt-6.1-sol-900k` não), e o próprio core não o considera
elegível para o sufixo (`_CODEX_900K_ELIGIBLE_BASES` não inclui `gpt-6.1-sol`).

Verificado ao vivo, duas vezes (com e sem o plugin instalado):

```text
⚠️ Model fallback: gpt-6.1-sol-900k via openai-codex unavailable (model entitlement);
   using gpt-6-luna via openai-codex.
```

Antes da remoção, um turno roteado para esse id produzia o pior resultado possível para a auditoria:
registro `event: route, model: gpt-6.1-sol-900k, effort: low` seguido de `skip model_not_in_grid` do turno
de fallback — o audit afirmava uma rota que não aconteceu. Na rodada de revisão (P1) o id foi **removido**
de `CODEX_GRID`/`CODEX_ALLOWLIST`: um catálogo não deve oferecer modelo que o endpoint rejeita. O
código passa a dar três garantias: (1) a grade da rota tem três ids — `gpt-6-luna`, `gpt-6-luna-900k`,
`gpt-6.1-sol`; (2) `ModelCatalog.is_known` responde `False` para o id morto, então ele não é "sem
evidência"; (3) uma grade do operador que o nomeie é capada e cai de volta no catálogo da rota, e uma
decisão que o nomeie mesmo assim é recusada com `skip model_not_in_provider_catalog`, com a request
preservada. Antes da remoção, as grades entregues nos perfis `code` e `job-search` já **não** o
listavam (`catalog.py`). A pendência que resta — o contrato de quatro modelos do card — está no §10.8.

### 10.5 Config dos perfis `code` e `job-search`

```yaml
        routable_providers:
          - opencode-go
          - commandcode
          - openai-codex
        grids:
          openai-codex:
            - 'gpt-6-luna: general work, coding, explanation and tool use'
            - 'gpt-6-luna-900k: general work and coding over a 900k context window (large repos, long documents)'
            - 'gpt-6.1-sol: harder reasoning, architecture, planning and long-context analysis'
```

`model.provider` de cada perfil ficou como estava: a troca de provider é por sessão/perfil, e
`routable_providers` só habilita a rota. Nenhum `effort_maps` para `openai-codex` — a política embutida é
a autoridade da rota (uma entrada no mapa resolveria pelo caminho custom, cujo clamp anti-escalada
responde `xhigh` com `high`).

### 10.6 Testes

- `tests/test_router.py`: rota não pulada e nível em `reasoning.effort` (com `reasoning_effort` ausente,
  `include`/`store`/`instructions` intactos); cada nível decidível × luna/sol; alias de picker não chega
  ao wire (inclusive vindo de grade do operador — a regressão do bug do §10.3); conversa lida de `input`
  (itens sem role, como `function_call`, ficam fora do estado); payload sem objeto `reasoning` mantém
  `effort: null` no audit; replay do corpo sem segunda decisão; alias de provider (`codex`).
- `tests/test_effort.py`: as duas famílias com seus conjuntos, `none` omitido em Sol, `minimal`/`ultra`/
  string desconhecida omitidos, modelo fora do catálogo omitido; e a config entregue (com um companion
  opt-in lendo o arquivo real via `JEV_PROFILE_CONFIG`).
- `tests/test_config_policy.py`: catálogo de três ids sob `jev_console` (e pelos dois aliases), cap
  sobre grade do operador, `wire_id` do alias, grade intacta em decider legado, e normalização de alias
  nos dois sentidos.
- `tests/test_router.py::test_codex_route_never_offers_an_id_the_backend_does_not_serve`: `Router`,
  `Settings` e `ModelCatalog` reais (sem rede) — o catálogo tem três ids, `is_known` responde `False`
  para `gpt-6.1-sol-900k`, uma grade do operador que o nomeie é capada (e cai de volta no catálogo da
  rota), e uma decisão que o nomeie deixa a request intacta, com `skip model_not_in_provider_catalog`.
- `tests/test_router.py::test_codex_picker_alias_never_reaches_the_wire`: o audit distingue `model`
  (`gpt-6-luna-900k`, o id escolhido) de `wire_model` (`gpt-6-luna`, o id enviado).
- Regressão de forma: `chat_completions` continua escrevendo só o `reasoning_effort` top-level.

Suíte completa: `pytest -q tests` → 306 coletados, 297 passam, 9 pulados (os pulos são os probes
opt-in), 0 falhas.

### 10.7 Verificação end-to-end (branch instalada no perfil `job-search`)

Clone do perfil apontado para a branch (`4d0296e`, depois `40fa1fb`), `HERMES_DUMP_REQUESTS=1`, e a grade
restrita a um modelo por rodada para forçar cada id (a grade natural só é decidida pelo Jev):

| Modelo forçado | Audit | Corpo enviado | HTTP |
|---|---|---|---|
| `gpt-6-luna` (grade natural 3 modelos) | `route, model=gpt-6-luna, effort=low, effort_requested=low, model_confidence=1.0` | `model=gpt-6-luna, reasoning.effort=low` | 200 |
| `gpt-6-luna-900k` | `route, model=gpt-6-luna-900k, effort=low` | `model=gpt-6-luna, reasoning.effort=low` | 200 |
| `gpt-6.1-sol` | `route, model=gpt-6.1-sol, effort=low` | `model=gpt-6.1-sol, reasoning.effort=low` | 200 |
| `gpt-6.1-sol-900k` — **histórico**, rodada anterior à remoção do catálogo; não é mais opção da rota | `route, model=gpt-6.1-sol-900k, effort=low` + `skip model_not_in_grid` | fallback: `model=gpt-6-luna, reasoning.effort=high` | **400** (model entitlement) |

A última linha é histórica: foi medida **antes** de o id ser removido de `CODEX_GRID`; `gpt-6.1-sol-900k`
não é mais opção da rota (ver §10.4).

Turno natural com a grade de três modelos e loop de ferramentas (`gpt-6.1-sol`, 41 s, 4 chamadas):

```json
{"event":"route","model":"gpt-6.1-sol","effort":"high","effort_requested":"high",
 "model_confidence":0.91,"effort_confidence":0.67,"provider":"openai-codex",
 "replayed":false,"api_call_count":1}
{"event":"route","model":"gpt-6.1-sol","effort":"high","replayed":true,"api_call_count":2}
{"event":"route","model":"gpt-6.1-sol","effort":"high","replayed":true,"api_call_count":3}
```

Corpo correspondente: `model=gpt-6.1-sol, reasoning.effort=high`. O replay do turno reescreve o mesmo
corpo sem nova decisão, e o nível do Jev (`high`) é o que sai — não o default da sessão.

Estado vivo devolvido ao pin: o clone do perfil voltou para `9a4b919` (código revisado) e a config ficou
com a grade de três modelos. `hermes plugin update` pós-merge continua sendo etapa do dono.

### 10.8 Limitações e pendências

- **Os `-900k` como opção de grade não mudam o wire.** Escolher `gpt-6-luna-900k` em vez de `gpt-6-luna`
  produz o mesmo corpo; a diferença de janela é decidida em `agent.model` na construção da sessão. O
  catálogo hoje carrega só `gpt-6-luna-900k` como rótulo (o `gpt-6.1-sol-900k` saiu por não ser
  servido — §10.4), e o contrato de quatro modelos do card não pode ser cumprido: um id não é servido e
  o sufixo não concede nada a partir do middleware. Pendente de decisão do dono.
- **`none` na família Sol é contrato do core, não probe desta rodada.** O card pede que `none` nunca seja
  enviado; o teste prova que o plugin o omite. O 400 em si vem da declaração do core (`NO_DISABLE_TIER_PREFIXES`,
  live 2026-09-29) — não houve POST com `reasoning.effort: "none"` em `gpt-6.1-sol` nesta rodada.
- **HTTP 200 não gradua nada:** os turnos provam que o campo é aceito e que o nível decidido é o que sai,
  não que a profundidade de raciocínio mude.
- **`max`/`xhigh` são contrato, não caminho de produção:** o decider só oferece `low`/`medium`/`high`.
- **Turnos de cron com `provider: openai-codex` deixam de ser pulados** com esta config, exatamente como
  no §9.9: o alcance não é só o CLI.

## 11. Rota `anthropic` (Messages API) — 3 turnos reais e reprodução do corpo (`t_173d3150`)

Rodado em 2026-11-10 entre 19:25 e 20:02 (-03) no `rafael-notebook`, pelo card de operação
`t_21bd35bd` (board `homelab`), a pedido do card de código `t_173d3150`. **Nenhum restart** de
`hermes-gateway.service`, `hermes-dashboard` ou `hermes-relay`: `MainPID=4008646` e
`ActiveEnterTimestamp=2026-11-09 12:32:38 -03` iguais antes e depois, e o plugin entrou no processo
vivo por `reload-plugins` (`{"reloaded": true, "adapters_rewired": 2}`, com `jev-effort-router` na
lista e `hooks: [on_session_end, post_llm_call]`).

Estado vivo neste teste: clone do perfil `code` em `ffafb23` (detached, tree limpo, `plugin.yaml`
0.5.0), `config.yaml` com exatamente +5 linhas — `anthropic` em `routable_providers` e as 4 linhas de
`grids.anthropic` (Haiku/Sonnet/Opus); `decider`, `model.default`, `effort_maps` e `max_effort`
intocados. Backup e evidências brutas em
`/home/rafael/hermes/backups/pre-anthropic-gate-20261110-1925/`.

### 11.1 Os três turnos roteados

Cada rodada usou `grids.anthropic` com **uma** entrada e um `reload-plugins` entre elas, terminando
com a grade de três entradas restaurada e novo reload (também `reloaded: true`). O decider escolheu
`effort=low` nas três (o nível é dele, não do teste).

| Modelo | ts (UTC) | Audit (`event=route`) | API call real | HTTP |
|---|---|---|---|---|
| `claude-haiku-5-5` | 22:55:23 | `provider=anthropic, wire_model=claude-haiku-5-5, effort=low, effort_requested=low` | `API call #1: model=claude-haiku-5-5 in=36946 out=4` | 200 |
| `claude-sonnet-5-5` | 22:57:10 | `provider=anthropic, wire_model=claude-sonnet-5-5, effort=low, effort_requested=low` | `API call #1: model=claude-sonnet-5-5 in=36782 out=4` | 200 |
| `claude-opus-5-5` | 22:57:55 | `provider=anthropic, wire_model=claude-opus-5-5, effort=low, effort_requested=low` | `API call #1: model=claude-opus-5-5 in=36780 out=4` | 200 |

Nenhum registro `skip` nessas rodadas; `effort` não nulo em todas; `Turn ended:
reason=text_response(finish_reason=stop)` em cada turno, resposta `ok` impressa, e nenhum HTTP
4xx/5xx no `logs/errors.log` do perfil na janela (só os WARNING pré-existentes de
`provides_middleware` do manifest). Registros brutos: `evidence/audit-3-routes.jsonl`,
`evidence/api-calls-live.txt`, `evidence/round{1,2,3}-*-cli.txt`.

Prova de que o código novo estava vivo e não o pin antigo: `jev_effort_router_status` devolve
`routable_providers` com `anthropic` e `grids.anthropic` com as três entradas — nada disso existe
em `6c71ea2`, só em `ffafb23` (`evidence/jev-status-cli.txt`).

### 11.2 O corpo: **reprodução, não captura ao vivo**

`HERMES_DUMP_REQUESTS=1` **não produziu dump** neste host, e o motivo é estrutural: o turno de
`hermes chat -q` não roda no processo do cliente — a chamada ao provider acontece no **managed
worker**. Passar a variável só ao CLI não chega ao worker, e adicioná-la ao `.env` do perfil e abrir
novo turno também não gerou `request_dump_*` (`find` em `/home/rafael` por 10 min: zero; a linha foi
removida do `.env` ao final). Patches in-process no cliente (SDK Anthropic, `httpx`,
`apply_llm_request_middleware`, `turn_api_request.env_var_enabled`) nunca disparam — ver
`evidence/capture-{haiku,http-haiku,mw-haiku}.txt` e `evidence/diag-dump.txt`.

O que se entrega no lugar é o corpo que o core monta para os mesmos modelo e nível, via
`agent.anthropic_adapter.build_anthropic_kwargs` (`evidence/body-repro-<modelo>.json`):

```json
{
  "model": "claude-haiku-5-5",
  "messages": [{"role": "user", "content": "Reply with only: ok"}],
  "max_tokens": 32000,
  "thinking": {"type": "adaptive", "display": "summarized"},
  "output_config": {"effort": "low"}
}
```

Idêntico em forma para `claude-sonnet-5-5` e `claude-opus-5-5`: `thinking` do transporte,
`output_config.effort` com o nível decidido, **sem** `reasoning_effort` top-level e **sem**
`reasoning_config`. O plugin reescreve apenas o valor de `output_config.effort` em um
`output_config` que o transporte já criou (`Router._apply_anthropic_effort`); não inventa o
container nem a chave. Isso fecha a lacuna entre a reprodução e o que foi enviado nos turnos acima —
mas **não é uma captura ao vivo** e não deve ser descrito como tal.

### 11.3 Limitações e pendências

- **Sem dump ao vivo nesta rota:** o contrato é declarado pelo core (`_thinking_kwargs` +
  `ADAPTIVE_EFFORT_MAP`) e reproduzido aqui; um ambiente onde `HERMES_DUMP_REQUESTS` alcance o
  worker ainda é o teste definitivo.
- **HTTP 200 não gradua nada:** os turnos provam que o endpoint aceita o corpo e que o nível
  decidido foi o registrado no audit, não que a profundidade mude.
- **Caps são decisão do plugin, não limite do wire:** Opus/Sonnet até `high`, Haiku até `max`; um
  nível fora do cap é omitido, nunca clampado, e `effort_maps` do operador é o caminho suportado
  para alargar uma família.
- **O deploy final é do card de operação:** o clone instalado no perfil `code` está em `ffafb23`;
  se o branch mudar, `hermes plugin update`/install + `reload-plugins` precisam rodar de novo.
  Rollback em `evidence/RESUMO.md`.


## 12. Rota `claude-subscription-directsdk-experimental` — corpo reproduzido no parser do provider (`t_c5151092`) — **HISTÓRICO**

> **Histórico (não descreve o código atual).** A rota, o catálogo `SUBSCRIPTION_GRID`, as políticas
> `SUBSCRIPTION_*`, o estilo de wire `extra_body_reasoning` e a variante de id `[1m]` foram **removidos
> na 0.8.0** (card `t_1f6f98d1`): o plugin do provider saiu do host em 2026-11-15 e nenhum dos 9 perfis
> o listava em `routable_providers`. A seção fica como registro da evidência da época; os símbolos que
> ela cita não existem mais.

Esta rota é **outro provider**, não a rota `anthropic` nativa da §11: Claude Pro/Max através do
Claude Code CLI (`api_mode=chat_completions`), com o nível em **`extra_body.reasoning.effort`**.
O card entrega só o código da rota — a configuração de planner/reviewer fica para o card de operação
seguinte —, então aqui **não há turno roteado nem status HTTP**: o que se registra é a reprodução do
corpo que chega ao parser do provider, derivada de três fontes verificadas no código.

### 12.1 As três fontes do contrato

1. `claude-subscription-directsdk-experimental/__init__.py::build_api_kwargs_extras` devolve
   `{'reasoning': dict(reasoning_config)}` (plugin do provider 0.3.4), e o core coloca esse dicionário
   em `api_kwargs['extra_body']['reasoning']`
   (`core/agent/transports/chat_completions.py`, merge de `extra_body`).
2. O plugin escreve **apenas** `extra_body.reasoning.effort` dentro de um objeto `reasoning` que já
   existe (`Router._apply_extra_body_reasoning`, escolhido por declaração:
   `config.ROUTE_WIRE_STYLES` → `WIRE_STYLE_EXTRA_BODY_REASONING`). `reasoning.enabled` e as demais
   chaves de `extra_body` são preservados; nem `extra_body` nem `reasoning` são criados; um
   `reasoning` desabilitado (`enabled: false`) é deixado como o transporte o montou.
3. `directsdk.py::request_body` (provider 0.3.4) valida os kwargs contra uma allowlist que **não**
   contém `reasoning_effort`, faz `body.pop('reasoning')`, recusa qualquer chave além de
   `enabled`/`effort`, limita o esforço ao vocabulário `(none, low, medium, high, xhigh, max)`,
   escreve `thinking: {type: adaptive, display: summarized}` (famílias adaptativas com
   `enabled: true`) e `output_config: {effort: …}`, e passa o mesmo valor ao CLI como `--effort`
   (`directsdk.py`, montagem do comando; `parsed['output_config']['effort']`).

### 12.2 Reprodução: as três famílias no parser real

Execução local (2026-11-10), sem rede e sem spawn do CLI: `Router._apply` com
`provider=claude-subscription-directsdk-experimental`, `api_mode=chat_completions`, sobre um corpo em
que o transporte já pôs `extra_body.reasoning = {"enabled": true, "effort": "medium"}`, e então
`directsdk.request_body(routed)` — o parser do próprio provider, importado do seu diretório de
instalação.

| Modelo | Nível decidido | `extra_body.reasoning` após o plugin | `thinking` (nativo) | `output_config` (nativo) |
|---|---|---|---|---|
| `claude-opus-5-5` | `high` | `{"enabled": true, "effort": "high"}` | `{"type": "adaptive", "display": "summarized"}` | `{"effort": "high"}` |
| `claude-sonnet-5-5` | `medium` | `{"enabled": true, "effort": "medium"}` | `{"type": "adaptive", "display": "summarized"}` | `{"effort": "medium"}` |
| `claude-haiku-5-5` | `max` | `{"enabled": true, "effort": "max"}` | `{"type": "adaptive", "display": "summarized"}` | `{"effort": "max"}` |

Em nenhum dos três o corpo roteado carrega `reasoning_effort` top-level, e as chaves do corpo nativo
ficam `['max_tokens', 'output_config', 'thinking', 'tools']` — nenhuma chave inventada pelo plugin.

O mesmo parser, com `reasoning_effort` top-level no corpo, responde
`ValueError: Unsupported request parameters: reasoning_effort`. É a razão de o plugin nunca escrever
esse campo nesta rota (o `wire_parameter` da policy serve de metadado, não de seletor de transporte).

### 12.3 `reasoning.enabled = false`: o plugin não escolhe o nível

Corpo com `extra_body.reasoning = {"enabled": false, "effort": "medium"}` e decisão `high`: o plugin
deixa o objeto **intacto** (`{"enabled": false, "effort": "medium"}`) e o audit registra
`effort: null` / `effort_requested: high`. No parser do provider esse corpo vira
`output_config = {"effort": "low"}` e **sem** bloco `thinking`, porque as três famílias servidas aqui
estão em `MANDATORY_THINKING` (`model_catalog.py`) — o desligamento é impossível e o provider cai no
próprio caminho de menor esforço. Ou seja: nesse caso o `effort: null` do audit significa "o plugin
não escreveu nível nenhum", e o `low` que vai ao CLI é decisão do provider, não uma escolha roteada.

### 12.4 Limitações e pendências

- **Reprodução, não captura ao vivo.** O corpo foi derivado de `build_api_kwargs_extras` + a
  reescrita do plugin + `directsdk.request_body`; nenhum `request_dump_*` foi produzido (o mesmo
  limite estrutural da §11: `HERMES_DUMP_REQUESTS=1` não alcança o managed worker).
- **Sem turno roteado nesta rodada.** Não há status HTTP a registrar: a rota é entregue desligada
  (`routable_providers` não a lista por padrão) e a configuração de planner/reviewer é do card de
  operação.
- **Caps são decisão do plugin, não limite do wire:** Opus/Sonnet até `high`, Haiku até `max`; nível
  fora do cap é omitido, nunca clampado, e `effort_maps` do operador é o caminho suportado para
  alargar uma família.
- **`max` no Haiku é o único caso oferecido:** a pergunta de esforço é feita uma vez, antes do
  modelo, e `offered_levels_for_grid` intersecta as famílias — na grade de três modelos o `max` não
  é perguntado.
