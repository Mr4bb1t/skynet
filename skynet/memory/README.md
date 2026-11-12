# Memória temporal (curto + longo prazo)

Princípio da arquitetura:

> **Memória não é percepção. Percepção não é conhecimento. Conhecimento não é estado atual.**

O sistema não guarda tudo no mesmo “saco”: ele separa o que está acontecendo
agora do que aconteceu, do que é conhecimento durável e do que é plano futuro.
O banco continua sendo um único SQLite, com separação lógica por tipo temporal.

## As quatro camadas

| Camada | O que guarda | Onde vive | Como aparece no prompt |
|---|---|---|---|
| **Working Memory** | últimas falas, tarefa ativa, pessoas visíveis, estado da sessão | RAM (`memory/working.py`), TTL 30 min (configurável) | bloco `CURRENT STATE` |
| **Episodic** | acontecimentos passados, sempre com data | SQLite (`memory_type='event'`) | bloco `EPISODIC MEMORIES` |
| **Semantic** | fatos, preferências, relações, rotinas | SQLite (`fact/preference/relationship/routine`) | bloco `SEMANTIC KNOWLEDGE` |
| **Plans / Tasks** | intenções, compromissos e tarefas futuras | SQLite (`plan/task`) | bloco `FUTURE PLANS` |

A presença de uma pessoa no `CURRENT STATE` vem **somente da percepção visual
atual** (foto/rosto reconhecido agora), nunca de uma memória antiga.

## Banco (schema v2)

Além dos campos antigos (`kind`, `subject`, `content`, `tags`, `importance`,
`source`, `metadata`, `created_at`, `updated_at`), cada memória tem:

| Campo | Uso |
|---|---|
| `memory_type` | `fact`, `event`, `plan`, `task`, `preference`, `relationship`, `routine`, `state` |
| `event_time` | quando o acontecimento ocorreu/começou |
| `event_end` | quando terminou (eventos com duração) |
| `valid_from` / `valid_until` | início/fim da validade de um fato/estado |
| `last_confirmed_at` | última confirmação/salvamento |
| `expires_at` | expiração de estados temporários |
| `temporal_scope` | `instant`, `interval`, `timeless`, `unknown` |
| `status` | `active`, `done`, `cancelled`, `superseded`, `expired`, `uncertain` |
| `confidence` | 0 a 1 |
| `verification_status` | `confirmed`, `inferred`, `uncertain` |
| `session_id` | sessão que originou a memória |
| `supersedes_id` | memória antiga que esta substitui |

> **`created_at` ≠ `event_time`.** `created_at` é quando a memória foi salva;
> `event_time` é quando o acontecimento ocorreu. Registros antigos migrados
> ficam com `temporal_scope='unknown'` e `event_time=''` — o sistema **não
> inventa** uma data de acontecimento a partir de `created_at`.

A migração v1 → v2 é automática e não apaga registros. Faça backup de
`data/memory.db` antes de atualizar o box.

## Quatro blocos de contexto

`MemoryManager.build_context()` monta:

```text
=== CURRENT STATE ===
Data/hora atual: 11/11/2026 22:15 -03 (America/Sao_Paulo)
Pessoas visíveis agora: João
Conversa recente (sessão atual): ...
Tarefa ativa: ...

=== EPISODIC MEMORIES ===
- [evento] Em 10/11/2026 (ontem): João: João esteve na sala

=== SEMANTIC KNOWLEDGE ===
- [fato] Vigente desde 02/11/2026: João: trabalha na empresa Y

=== FUTURE PLANS ===
- [plano] Para 18/11/2026 (em 7 dias): casa: Comprar café (não realizado)
```

O system prompt reforça:

> Somente CURRENT STATE representa o estado atual do ambiente e da sessão.
> Memórias episódicas representam acontecimentos passados. Conhecimento
> semântico representa informações armazenadas cuja validade deve ser
> respeitada. Planos representam intenções futuras, não acontecimentos
> realizados. Nunca transforme uma memória histórica em observação presente.

A data/hora atual (fuso `America/Sao_Paulo`) também é injetada no prompt.

## Recuperação temporal

`recall()` interpreta a intenção da consulta:

- **agora / atualmente / neste momento** → estado vigente e fatos/preferências;
  eventos passados e planos futuros não entram como “agora”;
- **hoje / ontem / semana passada / mês passado** → eventos na janela;
- **futuro / amanhã / vou fazer** → planos e tarefas com data futura;
- **o que aconteceu com X?** → episódica + semântica;
- **sem pista temporal** → fatos/preferências vigentes + eventos recentes.

O ranking mistura similaridade semântica, similaridade lexical (FTS5),
importância, recência, vínculo com a entidade e aderência temporal. Memórias
`superseded`/`expired` ficam fora por padrão; `include_history=True` as mostra
para consultas históricas.

Memórias com `verification_status='inferred'` recebem rótulo
`(inferido, confiança 0.xx)` e nunca são apresentadas como fato confirmado.

## Working Memory (`memory/working.py`)

- `add_turn(role, text)` — histórico recente da sessão.
- `set_state(key, value, ttl_seconds=...)` — estado atual/tarefa ativa.
- `set_people(names)` / `current_people()` — presença com TTL curto (120 s).
- `snapshot()` — visão serializável para o `CURRENT STATE`.
- TTL global (padrão 1800 s) limpa a sessão sozinho.

O agente de voz e o chat alimentam a working memory com cada turno finalizado.

## Supersessão (informação que mudou)

Nunca sobrescrever silenciosamente:

```python
remember("João trabalha na empresa Y", memory_type="fact",
         memory_id=old_id, supersede=True, valid_from="2026-11-02")
```

O registro antigo vira `status='superseded'` com `valid_until` preenchido; o
novo recebe `supersedes_id` e `valid_from`. O histórico continua consultável.

## Consolidação automática (`memory/consolidate.py`)

A cada ~10 turnos (`MEMORY_CONSOLIDATE_EVERY_TURNS`) ou no encerramento, um
modelo econômico propõe memórias. Uma camada determinística valida:

- data (não inventa; inválida vira `unknown`);
- tipo temporal;
- origem e confiança (consolidação é `inferred` por padrão);
- duplicidade (fingerprint + atualização);
- supersessão (só IDs ativos);
- controle de turnos já processados (`memory_meta`) para não duplicar.

## Ferramentas da IA

| Ferramenta | Uso |
|---|---|
| `remember_memory(...)` | guarda/atualiza; aceita `memory_type`, `when`, `event_end`, `valid_from`, `valid_until`, `expires_at`, `confidence`, `verification_status`, `supersede` |
| `recall_memory(query, time_range, include_history)` | busca com janela temporal |
| `forget_memory(...)` | remove somente quando o usuário pedir |

## Configuração

```env
MEMORY_ENABLED=true
MEMORY_DB_PATH=data/memory.db
MEMORY_EMBEDDINGS_ENABLED=true
MEMORY_EMBEDDING_MODEL=gemini-embedding-001
MEMORY_EMBEDDING_DIM=768
MEMORY_RAG_TOP_K=5
MEMORY_RAG_TOKEN_BUDGET=380
MEMORY_PHOTO_TOKEN_BUDGET=160
MEMORY_RAG_MIN_SCORE=0.20
MEMORY_RECENCY_HALF_LIFE_DAYS=45
MEMORY_MAX_CONTENT_CHARS=700
MEMORY_WORKING_TTL_SEC=1800
MEMORY_CONSOLIDATE_EVERY_TURNS=10
MEMORY_CONSOLIDATION_MODEL=gemini-3.1-flash-lite
```

Para apagar tudo, pare os processos e remova `data/memory.db`.

## Testes

```bash
cd /root/skynet
python3 tests/test_memory_rag.py
python3 tests/test_memory_temporal.py
```

Cobrem: pessoa vista ontem não é presente hoje, pessoa visível agora é
presente, fato antigo não é atual, supersessão preserva histórico, plano
futuro não é concluído, memória sem data não ganha data inventada, inferência
não vira fato confirmado e consolidação não duplica registros.
