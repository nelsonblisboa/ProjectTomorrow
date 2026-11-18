# MAG Tracker

Sistema web para acompanhamento de benefícios e regulação de sinistros da MAG Seguros.

## Desenvolvimento local (Windows PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MAG_DEMO = "1"
python -m uvicorn app.web:app --host 127.0.0.1 --port 8000
```

Abra http://localhost:8000/login. `MAG_DEMO=1` ativa apenas as contas de acesso de demonstração em banco novo; não gera registros de processos fictícios. Não use essas credenciais em ambientes expostos.

## Remover os registros fictícios existentes

Pare o servidor com Ctrl+C, atualize a branch e execute:

```powershell
git pull origin copilot/security-quality
python -m app.limpar_demo
```

O comando mostra quantos processos têm exatamente o padrão criado pelo gerador demo (`SIM` + seis dígitos), pede confirmação e cria um backup datado do banco antes de excluir esses processos e seus históricos. Contas, logs de auditoria, execuções do scraper e processos com outros números são preservados. Sem o texto de confirmação, nada é apagado.

## Cadastrar dados reais manualmente

Entre como administrador, abra **Processos → Cadastrar processo / testar consulta**, preencha o formulário e salve. Nome e CPF são armazenados criptografados com a chave Fernet; a lista mascara o CPF. Só insira informações cujo tratamento esteja autorizado. Mantenha o servidor local e o banco protegidos.

## Testar uma consulta real do scraper

Consultas reais ficam desativadas por padrão e nunca são feitas automaticamente. Confirme que você está autorizado a consultar o portal e os dados em questão, instale o Chromium (`python -m playwright install chromium`) e reinicie a aplicação com `MAG_DEMO=0`, `MAG_ENABLE_REAL_SCRAPER=1` e os segredos de produção descritos abaixo. O formulário permite testar um processo por vez, com limite de uma consulta a cada 60 segundos por processo. O scraper não contorna login, token, CAPTCHA ou bloqueios; se o portal negar acesso, peça autorização oficial à MAG.

## Testes

```powershell
python -m pytest -q
```

## Configuração fora do modo demo

O modo seguro é o padrão (`MAG_DEMO=0`). Antes de iniciar, configure segredos fora do repositório:

- `MAG_SECRET`: segredo de sessão com pelo menos 32 caracteres aleatórios.
- `MAG_FERNET_KEY`: chave Fernet válida para criptografia em repouso.
- `MAG_BLIND_SALT`: salt aleatório com pelo menos 32 caracteres para índice de busca.
- `MAG_ADMIN_EMAIL` e `MAG_ADMIN_PASSWORD`: usados somente para criar o primeiro administrador quando a base ainda não tem usuários; senha com pelo menos 12 caracteres.
- `MAG_COOKIE_SECURE`: opcional; cookies Secure ficam habilitados fora do modo demo por padrão; desative somente para localhost sem HTTPS.
- `MAG_ENABLE_REAL_SCRAPER=1`: opt-in explícito para consulta real individual autorizada.
- `MAG_ALLOW_DEMO_DATA=1`: opt-in adicional para dados fictícios de teste; mantenha desativado na base de dados real.

Nunca publique segredos, chaves, CPFs ou credenciais de demonstração no Git.

A documentação de arquitetura e o roadmap estão em [ARQUITETURA.md](ARQUITETURA.md).
