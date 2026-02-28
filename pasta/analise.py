# -*- coding: utf-8 -*-
"""
extrato_api.py (FastAPI) — Render
✅ FIX: agora escolhe a tabela "Itens da nota" de verdade:
   - exige header com NCM
   - exige Item + Descrição + (CFOP ou O/CST/CSOSN)
✅ Extrai também Produto SEFIN (ex: 8231) quando existir
✅ Merge principal por Item; fallback por Produto SEFIN

Start Command:
  PYTHONPATH=. uvicorn extrato_api:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import os
import re
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
# /debitos (igual antes, mantido)
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

        link_extrato = tr.find("a", href=re.compile(r"extrato\.jsp"))

        def norm_url(href: Optional[str]) -> str:
            if not href:
                return ""
            href = href.replace("%22", "").strip('"')
            if href.startswith("http"):
                return href
            return requests.compat.urljoin(URL_CONSULTA_DEBITOS_LISTA, href)

        debitos.append({
            "referencia": txt(4),
            "receita": txt(6),
            "situacao": txt(7),
            "data_vencimento": txt(8),
            "valor_atualizado": txt(10),
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
        payload = {"inscricaoEstadual": ie_val, "ano": str(ano), "tipoDevedor": tipo_devedor, "Submit": "Consultar Débitos"}
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
def debitos(user: str = Query(...), codi: str = Query(...), incluir_ano_anterior: int = Query(1)):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    if not codi:
        raise HTTPException(status_code=400, detail="codi obrigatório.")

    certs = carregar_certificados_validos(user)
    cert = selecionar_cert_por_codi(certs, codi)

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(status_code=401, detail="Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(status_code=401, detail="Falha ao abrir Portal.")

        ano_atual = date.today().year
        anos = [ano_atual] + ([ano_atual - 1] if incluir_ano_anterior == 1 else [])

        all_debs: List[Dict[str, str]] = []
        for a in anos:
            debs, _ = consultar_debitos_ano(sess, a)
            all_debs.extend(debs or [])

        return {"ok": True, "user": user, "codi": str(cert.get("codi") or ""), "empresa": cert.get("empresa"), "debitos": all_debs}

    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# =========================================================
# ✅ EXTRATO — FIX TABELA "ITENS DA NOTA"
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


def _to_number_ptbr(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    t = re.sub(r"[^\d,.\-]", "", s)
    if not t:
        return None
    t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except Exception:
        return None


def _table_headers(table) -> List[str]:
    ths = table.find_all("th")
    headers = [_norm(th.get_text(" ", strip=True)) for th in ths]
    return [h for h in headers if h]


def _table_rows(table) -> List[List[str]]:
    tbody = table.find("tbody")
    trs = tbody.find_all("tr") if tbody else table.find_all("tr")
    out = []
    for tr in trs:
        tds = tr.find_all("td")
        if not tds:
            continue
        cols = [td.get_text(" ", strip=True).strip() for td in tds]
        if any(c for c in cols):
            out.append(cols)
    return out


def _score_item_table(headers: List[str]) -> int:
    H = " | ".join(headers)
    score = 0

    # ✅ FORÇA NCM
    if "ncm" in H:
        score += 60

    # precisa ter item + descrição
    if "item" in H:
        score += 10
    if "descricao" in H:
        score += 10

    # pelo menos um desses
    if "cfop" in H:
        score += 10
    if "o/cst" in H or "cst" in H or "csosn" in H:
        score += 8

    # normalmente a "Itens da nota" tem muitos "valor ..."
    score += min(H.count("valor"), 10)

    # penaliza tabelas de "dados da nota" / "demonstrativo"
    if "dados da nota fiscal" in H or "demonstrativo" in H:
        score -= 25

    return score


def _score_calc_table(headers: List[str]) -> int:
    H = " | ".join(headers)
    score = 0
    keys = [
        ("valor a recolher", 25),
        ("recolher", 15),
        ("produto", 10),
        ("bc final", 10),
        ("aliq", 8),
        ("credito", 8),
        ("debito", 8),
        ("item", 6),
    ]
    for k, w in keys:
        if k in H:
            score += w
    return score


def _pick_best_tables(soup: BeautifulSoup) -> Tuple[Optional[Any], Optional[Any], Dict[str, Any]]:
    tables = soup.find_all("table")
    diag = {
        "tables_found": len(tables),
        "tables_headers": [],
        "best_item_score": 0,
        "best_calc_score": 0,
    }

    best_item = None
    best_calc = None
    best_item_score = -10**9
    best_calc_score = -10**9

    for tb in tables:
        headers = _table_headers(tb)
        if headers:
            diag["tables_headers"].append(headers[:80])

        si = _score_item_table(headers)
        sc = _score_calc_table(headers)

        if si > best_item_score:
            best_item_score = si
            best_item = tb
        if sc > best_calc_score:
            best_calc_score = sc
            best_calc = tb

    diag["best_item_score"] = best_item_score
    diag["best_calc_score"] = best_calc_score
    return best_item, best_calc, diag


def _map_row(headers: List[str], cols: List[str]) -> Dict[str, str]:
    if len(cols) < len(headers):
        cols = cols + [""] * (len(headers) - len(cols))
    if len(cols) > len(headers):
        cols = cols[: len(headers)]
    return {headers[i]: (cols[i] or "").strip() for i in range(len(headers))}


def _pick(raw: Dict[str, str], contains: str) -> Optional[str]:
    for k, v in raw.items():
        if contains in k and v:
            return v
    return None


def parse_itens_da_nota(table) -> List[Dict[str, Any]]:
    headers = _table_headers(table)
    rows = _table_rows(table)
    out: List[Dict[str, Any]] = []

    for cols in rows:
        raw = _map_row(headers, cols)

        item = _pick(raw, "item")
        desc = _pick(raw, "descricao")
        ncm = _pick(raw, "ncm")
        cfop = _pick(raw, "cfop")
        cest = _pick(raw, "cest")

        # Produto SEFIN pode aparecer como "produto sefin" ou algo parecido
        prod_sefin = _pick(raw, "produto sefin")
        if not prod_sefin:
            # às vezes é duas colunas separadas, ou só "produto" no final
            # cuidado: a tabela de itens também tem "valor produto", então preferimos campo que tenha "sefin"
            pass

        if not (item or desc or ncm):
            continue

        out.append({
            "item": item,
            "descricao": desc,
            "ncm": ncm,
            "cfop": cfop,
            "cest": cest,
            "produto_sefin": prod_sefin,  # pode vir None; mas NCM vem ✅
            "campos_raw": raw,
        })

    # filtra só linhas que parecem item mesmo (item numérico)
    filtered: List[Dict[str, Any]] = []
    for it in out:
        v = (it.get("item") or "").strip()
        if re.fullmatch(r"\d+", v):
            filtered.append(it)

    return filtered or out


def parse_calculo_itens(table) -> List[Dict[str, Any]]:
    headers = _table_headers(table)
    rows = _table_rows(table)
    out: List[Dict[str, Any]] = []

    def idx(needle: str) -> Optional[int]:
        for i, h in enumerate(headers):
            if needle in h:
                return i
        return None

    i_item = idx("item")
    i_prod = idx("produto")
    i_recolher = idx("recolher")

    for cols in rows:
        if len(cols) < 2:
            continue
        raw = _map_row(headers, cols)

        item = None
        prod = None
        recolher_txt = None

        if i_item is not None and i_item < len(cols):
            item = cols[i_item].strip()
        else:
            item = _pick(raw, "item")

        if i_prod is not None and i_prod < len(cols):
            prod = cols[i_prod].strip()
        else:
            prod = _pick(raw, "produto")

        if i_recolher is not None and i_recolher < len(cols):
            recolher_txt = cols[i_recolher].strip()
        else:
            # tenta achar um campo com "recolher"
            recolher_txt = _pick(raw, "recolher") or (cols[-1].strip() if cols else "")

        if not (item or prod):
            continue

        out.append({
            "item": item,
            "produto_sefin": prod,
            "valor_a_recolher": _to_number_ptbr(recolher_txt or ""),
            "campos_raw": raw,
        })

    return out


def merge_itens_calc(itens: List[Dict[str, Any]], calc: List[Dict[str, Any]]) -> Dict[str, Any]:
    # 1) por item
    calc_by_item: Dict[str, Dict[str, Any]] = {}
    for c in calc:
        k = str(c.get("item") or "").strip()
        if k and k not in calc_by_item:
            calc_by_item[k] = c

    # 2) fallback por produto_sefin (8231)
    calc_by_prod: Dict[str, Dict[str, Any]] = {}
    for c in calc:
        p = str(c.get("produto_sefin") or "").strip()
        if p and p not in calc_by_prod:
            calc_by_prod[p] = c

    merged = []
    total_recolher = 0.0

    for it in itens:
        k = str(it.get("item") or "").strip()
        p = str(it.get("produto_sefin") or "").strip()

        c = calc_by_item.get(k) or (calc_by_prod.get(p) if p else None)

        merged.append({
            **it,
            "calculo": c or None,
        })

        v = (c or {}).get("valor_a_recolher")
        if isinstance(v, (int, float)):
            total_recolher += float(v)

    return {
        "itens_mesclados": merged,
        "totais": {
            "qtd_itens": len(merged),
            "itens_sem_ncm": sum(1 for x in merged if not x.get("ncm")),
            "total_valor_a_recolher": round(total_recolher, 2),
        }
    }


def extrair_extrato_mesclado(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    tb_itens, tb_calc, diag = _pick_best_tables(soup)

    itens = parse_itens_da_nota(tb_itens) if tb_itens else []
    calc = parse_calculo_itens(tb_calc) if tb_calc else []
    merge = merge_itens_calc(itens, calc)

    return {
        "ok": True,
        "itens_da_nota": itens,
        "calculo_itens": calc,
        "merge": merge,
        "diagnostico": diag,
    }


def baixar_extrato(sess: requests.Session, url_extrato: str) -> str:
    r = sess.get(url_extrato, timeout=30, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} ao abrir extrato")
    return r.text


@app.get("/extrato")
def route_extrato(user: str = Query(...), codi: str = Query(...), url_extrato: str = Query(...)):
    if not user or "@" not in user:
        raise HTTPException(status_code=400, detail="user inválido.")
    if not codi:
        raise HTTPException(status_code=400, detail="codi obrigatório.")
    if not url_extrato.startswith("http"):
        raise HTTPException(status_code=400, detail="url_extrato deve começar com http/https.")

    certs = carregar_certificados_validos(user)
    cert = selecionar_cert_por_codi(certs, codi)

    empresa = (cert.get("empresa") or "").strip()
    codi_sel = str(cert.get("codi") or "").strip()

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(status_code=401, detail="Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(status_code=401, detail="Falha ao abrir Portal (LoginToken/home).")

        html = baixar_extrato(sess, url_extrato)
        data = extrair_extrato_mesclado(html)

        q = (data.get("merge") or {}).get("totais", {}).get("qtd_itens", 0)
        sem_ncm = (data.get("merge") or {}).get("totais", {}).get("itens_sem_ncm", 0)
        logger.info("EXTRATO_DONE | user=%s | codi=%s | itens=%s | sem_ncm=%s", user, codi_sel, q, sem_ncm)

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
