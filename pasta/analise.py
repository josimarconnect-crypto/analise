# -*- coding: utf-8 -*-
"""
analise.py — FastAPI (Render)
✅ Rotas:
  GET /health
  GET /empresas?user=...
  GET /debitos?user=...&codi=...&incluir_ano_anterior=1
  GET /extrato-produto?user=...&codi=...&url_extrato=...&chave=...

✅ Login: DET → Portal usando mTLS (cert pem/key do Supabase)

✅ /extrato-produto (NOVA SAÍDA):
  - Abre extrato.jsp, extrai TOKEN + USUARIO e CHAVE (se houver)
  - Abre internamento (capa_internamentos)
  - Extrai APENAS a ÚLTIMA tabela de "Cálculo Itens" que contém "Val. a Recolher."
  - Retorna somente:
      result.calculo_itens (lista)
      result.totais (qtd/colunas)
      result.diagnostico (como achou a tabela)
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
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, Tag

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn


# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════
SUPABASE_URL  = os.getenv("SUPABASE_URL", "https://hysrxadnigzqadnlkynq.supabase.co").strip()
SUPABASE_KEY  = os.getenv(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh5c3J4YWRuaWd6cWFkbmxreW5xIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM3MTQwODAsImV4cCI6MjA1OTI5MDA4MH0.RLcu44IvY4X8PLK5BOa_FL5WQ0vJA3p0t80YsGQjTrA",
).strip()

TABELA_CERTS  = os.getenv("TABELA_CERTS", "certifica_dfe").strip()
DEBUG_ERRORS  = os.getenv("DEBUG_ERRORS", "1") == "1"
LOG_FILE      = os.getenv("LOG_FILE", "/tmp/analise.log" if os.getenv("RENDER") else "analise.log")

URL_DET_HOME            = "https://detsec.sefin.ro.gov.br/certificados"
URL_ENTRAR              = "https://detsec.sefin.ro.gov.br/entrar"
URL_REDIRECT_PORTAL     = "https://detsec.sefin.ro.gov.br/contribuinte/notificacoes/redirect_portal"
URL_PORTAL_HOME_DEFAULT = "https://portalcontribuinte.sefin.ro.gov.br/app/home/?exibir_modal=true"
URL_CONSULTA_DEBITOS    = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/"
URL_CONSULTA_DEBITOS_LISTA = "https://portalcontribuinte.sefin.ro.gov.br/app/consultadebitos/lista.jsp"
BASE_INTERNAMENTO       = "https://internamentonotas.sefin.ro.gov.br"


# ══════════════════════════════════════════════════════════════════
# LOG
# ══════════════════════════════════════════════════════════════════
logger = logging.getLogger("analise")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════
# SUPABASE
# ══════════════════════════════════════════════════════════════════
def _require_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY or SUPABASE_KEY == "CHANGE_ME":
        raise HTTPException(500, "Configure SUPABASE_URL e SUPABASE_KEY no ENV do Render.")


def supabase_headers() -> Dict[str, str]:
    _require_supabase()
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def carregar_certificados(user_filter: str) -> List[Dict[str, Any]]:
    url = f"{SUPABASE_URL}/rest/v1/{TABELA_CERTS}"
    params: Dict[str, str] = {
        "select": 'id,pem,key,empresa,codi,user,vencimento,"cnpj/cpf"',
        "user":   f"eq.{user_filter}",
        "order":  "id.desc",
        "limit":  "100",
    }
    r = requests.get(url, headers=supabase_headers(), params=params, timeout=30)
    if r.status_code >= 300:
        raise HTTPException(400, f"Supabase REST falhou: {r.text}")
    return r.json() or []


def selecionar_cert_por_codi(certs: List[Dict[str, Any]], codi: str) -> Dict[str, Any]:
    codi = (codi or "").strip()
    if not certs:
        raise HTTPException(404, "Nenhum certificado encontrado para este user.")
    if not codi:
        return certs[0]
    for c in certs:
        if str(c.get("codi") or "").strip() == codi:
            return c
    raise HTTPException(404, f"Não encontrei certificado com CODI={codi} para este user.")


# ══════════════════════════════════════════════════════════════════
# CERT TEMP + SESSION
# ══════════════════════════════════════════════════════════════════
def criar_arquivos_cert_temp(cert_row: Dict[str, Any]) -> Tuple[str, str]:
    pem_b64 = cert_row.get("pem") or ""
    key_b64 = cert_row.get("key") or ""
    if not pem_b64 or not key_b64:
        raise HTTPException(400, "Certificado inválido: pem/key vazios no Supabase.")
    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_file  = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    cert_file.write(base64.b64decode(pem_b64)); cert_file.close()
    key_file.write(base64.b64decode(key_b64));  key_file.close()
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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


# ══════════════════════════════════════════════════════════════════
# DET / PORTAL
# ══════════════════════════════════════════════════════════════════
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
    return "/certificado/acessos" in (r_ent.url or "")


def _extrair_form_logintoken(html: str) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if "portalcontribuinte.sefin.ro.gov.br" in action or "LoginToken" in action:
            if not action.startswith("http"):
                action = requests.compat.urljoin(URL_REDIRECT_PORTAL, action)
            data: Dict[str, str] = {
                inp.get("name"): inp.get("value", "") or ""
                for inp in form.find_all("input")
                if inp.get("name")
            }
            return action, data
    return None, None


def _extrair_redirect_do_logintoken(html: str) -> Optional[str]:
    m = re.search(
        r"location\s*=\s*['\"]"
        r"(https://portalcontribuinte\.sefin\.ro\.gov\.br[^'\"]+)['\"]", html
    )
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
            return r_home.status_code == 200 and "portalcontribuinte.sefin.ro.gov.br" in (r_home.url or "")
    r_portal = sess.get(URL_PORTAL_HOME_DEFAULT, timeout=30, allow_redirects=True)
    return r_portal.status_code == 200 and "LoginToken" not in (r_portal.url or "")


# ══════════════════════════════════════════════════════════════════
# DÉBITOS
# ══════════════════════════════════════════════════════════════════
def _listar_inscricoes_estaduais(html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    sel = soup.find("select", {"name": "inscricaoEstadual"})
    if not sel:
        return []
    seen: set = set()
    out = []
    for opt in sel.find_all("option"):
        v = (opt.get("value") or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def obter_debitos_inscricao_estadual(html_deb: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_deb, "lxml")
    tabela_alvo = None
    for tab in soup.find_all("table"):
        ths = tab.find_all("th")
        if ths and "DÉBITOS NA INSCRIÇÃO ESTADUAL" in ths[0].get_text(" ", strip=True).upper():
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
            return href if href.startswith("http") else requests.compat.urljoin(URL_CONSULTA_DEBITOS_LISTA, href)

        debitos.append({
            "nr_lancamento":   txt(2),
            "parcela":         txt(3),
            "referencia":      txt(4),
            "complemento":     txt(5),
            "receita":         txt(6),
            "situacao":        txt(7),
            "data_vencimento": txt(8),
            "valor_lancamento": txt(9),
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
            d["ie"]  = ie_val
        return debs, None

    return [], last_err or f"Falha ao consultar lista (ano {ano})"


# ══════════════════════════════════════════════════════════════════
# EXTRATO → TOKEN / USUARIO / CHAVE
# ══════════════════════════════════════════════════════════════════
def _extrair_token_e_usuario(html_extrato: str) -> Tuple[Optional[str], Optional[str]]:
    m1 = re.search(r"var\s+TOKEN\s*=\s*'([^']+)'", html_extrato)
    token = m1.group(1).strip() if m1 else None
    m2 = re.search(r"Ol[áa]\s*<strong>\s*([0-9]{11})\s*-", html_extrato, flags=re.I)
    usuario = m2.group(1).strip() if m2 else None
    return token, usuario


def _extrair_chaves_nfe(html_extrato: str) -> List[str]:
    chaves = re.findall(r"\b\d{44}\b", html_extrato)
    seen: set = set()
    out = []
    for c in chaves:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _url_capa_internamento(usuario: str, chave: str, token: Optional[str]) -> str:
    base = f"{BASE_INTERNAMENTO}/capa_internamentos/{usuario}/{chave}"
    return base + "?token=" + requests.utils.quote(token, safe="") if token else base


# ══════════════════════════════════════════════════════════════════
# PARSER (SOMENTE ÚLTIMA TABELA DE CÁLCULO)
# ══════════════════════════════════════════════════════════════════
def _norm_text(text: str) -> str:
    """Lowercase + sem acentos + espaço único."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    for src, dst in [
        ("á","a"),("à","a"),("â","a"),("ã","a"),
        ("é","e"),("ê","e"),("è","e"),
        ("í","i"),("î","i"),
        ("ó","o"),("ô","o"),("õ","o"),
        ("ú","u"),("û","u"),
        ("ç","c"),
    ]:
        t = t.replace(src, dst)
    return t


def _table_headers(table: Tag) -> List[str]:
    return [th.get_text(" ", strip=True) for th in table.find_all("th")]


_SCORE_CALC_KW: List[Tuple[str, int]] = [
    ("val. mercadoria",        40),
    ("val. debito mercadoria", 40),
    ("val. a recolher",        60),
    ("base calc. final",       30),
    ("frete fob",              25),
    ("aliq. interna",          20),
    ("% reducao",              20),
    ("val. credito nfe",       20),
    ("val. cred. comple",      20),
    ("aliq. orig. nfe",        15),
    ("aliq. orig. cte",        15),
    ("base calc. mercadoria",  15),
    ("base calc. frete",       10),
    ("produto",                 5),
    ("item",                    5),
]


def _score_calc_headers(ths: List[str]) -> int:
    joined = _norm_text(" | ".join(ths))
    score = sum(pts for kw, pts in _SCORE_CALC_KW if kw in joined)
    if len(ths) == 17:
        score += 50
    return score


def _is_valid_calc_table(t: Optional[Tag]) -> Tuple[bool, Dict[str, Any]]:
    if not t:
        return False, {"ok": False, "motivo": "tabela_none"}

    ths = _table_headers(t)
    if not ths:
        return False, {"ok": False, "motivo": "sem_th"}

    joined = _norm_text(" | ".join(ths))
    has_recolher = ("val. a recolher" in joined) or ("valor a recolher" in joined)
    score = _score_calc_headers(ths)

    ok = False
    if len(ths) == 17 and has_recolher:
        ok = True
    elif score >= 120 and has_recolher:
        ok = True

    return ok, {
        "ok": ok,
        "n_headers": len(ths),
        "has_recolher": has_recolher,
        "score": score,
        "headers_preview": ths[:6],
    }


_CALC_COL_ALIASES: Dict[str, List[str]] = {
    "item":                   ["item"],
    "produto":                ["produto"],
    "val_mercadoria":         ["val. mercadoria", "valor mercadoria", "mercadoria"],
    "perc_reducao":           ["% reducao base calc", "reducao base calc", "% reducao"],
    "bc_mercadoria":          ["base calc. mercadoria", "base calc mercadoria"],
    "frete_fob":              ["frete fob (cte)", "frete fob", "frete"],
    "bc_frete":               ["base calc. frete-fob", "base calc frete fob", "base calc frete"],
    "bc_final":               ["base calc. final", "base calc final"],
    "aliq_interna":           ["% aliq. interna", "aliq. interna", "aliq interna"],
    "aliq_orig_nfe":          ["%aliq. orig. nfe", "aliq. orig. nfe", "aliq orig nfe"],
    "aliq_orig_cte":          ["%aliq. orig. cte", "aliq. orig. cte", "aliq orig cte"],
    "val_deb_mercadoria":     ["val. debito mercadoria", "val. débito mercadoria", "val. debito merc"],
    "val_deb_frete":          ["val. debito frete-fob", "val. débito frete-fob", "val. debito frete"],
    "val_cred_nfe":           ["val. credito nfe", "val. crédito nfe", "val. cred. nfe"],
    "val_cred_cte":           ["val. credito cte", "val. crédito cte", "val. cred. cte"],
    "val_cred_complementar":  ["val. créd. comple", "val. cred. comple", "val. credito compl", "cred. comple"],
    "valor_a_recolher":       ["val. a recolher", "val. a recolher.", "valor a recolher"],
}


def _build_calc_idx(header_cells: List[str]) -> Dict[str, Optional[int]]:
    hn = [_norm_text(h) for h in header_cells]
    result: Dict[str, Optional[int]] = {}
    for field, aliases in _CALC_COL_ALIASES.items():
        found = None
        for alias in aliases:
            alias_n = _norm_text(alias)
            for i, h in enumerate(hn):
                if alias_n in h:
                    found = i
                    break
            if found is not None:
                break
        result[field] = found
    return result


def _parse_calculo_itens(table: Tag) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    all_tr = table.find_all("tr")
    if not all_tr:
        return [], {"erro": "tabela_vazia"}

    header_cells: List[str] = []
    header_tr: Optional[Tag] = None
    for tr in all_tr[:12]:
        ths = tr.find_all("th")
        if len(ths) >= 10:
            header_cells = [th.get_text(" ", strip=True) for th in ths]
            header_tr = tr
            break
    if not header_cells and all_tr:
        cells = all_tr[0].find_all(["td", "th"])
        header_cells = [c.get_text(" ", strip=True) for c in cells]
        header_tr = all_tr[0]

    col_idx = _build_calc_idx(header_cells)
    unmapped = [f for f, i in col_idx.items() if i is None]
    if unmapped:
        logger.warning("Cálculo Itens — colunas não mapeadas: %s", unmapped)

    def get(cols: List[str], field: str) -> Optional[str]:
        i = col_idx.get(field)
        if i is None or i < 0 or i >= len(cols):
            return None
        v = cols[i].strip()
        return v or None

    out: List[Dict[str, Any]] = []
    for tr in all_tr:
        if tr is header_tr:
            continue
        tds = tr.find_all("td")
        if not tds:
            continue
        cols = [td.get_text(" ", strip=True) for td in tds]
        if len(cols) < 5:
            continue

        item_val = get(cols, "item") or cols[0].strip()
        if not item_val or not re.fullmatch(r"\d+", item_val):
            continue

        row: Dict[str, Any] = {f: get(cols, f) for f in _CALC_COL_ALIASES}
        row["item"] = item_val
        out.append(row)

    diag = {
        "headers_found": header_cells,
        "n_headers": len(header_cells),
        "col_map": col_idx,
        "colunas_nao_mapeadas": unmapped,
        "linhas_extraidas": len(out),
    }
    return out, diag


def _localizar_ultima_tabela_calculo(soup: BeautifulSoup) -> Tuple[Optional[Tag], Dict[str, Any]]:
    """
    Encontra TODAS as tabelas candidatas de cálculo e retorna a ÚLTIMA no DOM
    que seja válida e contenha "Val. a Recolher".
    """
    tables = soup.find_all("table")
    valid: List[Tag] = []
    checks: List[Dict[str, Any]] = []

    for i, t in enumerate(tables):
        ok, info = _is_valid_calc_table(t)
        info2 = {"i": i, **info}
        checks.append(info2)
        if ok:
            valid.append(t)

    if not valid:
        return None, {
            "estrategia": "ultima_validada",
            "tables_found": len(tables),
            "validas": 0,
            "checks": checks,
            "motivo": "nenhuma_tabela_calc_valida",
        }

    # ✅ pega a última válida no DOM
    chosen = valid[-1]
    # acha o índice dela para diagnóstico
    chosen_index = None
    for i, t in enumerate(tables):
        if t == chosen:
            chosen_index = i
            break

    ths = _table_headers(chosen)
    return chosen, {
        "estrategia": "ultima_validada",
        "tables_found": len(tables),
        "validas": len(valid),
        "chosen_index": chosen_index,
        "chosen_n_headers": len(ths),
        "chosen_has_recolher": ("val. a recolher" in _norm_text(" | ".join(ths))),
        "chosen_score": _score_calc_headers(ths),
        "checks": checks,
    }


def parse_internamento(html_intern: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html_intern, "lxml")

    tab_calc, diag_find = _localizar_ultima_tabela_calculo(soup)

    calc_itens: List[Dict[str, Any]] = []
    diag_parse: Dict[str, Any] = {}
    if tab_calc:
        calc_itens, diag_parse = _parse_calculo_itens(tab_calc)

    return {
        "calculo_itens": calc_itens,
        "totais": {
            "qtd_itens_calculo": len(calc_itens),
            "n_cols_calculo": diag_parse.get("n_headers", 0),
            "itens_sem_valor_a_recolher": sum(1 for x in calc_itens if not x.get("valor_a_recolher")),
        },
        "diagnostico": {
            "find_calculo": diag_find,
            "parse_calculo": diag_parse,
        },
    }


# ══════════════════════════════════════════════════════════════════
# APP FASTAPI
# ══════════════════════════════════════════════════════════════════
app = FastAPI(title="analise — Débitos + Extrato por Produto (somente última tabela de cálculo)")

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
            traceback.format_exc(),
        )
    except Exception:
        pass

    base = {"ok": False, "error": str(exc)}
    if DEBUG_ERRORS:
        base.update({
            "type":      exc.__class__.__name__,
            "path":      str(request.url.path),
            "query":     dict(request.query_params),
            "traceback": traceback.format_exc(),
            "log_file":  LOG_FILE,
        })
    return JSONResponse(status_code=500, content=base)


@app.get("/")
def root():
    return {
        "ok":      True,
        "service": "analise",
        "date_utc": _now_iso(),
        "routes":  ["/health", "/empresas", "/debitos", "/extrato-produto"],
    }


@app.get("/health")
def health():
    return {"ok": True, "date_utc": _now_iso()}


@app.get("/empresas")
def empresas(user: str = Query(...)):
    if not user or "@" not in user:
        raise HTTPException(400, "user inválido.")
    certs = carregar_certificados(user)
    seen: set = set()
    out = []
    for c in certs:
        codi = str(c.get("codi") or "").strip()
        if not codi or codi in seen:
            continue
        seen.add(codi)
        out.append({
            "codi":       codi,
            "empresa":    (c.get("empresa") or "").strip(),
            "cnpj":       (c.get("cnpj/cpf") or "").strip(),
            "vencimento": (c.get("vencimento") or ""),
        })
    return {"ok": True, "user": user, "total": len(out), "empresas": out}


@app.get("/debitos")
def route_debitos(
    user:                str = Query(...),
    codi:                str = Query(...),
    incluir_ano_anterior: int = Query(1),
):
    if not user or "@" not in user:
        raise HTTPException(400, "user inválido.")
    if not codi:
        raise HTTPException(400, "codi obrigatório.")

    certs = carregar_certificados(user)
    cert  = selecionar_cert_por_codi(certs, codi)

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(401, "Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(401, "Falha ao abrir Portal.")

        ano_atual = date.today().year
        anos = [ano_atual] + ([ano_atual - 1] if incluir_ano_anterior == 1 else [])

        all_debs: List[Dict[str, str]] = []
        for a in anos:
            debs, _ = consultar_debitos_ano(sess, a)
            all_debs.extend(debs or [])

        return {
            "ok":      True,
            "user":    user,
            "codi":    str(cert.get("codi") or ""),
            "empresa": (cert.get("empresa") or ""),
            "debitos": all_debs,
        }

    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


@app.get("/extrato-produto")
def extrato_produto(
    user:        str = Query(...),
    codi:        str = Query(...),
    url_extrato: str = Query(...),
    chave:       str = Query(""),
):
    if not user or "@" not in user:
        raise HTTPException(400, "user inválido.")
    if not codi:
        raise HTTPException(400, "codi obrigatório.")
    if not url_extrato.startswith("http"):
        raise HTTPException(400, "url_extrato deve começar com http/https.")

    certs = carregar_certificados(user)
    cert  = selecionar_cert_por_codi(certs, codi)

    cert_path = key_path = None
    try:
        cert_path, key_path = criar_arquivos_cert_temp(cert)
        sess = criar_sessao(cert_path, key_path)

        if not abrir_acesso_digital_e_entrar(sess):
            raise HTTPException(401, "Falha ao entrar no DET (mTLS).")
        if not ir_para_portal(sess):
            raise HTTPException(401, "Falha ao abrir Portal (LoginToken/home).")

        url_extrato_clean = url_extrato.strip().replace("%22", "").split("#", 1)[0]
        r = sess.get(url_extrato_clean, timeout=45, allow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(400, f"Erro ao abrir extrato.jsp: HTTP {r.status_code}")

        token, usuario = _extrair_token_e_usuario(r.text)
        if not usuario:
            raise HTTPException(400, "Não consegui extrair USUARIO/CPF do extrato.jsp.")

        chaves     = _extrair_chaves_nfe(r.text)
        chave_alvo = chave.strip() or (chaves[0] if chaves else "")
        if not chave_alvo:
            raise HTTPException(400, "Não encontrei chave NFe (44 dígitos) no extrato.jsp.")

        url_capa = _url_capa_internamento(usuario, chave_alvo, token)
        r2 = sess.get(url_capa, timeout=60, allow_redirects=True)
        if r2.status_code != 200:
            raise HTTPException(400, f"Falha ao abrir internamento: HTTP {r2.status_code}")

        parsed = parse_internamento(r2.text)

        return {
            "ok":      True,
            "user":    user,
            "codi":    str(cert.get("codi") or ""),
            "empresa": cert.get("empresa") or "",
            "result": {
                "ok":          True,
                "final_url":   r2.url,
                "token_found": bool(token),
                "usuario":     usuario,
                "chave":       chave_alvo,
                **parsed,  # ✅ agora vem SOMENTE calculo_itens + totais + diagnostico
            },
        }

    finally:
        for p in (cert_path, key_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    uvicorn.run(
        "analise:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        reload=False,
    )
