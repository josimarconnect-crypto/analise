import base64
import json
import os
import re
import tempfile
import time
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

app = Flask(__name__)
CORS(app)


def digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


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
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": 2},
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
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": 2},
        "pedidoDados": {
            "idSistema": PARCELAMENTO_ID_SISTEMA,
            "idServico": PARCELAMENTO_ID_SERVICO_EMITIR,
            "versaoSistema": PARCELAMENTO_VERSAO,
            "dados": json.dumps({"parcelaParaEmitir": int(parcela_aaaamm)}, ensure_ascii=False),
        },
    }


def body_apoiar(cnpj_contador: str, cnpj_contribuinte: str) -> Dict[str, Any]:
    return {
        "contratante": {"numero": cnpj_contador, "tipo": 2},
        "autorPedidoDados": {"numero": cnpj_contador, "tipo": 2},
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": 2},
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
        "contribuinte": {"numero": cnpj_contribuinte, "tipo": 2},
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


def normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\r", "\n", text)
    return text.strip()


def money_to_float(value: str) -> Optional[float]:
    try:
        return float(value.replace(".", "").replace(",", "."))
    except Exception:
        return None


def parse_sitfis_simplificado(pdf_b64: str) -> Dict[str, List[Dict[str, Any]]]:
    text = normalize_text(extract_pdf_text(pdf_b64))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    debitos: List[Dict[str, Any]] = []
    ausencia: List[Dict[str, Any]] = []

    for line in lines:
        low = line.lower()
        is_debito = any(k in low for k in ("débito", "debito", "débitos", "debitos"))
        is_ausencia = "ausência" in low or "ausencia" in low or "omiss" in low

        if not is_debito and not is_ausencia:
            continue

        valor_match = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2})", line)
        periodo_match = re.search(
            r"(?:per[ií]odo|pa|refer[eê]ncia|compet[eê]ncia)[:\s]+([0-9/\-]+)",
            line,
            re.IGNORECASE,
        )
        item = {
            "descricao": line,
            "periodo": periodo_match.group(1) if periodo_match else None,
        }

        if is_debito:
            item["valor"] = money_to_float(valor_match.group(1)) if valor_match else None
            debitos.append(item)
        if is_ausencia:
            ausencia.append(item)

    return {
        "debitos": debitos,
        "ausencia_declaracao": ausencia,
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
        return {"cnpj": cnpj_contribuinte, "ok": False, "debitos": [], "ausencia_declaracao": [], "erro": "Falha ao solicitar protocolo."}

    protocolo = extrair_protocolo(apoiar)
    if not protocolo:
        return {"cnpj": cnpj_contribuinte, "ok": False, "debitos": [], "ausencia_declaracao": [], "erro": "Protocolo não localizado."}

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
        return {"cnpj": cnpj_contribuinte, "ok": False, "debitos": [], "ausencia_declaracao": [], "erro": "Relatório sem PDF."}

    simplificado = parse_sitfis_simplificado(pdf_b64)
    return {
        "cnpj": cnpj_contribuinte,
        "ok": True,
        "debitos": simplificado["debitos"],
        "ausencia_declaracao": simplificado["ausencia_declaracao"],
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

    if isinstance(cnpjs, str):
        cnpjs = [x for x in re.split(r"[\s,;]+", cnpjs) if x]
    cnpjs = [digits(cnpj) for cnpj in cnpjs]
    cnpjs = [cnpj for cnpj in dict.fromkeys(cnpjs) if len(cnpj) == 14]

    if not user:
        return jsonify({"ok": False, "erro": "Informe o user."}), 400
    if len(cnpj_contador) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contador com 14 dígitos."}), 400
    if not consumer_key or not consumer_secret:
        return jsonify({"ok": False, "erro": "Informe consumer_key e consumer_secret."}), 400
    if not cnpjs:
        return jsonify({"ok": False, "erro": "Informe uma lista de CNPJs."}), 400

    try:
        pem_bytes = decode_cert(payload.get("pem_base64") or payload.get("pem"))
        key_bytes = decode_cert(payload.get("key_base64") or payload.get("key"))
    except Exception as exc:
        return jsonify({"ok": False, "user": user, "erro": f"Certificado inválido: {exc}"}), 400

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as pem_file, tempfile.NamedTemporaryFile(delete=False, suffix=".key") as key_file:
        pem_file.write(pem_bytes)
        key_file.write(key_bytes)
        pem_path = pem_file.name
        key_path = key_file.name

    try:
        session = requests.Session()
        cert = (pem_path, key_path)
        auth = authenticate(session, consumer_key, consumer_secret, cert)
        resultados = [
            consultar_um_cnpj(
                session=session,
                cert=cert,
                access_token=auth["access_token"],
                jwt_token=auth["jwt_token"],
                cnpj_contador=cnpj_contador,
                cnpj_contribuinte=cnpj,
                procurador_token=procurador_token,
            )
            for cnpj in cnpjs
        ]
        return jsonify({
            "ok": all(item.get("ok") for item in resultados),
            "user": user,
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "total_cnpjs": len(resultados),
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
    if len(contribuinte_cnpj) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contribuinte com 14 digitos."}), 400
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
    if len(contribuinte_cnpj) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contribuinte com 14 digitos."}), 400
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
    if len(contribuinte_cnpj) != 14:
        return jsonify({"ok": False, "erro": "Informe o CNPJ do contribuinte com 14 digitos."}), 400
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
        ],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
