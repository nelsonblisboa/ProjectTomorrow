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

Abra http://localhost:8000/login. No modo demo, os usuários de demonstração são criados localmente pelo aplicativo. Não use essas credenciais em ambientes expostos.

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
- `MAG_COOKIE_SECURE`: opcional; por padrão cookies Secure ficam habilitados fora do modo demo (requer HTTPS).

Nunca publique segredos, chaves ou credenciais de demonstração no Git.

A documentação de arquitetura e o roadmap estão em [ARQUITETURA.md](ARQUITETURA.md).
