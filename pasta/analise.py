# -*- coding: utf-8 -*-
"""
analise.py — FastAPI (Render)
✅ Rotas:
  GET /health
  GET /empresas?user=...
  GET /debitos?user=...&codi=...&incluir_ano_anterior=1
  GET /extrato-produto?user=...&codi=...&url_extrato=...&chave=...

✅ Login: DET → Portal usando mTLS (cert pem/key do Supabase)

✅ /extrato-produto:
  - Abre extrato.jsp, extrai TOKEN + USUARIO e CHAVE (se houver)
  - Abre internamento (capa_internamentos)
  - Extrai 3 tabelas (100% dinâmico):
      • "Itens lançamentos" (table#itens_lancamentos)  -> JSON separado
      • "Itens da Nota" (score headers)                -> JSON separado
      • "Cálculo Itens" (4 estratégias em cascata)     -> JSON separado
  - Tenta mesclar no backend (por ITEM) em merged_by_item
  - Retorna também as tabelas separadas para o HTML unir do jeito que quiser
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
# ⚠️ ideal: mover para ENV no Render
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
# PARSER DINÂMICO DE INTERNAMENTO
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
    """Retorna textos de todos os <th> da tabela."""
    return [th.get_text(" ", strip=True) for th in table.find_all("th")]


def _table_data_rows(table: Tag) -> List[List[str]]:
    """Retorna todas as linhas com <td> como lista de strings."""
    rows = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if tds:
            rows.append([td.get_text(" ", strip=True) for td in tds])
    return rows


# ──────────────────────────────────────────────────────────────────
# NOVO: parse da tabela "itens_lancamentos" (primeira tabela)
# ──────────────────────────────────────────────────────────────────
def _find_col_idx(norm_headers: List[str], *aliases: str) -> Optional[int]:
    """Acha o primeiro índice que contenha qualquer alias (normalizado)."""
    aliases_n = [_norm_text(a) for a in aliases]
    for i, h in enumerate(norm_headers):
        if any(a in h for a in aliases_n):
            return i
    return None


def _parse_itens_lancamentos(soup: BeautifulSoup) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Tabela: <table id="itens_lancamentos">
    Cabeçalho típico:
      Tipo | Produto | Descrição | Valor Item | Valor Débito | Valor Crédito | Valor a Recolher
    """
    table = soup.find("table", {"id": "itens_lancamentos"})
    if not table:
        return [], {"encontrada": False, "motivo": "tabela id=itens_lancamentos nao existe"}

    headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    hn = [_norm_text(h) for h in headers]

    idx = {
        "tipo":           _find_col_idx(hn, "tipo"),
        "produto":        _find_col_idx(hn, "produto"),
        "descricao":      _find_col_idx(hn, "descricao", "descrição", "descr"),
        "valor_item":     _find_col_idx(hn, "valor item"),
        "valor_debito":   _find_col_idx(hn, "valor debito", "valor débito"),
        "valor_credito":  _find_col_idx(hn, "valor credito", "valor crédito"),
        "valor_recolher": _find_col_idx(hn, "valor a recolher"),
    }

    def get(cols: List[str], key: str) -> Optional[str]:
        i = idx.get(key)
        if i is None or i < 0 or i >= len(cols):
            return None
        v = cols[i].strip()
        return v or None

    out: List[Dict[str, Any]] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        cols = [td.get_text(" ", strip=True) for td in tds]
        if len(cols) < 3:
            continue

        row = {k: get(cols, k) for k in idx}
        row["_data_tipo1"] = tr.get("data-tipo1")
        row["_data_tipo2"] = tr.get("data-tipo2")

        if not any([row.get("tipo"), row.get("produto"), row.get("descricao")]):
            continue
        out.append(row)

    diag = {
        "encontrada": True,
        "headers_found": headers,
        "n_headers": len(headers),
        "col_map": idx,
        "linhas_extraidas": len(out),
    }
    return out, diag


# ── Scorers ────────────────────────────────────────────────────────
_SCORE_ITENS_KW: List[Tuple[str, int]] = [
    ("ncm",              50),
    ("cfop",             30),
    ("cest",             20),
    ("cst",              20),
    ("csosn",            20),
    ("o/cst",            20),
    ("descricao",        15),
    ("descr",            10),
    ("item",             10),
    ("valor produto",     5),
    ("itens da nota",    60),
]

_SCORE_CALC_KW: List[Tuple[str, int]] = [
    ("val. mercadoria",        40),
    ("val. debito mercadoria", 40),
    ("val. a recolher",        35),
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


def _score_table(table: Tag, keywords: List[Tuple[str, int]], bonus_if_n_headers: Optional[int] = None) -> int:
    ths = _table_headers(table)
    if not ths:
        return 0
    joined = _norm_text(" | ".join(ths))
    score = sum(pts for kw, pts in keywords if kw in joined)
    if bonus_if_n_headers and len(ths) == bonus_if_n_headers:
        score += 30
    for row in _table_data_rows(table):
        if len(row) >= 10 and re.fullmatch(r"\d+", row[0].strip()):
            score += 5
    return score


def _pick_best_table(
    soup: BeautifulSoup,
    keywords: List[Tuple[str, int]],
    min_score: int = 30,
    bonus_if_n_headers: Optional[int] = None,
) -> Tuple[Optional[Tag], Dict[str, Any]]:
    tables = soup.find_all("table")
    best: Optional[Tag] = None
    best_score = -1
    scores = []

    for i, t in enumerate(tables):
        sc = _score_table(t, keywords, bonus_if_n_headers)
        ths = _table_headers(t)
        data_count = len(_table_data_rows(t))
        scores.append({"i": i, "score": sc, "headers": len(ths), "data_rows": data_count})
        if sc > best_score:
            best_score = sc
            best = t

    diag = {"tables_found": len(tables), "best_score": best_score, "scores": scores}
    return (best if best_score >= min_score else None), diag


def _localizar_tabela_calculo(soup: BeautifulSoup) -> Tuple[Optional[Tag], str]:
    """
    Localiza a tabela de Cálculo Itens com 4 estratégias em cascata.
    """
    container = soup.find("div", {"id": "calculo-itens-container"})
    if container:
        table = container.find("table")
        if table:
            return table, "id:calculo-itens-container"

    rows = soup.find_all("tr", class_="itens-calculos-row")
    if rows:
        table = rows[0].find_parent("table")
        if table:
            return table, "class:itens-calculos-row"

    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        txt = _norm_text(heading.get_text())
        if "calculo itens" in txt or "calculo de itens" in txt:
            for parent in heading.parents:
                if parent.name in ("div", "section", "article"):
                    t = parent.find("table")
                    if t:
                        return t, "heading:parent"
            t = heading.find_next("table")
            if t:
                return t, "heading:next"

    tables = soup.find_all("table")
    best: Optional[Tag] = None
    best_score = -1
    for t in tables:
        sc = _score_table(t, _SCORE_CALC_KW, bonus_if_n_headers=17)
        if sc > best_score:
            best_score = sc
            best = t
    if best and best_score >= 100:
        return best, f"scorer:score={best_score}"

    return None, "nao_encontrada"


# ── Parse: Itens da Nota (dinâmico) ───────────────────────────────
def _parse_itens_da_nota(table: Tag) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    all_tr = table.find_all("tr")

    header_cells: List[str] = []
    header_tr: Optional[Tag] = None
    for tr in all_tr[:15]:
        ths = tr.find_all("th")
        if len(ths) >= 4:
            header_cells = [th.get_text(" ", strip=True) for th in ths]
            header_tr = tr
            break
    if not header_cells and all_tr:
        cells = all_tr[0].find_all(["td", "th"])
        header_cells = [c.get_text(" ", strip=True) for c in cells]
        header_tr = all_tr[0]

    hn = [_norm_text(h) for h in header_cells]

    idx = {
        "item":      _find_col_idx(hn, "item"),
        "descricao": _find_col_idx(hn, "descricao", "descrição", "descr"),
        "cst":       _find_col_idx(hn, "o/cst", "o/csosn", "cst", "csosn"),
        "cfop":      _find_col_idx(hn, "cfop"),
        "cest":      _find_col_idx(hn, "cest"),
        "ncm":       _find_col_idx(hn, "ncm"),

        "vbc_st":    _find_col_idx(hn, "valor base calc. st"),
        "vicms_st":  _find_col_idx(hn, "valor icms st"),
        "vicms_dest":_find_col_idx(hn, "valor icms uf dest"),
        "vbc_icms":  _find_col_idx(hn, "valor base calc. icms"),
        "aliq_icms": _find_col_idx(hn, "% aliq"),
        "vicms":     _find_col_idx(hn, "valor icms", "valor icms sn"),
        "vprod":     _find_col_idx(hn, "valor produto"),
        "vdesc":     _find_col_idx(hn, "valor desconto"),
        "vipi":      _find_col_idx(hn, "valor ipi"),
        "vseg":      _find_col_idx(hn, "valor seguro"),
        "voutro":    _find_col_idx(hn, "valor outros"),
        "vfrete":    _find_col_idx(hn, "valor frete"),
        "vsubtotal": _find_col_idx(hn, "valor sub total"),
        "vicms_deson":_find_col_idx(hn, "valor icms desonerado"),
        "vtotal":    _find_col_idx(hn, "valor total"),
        "cls":       _find_col_idx(hn, "cls"),
        "prod_sefin":_find_col_idx(hn, "produto sefin"),
        "obs":       _find_col_idx(hn, "obs"),
    }

    def get(cols: List[str], key: str) -> Optional[str]:
        i = idx.get(key)
        if i is None or i < 0 or i >= len(cols):
            return None
        v = cols[i].strip()
        return v or None

    items: List[Dict[str, Any]] = []
    for tr in all_tr:
        if tr is header_tr:
            continue
        tds = tr.find_all("td")
        if not tds:
            continue
        cols = [td.get_text(" ", strip=True) for td in tds]
        if len(cols) < 2:
            continue

        item_str = get(cols, "item") or cols[0].strip()
        if not item_str or not re.fullmatch(r"\d+", item_str.strip()):
            continue

        row: Dict[str, Any] = {k: get(cols, k) for k in idx}
        row["item"] = item_str.strip()
        row["_n_cols"] = len(cols)

        if not any([row.get("descricao"), row.get("ncm"), row.get("cfop"), row.get("cst")]):
            continue

        items.append(row)

    diag = {
        "headers_found": header_cells,
        "n_headers": len(header_cells),
        "col_map": idx,
        "items_extraidos": len(items),
    }
    return items, diag


# ── Parse: Cálculo Itens (dinâmico) ───────────────────────────────
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
    "val_deb_mercadoria":     ["val. debito mercadoria", "val. debito merc"],
    "val_deb_frete":          ["val. debito frete-fob", "val. debito frete"],
    "val_cred_nfe":           ["val. credito nfe", "val. cred. nfe"],
    "val_cred_cte":           ["val. credito cte", "val. cred. cte"],
    "val_cred_complementar":  ["val. cred. comple", "val. credito compl", "cred. comple"],
    "valor_a_recolher":       ["val. a recolher", "valor a recolher"],
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
        return [], {"erro": "tabela vazia"}

    header_cells: List[str] = []
    header_tr: Optional[Tag] = None
    for tr in all_tr[:10]:
        ths = tr.find_all("th")
        if len(ths) >= 5:
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
        if len(cols) < 3:
            continue

        item_val = get(cols, "item") or cols[0].strip()
        if not item_val or not re.fullmatch(r"\d+", item_val):
            continue

        row: Dict[str, Any] = {f: get(cols, f) for f in _CALC_COL_ALIASES}
        row["item"] = item_val
        row["_n_cols"] = len(cols)
        out.append(row)

    diag = {
        "headers_found": header_cells,
        "n_headers": len(header_cells),
        "col_map": col_idx,
        "colunas_nao_mapeadas": unmapped,
        "linhas_extraidas": len(out),
    }
    return out, diag


# ── Merge consolidado (por ITEM) ──────────────────────────────────
def _merge_itens(
    itens_nota: List[Dict[str, Any]],
    calculo:    List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    calc_by_item: Dict[str, Dict[str, Any]] = {}
    for c in calculo or []:
        k = str(c.get("item") or "").strip()
        if k and k not in calc_by_item:
            calc_by_item[k] = c

    merged: List[Dict[str, Any]] = []
    with_calc = 0

    for it in itens_nota or []:
        k = str(it.get("item") or "").strip()
        c = calc_by_item.get(k)

        row: Dict[str, Any] = {
            "item":       k or None,
            "descricao":  it.get("descricao"),
            "cst":        it.get("cst"),
            "cfop":       it.get("cfop"),
            "cest":       it.get("cest"),
            "ncm":        it.get("ncm"),

            "vbc_st":     it.get("vbc_st"),
            "vicms_st":   it.get("vicms_st"),
            "vicms_dest": it.get("vicms_dest"),
            "vbc_icms":   it.get("vbc_icms"),
            "aliq_icms":  it.get("aliq_icms"),
            "vicms":      it.get("vicms"),
            "vprod":      it.get("vprod"),
            "vdesc":      it.get("vdesc"),
            "vipi":       it.get("vipi"),
            "vseg":       it.get("vseg"),
            "voutro":     it.get("voutro"),
            "vfrete":     it.get("vfrete"),
            "vsubtotal":  it.get("vsubtotal"),
            "vicms_deson":it.get("vicms_deson"),
            "vtotal":     it.get("vtotal"),
            "cls":        it.get("cls"),
            "prod_sefin": it.get("prod_sefin"),
            "obs":        it.get("obs"),

            "calc_produto":      None,
            "calc_val_merc":     None,
            "calc_perc_red":     None,
            "calc_bc_merc":      None,
            "calc_frete":        None,
            "calc_bc_frete":     None,
            "calc_bc_final":     None,
            "calc_aliq_int":     None,
            "calc_aliq_nfe":     None,
            "calc_aliq_cte":     None,
            "calc_deb_merc":     None,
            "calc_deb_frete":    None,
            "calc_cred_nfe":     None,
            "calc_cred_cte":     None,
            "calc_cred_compl":   None,
            "calc_a_recolher":   None,
        }

        if c:
            with_calc += 1
            row.update({
                "calc_produto":    c.get("produto"),
                "calc_val_merc":   c.get("val_mercadoria"),
                "calc_perc_red":   c.get("perc_reducao"),
                "calc_bc_merc":    c.get("bc_mercadoria"),
                "calc_frete":      c.get("frete_fob"),
                "calc_bc_frete":   c.get("bc_frete"),
                "calc_bc_final":   c.get("bc_final"),
                "calc_aliq_int":   c.get("aliq_interna"),
                "calc_aliq_nfe":   c.get("aliq_orig_nfe"),
                "calc_aliq_cte":   c.get("aliq_orig_cte"),
                "calc_deb_merc":   c.get("val_deb_mercadoria"),
                "calc_deb_frete":  c.get("val_deb_frete"),
                "calc_cred_nfe":   c.get("val_cred_nfe"),
                "calc_cred_cte":   c.get("val_cred_cte"),
                "calc_cred_compl": c.get("val_cred_complementar"),
                "calc_a_recolher": c.get("valor_a_recolher"),
            })

        merged.append(row)

    diag = {
        "itens_nota":        len(itens_nota or []),
        "linhas_calculo":    len(calculo or []),
        "itens_com_calculo": with_calc,
        "itens_sem_calculo": len(itens_nota or []) - with_calc,
    }
    return merged, diag


# ── Entry point do parser ──────────────────────────────────────────
def parse_internamento(html_intern: str) -> Dict[str, Any]:
    """
    Retorna:
      - tables.itens_lancamentos
      - tables.itens_nota
      - tables.calculo_itens
      - merged_by_item (tentativa de merge por ITEM)
    """
    soup = BeautifulSoup(html_intern, "lxml")

    # 0) Primeira tabela (itens_lancamentos)
    itens_lanc, diag_lanc = _parse_itens_lancamentos(soup)

    # 1) Itens da Nota (tabela grande)
    tab_itens, diag_sel_itens = _pick_best_table(soup, _SCORE_ITENS_KW, min_score=50)
    itens_nota: List[Dict[str, Any]] = []
    diag_parse_itens: Dict[str, Any] = {}
    if tab_itens:
        itens_nota, diag_parse_itens = _parse_itens_da_nota(tab_itens)
    for it in itens_nota:
        it.pop("_n_cols", None)

    # 2) Cálculo Itens (tabela final)
    tab_calc, estrategia_calc = _localizar_tabela_calculo(soup)
    calc_itens: List[Dict[str, Any]] = []
    diag_parse_calc: Dict[str, Any] = {"estrategia": estrategia_calc}
    if tab_calc:
        _linhas, _d = _parse_calculo_itens(tab_calc)
        calc_itens = _linhas
        diag_parse_calc.update(_d)
    for c in calc_itens:
        c.pop("_n_cols", None)

    # 3) Merge backend por ITEM (se der)
    itens_consolidados, diag_merge = _merge_itens(itens_nota, calc_itens)

    return {
        "tables": {
            "itens_lancamentos": itens_lanc,
            "itens_nota": itens_nota,
            "calculo_itens": calc_itens,
        },
        "merged_by_item": itens_consolidados,
        "totais": {
            "qtd_itens_merge":       len(itens_consolidados),
            "qtd_itens_nota":        len(itens_nota),
            "qtd_itens_calculo":     len(calc_itens),
            "qtd_itens_lancamentos": len(itens_lanc),

            "n_cols_itens_nota":     diag_parse_itens.get("n_headers", 0),
            "n_cols_calculo":        diag_parse_calc.get("n_headers", 0),

            "itens_com_calculo":     diag_merge.get("itens_com_calculo", 0),
            "itens_sem_calculo":     diag_merge.get("itens_sem_calculo", 0),
            "itens_sem_ncm":         sum(1 for x in itens_consolidados if not x.get("ncm")),
        },
        "diagnostico": {
            "itens_lancamentos": diag_lanc,
            "itens_da_nota": {**diag_sel_itens, "parse": diag_parse_itens},
            "calculo_itens": diag_parse_calc,
            "merge": diag_merge,
        },
    }


# ══════════════════════════════════════════════════════════════════
# APP FASTAPI
# ══════════════════════════════════════════════════════════════════
app = FastAPI(title="analise — Débitos + Extrato por Produto (3 tabelas + merge opcional)")

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

        # Abrir extrato.jsp
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

        # Abrir internamento
        url_capa = _url_capa_internamento(usuario, chave_alvo, token)
        r2 = sess.get(url_capa, timeout=60, allow_redirects=True)
        if r2.status_code != 200:
            raise HTTPException(400, f"Falha ao abrir internamento: HTTP {r2.status_code}")

        # Parser dinâmico (agora retorna tabelas separadas + merge opcional)
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
                **parsed,
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
