## Diagnóstico do scraper MAG

Na consulta real, o sistema procura o formulário normal do portal, incluindo seus iframes, associa os campos por rótulo/nome/placeholder e aciona o botão visível “Acompanhar Processo”. A resposta é interpretada por rótulos e só é salva se campos úteis forem reconhecidos; a entrada do formulário nunca é tratada como resultado. A data ISO da interface é convertida para o formato esperado pelo campo do site. A consulta é individual e não é repetida automaticamente após o envio.

Antes de testar, instale o navegador do Playwright (`python -m playwright install chromium`), habilite `MAG_ENABLE_REAL_SCRAPER=1`, mantenha `MAG_DEMO=0`, reinicie o servidor e use apenas consultas autorizadas. Se o portal alterar seu formulário ou negar acesso, a resposta mostrará um diagnóstico genérico; não tente contornar autenticação, CAPTCHA, token ou bloqueios. O portal pode exigir uma sessão/acesso oficial; nesse caso, solicite à MAG um método de integração autorizado.
