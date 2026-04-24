# Backed - Entendimento Tecnico (analise.py, danfe.py, parcelamento.py)

## Escopo analisado
- Arquivo 1: `C:/Users/andre/Documents/ComunidadeFiscalB/analise/pasta/analise.py`
- Arquivo 2: `C:/Users/andre/Documents/ComunidadeFiscalB/analise/pasta/danfe.py`
- Arquivo 3: `C:/Users/andre/Documents/ComunidadeFiscalB/analise/pasta/parcelamento.py`

Observacao: este documento descreve arquitetura, fluxo, rotas, validacoes, dependencias e riscos atuais do backend.

## Visao geral da arquitetura
- O backend principal e um app FastAPI em `analise.py`.
- Esse app atua como agregador de modulos:
- Monta (`app.mount`) o modulo DANFSe (`danfe.py`) em `/danfe` quando disponivel.
- Monta (`app.mount`) o modulo Parcelamento/SitFis (`parcelamento.py`) na raiz (`""`) quando disponivel.
- O mesmo processo expõe:
- Rotas de consulta de empresas/debitos/extrato (SEFIN-RO) em `analise.py`.
- Rotas DANFSe (NFS-e nacional) em `danfe.py`.
- Rotas Serpro Integra Contador (parcelamento + situacao fiscal) em `parcelamento.py`.

## 1) analise.py - Entendimento completo

### Objetivo funcional
- Orquestrar autenticacao com certificado mTLS (PEM/KEY vindos do Supabase), navegar no DET/Portal Contribuinte da SEFIN-RO e extrair dados fiscais.
- Expor API HTTP para:
- listar empresas (`/empresas`),
- consultar debitos (`/debitos`),
- extrair detalhes de produto/internamento por chave (`/extrato-produto`).
- Servir como gateway que agrega tambem os modulos DANFSe e Parcelamento/SitFis.

### Principais blocos
- Configuracao e logging:
- Usa `SUPABASE_URL`, `SUPABASE_KEY`, `TABELA_CERTS`, `DEBUG_ERRORS`, `LOG_FILE`.
- Logging com `RotatingFileHandler`.
- Acesso a certificados no Supabase:
- Busca na tabela `certifica_dfe` (`id,pem,key,empresa,codi,user,vencimento,"cnpj/cpf"`).
- Selecao por `codi`.
- Decodifica base64 de PEM/KEY e escreve em arquivos temporarios.
- Navegacao autenticada (mTLS):
- `abrir_acesso_digital_e_entrar` entra no DET.
- `ir_para_portal` resolve formulario `LoginToken` e redirect para portal.
- Debitos:
- `consultar_debitos_ano` chama `consultadebitos/lista.jsp` por inscricao estadual.
- `obter_debitos_inscricao_estadual` parseia tabela HTML de debitos.
- Extrato/internamento:
- Extrai token/usuario do extrato.
- Lista notas NFe e links CTe.
- Abre capa de internamento e parseia tabela "Itens da nota".

### Funcoes relevantes
- `carregar_certificados`, `selecionar_cert_por_codi`, `criar_arquivos_cert_temp`, `criar_sessao`
- `_extrair_form_logintoken`, `_extrair_redirect_do_logintoken`, `ir_para_portal`
- `_listar_inscricoes_estaduais`, `obter_debitos_inscricao_estadual`, `consultar_debitos_ano`
- `_extrair_token_e_usuario`, `_extrair_notas_do_extrato_contacorrente`, `_buscar_chaves_cte`
- `_parse_itens_da_nota_primeiras_10_colunas`, `parse_internamento`

### Rotas expostas em analise.py
- `GET /`
- Lista health e rotas disponiveis (incluindo montadas, se carregadas).
- `GET /health`
- Health basico.
- `GET /empresas?user=...`
- Retorna empresas deduplicadas por `codi` para o user.
- `GET /debitos?user=...&codi=...&incluir_ano_anterior=0|1`
- Loga no portal e retorna debitos do ano atual (e opcionalmente anterior).
- `GET /extrato-produto?user=...&codi=...&url_extrato=...&chave=...&max_notas=...&buscar_cte=...`
- Retorna notas NFe com itens de internamento e chaves CTe relacionadas.

### Tratamento de erros
- Handler global de excecao FastAPI retorna JSON com detalhes quando `DEBUG_ERRORS=1`.
- Limpeza de arquivos temporarios em blocos `finally`.

### Dependencias externas
- FastAPI, Starlette WSGIMiddleware, requests, BeautifulSoup/lxml.
- Supabase REST (nao SDK).
- DET/Portal Contribuinte SEFIN-RO.

### Pontos de atencao
- Chaves sensiveis estao hardcoded no arquivo (ex.: `SUPABASE_KEY` padrao).
- Forte acoplamento a estrutura HTML do portal (mudancas de layout podem quebrar parse).
- Encoding de textos em alguns trechos indica possivel problema de charset em respostas externas.

## 2) danfe.py - Entendimento completo

### Objetivo funcional
- Gerar/obter PDF de DANFSe (Ambiente Nacional NFS-e) via mTLS.
- Buscar certificado de cliente na tabela `certifica_dfe` (Supabase) por `user + cnpj`.

### Fluxo principal
- Recebe payload em `POST /danfse/pdf` com:
- `user`, `cnpj`, `chave` (50 digitos), `env` opcional.
- Valida campos.
- Busca registro do certificado no Supabase.
- Decodifica PEM/KEY base64 e grava em arquivos temporarios.
- Monta URL DANFSe (`base + /danfse/{chave}`).
- Faz GET com mTLS e retry para 502/503/504.
- Se retorno for PDF, devolve `application/pdf` inline.
- Se erro HTTP/conteudo nao PDF, devolve JSON de erro com preview de body.

### Rotas
- `GET /health`
- `POST /danfse/pdf`

### Funcoes relevantes
- `fetch_cert_row`, `create_temp_cert_files`
- `make_session`, `http_get_with_retry`
- `build_base_url`, `build_url`

### Dependencias externas
- Flask, flask-cors, requests, urllib3 Retry.
- Supabase REST.
- Endpoint NFS-e nacional (`adn.nfse.gov.br` ou `adn.producaorestrita.nfse.gov.br`).

### Pontos de atencao
- Service role/anon key tambem estao hardcoded no fonte.
- Retry custom no loop manual (nao no adapter) - funcional, mas requer cuidado de timeout total.
- Critico manter limpeza dos arquivos temporarios (ja existe `td.cleanup()`).

## 3) parcelamento.py - Entendimento completo

### Objetivo funcional
- Integrar com Serpro Integra Contador para:
- Situacao fiscal (Apoiar + Emitir para gerar relatorio PDF)
- Parcelamento (Consultar + Emitir DAS)
- Expor rotas HTTP usadas pelo frontend (`serpro.html`).

### Estrategia de integracao Serpro
- Autenticacao OAuth-like via endpoint de token (`authenticate`) com certificado mTLS e `consumer_key/consumer_secret`.
- Todas as chamadas de negocio usam `session.post(..., cert=(pem,key))`.
- Estrutura de `pedidoDados` varia por servico (`idSistema`, `idServico`, `versaoSistema`).

### Montagem de payloads
- Funcoes `body_*` montam objetos com:
- `contratante`, `autorPedidoDados`, `contribuinte`
- Hoje todos com `tipo: 2` (PJ/CNPJ).
- Para parcelamento:
- `PARCELAMENTO_ID_SISTEMA` default `PARCSN`
- consultar: servico `PEDIDOSPARC163`
- emitir: servico `GERARDAS161`
- Para SitFis:
- `ID_SISTEMA = SITFIS`
- apoiar: `SOLICITARPROTOCOLO91`
- emitir: `RELATORIOSITFIS92`

### Parser de SitFis
- Recebe PDF base64 e extrai texto com `pdfplumber` (ou fallback `pypdf`).
- Parseia secoes:
- omissoes (`parse_omissoes_section`)
- debitos (`parse_debitos_section`)
- processos (`parse_processos_section`)
- Retorna estrutura simplificada para frontend.

### Rotas expostas
- `POST /integra-sitfis/situacao/consultar`
- `POST /integra-parcelamento/situacao/consultar`
- `POST /situacao/consultar`
- `POST /integra-parcelamento/parcelamentos/consultar`
- `POST /integra-parcelamento/parcelamentos/emitir`
- `POST /integra-parcelamento/parcelamentos/consultar-com-sitfis`
- `GET /health`

### Validacoes atuais (ponto critico)
- O backend filtra lista de contribuintes com `len(cnpj) == 14` em `consultar_situacao`.
- Rotas de parcelamento/consultar/emitir exigem `len(contribuinte_cnpj) == 14`.
- Contador tambem precisa ter `len(cnpj_contador) == 14`.
- Ou seja: hoje esta explicitamente orientado a CNPJ.

### Dependencias externas
- Flask, flask-cors, requests.
- Opcional: pdfplumber / pypdf.
- APIs Serpro Integra Contador (token + endpoints de negocio).

### Pontos de atencao
- Alguns textos de erro apresentam encoding quebrado (ex.: "invÃ¡lido").
- URL base de parcelamento/sitfis usa mesmos caminhos `/Consultar` e `/Emitir`, variando pelo `pedidoDados`.
- Segredos e parametros sensiveis podem vir no payload; log/telemetria devem evitar exposicao.

## Relacao entre os 3 arquivos
- `analise.py` e o entrypoint agregado e monta WSGI dos outros modulos.
- `danfe.py` e especializado em DANFSe (NFS-e nacional).
- `parcelamento.py` e especializado em Serpro Integra Contador (parcelamento + situacao fiscal).
- O frontend `serpro.html` chama majoritariamente rotas de `parcelamento.py` expostas atraves de `analise.py`.

## Mapa de dados importante para o frontend
- Tabela Supabase `certifica_dfe`:
- campos essenciais: `pem`, `key`, `empresa`, `codi`, `user`, `cnpj/cpf`.
- Frontend costuma enviar:
- `consumer_key`, `consumer_secret`
- `pem_base64`, `key_base64`
- `contratante_cnpj`, `autor_cnpj`
- `contribuinte_cnpj` (ou lista `cnpjs`)

## Riscos tecnicos identificados
- Secrets hardcoded em fonte (alto risco de seguranca).
- Validacoes/rotas hoje acopladas a CNPJ de 14 digitos.
- Dependencia de parse de PDF/HTML por regex e estrutura textual (fragil a mudancas de layout).
- Possivel variacao de resposta Serpro em `dados` exigindo robustez no parser.

## Base para o proximo passo (suporte CPF)
- Para suportar CPF, sera necessario ajustar em conjunto:
- frontend (`serpro.html`) para aceitar documento 11 digitos no fluxo SitFis/Parcelamento onde aplicavel.
- backend (`parcelamento.py`) para nao filtrar/exigir apenas 14 digitos.
- payload Serpro para usar `tipo` correto por documento (PJ vs PF), em vez de fixar sempre `tipo: 2`.
- revisao de validacoes e mensagens de erro para "documento" ao inves de "CNPJ" quando apropriado.
