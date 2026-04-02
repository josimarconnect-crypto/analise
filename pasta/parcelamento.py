# -*- coding: utf-8 -*-
"""
integra_parcelamento_api.py

Módulo secundário para ser importado no app principal (FastAPI/Render).

Funções:
- Recebe PFX/P12 em base64 OU PEM/KEY em base64
- Converte PFX para cert.pem / key.pem quando necessário
- Retorna cert/key em texto e em base64
- Autentica na SAPI (mTLS + Basic consumerKey:consumerSecret)
- Consulta parcelamentos (PARCSN / PEDIDOSPARC163)
- Emite DAS (PARCSN / GERARDAS161)
- Pode devolver PDF em base64
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
import shutil
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates


AUTH_URL = "https://autenticacao.sapi.serpro.gov.br/authenticate"
URL_CONSULTAR = "https://gateway.apiserpro.serpro.gov.br/integra-contador/v1/Consultar"
URL_EMITIR = "https://gateway.apiserpro.serpro.gov.br/integra-contador/v1/Emitir"

router = APIRouter(prefix="/integra-parcelamento", tags=["Integra Parcelamento"])


class CertificadoBase64In(BaseModel):
    pfx_base64: str = Field(default="", description="Arquivo .pfx/.p12 em base64")
    pfx_password: str = Field(default="", description="Senha do certificado PFX/P12")
    pem_base64: str = Field(default="", description="Certificado PEM em base64")
    key_base64: str = Field(default="", description="Chave privada PEM em base64")

    @model_validator(mode="after")
    def validar_origem_certificado(self):
        has_pfx = bool((self.pfx_base64 or "").strip())
        has_pem_pair = bool((self.pem_base64 or "").strip() and (self.key_base64 or "").strip())

        if not has_pfx and not has_pem_pair:
            raise ValueError("Informe pfx_base64 OU pem_base64 + key_base64.")

        if has_pfx and has_pem_pair:
            # permitido, mas priorizamos PEM/KEY quando vierem ambos
            return self

        if (self.pem_base64 or "").strip() and not (self.key_base64 or "").strip():
            raise ValueError("Ao informar pem_base64, informe também key_base64.")

        if (self.key_base64 or "").strip() and not (self.pem_base64 or "").strip():
            raise ValueError("Ao informar key_base64, informe também pem_base64.")

        return self


class AuthIn(CertificadoBase64In):
    consumer_key: str
    consumer_secret: str
    role_type: str = "TERCEIROS"


class ConsultarParcelamentoIn(AuthIn):
    contratante_cnpj: str
    autor_cnpj: str
    contribuinte_cnpj: str


class EmitirDasIn(AuthIn):
    contratante_cnpj: str
    autor_cnpj: str
    contribuinte_cnpj: str
    parcela_aaaamm: int
    return_pdf_base64: bool = True


@dataclass
class SerproTokens:
    access_token: str
    jwt_token: str
    expires_in: int


def only_digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def decode_b64_to_bytes(data_b64: str) -> bytes:
    try:
        return base64.b64decode(data_b64)
    except (ValueError, binascii.Error) as e:
        raise ValueError(f"Base64 inválido: {e}")


def _try_parse_json_string(s: Any) -> Any:
    if isinstance(s, str) and s.strip():
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return s
    return s


def pfx_bytes_to_pem_material(pfx_bytes: bytes, pfx_password: str) -> Dict[str, str]:
    key, cert, additional_certs = load_key_and_certificates(
        pfx_bytes,
        pfx_password.encode("utf-8") if pfx_password else None,
    )

    if not key or not cert:
        raise ValueError("Não foi possível extrair o certificado e a chave do PFX/P12.")

    cert_pem_bytes = cert.public_bytes(Encoding.PEM)
    key_pem_bytes = key.private_bytes(
        Encoding.PEM,
        PrivateFormat.TraditionalOpenSSL,
        NoEncryption(),
    )

    cadeia_pem_bytes = b""
    if additional_certs:
        for c in additional_certs:
            cadeia_pem_bytes += c.public_bytes(Encoding.PEM)

    return {
        "cert_pem_text": cert_pem_bytes.decode("utf-8"),
        "key_pem_text": key_pem_bytes.decode("utf-8"),
        "cert_pem_base64": base64.b64encode(cert_pem_bytes).decode("ascii"),
        "key_pem_base64": base64.b64encode(key_pem_bytes).decode("ascii"),
        "chain_pem_text": cadeia_pem_bytes.decode("utf-8") if cadeia_pem_bytes else "",
        "chain_pem_base64": base64.b64encode(cadeia_pem_bytes).decode("ascii") if cadeia_pem_bytes else "",
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "serial_number": str(cert.serial_number),
        "has_chain": bool(additional_certs),
        "chain_count": len(additional_certs or []),
    }


def pem_key_b64_to_material(pem_base64: str, key_base64: str) -> Dict[str, str]:
    cert_pem_bytes = decode_b64_to_bytes(pem_base64)
    key_pem_bytes = decode_b64_to_bytes(key_base64)

    cert_text = cert_pem_bytes.decode("utf-8", errors="strict")
    key_text = key_pem_bytes.decode("utf-8", errors="strict")

    if "BEGIN CERTIFICATE" not in cert_text:
        raise ValueError("pem_base64 não contém um CERTIFICATE PEM válido.")

    if "BEGIN" not in key_text or "PRIVATE KEY" not in key_text:
        raise ValueError("key_base64 não contém uma PRIVATE KEY PEM válida.")

    return {
        "cert_pem_text": cert_text,
        "key_pem_text": key_text,
        "cert_pem_base64": base64.b64encode(cert_pem_bytes).decode("ascii"),
        "key_pem_base64": base64.b64encode(key_pem_bytes).decode("ascii"),
        "chain_pem_text": "",
        "chain_pem_base64": "",
        "subject": "",
        "issuer": "",
        "serial_number": "",
        "has_chain": False,
        "chain_count": 0,
    }


def write_temp_pem_files(cert_pem_text: str, key_pem_text: str) -> Tuple[str, str, str]:
    tmpdir = tempfile.mkdtemp(prefix="serpro_mtls_")
    cert_path = os.path.join(tmpdir, "cert.pem")
    key_path = os.path.join(tmpdir, "key.pem")

    with open(cert_path, "w", encoding="utf-8") as f:
        f.write(cert_pem_text)

    with open(key_path, "w", encoding="utf-8") as f:
        f.write(key_pem_text)

    return cert_path, key_path, tmpdir


def cleanup_tmpdir(tmpdir: Optional[str]) -> None:
    if tmpdir and os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)


def build_pem_material(
    pfx_base64: str = "",
    pfx_password: str = "",
    pem_base64: str = "",
    key_base64: str = "",
) -> Dict[str, str]:
    if (pem_base64 or "").strip() and (key_base64 or "").strip():
        return pem_key_b64_to_material(pem_base64, key_base64)

    if (pfx_base64 or "").strip():
        pfx_bytes = decode_b64_to_bytes(pfx_base64)
        return pfx_bytes_to_pem_material(pfx_bytes, pfx_password)

    raise ValueError("Informe pfx_base64 OU pem_base64 + key_base64.")


def get_tokens_from_cert_base64(
    consumer_key: str,
    consumer_secret: str,
    *,
    pfx_base64: str = "",
    pfx_password: str = "",
    pem_base64: str = "",
    key_base64: str = "",
    role_type: str = "TERCEIROS",
    timeout: int = 60,
) -> Tuple[SerproTokens, Dict[str, str]]:
    if not consumer_key or not consumer_secret:
        raise ValueError("Preencha consumer_key e consumer_secret.")

    pem_material = build_pem_material(
        pfx_base64=pfx_base64,
        pfx_password=pfx_password,
        pem_base64=pem_base64,
        key_base64=key_base64,
    )

    cert_path = None
    key_path = None
    tmpdir = None

    try:
        cert_path, key_path, tmpdir = write_temp_pem_files(
            pem_material["cert_pem_text"],
            pem_material["key_pem_text"],
        )

        basic = base64.b64encode(
            f"{consumer_key}:{consumer_secret}".encode("utf-8")
        ).decode("ascii")

        headers = {
            "Authorization": f"Basic {basic}",
            "Role-Type": role_type,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {"grant_type": "client_credentials"}

        r = requests.post(
            AUTH_URL,
            headers=headers,
            data=data,
            cert=(cert_path, key_path),
            timeout=timeout,
        )

        if not r.ok:
            raise RuntimeError(f"Falha autenticação ({r.status_code}): {r.text}")

        j = r.json()
        if "access_token" not in j or "jwt_token" not in j:
            raise RuntimeError(f"Resposta inesperada da autenticação: {j}")

        tokens = SerproTokens(
            access_token=j["access_token"],
            jwt_token=j["jwt_token"],
            expires_in=int(j.get("expires_in", 0)),
        )
        return tokens, pem_material

    finally:
        cleanup_tmpdir(tmpdir)


def get_tokens_from_pfx_base64(
    consumer_key: str,
    consumer_secret: str,
    pfx_base64: str,
    pfx_password: str,
    role_type: str = "TERCEIROS",
    timeout: int = 60,
) -> Tuple[SerproTokens, Dict[str, str]]:
    return get_tokens_from_cert_base64(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        pfx_base64=pfx_base64,
        pfx_password=pfx_password,
        role_type=role_type,
        timeout=timeout,
    )


def build_body(
    contratante_numero: str,
    autor_numero: str,
    contribuinte_numero: str,
    id_sistema: str,
    id_servico: str,
    versao_sistema: str,
    dados_obj: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if dados_obj is None:
        dados = ""
    else:
        dados = json.dumps(dados_obj, ensure_ascii=False)

    return {
        "contratante": {"numero": only_digits(contratante_numero), "tipo": 2},
        "autorPedidoDados": {"numero": only_digits(autor_numero), "tipo": 2},
        "contribuinte": {"numero": only_digits(contribuinte_numero), "tipo": 2},
        "pedidoDados": {
            "idSistema": id_sistema,
            "idServico": id_servico,
            "versaoSistema": versao_sistema,
            "dados": dados,
        },
    }


def call_integra(
    url: str,
    tokens: SerproTokens,
    body: Dict[str, Any],
    timeout: int = 60,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {tokens.access_token}",
        "jwt_token": tokens.jwt_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    r = requests.post(url, headers=headers, json=body, timeout=timeout)

    if not r.ok:
        raise RuntimeError(f"Erro Integra ({r.status_code}): {r.text}")

    return r.json()


def consultar_pedidos_parcelamento_parcsn(
    tokens: SerproTokens,
    contratante_cnpj: str,
    autor_cnpj: str,
    contribuinte_cnpj: str,
) -> Dict[str, Any]:
    body = build_body(
        contratante_numero=contratante_cnpj,
        autor_numero=autor_cnpj,
        contribuinte_numero=contribuinte_cnpj,
        id_sistema="PARCSN",
        id_servico="PEDIDOSPARC163",
        versao_sistema="1.0",
        dados_obj=None,
    )

    resp = call_integra(URL_CONSULTAR, tokens, body)
    if "dados" in resp:
        resp["dados_parsed"] = _try_parse_json_string(resp.get("dados"))
    return resp


def emitir_das_parcsn(
    tokens: SerproTokens,
    contratante_cnpj: str,
    autor_cnpj: str,
    contribuinte_cnpj: str,
    parcela_aaaamm: int,
) -> Dict[str, Any]:
    body = build_body(
        contratante_numero=contratante_cnpj,
        autor_numero=autor_cnpj,
        contribuinte_numero=contribuinte_cnpj,
        id_sistema="PARCSN",
        id_servico="GERARDAS161",
        versao_sistema="1.0",
        dados_obj={"parcelaParaEmitir": int(parcela_aaaamm)},
    )

    resp = call_integra(URL_EMITIR, tokens, body)

    dados_parsed = _try_parse_json_string(resp.get("dados"))
    resp["dados_parsed"] = dados_parsed

    pdf_b64 = None
    if isinstance(dados_parsed, dict):
        pdf_b64 = dados_parsed.get("docArrecadacaoPdfB64")

    if pdf_b64:
        resp["pdf_base64"] = pdf_b64

    return resp


@router.get("/health")
def health():
    return {"ok": True, "modulo": "integra_parcelamento_api"}


@router.post("/certificado/converter")
def converter_certificado(payload: CertificadoBase64In):
    try:
        pem_material = build_pem_material(
            pfx_base64=payload.pfx_base64,
            pfx_password=payload.pfx_password,
            pem_base64=payload.pem_base64,
            key_base64=payload.key_base64,
        )
        return {
            "ok": True,
            "certificado": pem_material,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth")
def auth(payload: AuthIn):
    try:
        tokens, pem_material = get_tokens_from_cert_base64(
            consumer_key=payload.consumer_key,
            consumer_secret=payload.consumer_secret,
            pfx_base64=payload.pfx_base64,
            pfx_password=payload.pfx_password,
            pem_base64=payload.pem_base64,
            key_base64=payload.key_base64,
            role_type=payload.role_type,
        )

        return {
            "ok": True,
            "tokens": {
                "access_token": tokens.access_token,
                "jwt_token": tokens.jwt_token,
                "expires_in": tokens.expires_in,
            },
            "certificado": pem_material,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/parcelamentos/consultar")
def consultar_parcelamentos(payload: ConsultarParcelamentoIn):
    try:
        tokens, pem_material = get_tokens_from_cert_base64(
            consumer_key=payload.consumer_key,
            consumer_secret=payload.consumer_secret,
            pfx_base64=payload.pfx_base64,
            pfx_password=payload.pfx_password,
            pem_base64=payload.pem_base64,
            key_base64=payload.key_base64,
            role_type=payload.role_type,
        )

        resp = consultar_pedidos_parcelamento_parcsn(
            tokens=tokens,
            contratante_cnpj=payload.contratante_cnpj,
            autor_cnpj=payload.autor_cnpj,
            contribuinte_cnpj=payload.contribuinte_cnpj,
        )

        return {
            "ok": True,
            "tokens": {
                "expires_in": tokens.expires_in,
            },
            "certificado": pem_material,
            "resposta": resp,
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/parcelamentos/emitir")
def emitir_parcelamento(payload: EmitirDasIn):
    try:
        tokens, pem_material = get_tokens_from_cert_base64(
            consumer_key=payload.consumer_key,
            consumer_secret=payload.consumer_secret,
            pfx_base64=payload.pfx_base64,
            pfx_password=payload.pfx_password,
            pem_base64=payload.pem_base64,
            key_base64=payload.key_base64,
            role_type=payload.role_type,
        )

        resp = emitir_das_parcsn(
            tokens=tokens,
            contratante_cnpj=payload.contratante_cnpj,
            autor_cnpj=payload.autor_cnpj,
            contribuinte_cnpj=payload.contribuinte_cnpj,
            parcela_aaaamm=payload.parcela_aaaamm,
        )

        retorno = {
            "ok": True,
            "tokens": {
                "expires_in": tokens.expires_in,
            },
            "certificado": pem_material,
            "resposta": resp,
        }

        if not payload.return_pdf_base64:
            retorno["resposta"].pop("pdf_base64", None)

        return retorno

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
