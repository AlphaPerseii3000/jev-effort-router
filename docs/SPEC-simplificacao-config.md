# SPEC — Simplificação do jev-effort-router (config, texto, features ociosas)

Card de origem: `t_06cb3494` (planner). Medido em 2026-11-15 no `rafael-notebook`, commit `eaaeab7`.
Baseline de testes: `.venv/bin/python -m pytest -p no:cacheprovider tests` → **646 passed, 10 skipped**.

## 0. Premissas do card corrigidas pela medição

| Premissa do card | Medição | Consequência |
|---|---|---|
| `console_jev_fallback_model: ''` é inerte | `config.py:1239-1247`: `''` vira "sem fallback"; ausente vira `jev-1.13` (pago). `decider.py:917-920` só tenta o 2º modelo se não vazio | A linha **segura custo**. Fica, ou o default teria de mudar |
| fallback commandcode é ocioso/duplicado | audit dos 9 perfis: **3473** decisões com `decider_endpoint=https://api.commandcode.ai/provider/v1/systemone` (`decider_fallback_reason=upstream_error`) contra 7082 do Console | Feature **em uso**. Fica; só encolhe de 3 linhas para 1 |
| `effort_maps` repete o código | Os valores são iguais a `effort.py` (`OPENCODE_GO_/COMMANDCODE_/CODEX_REASONING_POLICIES`), mas `effort.py:961-963` só oferece `max` quando o **operador** declara | Tirar do config sem mudar o código perde `max` em opencode-go, commandcode e codex. É preciso declarar `offered` no código |
| 1 provider principal → 4 rotas são demais | audit: code/finance/job-search/sysadmin/ventures/default usam os 4; executor usa commandcode+opencode-go; planner/reviewer usam anthropic+codex (troca no picker, delegação, fallback) | `routable_providers` continua com os 4 em todos |
| `session_selection` só o `webui` consome | "webui" é a **superfície**, não um perfil. Já houve turnos `platform=webui` em code, finance, job-search, sysadmin e ventures. A chave entrou nos configs hoje, 10:15–10:31, então ainda não há audit `selection_*` para avaliar | Fica como `mode: apply`; `platforms: [webui]` é igual ao default e sai |
| 9 clones | 8 em `profiles/*/plugins/` + 1 em `~/.hermes/plugins/` (perfil default); os 9 blocos têm md5 `5cd322e7` | igual |

Detalhe que muda o desenho: `ctx.get_config` **não aplica** o `default` do `plugin.yaml`
(`core/hermes_cli/plugins.py:266-276` devolve o default de quem chama). O default efetivo é o que
está em `config.py:load_settings`; o `config_schema` só documenta e alimenta a UI. "Virar default" quer dizer
mudar `config.py`/`effort.py` **e** manter o `plugin.yaml` igual. Corrigir a docstring errada em
`config.py:4-6`.

## 1. Tabela chave a chave (31 do schema + 1 fora dele)

Legenda: **M** = mantém no config do perfil · **D** = sai do config (o default do código cobre) ·
**R** = remove do código (feature sem consumidor) · **=** = já é default, nada muda.

| # | Chave | Nos 9 perfis | Decisão | Justificativa | Consumidor |
|---|---|---|---|---|---|
| 1 | `enabled` | `true` | **D** | igual ao default | `config.py:1250`, `router.py:288` |
| 2 | `decider` | `jev_console` | **M** | default é `jev`. Trocar o default ligaria os caps/allowlists (`config.py:433-458`) e quebraria o contrato legado (`tests/test_legacy_compat.py`) | `config.py:1272`, `router.py:237,675-698` |
| 3 | `console_api_key_env` | `OPENCODE_GO_API_KEY` | **M** | o default `OPENCODE_CONSOLE_API_KEY` é outro; nomeia credencial | `config.py:863,1279` |
| 4 | `console_jev_model` | `jev-1.13-free` | **D** | igual a `DEFAULT_CONSOLE_JEV_MODEL` | `config.py:886,1233-1237` |
| 5 | `console_jev_fallback_model` | `''` | **M** | não é inerte: desliga o `jev-1.13` pago (§0) | `config.py:1239-1247`, `decider.py:917`, `router.py:570,692,812` |
| 6 | `console_jev_commandcode_base_url` | URL systemone | **M** | é o opt-in de privacidade (upstream sem ZDR, `config.py:268-271`) e precisa ser explícito por perfil | `config.py:272,892,1280` |
| 7 | `console_jev_commandcode_api_key_env` | `COMMANDCODE_API_KEY` | **D** (código) | default `COMMANDCODE_API_KEY` vale **só quando** `base_url` está preenchido | `config.py:274,577-620,894` |
| 8 | `console_jev_commandcode_model` | `typesafe/jev` | **D** (código) | default `typesafe/jev` vale **só quando** `base_url` está preenchido | `config.py:273,577-620,893` |
| 9 | `timeout_s` | `3.0` | **M** | default 2.0 também serve os deciders legados; não há medição para mudar o default publicado | `config.py:1258`, `decider.py:294,300,306,494,505` |
| 10 | `routable_providers` | 4 rotas | **M** (1 linha, flow) | é o gate por perfil (restrição do card). Mudar o default `[ollama-cloud]` com o decider `jev` mandaria ids do `DEFAULT_GRID` para anthropic | `config.py:915-930,1222-1230`, `router.py:294` |
| 11 | `grids` | 8 linhas de texto | **D** | com `jev_console`, `grid_for` cai em `route_catalog` (`config.py:838-846`). 6 linhas são iguais a `grid.py:151-202`; as 2 do deepseek diferem, mas grid de um modelo só não manda a pergunta de modelo (`decider.py:1126-1134`) e o texto nunca vai para o wire | `config.py:821-846,1284`, `tools.py:205` |
| 12 | `effort_maps` | 3 rotas, 5 modelos | **D** (código) | declarar `offered=("low","medium","high","max")` nas políticas built-in de opencode-go, commandcode e codex. Hoje `accepted`/`overrides` já são iguais | `effort.py:765-801,950-994`, `decider.py:179` |
| 13 | `session_selection` | `{mode: apply, platforms: [webui]}` | **M** só `mode: apply` | `platforms` é igual ao default (`config.py:229`); o default `mode: off` fica, porque é seguro | `config.py:1073-1101,1288`, `router.py:1041,1079,1149,1191,1311` |
| 13a | `session_selection.provider_map` (default) | — | **R** (C2) | o default `anthropic → claude-subscription-directsdk-experimental` (`config.py:238-240`) aponta para um provider removido do host. Novo default: `{}` (só identidade) | `config.py:238-240,1048-1070`, `router.py:1079` |
| 14 | `confidence_threshold_per_decider` | — | **R** (C3) | 0/9; com um decider por perfil, `confidence_threshold` já resolve | `config.py:779,908-913,1164-1174,1289` |
| 15 | `effort_criteria` | — | **R** (C3) | 0/9; o texto built-in (`state.py`) é a fonte | `config.py:785,1184-1204,1286`, `decider.py:44,186,550,1219` |
| 16 | `effort_instructions` | — | **R** (C3) | 0/9; mesma razão | `config.py:788,1207-1212,1287`, `decider.py:45,187,560`, `state.py:343` |
| 17 | `platform_overrides` | — | **R** (C3) | 0/9 em 26 dias de audit; leva junto `PlatformPolicy`/`max_effort` | `config.py:623-636,816-819,932-971,974-1013,1271`, `router.py:311`, `decider.py:437-445` |
| 18–28 | `jev_model`, `grid`, `decider_base_url`, `decider_model`, `decider_api_key_env`, `confidence_threshold`, `default_model`, `default_effort`, `context_turns`, `route_per_turn`, `console_endpoint` | — | **=** | 0/9. Os da esquerda pertencem aos deciders legados `jev`/`openai_compatible`, e removê-los é outra decisão (§6). Os demais são rede de segurança sem custo de config | `config.py:1251-1276` |
| 29–31 | `audit_enabled`, `log_skips`, `include_user_message_in_audit` | — | **=** | controles de privacidade e diagnóstico; ficam como escape hatch | `config.py:1267-1269`, `router.py:229,1488` |
| — | `endpoint` (fora do schema) | — | **=** | é lido em `config.py:1252` e falta no `plugin.yaml`. C1 acrescenta ao schema (só documentação) | `config.py:743,873,1252` |

## 2. Alvo da config por perfil

**50 linhas / 2254 chars → 9 linhas (~380 chars), −82%.** Total: 451 → 81 linhas. O bloco é idêntico nos 9:

```yaml
    jev-effort-router:
      settings:
        decider: jev_console
        console_api_key_env: OPENCODE_GO_API_KEY
        console_jev_fallback_model: ''
        console_jev_commandcode_base_url: https://api.commandcode.ai/provider/v1/systemone
        timeout_s: 3.0
        routable_providers: [opencode-go, commandcode, openai-codex, anthropic]
        session_selection: {mode: apply}
```

Não vou abaixo de 9. As 7 chaves que restam são escolhas do operador (backend, credencial, custo,
consentimento de privacidade, gate de rotas). Pôr esses valores do host como default publicado
mudaria o plugin para qualquer outra instalação, e para os deciders legados.

## 3. Fonte única das descrições de catálogo

- A fonte única passa a ser o código: `grid.py` (`OPENCODE_GO_GRID`, `COMMANDCODE_GRID`, `CODEX_GRID`, `ANTHROPIC_GRID`).
- A sobrescrita pelo operador já existe e não muda: `grids.<provider>: ["id: descrição"]`. Ela continua limitada ao catálogo da rota (`config.py:842-846`).
- Uso só para experimento local. Não entra em config versionado.
- Nenhum código novo.

## 4. Features removidas e prova de não uso

| Feature | Prova | Card |
|---|---|---|
| Rota `claude-subscription-directsdk-experimental` (`CLAUDE_SUBSCRIPTION_*`, `SUBSCRIPTION_GRID`, `SUBSCRIPTION_MODEL_LEVELS/POLICIES`, `WIRE_STYLE_EXTRA_BODY_REASONING` + `router._apply_extra_body_reasoning`, `ROUTE_WIRE_STYLES`, `ROUTE_DEFAULT_MODELS[sub]`, `WIRE_PARAMETERS[sub]`, alias em `_GRID_ALIASES`) | não está em `routable_providers` em 0/9 configs (md5 único `5cd322e7`). Plugin removido do host em 2026-11-15. Último audit com esse provider: 2026-11-13T02:24, como skip | C2 |
| Sufixo de variante `[1m]` (`CONTEXT_VARIANT_SUFFIXES`, `split_context_variant`, `context_variant_suffixes`) | declarado só para a rota subscription (`config.py:150-152`). A rota nativa usa header beta, não o id | C2 |
| Default `provider_map` anthropic→subscription | o alvo não existe mais | C2 |
| `confidence_threshold_per_decider`, `effort_criteria`, `effort_instructions`, `platform_overrides` | `grep` nos 9 configs = 0. Só aparecem em `config.py`, `decider.py`, `router.py`, `state.py` e `tests/` | C3 |

Comando de prova que o executor repete antes de remover (precisa dar zero linhas nos configs):

```
grep -nE 'claude-subscription|confidence_threshold_per_decider|effort_criteria|effort_instructions|platform_overrides|\[1m\]' \
  /home/rafael/hermes/home/profiles/*/config.yaml /home/rafael/.hermes/config.yaml
```

## 5. Estrutura: 9 clones → 1 checkout compartilhado

**Decisão:** um checkout de release em `/home/rafael/hermes/shared-plugins/jev-effort-router`, fixado
num SHA da `main`. Cada `plugins/jev-effort-router` dos 9 perfis vira um **symlink** para ele.
Não usar o workspace de dev, porque nele há `.venv`, `graphify-out` e working tree.

- **Suporte no core:** symlink é um formato reconhecido (`plugins_cmd_install.py:452-458`, e `plugin_dev.py:433` recomenda "symlink the checkout into plugins/"). Os clones não estão no `.install-metadata.json`, então `hermes plugins update` não os gerencia hoje e não quebra.
- **Isolamento:**
  - Config, audit e memo continuam por perfil (`<HERMES_HOME>/plugin-data/agent-plugin-jev-effort-router-ee542f23`). O sufixo é igual nos 9 paths diferentes, então não depende do path e o symlink não move o audit.
  - O `state.db` lido pelo `session_selection` também continua por perfil.
  - Perde-se só a **versão por perfil**. Hoje os 9 já estão no mesmo SHA, então não há perda real.
- **Canário:** um perfil pode apontar o symlink para um 2º checkout (`…/jev-effort-router@<sha-novo>`) antes dos outros.
- **Atualização:** `git -C <shared> fetch && git checkout <sha>` + reload. Uma operação em vez de 9.

## 6. Fora de escopo e perguntas abertas

1. Remover os deciders legados `jev` (OpenRouter) e `openai_compatible` e o `ollama-cloud` (`grid`, `jev_model`, `decider_*`, `endpoint`, `DEFAULT_GRID`, `Family`/clamp legado). Seria o maior corte de código, mas muda o default do plugin publicado. Precisa de decisão do operador e de um card próprio.
2. O diretório `plugins/claude-subscription-directsdk-experimental` ainda existe em 8 perfis. "Removido" parece ter sido só desabilitar. Fica para o sysadmin confirmar e limpar com gate.
3. `finance/config.yaml:2` tem `model.default: --model deepseek/deepseek-v4.1-flash`, um prefixo `--model` espúrio. Fica para o sysadmin.

## 7. Contrato de equivalência (o check que fica no repo)

Teste novo, `tests/test_config_simplification_equivalence.py`, com duas fixtures em `tests/fixtures/`:
o bloco atual de 50 linhas e o bloco alvo de 9. Para `load_settings(old)` e `load_settings(new)`,
e para cada provider ∈ {opencode-go, commandcode, openai-codex, anthropic}, precisam ser iguais:

- `grid_for(p)` (ids **e** `wire_id`)
- `effective(None, p).default_model`
- `offered_levels_for_grid(grid, p, effort_maps)`
- para cada modelo × nível ∈ LADDER: `resolve_effort(...)` (o caso "omitido" é `None`, nunca clamp)
- `active_commandcode_fallback()` (url, model, key_env, enabled)
- `decider_model_for()`, `base_url_for()`, `api_key_env_for()`, `console_jev_fallback_model`
- `session_selection.mode/platforms`, `is_provider_routable(p)`

A única diferença permitida é o texto `description` das 2 entradas deepseek. Ela não vai para o wire (grid de um modelo).
Asserts fixos dos caps: anthropic Opus/Sonnet oferecem `low,medium,high`; Haiku oferece até `max`; `xhigh` e `max` em Opus resolvem para `None`.

## 8. Migração (C4, sysadmin, Nível 2 com gate)

Regra de ordem: **o código novo aceita a config antiga sem mudar nada** (valor explícito vence o
default). A recíproca é falsa: config nova com código antigo perde `max` e desabilita o fallback
commandcode. Por isso: **subir o código primeiro e cortar a config depois. No rollback, inverter: restaurar a config primeiro e reverter o código depois.**

1. **Snapshot.** Para cada um dos 9: `cp config.yaml config.yaml.bak-jevsimplify-<ts>`. Registrar o SHA do clone. Capturar `jev_effort_router_status` (JSON) em `~/hermes/home/profiles/sysadmin/cache/scratch/jevsimplify/<perfil>-before.json`.
2. **Checkout compartilhado** no SHA aprovado (main com C1–C3). Em cada perfil: `mv plugins/jev-effort-router plugins/jev-effort-router.pre-shared-<ts>`, depois `ln -s <shared> plugins/jev-effort-router`. Nada é apagado.
3. **Reload por perfil**, sempre com a config antiga. Ordem: `executor` (canário: kanban, commandcode e opencode-go) → `planner` (anthropic nativo) → os outros 7. Esperar 1 turno real `event=route` por perfil antes de seguir.
4. **Corte da config**, perfil a perfil na mesma ordem: trocar o bloco pelo de §2, reload, capturar `<perfil>-after.json`.
5. **Validação pós-mudança, por perfil:**
   - O diff de status before/after é vazio em grid ids, offered, fallback e decider.
   - Há 1 turno real com `"event": "route"` no `routes.jsonl`, com o provider principal do perfil.
   - Em planner/reviewer, `provider=anthropic` com `effort` ≤ `high` para Opus.
   - Em 24 h, pelo menos 1 registro com `decider_endpoint` commandcode em algum perfil (prova de que o fallback continua vivo).
   - Nenhum `WARNING jev-effort-router` novo no `agent.log`.
6. **Rollback.**
   - Config: `cp config.yaml.bak-jevsimplify-<ts> config.yaml` e reload.
   - Código: `ln -sfn plugins/jev-effort-router.pre-shared-<ts> plugins/jev-effort-router` (ou `git -C <shared> checkout eaaeab7`) e reload.
   - Teste do rollback: executar no canário `executor` antes do passo 4 dos outros perfis.
   - Limpeza dos `.pre-shared-*`: só depois de 7 dias estável, com gate.

## 9. DAG

```
t_06cb3494 (planner, esta spec)
  └─ t_64c30ced (code, triage)  C1 — config mínima equivalente (offered built-in, defaults do trio, plugin.yaml, teste §7)
       └─ t_1f6f98d1 (code)     C2 — remover rota subscription + [1m] + provider_map default
            └─ t_0888cdb6 (code) C3 — remover confidence_threshold_per_decider, effort_criteria, effort_instructions, platform_overrides
                 └─ t_4caed084 (sysadmin, parents C1+C2+C3) C4 — checkout compartilhado + migração §8 + validação (gate needs_input)
```

A sequência é serial de propósito: `config.py`, `effort.py` e `router.py` são hotspot comum a C1–C3.
Cada card de código: branch `wt/<task_id>`, `pytest tests` verde com a contagem no card, CHANGELOG,
e `kanban_request_review(reviewer="reviewer")` com gate **reinforced** (regressão de roteamento).
Versão: C1 → 0.7.0. C2 e C3 → 0.8.0 (quebra: removem chaves).
