"""MAG benefit status lookup scraper.

Uses the public MAG page and its normal form only. It does not bypass login,
CAPTCHA, session/token checks, or access restrictions. Real lookups must be
explicitly enabled and authorized by the operator.
"""
from __future__ import annotations

import json
import logging
import random
import re
import string
import time
import unicodedata
from datetime import datetime, timezone, timedelta

from . import db

log = logging.getLogger("mag.scraper")
URL_MAG = "https://mag.com.br/acompanhar-beneficio/"
INTERVALO_MINUTOS = 30


def _plain(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value).strip().lower()


def _norm_data(valor: str | None) -> str | None:
    """Normalize common Brazilian and ISO date formats to yyyy-mm-dd."""
    if not valor:
        return None
    value = valor.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _norm_cpf(valor: str | None) -> str | None:
    if not valor:
        return None
    digits = re.sub(r"\D", "", valor)
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return valor.strip()


_FIELD_LABELS = {
    "numero_processo": ("numero do processo", "numero processo", "processo"),
    "cpf": ("cpf",),
    "data_nascimento": ("data de nascimento", "nascimento"),
    "beneficio_solicitado": ("beneficio solicitado", "beneficio"),
    "codigo_produtor": ("codigo do produtor", "produtor"),
    "cliente_nome": ("nome do cliente", "cliente", "segurado"),
    "data_solicitacao": ("data da solicitacao", "data de entrada"),
    "ultima_atualizacao": ("ultima atualizacao", "atualizado em"),
    "dias_corridos": ("dias corridos",),
    "status": ("status", "situacao do processo", "situacao"),
    "comentarios": ("pendencia", "comentario", "observacao"),
}


def _parse_timeline(texto: str) -> dict:
    """Extract labeled values from the page response without treating the form as a result."""
    lines = [re.sub(r"\s+", " ", line).strip(" \t:–—-") for line in (texto or "").splitlines()]
    lines = [line for line in lines if line]
    found: dict[str, str] = {}
    labels = sorted(
        (( _plain(label), field) for field, variants in _FIELD_LABELS.items() for label in variants),
        key=lambda pair: len(pair[0]), reverse=True,
    )
    for index, line in enumerate(lines):
        normalized = _plain(line)
        for label, field in labels:
            # Accept a label on its own or followed by colon/dash and the value.
            match = re.match(rf"^{re.escape(label)}\s*(?::|\s[-–—]\s)?\s*(.*)$", normalized)
            if not match:
                continue
            value = match.group(1).strip(" :–—-")
            if not value and index + 1 < len(lines):
                candidate = lines[index + 1]
                if not any(_plain(candidate) == label_text for label_text, _ in labels):
                    value = candidate.strip()
            if value:
                found[field] = value
            break

    days_raw = found.get("dias_corridos", "")
    days_match = re.search(r"\d+", days_raw)
    status = found.get("status")
    return {
        "numero_processo": found.get("numero_processo"),
        "beneficio_solicitado": found.get("beneficio_solicitado"),
        "codigo_produtor": found.get("codigo_produtor"),
        "cliente_nome": found.get("cliente_nome"),
        "cpf": _norm_cpf(found.get("cpf")),
        "data_nascimento": _norm_data(found.get("data_nascimento")),
        "data_solicitacao": _norm_data(found.get("data_solicitacao")),
        "ultima_atualizacao": _norm_data(found.get("ultima_atualizacao")),
        "dias_corridos": int(days_match.group()) if days_match else None,
        "status": status.strip().title() if status else None,
        "comentarios": found.get("comentarios"),
    }


def _input_description(locator) -> dict:
    return locator.evaluate("""el => {
      const label = el.labels && el.labels.length
        ? Array.from(el.labels).map(item => item.innerText).join(' ')
        : '';
      const parent = el.parentElement;
      return {
        type: el.getAttribute('type') || 'text',
        hint: [el.getAttribute('placeholder'), el.getAttribute('name'),
               el.getAttribute('id'), el.getAttribute('aria-label'), label,
               parent ? parent.innerText : ''].filter(Boolean).join(' ')
      };
    }""")


def _choose_form_frame(page):
    """Find the main document or iframe containing all three expected inputs."""
    for frame in page.frames:
        try:
            inputs = frame.locator("input:not([type=hidden])")
            candidates = []
            for i in range(inputs.count()):
                item = inputs.nth(i)
                if item.is_visible():
                    candidates.append((item, _input_description(item)))
            selected = {}
            for item, description in candidates:
                hint = _plain(description.get("hint"))
                if "cpf" in hint and "cpf" not in selected:
                    selected["cpf"] = (item, description)
                elif any(token in hint for token in ("data de nascimento", "nascimento", "birth")):
                    selected.setdefault("data_nascimento", (item, description))
                elif any(token in hint for token in ("numero do processo", "numero processo", "processo")):
                    selected.setdefault("numero_processo", (item, description))

            # Some embedded widgets expose no labels. Positional fallback is used only
            # when exactly three visible inputs exist, matching the documented form.
            if len(selected) != 3 and len(candidates) == 3:
                selected = {
                    "numero_processo": candidates[0],
                    "cpf": candidates[1],
                    "data_nascimento": candidates[2],
                }
            if len(selected) == 3:
                return frame, selected
        except Exception:  # Frame may detach while the page is rendering.
            continue
    return None, {}


def consultar_processo_real(numero_processo: str, cpf: str | None = None,
                            data_nascimento: str | None = None,
                            headless: bool = True) -> dict:
    """Submit one authorized lookup and return parsed result or a safe diagnostic."""
    if not numero_processo or not cpf or not data_nascimento:
        return {"ok": False, "erro": "Número do processo, CPF e data de nascimento são obrigatórios."}
    normalized_birth = _norm_data(data_nascimento)
    if not normalized_birth:
        return {"ok": False, "erro": "Data de nascimento inválida; informe uma data válida."}

    from playwright.sync_api import sync_playwright

    browser = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(viewport={"width": 1366, "height": 900})
            page = context.new_page()
            page.goto(URL_MAG, timeout=45_000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            frame, fields = _choose_form_frame(page)
            if frame is None:
                return {"ok": False, "erro": "Não encontrei no site o formulário com processo, CPF e nascimento. O layout do portal pode ter mudado."}

            values = {
                "numero_processo": numero_processo.strip(),
                "cpf": _norm_cpf(cpf),
                "data_nascimento": normalized_birth,
            }
            for name, value in values.items():
                locator, description = fields[name]
                input_type = (description.get("type") or "text").lower()
                if name == "data_nascimento" and input_type != "date":
                    value = datetime.strptime(normalized_birth, "%Y-%m-%d").strftime("%d/%m/%Y")
                locator.fill(value)

            # Use the visible official button; do not submit arbitrary/hidden forms.
            buttons = frame.locator("button:visible, input[type=submit]:visible")
            submit = None
            for i in range(buttons.count()):
                button = buttons.nth(i)
                label = _plain(button.inner_text() or button.get_attribute("value") or button.get_attribute("aria-label"))
                if any(text in label for text in ("acompanhar processo", "acompanhar", "consultar", "buscar")):
                    submit = button
                    break
            if submit is None:
                return {"ok": False, "erro": "Preenchi os campos, mas não encontrei o botão oficial de acompanhar processo."}

            before = frame.locator("body").inner_text(timeout=5000)
            submit.click(timeout=10000)
            # Wait for the widget's normal response; do not replay submissions.
            deadline = time.monotonic() + 15
            response_text = before
            while time.monotonic() < deadline:
                page.wait_for_timeout(500)
                texts = []
                for current_frame in page.frames:
                    try:
                        texts.append(current_frame.locator("body").inner_text(timeout=1000))
                    except Exception:
                        continue
                response_text = "\n".join(texts)
                parsed = _parse_timeline(response_text)
                if any(parsed.get(key) for key in ("status", "cliente_nome", "beneficio_solicitado", "comentarios", "ultima_atualizacao")):
                    break
                if _plain(response_text) != _plain(before) and any(
                    term in _plain(response_text) for term in ("nao encontrado", "nao localizado", "dados invalidos", "nao foi possivel", "erro", "captcha")
                ):
                    break

            normalized_text = _plain(response_text)
            if "403" in normalized_text or "acesso negado" in normalized_text or "captcha" in normalized_text:
                return {"ok": False, "bloqueado": True,
                        "erro": "O portal negou a consulta ou solicitou verificação/autorização. Não foi feita nenhuma tentativa de contornar essa proteção."}
            if _plain(response_text) == _plain(before):
                return {"ok": False, "erro": "O botão foi acionado, mas o portal não apresentou uma resposta diferente. Verifique a consulta manualmente no site."}

            data = _parse_timeline(response_text)
            meaningful = any(data.get(key) for key in
                             ("status", "cliente_nome", "beneficio_solicitado", "comentarios", "ultima_atualizacao", "data_solicitacao"))
            if not meaningful:
                return {"ok": False, "erro": "O portal respondeu, mas os campos da resposta não foram reconhecidos. Nada foi salvo; a estrutura do resultado precisa ser ajustada."}
            data["numero_processo"] = numero_processo.strip()
            # Do not replace response values with the submitted identifiers.
            return {"ok": True, "dados": data}
    except Exception as exc:
        # Avoid echoing submitted personal data or page contents into logs/API errors.
        log.warning("MAG lookup failed (%s)", type(exc).__name__)
        return {"ok": False, "erro": f"Falha na consulta ao portal ({type(exc).__name__}). Confira rede, Chromium e disponibilidade do site."}
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


# Optional development fixture generator retained only behind explicit test opt-in.
_STATUS = ["Em análise", "Pendente", "Concluído", "Aguardando documentação", "Deferido", "Indeferido"]
_BENEFICIOS = ["Renda por invalidez", "Morte / auxílio funeral", "Diária por incapacidade temporária", "Doença grave", "Amparo familiar"]
_NOMES = ["Ana Paula Souza", "Carlos Eduardo Lima", "Maria Fernanda Alves", "João Batista Rocha", "Juliana Mendes Castro", "Roberto Silva Nunes", "Patrícia Gomes Dias", "Marcos Vinícius Prado"]


def _cpf_ficticio() -> str:
    digits = "".join(random.choice(string.digits) for _ in range(11))
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def gerar_demo(n: int = 12) -> list[dict]:
    """Create fictitious records for isolated development/test databases only."""
    today = datetime.now(timezone.utc).date()
    result = []
    for _ in range(n):
        requested = today - timedelta(days=random.randint(5, 120))
        status = random.choice(_STATUS)
        pending = status in ("Pendente", "Aguardando documentação")
        result.append({
            "numero_processo": f"SIM{random.randint(100000, 999999)}",
            "beneficio_solicitado": random.choice(_BENEFICIOS),
            "codigo_produtor": f"P{random.randint(1000, 9999)}",
            "cliente_nome": random.choice(_NOMES), "cpf": _cpf_ficticio(),
            "data_nascimento": f"19{random.randint(50, 99)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
            "data_solicitacao": requested.isoformat(),
            "ultima_atualizacao": today.isoformat(), "dias_corridos": (today - requested).days,
            "status": status,
            "comentarios": "Pendência: falta documentação." if pending else "",
        })
    return result


def executar_coleta(processos_para_consultar: list[dict] | None = None,
                    demo: bool = False) -> dict:
    """Run explicit demo generation or the supplied individual lookups."""
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.init_db()
    new = changed = seen = 0
    status = "ok"
    message = ""
    try:
        if demo:
            records = gerar_demo()
        else:
            records = []
            for target in (processos_para_consultar or []):
                result = consultar_processo_real(target.get("numero_processo", ""),
                                                  target.get("cpf"), target.get("data_nascimento"))
                if result.get("bloqueado"):
                    status, message = "bloqueado", result.get("erro", "")
                elif result.get("ok"):
                    records.append(result["dados"])
                else:
                    status, message = "erro", result.get("erro", "Consulta não concluída.")
        for record in records:
            if not record.get("numero_processo"):
                continue
            seen += 1
            _, action = db.upsert_processo(record)
            if action == "novo":
                new += 1
            elif action == "alterado":
                changed += 1
    except Exception as exc:
        status, message = "erro", "Falha interna durante a coleta."
        log.exception("MAG collection failed (%s)", type(exc).__name__)
    db.registrar_execucao(started, status, seen, new, changed, message)
    result = {"status": status, "vistos": seen, "novos": new,
              "alterados": changed, "mensagem": message}
    log.info("Collection complete: %s", json.dumps(result))
    return result
