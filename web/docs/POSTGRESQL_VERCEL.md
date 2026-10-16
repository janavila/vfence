# PostgreSQL persistente no VFence Central

## Arquitetura

Antes, o Central gravava em `web/data/vfence.db`. Isso continua adequado para
desenvolvimento local, mas um arquivo SQLite dentro de uma função serverless não
é armazenamento persistente.

Em produção, a arquitetura preparada é:

```text
Vercel → FastAPI do VFence Central → PostgreSQL externo persistente
Raspberry Pi → SQLite local do Monitor
```

A mudança é exclusiva do `web/`. O SQLite, a outbox e o `CentralClient` do
Raspberry não foram alterados. Os contratos HTTP, E6 e CRC também permanecem
iguais.

## Seleção do banco

- `DATABASE_URL` ausente ou vazia: `DATA_DIR/vfence.db` (SQLite).
- `DATABASE_URL=sqlite:///...`: SQLite indicado explicitamente.
- `DATABASE_URL=postgresql://...` ou `postgres://...`: PostgreSQL.
- outro esquema: a inicialização é recusada sem imprimir a URL ou credenciais.

O PostgreSQL usa `psycopg` 3. Cada operação abre uma conexão, executa uma
transação e sempre fecha a conexão. Para Neon ou Supabase, prefira a URL de
conexão com pool compatível com aplicações serverless e mantenha os parâmetros
TLS fornecidos pelo provedor.

## Configuração local

SQLite, sem serviço externo:

```powershell
cd web
Copy-Item .env.example .env
python -m pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Deixe `DATABASE_URL=` vazia. O banco será criado em `DATA_DIR/vfence.db`.

Para testar o Central localmente contra PostgreSQL, defina uma URL somente na
sessão ou no `.env` não versionado:

```text
DATABASE_URL=postgresql://usuario:senha@host/banco?sslmode=require
```

Na primeira inicialização, `backend/schema_postgresql.sql` é aplicado de forma
idempotente. Não é necessário executar o arquivo à mão.

## Neon ou Supabase

1. Crie um projeto e um banco PostgreSQL no provedor escolhido.
2. Copie a URL recomendada para aplicações serverless; use conexão com pool
   quando o provedor oferecer essa opção.
3. Confirme que TLS/SSL está habilitado conforme a instrução do provedor.
4. Não grave a URL em arquivo versionado.
5. Teste primeiro com um banco separado de homologação.
6. Configure a mesma URL como `DATABASE_URL` no ambiente correto do Vercel.

O VFence não depende de extensão específica de Neon ou Supabase.

## Variáveis no Vercel

Configure no painel do projeto, no mínimo:

- `DATABASE_URL`: URL PostgreSQL completa fornecida pelo provedor;
- `BASE_ID` e `BASE_TOKEN`: identidade e segredo compartilhado com o Raspberry;
- `BASE_LAT` e `BASE_LON`;
- `ADMIN_USER`, `ADMIN_PASSWORD` e `SESSION_SECRET`;
- demais regras já listadas em `.env.example`, se os padrões não forem usados.

`DATA_DIR` não é usado para o banco nem para logs em arquivo quando
`DATABASE_URL` aponta para PostgreSQL. Os logs vão para stdout, coletado pela
plataforma. Nunca coloque `DATABASE_URL`, `BASE_TOKEN` ou senhas no Git.

O repositório usa o entrypoint `[tool.vercel]` em `pyproject.toml`:
`backend.main:app`. Não existe `vercel.json` no estado atual do projeto.

## Testes

Suíte rápida com SQLite:

```powershell
cd web
python -m pytest -q
```

Integração real com PostgreSQL exige um banco descartável e exclusivo de teste.
As tabelas VFence desse banco são esvaziadas pelos testes:

```powershell
$env:TEST_DATABASE_URL="postgresql://usuario:senha@host/vfence_test?sslmode=require"
python -m pytest tests/test_postgresql_integration.py -q
Remove-Item Env:TEST_DATABASE_URL
```

Sem `TEST_DATABASE_URL`, esses testes aparecem como `skipped` e nenhuma conexão
externa é tentada. Eles cobrem schema idempotente, publicação e recuperação da
cerca, pontos, versões, CRC, heartbeat, download E6, batch, `acked_up_to`, token
inválido, rollback, concorrência de versão e persistência entre duas instâncias.

## Validação manual depois do deploy

1. Abra o editor no endereço do Vercel e publique uma cerca.
2. Confirme resposta `201` em `POST /api/fences`.
3. Reabra a aplicação e confirme a mesma versão em `GET /api/fences/active`.
4. No Raspberry, configure `CENTRAL_BASE_URL`, `BASE_ID` e o mesmo `BASE_TOKEN`.
5. O heartbeat `POST /api/edge/heartbeat` deve receber
   `desired_fence_version` com a nova versão.
6. O Raspberry baixa `GET /api/edge/fences/{version}`, valida CRC e salva em seu
   SQLite local.
7. Abra o VFence Monitor no Raspberry e confirme o mesmo polígono no mapa.
8. Confirme que `POST /api/edge/batch` responde `acked_up_to` e que telemetria,
   eventos e entregas aparecem no Central.

## Garantias da implementação

- Criação da cerca e de todos os pontos ocorre na mesma transação.
- O batch envolve o `edge_seq` e o conteúdo na mesma transação; falha inesperada
  desfaz a reivindicação e evita perda silenciosa.
- A versão da cerca no PostgreSQL usa `pg_advisory_xact_lock`, impedindo duas
  instâncias de escolherem o mesmo `MAX(version) + 1`.
- IDs automáticos usam `IDENTITY`; `edge_seq` usa `BIGINT`.
- Datas continuam como texto ISO 8601 UTC e coordenadas continuam E6 para não
  alterar o contrato externo.
