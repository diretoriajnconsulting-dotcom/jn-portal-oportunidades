"""Chamadas do CNPq, inclusive as do Ministério da Saúde (Decit/SECTICS e outras secretarias).

O CNPq é o canal real das chamadas de pesquisa do MS: o Decit não as anuncia no gov.br/saude
(estudo de 14/10/2026, `radar-fontes-saude/estudo/ms_decit.md`). O site é Plone 4, com o HTML
pronto no servidor e três coleções de URL fixa, lidas todo dia:

  · abertas para submissão;
  · análise em andamento;
  · resultados publicados (cerca de 980 itens, 20 por página; no dia a dia, só a 1ª página).

Cada chamada é um `div.item` com título, resumo, documentos e a linha "Inscrições: dd/mm/aaaa a
dd/mm/aaaa" (só nas páginas de 2025 em diante). O link de compartilhamento traz o UID Plone
(`resolveuid/<32 hex>`), que junta a mesma chamada nas três coleções.

Identidade: o número vem da pasta Plone (`chamada-no-30-2025` → `30-2025`; `chamamento-no-01-2026`
→ `chamamento-1-2026`), nunca do título; sem pasta numerada, do "Nº NN/AAAA" do título. Sem zero à
esquerda, como os ids que já estão no catálogo (`cnpq-30-2026`).

Situação:
  · abertas → `open` com prazo lido e ≥ hoje; prazo lido e vencido → `closed`; sem prazo → `unknown`;
  · em análise e resultados → `closed` (a inscrição já encerrou).

"Publicado em" não é lançamento: as chamadas de 2024 foram migradas em abril de 2025 e aparecem
todas com essa data. Lançamento e prazo vêm da linha "Inscrições" ou do cronograma do PDF.

Carga histórica (`RADAR_CARGA_HISTORICA=1`): os últimos 24 meses pela `busca_de_resultados`, ano a
ano, conferindo os buracos da numeração com uma busca pontual por "NN/AAAA". A paginação das
coleções e da busca é instável (itens repetidos de uma página para outra), por isso a junção é
pela pasta e pelo UID, nunca pela posição.
"""

from __future__ import annotations

import io
import json
import re
import time
import urllib.error
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from pypdf import PdfReader

from funding_intelligence.adapters.base import BaseAdapter
from funding_intelligence.models import THEME_ALIASES, ascii_fold, clean_text, opportunity, parse_br_date, slug


BASE = "https://www.gov.br/cnpq/pt-br/chamadas/"
COLECOES = (
    ("abertas", BASE + "abertas-para-submissao"),
    ("analise", BASE + "analise-em-andamento"),
    ("resultados", BASE + "resultados-publicados"),
)
BUSCA = BASE + "busca_de_resultados"

# Orçamento de pedidos (CONSTRUCAO.md): 25 por dia, dos quais no máximo 20 de PDF; a carga
# histórica, que roda uma vez, vai até cerca de 150.
ORCAMENTO_DIA = 25
ORCAMENTO_PDF_DIA = 20
ORCAMENTO_HISTORICO = 150
PAUSA_S = 1.0
RETENCAO_DIAS = 730
# Na carga histórica, os números de um ano são lidos do maior para o menor. Três chamadas
# seguidas com prazo anterior ao corte dos 24 meses encerram a descida naquele ano: o CNPq numera
# pela ordem de lançamento, e as janelas duram de um a três meses.
ANTIGAS_SEGUIDAS_PARA_PARAR = 3
# Buracos na numeração conferidos por ano. Número que o CNPq não usou custa um pedido à toa; o
# teto impede que um ano cheio de buracos gaste o orçamento da carga.
BUSCAS_PONTUAIS_POR_ANO = 15

DATA = r"\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})"
JANELA = re.compile(
    rf"Inscri[cç][oõ]es?(?:\s+\d+\S*\s+Rodada)?\s*:?\s*(?:de\s+|no\s+per[ií]odo\s+de\s+)?"
    rf"({DATA})\s*(?:a|at[eé]|-|–)\s*({DATA})",
    re.I,
)
JANELA_ATE = re.compile(rf"Inscri[cç][oõ]es?\s*:?\s*at[eé]\s*(?:o\s+dia\s+)?({DATA})", re.I)
CABECALHO_CRONOGRAMA = re.compile(
    r"Cronograma\.?\s*(?:geral\s*)?(?:FASES|ATIVIDADES?|ETAPAS?|PER[IÍ]ODO|DATAS?)\b", re.I,
)
LANCAMENTO = re.compile(rf"Lan[cç]amento\s+da\s+Chamada[^0-9]{{0,160}}?({DATA})", re.I)
LIMITE = re.compile(
    rf"(?:Data\s+limite|Prazo(?:\s+final)?)\s+para\s+(?:a\s+)?(?:submiss[aã]o|envio)\s+d[ae]s?\s+propostas?"
    rf"[^0-9]{{0,80}}?({DATA})",
    re.I,
)
VALOR_GLOBAL = re.compile(
    r"valor\s+global\s+(?:de\s+|estimado\s+(?:de\s+|em\s+)?)?(?:at[eé]\s+)?R\$\s*([\d.]+,\d{2})", re.I,
)

# Pasta Plone da chamada: chamada-no-30-2025, chamada-ndeg-46-2024, chamamento-no-01-2026 e
# chamada-ndeg-30-2024-1 (o sufixo `-1` é cópia do Plone e não entra no id).
PASTA = re.compile(
    r"/chamadas-(\d{4})/(chamada|chamamento)-n(?:o|deg)-0*(\d{1,3})-(\d{4})(?:-\d+)?(?:/|$)", re.I,
)
PASTA_LIVRE = re.compile(r"/chamadas-(\d{4})/([a-z0-9][a-z0-9-]{2,})(?:/|$)", re.I)
NUMERO_TITULO = re.compile(r"\bN\s*[º°o]?\s*\.?\s*0*(\d{1,3})\s*/\s*(20\d{2})\b", re.I)
NUMERO_SOLTO = re.compile(r"(?<![\d/])0*(\d{1,3})\s*/\s*(20\d{2})\b")

# Órgãos do Ministério da Saúde que aparecem no bloco de siglas do título ou na 1ª frase do edital.
# Os departamentos vêm antes das secretarias no rótulo do parceiro ("Daevs/SVSA/MS").
MS_DEPARTAMENTOS = {"decit": "Decit", "daevs": "Daevs", "dgits": "DGITS", "dgitis": "DGITIS", "ceis": "CEIS"}
MS_SECRETARIAS = {
    "sctie": "SCTIE", "sectics": "SECTICS", "svsa": "SVSA", "saes": "SAES", "saps": "SAPS",
    "sgtes": "SGTES", "sesai": "SESAI",
}
MS_ORGAOS = {**MS_DEPARTAMENTOS, **MS_SECRETARIAS, "ms": "MS"}
MS_NO_TEXTO = re.compile(
    r"\b(?:Decit|SCTIE|SECTICS|SVSA|Daevs|SAES|SAPS|SGTES|SESAI|DGITI?S)\b(?:\s*[/-]\s*[A-Za-z]+)*\s*[/-]\s*MS\b"
    r"|Minist[eé]rio\s+da\s+Sa[uú]de|\bPPSUS\b",
)
# Siglas escritas sem duas maiúsculas, que o filtro genérico deixaria de fora.
SIGLAS_CONHECIDAS = {"fiocruz": "Fiocruz", "faps": "FAPs", "minc": "MinC", "mmulheres": "MMulheres"}
NAO_PARCEIROS = {"cnpq", "chamada", "publica", "pública", "chamamento", "edital"}

REDES_SOCIAIS = ("facebook.com", "twitter.com", "linkedin.com", "whatsapp.com", "x.com/")


class Bloqueio(RuntimeError):
    """403, 429, CAPTCHA ou Cloudflare: o adaptador para naquele host e registra."""


class OrcamentoEsgotado(RuntimeError):
    """O limite de pedidos da rodada acabou; o que já foi lido segue para o catálogo."""


def _parece_bloqueio(corpo: bytes) -> bool:
    """Página de desafio, não conteúdo. O gov.br cita "cloudflare" no HTML normal (cdnjs), por isso
    o teste olha o título e as marcas próprias das páginas de desafio."""
    inicio = corpo[:6000].lower()
    titulo = re.search(rb"<title>([^<]*)", inicio)
    titulo_texto = titulo.group(1) if titulo else b""
    if any(x in titulo_texto for x in (b"just a moment", b"attention required", b"captcha", b"access denied")):
        return True
    return any(x in inicio for x in (b"cf-chl", b"challenge-platform", b"h-captcha", b"g-recaptcha"))


def _valor_reais(texto: str) -> float | None:
    match = VALOR_GLOBAL.search(texto)
    if not match:
        return None
    try:
        return float(match.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


FIM_DE_SECAO = re.compile(
    r"Quanto\s+[àa]o?s?\s|\s\d{1,2}\s*[-–.]\s*(?:Recursos|Itens|Submiss|Julgamento|Cronograma|Crit[eé]rios|Disposi)",
    re.I,
)


def _secao(texto: str, cabecalho: str, tamanho: int = 2000) -> str | None:
    """O trecho de um item do edital ("Quanto à Instituição de Execução…") até o próximo "Quanto
    a…" ou o próximo item de primeiro nível."""
    match = re.search(cabecalho, texto, re.I)
    if not match:
        return None
    resto = texto[match.start():match.start() + tamanho]
    fim = FIM_DE_SECAO.search(resto, 20)
    return resto[:fim.start()] if fim else resto


def _elegibilidade(texto: str) -> tuple[list[str], list[str]] | None:
    """Proponentes (executoras) e parceiras pelas regras do edital, ou None quando não há o item.

    Executora vai em `organization_types`; quem só participa vai em `partner_types` (contrato 2.2).
    Na Chamada 30/2025 a executora é ICT, empresa pública ou OSC, e a empresa privada só entra como
    instituição participante; na 31/2025 e na 32/2025 a empresa privada pôde ser executora.
    """
    texto = re.sub(r"\s+", " ", texto)
    secao = _secao(texto, r"Quanto\s+[àa]\s+Institui[çc][ãa]o\s+(?:de\s+Execu[çc][ãa]o|Executora)")
    if not secao:
        return None
    tipos: list[str] = []
    # `\s*`: o texto do PDF às vezes cola as palavras ("direito privadosem fins lucrativos").
    privada_sem_fins = re.search(r"privad[oa]s?,?\s*sem\s+fins\s+lucrativos", secao, re.I)
    if re.search(r"\bICTs?\b|Institui[çc][ãa]o\s+Cient[ií]fica", secao, re.I):
        tipos += ["ict", "universidade"]
        if privada_sem_fins and not re.search(r"\bICTs?\s+p[uú]blicas?\b(?!\s+ou)", secao, re.I):
            tipos.append("ict_privada")
    if re.search(r"Institui[çc][ãa]o\s+de\s+Ensino\s+Superior|\bIES\b|universidade", secao, re.I):
        tipos.append("universidade")
    if re.search(r"institui[çc][ãa]o\s+privada\s+sem\s+fins\s+lucrativos", secao, re.I):
        tipos.append("ict_privada")
    if re.search(r"empresas?\s+privadas?", secao, re.I):
        tipos.append("empresa")
    if re.search(r"organiza[çc][ãa]o\s+da\s+sociedade\s+civil|\bOSCs?\b", secao, re.I):
        tipos.append("osc")
    if re.search(r"hospita", secao, re.I):
        tipos.append("hospital")
    if re.search(r"empresas?\s+p[uú]blicas?", secao, re.I):
        # Empresa pública executora: não há tipo próprio no contrato.
        tipos.append("outros")
    proponente = _secao(texto, r"Quanto\s+ao\s+Proponente")
    if not (proponente and re.search(r"Representante\s+Institucional", proponente, re.I)):
        # O auxílio do CNPq é individual, em nome do coordenador (Termo de Outorga).
        tipos.insert(0, "pesquisador")
    if not tipos:
        return None

    parceiras: list[str] = []
    secao_parceira = _secao(texto, r"Quanto\s+[àa]s?\s+Institui[çc](?:[ãa]o|[õo]es)\s+Parceiras?")
    if secao_parceira:
        if re.search(r"empres", secao_parceira, re.I):
            parceiras.append("empresa")
        if re.search(r"terceiro\s+setor|sociedade\s+civil|\bOSCs?\b", secao_parceira, re.I):
            parceiras.append("osc")
        if re.search(r"[óo]rg[ãa]os?\s+de\s+governo|administra[çc][ãa]o\s+p[uú]blica", secao_parceira, re.I):
            parceiras += ["estado", "municipio"]
    elif "empresa" not in tipos and re.search(
        # Só com a empresa nomeada (revisão do supervisor, 14/10/2026). "Instituições participantes", sozinho,
        # não diz o tipo: nos editais do CNPq costuma ser outra ICT da equipe. Marcar a empresa como parceira
        # por isso prometeria a ela uma porta que o texto não abre.
        r"participa[çc][ãa]o\s+de\s+empresas?"
        r"|empresas?\s+(?:\S+\s+){0,3}(?:colaboradoras?|parceiras?|participantes?|intervenientes?)",
        texto, re.I,
    ):
        parceiras.append("empresa")
    return list(dict.fromkeys(tipos)), [x for x in dict.fromkeys(parceiras) if x not in tipos]


def _cronograma(texto: str) -> tuple[str | None, str | None]:
    """(lançamento, prazo de submissão) do cronograma do PDF.

    Vale a última tabela de cronograma do documento: nas retificações a tabela nova vem depois da
    antiga. Nunca o maior valor de todas as datas, que pegaria o prazo de recurso.
    """
    texto = re.sub(r"\s*/\s*", "/", re.sub(r"\s+", " ", texto))
    regioes = [texto[m.start():m.start() + 3000] for m in CABECALHO_CRONOGRAMA.finditer(texto)] or [texto]
    for regiao in reversed(regioes):
        limites = LIMITE.findall(regiao)
        if limites:
            lancamento = LANCAMENTO.search(regiao)
            return (parse_br_date(lancamento.group(1)) if lancamento else None), parse_br_date(limites[-1])
    lancamento = LANCAMENTO.search(texto)
    return (parse_br_date(lancamento.group(1)) if lancamento else None), None


class CnpqAdapter(BaseAdapter):
    id = "cnpq"
    name = "CNPq"
    url = "https://www.gov.br/cnpq/pt-br/chamadas/abertas-para-submissao"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.pedidos = 0
        self.pedidos_pdf = 0
        self.avisos: list[str] = []
        self._ultimo_pedido: dict[str, float] = {}
        self._bloqueados: set[str] = set()
        self._pdfs: dict[str, str] = {}
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

    def _pedir(self, url: str, *, pdf: bool = False) -> bytes:
        pdfs_esgotados = pdf and not self.historical and self.pedidos_pdf >= ORCAMENTO_PDF_DIA
        if self.pedidos >= self.limite_pedidos or pdfs_esgotados:
            raise OrcamentoEsgotado(url)
        host = urlsplit(url).netloc
        if host in self._bloqueados:
            raise Bloqueio(f"host bloqueado nesta rodada: {host}")
        self.pedidos += 1
        self.pedidos_pdf += int(pdf)
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
        """Texto do PDF, lido uma vez por rodada. Na fixture, o PDF é um `.txt` já extraído."""
        if url in self._pdfs:
            return self._pdfs[url]
        corpo = self._pedir(url, pdf=True)
        if corpo[:5] == b"%PDF-":
            reader = PdfReader(io.BytesIO(corpo))
            texto = "\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            texto = corpo.decode("utf-8", errors="replace")
        self._pdfs[url] = texto
        return texto

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        if self.avisos:
            # Falha de uma coleção secundária ou orçamento esgotado: a rodada segue saudável, com aviso.
            snap["error"] = "; ".join(self.avisos)[:500]
        return snap

    # ------------------------------------------------------------------ coleta

    def collect(self) -> list[dict[str, Any]]:
        if self.fixture and not self._fixture_dir():
            # Fixture de arquivo único (testes antigos e `--fixture` do CLI): é a página das abertas.
            chamadas = self._itens_da_pagina(Path(self.fixture).read_text(encoding="utf-8"), self.url, "abertas")
            return [item for item in (self._montar(c, ler_pdf=False) for c in chamadas) if item]

        chamadas: dict[str, dict[str, Any]] = {}
        lidas = 0
        for fase, url in COLECOES:
            try:
                html = self._html(url)
            except Bloqueio:
                raise
            except Exception as erro:  # noqa: BLE001 - uma coleção secundária não derruba a rodada
                if fase == "abertas":
                    raise
                self.avisos.append(f"{fase}: {type(erro).__name__}: {erro}")
                continue
            achadas = self._itens_da_pagina(html, url, fase)
            if not achadas:
                # Plone 4 legado: uma migração do gov.br pode mudar o HTML sem dar erro.
                self.avisos.append(f"{fase}: nenhuma chamada lida (mudou o leiaute?)")
            lidas += len(achadas)
            for chamada in achadas:
                self._juntar(chamadas, chamada)
        if not lidas:
            # Nenhuma chamada nas três coleções é leiaute novo, não falta de edital: falhar mantém o
            # catálogo anterior (marcado como defasado) em vez de encerrar tudo.
            raise RuntimeError("nenhuma chamada lida nas três coleções do CNPq")

        itens: list[dict[str, Any]] = []
        prioridade = {"abertas": 0, "analise": 1, "resultados": 2}
        for chamada in sorted(chamadas.values(), key=lambda c: prioridade[c["fase"]]):
            item = self._montar(chamada)
            if item:
                itens.append(item)
        if self.historical:
            itens.extend(self._historico(chamadas, itens))
        return itens

    @staticmethod
    def _chave(chamada: dict[str, Any]) -> str:
        return chamada["external_id"] or chamada["uid"] or chamada["url"]

    def _juntar(self, chamadas: dict[str, dict[str, Any]], nova: dict[str, Any]) -> None:
        """Junta pela identidade (pasta) e pelo UID. A fase mais adiantada vence: uma chamada que
        aparece em "abertas" e em "análise" no mesmo dia já fechou a submissão."""
        ordem = {"abertas": 0, "analise": 1, "resultados": 2}
        chave = self._chave(nova)
        por_uid = next((k for k, c in chamadas.items() if nova["uid"] and c["uid"] == nova["uid"]), None)
        chave = por_uid or chave
        atual = chamadas.get(chave)
        if not atual:
            chamadas[chave] = nova
            return
        if ordem[nova["fase"]] > ordem[atual["fase"]]:
            nova["documentos"] = nova["documentos"] or atual["documentos"]
            chamadas[chave] = nova

    # ------------------------------------------------------------------ leitura das páginas

    def _itens_da_pagina(self, html: str, url: str, fase: str) -> list[dict[str, Any]]:
        soup = self.soup(html)
        core = soup.select_one("#content-core") or soup
        blocos = core.select("div.item")
        if blocos:
            return [c for c in (self._ler_bloco(b, url, fase) for b in blocos) if c]
        # Leiaute antigo: título em h2..h5 e o texto nos irmãos seguintes.
        return [c for c in (self._ler_por_titulo(h, url, fase) for h in core.select("h2, h3, h4, h5")) if c]

    def _ler_bloco(self, bloco: Any, base: str, fase: str) -> dict[str, Any] | None:
        titulo_el = bloco.select_one("h2, h3, h4, h5")
        if not titulo_el:
            return None
        title = clean_text(titulo_el.get_text(" ", strip=True))
        link = titulo_el.select_one("a[href]")
        url = urljoin(base, link.get("href")) if link else None
        uid = re.search(r"resolveuid/([0-9a-f]{32})", str(bloco))
        for lixo in bloco.select("script, style, .social-links, .documentByLine, .keywords"):
            lixo.decompose()
        resumo_el = bloco.select_one("#parent-fieldname-text p, p")
        documentos = self._documentos(bloco.select("a[href]"), base, url)
        return self._chamada(
            title, url, uid.group(1) if uid else None, fase,
            clean_text(bloco.get_text(" ", strip=True)),
            clean_text(resumo_el.get_text(" ", strip=True)) if resumo_el else None, documentos,
        )

    def _ler_por_titulo(self, heading: Any, base: str, fase: str) -> dict[str, Any] | None:
        title = clean_text(heading.get_text(" ", strip=True))
        partes: list[str] = []
        links: list[Any] = []
        for irmao in heading.next_siblings:
            if getattr(irmao, "name", None) in {"h2", "h3", "h4", "h5"}:
                break
            if hasattr(irmao, "select"):
                partes.append(clean_text(irmao.get_text(" ", strip=True)))
                links.extend(irmao.select("a[href]"))
        texto = " ".join(partes)
        objetivo = re.search(r"Objetivo\s*:?\s*(.+?)(?=Inscri[cç][oõ]es|$)", texto, re.I)
        documentos = self._documentos(links, base, None)
        url = documentos[0]["url"] if documentos else None
        return self._chamada(title, url, None, fase, texto, objetivo.group(1) if objetivo else None, documentos)

    def _documentos(self, links: list[Any], base: str, pagina: str | None) -> list[dict[str, Any]]:
        documentos: list[dict[str, Any]] = []
        if pagina:
            documentos.append({"title": "Página da chamada", "url": pagina, "published": None})
        vistos = {pagina}
        for link in links:
            href = urljoin(base, link.get("href") or "")
            if (
                not href.startswith("http") or href in vistos
                or any(rede in href for rede in REDES_SOCIAIS)
                or "@@search" in href or "SearchableText=%23" in href
            ):
                continue
            vistos.add(href)
            documentos.append({
                "title": clean_text(link.get_text(" ", strip=True)) or "Documento da chamada",
                "url": href, "published": None,
            })
        return documentos

    def _chamada(
        self, title: str, url: str | None, uid: str | None, fase: str, texto: str,
        resumo: str | None, documentos: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        external = self._external_id(url, title)
        if not external or not title:
            return None
        return {
            "title": title, "url": url, "uid": uid, "fase": fase, "texto": texto,
            "resumo": resumo, "documentos": documentos, "external_id": external,
            "ano": self._ano(external, url),
        }

    # ------------------------------------------------------------------ identidade

    @staticmethod
    def _external_id(url: str | None, title: str) -> str | None:
        """`NN-AAAA` (ou `chamamento-NN-AAAA`) da pasta Plone; sem pasta numerada, do título.

        Títulos sem a palavra "chamada" ("MCTI/CNPq/DFG Nº 14/2025") continuam valendo. Chamamento
        e chamada são séries separadas de números. O número vai sem zero à esquerda ("Nº 008/2025"
        → `8-2025`), como os ids que já estão no catálogo.
        """
        caminho = urlsplit(url).path if url else ""
        pasta = PASTA.search(caminho)
        if pasta:
            tipo, numero, ano = pasta.group(2).lower(), int(pasta.group(3)), pasta.group(4)
        else:
            numero_titulo = NUMERO_TITULO.search(title) or NUMERO_SOLTO.search(title)
            if not numero_titulo:
                livre = PASTA_LIVRE.search(caminho)
                # Sem número em lugar nenhum: a pasta Plone é o id estável (não muda com o título).
                return slug(livre.group(2))[:80] if livre else None
            tipo = "chamamento" if re.search(r"\bchamamento\b", title, re.I) else "chamada"
            numero, ano = int(numero_titulo.group(1)), numero_titulo.group(2)
        if numero == 0:
            return None
        return f"{'chamamento-' if tipo == 'chamamento' else ''}{numero}-{ano}"

    @staticmethod
    def _ano(external_id: str | None, url: str | None = None) -> int | None:
        """O ano da chamada: o do número ("30-2025") ou, sem número, o da pasta do ano no caminho."""
        match = re.search(r"-(20\d{2})$", external_id or "")
        if match:
            return int(match.group(1))
        pasta = re.search(r"/chamadas-(20\d{2})/", urlsplit(url).path if url else "")
        return int(pasta.group(1)) if pasta else None

    # ------------------------------------------------------------------ montagem

    def _janela(self, texto: str) -> tuple[str | None, str | None]:
        texto = re.sub(r"\s*/\s*", "/", texto)
        janelas = [(parse_br_date(a), parse_br_date(b)) for a, b in JANELA.findall(texto)]
        janelas = [(a, b) for a, b in janelas if b]
        if not janelas:
            ate = JANELA_ATE.search(texto)
            return None, parse_br_date(ate.group(1)) if ate else None
        hoje = self.today.isoformat()
        # Várias rodadas: vale o próximo prazo ainda por vir; se todos passaram, o último.
        futuros = sorted(b for _, b in janelas if b >= hoje)
        prazo = futuros[0] if futuros else max(b for _, b in janelas)
        return janelas[0][0], prazo

    @staticmethod
    def _eh_ms(chamada: dict[str, Any]) -> bool:
        return bool(MS_NO_TEXTO.search(chamada["title"] + " " + (chamada["resumo"] or ""))) or any(
            p.endswith("/MS") or p == "MS" for p in CnpqAdapter._parceiros(chamada["title"])
        )

    @staticmethod
    def _documento_principal(documentos: list[dict[str, Any]]) -> dict[str, Any] | None:
        pdfs = [d for d in documentos if urlsplit(d["url"]).path.lower().endswith(".pdf")]
        fora = r"resultado|anexo|faq|retifica|cronograma|errata|fluxograma|pergunta|recurso|lista|homolog|plano|modelo"
        for doc in pdfs:
            if slug(doc["title"]) in {"chamada", "chamada-publica", "edital", "chamamento", "chamamento-publico"}:
                return doc
        for doc in pdfs:
            nome = ascii_fold(doc["title"] + " " + urlsplit(doc["url"]).path.rsplit("/", 1)[-1])
            if re.search(r"chamada|edital|chamamento", nome) and not re.search(fora, ascii_fold(doc["title"])):
                return doc
        return next((d for d in pdfs if not re.search(fora, ascii_fold(d["title"]))), None)

    def _ler_pdf(self, documento: dict[str, Any] | None) -> str:
        if not documento:
            return ""
        try:
            return self._pdf_texto(documento["url"])
        except (Bloqueio, OrcamentoEsgotado):
            raise
        except Exception as erro:  # noqa: BLE001 - um anexo quebrado não esconde a chamada
            self.avisos.append(f"PDF ilegível: {documento['url']} ({type(erro).__name__})")
            return ""

    def _montar(self, chamada: dict[str, Any], ler_pdf: bool = True) -> dict[str, Any] | None:
        published, deadline = self._janela(chamada["texto"])
        fase = chamada["fase"]
        texto_pdf = ""
        # PDF só para item aberto, item sem prazo na página ou chamada do MS (que é o motivo desta
        # ampliação). A regra é a mesma no dia a dia e na carga histórica, para a rodada diária não
        # apagar o que a carga leu.
        if ler_pdf and (fase == "abertas" or not deadline or self._eh_ms(chamada)):
            try:
                texto_pdf = self._ler_pdf(self._documento_principal(chamada["documentos"]))
                if not deadline:
                    lancamento, prazo = _cronograma(texto_pdf) if texto_pdf else (None, None)
                    retificacoes = [
                        d for d in chamada["documentos"]
                        if re.search(r"cronograma|retifica", ascii_fold(d["title"]))
                        and urlsplit(d["url"]).path.lower().endswith(".pdf")
                    ]
                    if retificacoes:
                        # A retificação mais recente (a última da página) manda no prazo.
                        _, prazo_retificado = _cronograma(self._ler_pdf(retificacoes[-1]))
                        prazo = prazo_retificado or prazo
                    published, deadline = published or lancamento, prazo
                elif not published and texto_pdf:
                    published = _cronograma(texto_pdf)[0]
            except OrcamentoEsgotado:
                self._avisar_orcamento()
            except Bloqueio as erro:
                self.avisos.append(str(erro))

        hoje = self.today.isoformat()
        if fase == "abertas":
            status = "open" if deadline and deadline >= hoje else ("closed" if deadline else "unknown")
        else:
            status = "closed"
        piso = (self.today - timedelta(days=RETENCAO_DIAS)).isoformat()
        ano = chamada["ano"]
        if status == "closed" and (
            (deadline and deadline < piso) or (not deadline and (ano or 0) <= int(piso[:4]))
        ):
            # Fora da janela de 24 meses, ou sem prazo e de um ano que pode estar fora dela.
            return None

        title = chamada["title"]
        parceiros = self._parceiros(title, chamada["resumo"] or "", texto_pdf[:1500])
        elegibilidade = _elegibilidade(texto_pdf) if texto_pdf else None
        if elegibilidade:
            organization_types, partner_types = elegibilidade
        else:
            # Sem o edital lido, o comportamento de antes, sem inventar.
            organization_types, partner_types = self._audience(title, chamada["texto"]), []
        temas = self._themes(" ".join([title, chamada["resumo"] or ""]))
        if any(p.endswith("/MS") or p == "MS" for p in parceiros) and "saude" not in temas:
            temas.append("saude")
        return opportunity(
            source_id=self.id, source_name=self.name, source_url=chamada["url"] or self.url,
            checked_at=self.checked_at, external_id=chamada["external_id"], title=title,
            funder="CNPq", status=status, published=published, deadline=deadline,
            organization_types=organization_types, partner_types=partner_types, partners=parceiros,
            geography=["BR"], themes=temas, instrument_type=self._instrument(title, chamada["resumo"] or ""),
            repayable=False, program_budget=_valor_reais(re.sub(r"\s+", " ", texto_pdf)) if texto_pdf else None,
            documents=chamada["documentos"], description=chamada["resumo"],
        )

    def _avisar_orcamento(self) -> None:
        aviso = f"orçamento de pedidos esgotado ({self.pedidos} pedidos)"
        if aviso not in self.avisos:
            self.avisos.append(aviso)

    # ------------------------------------------------------------------ carga histórica

    def _resultados_da_busca(self, html: str, url: str) -> tuple[list[dict[str, Any]], str | None]:
        soup = self.soup(html)
        core = soup.select_one("#content-core") or soup
        achados = []
        for artigo in core.select("article"):
            link = artigo.select_one("h2 a[href], h3 a[href]")
            if not link:
                continue
            href = urljoin(url, link.get("href"))
            if "/todas-as-chamadas/" not in href:
                continue
            tags = " ".join(a.get_text(" ", strip=True) for a in artigo.select(".keywords a"))
            fase = "abertas" if "#abertas" in tags else "analise" if "#analise" in tags else "resultados"
            achados.append({"title": clean_text(link.get_text(" ", strip=True)), "url": href, "fase": fase})
        proximo = soup.select_one("a.proximo[href]")
        return achados, urljoin(url, proximo.get("href")) if proximo else None

    def _detalhe(self, achado: dict[str, Any]) -> dict[str, Any] | None:
        html = self._html(achado["url"])
        soup = self.soup(html)
        uid = re.search(r"resolveuid/([0-9a-f]{32})", html)
        tags = " ".join(
            a.get_text(" ", strip=True) for a in soup.select(".keywords a, a[rel=tag], #category a, a.link-category")
        )
        fase = "abertas" if "#abertas" in tags else "analise" if "#analise" in tags else achado["fase"]
        conteudo = soup.select_one("#content") or soup
        titulo_el = conteudo.select_one("h1")
        title = clean_text(titulo_el.get_text(" ", strip=True)) if titulo_el else achado["title"]
        for lixo in conteudo.select("script, style, .social-links, .documentByLine, .keywords, #category, h1"):
            lixo.decompose()
        corpo = conteudo.select_one("#parent-fieldname-text") or conteudo
        resumo_el = corpo.select_one("p")
        return self._chamada(
            title, achado["url"], uid.group(1) if uid else None, fase,
            clean_text(conteudo.get_text(" ", strip=True)),
            clean_text(resumo_el.get_text(" ", strip=True)) if resumo_el else None,
            self._documentos(conteudo.select("a[href]"), achado["url"], achado["url"]),
        )

    def _historico(self, chamadas: dict[str, dict[str, Any]], itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Os 24 meses de encerradas: busca por ano, buracos da numeração e detalhe de cada uma."""
        piso = self.today - timedelta(days=RETENCAO_DIAS)
        anos = list(range(self.today.year, piso.year - 1, -1))
        conhecidas = {c["external_id"] for c in chamadas.values()}
        prazos = {c["external_id"]: self._janela(c["texto"])[1] for c in chamadas.values()}
        prazos.update({item["external_id"]: item["dates"]["deadline"] for item in itens})
        uids = {c["uid"] for c in chamadas.values() if c["uid"]}
        achados: dict[str, dict[str, Any]] = {}
        novos: list[dict[str, Any]] = []
        try:
            for ano in anos:
                url: str | None = f"{BUSCA}?SearchableText={ano}&sort_on=Date"
                paginas_vistas: set[str] = set()
                while url and url not in paginas_vistas and len(paginas_vistas) < 25:
                    paginas_vistas.add(url)
                    try:
                        resultados, url = self._resultados_da_busca(self._html(url), url)
                    except (Bloqueio, OrcamentoEsgotado):
                        raise
                    except Exception as erro:  # noqa: BLE001 - os buracos ficam para a numeração
                        self.avisos.append(f"busca {ano}: {type(erro).__name__}")
                        break
                    for achado in resultados:
                        externo = self._external_id(achado["url"], achado["title"])
                        ano_item = self._ano(externo, achado["url"])
                        # Itens repetidos entre páginas (paginação instável): vale o primeiro.
                        if externo and externo not in conhecidas and ano_item and ano_item >= piso.year:
                            achados.setdefault(externo, achado)

            for ano in anos:
                numeros = {
                    int(m.group(1)) for externo in conhecidas | set(achados)
                    if (m := re.fullmatch(rf"(\d+)-{ano}", externo))
                }
                antigas_seguidas = 0
                buscas_pontuais = 0
                for numero in range(max(numeros, default=0), 0, -1):
                    if antigas_seguidas >= ANTIGAS_SEGUIDAS_PARA_PARAR:
                        break
                    externo = f"{numero}-{ano}"
                    if externo in conhecidas:
                        prazo = prazos.get(externo)
                    else:
                        achado = achados.pop(externo, None)
                        if not achado:
                            # Buraco na numeração: a busca do ano pode ter pulado o item
                            # (paginação instável), e uma busca pontual por "NN/AAAA" confere.
                            if buscas_pontuais >= BUSCAS_PONTUAIS_POR_ANO:
                                continue
                            buscas_pontuais += 1
                            achado = self._busca_pontual(numero, ano)
                        if not achado:
                            continue
                        prazo = self._ler_historica(achado, uids, novos)
                    if prazo and prazo < piso.isoformat():
                        antigas_seguidas += 1
                    elif prazo:
                        antigas_seguidas = 0
            # Chamamentos e itens sem número entram se o ano estiver dentro da janela. Chamadas
            # numeradas que sobraram ficaram abaixo do ponto de parada do ano: não são lidas.
            for externo, achado in sorted(achados.items(), reverse=True):
                if not re.fullmatch(r"\d+-\d{4}", externo):
                    self._ler_historica(achado, uids, novos)
        except OrcamentoEsgotado:
            self._avisar_orcamento()
        except Bloqueio as erro:
            self.avisos.append(str(erro))
        return novos

    def _busca_pontual(self, numero: int, ano: int) -> dict[str, Any] | None:
        url = f"{BUSCA}?SearchableText={numero:02d}%2F{ano}&sort_on=Date"
        try:
            resultados, _ = self._resultados_da_busca(self._html(url), url)
        except (Bloqueio, OrcamentoEsgotado):
            raise
        except Exception:  # noqa: BLE001 - busca que falha é um buraco que fica para a próxima carga
            return None
        alvo = f"{numero}-{ano}"
        return next((r for r in resultados if self._external_id(r["url"], r["title"]) == alvo), None)

    def _ler_historica(self, achado: dict[str, Any], uids: set[str], novos: list[dict[str, Any]]) -> str | None:
        try:
            chamada = self._detalhe(achado)
        except (Bloqueio, OrcamentoEsgotado):
            raise
        except Exception as erro:  # noqa: BLE001 - uma página quebrada não derruba a carga
            self.avisos.append(f"detalhe ilegível: {achado['url']} ({type(erro).__name__})")
            return None
        if not chamada or (chamada["uid"] and chamada["uid"] in uids):
            return None
        if chamada["uid"]:
            uids.add(chamada["uid"])
        item = self._montar(chamada)
        prazo = None
        if item:
            novos.append(item)
            prazo = item["dates"]["deadline"]
        else:
            prazo = self._janela(chamada["texto"])[1] or "0000-00-00"
        return prazo

    # ------------------------------------------------------------------ classificação

    @staticmethod
    def _parceiros(title: str, *textos: str) -> list[str]:
        """Parceiros do bloco de siglas do título ("CNPq/MS-SCTIE-Decit", "MCTI/CNPq/ANR") e, para o
        Ministério da Saúde, também do resumo e da 1ª frase do edital ("Com financiamento do
        Decit/SCTIE/MS"). O MS vira um rótulo só: "Decit/MS", "Daevs/SVSA/MS" ou "MS".
        """
        antes = re.split(r"\bN\s*[º°o]\s*\.?\s*0*\d", title, maxsplit=1, flags=re.I)[0]
        tokens: list[str] = []
        for grupo in re.findall(r"[A-Za-zÀ-ú][\w+]*(?:-[\w+]+)*(?:\s*/\s*[A-Za-zÀ-ú][\w+]*(?:-[\w+]+)*)+", antes):
            for token in re.split(r"\s*/\s*", grupo):
                partes = token.split("-")
                if len(partes) > 1 and all(p.lower() in MS_ORGAOS for p in partes):
                    tokens.extend(partes)
                else:
                    tokens.append(token)
        ms: list[str] = []
        outros: list[str] = []
        for token in tokens:
            chave = token.lower()
            if chave in MS_ORGAOS:
                ms.append(MS_ORGAOS[chave])
            elif chave in SIGLAS_CONHECIDAS:
                outros.append(SIGLAS_CONHECIDAS[chave])
            elif chave not in NAO_PARCEIROS and sum(c.isupper() for c in token) >= 2:
                outros.append(token)
        for texto in (title, *textos):
            for achado in MS_NO_TEXTO.findall(texto or ""):
                if "ppsus" in achado.lower():
                    ms.append("Decit")
                for sigla in re.findall(r"[A-Za-z]+", achado):
                    if sigla.lower() in MS_ORGAOS:
                        ms.append(MS_ORGAOS[sigla.lower()])
                if re.search(r"Minist[eé]rio", achado):
                    ms.append("MS")
        parceiros: list[str] = []
        if ms:
            if "Decit" in ms:
                parceiros.append("Decit/MS")
            else:
                orgaos = [x for x in dict.fromkeys(ms) if x in MS_DEPARTAMENTOS.values()]
                orgaos += [x for x in dict.fromkeys(ms) if x in MS_SECRETARIAS.values()]
                parceiros.append("/".join([*orgaos, "MS"]))
        parceiros.extend(x for x in dict.fromkeys(outros) if x not in parceiros)
        return parceiros

    @staticmethod
    def _instrument(title: str, resumo: str) -> str:
        """Bolsa só quando a chamada é de bolsas; o resto é auxílio a projeto (`auxilio_pesquisa`,
        contrato 2.2)."""
        titulo = ascii_fold(title)
        if re.search(r"\bpremio\b", titulo):
            return "premio"
        if re.search(
            r"\bbolsas?\b|mestrado|doutorado|produtividade|iniciacao cientifica|\bpibi|\bpibpg\b|\brhae\b|\bpci\b"
            r"|pos-?graduacao",
            titulo,
        ) or re.search(r"\bbolsas?\b", ascii_fold(resumo)[:300]):
            return "bolsa"
        return "auxilio_pesquisa"

    @staticmethod
    def _audience(title: str, block: str) -> list[str]:
        text = slug(title + " " + block)
        result = []
        for needle, canonical in (
            ("empresa", "empresa"), ("startup", "startup"), ("universidade", "universidade"),
            ("ict", "ict"), ("institui-cao-cientifica", "ict"), ("pesquisador", "pesquisador"),
        ):
            if needle in text and canonical not in result:
                result.append(canonical)
        return result or ["pesquisador", "ict", "universidade"]

    @staticmethod
    def _themes(text: str) -> list[str]:
        """Temas canônicos (`canonical_themes`: terapias_avancadas, pesquisa_clinica, saude,
        biotecnologia…) e os de antes (inovação e cooperação internacional)."""
        folded = ascii_fold(text)
        temas = [canonical for pattern, canonical in THEME_ALIASES if re.search(pattern, folded)]
        for pattern, theme in (
            (r"inovac", "inovacao"), (r"africa|atlant|internacion|cooperacao", "cooperacao_internacional"),
        ):
            if re.search(pattern, folded):
                temas.append(theme)
        return list(dict.fromkeys(temas))
