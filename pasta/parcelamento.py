import base64
import json
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


TOKEN_URL = os.getenv("SERPRO_TOKEN_URL", "https://autenticacao.sapi.serpro.gov.br/authenticate")
API_BASE = os.getenv("SERPRO_API_BASE", "https://gateway.apiserpro.serpro.gov.br/integra-contador/v1")
TIMEOUT = int(os.getenv("SERPRO_TIMEOUT", "120"))
VERIFY_SSL = os.getenv("SERPRO_VERIFY_SSL", "true").lower() != "false"

URL_PARCELAMENTO_CONSULTAR = API_BASE.rstrip("/") + "/Consultar"
URL_PARCELAMENTO_EMITIR = API_BASE.rstrip("/") + "/Emitir"
URL_SITFIS_APOIAR = API_BASE.rstrip("/") + "/Apoiar"
URL_SITFIS_EMITIR = API_BASE.rstrip("/") + "/Emitir"

ID_SISTEMA = "SITFIS"
ID_SERVICO_APOIAR = "SOLICITARPROTOCOLO91"
ID_SERVICO_EMITIR = "RELATORIOSITFIS92"
VERSAO_APOIAR = os.getenv("SERPRO_VERSAO_APOIAR", "2.0")
VERSAO_EMITIR = os.getenv("SERPRO_VERSAO_EMITIR", "2.0")

PARCELAMENTO_ID_SISTEMA = os.getenv("PARCELAMENTO_ID_SISTEMA", "PARCSN")
PARCELAMENTO_ID_SERVICO_CONSULTAR = os.getenv("PARCELAMENTO_ID_SERVICO_CONSULTAR", "PEDIDOSPARC163")
PARCELAMENTO_ID_SERVICO_EMITIR = os.getenv("PARCELAMENTO_ID_SERVICO_EMITIR", "GERARDAS161")
PARCELAMENTO_VERSAO = os.getenv("PARCELAMENTO_VERSAO", "1.0")

CAIXAPOSTAL_ID_SISTEMA = os.getenv("CAIXAPOSTAL_ID_SISTEMA", "CAIXAPOSTAL")
CAIXAPOSTAL_ID_SERVICO_LISTA = os.getenv("CAIXAPOSTAL_ID_SERVICO_LISTA", "MSGCONTRIBUINTE61")
CAIXAPOSTAL_ID_SERVICO_DETALHE = os.getenv("CAIXAPOSTAL_ID_SERVICO_DETALHE", "MSGDETALHAMENTO62")
CAIXAPOSTAL_ID_SERVICO_INDICADOR = os.getenv("CAIXAPOSTAL_ID_SERVICO_INDICADOR", "INNOVAMSG63")
CAIXAPOSTAL_VERSAO = os.getenv("CAIXAPOSTAL_VERSAO", "1.0")

app = Flask(__name__)
CORS(app)


def digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def infer_document_type(value: Any) -> int:
    doc = digits(value)
    if len(doc) == 11:
        return 1
    return 2


def decode_cert(value: str) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Certificado PEM/KEY vazio.")
    if "-----BEGIN" in text:
        return text.encode("utf-8")
    text = re.sub(r"^data:[^;]+;base64,", "", text)
    return base64.b64decode(re.sub(r"\s+", "", text))


def try_json(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return value
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def post_json(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    cert: Tuple[str, str],
) -> Tuple[int, Dict[str, Any]]:
    resp = session.post(
        url,
        headers=headers,
        json=body,
        cert=cert,
        timeout=TIMEOUT,
        verify=VERIFY_SSL,
    )
    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": resp.text}
    payload["_http_status"] = resp.status_code
    payload["dados_parseados"] = try_json(payload.get("dados"))
    return resp.status_code, payload


def first_not_empty(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def authenticate(session: requests.Session, consumer_key: str, consumer_secret: str, cert: Tuple[str, str]) -> Dict[str, Any]:
    basic = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode("utf-8")).decode("utf-8")
    resp = session.post(
        TOKEN_URL,
        data="grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {basic}",
            "role-type": "TERCEIROS",
            "content-type": "application/x-www-form-urlencoded",
        },
        cert=cert,
        timeout=TIMEOUT,
        verify=VERIFY_SSL,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("access_token") or not payload.get("jwt_token"):
        raise RuntimeError("Resposta de autenticação sem access_token ou jwt_token.")
    return payload


def headers(access_token: str, jwt_token: str, procurador_token: str = "") -> Dict[str, str]:
    out = {
        "Authorization": f"Bearer {access_token}",
        "jwt_token": jwt_token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    if procurador_token:
        out["autenticar_procurador_token"] = procurador_token
    return out


def body_parcelamento_consultar(cnpj_contador: str, cnpj_contribuinte: str) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": PARCELAMENTO_ID_SISTEMA,
            "idServico": PARCELAMENTO_ID_SERVICO_CONSULTAR,
            "versaoSistema": PARCELAMENTO_VERSAO,
            "dados": "",
        },
    }


def body_parcelamento_emitir(cnpj_contador: str, cnpj_contribuinte: str, parcela_aaaamm: int) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": PARCELAMENTO_ID_SISTEMA,
            "idServico": PARCELAMENTO_ID_SERVICO_EMITIR,
            "versaoSistema": PARCELAMENTO_VERSAO,
            "dados": json.dumps({"parcelaParaEmitir": int(parcela_aaaamm)}, ensure_ascii=False),
        },
    }


def body_caixa_postal_lista(
    cnpj_contador: str,
    cnpj_contribuinte: str,
    status_leitura: int,
    indicador_pagina: int,
    indicador_favorito: Optional[int] = None,
    ponteiro_pagina: str = "",
    cnpj_referencia: str = "",
) -> Dict[str, Any]:
    dados: Dict[str, Any] = {
        "statusLeitura": int(status_leitura),
        "indicadorPagina": int(indicador_pagina),
    }
    if indicador_favorito is not None:
        dados["indicadorFavorito"] = int(indicador_favorito)
    if ponteiro_pagina:
        dados["ponteiroPagina"] = digits(ponteiro_pagina)
    if cnpj_referencia:
        dados["cnpjReferencia"] = digits(cnpj_referencia)

    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": CAIXAPOSTAL_ID_SISTEMA,
            "idServico": CAIXAPOSTAL_ID_SERVICO_LISTA,
            "versaoSistema": CAIXAPOSTAL_VERSAO,
            "dados": json.dumps(dados, ensure_ascii=False),
        },
    }


def body_caixa_postal_detalhe(cnpj_contador: str, cnpj_contribuinte: str, isn: str) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": CAIXAPOSTAL_ID_SISTEMA,
            "idServico": CAIXAPOSTAL_ID_SERVICO_DETALHE,
            "versaoSistema": CAIXAPOSTAL_VERSAO,
            "dados": json.dumps({"isn": digits(isn)}, ensure_ascii=False),
        },
    }


def body_caixa_postal_indicador(cnpj_contador: str, cnpj_contribuinte: str) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": CAIXAPOSTAL_ID_SISTEMA,
            "idServico": CAIXAPOSTAL_ID_SERVICO_INDICADOR,
            "versaoSistema": CAIXAPOSTAL_VERSAO,
            "dados": "",
        },
    }


def body_apoiar(cnpj_contador: str, cnpj_contribuinte: str) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": ID_SISTEMA,
            "idServico": ID_SERVICO_APOIAR,
            "versaoSistema": VERSAO_APOIAR,
            "dados": "",
        },
    }


def body_emitir(cnpj_contador: str, cnpj_contribuinte: str, protocolo: str) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": ID_SISTEMA,
            "idServico": ID_SERVICO_EMITIR,
            "versaoSistema": VERSAO_EMITIR,
            "dados": json.dumps({"protocoloRelatorio": protocolo}, ensure_ascii=False),
        },
    }


def extrair_protocolo(payload: Dict[str, Any]) -> Optional[str]:
    dados = payload.get("dados_parseados")
    campos = ("protocoloRelatorio", "protocolo", "numeroProtocolo", "idProtocolo")
    if isinstance(dados, dict):
        for campo in campos:
            if dados.get(campo):
                return str(dados[campo])
    for campo in campos:
        if payload.get(campo):
            return str(payload[campo])
    return None


def extrair_pdf_e_espera(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    dados = payload.get("dados_parseados")
    if not isinstance(dados, dict):
        return None, None
    pdf_b64 = dados.get("pdf") or dados.get("pdfBase64") or dados.get("relatorio") or dados.get("arquivo")
    espera = dados.get("tempoEspera") or dados.get("tempo_espera") or dados.get("tempoDeEspera")
    try:
        return pdf_b64, int(espera) if espera is not None else None
    except Exception:
        return pdf_b64, None


def normalize_spaces(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\r", "\n", text)
    return text.strip()


def money_to_float(value: str) -> Optional[float]:
    try:
        return float(str(value).replace(".", "").replace(",", "."))
    except Exception:
        return None


def extract_pdf_text(pdf_b64: str) -> str:
    pdf_bytes = base64.b64decode(re.sub(r"\s+", "", str(pdf_b64 or "")))
    if pdfplumber:
        import io
        parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts)
    if PdfReader:
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise RuntimeError("Instale pdfplumber ou pypdf para extrair o texto do PDF.")


def parse_omissoes_section(text: str) -> List[Dict[str, Any]]:
    lines = [line.strip() for line in normalize_spaces(text).splitlines() if line.strip()]
    omissoes: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Omissão de "):
            tipo = re.sub(r"_+", "", line).strip()
            detalhe = lines[i + 1] if i + 1 < len(lines) else ""
            ref_match = re.match(r"\(([^)]+)\)\s*(.*)", detalhe)
            referencia_tipo = ref_match.group(1).strip() if ref_match else ""
            periodos_texto = ref_match.group(2).strip() if ref_match else detalhe.strip()
            periodos = [p for p in re.split(r"[ ,]+", periodos_texto) if p]
            omissoes.append({
                "tipo": tipo,
                "referencia_tipo": referencia_tipo,
                "periodos_texto": periodos_texto,
                "periodos": periodos,
            })
            i += 2
            continue
        i += 1
    return omissoes


def parse_debitos_section(text: str) -> List[Dict[str, Any]]:
    text = normalize_spaces(text)
    start = text.find("Pendência - Débito (SIEF)")
    if start < 0:
        return []

    end = text.find("Diagnóstico Fiscal na Procuradoria-Geral da Fazenda Nacional", start)
    block = text[start:end] if end > start else text[start:]

    lines = [line.strip() for line in block.splitlines() if line.strip()]
    debitos: List[Dict[str, Any]] = []
    ultimo: Optional[Dict[str, Any]] = None

    pattern = re.compile(
        r"^(?P<receita>.+?)\s+"
        r"(?P<pa>(?:\d{2}/\d{4}|\d{2}/\d{2}/\d{4}))\s+"
        r"(?P<venc>\d{2}/\d{2}/\d{4})\s+"
        r"(?P<vl_original>\d{1,3}(?:\.\d{3})*,\d{2})\s+"
        r"(?P<sdo_devedor>\d{1,3}(?:\.\d{3})*,\d{2})\s+"
        r"(?P<multa>\d{1,3}(?:\.\d{3})*,\d{2})\s+"
        r"(?P<juros>\d{1,3}(?:\.\d{3})*,\d{2})\s+"
        r"(?P<sdo_cons>\d{1,3}(?:\.\d{3})*,\d{2})\s*"
        r"(?P<situacao>[A-ZÇÃÕÁÉÍÓÚ\- ]+)$"
    )

    for line in lines:
        if "Receita PA/Exerc." in line or line.startswith("Pendência - Débito") or line.startswith("CNPJ:"):
            continue

        notif = re.search(r"Notificação de lançamento:\s*([0-9]+)", line, re.I)
        if notif and ultimo is not None:
            ultimo["notificacao_lancamento"] = notif.group(1)
            continue

        m = pattern.match(line)
        if not m:
            continue

        item = {
            "tipo": "Pendência - Débito (SIEF)",
            "receita": m.group("receita").strip(),
            "periodo_apuracao": m.group("pa").strip(),
            "data_vencimento": m.group("venc").strip(),
            "valor_original": money_to_float(m.group("vl_original")),
            "saldo_devedor": money_to_float(m.group("sdo_devedor")),
            "multa": money_to_float(m.group("multa")),
            "juros": money_to_float(m.group("juros")),
            "saldo_devedor_consolidado": money_to_float(m.group("sdo_cons")),
            "situacao": m.group("situacao").strip(),
        }
        debitos.append(item)
        ultimo = item

    return debitos


def parse_processos_section(text: str) -> List[Dict[str, Any]]:
    text = normalize_spaces(text)
    start = text.find("Pendência - Processo Fiscal (SIEF)")
    if start < 0:
        return []

    lines = [line.strip() for line in text[start:].splitlines() if line.strip()]
    processos: List[Dict[str, Any]] = []

    proc_pattern = re.compile(
        r"^(?P<processo>\d{5,}\.\d{3}\.\d{3}/\d{4}-\d{2})\s+"
        r"(?P<situacao>[A-ZÇÃÕÁÉÍÓÚ\- ]+?)\s+"
        r"(?P<localizacao>.+)$"
    )

    for line in lines:
        if line.startswith("Pendência - Processo Fiscal") or line.startswith("CNPJ:") or line.startswith("Processo Situação"):
            continue
        m = proc_pattern.match(line)
        if not m:
            continue
        processos.append({
            "tipo": "Pendência - Processo Fiscal (SIEF)",
            "processo": m.group("processo").strip(),
            "situacao": m.group("situacao").strip(),
            "localizacao": m.group("localizacao").strip(),
        })

    return processos


def parse_sitfis_estruturado(pdf_b64: str) -> Dict[str, Any]:
    text = extract_pdf_text(pdf_b64)
    text = normalize_spaces(text)

    cnpj_match = re.search(r"CNPJ:\s*([0-9./-]{14,18})\s*-\s*(.+)", text)
    cnpj = cnpj_match.group(1).strip() if cnpj_match else ""
    empresa = cnpj_match.group(2).splitlines()[0].strip() if cnpj_match else ""

    return {
        "cnpj": cnpj,
        "empresa": empresa,
        "omissoes": parse_omissoes_section(text),
        "debitos": parse_debitos_section(text),
        "processos": parse_processos_section(text),
    }


def flatten_scalar_pairs(node: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                out.update(flatten_scalar_pairs(value, name))
            elif value not in (None, ""):
                out[name] = value
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            name = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            if isinstance(value, (dict, list)):
                out.update(flatten_scalar_pairs(value, name))
            elif value not in (None, ""):
                out[name] = value
    return out


def compact_item(item: Dict[str, Any]) -> Dict[str, Any]:
    preferred_keys = (
        "descricao",
        "situacao",
        "modalidade",
        "codigo",
        "numero",
        "referencia",
        "periodo",
        "vencimento",
        "quantidadeParcelas",
        "quantidadePrestacoes",
        "qtdParcelas",
        "qtdPrestacoes",
        "parcelaInicial",
        "parcelaFinal",
        "valor",
        "valorParcela",
        "valorPrestacao",
        "valorConsolidado",
        "saldoDevedor",
        "mensagem",
    )
    compact = {
        key: item.get(key)
        for key in preferred_keys
        if item.get(key) not in (None, "", [], {})
    }
    if compact:
        return compact

    flat = flatten_scalar_pairs(item)
    trimmed: Dict[str, Any] = {}
    for key, value in flat.items():
        if any(token in key.lower() for token in ("pdf", "base64", "arquivo")):
            continue
        trimmed[key] = value
        if len(trimmed) >= 12:
            break
    return trimmed


def summarize_parcelamento_payload(payload: Dict[str, Any], contribuinte_cnpj: str) -> Dict[str, Any]:
    dados = payload.get("dados_parseados")
    candidatos: List[Dict[str, Any]] = []

    if isinstance(dados, list):
        candidatos = [item for item in dados if isinstance(item, dict)]
    elif isinstance(dados, dict):
        for key in ("parcelamentos", "listaParcelamentos", "lista", "itens", "dados", "parcelas", "pedidos"):
            value = dados.get(key)
            if isinstance(value, list) and any(isinstance(item, dict) for item in value):
                candidatos = [item for item in value if isinstance(item, dict)]
                break
        if not candidatos and dados:
            candidatos = [dados]

    return {
        "cnpj": contribuinte_cnpj,
        "codigo": first_not_empty(payload, "codigo", "status", "_http_status"),
        "mensagem": first_not_empty(payload, "mensagem", "message", "msg"),
        "total_registros": len(candidatos),
        "possui_parcelamento": bool(candidatos),
        "dados": [compact_item(item) for item in candidatos[:20]],
    }


def normalize_assunto_modelo(mensagem: Dict[str, Any]) -> str:
    assunto = str(first_not_empty(mensagem, "assuntoModelo", "assunto", "assunto_modelo") or "")
    variavel = first_not_empty(mensagem, "valorParametroAssunto", "valor_parametro_assunto")
    if assunto and variavel not in (None, "") and "++VARIAVEL++" in assunto:
        return assunto.replace("++VARIAVEL++", str(variavel))
    return assunto


def extract_object_list(node: Any) -> List[Dict[str, Any]]:
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    return []


def normalize_caixa_postal_mensagem(mensagem: Dict[str, Any]) -> Dict[str, Any]:
    indicador_leitura = str(first_not_empty(mensagem, "indicadorLeitura", "indLeitura") or "")
    indicador_favorito = str(first_not_empty(mensagem, "indicadorFavorito", "indFavorito") or "")
    return {
        "isn": str(first_not_empty(mensagem, "isn", "ISN") or ""),
        "assunto": normalize_assunto_modelo(mensagem),
        "assunto_modelo": str(first_not_empty(mensagem, "assuntoModelo", "assunto") or ""),
        "valor_parametro_assunto": str(first_not_empty(mensagem, "valorParametroAssunto", "valor_parametro_assunto") or ""),
        "data_envio": str(first_not_empty(mensagem, "dataEnvio", "data_envio") or ""),
        "hora_envio": str(first_not_empty(mensagem, "horaEnvio", "hora_envio") or ""),
        "origem": str(first_not_empty(mensagem, "descricaoOrigem", "origem", "nomeOrigem") or ""),
        "tipo_origem": str(first_not_empty(mensagem, "tipoOrigem", "tipo_origem") or ""),
        "relevancia": str(first_not_empty(mensagem, "relevancia") or ""),
        "indicador_leitura": indicador_leitura,
        "indicador_favorito": indicador_favorito,
        "lida": indicador_leitura == "1",
        "favorita": indicador_favorito == "1",
        "raw": mensagem,
    }


def summarize_caixa_postal_lista_payload(payload: Dict[str, Any], contribuinte_cnpj: str) -> Dict[str, Any]:
    dados = payload.get("dados_parseados")
    parsed: Dict[str, Any] = dados if isinstance(dados, dict) else {}

    mensagens = extract_object_list(parsed.get("listaMensagens"))
    if not mensagens:
        for key in ("mensagens", "conteudo", "lista", "itens"):
            mensagens = extract_object_list(parsed.get(key))
            if mensagens:
                break

    quantidade = first_not_empty(parsed, "quantidadeMensagens", "quantidade", "totalMensagens")
    if quantidade in (None, ""):
        quantidade = len(mensagens)

    return {
        "cnpj": contribuinte_cnpj,
        "codigo": first_not_empty(parsed, "codigo", "status", "_http_status"),
        "mensagem": first_not_empty(payload, "mensagem", "message", "msg"),
        "indicador_ultima_pagina": first_not_empty(parsed, "indicadorUltimaPagina", "ultimaPagina"),
        "ponteiro_pagina_retornada": first_not_empty(parsed, "ponteiroPaginaRetornada", "ponteiroPagina"),
        "ponteiro_proxima_pagina": first_not_empty(parsed, "ponteiroProximaPagina", "proximaPagina"),
        "quantidade_mensagens": int(quantidade) if str(quantidade).isdigit() else quantidade,
        "total_registros": len(mensagens),
        "mensagens": [normalize_caixa_postal_mensagem(item) for item in mensagens],
    }


def summarize_caixa_postal_detalhe_payload(payload: Dict[str, Any], contribuinte_cnpj: str, isn: str) -> Dict[str, Any]:
    dados = payload.get("dados_parseados")
    parsed: Dict[str, Any] = dados if isinstance(dados, dict) else {}

    conteudo = extract_object_list(parsed.get("conteudo"))
    if not conteudo:
        conteudo = extract_object_list(parsed.get("mensagens"))
    if not conteudo and parsed:
        conteudo = [parsed]

    mensagens = [normalize_caixa_postal_mensagem(item) for item in conteudo]
    return {
        "cnpj": contribuinte_cnpj,
        "isn": digits(isn),
        "codigo": first_not_empty(parsed, "codigo", "status", "_http_status"),
        "mensagem": first_not_empty(payload, "mensagem", "message", "msg"),
        "total_registros": len(mensagens),
        "mensagens": mensagens,
    }


def summarize_caixa_postal_indicador_payload(payload: Dict[str, Any], contribuinte_cnpj: str) -> Dict[str, Any]:
    dados = payload.get("dados_parseados")
    parsed: Dict[str, Any] = dados if isinstance(dados, dict) else {}
    indicador = first_not_empty(parsed, "indicadorMensagensNovas", "indicador_mensagens_novas", "indicador")
    indicador_int: Optional[int] = None
    try:
        indicador_int = int(str(indicador))
    except Exception:
        indicador_int = None
    return {
        "cnpj": contribuinte_cnpj,
        "codigo": first_not_empty(parsed, "codigo", "status", "_http_status"),
        "mensagem": first_not_empty(payload, "mensagem", "message", "msg"),
        "indicador_mensagens_novas": indicador_int,
        "possui_mensagem_nova": indicador_int in (1, 2),
    }


def consultar_um_cnpj(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    procurador_token: str,
) -> Dict[str, Any]:
    hdrs = headers(access_token, jwt_token, procurador_token)
    status, apoiar = post_json(
        session,
        URL_SITFIS_APOIAR,
        hdrs,
        body_apoiar(cnpj_contador, cnpj_contribuinte),
        cert,
    )
    if status >= 400:
        return {
            "cnpj": cnpj_contribuinte,
            "ok": False,
            "omissoes": [],
            "debitos": [],
            "processos": [],
            "erro": "Falha ao solicitar protocolo.",
        }

    protocolo = extrair_protocolo(apoiar)
    if not protocolo:
        return {
            "cnpj": cnpj_contribuinte,
            "ok": False,
            "omissoes": [],
            "debitos": [],
            "processos": [],
            "erro": "Protocolo não localizado.",
        }

    emitir = None
    pdf_b64 = None
    for _ in range(8):
        status, emitir = post_json(
            session,
            URL_SITFIS_EMITIR,
            hdrs,
            body_emitir(cnpj_contador, cnpj_contribuinte, protocolo),
            cert,
        )
        pdf_b64, espera_ms = extrair_pdf_e_espera(emitir)
        if status == 200 and pdf_b64:
            break
        if status == 202 or espera_ms:
            time.sleep(max(1, espera_ms or 5000) / 1000)
            continue
        break

    if not pdf_b64:
        return {
            "cnpj": cnpj_contribuinte,
            "ok": False,
            "omissoes": [],
            "debitos": [],
            "processos": [],
            "erro": "Relatório sem PDF.",
        }

    estruturado = parse_sitfis_estruturado(pdf_b64)
    cnpj_pdf = digits(estruturado.get("cnpj") or "")
    return {
        # Mantem o CNPJ consultado como chave canônica do retorno do lote.
        # Isso evita colapsar múltiplos resultados quando o parser do PDF
        # extrai o mesmo documento para entradas diferentes.
        "cnpj": cnpj_contribuinte,
        "cnpj_pdf": cnpj_pdf,
        "ok": True,
        "empresa": estruturado.get("empresa") or "",
        "omissoes": estruturado.get("omissoes") or [],
        "debitos": estruturado.get("debitos") or [],
        "processos": estruturado.get("processos") or [],
    }


def consultar_um_cnpj_safe(
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    procurador_token: str,
) -> Dict[str, Any]:
    try:
        with requests.Session() as session:
            return consultar_um_cnpj(
                session=session,
                cert=cert,
                access_token=access_token,
                jwt_token=jwt_token,
                cnpj_contador=cnpj_contador,
                cnpj_contribuinte=cnpj_contribuinte,
                procurador_token=procurador_token,
            )
    except Exception as item_exc:
        return {
            "cnpj": cnpj_contribuinte,
            "ok": False,
            "omissoes": [],
            "debitos": [],
            "processos": [],
            "erro": f"Falha ao processar CNPJ no lote: {item_exc}",
        }


def consultar_parcelamento(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
) -> Dict[str, Any]:
    status, resposta = post_json(
        session,
        URL_PARCELAMENTO_CONSULTAR,
        headers(access_token, jwt_token),
        body_parcelamento_consultar(cnpj_contador, cnpj_contribuinte),
        cert,
    )
    if status >= 400:
        raise RuntimeError(first_not_empty(resposta, "mensagem", "message", "raw") or "Falha ao consultar parcelamento.")

    return {
        "ok": True,
        "servico": "parcelamentos_consultar",
        "cnpj": cnpj_contribuinte,
        "sistema": PARCELAMENTO_ID_SISTEMA,
        "resumo": summarize_parcelamento_payload(resposta, cnpj_contribuinte),
    }


def emitir_parcelamento(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    parcela_aaaamm: int,
    return_pdf_base64: bool,
) -> Dict[str, Any]:
    status, resposta = post_json(
        session,
        URL_PARCELAMENTO_EMITIR,
        headers(access_token, jwt_token),
        body_parcelamento_emitir(cnpj_contador, cnpj_contribuinte, parcela_aaaamm),
        cert,
    )
    if status >= 400:
        raise RuntimeError(first_not_empty(resposta, "mensagem", "message", "raw") or "Falha ao emitir DAS.")

    dados = resposta.get("dados_parseados")
    pdf_b64 = None
    if isinstance(dados, dict):
        pdf_b64 = first_not_empty(dados, "docArrecadacaoPdfB64", "pdf", "pdfBase64", "arquivo")

    retorno = {
        "ok": True,
        "servico": "parcelamentos_emitir",
        "cnpj": cnpj_contribuinte,
        "parcela_aaaamm": int(parcela_aaaamm),
        "resumo": summarize_parcelamento_payload(resposta, cnpj_contribuinte),
    }
    if return_pdf_base64 and pdf_b64:
        retorno["pdf_base64"] = pdf_b64
    return retorno


def consultar_caixa_postal_lista(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    status_leitura: int,
    indicador_pagina: int,
    indicador_favorito: Optional[int] = None,
    ponteiro_pagina: str = "",
    cnpj_referencia: str = "",
) -> Dict[str, Any]:
    status, resposta = post_json(
        session,
        URL_PARCELAMENTO_CONSULTAR,
        headers(access_token, jwt_token),
        body_caixa_postal_lista(
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=cnpj_contribuinte,
            status_leitura=status_leitura,
            indicador_pagina=indicador_pagina,
            indicador_favorito=indicador_favorito,
            ponteiro_pagina=ponteiro_pagina,
            cnpj_referencia=cnpj_referencia,
        ),
        cert,
    )
    if status >= 400:
        raise RuntimeError(first_not_empty(resposta, "mensagem", "message", "raw") or "Falha ao consultar caixa postal.")
    return {
        "ok": True,
        "servico": "caixa_postal_caixa_entrada",
        "cnpj": cnpj_contribuinte,
        "resumo": summarize_caixa_postal_lista_payload(resposta, cnpj_contribuinte),
    }


def consultar_caixa_postal_detalhe(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    isn: str,
) -> Dict[str, Any]:
    status, resposta = post_json(
        session,
        URL_PARCELAMENTO_CONSULTAR,
        headers(access_token, jwt_token),
        body_caixa_postal_detalhe(cnpj_contador, cnpj_contribuinte, isn),
        cert,
    )
    if status >= 400:
        raise RuntimeError(first_not_empty(resposta, "mensagem", "message", "raw") or "Falha ao consultar detalhe da mensagem.")
    return {
        "ok": True,
        "servico": "caixa_postal_detalhe_mensagem",
        "cnpj": cnpj_contribuinte,
        "isn": digits(isn),
        "resumo": summarize_caixa_postal_detalhe_payload(resposta, cnpj_contribuinte, isn),
    }


def consultar_caixa_postal_indicador(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
) -> Dict[str, Any]:
    status, resposta = post_json(
        session,
        URL_PARCELAMENTO_CONSULTAR,
        headers(access_token, jwt_token),
        body_caixa_postal_indicador(cnpj_contador, cnpj_contribuinte),
        cert,
    )
    if status >= 400:
        raise RuntimeError(first_not_empty(resposta, "mensagem", "message", "raw") or "Falha ao consultar indicador de novas mensagens.")
    return {
        "ok": True,
        "servico": "caixa_postal_indicador",
        "cnpj": cnpj_contribuinte,
        "resumo": summarize_caixa_postal_indicador_payload(resposta, cnpj_contribuinte),
    }


def get_common_credentials(payload: Dict[str, Any]) -> Tuple[str, str, str]:
    consumer_key = str(payload.get("consumer_key") or "").strip()
    consumer_secret = str(payload.get("consumer_secret") or "").strip()
    cnpj_contador = digits(payload.get("contratante_cnpj") or payload.get("autor_cnpj") or payload.get("cnpj_contador"))
    return consumer_key, consumer_secret, cnpj_contador


def load_cert_paths(payload: Dict[str, Any]) -> Tuple[str, str]:
    pem_bytes = decode_cert(payload.get("pem_base64") or payload.get("pem"))
    key_bytes = decode_cert(payload.get("key_base64") or payload.get("key"))
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as pem_file, tempfile.NamedTemporaryFile(delete=False, suffix=".key") as key_file:
        pem_file.write(pem_bytes)
        key_file.write(key_bytes)
        return pem_file.name, key_file.name


def parse_int_field(
    value: Any,
    field_name: str,
    allowed: Optional[List[int]] = None,
    default: Optional[int] = None,
) -> int:
    if value in (None, ""):
        if default is None:
            raise ValueError(f"Informe {field_name}.")
        return int(default)
    try:
        out = int(str(value).strip())
    except Exception:
        raise ValueError(f"Campo {field_name} invalido.")
    if allowed is not None and out not in allowed:
        allowed_text = ", ".join(str(item) for item in allowed)
        raise ValueError(f"Campo {field_name} invalido. Valores permitidos: {allowed_text}.")
    return out


@app.post("/integra-sitfis/situacao/consultar")
@app.post("/integra-parcelamento/situacao/consultar")
@app.post("/situacao/consultar")
def consultar_situacao():
    payload = request.get_json(silent=True) or {}
    user = str(payload.get("user") or "").strip()
    consumer_key = str(payload.get("consumer_key") or "").strip()
    consumer_secret = str(payload.get("consumer_secret") or "").strip()
    cnpj_contador = digits(payload.get("contratante_cnpj") or payload.get("autor_cnpj") or payload.get("cnpj_contador"))
    cnpjs = payload.get("cnpjs") or payload.get("contribuintes") or payload.get("lista_cnpj") or []
    procurador_token = str(payload.get("procurador_token") or "").strip()
    max_workers_raw = payload.get("max_workers", os.getenv("SITFIS_MAX_WORKERS", "2"))

    if isinstance(cnpjs, str):
        cnpjs = [x for x in re.split(r"[\s,;]+", cnpjs) if x]
    cnpjs = [digits(cnpj) for cnpj in cnpjs]
    cnpjs = [cnpj for cnpj in dict.fromkeys(cnpjs) if len(cnpj) in (11, 14)]

    if not user:
        return jsonify({"ok": False, "erro": "Informe o user."}), 400
    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 dígitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not cnpjs:
        return jsonify({"ok": False, "erro": "Informe uma lista de CPFs/CNPJs."}), 400

    try:
        max_workers = int(str(max_workers_raw).strip())
    except Exception:
        max_workers = 2
    max_workers = max(1, min(max_workers, 8))

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "user": user, "erro": f"Certificado inválido: {exc}"}), 400

    try:
        with requests.Session() as auth_session:
            cert = (pem_path, key_path)
            auth = authenticate(auth_session, consumer_key, consumer_secret, cert)

        resultados: List[Dict[str, Any]] = []
        if max_workers <= 1 or len(cnpjs) <= 1:
            for cnpj in cnpjs:
                resultados.append(
                    consultar_um_cnpj_safe(
                        cert=cert,
                        access_token=auth["access_token"],
                        jwt_token=auth["jwt_token"],
                        cnpj_contador=cnpj_contador,
                        cnpj_contribuinte=cnpj,
                        procurador_token=procurador_token,
                    )
                )
        else:
            ordered_results: List[Optional[Dict[str, Any]]] = [None] * len(cnpjs)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(
                        consultar_um_cnpj_safe,
                        cert,
                        auth["access_token"],
                        auth["jwt_token"],
                        cnpj_contador,
                        cnpj,
                        procurador_token,
                    ): idx
                    for idx, cnpj in enumerate(cnpjs)
                }
                for future in as_completed(future_map):
                    idx = future_map[future]
                    try:
                        ordered_results[idx] = future.result()
                    except Exception as exc:
                        ordered_results[idx] = {
                            "cnpj": cnpjs[idx],
                            "ok": False,
                            "omissoes": [],
                            "debitos": [],
                            "processos": [],
                            "erro": f"Falha ao processar CNPJ no lote: {exc}",
                        }

            resultados = [
                item if item is not None else {
                    "cnpj": cnpjs[idx],
                    "ok": False,
                    "omissoes": [],
                    "debitos": [],
                    "processos": [],
                    "erro": "Falha interna ao consolidar resultado no lote.",
                }
                for idx, item in enumerate(ordered_results)
            ]

        return jsonify({
            "ok": all(item.get("ok") for item in resultados),
            "user": user,
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "total_cnpjs": len(resultados),
            "max_workers": max_workers,
            "resultados": resultados,
        })
    except Exception as exc:
        return jsonify({"ok": False, "user": user, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-parcelamento/parcelamentos/consultar")
def consultar_parcelamento_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = consultar_parcelamento(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-parcelamento/parcelamentos/emitir")
def emitir_parcelamento_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))
    parcela_aaaamm = digits(payload.get("parcela_aaaamm"))
    return_pdf_base64 = bool(payload.get("return_pdf_base64", True))

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not re.fullmatch(r"\d{6}", parcela_aaaamm):
        return jsonify({"ok": False, "erro": "Informe parcela_aaaamm no formato AAAAMM."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = emitir_parcelamento(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            parcela_aaaamm=int(parcela_aaaamm),
            return_pdf_base64=return_pdf_base64,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-parcelamento/parcelamentos/consultar-com-sitfis")
def consultar_parcelamento_com_sitfis_route():
    payload = request.get_json(silent=True) or {}
    user = str(payload.get("user") or "").strip()
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))
    procurador_token = str(payload.get("procurador_token") or "").strip()

    if not user:
        return jsonify({"ok": False, "erro": "Informe o user."}), 400
    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "user": user, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        parcelamento = consultar_parcelamento(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
        )
        situacao_fiscal = consultar_um_cnpj(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            procurador_token=procurador_token,
        )
        return jsonify({
            "ok": bool(parcelamento.get("ok") and situacao_fiscal.get("ok")),
            "user": user,
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "cnpj": contribuinte_cnpj,
            "parcelamento": parcelamento,
            "situacao_fiscal": situacao_fiscal,
        })
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "user": user, "erro": detail}), 400
    except Exception as exc:
        return jsonify({"ok": False, "user": user, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-caixapostal/caixa-entrada/consultar")
@app.post("/integra-caixapostal/mensagens/consultar")
def consultar_caixa_postal_caixa_entrada_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))
    ponteiro_pagina = digits(payload.get("ponteiro_pagina") or payload.get("ponteiroPagina"))
    cnpj_referencia = digits(payload.get("cnpj_referencia") or payload.get("cnpjReferencia"))

    try:
        status_leitura = parse_int_field(
            payload.get("status_leitura", payload.get("statusLeitura")),
            "status_leitura",
            allowed=[0, 1, 2],
            default=0,
        )
        indicador_pagina = parse_int_field(
            payload.get("indicador_pagina", payload.get("indicadorPagina")),
            "indicador_pagina",
            allowed=[0, 1],
            default=0,
        )
        indicador_favorito_raw = payload.get("indicador_favorito", payload.get("indicadorFavorito"))
        indicador_favorito = None
        if indicador_favorito_raw not in (None, ""):
            indicador_favorito = parse_int_field(
                indicador_favorito_raw,
                "indicador_favorito",
                allowed=[0, 1],
            )
    except ValueError as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 400

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if indicador_pagina == 1 and not ponteiro_pagina:
        return jsonify({"ok": False, "erro": "Para indicador_pagina=1, informe ponteiro_pagina."}), 400
    if cnpj_referencia and len(cnpj_referencia) != 14:
        return jsonify({"ok": False, "erro": "Campo cnpj_referencia deve possuir 14 digitos."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = consultar_caixa_postal_lista(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            status_leitura=status_leitura,
            indicador_pagina=indicador_pagina,
            indicador_favorito=indicador_favorito,
            ponteiro_pagina=ponteiro_pagina,
            cnpj_referencia=cnpj_referencia,
        )
        resultado["filtros"] = {
            "status_leitura": status_leitura,
            "indicador_pagina": indicador_pagina,
            "indicador_favorito": indicador_favorito,
            "ponteiro_pagina": ponteiro_pagina,
            "cnpj_referencia": cnpj_referencia,
        }
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-caixapostal/mensagens/detalhar")
def consultar_caixa_postal_detalhe_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))
    isn = digits(payload.get("isn"))

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not isn:
        return jsonify({"ok": False, "erro": "Informe o ISN da mensagem."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = consultar_caixa_postal_detalhe(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            isn=isn,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-caixapostal/mensagens/indicador")
def consultar_caixa_postal_indicador_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = consultar_caixa_postal_indicador(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "rotas": [
            "/integra-sitfis/situacao/consultar",
            "/integra-parcelamento/situacao/consultar",
            "/situacao/consultar",
            "/integra-parcelamento/parcelamentos/consultar",
            "/integra-parcelamento/parcelamentos/emitir",
            "/integra-parcelamento/parcelamentos/consultar-com-sitfis",
            "/integra-caixapostal/caixa-entrada/consultar",
            "/integra-caixapostal/mensagens/consultar",
            "/integra-caixapostal/mensagens/detalhar",
            "/integra-caixapostal/mensagens/indicador",
        ],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
