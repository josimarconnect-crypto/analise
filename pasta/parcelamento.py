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
URL_INTEGRA_CONSULTAR = API_BASE.rstrip("/") + "/Consultar"
URL_INTEGRA_EMITIR = API_BASE.rstrip("/") + "/Emitir"
URL_INTEGRA_DECLARAR = API_BASE.rstrip("/") + "/Declarar"
URL_INTEGRA_APOIAR = API_BASE.rstrip("/") + "/Apoiar"

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

PGDASD_ID_SISTEMA = os.getenv("PGDASD_ID_SISTEMA", "PGDASD")
PGDASD_VERSAO = os.getenv("PGDASD_VERSAO", "1.0")

PROCURACOES_ID_SISTEMA = os.getenv("PROCURACOES_ID_SISTEMA", "PROCURACOES")
PROCURACOES_ID_SERVICO_OBTER = os.getenv("PROCURACOES_ID_SERVICO_OBTER", "OBTERPROCURACAO41")
PROCURACOES_VERSAO = os.getenv("PROCURACOES_VERSAO", "1")
PAGTOWEB_ID_SISTEMA = os.getenv("PAGTOWEB_ID_SISTEMA", "PAGTOWEB")
PAGTOWEB_VERSAO = os.getenv("PAGTOWEB_VERSAO", "1.0")
DCTFWEB_ID_SISTEMA = os.getenv("DCTFWEB_ID_SISTEMA", "DCTFWEB")
DCTFWEB_VERSAO = os.getenv("DCTFWEB_VERSAO", "1.0")
MIT_ID_SISTEMA = os.getenv("MIT_ID_SISTEMA", "MIT")
MIT_VERSAO = os.getenv("MIT_VERSAO", "1.0")
EPROCESSO_ID_SISTEMA = os.getenv("EPROCESSO_ID_SISTEMA", "EPROCESSO")
EPROCESSO_ID_SERVICO_CONSULTAR = os.getenv("EPROCESSO_ID_SERVICO_CONSULTAR", "CONSPROCPORINTER271")
EPROCESSO_VERSAO = os.getenv("EPROCESSO_VERSAO", "2.0")
EVENTOS_ID_SISTEMA = os.getenv("EVENTOS_ID_SISTEMA", "EVENTOSATUALIZACAO")
EVENTOS_VERSAO = os.getenv("EVENTOS_VERSAO", "1.0")
SITCAD_LOOKUP_URL = os.getenv("SITCAD_LOOKUP_URL", "").strip()
SITCAD_LOOKUP_METHOD = os.getenv("SITCAD_LOOKUP_METHOD", "POST").strip().upper() or "POST"
SITCAD_LOOKUP_TIMEOUT = int(os.getenv("SITCAD_LOOKUP_TIMEOUT", "30"))
SITCAD_LOOKUP_DOC_FIELD = os.getenv("SITCAD_LOOKUP_DOC_FIELD", "documento").strip() or "documento"
SITCAD_LOOKUP_TOKEN_HEADER = os.getenv("SITCAD_LOOKUP_TOKEN_HEADER", "X-API-Key").strip() or "X-API-Key"
SITCAD_LOOKUP_TOKEN = os.getenv("SITCAD_LOOKUP_TOKEN", "").strip()

CND_TOKEN_URL = os.getenv("CND_TOKEN_URL", "https://gateway.apiserpro.serpro.gov.br/token").strip()
CND_CERTIDAO_URL = os.getenv(
    "CND_CERTIDAO_URL",
    "https://gateway.apiserpro.serpro.gov.br/consulta-cnd/v1/certidao",
).strip()
CND_STATUS7_WAIT_MS = int(os.getenv("CND_STATUS7_WAIT_MS", "500"))
CND_MAX_TENTATIVAS = int(os.getenv("CND_MAX_TENTATIVAS", "12"))
CND_OUTPUT_DIR = os.getenv("CND_OUTPUT_DIR", "jsoncnd").strip() or "jsoncnd"

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


class PgdasdUpstreamError(RuntimeError):
    def __init__(self, message: str, status: int, resposta: Dict[str, Any], url: str, body: Dict[str, Any]):
        super().__init__(message)
        self.status = int(status or 500)
        self.resposta = resposta
        self.url = url
        self.body = body


def first_not_empty(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _iter_nodes(root: Any):
    queue: List[Any] = [try_json(root)]
    seen: set = set()
    while queue:
        node = queue.pop(0)
        if node is None:
            continue
        if isinstance(node, (str, int, float, bool)):
            yield node
            continue
        if isinstance(node, list):
            for item in node:
                queue.append(try_json(item))
            continue
        if not isinstance(node, dict):
            continue
        marker = id(node)
        if marker in seen:
            continue
        seen.add(marker)
        yield node
        for value in node.values():
            queue.append(try_json(value))


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except Exception:
        return None


def _pick_first_value(payload: Any, keys: List[str]) -> Any:
    wanted = {re.sub(r"[^a-z0-9]+", "", k.lower()) for k in keys}
    for node in _iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        for k, v in node.items():
            nk = re.sub(r"[^a-z0-9]+", "", str(k).lower())
            if nk in wanted and v not in (None, "", [], {}):
                return v
    return None


def _pick_first_list(payload: Any, keys: List[str]) -> List[Any]:
    wanted = {re.sub(r"[^a-z0-9]+", "", k.lower()) for k in keys}
    for node in _iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        for k, v in node.items():
            nk = re.sub(r"[^a-z0-9]+", "", str(k).lower())
            if nk in wanted and isinstance(v, list):
                return v
    return []


def _mit_status_label(raw: Any) -> str:
    text = _normalize_text(raw)
    norm = re.sub(r"[^a-z0-9]+", "", text.lower())
    mapa = {
        "0": "Não Entregue",
        "1": "Entregues",
        "2": "Encerrado",
        "3": "Em edição",
        "4": "Não Obrigado",
    }
    if text in mapa:
        return mapa[text]
    if norm in mapa:
        return mapa[norm]
    if "naoobrig" in norm:
        return "Não Obrigado"
    if "edicao" in norm or "andamento" in norm:
        return "Em edição"
    if "encerr" in norm:
        return "Encerrado"
    if "entreg" in norm or "transmit" in norm:
        return "Entregues"
    if "naoentreg" in norm:
        return "Não Entregue"
    return text or "Não Entregue"


def normalize_mit_response(payload: Any) -> Dict[str, Any]:
    data = try_json(payload)
    detalhes = try_json(first_not_empty(data if isinstance(data, dict) else {}, "dados_parseados", "dados")) if isinstance(data, dict) else None
    source = detalhes if detalhes not in (None, "", [], {}) else data

    apuracoes = _pick_first_list(source, [
        "listaApuracoes",
        "apuracoes",
        "listaApuracao",
        "listaApuracoesMesAno",
        "listaApuracoesPorPeriodo",
    ])
    ap_item = apuracoes[0] if apuracoes else None
    if not isinstance(ap_item, dict):
        ap_item = source if isinstance(source, dict) else {}

    status_raw = (
        _pick_first_value(ap_item, ["situacaoApuracao", "statusApuracao", "situacao", "status"])
        or _pick_first_value(source, ["situacaoApuracao", "statusApuracao", "situacao", "status"])
    )
    data_enc = (
        _pick_first_value(ap_item, ["dataEncerramento", "dataHoraEncerramento", "dataEncerramentoApuracao"])
        or _pick_first_value(source, ["dataEncerramento", "dataHoraEncerramento", "dataEncerramentoApuracao"])
    )
    valor = (
        _pick_first_value(ap_item, ["valorApurado", "valorTotalApurado", "valorTotal", "totalApurado"])
        or _pick_first_value(source, ["valorApurado", "valorTotalApurado", "valorTotal", "totalApurado"])
    )
    id_apuracao = (
        _pick_first_value(ap_item, ["idApuracao", "idapuracao"])
        or _pick_first_value(source, ["idApuracao", "idapuracao"])
    )
    protocolo = (
        _pick_first_value(ap_item, ["protocoloEncerramento", "protocolo"])
        or _pick_first_value(source, ["protocoloEncerramento", "protocolo"])
    )

    return {
        "status": _mit_status_label(status_raw),
        "status_raw": _normalize_text(status_raw),
        "data_encerramento": _normalize_text(data_enc),
        "valor_apurado": _safe_float(valor) if _safe_float(valor) is not None else 0.0,
        "id_apuracao": _normalize_text(id_apuracao),
        "protocolo_encerramento": _normalize_text(protocolo),
        "apuracoes": apuracoes if isinstance(apuracoes, list) else [],
        "detalhes": source if isinstance(source, (dict, list)) else {},
    }


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


def encode_pedido_dados(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        parsed = try_json(value)
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, ensure_ascii=False)
        return value
    return json.dumps(value, ensure_ascii=False)


def body_pgdasd_executar(
    cnpj_contador: str,
    cnpj_contribuinte: str,
    id_servico: str,
    dados: Any,
    id_sistema: str = PGDASD_ID_SISTEMA,
    versao_sistema: str = PGDASD_VERSAO,
) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": id_sistema or PGDASD_ID_SISTEMA,
            "idServico": id_servico,
            "versaoSistema": versao_sistema or PGDASD_VERSAO,
            "dados": encode_pedido_dados(dados),
        },
    }


def body_procuracoes_obter(
    cnpj_contador: str,
    cnpj_contribuinte: str,
    outorgante: str,
    outorgado: str,
    id_servico: str = PROCURACOES_ID_SERVICO_OBTER,
    id_sistema: str = PROCURACOES_ID_SISTEMA,
    versao_sistema: str = PROCURACOES_VERSAO,
) -> Dict[str, Any]:
    dados = {
        "outorgante": digits(outorgante),
        "tipoOutorgante": str(infer_document_type(outorgante)),
        "outorgado": digits(outorgado),
        "tipoOutorgado": str(infer_document_type(outorgado)),
    }
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": id_sistema or PROCURACOES_ID_SISTEMA,
            "idServico": id_servico or PROCURACOES_ID_SERVICO_OBTER,
            "versaoSistema": versao_sistema or PROCURACOES_VERSAO,
            "dados": json.dumps(dados, ensure_ascii=False),
        },
    }


def body_pagtoweb_executar(
    cnpj_contador: str,
    cnpj_contribuinte: str,
    id_servico: str,
    dados: Any,
    id_sistema: str = PAGTOWEB_ID_SISTEMA,
    versao_sistema: str = PAGTOWEB_VERSAO,
) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": id_sistema or PAGTOWEB_ID_SISTEMA,
            "idServico": id_servico,
            "versaoSistema": versao_sistema or PAGTOWEB_VERSAO,
            "dados": encode_pedido_dados(dados),
        },
    }


def body_dctfweb_executar(
    cnpj_contador: str,
    cnpj_contribuinte: str,
    id_servico: str,
    dados: Any,
    id_sistema: str = DCTFWEB_ID_SISTEMA,
    versao_sistema: str = DCTFWEB_VERSAO,
) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": id_sistema or DCTFWEB_ID_SISTEMA,
            "idServico": id_servico,
            "versaoSistema": versao_sistema or DCTFWEB_VERSAO,
            "dados": encode_pedido_dados(dados),
        },
    }


def body_mit_executar(
    cnpj_contador: str,
    cnpj_contribuinte: str,
    id_servico: str,
    dados: Any,
    id_sistema: str = MIT_ID_SISTEMA,
    versao_sistema: str = MIT_VERSAO,
) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": id_sistema or MIT_ID_SISTEMA,
            "idServico": id_servico,
            "versaoSistema": versao_sistema or MIT_VERSAO,
            "dados": encode_pedido_dados(dados),
        },
    }


def body_eprocesso_executar(
    cnpj_contador: str,
    cnpj_contribuinte: str,
    id_servico: str,
    dados: Any,
    id_sistema: str = EPROCESSO_ID_SISTEMA,
    versao_sistema: str = EPROCESSO_VERSAO,
) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": infer_document_type(cnpj_contribuinte)},
        "pedidoDados": {
            "idSistema": id_sistema or EPROCESSO_ID_SISTEMA,
            "idServico": id_servico,
            "versaoSistema": versao_sistema or EPROCESSO_VERSAO,
            "dados": encode_pedido_dados(dados),
        },
    }


def body_eventos_executar(
    cnpj_contador: str,
    contribuinte_numero: str,
    contribuinte_tipo: int,
    id_servico: str,
    dados: Any,
    id_sistema: str = EVENTOS_ID_SISTEMA,
    versao_sistema: str = EVENTOS_VERSAO,
) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {
            "numero": str(contribuinte_numero or ""),
            "tipo": int(contribuinte_tipo or 4),
        },
        "pedidoDados": {
            "idSistema": id_sistema or EVENTOS_ID_SISTEMA,
            "idServico": id_servico,
            "versaoSistema": versao_sistema or EVENTOS_VERSAO,
            "dados": encode_pedido_dados(dados),
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


def infer_cnd_tipo_contribuinte(documento: str) -> int:
    doc = digits(documento)
    if len(doc) == 14:
        return 1
    if len(doc) == 11:
        return 2
    if len(doc) == 8:
        return 3
    raise ValueError("Documento invalido para CND. Informe CNPJ (14), CPF (11) ou NIRF (8).")


def infer_cnd_codigo_identificacao(tipo_contribuinte: int) -> str:
    mapping = {1: "9001", 2: "9002", 3: "9003"}
    if tipo_contribuinte not in mapping:
        raise ValueError("TipoContribuinte invalido para CND.")
    return mapping[tipo_contribuinte]


def body_cnd_consultar(
    documento: str,
    gerar_certidao_pdf: bool,
    chave: str = "",
    tipo_contribuinte: Optional[int] = None,
    codigo_identificacao: str = "",
) -> Dict[str, Any]:
    doc = digits(documento)
    tipo = tipo_contribuinte if tipo_contribuinte in (1, 2, 3) else infer_cnd_tipo_contribuinte(doc)
    codigo = str(codigo_identificacao or infer_cnd_codigo_identificacao(tipo)).strip()
    body: Dict[str, Any] = {
        "TipoContribuinte": int(tipo),
        "ContribuinteConsulta": doc,
        "CodigoIdentificacao": codigo,
        "GerarCertidaoPdf": bool(gerar_certidao_pdf),
    }
    chave = str(chave or "").strip()
    if chave:
        body["Chave"] = chave
    return body


def post_cnd_json(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
) -> Tuple[int, Dict[str, Any]]:
    resp = session.post(
        url,
        headers=headers,
        json=body,
        timeout=TIMEOUT,
        verify=VERIFY_SSL,
    )
    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": resp.text}
    payload["_http_status"] = resp.status_code
    return resp.status_code, payload


def authenticate_cnd(session: requests.Session, consumer_key: str, consumer_secret: str, token_url: str) -> Dict[str, Any]:
    basic = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode("utf-8")).decode("utf-8")
    resp = session.post(
        token_url,
        data="grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {basic}",
            "content-type": "application/x-www-form-urlencoded",
        },
        timeout=TIMEOUT,
        verify=VERIFY_SSL,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("access_token"):
        raise RuntimeError("Resposta de autenticacao CND sem access_token.")
    return payload


def headers_cnd(access_token: str, request_tag: str = "") -> Dict[str, str]:
    out = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    tag = str(request_tag or "").strip()
    if tag:
        out["X-Request-Tag"] = tag[:32]
    return out


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


def emitir_cnd(
    session: requests.Session,
    access_token: str,
    contribuinte_documento: str,
    cnd_config: Dict[str, str],
    return_pdf_base64: bool = True,
) -> Dict[str, Any]:
    doc = digits(contribuinte_documento)
    if len(doc) not in (8, 11, 14):
        raise ValueError("Documento invalido para CND. Informe CNPJ (14), CPF (11) ou NIRF (8).")

    tipo_contribuinte = infer_cnd_tipo_contribuinte(doc)
    codigo_identificacao = infer_cnd_codigo_identificacao(tipo_contribuinte)

    request_tag = cnd_config.get("request_tag", "")
    certidao_url = cnd_config["certidao_url"]
    max_tentativas = int(cnd_config["max_tentativas"])
    wait_status7_ms = int(cnd_config["status7_wait_ms"])
    wait_seconds = max(0.5, wait_status7_ms / 1000.0)

    chave = ""
    ultimo_payload: Dict[str, Any] = {}
    ultimo_http_status = 0

    for tentativa in range(1, max_tentativas + 1):
        req_body = body_cnd_consultar(
            documento=doc,
            gerar_certidao_pdf=return_pdf_base64,
            chave=chave,
            tipo_contribuinte=tipo_contribuinte,
            codigo_identificacao=codigo_identificacao,
        )
        ultimo_http_status, ultimo_payload = post_cnd_json(
            session=session,
            url=certidao_url,
            headers=headers_cnd(access_token, request_tag),
            body=req_body,
        )

        status_api = first_not_empty(ultimo_payload, "Status", "status")
        try:
            status_api_int = int(str(status_api))
        except Exception:
            status_api_int = None

        certidao = ultimo_payload.get("Certidao") if isinstance(ultimo_payload.get("Certidao"), dict) else {}
        pdf_b64 = first_not_empty(certidao, "DocumentoPdf", "documentoPdf", "pdf")

        # Status 7: aguardar e repetir a chamada informando a chave.
        if status_api_int == 7:
            chave = str(first_not_empty(ultimo_payload, "Chave", "chave") or "").strip()
            if not chave:
                break
            if tentativa < max_tentativas:
                time.sleep(wait_seconds)
                continue

        # Status 5 e 6: a documentacao orienta repetir a requisicao.
        if status_api_int in (5, 6) and tentativa < max_tentativas:
            chave = ""
            time.sleep(wait_seconds)
            continue

        is_success = status_api_int in (1, 2)
        retorno: Dict[str, Any] = {
            "ok": is_success,
            "servico": "cnd_consultar",
            "documento_consulta": doc,
            "cnpj": doc if len(doc) == 14 else "",
            "cpf": doc if len(doc) == 11 else "",
            "nirf": doc if len(doc) == 8 else "",
            "tipo_contribuinte": tipo_contribuinte,
            "codigo_identificacao": codigo_identificacao,
            "status_http": ultimo_http_status,
            "status_cnd": status_api_int,
            "mensagem": first_not_empty(ultimo_payload, "Mensagem", "mensagem", "message", "raw"),
            "chave": str(first_not_empty(ultimo_payload, "Chave", "chave") or chave or ""),
            "certidao": certidao,
        }
        if return_pdf_base64:
            retorno["pdf_base64"] = str(pdf_b64 or "")
        if not is_success:
            retorno["erro"] = str(retorno["mensagem"] or "Consulta CND nao concluida com sucesso.")
        return retorno

    raise RuntimeError(
        first_not_empty(ultimo_payload, "Mensagem", "mensagem", "message", "raw")
        or "Consulta CND excedeu o numero maximo de tentativas para finalizar o status 7."
    )


def emitir_cnd_um_cnpj_safe(
    access_token: str,
    contribuinte_documento: str,
    cnd_config: Dict[str, str],
    return_pdf_base64: bool = True,
) -> Dict[str, Any]:
    try:
        with requests.Session() as session:
            return emitir_cnd(
                session=session,
                access_token=access_token,
                contribuinte_documento=contribuinte_documento,
                cnd_config=cnd_config,
                return_pdf_base64=return_pdf_base64,
            )
    except Exception as item_exc:
        doc = digits(contribuinte_documento)
        return {
            "ok": False,
            "servico": "cnd_consultar",
            "documento_consulta": doc,
            "cnpj": doc if len(doc) == 14 else "",
            "cpf": doc if len(doc) == 11 else "",
            "nirf": doc if len(doc) == 8 else "",
            "pdf_base64": "",
            "erro": f"Falha ao consultar CND no lote: {item_exc}",
        }


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


def executar_pgdasd(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    endpoint: str,
    id_sistema: str,
    id_servico: str,
    versao_sistema: str,
    dados: Any,
) -> Dict[str, Any]:
    endpoint_key = str(endpoint or "Consultar").strip().lower()
    url_map = {
        "consultar": URL_INTEGRA_CONSULTAR,
        "consulta": URL_INTEGRA_CONSULTAR,
        "emitir": URL_INTEGRA_EMITIR,
        "emissao": URL_INTEGRA_EMITIR,
        "declarar": URL_INTEGRA_DECLARAR,
        "declaracao": URL_INTEGRA_DECLARAR,
    }
    url = url_map.get(endpoint_key)
    if not url:
        raise ValueError("Endpoint PGDAS-D invalido. Use Consultar, Emitir ou Declarar.")

    body = body_pgdasd_executar(
        cnpj_contador=cnpj_contador,
        cnpj_contribuinte=cnpj_contribuinte,
        id_servico=id_servico,
        dados=dados,
        id_sistema=id_sistema,
        versao_sistema=versao_sistema,
    )
    status, resposta = post_json(
        session,
        url,
        headers(access_token, jwt_token),
        body,
        cert,
    )
    if status >= 400:
        dados_parseados = resposta.get("dados_parseados")
        mensagem = first_not_empty(resposta, "mensagem", "message", "erro", "error", "raw")
        if not mensagem and isinstance(dados_parseados, dict):
            mensagem = first_not_empty(dados_parseados, "mensagem", "message", "erro", "error")
        raise PgdasdUpstreamError(
            str(mensagem or "Falha ao executar PGDAS-D."),
            status=status,
            resposta=resposta,
            url=url,
            body=body,
        )

    return {
        "ok": True,
        "servico": "pgdasd_executar",
        "cnpj": cnpj_contribuinte,
        "endpoint": endpoint_key,
        "idSistema": id_sistema,
        "idServico": id_servico,
        "versaoSistema": versao_sistema,
        "dados_enviados": try_json(body["pedidoDados"].get("dados")),
        "retorno": resposta,
        "dados": resposta.get("dados"),
        "dados_parseados": resposta.get("dados_parseados"),
    }


def consultar_procuracoes(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    outorgante: str,
    outorgado: str,
    id_sistema: str = PROCURACOES_ID_SISTEMA,
    id_servico: str = PROCURACOES_ID_SERVICO_OBTER,
    versao_sistema: str = PROCURACOES_VERSAO,
) -> Dict[str, Any]:
    body = body_procuracoes_obter(
        cnpj_contador=cnpj_contador,
        cnpj_contribuinte=cnpj_contribuinte,
        outorgante=outorgante,
        outorgado=outorgado,
        id_servico=id_servico,
        id_sistema=id_sistema,
        versao_sistema=versao_sistema,
    )
    status, resposta = post_json(
        session,
        URL_INTEGRA_CONSULTAR,
        headers(access_token, jwt_token),
        body,
        cert,
    )
    if status >= 400:
      mensagem = first_not_empty(resposta, "mensagem", "message", "erro", "error", "raw") or "Falha ao consultar procurações."
      raise PgdasdUpstreamError(str(mensagem), status=status, resposta=resposta, url=URL_INTEGRA_CONSULTAR, body=body)

    dados = resposta.get("dados_parseados")
    procuracoes = dados if isinstance(dados, list) else []
    return {
        "ok": True,
        "servico": "procuracoes_obter",
        "cnpj": cnpj_contribuinte,
        "idSistema": id_sistema,
        "idServico": id_servico,
        "versaoSistema": versao_sistema,
        "dados_enviados": try_json(body["pedidoDados"].get("dados")),
        "retorno": resposta,
        "dados": resposta.get("dados"),
        "dados_parseados": dados,
        "procuracoes": procuracoes,
        "total_procuracoes": len(procuracoes),
        "gerado_em": datetime.utcnow().isoformat() + "Z",
    }


def executar_pagtoweb(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    endpoint: str,
    id_sistema: str,
    id_servico: str,
    versao_sistema: str,
    dados: Any,
) -> Dict[str, Any]:
    endpoint_key = str(endpoint or "Consultar").strip().lower()
    url_map = {
        "consultar": URL_INTEGRA_CONSULTAR,
        "consulta": URL_INTEGRA_CONSULTAR,
        "emitir": URL_INTEGRA_EMITIR,
        "emissao": URL_INTEGRA_EMITIR,
    }
    url = url_map.get(endpoint_key)
    if not url:
        raise ValueError("Endpoint PAGTOWEB invalido. Use Consultar ou Emitir.")

    body = body_pagtoweb_executar(
        cnpj_contador=cnpj_contador,
        cnpj_contribuinte=cnpj_contribuinte,
        id_servico=id_servico,
        dados=dados,
        id_sistema=id_sistema,
        versao_sistema=versao_sistema,
    )
    status, resposta = post_json(
        session,
        url,
        headers(access_token, jwt_token),
        body,
        cert,
    )
    if status >= 400:
        dados_parseados = resposta.get("dados_parseados")
        mensagem = first_not_empty(resposta, "mensagem", "message", "erro", "error", "raw")
        if not mensagem and isinstance(dados_parseados, dict):
            mensagem = first_not_empty(dados_parseados, "mensagem", "message", "erro", "error")
        raise PgdasdUpstreamError(
            str(mensagem or "Falha ao executar PAGTOWEB."),
            status=status,
            resposta=resposta,
            url=url,
            body=body,
        )

    return {
        "ok": True,
        "servico": "pagtoweb_executar",
        "cnpj": cnpj_contribuinte,
        "endpoint": endpoint_key,
        "idSistema": id_sistema,
        "idServico": id_servico,
        "versaoSistema": versao_sistema,
        "dados_enviados": try_json(body["pedidoDados"].get("dados")),
        "retorno": resposta,
        "dados": resposta.get("dados"),
        "dados_parseados": resposta.get("dados_parseados"),
    }


def executar_dctfweb(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    endpoint: str,
    id_sistema: str,
    id_servico: str,
    versao_sistema: str,
    dados: Any,
) -> Dict[str, Any]:
    endpoint_key = str(endpoint or "Consultar").strip().lower()
    url_map = {
        "consultar": URL_INTEGRA_CONSULTAR,
        "consulta": URL_INTEGRA_CONSULTAR,
        "emitir": URL_INTEGRA_EMITIR,
        "emissao": URL_INTEGRA_EMITIR,
        "declarar": URL_INTEGRA_DECLARAR,
        "declaracao": URL_INTEGRA_DECLARAR,
    }
    url = url_map.get(endpoint_key)
    if not url:
        raise ValueError("Endpoint DCTFWEB invalido. Use Consultar, Emitir ou Declarar.")

    body = body_dctfweb_executar(
        cnpj_contador=cnpj_contador,
        cnpj_contribuinte=cnpj_contribuinte,
        id_servico=id_servico,
        dados=dados,
        id_sistema=id_sistema,
        versao_sistema=versao_sistema,
    )
    status, resposta = post_json(
        session,
        url,
        headers(access_token, jwt_token),
        body,
        cert,
    )
    if status >= 400:
        dados_parseados = resposta.get("dados_parseados")
        mensagem = first_not_empty(resposta, "mensagem", "message", "erro", "error", "raw")
        if not mensagem and isinstance(dados_parseados, dict):
            mensagem = first_not_empty(dados_parseados, "mensagem", "message", "erro", "error")
        raise PgdasdUpstreamError(
            str(mensagem or "Falha ao executar DCTFWEB."),
            status=status,
            resposta=resposta,
            url=url,
            body=body,
        )

    return {
        "ok": True,
        "servico": "dctfweb_executar",
        "cnpj": cnpj_contribuinte,
        "endpoint": endpoint_key,
        "idSistema": id_sistema,
        "idServico": id_servico,
        "versaoSistema": versao_sistema,
        "dados_enviados": try_json(body["pedidoDados"].get("dados")),
        "retorno": resposta,
        "dados": resposta.get("dados"),
        "dados_parseados": resposta.get("dados_parseados"),
    }


def executar_mit(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    endpoint: str,
    id_sistema: str,
    id_servico: str,
    versao_sistema: str,
    dados: Any,
) -> Dict[str, Any]:
    endpoint_key = str(endpoint or "Consultar").strip().lower()
    url_map = {
        "consultar": URL_INTEGRA_CONSULTAR,
        "consulta": URL_INTEGRA_CONSULTAR,
        "emitir": URL_INTEGRA_EMITIR,
        "emissao": URL_INTEGRA_EMITIR,
        "declarar": URL_INTEGRA_DECLARAR,
        "declaracao": URL_INTEGRA_DECLARAR,
        "apoiar": URL_INTEGRA_APOIAR,
        "apoio": URL_INTEGRA_APOIAR,
    }
    url = url_map.get(endpoint_key)
    if not url:
        raise ValueError("Endpoint MIT invalido. Use Apoiar, Consultar, Emitir ou Declarar.")

    body = body_mit_executar(
        cnpj_contador=cnpj_contador,
        cnpj_contribuinte=cnpj_contribuinte,
        id_servico=id_servico,
        dados=dados,
        id_sistema=id_sistema,
        versao_sistema=versao_sistema,
    )
    status, resposta = post_json(
        session,
        url,
        headers(access_token, jwt_token),
        body,
        cert,
    )
    if status >= 400:
        dados_parseados = resposta.get("dados_parseados")
        mensagem = first_not_empty(resposta, "mensagem", "message", "erro", "error", "raw")
        if not mensagem and isinstance(dados_parseados, dict):
            mensagem = first_not_empty(dados_parseados, "mensagem", "message", "erro", "error")
        raise PgdasdUpstreamError(
            str(mensagem or "Falha ao executar MIT."),
            status=status,
            resposta=resposta,
            url=url,
            body=body,
        )

    retorno = {
        "ok": True,
        "servico": "mit_executar",
        "cnpj": cnpj_contribuinte,
        "endpoint": endpoint_key,
        "idSistema": id_sistema,
        "idServico": id_servico,
        "versaoSistema": versao_sistema,
        "dados_enviados": try_json(body["pedidoDados"].get("dados")),
        "retorno": resposta,
        "dados": resposta.get("dados"),
        "dados_parseados": resposta.get("dados_parseados"),
    }
    retorno["mit_normalizado"] = normalize_mit_response(retorno)
    return retorno


def executar_eprocesso(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    cnpj_contribuinte: str,
    endpoint: str,
    id_sistema: str,
    id_servico: str,
    versao_sistema: str,
    dados: Any,
) -> Dict[str, Any]:
    endpoint_key = str(endpoint or "Consultar").strip().lower()
    url_map = {
        "consultar": URL_INTEGRA_CONSULTAR,
        "consulta": URL_INTEGRA_CONSULTAR,
    }
    url = url_map.get(endpoint_key)
    if not url:
        raise ValueError("Endpoint EPROCESSO invalido. Use Consultar.")

    body = body_eprocesso_executar(
        cnpj_contador=cnpj_contador,
        cnpj_contribuinte=cnpj_contribuinte,
        id_servico=id_servico,
        dados=dados,
        id_sistema=id_sistema,
        versao_sistema=versao_sistema,
    )
    status, resposta = post_json(
        session,
        url,
        headers(access_token, jwt_token),
        body,
        cert,
    )
    if status >= 400:
        dados_parseados = resposta.get("dados_parseados")
        mensagem = first_not_empty(resposta, "mensagem", "message", "erro", "error", "raw")
        if not mensagem and isinstance(dados_parseados, dict):
            mensagem = first_not_empty(dados_parseados, "mensagem", "message", "erro", "error")
        raise PgdasdUpstreamError(
            str(mensagem or "Falha ao executar EPROCESSO."),
            status=status,
            resposta=resposta,
            url=url,
            body=body,
        )

    return {
        "ok": True,
        "servico": "eprocesso_executar",
        "cnpj": cnpj_contribuinte,
        "endpoint": endpoint_key,
        "idSistema": id_sistema,
        "idServico": id_servico,
        "versaoSistema": versao_sistema,
        "dados_enviados": try_json(body["pedidoDados"].get("dados")),
        "retorno": resposta,
        "dados": resposta.get("dados"),
        "dados_parseados": resposta.get("dados_parseados"),
    }


def executar_eventos_atualizacao(
    session: requests.Session,
    cert: Tuple[str, str],
    access_token: str,
    jwt_token: str,
    cnpj_contador: str,
    contribuinte_numero: str,
    contribuinte_tipo: int,
    id_sistema: str,
    id_servico: str,
    versao_sistema: str,
    dados: Any,
) -> Dict[str, Any]:
    body = body_eventos_executar(
        cnpj_contador=cnpj_contador,
        contribuinte_numero=contribuinte_numero,
        contribuinte_tipo=contribuinte_tipo,
        id_servico=id_servico,
        dados=dados,
        id_sistema=id_sistema,
        versao_sistema=versao_sistema,
    )
    status, resposta = post_json(
        session,
        API_BASE.rstrip("/") + "/Monitorar",
        headers(access_token, jwt_token),
        body,
        cert,
    )
    if status >= 400:
        dados_parseados = resposta.get("dados_parseados")
        mensagem = first_not_empty(resposta, "mensagem", "message", "erro", "error", "raw")
        if not mensagem and isinstance(dados_parseados, dict):
            mensagem = first_not_empty(dados_parseados, "mensagem", "message", "erro", "error")
        raise PgdasdUpstreamError(
            str(mensagem or "Falha ao executar EVENTOSATUALIZACAO."),
            status=status,
            resposta=resposta,
            url=API_BASE.rstrip("/") + "/Monitorar",
            body=body,
        )

    return {
        "ok": True,
        "servico": "eventosatualizacao_executar",
        "idSistema": id_sistema,
        "idServico": id_servico,
        "versaoSistema": versao_sistema,
        "contribuinte": {"numero": contribuinte_numero, "tipo": contribuinte_tipo},
        "dados_enviados": try_json(body["pedidoDados"].get("dados")),
        "retorno": resposta,
        "dados": resposta.get("dados"),
        "dados_parseados": resposta.get("dados_parseados"),
    }


def parse_bool_field(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "t", "sim", "s", "yes", "y", "on"):
        return True
    if normalized in ("0", "false", "f", "nao", "n", "no", "off"):
        return False
    return default


def sanitize_user_folder_name(user: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(user or "").strip())
    normalized = normalized.strip("._-")
    return normalized or "user"


def resolve_cnd_config(payload: Dict[str, Any]) -> Dict[str, str]:
    certidao_url = str(
        payload.get("cnd_certidao_url")
        or payload.get("cnd_url")
        or CND_CERTIDAO_URL
    ).strip()
    token_url = str(payload.get("cnd_token_url") or CND_TOKEN_URL).strip() or CND_TOKEN_URL
    request_tag = str(payload.get("cnd_request_tag") or payload.get("x_request_tag") or "").strip()

    max_tentativas_raw = payload.get("cnd_max_tentativas", CND_MAX_TENTATIVAS)
    status7_wait_ms_raw = payload.get("cnd_status7_wait_ms", CND_STATUS7_WAIT_MS)

    try:
        max_tentativas = int(str(max_tentativas_raw).strip())
    except Exception:
        max_tentativas = CND_MAX_TENTATIVAS
    try:
        status7_wait_ms = int(str(status7_wait_ms_raw).strip())
    except Exception:
        status7_wait_ms = CND_STATUS7_WAIT_MS

    max_tentativas = max(1, min(max_tentativas, 60))
    status7_wait_ms = max(500, min(status7_wait_ms, 30_000))

    if not certidao_url.lower().startswith(("http://", "https://")):
        raise ValueError("Campo cnd_certidao_url invalido.")
    if not token_url.lower().startswith(("http://", "https://")):
        raise ValueError("Campo cnd_token_url invalido.")

    return {
        "certidao_url": certidao_url,
        "token_url": token_url,
        "request_tag": request_tag[:32],
        "max_tentativas": str(max_tentativas),
        "status7_wait_ms": str(status7_wait_ms),
    }


def ensure_cnd_user_dir(user: str) -> str:
    base_dir = os.path.join(os.getcwd(), CND_OUTPUT_DIR)
    user_dir = os.path.join(base_dir, sanitize_user_folder_name(user))
    os.makedirs(user_dir, exist_ok=True)
    return user_dir


def write_json_file(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)


def save_cnd_lote_results(
    user: str,
    resultados: List[Dict[str, Any]],
    max_workers: int,
    cnd_config: Dict[str, str],
) -> Dict[str, Any]:
    generated_at = datetime.now().isoformat(timespec="seconds")
    user_dir = ensure_cnd_user_dir(user)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    consolidado = {
        "ok": all(item.get("ok") for item in resultados),
        "user": user,
        "gerado_em": generated_at,
        "total_cnpjs": len(resultados),
        "max_workers": max_workers,
        "servico": "cnd_consultar_lote",
        "cnd_certidao_url": cnd_config["certidao_url"],
        "cnd_token_url": cnd_config["token_url"],
        "cnd_request_tag": cnd_config.get("request_tag", ""),
        "resultados": resultados,
    }

    for item in resultados:
        documento = digits(
            first_not_empty(
                item,
                "documento_consulta",
                "cnpj",
                "cpf",
                "nirf",
            )
        )
        if not documento:
            continue
        per_doc_path = os.path.join(user_dir, f"{documento}.json")
        write_json_file(per_doc_path, item)

    latest_path = os.path.join(user_dir, "consolidado.json")
    historical_path = os.path.join(user_dir, f"consolidado_{timestamp}.json")
    write_json_file(latest_path, consolidado)
    write_json_file(historical_path, consolidado)

    consolidado["arquivos"] = {
        "pasta_user": user_dir,
        "consolidado": latest_path,
        "consolidado_historico": historical_path,
    }
    return consolidado


def read_cnd_consolidado(user: str, file_name: str = "consolidado.json") -> Dict[str, Any]:
    user_dir = ensure_cnd_user_dir(user)
    sanitized_name = os.path.basename(str(file_name or "consolidado.json").strip()) or "consolidado.json"
    path = os.path.join(user_dir, sanitized_name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Arquivo nao encontrado para o user informado: {sanitized_name}")
    with open(path, "r", encoding="utf-8") as fp:
        content = json.load(fp)
    if isinstance(content, dict):
        content.setdefault("arquivo", path)
    return content


def _extract_nome_from_html(raw_html: str) -> str:
    text = str(raw_html or "")
    if not text:
        return ""
    patterns = [
        r"(?is)Raz[aã]o\s*Social\s*</[^>]+>\s*([^<\n\r]{3,})",
        r"(?is)Nome\s*Empresarial\s*</[^>]+>\s*([^<\n\r]{3,})",
        r"(?is)Nome\s*do\s*Contribuinte\s*</[^>]+>\s*([^<\n\r]{3,})",
        r"(?is)Nome\s*:\s*([^<\n\r]{3,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        nome = _normalize_text(match.group(1))
        if nome and len(nome) >= 3:
            return nome
    return ""


def _extract_nome_from_payload(payload: Any, raw_text: str = "") -> str:
    nome_keys = [
        "nome",
        "nomeEmpresarial",
        "nomeRazaoSocial",
        "razaoSocial",
        "razao",
        "nomeContribuinte",
        "titular",
        "name",
    ]
    picked = _pick_first_value(payload, nome_keys)
    nome = _normalize_text(picked)
    if nome and len(nome) >= 3:
        return nome
    return _extract_nome_from_html(raw_text)


def consultar_nome_sem_certificado(
    session: requests.Session,
    documento: str,
    lookup_url: str,
    method: str = "POST",
    doc_field: str = "documento",
    timeout: int = 30,
    token_header: str = "X-API-Key",
    token_value: str = "",
) -> Dict[str, Any]:
    url = str(lookup_url or "").strip()
    if not url:
        raise ValueError("Endpoint de busca não configurado.")

    method_norm = str(method or "POST").strip().upper()
    if method_norm not in ("POST", "GET"):
        method_norm = "POST"

    field = str(doc_field or "documento").strip() or "documento"
    headers = {
        "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
        "User-Agent": "ComunidadeFiscal-SemCert/1.0",
    }
    if token_value:
        headers[str(token_header or "X-API-Key").strip() or "X-API-Key"] = token_value

    payload_json = {
        field: documento,
        "documento": documento,
        "doc": documento,
        "cnpj_cpf": documento,
        "cpf_cnpj": documento,
    }
    if method_norm == "GET":
        response = session.get(url, params=payload_json, headers=headers, timeout=timeout, verify=VERIFY_SSL)
    else:
        headers["Content-Type"] = "application/json"
        response = session.post(url, json=payload_json, headers=headers, timeout=timeout, verify=VERIFY_SSL)

    raw_text = response.text or ""
    content_type = (response.headers.get("content-type") or "").lower()

    parsed: Any = {}
    if "application/json" in content_type:
        try:
            parsed = response.json()
        except Exception:
            parsed = {"raw": raw_text}
    else:
        try:
            parsed = response.json()
        except Exception:
            parsed = {"raw": raw_text}

    nome = _extract_nome_from_payload(parsed, raw_text=raw_text)
    out = {
        "ok": response.status_code < 400 and bool(nome),
        "status_http": response.status_code,
        "documento": documento,
        "nome": nome,
        "retorno": parsed,
    }
    if not out["ok"]:
        mensagens = []
        if isinstance(parsed, dict):
            for key in ("erro", "error", "mensagem", "message", "detail"):
                value = parsed.get(key)
                if value not in (None, "", [], {}):
                    mensagens.append(str(value))
        erro = " / ".join(mensagens).strip() or "Nome não encontrado no retorno."
        out["erro"] = erro
    return out


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


@app.post("/integra-cnd/certidoes/emitir")
def emitir_cnd_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, _ = get_common_credentials(payload)
    contribuinte_documento = digits(
        payload.get("contribuinte_cnpj")
        or payload.get("cnpj")
        or payload.get("documento")
        or payload.get("contribuinte")
    )
    return_pdf_base64 = parse_bool_field(payload.get("return_pdf_base64"), default=True)

    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if len(contribuinte_documento) not in (8, 11, 14):
        return jsonify({"ok": False, "erro": "Informe CNPJ (14), CPF (11) ou NIRF (8)."}), 400

    try:
        cnd_config = resolve_cnd_config(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 400

    try:
        with requests.Session() as session:
            auth = authenticate_cnd(
                session=session,
                consumer_key=consumer_key,
                consumer_secret=consumer_secret,
                token_url=cnd_config["token_url"],
            )
            resultado = emitir_cnd(
                session=session,
                access_token=auth["access_token"],
                contribuinte_documento=contribuinte_documento,
                cnd_config=cnd_config,
                return_pdf_base64=return_pdf_base64,
            )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500


@app.post("/integra-cnd/certidoes/consultar")
@app.post("/integra-cnd/certidoes/emitir-lote")
def emitir_cnd_lote_route():
    payload = request.get_json(silent=True) or {}
    user = str(payload.get("user") or "").strip()
    consumer_key, consumer_secret, _ = get_common_credentials(payload)
    cnpjs = (
        payload.get("cnpjs")
        or payload.get("contribuintes")
        or payload.get("lista_cnpj")
        or payload.get("documentos")
        or payload.get("lista_documentos")
        or []
    )
    max_workers_raw = payload.get("max_workers", os.getenv("CND_MAX_WORKERS", "2"))
    # O consolidado do lote precisa carregar o PDF em base64 para o frontend.
    return_pdf_base64 = True

    if isinstance(cnpjs, str):
        cnpjs = [x for x in re.split(r"[\s,;]+", cnpjs) if x]
    cnpjs = [digits(cnpj) for cnpj in cnpjs]
    cnpjs = [cnpj for cnpj in dict.fromkeys(cnpjs) if len(cnpj) in (8, 11, 14)]

    if not user:
        return jsonify({"ok": False, "erro": "Informe o user."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not cnpjs:
        return jsonify({"ok": False, "erro": "Informe uma lista de CNPJs/CPFs/NIRFs."}), 400

    try:
        cnd_config = resolve_cnd_config(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "user": user, "erro": str(exc)}), 400

    try:
        max_workers = int(str(max_workers_raw).strip())
    except Exception:
        max_workers = 2
    max_workers = max(1, min(max_workers, 8))

    try:
        with requests.Session() as auth_session:
            auth = authenticate_cnd(
                session=auth_session,
                consumer_key=consumer_key,
                consumer_secret=consumer_secret,
                token_url=cnd_config["token_url"],
            )

        resultados: List[Dict[str, Any]] = []
        if max_workers <= 1 or len(cnpjs) <= 1:
            for cnpj in cnpjs:
                resultados.append(
                    emitir_cnd_um_cnpj_safe(
                        access_token=auth["access_token"],
                        contribuinte_documento=cnpj,
                        cnd_config=cnd_config,
                        return_pdf_base64=return_pdf_base64,
                    )
                )
        else:
            ordered_results: List[Optional[Dict[str, Any]]] = [None] * len(cnpjs)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(
                        emitir_cnd_um_cnpj_safe,
                        auth["access_token"],
                        cnpj,
                        cnd_config,
                        return_pdf_base64,
                    ): idx
                    for idx, cnpj in enumerate(cnpjs)
                }
                for future in as_completed(future_map):
                    idx = future_map[future]
                    try:
                        ordered_results[idx] = future.result()
                    except Exception as exc:
                        ordered_results[idx] = {
                            "ok": False,
                            "servico": "cnd_consultar",
                            "documento_consulta": cnpjs[idx],
                            "cnpj": cnpjs[idx] if len(cnpjs[idx]) == 14 else "",
                            "cpf": cnpjs[idx] if len(cnpjs[idx]) == 11 else "",
                            "nirf": cnpjs[idx] if len(cnpjs[idx]) == 8 else "",
                            "pdf_base64": "",
                            "erro": f"Falha ao consultar CND no lote: {exc}",
                        }

            resultados = [
                item if item is not None else {
                    "ok": False,
                    "servico": "cnd_consultar",
                    "documento_consulta": cnpjs[idx],
                    "cnpj": cnpjs[idx] if len(cnpjs[idx]) == 14 else "",
                    "cpf": cnpjs[idx] if len(cnpjs[idx]) == 11 else "",
                    "nirf": cnpjs[idx] if len(cnpjs[idx]) == 8 else "",
                    "pdf_base64": "",
                    "erro": "Falha interna ao consolidar resultado no lote.",
                }
                for idx, item in enumerate(ordered_results)
            ]

        consolidado = save_cnd_lote_results(
            user=user,
            resultados=resultados,
            max_workers=max_workers,
            cnd_config=cnd_config,
        )
        consolidado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(consolidado)
    except Exception as exc:
        return jsonify({"ok": False, "user": user, "erro": str(exc)}), 500


@app.post("/integra-cnd/certidoes/exibir-lote")
def exibir_cnd_lote_route():
    payload = request.get_json(silent=True) or {}
    user = str(payload.get("user") or "").strip()
    arquivo = str(payload.get("arquivo") or "consolidado.json").strip() or "consolidado.json"

    if not user:
        return jsonify({"ok": False, "erro": "Informe o user."}), 400

    try:
        consolidado = read_cnd_consolidado(user=user, file_name=arquivo)
        if isinstance(consolidado, dict):
            consolidado.setdefault("user", user)
            consolidado.setdefault("ok", True)
            return jsonify(consolidado)
        return jsonify({"ok": True, "user": user, "dados": consolidado})
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "user": user, "erro": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "user": user, "erro": str(exc)}), 500


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


@app.post("/integra-sn/pgdasd/executar")
def executar_pgdasd_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))
    pedido_dados = payload.get("pedidoDados") if isinstance(payload.get("pedidoDados"), dict) else {}
    endpoint = str(payload.get("endpoint") or "Consultar").strip()
    id_sistema = str(payload.get("idSistema") or pedido_dados.get("idSistema") or PGDASD_ID_SISTEMA).strip()
    id_servico = str(payload.get("idServico") or pedido_dados.get("idServico") or "").strip()
    versao_sistema = str(payload.get("versaoSistema") or pedido_dados.get("versaoSistema") or PGDASD_VERSAO).strip()
    dados = payload.get("dados")
    if dados in (None, ""):
        dados = pedido_dados.get("dados", "")

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not id_servico:
        return jsonify({"ok": False, "erro": "Informe idServico para executar o PGDAS-D."}), 400
    if id_sistema != PGDASD_ID_SISTEMA:
        return jsonify({"ok": False, "erro": "Esta rota aceita apenas idSistema PGDASD."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = executar_pgdasd(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            endpoint=endpoint,
            id_sistema=id_sistema,
            id_servico=id_servico,
            versao_sistema=versao_sistema,
            dados=dados,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except PgdasdUpstreamError as exc:
        pedido_dados_enviado = (exc.body or {}).get("pedidoDados", {})
        return jsonify({
            "ok": False,
            "erro": str(exc),
            "status_serpro": exc.status,
            "endpoint_serpro": exc.url,
            "retorno_serpro": exc.resposta,
            "pedidoDados": pedido_dados_enviado,
        }), 502
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-procuracoes/procuracoes/consultar")
def consultar_procuracoes_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj") or payload.get("outorgante"))
    pedido_dados = payload.get("pedidoDados") if isinstance(payload.get("pedidoDados"), dict) else {}
    dados_payload = payload.get("dados")
    if dados_payload in (None, ""):
        dados_payload = try_json(pedido_dados.get("dados", {}))
    if not isinstance(dados_payload, dict):
        dados_payload = {}

    outorgante = digits(payload.get("outorgante") or dados_payload.get("outorgante") or contribuinte_cnpj)
    outorgado = digits(payload.get("outorgado") or dados_payload.get("outorgado") or payload.get("autor_cnpj") or cnpj_contador)
    id_sistema = str(payload.get("idSistema") or pedido_dados.get("idSistema") or PROCURACOES_ID_SISTEMA).strip()
    id_servico = str(payload.get("idServico") or pedido_dados.get("idServico") or PROCURACOES_ID_SERVICO_OBTER).strip()
    versao_sistema = str(payload.get("versaoSistema") or pedido_dados.get("versaoSistema") or PROCURACOES_VERSAO).strip()

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if len(outorgante) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe outorgante com CPF/CNPJ valido."}), 400
    if len(outorgado) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe outorgado com CPF/CNPJ valido."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if id_sistema != PROCURACOES_ID_SISTEMA:
        return jsonify({"ok": False, "erro": "Esta rota aceita apenas idSistema PROCURACOES."}), 400
    if id_servico != PROCURACOES_ID_SERVICO_OBTER:
        return jsonify({"ok": False, "erro": "Esta rota aceita apenas idServico OBTERPROCURACAO41."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = consultar_procuracoes(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            outorgante=outorgante,
            outorgado=outorgado,
            id_sistema=id_sistema,
            id_servico=id_servico,
            versao_sistema=versao_sistema,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except PgdasdUpstreamError as exc:
        pedido_dados_enviado = (exc.body or {}).get("pedidoDados", {})
        return jsonify({
            "ok": False,
            "erro": str(exc),
            "status_serpro": exc.status,
            "endpoint_serpro": exc.url,
            "retorno_serpro": exc.resposta,
            "pedidoDados": pedido_dados_enviado,
        }), 502
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-pagtoweb/pagamentos/executar")
def executar_pagtoweb_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))
    pedido_dados = payload.get("pedidoDados") if isinstance(payload.get("pedidoDados"), dict) else {}
    endpoint = str(payload.get("endpoint") or "Consultar").strip()
    id_sistema = str(payload.get("idSistema") or pedido_dados.get("idSistema") or PAGTOWEB_ID_SISTEMA).strip()
    id_servico = str(payload.get("idServico") or pedido_dados.get("idServico") or "").strip()
    versao_sistema = str(payload.get("versaoSistema") or pedido_dados.get("versaoSistema") or PAGTOWEB_VERSAO).strip()
    dados = payload.get("dados")
    if dados in (None, ""):
        dados = pedido_dados.get("dados", "")

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not id_servico:
        return jsonify({"ok": False, "erro": "Informe idServico para executar o PAGTOWEB."}), 400
    if id_sistema != PAGTOWEB_ID_SISTEMA:
        return jsonify({"ok": False, "erro": "Esta rota aceita apenas idSistema PAGTOWEB."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = executar_pagtoweb(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            endpoint=endpoint,
            id_sistema=id_sistema,
            id_servico=id_servico,
            versao_sistema=versao_sistema,
            dados=dados,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except PgdasdUpstreamError as exc:
        pedido_dados_enviado = (exc.body or {}).get("pedidoDados", {})
        return jsonify({
            "ok": False,
            "erro": str(exc),
            "status_serpro": exc.status,
            "endpoint_serpro": exc.url,
            "retorno_serpro": exc.resposta,
            "pedidoDados": pedido_dados_enviado,
        }), 502
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-dctfweb/dctfweb/executar")
@app.post("/integra-dctfweb/executar")
def executar_dctfweb_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))
    pedido_dados = payload.get("pedidoDados") if isinstance(payload.get("pedidoDados"), dict) else {}
    endpoint = str(payload.get("endpoint") or "Consultar").strip()
    id_sistema = str(payload.get("idSistema") or pedido_dados.get("idSistema") or DCTFWEB_ID_SISTEMA).strip()
    id_servico = str(payload.get("idServico") or pedido_dados.get("idServico") or "").strip()
    versao_sistema = str(payload.get("versaoSistema") or pedido_dados.get("versaoSistema") or DCTFWEB_VERSAO).strip()
    dados = payload.get("dados")
    if dados in (None, ""):
        dados = pedido_dados.get("dados", "")

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not id_servico:
        return jsonify({"ok": False, "erro": "Informe idServico para executar o DCTFWEB."}), 400
    if id_sistema != DCTFWEB_ID_SISTEMA:
        return jsonify({"ok": False, "erro": "Esta rota aceita apenas idSistema DCTFWEB."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = executar_dctfweb(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            endpoint=endpoint,
            id_sistema=id_sistema,
            id_servico=id_servico,
            versao_sistema=versao_sistema,
            dados=dados,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except PgdasdUpstreamError as exc:
        pedido_dados_enviado = (exc.body or {}).get("pedidoDados", {})
        return jsonify({
            "ok": False,
            "erro": str(exc),
            "status_serpro": exc.status,
            "endpoint_serpro": exc.url,
            "retorno_serpro": exc.resposta,
            "pedidoDados": pedido_dados_enviado,
        }), 502
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-mit/mit/executar")
@app.post("/integra-mit/executar")
def executar_mit_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))
    pedido_dados = payload.get("pedidoDados") if isinstance(payload.get("pedidoDados"), dict) else {}
    endpoint = str(payload.get("endpoint") or "Consultar").strip()
    id_sistema = str(payload.get("idSistema") or pedido_dados.get("idSistema") or MIT_ID_SISTEMA).strip()
    id_servico = str(payload.get("idServico") or pedido_dados.get("idServico") or "").strip()
    versao_sistema = str(payload.get("versaoSistema") or pedido_dados.get("versaoSistema") or MIT_VERSAO).strip()
    dados = payload.get("dados")
    if dados in (None, ""):
        dados = pedido_dados.get("dados", "")

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not id_servico:
        return jsonify({"ok": False, "erro": "Informe idServico para executar o MIT."}), 400
    if id_sistema != MIT_ID_SISTEMA:
        return jsonify({"ok": False, "erro": "Esta rota aceita apenas idSistema MIT."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = executar_mit(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            endpoint=endpoint,
            id_sistema=id_sistema,
            id_servico=id_servico,
            versao_sistema=versao_sistema,
            dados=dados,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except PgdasdUpstreamError as exc:
        pedido_dados_enviado = (exc.body or {}).get("pedidoDados", {})
        return jsonify({
            "ok": False,
            "erro": str(exc),
            "status_serpro": exc.status,
            "endpoint_serpro": exc.url,
            "retorno_serpro": exc.resposta,
            "pedidoDados": pedido_dados_enviado,
        }), 502
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-eprocesso/eprocesso/executar")
@app.post("/integra-eprocesso/executar")
@app.post("/integra-eprocesso/eprocesso/consultar")
@app.post("/integra-eprocesso/processos/consultar")
@app.post("/integra-eprocesso/consultar")
def executar_eprocesso_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    contribuinte_cnpj = digits(payload.get("contribuinte_cnpj") or payload.get("cnpj"))
    pedido_dados = payload.get("pedidoDados") if isinstance(payload.get("pedidoDados"), dict) else {}
    endpoint = str(payload.get("endpoint") or "Consultar").strip()
    id_sistema = str(payload.get("idSistema") or pedido_dados.get("idSistema") or EPROCESSO_ID_SISTEMA).strip()
    id_servico = str(payload.get("idServico") or pedido_dados.get("idServico") or EPROCESSO_ID_SERVICO_CONSULTAR).strip()
    versao_sistema = str(payload.get("versaoSistema") or pedido_dados.get("versaoSistema") or EPROCESSO_VERSAO).strip()
    dados = payload.get("dados")
    if dados in (None, ""):
        dados = pedido_dados.get("dados", "")
    if dados in (None, ""):
        dados = {}

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if len(contribuinte_cnpj) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe o CPF/CNPJ do contribuinte com 11 ou 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not id_servico:
        return jsonify({"ok": False, "erro": "Informe idServico para executar o EPROCESSO."}), 400
    if id_sistema != EPROCESSO_ID_SISTEMA:
        return jsonify({"ok": False, "erro": "Esta rota aceita apenas idSistema EPROCESSO."}), 400

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = executar_eprocesso(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            cnpj_contribuinte=contribuinte_cnpj,
            endpoint=endpoint,
            id_sistema=id_sistema,
            id_servico=id_servico,
            versao_sistema=versao_sistema,
            dados=dados,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except PgdasdUpstreamError as exc:
        pedido_dados_enviado = (exc.body or {}).get("pedidoDados", {})
        return jsonify({
            "ok": False,
            "erro": str(exc),
            "status_serpro": exc.status,
            "endpoint_serpro": exc.url,
            "retorno_serpro": exc.resposta,
            "pedidoDados": pedido_dados_enviado,
        }), 502
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-monitoramento/eventos/executar")
@app.post("/integra-eventosatualizacao/executar")
def executar_eventos_atualizacao_route():
    payload = request.get_json(silent=True) or {}
    consumer_key, consumer_secret, cnpj_contador = get_common_credentials(payload)
    pedido_dados = payload.get("pedidoDados") if isinstance(payload.get("pedidoDados"), dict) else {}
    id_sistema = str(payload.get("idSistema") or pedido_dados.get("idSistema") or EVENTOS_ID_SISTEMA).strip()
    id_servico = str(payload.get("idServico") or pedido_dados.get("idServico") or "").strip()
    versao_sistema = str(payload.get("versaoSistema") or pedido_dados.get("versaoSistema") or EVENTOS_VERSAO).strip()
    dados = payload.get("dados")
    if dados in (None, ""):
        dados = pedido_dados.get("dados", "")

    contribuinte_obj = payload.get("contribuinte") if isinstance(payload.get("contribuinte"), dict) else {}
    contribuinte_tipo_raw = (
        payload.get("contribuinte_tipo")
        or payload.get("contribuinteTipo")
        or contribuinte_obj.get("tipo")
        or payload.get("tipo_contribuinte")
        or payload.get("tipoContribuinte")
        or 4
    )
    try:
        contribuinte_tipo = int(contribuinte_tipo_raw)
    except Exception:
        contribuinte_tipo = 4
    contribuinte_numero = str(
        payload.get("contribuinte_numero")
        or payload.get("contribuinteNumero")
        or contribuinte_obj.get("numero")
        or payload.get("contribuinte_cnpj")
        or payload.get("cnpj")
        or ""
    ).strip()
    numeros_lista = [digits(part) for part in contribuinte_numero.split(",") if digits(part)]

    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 digitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not id_servico:
        return jsonify({"ok": False, "erro": "Informe idServico para executar o EVENTOSATUALIZACAO."}), 400
    if id_sistema != EVENTOS_ID_SISTEMA:
        return jsonify({"ok": False, "erro": "Esta rota aceita apenas idSistema EVENTOSATUALIZACAO."}), 400
    if contribuinte_tipo not in (1, 2, 3, 4):
        return jsonify({"ok": False, "erro": "contribuinte_tipo invalido. Use 1, 2, 3 ou 4."}), 400

    solicitacao_ids = {"SOLICEVENTOSPF131", "SOLICEVENTOSPJ132"}
    obtencao_ids = {"OBTEREVENTOSPF133", "OBTEREVENTOSPJ134"}
    if contribuinte_tipo in (3, 4):
        if id_servico in solicitacao_ids:
            if not numeros_lista:
                return jsonify({"ok": False, "erro": "Para solicitar eventos em lote informe contribuinte_numero com lista CSV de documentos."}), 400
            max_len = 11 if contribuinte_tipo == 3 else 14
            if any(len(n) != max_len for n in numeros_lista):
                return jsonify({"ok": False, "erro": f"Todos os documentos da lista devem ter {max_len} digitos para contribuinte_tipo {contribuinte_tipo}."}), 400
            if len(numeros_lista) > 1000:
                return jsonify({"ok": False, "erro": "A lista de contribuintes suporta no maximo 1000 documentos."}), 400
            contribuinte_numero = ",".join(numeros_lista)
        elif id_servico in obtencao_ids:
            # Para OBTER, o campo numero deve estar vazio conforme documentação.
            contribuinte_numero = ""
    else:
        numero_unico = digits(contribuinte_numero)
        if len(numero_unico) not in (11, 14):
            return jsonify({"ok": False, "erro": "Informe contribuinte_numero valido para contribuinte_tipo 1/2."}), 400
        contribuinte_numero = numero_unico

    try:
        pem_path, key_path = load_cert_paths(payload)
    except Exception as exc:
        return jsonify({"ok": False, "erro": f"Certificado invalido: {exc}"}), 400

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultado = executar_eventos_atualizacao(
            session=session,
            cert=cert,
            access_token=auth["access_token"],
            jwt_token=auth["jwt_token"],
            cnpj_contador=cnpj_contador,
            contribuinte_numero=contribuinte_numero,
            contribuinte_tipo=contribuinte_tipo,
            id_sistema=id_sistema,
            id_servico=id_servico,
            versao_sistema=versao_sistema,
            dados=dados,
        )
        resultado["tokens"] = {"expires_in": auth.get("expires_in")}
        return jsonify(resultado)
    except requests.HTTPError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        return jsonify({"ok": False, "erro": detail}), 400
    except PgdasdUpstreamError as exc:
        pedido_dados_enviado = (exc.body or {}).get("pedidoDados", {})
        return jsonify({
            "ok": False,
            "erro": str(exc),
            "status_serpro": exc.status,
            "endpoint_serpro": exc.url,
            "retorno_serpro": exc.resposta,
            "pedidoDados": pedido_dados_enviado,
        }), 502
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500
    finally:
        for path in (pem_path, key_path):
            try:
                os.unlink(path)
            except Exception:
                pass


@app.post("/integra-cadastro/sem-certificado/buscar-nome")
@app.post("/integra-cadastro/sem-cert/buscar-nome")
@app.post("/integra-cadastro/buscar-nome")
@app.post("/cadastro/sem-certificado/buscar-nome")
@app.post("/cadastro/sem-cert/buscar-nome")
def buscar_nome_sem_certificado_route():
    payload = request.get_json(silent=True) or {}
    documento = digits(
        payload.get("documento")
        or payload.get("cnpj_cpf")
        or payload.get("cpf_cnpj")
        or payload.get("doc")
        or ""
    )
    if len(documento) not in (11, 14):
        return jsonify({"ok": False, "erro": "Informe CPF/CNPJ com 11 ou 14 digitos."}), 400

    lookup_url = str(payload.get("lookup_url") or SITCAD_LOOKUP_URL).strip()
    if not lookup_url:
        return jsonify({
            "ok": False,
            "erro": "Endpoint de busca não configurado. Defina SITCAD_LOOKUP_URL no backend.",
        }), 501

    method = str(payload.get("method") or SITCAD_LOOKUP_METHOD).strip().upper() or "POST"
    doc_field = str(payload.get("doc_field") or SITCAD_LOOKUP_DOC_FIELD).strip() or "documento"
    token_header = str(payload.get("token_header") or SITCAD_LOOKUP_TOKEN_HEADER).strip() or "X-API-Key"
    token_value = str(payload.get("token") or SITCAD_LOOKUP_TOKEN).strip()

    timeout_raw = payload.get("timeout", SITCAD_LOOKUP_TIMEOUT)
    try:
        timeout = max(5, int(str(timeout_raw).strip()))
    except Exception:
        timeout = SITCAD_LOOKUP_TIMEOUT

    try:
        session = requests.Session()
        resultado = consultar_nome_sem_certificado(
            session=session,
            documento=documento,
            lookup_url=lookup_url,
            method=method,
            doc_field=doc_field,
            timeout=timeout,
            token_header=token_header,
            token_value=token_value,
        )
        if resultado.get("ok"):
            return jsonify(resultado)

        status_http = int(resultado.get("status_http") or 404)
        if status_http < 400:
            status_http = 404
        return jsonify(resultado), status_http
    except requests.RequestException as exc:
        return jsonify({
            "ok": False,
            "erro": f"Falha ao consultar endpoint externo de cadastro: {exc}",
            "documento": documento,
        }), 502
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc), "documento": documento}), 500


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
            "/integra-cnd/certidoes/emitir",
            "/integra-cnd/certidoes/consultar",
            "/integra-cnd/certidoes/emitir-lote",
            "/integra-cnd/certidoes/exibir-lote",
            "/integra-caixapostal/caixa-entrada/consultar",
            "/integra-caixapostal/mensagens/consultar",
            "/integra-caixapostal/mensagens/detalhar",
            "/integra-caixapostal/mensagens/indicador",
            "/integra-sn/pgdasd/executar",
            "/integra-procuracoes/procuracoes/consultar",
            "/integra-pagtoweb/pagamentos/executar",
            "/integra-dctfweb/dctfweb/executar",
            "/integra-mit/mit/executar",
            "/integra-eprocesso/eprocesso/executar",
            "/integra-eprocesso/processos/consultar",
            "/integra-monitoramento/eventos/executar",
            "/integra-cadastro/sem-certificado/buscar-nome",
        ],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
