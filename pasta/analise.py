# -*- coding: utf-8 -*-
"""
extrato_api.py (FastAPI) — Render
- /empresas  -> lista empresas (certificados) do Supabase por user
- /debitos   -> loga DET/Portal via mTLS e retorna débitos (com url_extrato/url_dare)
- /extrato   -> abre extrato.jsp e retorna JSON por produto (com NCM)

Requisitos:
  pip install fastapi uvicorn requests beautifulsoup4 lxml

Start Command (Render):
  PYTHONPATH=. uvicorn extrato_api:app --host 0.0.0.0 --port $PORT

ENV (Render):
  SUPABASE_URL
  SUPABASE_KEY
  TABELA_CERTS=certifica_dfe (opcional)
  DEBUG_ERRORS=1 (opcional)
"""

import os
import re
import json
import base64
import tempfile
import traceback
import logging
from logging.handlers import RotatingFileHandler
from datetime import date, datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import requests
from bs4 import BeautifulSoup

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn


# =========================================================
# CONFIG
# =========================================================
SUPABASE_URL = "https://hysrxadnigzqadnlkynq.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA"
TABELA_CERTS = os.getenv("TABELA_CERTS", "certifica_dfe").strip()

DEBUG_ERRORS = os.getenv("DEBUG_ERRORS", "1") == "1"

LOG_FILE = os.getenv(
    "LOG_FILE",
    "/tmp/extrato_api.log" if "RENDER" in os.getenv("RENDER", "") else "extrato_api.log"
)

# URLs DET/PORTAL (RO)
URL_DET_HOME = "https://detsec.sefin.ro.gov.br/certificados"
URL_ENTRAR = "https://detsec.sefin.ro.gov.br/entrar"
URL_REDIRECT_PORTAL = "https://detsec.sefin.ro.gov.br/contribuinte/notificacoes/redirect_portal"
URL_PORTAL_HOME_DEFAULT = "https://portalcontribuinte.sefin.ro.gov.br/app/home/?exibir_modal=true"

URL_CONSULTA_DEBITOS = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/"
URL_CONSULTA_DEBITOS_LISTA = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/lista.jsp"


# =========================================================
# LOG
# =========================================================
logger = logging.getLogger("extrato_api")
logger.setLevel(logging.INFO)

if not logger.handlers:
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_exc(prefix: str, exc: Exception):
    try:
        logger.error("%s | %s\n%s", prefix, str(exc), traceback.format_exc())
    except Exception:
        pass


# =========================================================
# FASTAPI
# =========================================================
app = FastAPI(title="Extrato NF-e por Produto (Portal RO) — API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    try:
        logger.error(
            "UNHANDLED | path=%s | query=%s | err=%s\n%s",
            str(request.url.path),
            dict(request.query_params),
            str(exc),
            traceback.format_exc()
        )
    except Exception:
        pass

    if not DEBUG_ERRORS:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": str(exc),
            "type": exc.__class__.__name__,
            "path": str(request.url.path),
            "query": dict(request.query_params),
            "traceback": traceback.format_exc(),
            "log_file": LOG_FILE,
        },
    )


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "extrato_api",
        "date_utc": _now_iso(),
        "routes": ["/health", "/empresas", "/debitos", "/extrato"],
        "log_file": LOG_FILE,
        "debug_errors": DEBUG_ERRORS,
        "tabela_certs": TABELA_CERTS,
    }


@app.get("/health")
def health():
    return {"ok": True, "date_utc": _now_iso()}


# =========================================================
# SUPABASE CERTS
# =========================================================
def _require_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=400, detail="Configure SUPABASE_URL e SUPABASE_KEY nas ENV do Render.")


def supabase_headers() -> Dict[str, str]:
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def carregar_certificados_validos(user_filter: str) -> List[Dict[str, Any]]:
    _require_supabase()
    url = f"{SUPABASE_URL}/rest/v1/{TABELA_CERTS}"
    params: Dict[str, str] = {
        "select": 'id,pem,key,empresa,codi,user,vencimento,"cnpj/cpf"',
        "user": f"eq.{user_filter}",
        "order": "id.desc",
    }
    r = requests.get(url, headers=supabase_headers(), params=params, timeout=30)
    if r.status_code >= 300:
        raise HTTPException(status_code=400, detail=f"Supabase REST falhou: {r.text}")
    return r.json() or []


def selecionar_cert_por_codi(certs: List[Dict[str, Any]], codi: str) -> Dict[str, Any]:
    codi = (codi or "").strip()
    if not codi:
        return certs[0]
    for c in certs:
        if str(c.get("codi") or "").strip() == codi:
            return c
    raise HTTPException(status_code=404, detail=f"Não encontrei certificado com CODI={codi} para este user.")


@app.get("/empresas")
def empresas(user: str = Query(...)):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    certs = carregar_certificados_validos(user)
    out = []
    for c in certs:
        out.append({
            "codi": str(c.get("codi") or "").strip(),
            "empresa": (c.get("empresa") or "").strip(),
            "cnpj": (c.get("cnpj/cpf") or "").strip(),
            "vencimento": (c.get("vencimento") or ""),
        })
    # remove duplicados por CODI (fica o mais recente)
    seen = set()
    uniq = []
    for it in out:
        if it["codi"] in seen:
            continue
        seen.add(it["codi"])
        uniq.append(it)
    return {"ok": True, "user": user, "total": len(uniq), "empresas": uniq}


# =========================================================
# CERT TEMP + SESSION
# =========================================================
def criar_arquivos_cert_temp(cert_row: Dict[str, Any]) -> Tuple[str, str]:
    pem_b64 = cert_row.get("pem") or ""
    key_b64 = cert_row.get("key") or ""
    if not pem_b64 or not key_b64:
        raise HTTPException(status_code=400, detail="Certificado inválido: pem/key vazios no Supabase.")

    pem_bytes = base64.b64decode(pem_b64)
    key_bytes = base64.b64decode(key_b64)

    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    cert_file.write(pem_bytes)
    cert_file.close()
    key_file.write(key_bytes)
    key_file.close()
    return cert_file.name, key_file.name


def criar_sessao(cert_path: str, key_path: str) -> requests.Session:
    s = requests.Session()
    s.cert = (cert_path, key_path)
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    })
    return s


# =========================================================
# DET / PORTAL (login + home)
# =========================================================
def abrir_acesso_digital_e_entrar(sess: requests.Session) -> bool:
    r = sess.get(URL_DET_HOME, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        return False

    soup = BeautifulSoup(r.text, "lxml")
    form = soup.find("form")
    if not form:
        return False

    action = form.get("action") or URL_ENTRAR
    if not action.startswith("http"):
        action = requests.compat.urljoin(URL_DET_HOME, action)

    r_ent = sess.get(action, timeout=30, allow_redirects=True)
    if r_ent.status_code != 200:
        return False

    return ("/certificado/acessos" in (r_ent.url or ""))


def _extrair_form_logintoken(html: str) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "portalcontribuinte.sefin.ro.gov.br" in action or "LoginToken" in action:
            if not action.startswith("http"):
                action = requests.compat.urljoin(URL_REDIRECT_PORTAL, action)
            data: Dict[str, str] = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if not name:
                    continue
                data[name] = inp.get("value", "") or ""
            return action, data
    return None, None


def _extrair_redirect_do_logintoken(html: str) -> Optional[str]:
    m = re.search(r"location\s*=\s*['\"](https://portalcontribuinte\.sefin\.ro\.gov\.br[^'\"]+)['\"]", html)
    if m:
        return m.group(1)
    m = re.search(r"location\.href\s*=\s*['\"](/app/home[^'\"]*)['\"]", html)
    if m:
        return "https://portalcontribuinte.sefin.ro.gov.br" + m.group(1)
    return None


def ir_para_portal(sess: requests.Session) -> bool:
    r_red = sess.get(URL_REDIRECT_PORTAL, timeout=30, allow_redirects=True)
    if r_red.status_code != 200:
        return False

    action_form, data_form = _extrair_form_logintoken(r_red.text)
    if action_form:
        r_login = sess.post(action_form, data=data_form, timeout=30, allow_redirects=True)
        if r_login.status_code == 200 and "LoginToken" not in (r_login.url or ""):
            return True
        if r_login.status_code == 200 and "LoginToken" in (r_login.url or ""):
            next_url = _extrair_redirect_do_logintoken(r_login.text) or URL_PORTAL_HOME_DEFAULT
            r_home = sess.get(next_url, timeout=30, allow_redirects=True)
            return (r_home.status_code == 200 and "portalcontribuinte.sefin.ro.gov.br" in (r_home.url or ""))

    r_portal = sess.get(URL_PORTAL_HOME_DEFAULT, timeout=30, allow_redirects=True)
    return (r_portal.status_code == 200 and "LoginToken" not in (r_portal.url or ""))


# =========================================================
# DÉBITOS (lista + urls)
# =========================================================
def _listar_inscricoes_estaduais(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    sel_ie = soup.find("select", {"name": "inscricaoEstadual"})
    if not sel_ie:
        return []
    vals = []
    for opt in sel_ie.find_all("option"):
        v = (opt.get("value") or "").strip()
        if v:
            vals.append(v)
    out, seen = [], set()
    for v in vals:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def obter_debitos_inscricao_estadual(html_deb: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_deb, "lxml")
    tabela_alvo = None
    for tab in soup.find_all("table"):
        ths = tab.find_all("th")
        if not ths:
            continue
        if "DÉBITOS NA INSCRIÇÃO ESTADUAL" in ths[0].get_text(" ", strip=True).upper():
            tabela_alvo = tab
            break
    if not tabela_alvo:
        return []

    linhas = tabela_alvo.find_all("tr")
    if len(linhas) <= 2:
        return []

    debitos: List[Dict[str, str]] = []
    for tr in linhas[2:]:
        tds = tr.find_all("td")
        if len(tds) < 11:
            continue

        def txt(i: int) -> str:
            return tds[i].get_text(" ", strip=True) if i < len(tds) else ""

        link_dare = tr.find("a", href=re.compile(r"dare\.sefin\.ro\.gov\.br/adm"))
        link_extrato = tr.find("a", href=re.compile(r"extrato\.jsp"))

        def norm_url(href: Optional[str]) -> str:
            if not href:
                return ""
            href = href.replace("%22", "").strip('"')
            if href.startswith("http"):
                return href
            return requests.compat.urljoin(URL_CONSULTA_DEBITOS_LISTA, href)

        debitos.append({
            "dare": txt(0),
            "extrato": txt(1),
            "nr_lancamento": txt(2),
            "parcela": txt(3),
            "referencia": txt(4),
            "complemento": txt(5),
            "receita": txt(6),
            "situacao": txt(7),
            "data_vencimento": txt(8),
            "valor_lancamento": txt(9),
            "valor_atualizado": txt(10),
            "url_dare": norm_url(link_dare.get("href") if link_dare else ""),
            "url_extrato": norm_url(link_extrato.get("href") if link_extrato else ""),
        })

    return debitos


def consultar_debitos_ano(sess: requests.Session, ano: int) -> Tuple[List[Dict[str, str]], Optional[str]]:
    r = sess.get(URL_CONSULTA_DEBITOS, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        return [], f"Erro HTTP {r.status_code} ao abrir Consulta de Débitos"

    soup = BeautifulSoup(r.text, "lxml")
    input_tipo = soup.find("input", {"name": "tipoDevedor"})
    tipo_devedor = input_tipo.get("value", "1") if input_tipo else "1"

    inscricoes = _listar_inscricoes_estaduais(r.text)
    if not inscricoes:
        return [], "Nenhuma inscrição estadual disponível (select vazio)"

    last_err = None
    for ie_val in inscricoes:
        payload = {
            "inscricaoEstadual": ie_val,
            "ano": str(ano),
            "tipoDevedor": tipo_devedor,
            "Submit": "Consultar Débitos",
        }
        r2 = sess.post(URL_CONSULTA_DEBITOS_LISTA, data=payload, timeout=30, allow_redirects=True)
        if r2.status_code != 200:
            last_err = f"Erro HTTP {r2.status_code} lista (ano {ano}) IE={ie_val}"
            continue

        debs = obter_debitos_inscricao_estadual(r2.text)
        for d in debs:
            d["ano"] = str(ano)
            d["ie"] = ie_val
        return debs, None

    return [], last_err or f"Falha ao consultar lista (ano {ano})"


@app.get("/debitos")
def debitos(
    user: str = Query(...),
    codi: str = Query(...),
    incluir_ano_anterior: int = Query(1, description="1=ano atual+anterior; 0=só ano atual"),
):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    if not codi:
        raise HTTPException(status_code=400, detail="codi obrigatório.")

    certs = carregar_certificados_validos(user)
    if not certs:
        raise HTTPException(status_code=404, detail="Nenhum certificado encontrado para este user.")
    cert = selecionar_cert_por_codi(certs, codi)

    empresa = (cert.get("empresa") or "").strip()
    codi_sel = str(cert.get("codi") or "").strip()

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        logger.info("DEBITOS_START | user=%s | codi=%s | empresa=%s", user, codi_sel, empresa)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(status_code=401, detail="Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(status_code=401, detail="Falha ao abrir Portal (LoginToken/home).")

        ano_atual = date.today().year
        anos = [ano_atual] + ([ano_atual - 1] if incluir_ano_anterior == 1 else [])

        all_debs: List[Dict[str, str]] = []
        erros: List[str] = []

        for a in anos:
            debs, err = consultar_debitos_ano(sess, a)
            if err:
                erros.append(f"{a}: {err}")
            if debs:
                all_debs.extend(debs)

        # ordena por vencimento texto (mantém como vem)
        logger.info("DEBITOS_DONE | user=%s | codi=%s | debitos=%s | erros=%s", user, codi_sel, len(all_debs), len(erros))

        return {
            "ok": True,
            "user": user,
            "codi": codi_sel,
            "empresa": empresa,
            "anos": anos,
            "qtd": len(all_debs),
            "erros": erros,
            "debitos": all_debs,
        }

    except HTTPException:
        raise
    except Exception as e:
        _log_exc(f"DEBITOS_FAIL | user={user} codi={codi}", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# =========================================================
# PARSER EXTRATO (PRODUTOS + NCM + IMPOSTOS)
# =========================================================
def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = (
        s.replace("á", "a").replace("à", "a").replace("ã", "a").replace("â", "a")
         .replace("é", "e").replace("ê", "e")
         .replace("í", "i")
         .replace("ó", "o").replace("ô", "o").replace("õ", "o")
         .replace("ú", "u")
         .replace("ç", "c")
    )
    return s


def _parse_num_br(s: str) -> Optional[float]:
    if not s:
        return None
    t = re.sub(r"[^\d,.\-]", "", (s or "").strip())
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except Exception:
        return None


def _table_headers_and_rows(table) -> Tuple[List[str], List[List[str]]]:
    ths = table.find_all("th")
    headers = [_norm(th.get_text(" ", strip=True)) for th in ths]
    headers = [h for h in headers if h]

    rows: List[List[str]] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        cols = [td.get_text(" ", strip=True) for td in tds]
        if any(c.strip() for c in cols):
            rows.append(cols)

    return headers, rows


def _score(headers: List[str], kind: str) -> int:
    H = " | ".join(headers)
    score = 0
    if kind == "prod":
        keys = [
            ("produto", 6), ("descricao", 4), ("item", 2),
            ("codigo", 4), ("cod", 2),
            ("ncm", 10),
            ("cfop", 5),
            ("qtd", 3), ("quant", 3),
            ("un", 2), ("unid", 2),
            ("valor", 2), ("unit", 2), ("total", 2),
        ]
    else:
        keys = [
            ("icms", 8), ("base", 6), ("aliquota", 6),
            ("multa", 3), ("juros", 3),
            ("principal", 3), ("atualizado", 3),
            ("total", 2), ("tribut", 2),
        ]
    for k, w in keys:
        if k in H:
            score += w
    return score


def _map_prod_row(headers: List[str], cols: List[str]) -> Dict[str, Any]:
    if len(cols) < len(headers):
        cols = cols + [""] * (len(headers) - len(cols))
    if len(cols) > len(headers):
        cols = cols[: len(headers)]
    raw = {headers[i]: (cols[i] or "").strip() for i in range(len(headers))}

    def pick_contains(*needles: str) -> Optional[str]:
        for n in needles:
            for k, v in raw.items():
                if n in k and v:
                    return v
        return None

    codigo = pick_contains("codigo", "cod")
    desc = pick_contains("descricao", "produto")
    ncm = pick_contains("ncm")  # ✅ chave obrigatória no retorno (mesmo que None)
    cfop = pick_contains("cfop")
    und = pick_contains("unid", "un")
    qtd = pick_contains("qtd", "quant")
    v_unit = pick_contains("unit")

    v_total = None
    for k, v in raw.items():
        kn = _norm(k)
        if ("valor" in kn and "total" in kn) or kn in ("total", "v total", "valor total"):
            if v:
                v_total = v
                break

    return {
        "codigo_produto": codigo,
        "descricao": desc,
        "ncm": ncm or None,
        "cfop": cfop,
        "unidade": und,
        "quantidade": _parse_num_br(qtd) if qtd else None,
        "valor_unitario": _parse_num_br(v_unit) if v_unit else None,
        "valor_total": _parse_num_br(v_total) if v_total else None,
        "campos_raw": raw,
    }


def _map_generic_rows(headers: List[str], rows: List[List[str]]) -> List[Dict[str, str]]:
    out = []
    for cols in rows:
        if len(cols) < len(headers):
            cols = cols + [""] * (len(headers) - len(cols))
        if len(cols) > len(headers):
            cols = cols[: len(headers)]
        out.append({headers[i]: (cols[i] or "").strip() for i in range(len(headers))})
    return out


def extrair_extrato_nfe_json(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")

    best_prod_score = 0
    best_imp_score = 0
    best_prod_headers: List[str] = []
    best_imp_headers: List[str] = []
    best_prod_rows: List[List[str]] = []
    best_imp_rows: List[List[str]] = []

    for tb in tables:
        headers, rows = _table_headers_and_rows(tb)
        if not headers or not rows:
            continue
        sp = _score(headers, "prod")
        si = _score(headers, "imp")

        if sp > best_prod_score:
            best_prod_score = sp
            best_prod_headers = headers
            best_prod_rows = rows

        if si > best_imp_score:
            best_imp_score = si
            best_imp_headers = headers
            best_imp_rows = rows

    produtos: List[Dict[str, Any]] = []
    impostos: List[Dict[str, str]] = []
    totais: Dict[str, Any] = {}

    if best_prod_score >= 10:
        for cols in best_prod_rows:
            prod = _map_prod_row(best_prod_headers, cols)
            if prod.get("codigo_produto") or prod.get("descricao") or prod.get("ncm"):
                produtos.append(prod)

        soma_vt = 0.0
        count_vt = 0
        for p in produtos:
            vt = p.get("valor_total")
            if isinstance(vt, (int, float)):
                soma_vt += float(vt)
                count_vt += 1
        totais["produtos_total_valor_total_somado"] = round(soma_vt, 2) if count_vt else None
        totais["produtos_qtd_com_valor_total"] = count_vt
        totais["produtos_sem_ncm"] = sum(1 for p in produtos if not p.get("ncm"))

    if best_imp_score >= 10:
        impostos = _map_generic_rows(best_imp_headers, best_imp_rows)

    return {
        "ok": True,
        "produtos": produtos,
        "impostos": impostos,
        "totais": totais,
        "meta": {
            "tabela_produtos_score": best_prod_score,
            "tabela_impostos_score": best_imp_score,
            "produtos_headers": best_prod_headers[:50],
            "impostos_headers": best_imp_headers[:50],
        },
    }


def baixar_extrato(sess: requests.Session, url_extrato: str) -> str:
    r = sess.get(url_extrato, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} ao abrir extrato")
    return r.text


# =========================================================
# /extrato
# =========================================================
@app.get("/extrato")
def route_extrato(
    user: str = Query(...),
    codi: str = Query(...),
    url_extrato: str = Query(...),
):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    if not codi:
        raise HTTPException(status_code=400, detail="codi obrigatório.")
    if not url_extrato.startswith("http"):
        raise HTTPException(status_code=400, detail="url_extrato deve começar com http/https.")

    certs = carregar_certificados_validos(user)
    if not certs:
        raise HTTPException(status_code=404, detail="Nenhuma empresa/certificado encontrado para este user.")
    cert = selecionar_cert_por_codi(certs, codi)

    empresa = (cert.get("empresa") or "").strip()
    codi_sel = str(cert.get("codi") or "").strip()

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        logger.info("EXTRATO_START | user=%s | codi=%s | empresa=%s", user, codi_sel, empresa)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(status_code=401, detail="Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(status_code=401, detail="Falha ao abrir Portal (LoginToken/home).")

        html = baixar_extrato(sess, url_extrato)
        data = extrair_extrato_nfe_json(html)

        logger.info("EXTRATO_DONE | user=%s | codi=%s | produtos=%s", user, codi_sel, len(data.get("produtos") or []))

        return {"ok": True, "user": user, "codi": codi_sel, "empresa": empresa, "result": data}

    except HTTPException:
        raise
    except Exception as e:
        _log_exc(f"EXTRATO_FAIL | user={user} codi={codi}", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


if __name__ == "__main__":
    uvicorn.run("extrato_api:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)
