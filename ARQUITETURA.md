# MAG Tracker — Arquitetura Funcional do Sistema de Acompanhamento de Benefícios

**Autor:** Análise Sênior de Sistemas | **Data:** 2026-11-18 | **Status:** Proposta técnica validada com reconhecimento real do portal

---

## 0. Descoberta Crítica (reconhecimento realizado em runtime)

A análise do HTML de `https://mag.com.br/acompanhar-beneficio/` (HTTP 200, 224 KB) revelou a arquitetura real do portal:

- A página é WordPress institucional; **a timeline não está nela**.
- O conteúdo é um **iframe**: `https://widgets.mag.com.br/widget-timeline-beneficio-anonimo/?fluxo=portal&access_token=<JWT>`
- O JWT é **client-side credentials** (OAuth2 client `usr_institucional_beneficio_prd`, scope `beneficios`), emitido por `identidade.mongeraeagon.com.br` (Keycloak/OIDC), expiração ~24h.
- Consequência: **Playwright é obrigatório no MVP** (renderiza o iframe e captura a DOM da timeline). Após engenharia reversa dos XHRs do widget (`apiserver.magpay.com.br`), o scraper pode degradar para chamadas HTTP diretas + renovação de token — mais leve e estável.
- Existe API documentada publicamente em `developers.mag.com.br/api-portal` — avaliar como via oficial de longo prazo.

---

## 1. Visão Geral

```
┌─────────────────────────────────────────────────────────────┐
│                     MAG TRACKER (SaaS interno)              │
│                                                             │
│  ┌───────────┐   30min    ┌──────────────┐    ┌──────────┐  │
│  │ APScheduler│─────────▶ │  Scraper      │──▶ │ PostgreSQL│ │
│  │ (cron 24/7)│          │ Playwright →  │    │  + cache  │  │
│  └───────────┘           │  API direta   │    └────┬─────┘  │
│         │                │ retries+backoff│        │        │
│         │                │ hash mudança  │        ▼        │
│         │                └──────────────┘   ┌──────────┐   │
│         │ notificações                      │ FastAPI  │   │
│         ▼                                   │ + AuthJWT│   │
│  ┌──────────────┐                           │  RBAC    │   │
│  │ E-mail/      │◀── eventos de mudança ◀───┤──────────│   │
│  │ Telegram     │                           └────┬─────┘   │
│  └──────────────┘                                ▼         │
│                                        ┌──────────────┐    │
│                                        │ React/Vite   │    │
│                                        │ Dashboard +  │    │
│                                        │ Timeline     │    │
│                                        └──────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

Módulos: **Coleta (scraper)**, **Persistência**, **API/Web**, **Notificações**, **Segurança/LGPD**.

---

## 2. Módulo de Coleta Automatizada (Scraper)

### 2.1 Agendamento
- **APScheduler** `IntervalTrigger(minutes=30)` dentro do container `worker` (24/7). Alternativa serverless: GitHub Actions cron `*/30 * * * *` (limitação: runners públicos têm fila; não ideal p/ 30 min contínuos).
- Lock distribuído via tabela `jobs_lock` ou Redis para evitar execução dupla ao escalar réplicas.

### 2.2 Estratégia de Navegação (3 fases evolutivas)
| Fase | Técnica | Quando |
|------|---------|--------|
| F1 (MVP) | Playwright headless Chromium: abre página, espera iframe `widget-timeline-beneficio-anonimo`, preenche formulário (nº processo + CPF), extrai timeline da frame | Imediato |
| F2 | Engenharia reversa dos XHRs do widget (interceptar requests via `page.on("response")`), replicar chamadas com `httpx` + token JWT renovado programaticamente (client credentials no OIDC) | Após mapear endpoints |
| F3 | Integração oficial via `developers.mag.com.br/api-portal` (se contrato permitir) | Longo prazo |

### 2.3 Extração e Normalização
Campos capturados: Nº Processo, Benefício Solicitado, Código do Produtor, Cliente, CPF, Data Nascimento, Data Solicitação, Última Atualização, Dias Corridos, Status, Comentários/pendências (lista de eventos da timeline).

Normalização antes da persistência:
- Datas `dd/mm/yyyy` → `date` ISO; CPF → somente dígitos + validação de check-digit; Status → mapa canônico (`Em análise`, `Pendente documentação`, `Aguardando perícia`, `Deferido`, `Indeferido`, `Concluído`); texto → NFKC, strip.

### 2.4 Robustez
- **Retries**: tenacity, backoff exponencial (base 2s, max 5 tentativas, jitter).
- **Seletores resilientes**: preferir `text=` locators e atributos `data-*`; fallback screenshot + OCR (Tesseract/paddleocr) quando a DOM mudar criticamente; alerta "layout mudou" se >2 campos falharem.
- **Anti-bloqueio**: User-Agent realista, rate limit (máx 1 consulta/min/alvo), rotação de proxies residenciais apenas se necessário (risco jurídico primeiro — ver §7).
- **Auditoria**: log estruturado JSON (logger `scraper`) com carimbo, alvo, hash anterior/novo, duracao; screenshots de erro salvos em volume.

---

## 3. Módulo de Persistência

### 3.1 Schema (PostgreSQL; SQLite no MVP local)

```sql
CREATE TABLE usuarios (
  id SERIAL PK, email UNIQUE, senha_hash, nome,
  papel VARCHAR CHECK (papel IN ('admin','consulta')),
  criado_em, ultimo_login, mfa_secret NULL
);

CREATE TABLE processos (                      -- estado atual
  id BIGSERIAL PK,
  numero_processo VARCHAR(30) UNIQUE NOT NULL,
  beneficio_solicitado TEXT, codigo_produtor VARCHAR(20),
  cliente_nome_enc BYTEA, cpf_enc BYTEA,       -- criptografados (pgcrypto/AES-GCM)
  cliente_nome_hash CHAR(64), cpf_hash CHAR(64),-- índice p/ busca sem descriptografia
  data_nascimento DATE, data_solicitacao DATE,
  ultima_atualizacao TIMESTAMP, dias_corridos INT,
  status VARCHAR(40), comentarios TEXT,
  hash_conteudo CHAR(64),                      -- SHA-256 da timeline serializada
  primeira_consulta TIMESTAMP, ultima_coleta TIMESTAMP,
  ativo BOOL DEFAULT true
);

CREATE TABLE historico_processos (             -- append-only (SCD-2)
  id BIGSERIAL PK, processo_id FK,
  snapshot JSONB NOT NULL,                     -- todos os campos coletados
  delta_eventos JSONB,                         --novos itens de timeline
  hash_conteudo CHAR(64), coletado_em TIMESTAMP
);

CREATE TABLE eventos_mudanca (                 -- dispara notificações
  id, processo_id, tipo ('novo_status','nova_pendencia','concluido'),
  de VARCHAR, para VARCHAR, detalhes JSONB, criado_em, notificado BOOL
);

CREATE TABLE logs_acesso (                     -- LGPD art.37
  id, usuario_id, acao, recurso, ip, user_agent, criado_em
);
```

### 3.2 Regras
- Upsert por `numero_processo`; grava em `historico_processos` **somente quando `hash_conteudo` muda** (economia de storage e trilha de auditoria limpa).
- `dias_corridos` recalculado na leitura (`CURRENT_DATE - data_solicitacao`) para nunca ficar obsoleto.
- Retenção: histórico completo; dados criptografados com chave em KMS/variável fora do repo, rotação anual.

---

## 4. Módulo Interface Web

### 4.1 Autenticação
- FastAPI + OAuth2 password flow (JWT access 30min + refresh 7d) **ou** Auth0/Keycloak hosted (Google OAuth) — recomendado segundo pela superfície de ataque reduzida.
- Senhas com argon2id; rate-limit login (slowapi); RBAC: `admin` (usuários + todos processos) vs `consulta` (apenas processos vinculados à sua carteira/mesa).
- Middleware registra toda leitura de dado pessoal em `logs_acesso`.

### 4.2 Endpoints principais
```
POST /auth/login | POST /auth/refresh
GET  /processos?status=&q=&cpf=&dias_min=&pagina=   (filtros combinados)
GET  /processos/{id}                                  (detalhe + timeline)
GET  /processos/{id}/historico                        (evolução)
GET  /dashboard/resumo        → cards agregados
CRUD /usuarios (admin) | GET /config/limite-dias (admin)
WS   /ws/alertas            → push de eventos_mudanca pro frontend
```

### 4.3 Frontend (React + Vite + Tailwind + TanStack Query)
- **Dashboard**: cards (Pendências, Aguardando Documentação, Concluídos 7d, SLA médio), gráfico status×tempo.
- **Tabela**: colunas = planilha anexada; filtros server-side; ordenação; busca full-text (nome/CPF parcial com máscara).
- **Detalhe**: timeline vertical replicando o widget MAG + diff entre snapshots ("o que mudou desde a última coleta").
- **Alertas visuais**: linha vermelha se `status=Pendente && dias_corridos > limite` (configurável, default 30); badge pulsante para mudanças <24h.
- Prototipagem alternativa: Streamlit nas 2 primeiras semanas (mesmo schema, zero retrabalho de dados).

---

## 5. Módulo de Notificações
- Worker consome `eventos_mudanca where notificado=false`: e-mail (SMTP/Resend) e Telegram (Bot API) com deep-link para o detalhe.
- Digest diário opcional 08:00 (pendências vencendo SLA). Template Jinja2; dedupe por janela deslizante 1h.

---

## 6. Infra & Deploy
- **Docker Compose**: `web` (FastAPI + static React), `worker` (APScheduler+Playwright), `db` (Postgres 16 + pgcrypto), `proxy` (Caddy TLS).
- Alvos: Railway/Render (free tier não roda Playwright direito → VPS Hetzner/Contabo ~US$5 ou Fly.io com volume). Backup diário `pg_dump` → S3 compatível.
- CI: GitHub Actions (lint, pytest, build imagem, deploy). Observabilidade: Loki + Grafana dashboards de sucesso de coleta.

## 7. Riscos e Conformidade (avaliar antes do go-live)
1. **Termos de uso**: raspagem de portal segurador com dados de terceiros pode violar ToS da MAG; priorizar API oficial (developers.mag.com.br) e/ou autorização contratual. O token JWT embutido é público/anônimo, mas uso automatizado intensivo pode caracterizar acesso não autorizado.
2. **LGPD**: CPF/nome/data nascimento = dados pessoais sensíveis em contexto de saúde/previdência. Exigir: base legal, minimização (armazenar só o necessário), criptografia em repouso (§3), logs de acesso (§4.1), DPO quando aplicável, política de retenção.
3. **Fragilidade do widget**: token/iframe podem mudar sem aviso → monitorar taxa de falha de parsing e alertar humano.

## 8. Roadmap Incremental
| Semana | Entrega |
|---|---|
| 1 | MVP scraper Playwright + SQLite + script CLI; validação com 3–5 processos reais |
| 2 | Esquema PG, histórico/hash, APScheduler 30min, testes de resiliência |
| 3 | API FastAPI + auth + RBAC; dashboard Streamlit interno |
| 4 | React completo, WebSocket alertas, notificações e-mail/TG |
| 5+ | Fase F2 (API direta), Docker/VPS, hardening LGPD, migração API oficial |

## 9. Referências de código aberto
Scrapling (scraping adaptativo) · SeleniumBase (anti-bot) · crawl4ai (LLM-ready crawling) · ai-goofish-monitor (padrão cron+SQLite) · hwg-analytics-hub (dashboards de sinistros) · claims-api (modelo de domínio de sinistros em NestJS).

---

### Veredito para este sandbox
Python 3.12, Node 20, Playwright 1.44 e FastAPI **já instalados**; RAM 2 GB e disco 9,3 GB livres comportam o MVP inteiro (scraper + API + SQLite + frontend dev) aqui mesmo — com a ressalva de que o ambiente é efêmero: versionar tudo no Git e fazer deploy externo para produção 24/7.
