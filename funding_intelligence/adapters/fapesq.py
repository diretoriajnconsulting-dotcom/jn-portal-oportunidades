"""Editais da FAPESQ-PB (Fundação de Apoio à Pesquisa do Estado da Paraíba).

O site é Plone clássico, sem `++api++`, e muda de leiaute de um ano para o outro (estudo de
14/10/2026, `radar-fontes-saude/estudo/ppsus_norte_nordeste.md`, seção 5.4):

  · 2026: capa em `/editais/2026/editais-2026` com os 5 editais mais novos e a coleção do ano;
    cada edital é uma página com os PDFs (edital e retificações);
  · 2025: pasta de arquivos em `/editais/editais-2025`, do mais velho para o mais novo;
  · a raiz `/editais` pede login. Não dá para adivinhar a pasta de 2027.

Por isso a leitura começa pela home, que liga a capa do ano e a página "EDITAIS LANÇADOS E/OU COM
PRAZO DE SUBMISSÃO DE PROPOSTAS ABERTO" (cada edital com a linha "Inscrições: …"), e completa com
a busca `@@search?SearchableText=ppsus`, que acha o PPSUS em qualquer pasta, com a data de cada
documento.

Correções de 14/10/2026 (defeitos achados no estudo):
  · prazo: vale a versão mais recente do edital (a última retificação) e, nela, a linha de
    inscrição ou submissão do cronograma. Antes valia o maior valor de todas as datas, o que
    mantinha aberto por engano quando havia prazo de recurso depois da inscrição;
  · todas as retificações entram como documento (a PB teve quatro no PPSUS 02/2025), e o PDF lido
    é o da versão mais recente, não os três primeiros;
  · `empresa` deixou de entrar só porque a palavra aparece: no PPSUS, "empresa pública" é
    executora e "participação de empresa" é parceira (`partner_types`);
  · instrumento: bolsa só para seleção de bolsistas; o PPSUS é auxílio a projeto;
  · o PPSUS leva o tema `pesquisa_sus`, os parceiros (Decit/MS, CNPq, SES-PB) e o valor global.

Identidade: `NN-AAAA` do número do edital, sem zero à esquerda (`fapesq-66-2026`), nunca do título.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import time
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from pypdf import PdfReader

from funding_intelligence.adapters.base import BaseAdapter
from funding_intelligence.models import THEME_ALIASES, ascii_fold, clean_text, opportunity, parse_br_date


HOME = "https://fapesq.rpp.br/"
BUSCA_PPSUS = "https://fapesq.rpp.br/@@search?SearchableText=ppsus&sort_on=Date&sort_order=reverse"

# Orçamento de pedidos (CONSTRUCAO.md): 15 por dia. A carga histórica só percorre a busca do PPSUS
# (os demais editais da FAPESQ são seleção de bolsistas, de interesse baixo para o radar e caros:
# dois pedidos cada).
ORCAMENTO_DIA = 15
ORCAMENTO_HISTORICO = 40
PAUSA_S = 1.0
RETENCAO_DIAS = 730

MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
DATA_CURTA = r"\d{1,2}\s*[/.]\s*\d{1,2}\s*[/.]\s*(?:\d{4}|\d{2})(?!\d)"
DATA_LONGA = r"\d{1,2}º?\s+de\s+[a-zç]+\s+de\s+\d{4}"
DATA = rf"(?:{DATA_CURTA}|{DATA_LONGA})"
DATA_RE = re.compile(DATA, re.I)
# Rótulo de inscrição ou submissão, tolerante ao acento quebrado dos PDFs ("Inscri��o").
ROTULO = re.compile(r"inscri\S{0,4}(?:es|o)\b|submiss\S{0,3}o\b|envio\s+d[ae]s?\s+propostas?", re.I)
# Linhas do cronograma que citam a inscrição, mas não são o prazo dela.
NAO_E_PRAZO = re.compile(
    r"recurso|homologa|resultado|impugna|divulga|deferid|confirma|in[ií]cio|abertura|entrevista|convoca",
    re.I,
)
CABECALHO = re.compile(
    r"CRONOGRAMA[^.]{0,40}?\b(PER[IÍ]ODO|DATAS?|ATIVIDADES?|ETAPAS?|FASES)"
    r"\s+(PER[IÍ]ODO|DATAS?|ATIVIDADES?|ETAPAS?|FASES)\b",
    re.I,
)
PROXIMA_SECAO = re.compile(r"\s\d{1,2}\.\s+(?:DOS?\s+|DAS?\s+)?[A-ZÁÉÍÓÚÇÃÕ]{4,}")
VALOR_GLOBAL = re.compile(
    r"valor\s+global\s+(?:de\s+|estimado\s+(?:de\s+|em\s+)?)?(?:at[eé]\s+)?R\$\s*([\d.]+,\d{2})", re.I,
)

EDITAL_NUMERO = re.compile(r"(?:edital|chamada)\D{0,25}?0*(\d{1,3})\s*(?:/|\s+de\s+)\s*(20\d{2})\b", re.I)
N_NUMERO = re.compile(r"\bN\s*[º°o]\s*\.?\s*0*(\d{1,3})\s*/\s*(20\d{2})\b", re.I)
URL_NUMERO = re.compile(r"edital[-_ ]n?[o0]?[-_ ]*0*(\d{1,3})[-_ /](20\d{2})(?!\d)", re.I)
PASTA_DO_ANO = re.compile(r"/editais/(?:(20\d\d)/)?editais-(20\d\d)/?$")
PPSUS = re.compile(r"ppsus|pesquisa\s+para\s+o\s+sus", re.I)
# Documentos que não são versão do edital: o prazo nunca vem deles.
NAO_E_VERSAO = re.compile(r"resultado|classifica|homologa|convoca|recurso|\blista\b|gabarito|\bata\b|deferid", re.I)
# Bloco de siglas logo depois do número ("EDITAL Nº 70/2026 – CODATA/FAPESQ/PB PROCESSO…"): são os
# parceiros do edital, e saem do título para ele ser igual em todas as páginas.
SIGLAS_DO_NUMERO = re.compile(r"N\s*[º°o]?\s*\d{1,3}\s*/\s*20\d{2}\s*[–-]?\s*([A-Z]{2,}(?:/[A-Z]{2,})+)\b")


class Bloqueio(RuntimeError):
    """403, 429, CAPTCHA ou Cloudflare: o adaptador para naquele host e registra."""


class OrcamentoEsgotado(RuntimeError):
    """O limite de pedidos da rodada acabou; o que já foi lido segue para o catálogo."""


def _parece_bloqueio(corpo: bytes) -> bool:
    inicio = corpo[:6000].lower()
    titulo = re.search(rb"<title>([^<]*)", inicio)
    titulo_texto = titulo.group(1) if titulo else b""
    if any(x in titulo_texto for x in (b"just a moment", b"attention required", b"captcha", b"access denied")):
        return True
    return any(x in inicio for x in (b"cf-chl", b"challenge-platform", b"h-captcha", b"g-recaptcha"))


def _data(valor: str, ano_padrao: int | None = None) -> str | None:
    """dd/mm/aaaa, dd.mm.aaaa, dd/mm/aa ou "9 de novembro de 2026" → AAAA-MM-DD."""
    numerica = parse_br_date(re.sub(r"\s+", "", valor))
    if numerica:
        return numerica
    longa = re.search(r"(\d{1,2})º?\s+de\s+([a-z]+)\s+de\s+(\d{4})", ascii_fold(valor))
    if longa and longa.group(2) in MESES:
        try:
            return date(int(longa.group(3)), MESES[longa.group(2)], int(longa.group(1))).isoformat()
        except ValueError:
            return None
    return None


def _normaliza(texto: str) -> str:
    return re.sub(r"\s+", " ", texto or "")


def _data_depois(trecho: str) -> str | None:
    """A primeira data completa do trecho; num intervalo ("01/10/2026 a 16/10/2026"), o fim."""
    achada = DATA_RE.search(trecho)
    if not achada:
        return None
    intervalo = re.match(rf"\s*(?:a|à|at[eé]|e|-|–)\s*({DATA})", trecho[achada.end():], re.I)
    return _data(intervalo.group(1) if intervalo else achada.group(0))


def _data_antes(trecho: str) -> str | None:
    datas = DATA_RE.findall(trecho)
    return _data(datas[-1]) if datas else None


def _candidatos(regiao: str, orientacao: str | None) -> list[str]:
    """Prazos de inscrição ou submissão na ordem em que aparecem.

    `orientacao` diz de que lado da linha fica a data: "depois" (ATIVIDADES | DATA, como no
    PPSUS), "antes" (PERÍODO | ETAPA, como nos editais de seleção de 2026) ou None (texto corrido).
    """
    achados = []
    for rotulo in ROTULO.finditer(regiao):
        antes = regiao[max(0, rotulo.start() - 45):rotulo.start()]
        if NAO_E_PRAZO.search(antes[-35:] + rotulo.group(0)):
            continue
        depois = regiao[rotulo.end():rotulo.end() + 160]
        if orientacao == "antes":
            prazo = _data_antes(antes)
            if not prazo and re.match(r"\D{0,40}?\bat[eé]\b", depois, re.I):
                prazo = _data_depois(depois)
        elif orientacao == "depois":
            prazo = _data_depois(depois)
        else:
            prazo = _data_depois(depois[:120]) or _data_antes(antes)
        if prazo:
            achados.append(prazo)
    return achados


def _regiao_do_cronograma(texto: str) -> tuple[str, str] | None:
    """A última tabela de cronograma do documento e o lado em que fica a data."""
    cabecalhos = list(CABECALHO.finditer(texto))
    if not cabecalhos:
        return None
    ultimo = cabecalhos[-1]
    regiao = texto[ultimo.start():ultimo.start() + 8000]
    fim = PROXIMA_SECAO.search(regiao, 40)
    regiao = regiao[:fim.start()] if fim else regiao
    primeira = ascii_fold(ultimo.group(1))
    return regiao, ("antes" if primeira.startswith(("periodo", "data")) else "depois")


def prazo_de_submissao(texto: str) -> str | None:
    """Prazo de inscrição/submissão da versão do edital que está no texto.

    Com tabela de cronograma, vale a última linha de inscrição da última tabela: nas retificações
    do PPSUS da PB as tabelas antigas ficam no documento, antes da nova. Sem tabela, a primeira
    menção no texto corrido. Nunca o maior valor de todas as datas.
    """
    texto = _normaliza(texto)
    cronograma = _regiao_do_cronograma(texto)
    if cronograma:
        achados = _candidatos(*cronograma)
        if achados:
            return achados[-1]
    achados = _candidatos(texto, None)
    return achados[0] if achados else None


def lancamento(texto: str) -> str | None:
    texto = _normaliza(texto)
    cronograma = _regiao_do_cronograma(texto)
    if cronograma:
        regiao, orientacao = cronograma
        padrao = (
            rf"({DATA})\s+Lan[cç]amento" if orientacao == "antes" else rf"Lan[cç]amento[^0-9]{{0,160}}?({DATA})"
        )
        achado = re.search(padrao, regiao, re.I)
        if achado:
            return _data(achado.group(1))
    achado = re.search(rf"(?:publica[cç][aã]o|lan[cç]amento)[^\n.]{{0,80}}?({DATA})", texto, re.I)
    return _data(achado.group(1)) if achado else None


def _valor_reais(texto: str) -> float | None:
    achado = VALOR_GLOBAL.search(_normaliza(texto))
    if not achado:
        return None
    try:
        return float(achado.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _url_limpa(url: str) -> str:
    """Tira o `/view` do Plone e os `../` que a página de prazos abertos põe no caminho."""
    partes = urlsplit(url)
    caminho = posixpath.normpath(partes.path) if partes.path else "/"
    while caminho.startswith("/.."):
        caminho = caminho[3:] or "/"
    if caminho.endswith("/view"):
        caminho = caminho[:-5]
    return urlunsplit((partes.scheme, partes.netloc, caminho, partes.query, ""))


def _eh_pdf(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


class FapesqAdapter(BaseAdapter):
    """Edital → documentos → prazo da versão mais recente.

    A FAPESQ publica o edital, as retificações e os resultados como documentos soltos. Eles são
    agrupados pelo número do edital, e "open" só sai com prazo lido numa página oficial (a de
    prazos abertos) ou no cronograma da versão mais recente.
    """

    id = "fapesq"
    name = "FAPESQ-PB"
    # Endereço registrado em `config/sources.json` (o teste de contrato exige o mesmo valor): a home,
    # de onde a coleta parte para achar a pasta do ano.
    url = HOME

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.pedidos = 0
        self.avisos: list[str] = []
        self._ultimo_pedido: dict[str, float] = {}
        self._bloqueados: set[str] = set()
        self._mapa_fixture: dict[str, str] | None = None

    # ------------------------------------------------------------------ pedidos

    @property
    def limite_pedidos(self) -> int:
        return ORCAMENTO_HISTORICO if self.historical else ORCAMENTO_DIA

    def _fixture_dir(self) -> Path | None:
        return Path(self.fixture) if self.fixture and Path(self.fixture).is_dir() else None

    def _da_fixture(self, url: str) -> bytes:
        """Fixture em pasta: `urls.json` liga cada URL a um arquivo. URL sem arquivo = falha de rede."""
        pasta = self._fixture_dir()
        if self._mapa_fixture is None:
            self._mapa_fixture = json.loads((pasta / "urls.json").read_text(encoding="utf-8"))
        nome = self._mapa_fixture.get(url)
        if not nome:
            raise urllib.error.URLError(f"sem fixture: {url}")
        return (pasta / nome).read_bytes()

    def _pedir(self, url: str) -> bytes:
        if self.pedidos >= self.limite_pedidos:
            raise OrcamentoEsgotado(url)
        host = urlsplit(url).netloc
        if host in self._bloqueados:
            raise Bloqueio(f"host bloqueado nesta rodada: {host}")
        self.pedidos += 1
        if self._fixture_dir():
            return self._da_fixture(url)
        # Educação com o servidor: no máximo 1 pedido por segundo por host (sem pausa na fixture).
        espera = PAUSA_S - (time.monotonic() - self._ultimo_pedido.get(host, float("-inf")))
        if espera > 0:
            time.sleep(espera)
        try:
            corpo = self.fetch_bytes(url)
        except urllib.error.HTTPError as erro:
            if erro.code in (403, 429):
                self._bloqueados.add(host)
                raise Bloqueio(f"HTTP {erro.code} em {url}") from erro
            raise
        finally:
            self._ultimo_pedido[host] = time.monotonic()
        if _parece_bloqueio(corpo):
            self._bloqueados.add(host)
            raise Bloqueio(f"página de desafio em {url}")
        return corpo

    def _html(self, url: str) -> str:
        return self._pedir(url).decode("utf-8", errors="replace")

    def _pdf_texto(self, url: str) -> str:
        corpo = self._pedir(url)
        if corpo[:5] == b"%PDF-":
            reader = PdfReader(io.BytesIO(corpo))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        # Na fixture, o PDF é um `.txt` com o trecho já extraído.
        return corpo.decode("utf-8", errors="replace")

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        if self.avisos:
            snap["error"] = "; ".join(self.avisos)[:500]
        return snap

    # ------------------------------------------------------------------ coleta

    def collect(self) -> list[dict[str, Any]]:
        if self.fixture and not self._fixture_dir():
            # Fixture de arquivo único (testes antigos e `--fixture` do CLI): uma listagem.
            grupos: dict[str, dict[str, Any]] = {}
            self._ler_listagem(Path(self.fixture).read_text(encoding="utf-8"), HOME, grupos, "listagem")
            return [item for item in (self._montar(g) for g in self._ordenados(grupos)) if item]

        home = self._html(HOME)
        anos, pagina_abertos = self._descobrir(home)
        grupos = {}
        listagens_lidas = 0
        if pagina_abertos:
            listagens_lidas += self._tentar(lambda: self._ler_listagem(
                self._html(pagina_abertos), pagina_abertos, grupos, "abertos",
            ), "prazos abertos")
        correntes = [ano for ano in anos if ano <= self.today.year]
        if correntes:
            capa = anos[max(correntes)]
            listagens_lidas += self._tentar(
                lambda: self._ler_listagem(self._html(capa), capa, grupos, "capa"), f"capa {max(correntes)}",
            )
        if not listagens_lidas:
            raise RuntimeError("nem a página de prazos abertos nem a capa do ano foram lidas")
        self._tentar(lambda: self._ler_busca_ppsus(grupos), "busca PPSUS")

        # PDF só para o PPSUS e para o edital sem prazo lido na página de prazos abertos (o da
        # capa que já saiu dela, ou linha "Inscrições" ilegível).
        ano_do_corte = (self.today - timedelta(days=RETENCAO_DIAS)).year
        try:
            for grupo in self._ordenados(grupos):
                linha = grupo["linha_inscricoes"]
                if grupo["ano"] < ano_do_corte:
                    continue  # edição antiga que a busca ainda mostra: sairia pelo corte dos 24 meses
                if grupo["ppsus"] or not (linha and self._prazo_da_linha(linha, grupo["ano"])[0]):
                    self._ler_versao_mais_recente(grupo)
        except OrcamentoEsgotado:
            self.avisos.append(f"orçamento de pedidos esgotado ({self.pedidos} pedidos)")
        except Bloqueio as erro:
            self.avisos.append(str(erro))
        return [item for item in (self._montar(g) for g in self._ordenados(grupos)) if item]

    def _tentar(self, acao: Any, nome: str) -> int:
        try:
            acao()
            return 1
        except Bloqueio:
            raise
        except OrcamentoEsgotado:
            self.avisos.append(f"orçamento de pedidos esgotado antes de: {nome}")
        except Exception as erro:  # noqa: BLE001 - uma listagem que falha não derruba as outras
            self.avisos.append(f"{nome}: {type(erro).__name__}: {erro}")
        return 0

    @staticmethod
    def _ordenados(grupos: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """PPSUS primeiro; depois do edital mais novo para o mais velho (é a ordem do orçamento)."""
        return sorted(grupos.values(), key=lambda g: (not g["ppsus"], -g["ano"], -g["numero"]))

    # ------------------------------------------------------------------ leitura das páginas

    def _descobrir(self, home: str) -> tuple[dict[int, str], str | None]:
        """Capas por ano (`/editais/2026/editais-2026`, `/editais/editais-2025`) e a página de
        prazos abertos, achadas pelos links da home."""
        anos: dict[int, str] = {}
        abertos = None
        for link in self.soup(home).select("a[href]"):
            href = _url_limpa(urljoin(HOME, link.get("href") or ""))
            if urlsplit(href).netloc != urlsplit(HOME).netloc:
                continue
            pasta = PASTA_DO_ANO.search(urlsplit(href).path)
            if pasta:
                anos.setdefault(int(pasta.group(2)), href)
            rotulo = ascii_fold(link.get_text(" ", strip=True))
            if not abertos and ("prazo-de-submissao" in href or "prazo de submissao" in rotulo):
                abertos = href
        return anos, abertos

    def _grupo(self, grupos: dict[str, dict[str, Any]], externo: str) -> dict[str, Any]:
        numero, ano = (int(x) for x in externo.split("-"))
        return grupos.setdefault(externo, {
            "external_id": externo, "numero": numero, "ano": ano, "titulos": {}, "paginas": [],
            "documentos": [], "linha_inscricoes": None, "contextos": [], "ppsus": False,
            "texto_pdf": "", "titulo_pagina": None,
        })

    def _ler_listagem(self, html: str, base: str, grupos: dict[str, dict[str, Any]], origem: str) -> None:
        """Capa do ano, página de prazos abertos ou listagem de arquivos: cada link com número de
        edital vira página ou documento do grupo; o texto do parágrafo é o contexto."""
        soup = self.soup(html)
        corpo = soup.select_one("#content-core") or soup.select_one("#content") or soup
        for lixo in corpo.select("script, style"):
            lixo.decompose()
        for link in corpo.select("a[href]"):
            rotulo = clean_text(link.get_text(" ", strip=True))
            href = _url_limpa(urljoin(base, link.get("href") or ""))
            externo = self._external_id(rotulo, href)
            if not externo or not rotulo:
                continue
            grupo = self._grupo(grupos, externo)
            grupo["titulos"].setdefault(origem, rotulo)
            contexto = clean_text(link.parent.get_text(" ", strip=True)) if link.parent else ""
            if origem == "abertos" and not re.search(r"Inscri", contexto, re.I) and link.parent:
                # Às vezes a linha "Inscrições: …" fica no parágrafo seguinte, sem link.
                seguinte = link.parent.find_next_sibling()
                if seguinte is not None and not seguinte.select_one("a[href]"):
                    contexto += " " + clean_text(seguinte.get_text(" ", strip=True))
            if PPSUS.search(rotulo + " " + href):
                grupo["ppsus"] = True
            if origem == "abertos":
                linha = re.search(r"Inscri\S{0,5}\s*:?.*", contexto, re.I)
                grupo["linha_inscricoes"] = linha.group(0) if linha else None
            if _eh_pdf(href):
                self._documento(grupo, rotulo, href, None, contexto)
            elif href not in grupo["paginas"]:
                grupo["paginas"].append(href)

    def _documento(
        self, grupo: dict[str, Any], rotulo: str, url: str, publicado: str | None, contexto: str = "",
    ) -> None:
        if any(doc["url"] == url for doc in grupo["documentos"]):
            return
        grupo["documentos"].append({
            "title": rotulo or "Documento do edital", "url": url, "published": publicado,
            "contexto": contexto, "ordem": len(grupo["documentos"]),
        })
        if PPSUS.search(rotulo + " " + url):
            grupo["ppsus"] = True

    def _ler_busca_ppsus(self, grupos: dict[str, dict[str, Any]]) -> None:
        """A busca acha o PPSUS em qualquer pasta, do documento mais novo para o mais velho. No dia
        a dia, só a 1ª página; na carga histórica, até passar do corte dos 24 meses."""
        piso = (self.today - timedelta(days=RETENCAO_DIAS)).isoformat()
        url: str | None = BUSCA_PPSUS
        vistas: set[str] = set()
        while url and url not in vistas:
            vistas.add(url)
            soup = self.soup(self._html(url))
            datas = []
            for titulo in soup.select("dl.searchResults dt, #search-results dt, dt"):
                link = titulo.select_one("a[href]")
                if not link:
                    continue
                href = _url_limpa(urljoin(url, link.get("href") or ""))
                if "/editais/" not in urlsplit(href).path:
                    continue
                rotulo = clean_text(link.get_text(" ", strip=True))
                descricao = titulo.find_next_sibling("dd")
                linha = descricao.get_text(" ", strip=True) if descricao else ""
                publicado = re.search(r"publicado\s+em\s+(\d{1,2}/\d{1,2}/\d{4})", linha, re.I)
                data_doc = parse_br_date(publicado.group(1)) if publicado else None
                if data_doc:
                    datas.append(data_doc)
                externo = self._external_id(rotulo, href)
                if not externo or not PPSUS.search(rotulo + " " + href):
                    continue
                grupo = self._grupo(grupos, externo)
                grupo["ppsus"] = True
                grupo["titulos"].setdefault("busca", rotulo)
                if _eh_pdf(href):
                    self._documento(grupo, rotulo, href, data_doc)
                elif href not in grupo["paginas"]:
                    grupo["paginas"].append(href)
            if not self.historical or (datas and max(datas) < piso):
                break
            proximo = soup.select_one("a.proximo[href], .listingBar .next a[href], a.next[href]")
            url = urljoin(url, proximo.get("href")) if proximo else None

    def _ler_versao_mais_recente(self, grupo: dict[str, Any]) -> None:
        """Abre a página do edital (se houver) e lê o PDF da versão mais recente."""
        if not any(_eh_pdf(d["url"]) and not NAO_E_VERSAO.search(d["title"]) for d in grupo["documentos"]):
            for pagina in grupo["paginas"][:1]:
                try:
                    self._ler_pagina_do_edital(grupo, pagina)
                except (Bloqueio, OrcamentoEsgotado):
                    raise
                except Exception as erro:  # noqa: BLE001 - uma página quebrada não esconde o edital
                    self.avisos.append(f"página ilegível: {pagina} ({type(erro).__name__})")
        versao = self._versao_mais_recente(grupo["documentos"])
        if not versao:
            return
        try:
            grupo["texto_pdf"] = self._pdf_texto(versao["url"])
            grupo["versao"] = versao
        except (Bloqueio, OrcamentoEsgotado):
            raise
        except Exception as erro:  # noqa: BLE001 - um anexo quebrado não esconde o edital
            self.avisos.append(f"PDF ilegível: {versao['url']} ({type(erro).__name__})")

    def _ler_pagina_do_edital(self, grupo: dict[str, Any], pagina: str) -> None:
        soup = self.soup(self._html(pagina))
        titulo = soup.select_one("h1.documentFirstHeading, h1")
        if titulo:
            grupo["titulo_pagina"] = clean_text(titulo.get_text(" ", strip=True))
        publicado = soup.select_one(".documentPublished")
        grupo["publicado_pagina"] = parse_br_date(publicado.get_text(" ", strip=True)) if publicado else None
        corpo = soup.select_one("#parent-fieldname-text") or soup.select_one("#content-core") or soup
        for link in corpo.select("a[href]"):
            href = _url_limpa(urljoin(pagina, link.get("href") or ""))
            if _eh_pdf(href):
                self._documento(grupo, clean_text(link.get_text(" ", strip=True)), href, None)

    @staticmethod
    def _data_do_documento(documento: dict[str, Any]) -> str:
        """Data da versão: a da busca, a de "Retificado em dd/mm/aa" no rótulo ou a do prefixo do
        arquivo ("13_10_26-edital-70-2026…")."""
        if documento.get("published"):
            return documento["published"]
        retificado = re.search(r"retificad[oa]\s+em\s+(\d{1,2})[./](\d{1,2})[./](\d{2,4})", documento["title"], re.I)
        if retificado:
            return parse_br_date("/".join(retificado.groups())) or ""
        nome = urlsplit(documento["url"]).path.rsplit("/", 1)[-1]
        prefixo = re.match(r"(\d{2})[_-](\d{2})[_-](\d{2,4})[_-]", nome)
        return (parse_br_date("/".join(prefixo.groups())) or "") if prefixo else ""

    def _versao_mais_recente(self, documentos: list[dict[str, Any]]) -> dict[str, Any] | None:
        versoes = [
            d for d in documentos
            if _eh_pdf(d["url"]) and not NAO_E_VERSAO.search(d["title"] + " " + d["url"].rsplit("/", 1)[-1])
        ]
        if not versoes:
            return None
        return max(versoes, key=lambda d: (self._data_do_documento(d), d["ordem"]))

    # ------------------------------------------------------------------ montagem

    def _montar(self, grupo: dict[str, Any]) -> dict[str, Any] | None:
        texto = grupo["texto_pdf"]
        prazo = prazo_de_submissao(texto) if texto else None
        inferido = False
        if not prazo and grupo["linha_inscricoes"]:
            prazo, inferido = self._prazo_da_linha(grupo["linha_inscricoes"], grupo["ano"])
        if not prazo:
            # Listagem sem a página de prazos (fixture antiga): o contexto do documento mais recente.
            recentes = sorted(grupo["documentos"], key=lambda d: (self._data_do_documento(d), d["ordem"]), reverse=True)
            for documento in recentes:
                if documento["contexto"] and not NAO_E_VERSAO.search(documento["title"]):
                    prazo = prazo_de_submissao(documento["contexto"])
                    if prazo:
                        break
        hoje = self.today.isoformat()
        if prazo and prazo >= hoje:
            status = "open"
        elif prazo and inferido and grupo["linha_inscricoes"]:
            # Data sem ano ("Até 06/11") que, com o ano do edital, já passou, numa página que
            # diz que o prazo está aberto: não dá para saber.
            status = "unknown"
            prazo = None
        elif prazo:
            status = "closed"
        else:
            status = "unknown"
        piso = (self.today - timedelta(days=RETENCAO_DIAS)).isoformat()
        if status == "closed" and prazo < piso:
            return None
        if not prazo and not ({"abertos", "capa", "listagem"} & set(grupo["titulos"])):
            # Achado só pela busca e sem versão do edital lida (a 1ª página da busca já não mostra o
            # edital, só resultados): não reemite. O catálogo guarda a encerrada por 24 meses.
            return None

        titulo, parceiros_titulo = self._titulo_e_parceiros(grupo)
        ppsus = grupo["ppsus"] or bool(PPSUS.search(titulo))
        organization_types, partner_types = self._audience(titulo, texto, ppsus)
        parceiros = self._parceiros(texto, ppsus) + [p for p in parceiros_titulo if p not in {"FAPESQ", "PB"}]
        temas = self._themes(titulo)
        if ppsus:
            temas = list(dict.fromkeys([*temas, "saude", "pesquisa_sus"]))
        documentos = [
            {"title": d["title"], "url": d["url"], "published": d["published"]}
            for d in sorted(grupo["documentos"], key=lambda d: (self._data_do_documento(d), d["ordem"]))
        ]
        paginas = [{"title": "Página do edital", "url": p, "published": None} for p in grupo["paginas"]]
        source_url = (grupo["paginas"] or [d["url"] for d in documentos] or [HOME])[0]
        return opportunity(
            source_id=self.id, source_name=self.name, source_url=source_url,
            checked_at=self.checked_at, external_id=grupo["external_id"], title=titulo,
            funder="FAPESQ-PB", status=status,
            published=(lancamento(texto) if texto else None) or grupo.get("publicado_pagina"),
            deadline=prazo, organization_types=organization_types, partner_types=partner_types,
            partners=parceiros, geography=["PB", "BR"], themes=temas,
            instrument_type=self._instrument(titulo, texto, ppsus), repayable=False,
            program_budget=_valor_reais(texto) if texto else None, documents=paginas + documentos,
        )

    @staticmethod
    def _prazo_da_linha(linha: str, ano: int) -> tuple[str | None, bool]:
        """A linha "Inscrições: …" da página de prazos abertos. Sem ano ("Até 06/11 (17h)"), vale o
        ano do edital, e o prazo sai marcado como inferido."""
        prazo = prazo_de_submissao(linha)
        if prazo:
            return prazo, False
        sem_ano = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})(?!\s*/\s*\d)", linha)
        if sem_ano:
            try:
                return date(ano, int(sem_ano.group(2)), int(sem_ano.group(1))).isoformat(), True
            except ValueError:
                return None, False
        return None, False

    def _titulo_e_parceiros(self, grupo: dict[str, Any]) -> tuple[str, list[str]]:
        """Título estável entre as fontes da mesma rodada (página do edital, capa, página de prazos,
        busca) e as siglas parceiras do título ("CODATA/FAPESQ/PB" → CODATA)."""
        titulos = grupo["titulos"]
        versao = grupo.get("versao")
        candidato = (
            grupo["titulo_pagina"] or titulos.get("capa") or titulos.get("abertos") or titulos.get("listagem")
            or (versao["title"] if versao else None) or titulos.get("busca")
            or f"Edital FAPESQ {grupo['numero']}/{grupo['ano']}"
        )
        # As siglas logo depois do número, em todas as fontes: a página de prazos e o cabeçalho do
        # PDF escrevem "Nº 70/2026 – CODATA/FAPESQ/PB"; a capa, não.
        rotulos = [candidato, *titulos.values(), _normaliza(grupo["texto_pdf"][:400])]
        siglas = list(dict.fromkeys(
            s for rotulo in rotulos for bloco in SIGLAS_DO_NUMERO.findall(rotulo) for s in bloco.split("/")
        ))
        titulo = re.sub(
            r"\s*\((?:retificad|resultado|classifica|homologa|convoca|errata|prorroga|republica)[^)]*\)?\s*$",
            "", clean_text(candidato), flags=re.I,
        )
        titulo = re.sub(
            SIGLAS_DO_NUMERO.pattern + r"\s*(?:[–-]\s*)?", lambda m: m.group(0)[:m.start(1) - m.start(0)], titulo,
        )
        titulo = clean_text(titulo)
        if re.match(r"N\s*[º°o]", titulo):
            titulo = "Edital " + titulo
        return (titulo if len(titulo) >= 8 else f"Edital FAPESQ {grupo['numero']}/{grupo['ano']}"), siglas

    @staticmethod
    def _parceiros(texto: str, ppsus: bool) -> list[str]:
        """No PPSUS, Decit/MS, CNPq e a SES da PB, lidos no edital; sem o edital lido, os do
        desenho do programa (Decit/SECTICS/MS pelo CNPq, mais a SES da UF)."""
        if not ppsus:
            return []
        texto = _normaliza(texto)
        if not texto:
            return ["Decit/MS", "CNPq", "SES-PB"]
        parceiros = []
        if re.search(r"Decit|Minist[eé]rio\s+da\s+Sa[uú]de", texto):
            parceiros.append("Decit/MS")
        if re.search(r"\bCNPq\b", texto):
            parceiros.append("CNPq")
        if re.search(
            r"SES\s*[/-]\s*PB|Secretaria\s+(?:de\s+Estado\s+)?d[ae]\s+Sa[uú]de\s+do\s+Estado\s+da\s+Para[ií]ba", texto, re.I,
        ):
            parceiros.append("SES-PB")
        return parceiros or ["Decit/MS", "CNPq", "SES-PB"]

    @staticmethod
    def _audience(titulo: str, texto: str, ppsus: bool) -> tuple[list[str], list[str]]:
        """Proponentes e parceiras.

        PPSUS: o proponente é o pesquisador, com vínculo numa executora (IES pública ou privada,
        ICT pública ou privada sem fins lucrativos, empresa pública); a empresa privada só entra
        como parceira, com termo de compromisso e contrapartida. Nos demais editais, o título diz
        quem se inscreve: a palavra "empresa" solta no texto não basta (antes era assim, e todo
        edital de seleção de bolsista saía como aberto a empresas).
        """
        if ppsus:
            tipos = ["pesquisador", "universidade", "ict", "ict_privada"]
            secao = None
            if texto:
                normal = _normaliza(texto)
                inicio = re.search(r"Quanto\s+[àa]\s+Institui[çc][ãa]o\s+Executora", normal, re.I)
                secao = normal[inicio.start():inicio.start() + 2000] if inicio else None
            if secao:
                tipos = ["pesquisador"]
                if re.search(r"ensino\s+superior|\bIES\b", secao, re.I):
                    tipos.append("universidade")
                if re.search(r"\bICTs?\b|Cient[ií]fica,?\s+Tecnol[óo]gica", secao, re.I):
                    tipos.append("ict")
                    if re.search(r"privad[oa]s?,?\s*sem\s+fins\s+lucrativos", secao, re.I):
                        tipos.append("ict_privada")
                if re.search(r"empresas?\s+p[uú]blicas?", secao, re.I):
                    tipos.append("outros")
            com_empresa = not texto or re.search(r"participa[çc][ãa]o\s+de\s+empresas?", texto, re.I)
            parceiras = ["empresa"] if com_empresa else []
            return tipos, parceiras

        folded = ascii_fold(titulo)
        tipos: list[str] = []
        if re.search(r"subvencao|tecnova|centelha|embrapii|\bempresas?\b(?!\s+(?:junior|juniores|publica))", folded):
            tipos.append("empresa")
        if "startup" in folded:
            tipos.append("startup")
        if re.search(r"pesquisador|pos-?doutorado|doutorado|mestrado|eventos", folded):
            tipos.append("pesquisador")
        if re.search(
            r"selecao|seletivo|bolsas?\b|bolsistas?|estudantes|graduandos|equipe|oficineir|tradutor|professores"
            r"|coordenadores|cadastro de reserva",
            folded,
        ):
            tipos.append("pessoa_fisica")
        if re.search(r"laboratorios|\bicts?\b|instituic|universidades?", folded):
            tipos += ["ict", "universidade"]
        if re.search(r"empresas? junior|empresas juniores", folded):
            tipos.append("outros")
        return list(dict.fromkeys(tipos)) or ["pesquisador", "ict", "universidade"], []

    @staticmethod
    def _instrument(titulo: str, texto: str, ppsus: bool) -> str:
        folded = ascii_fold(titulo)
        if ppsus:
            # Auxílio a projeto de pesquisa (contrato 2.2). O PPSUS prevê bolsas de IC, mas o objeto é
            # o projeto.
            return "auxilio_pesquisa"
        if re.search(r"subvencao|tecnova|centelha", folded):
            return "subvencao_economica"
        if re.search(r"\bpremio\b", folded):
            return "premio"
        if re.search(
            r"bolsas?\b|bolsistas?|selecao|seletivo|equipe|estudantes|graduandos|pos-?doutorado|mestrado|doutorado",
            folded,
        ):
            return "bolsa"
        if re.search(r"auxilio|apoio a projetos|projetos? de pesquisa", folded):
            return "auxilio_pesquisa"
        corpo = ascii_fold(texto)
        if "subvencao" in corpo:
            return "subvencao_economica"
        if re.search(r"\bbolsas?\b", corpo):
            return "bolsa"
        return "outros"

    @staticmethod
    def _themes(titulo: str) -> list[str]:
        """Temas do título (antes vinham do texto da página inteira, com o menu do site, e todo
        edital saía com saúde, agroindústria e tecnologia)."""
        folded = ascii_fold(titulo)
        temas = [canonical for pattern, canonical in THEME_ALIASES if re.search(pattern, folded)]
        for pattern, tema in ((r"inovac", "inovacao"), (r"tecnolog", "tecnologia")):
            if re.search(pattern, folded):
                temas.append(tema)
        return list(dict.fromkeys(temas))

    # ------------------------------------------------------------------ identidade

    @staticmethod
    def _external_id(rotulo: str, href: str = "") -> str | None:
        """`NN-AAAA` do número do edital no rótulo ("EDITAL Nº 70/2026", "Edital nº 67 de 2026",
        "N° 02/2025") ou no nome do arquivo ("edital-n0-02_2025-…"); nunca do resto do título.
        Sem zero à esquerda, como os ids do catálogo."""
        for padrao, texto in ((EDITAL_NUMERO, rotulo), (N_NUMERO, rotulo), (URL_NUMERO, urlsplit(href).path)):
            achado = padrao.search(texto)
            if achado and int(achado.group(1)) > 0:
                return f"{int(achado.group(1))}-{achado.group(2)}"
        return None

    # Compatibilidade com o nome antigo, usado por testes e scripts.
    _deadline = staticmethod(prazo_de_submissao)
