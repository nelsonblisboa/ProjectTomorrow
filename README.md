# MAG Tracker

Sistema web de gestão inteligente com IA para acompanhamento de benefícios e regulação de sinistros da [MAG Seguros](https://mag.com.br/acompanhar-beneficio/).

## Visão Geral
- **Coleta automatizada (RPA):** scraper via Playwright a cada 30 minutos, com retries e normalização de dados.
- **Persistência:** tabelas `processos` e `historico_processos` (SQLite no MVP → PostgreSQL), com detecção de mudanças por hash.
- **Interface web:** FastAPI + frontend com dashboard de pendências, filtros, timeline e alertas visuais.
- **LGPD:** criptografia em repouso de CPF/nome e logs de acesso.
- **IA (fase 2):** análise automática de pendências e priorização de processos.

📄 Documentação completa: [ARQUITETURA.md](ARQUITETURA.md)

## Status
- ✅ Fase 1 — Arquitetura definida
- 🚧 Fase 2 — MVP (scraper + banco + API)
- ⏳ Fase 3 — Interface web + autenticação
- ⏳ Fase 4 — Notificações + IA

## Preview local
```bash
bash serve.sh   # inicia http://localhost:8000
```
