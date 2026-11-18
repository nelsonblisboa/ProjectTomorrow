from app.scraper import _norm_data, _parse_timeline, consultar_processo_real


def test_parse_labeled_result_lines():
    parsed = _parse_timeline("""
Número do Processo
12345/2026
Cliente: Maria de Exemplo
Benefício solicitado: Renda por invalidez
Status: Pendente
Dias corridos: 18
Última atualização: 17/11/2026
Pendência: Enviar documentação
""")
    assert parsed["numero_processo"] == "12345/2026"
    assert parsed["cliente_nome"] == "Maria de Exemplo"
    assert parsed["beneficio_solicitado"] == "Renda por invalidez"
    assert parsed["status"] == "Pendente"
    assert parsed["dias_corridos"] == 18
    assert parsed["ultima_atualizacao"] == "2026-11-17"
    assert parsed["comentarios"] == "Enviar documentação"


def test_parse_empty_or_form_only_text_does_not_invent_response():
    parsed = _parse_timeline("Número do Processo CPF Data de Nascimento Acompanhar Processo")
    assert parsed["status"] is None
    assert parsed["cliente_nome"] is None
    assert parsed["beneficio_solicitado"] is None


def test_date_parser_handles_portal_and_html_date_formats():
    assert _norm_data("18/11/2026") == "2026-11-18"
    assert _norm_data("2026-11-18") == "2026-11-18"
    assert _norm_data("not a date") is None


def test_real_lookup_requires_all_form_fields_without_browser():
    result = consultar_processo_real("123", None, None)
    assert result["ok"] is False
    assert "obrigatórios" in result["erro"]
