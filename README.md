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

## Cadastrar dados reais manualmente

Entre como administrador, abra **Processos → Cadastrar processo / testar consulta**, preencha o formulário e salve. Nome e CPF são armazenados criptografados com a chave Fernet; a lista mascara o CPF. Só insira dados pessoais se tiver autorização e base legal para tratá-los. A instância local de desenvolvimento não deve ser exposta publicamente.

## Testar uma consulta real do scraper

Por segurança, consultas reais ficam desativadas por padrão e nunca são feitas pelo agendador. Primeiro confirme que você está autorizado a consultar o portal e os dados em questão, instale o Chromium (`python -m playwright install chromium`) e reinicie a aplicação com:

```powershell
$env:MAG_DEMO = "0"
$env:MAG_ENABLE_REAL_SCRAPER = "1"
$env:MAG_COOKIE_SECURE = "0" # somente localhost sem HTTPS
$env:MAG_SECRET = "gere-um-segredo-aleatorio-de-32-caracteres-ou-mais"
$env:MAG_FERNET_KEY = "chave-fernet-valida-gerada-para-este-ambiente"
$env:MAG_BLIND_SALT = "salt-aleatorio-de-32-caracteres-ou-mais"
$env:MAG_ADMIN_EMAIL = "seu-email"
$env:MAG_ADMIN_PASSWORD = "uma-senha-forte-com-12-caracteres-ou-mais"
python -m uvicorn app.web:app --host 127.0.0.1 --port 8000
```

Gere uma chave Fernet com `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Defina os valores secretos de verdade localmente; não use os exemplos acima literalmente nem os adicione ao Git. Na tela de cadastro, teste **um processo por vez**. Há intervalo mínimo de 60 segundos para repetir a consulta ao mesmo número. O scraper não contorna login, token, CAPTCHA ou bloqueios; se o portal negar acesso, peça acesso oficial à MAG. A página de acompanhamento pode exigir token/sessão que o fluxo público não disponibiliza, portanto a consulta pode falhar legitimamente.

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

Nunca publique segredos, chaves, CPFs ou credenciais de demonstração no Git.

A documentação de arquitetura e o roadmap estão em [ARQUITETURA.md](ARQUITETURA.md).
