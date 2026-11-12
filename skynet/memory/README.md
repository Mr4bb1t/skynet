# Memória de longo prazo (RAG local)

Sistema de memória persistente para o chat de texto e para o agente de voz.
A IA decide **o que guardar**, **o que consultar** e **o que usar** na conversa.
Nada é enviado por inteiro ao modelo: a recuperação é híbrida e limitada por
orçamento de tokens.

## O que é guardado

Qualquer memória durável: pessoas, animais, acontecimentos, projetos, ideias,
preferências, fatos, lugares, objetos e tarefas.

Cada memória tem:

| Campo | Uso |
|---|---|
| `kind` | `person`, `animal`, `event`, `project`, `idea`, `preference`, `fact`, `place`, `object`, `task`, `other` |
| `subject` | entidade principal (`João`, `Rex`, `Skynet`). Pode ter várias separadas por vírgula |
| `content` | o fato em uma frase curta |
| `tags` | palavras-chave para busca |
| `importance` | 0 a 1, usada no ranking |
| `memory_id` | usado para atualizar uma memória existente |

## Banco

SQLite em `data/memory.db` (configurável):

- tabela `memories` com conteúdo, importância, uso e embedding;
- tabela `memory_entities` para busca por pessoa/animal/projeto;
- tabela virtual `memories_fts` (FTS5) para busca lexical;
- `memory_meta` para versão do schema.

## RAG híbrido

1. A consulta combina a mensagem atual + um trecho curto da conversa + nomes
   de rostos reconhecidos pelo `face_recognition`.
2. Candidatos vêm de três fontes: FTS5 (lexical), embeddings (semântica) e
   entidades (rosto/pessoa).
3. O ranking mistura semelhança semântica, sobreposição lexical, importância,
   recência e vínculo com a entidade reconhecida.
4. Só as melhores memórias são formatadas, truncadas e limitadas por
   `MEMORY_RAG_TOKEN_BUDGET` (chat) ou `MEMORY_PHOTO_TOKEN_BUDGET` (fotos no
   agente de voz).

Sem consulta e sem entidade, `recall` retorna vazio: a memória nunca é
despejada inteira.

## Embeddings

- Primário: Gemini (`gemini-embedding-001`, configurável). O **texto** da
  memória é enviado ao Gemini para gerar o vetor; imagens e rostos nunca são
  enviados pelo reconhecimento facial.
- Fallback local: hashing de palavras + trigramas, sem rede.
- Cada vetor guarda o provider que o gerou; vetores de providers diferentes
  nunca são comparados.
- Para operar 100% offline, use `MEMORY_EMBEDDINGS_ENABLED=false` (busca
  lexical FTS5 + hashing local).

## Ferramentas da IA

| Ferramenta | Uso |
|---|---|
| `remember_memory(content, kind, subject, tags, importance, memory_id)` | guarda ou atualiza um fato |
| `recall_memory(query, kind, limit)` | busca poucas memórias relevantes |
| `forget_memory(memory_id, query)` | remove, somente quando o usuário pedir |

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
```

Para apagar tudo, pare os processos e remova `data/memory.db`.
