"""PDIL — Programa de Desenvolvimento e Inovação Local (Ministério da Saúde, SECTICS).

Estudo de 14/10/2026 (`radar-fontes-saude/estudo/pdil.md`). O que o adaptador assume:

- O portal é Plone 5 clássico. A API `++api++` dá 404 e a `plone.restapi` dá 401 ao
  anônimo, mas cada pasta tem RSS (RDF 1.0) e a busca do site também sai em RSS
  (no máximo 15 itens). É só isso que se lê no dia a dia.
- Os itens do tipo Link respondem 302 para o PDF, para o Saúde Legis ou para o DOU.
  O `Location` é lido sem seguir; o DOU (`www.in.gov.br`) deu 403 ao radar no estudo
  e fica fora desta versão (o vigia do DOU seria outra fonte). Só se segue para
  `www.gov.br/saude` e `bvsms.saude.gov.br/bvs`, cujos `robots.txt` liberam esses
  caminhos (conferidos em 14/10/2026).
- O `dc:date` do Plone é a data de criação ou de reorganização da pasta, não a do
  ato: a data do ato sai do texto (assinatura no SEI ou "Brasília, DD de MÊS").
- A janela de submissão vem por extenso ("entre 24 de junho e 23 de setembro de
  2024") e a prorrogação vem em outro ato ("prorrogado até o dia 30 de setembro de
  2024", Nota Informativa nº 5/2024), que atualiza o prazo do mesmo item.

Identidade: o `external_id` é o número do ato que abriu a janela, tirado do
endereço (`informe-no-02-2024-sectics-ms` → `informe-02-2024`). O número vai com
dois dígitos porque o mesmo ato aparece como "nº 02/2024" e "nº 2/2024" em pastas
diferentes. Resultados, ranking e regimentos não viram itens.

Situação: `open` só com prazo lido e ≥ hoje; `scheduled` com início lido depois de
hoje, ou com anúncio oficial do MS sem prazo; `closed` com prazo lido < hoje;
`unknown` quando o ato existe mas o texto não pôde ser lido (link para o DOU, 404).
Nada sobre o regime da Lei 15.471/2026 é emitido sem um ato que o anuncie.

Orçamento: no máximo 10 pedidos por rodada (7 RSS e até 3 documentos), 1 por
segundo por host. Não há modo histórico: os RSS já mostram tudo o que a fonte tem, e
a única janela até hoje (2024) o pipeline corta pelos 24 meses.
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from pypdf import PdfReader

from funding_intelligence.adapters.base import USER_AGENT, BaseAdapter
from funding_intelligence.models import ascii_fold, clean_text, opportunity
from funding_intelligence.pipeline import RETENCAO_ENCERRADAS_DIAS


log = logging.getLogger(__name__)

BASE = "https://www.gov.br/saude/pt-br/composicao/sectics/pdil"
RAIZ_RSS = BASE + "/RSS"
BUSCA_RSS = "https://www.gov.br/saude/pt-br/search_rss?SearchableText={}&sort_on=Date&sort_order=reverse"

# A raiz vem primeiro: uma pasta nova ("Processo seletivo", "Edital") é lida na
# mesma rodada, antes dos documentos. A segunda busca existe porque a Nota
# Informativa nº 5/2024 (a prorrogação de 2024) não está em nenhuma pasta do PDIL:
# fica além dos 15 itens do RSS do DECEIIS e só aparece na busca.
FEEDS = (
    RAIZ_RSS,
    BASE + "/informes-tecnicos/RSS",
    BASE + "/notas-tecnicas-e-informativas/RSS",
    BASE + "/legislacao/RSS",
    "https://www.gov.br/saude/pt-br/composicao/sectics/deceiis/publicacoes/RSS",
    BUSCA_RSS.format("PDIL"),
    BUSCA_RSS.format(quote("PDIL prorrogação")),
)

PASTAS_CONHECIDAS = {
    "ciclo-do-pdil", "legislacao", "publicacoes", "informes-tecnicos",
    "notas-tecnicas-e-informativas", "faq", "programa-de-desenvolvimento-e-inovacao-local",
}

ORCAMENTO_PEDIDOS = 10
INTERVALO_S = 1.1
# Host → prefixo de caminho que o robots.txt libera e que o radar lê.
HOSTS_PERMITIDOS = {"www.gov.br": "/saude/", "bvsms.saude.gov.br": "/bvs/"}
REDIRECIONAMENTOS = {301, 302, 303, 307, 308}

# Regra vigente: Portaria GM/MS nº 4.473/2024 (Anexo CIX da PRC 5/2017), art. 7º.
# Propõe "instituição pública, ICT ou entidade privada sem fins lucrativos":
# - instituição pública: `ict` (ICT pública), `universidade` pública, `hospital`
#   público (o HC-FMUSP teve projeto aprovado em 2024), `estado` e `municipio`
#   (órgãos e laboratórios oficiais, como os produtores públicos estaduais);
# - ICT: `ict` e `ict_privada` (o SENAI CIMATEC propôs em 2024);
# - entidade privada sem fins lucrativos: `osc`, `fundacao_apoio` (a Fundação
#   Zerbini foi aprovada), `ict_privada`, hospital filantrópico e universidade
#   comunitária.
# A empresa só entra como parceira da aliança: as duas propostas de uma Ltda. saíram
# "Inelegível" (Informe nº 07/2025 e Portaria GM/MS nº 10.154/2026).
PROPONENTES_VIGENTES = [
    "ict", "ict_privada", "universidade", "osc", "hospital", "fundacao_apoio", "estado", "municipio",
]
PARCEIRAS_VIGENTES = ["empresa", "startup"]
REGRA_VIGENTE = (
    "Proponente: instituição pública, ICT ou entidade privada sem fins lucrativos (Portaria GM/MS "
    "nº 4.473/2024, art. 7º); a empresa entra só como parceira da aliança estratégica."
)
# Lei 15.471/2026, art. 18: o projeto reúne, obrigatoriamente, uma Empresa Estratégica
# de Saúde credenciada e uma ICT ou produtor público. Quem propõe fica para o
# regulamento, que ainda não saiu; só se aplica quando o próprio ato cita a lei.
PROPONENTES_LEI_15471 = ["empresa", "ict", "ict_privada", "universidade", "estado"]
REGRA_LEI_15471 = (
    "O ato cita o regime da Lei nº 15.471/2026: a aliança reúne uma Empresa Estratégica de Saúde "
    "credenciada e uma ICT ou produtor público; confira no ato quem é a proponente."
)
# Matriz de Desafios Produtivos e Tecnológicos em Saúde (Portaria GM/MS nº 2.261/2023),
# que o ato de abertura manda seguir. Traz a "plataforma produtiva para terapias
# avançadas" em quatro desafios.
TEMAS_BASE = ["saude", "complexo_economico_industrial_saude", "inovacao"]
TEMAS_MATRIZ = ["biotecnologia", "terapias_avancadas", "vacinas", "medicamentos", "diagnostico",
                "dispositivos_medicos", "saude_digital"]

# Classificação pelo título e pela descrição do RSS (texto já sem acento).
_PDIL = re.compile(r"\bpdil\b|desenvolvimento e inovacao local|inovacao e desenvolvimento local")
_EXCLUI = re.compile(
    r"resultad|ranque|ranking|ineleg|regimento|comite deliberativo|comissao tecnica de avaliacao"
    r"|modelo de proposta|submetidas em \d{4}|relacao das propostas|perguntas frequentes|\bfaq\b"
    r"|institui o programa"
)
_PRORROGA = re.compile(r"prorroga")
_ABERTURA = re.compile(
    r"submissao de propostas?|periodo de submissao|recebimento de propostas|processo seletivo"
    r"|chamamento|\bedital\b|inscricoes|nova rodada|novo ciclo|proxima rodada|proximo ciclo"
    r"|abertura d[eao]s? (?:periodo|prazo|processo|inscric|submiss|rodada|ciclo)"
)
# No texto do ato: o que conta como anúncio oficial de janela quando não há data.
_ANUNCIO = re.compile(
    r"sera(?:o)? (?:aberto|aberta|recebid)|abertura d[eao]s? (?:periodo|prazo|processo|inscric|submiss|rodada|ciclo)"
    r"|torna publico|fica(?:m)? abert|estara(?:o)? abert|periodo de submissao|recebimento de propostas"
    r"|nova rodada|novo ciclo|proxima rodada|proximo ciclo|processo seletivo|chamamento publico"
)
_LEI_15471 = re.compile(r"15\.471|empresas? estrategicas? de saude")
_MATRIZ = re.compile(r"matriz de desafios")

MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
_MES = "(" + "|".join(MESES) + ")"
_DIA = r"(\d{1,2})(?:o|°)?"  # "1º" vira "1o" depois de tirar o acento
_DATA_EXT = rf"{_DIA}\s+de\s+{_MES}\s+de\s+(\d{{4}})"
_DATA_EXT_SEM_ANO = rf"{_DIA}\s+de\s+{_MES}(?:\s+de\s+(\d{{4}}))?"
_DATA_NUM = r"(\d{1,2})/(\d{1,2})/(\d{4})"
_O_DIA = r"(?:os?\s+)?(?:dias?\s+)?"
_INTERVALO_EXT = re.compile(
    rf"\b(?:entre|de|desde|periodo de)\s+{_O_DIA}{_DATA_EXT_SEM_ANO}\s+(?:e|a|ate)\s+{_O_DIA}{_DATA_EXT}"
)
_INTERVALO_NUM = re.compile(rf"\b(?:entre|de|desde)\s+{_O_DIA}{_DATA_NUM}\s+(?:e|a|ate)\s+{_O_DIA}{_DATA_NUM}")
_ATE = re.compile(rf"\b(?:ate|prorrogad[oa]s?\s+para)\s+{_O_DIA}(?:{_DATA_EXT}|{_DATA_NUM})")
_PRORROGACAO_ATE = re.compile(rf"prorrog[^.;]{{0,200}}?\b(?:ate|para)\s+{_O_DIA}(?:{_DATA_EXT}|{_DATA_NUM})")
# Um intervalo só vale perto de palavra de submissão; evita a vigência de um acordo
# ou a data de um decreto citado.
_CONTEXTO = re.compile(r"submiss|propostas?|inscric|recebimento|periodo|prazo|prorrog|processo seletivo")
_ASSINATURA = re.compile(r"\bem (\d{1,2}/\d{1,2}/\d{4}),? as \d{1,2}[:h]")
_LOCAL_DATA = re.compile(rf"brasilia(?:\s*/\s*df)?\s*,\s*(?:em\s+)?{_DATA_EXT}")
_DATA_TITULO = re.compile(rf"\bde\s+{_DATA_EXT}")

# Número do ato no endereço do Plone e no texto.
_ATO_SLUG = re.compile(
    r"(?P<tipo>informe(?:-tecnico)?|nota-(?:informativa|tecnica)|portaria(?:-[a-z]+)*?|resolucao(?:-[a-z]+)*?"
    r"|edital(?:-[a-z]+)*?|chamamento(?:-[a-z]+)*?)"
    r"-no?-(?P<num>\d+(?:-\d{3})*)-(?:de-\d{1,2}o?-de-[a-z]+-de-)?(?P<ano>\d{4})(?!\d)"
)
_ATO_TEXTO = re.compile(
    r"\b(?P<tipo>informe(?: tecnico)?|nota (?:informativa|tecnica)|edital|chamamento(?: publico)?)"
    r"(?: [a-z]+/[a-z]+(?:/[a-z]+)?)?\s*n\s*[o°.]?\s*(?P<num>\d{1,3})\s*/\s*(?P<ano>\d{4})"
)
_PORTARIA_TEXTO = re.compile(
    r"\b(?P<tipo>portaria|resolucao)(?P<orgao>(?: [a-z]+(?:/[a-z]+)*)?)\s*n\s*[o°.]?\s*"
    r"(?P<num>\d{1,2}(?:\.\d{3})*)\s*,?\s*de\s+\d{1,2}o?\s+de\s+[a-z]+\s+de\s+(?P<ano>\d{4})"
)
_TIPOS_NOTICIA = {"noticia", "news item", "collective.nitf.content"}
_MARCAS_BLOQUEIO = ("just a moment", "cf-chl", "hcaptcha", "g-recaptcha", "the requested url was rejected")
_MARCAS_BLOQUEIO_ERRO = _MARCAS_BLOQUEIO + ("captcha", "access denied", "attention required", "request rejected")


class Bloqueio(RuntimeError):
    """403, 429, CAPTCHA ou Cloudflare: o adaptador para de pedir àquele host."""


class _SemOrcamento(RuntimeError):
    pass


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    """O 30x volta como resposta: o `Location` é lido e decidido aqui, sem seguir sozinho."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_ABRIDOR = urllib.request.build_opener(_SemRedirecionar())


@dataclass
class Resposta:
    status: int  # 0 = falha de rede (passageira)
    corpo: bytes = b""
    local: str | None = None
    tipo: str = ""


@dataclass
class Entrada:
    """Um item de RSS do Plone. `data` é a do Plone, não a do ato."""
    titulo: str
    link: str
    descricao: str = ""
    tipo: str = ""
    data: str | None = None


@dataclass
class Ato:
    id: str
    ano: int
    rotulo: str


@dataclass
class Candidato:
    entrada: Entrada
    papel: str  # "abertura" ou "prorrogacao"
    ato: Ato | None
    noticia: bool = False
    texto: str | None = None
    falha: str | None = None  # "passageira", "permanente" ou "orcamento"
    motivo: str | None = None


# ---------------------------------------------------------------------------
# Datas por extenso


def _dobra(texto: str | None) -> str:
    return " ".join(ascii_fold(texto).split())


def _iso(dia: int, mes: int, ano: int) -> str | None:
    try:
        return date(ano, mes, dia).isoformat()
    except ValueError:
        return None


def data_por_extenso(texto: str | None) -> str | None:
    """'30 de setembro de 2024', '1º de março de 2027' ou '30/09/2024' → AAAA-MM-DD."""
    t = _dobra(texto)
    m = re.search(_DATA_EXT, t)
    if m:
        return _iso(int(m.group(1)), MESES[m.group(2)], int(m.group(3)))
    m = re.search(_DATA_NUM, t)
    return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _intervalo(m: re.Match[str], numerico: bool) -> tuple[str, str] | None:
    if numerico:
        d1, m1, a1, d2, m2, a2 = (int(x) for x in m.groups())
        inicio, fim = _iso(d1, m1, a1), _iso(d2, m2, a2)
    else:
        d1, mes1, a1, d2, mes2, a2 = m.groups()
        ano_fim = int(a2)
        if a1:
            ano_ini = int(a1)
        else:
            # "entre 24 de junho e 23 de setembro de 2024": a primeira data herda o ano
            # da segunda; se o mês volta ("de 15 de novembro a 15 de janeiro de 2027"),
            # é do ano anterior.
            ano_ini = ano_fim - 1 if (MESES[mes1], int(d1)) > (MESES[mes2], int(d2)) else ano_fim
        inicio, fim = _iso(int(d1), MESES[mes1], ano_ini), _iso(int(d2), MESES[mes2], ano_fim)
    if not inicio or not fim or inicio > fim:
        return None
    return inicio, fim


def _data_do_match(m: re.Match[str]) -> str | None:
    g = m.groups()
    if g[0]:  # por extenso
        return _iso(int(g[0]), MESES[g[1]], int(g[2]))
    return _iso(int(g[3]), int(g[4]), int(g[5]))


def _no_contexto(t: str, inicio: int, fim: int) -> bool:
    return bool(_CONTEXTO.search(t[max(0, inicio - 400):fim]))


def periodo_por_extenso(texto: str | None) -> tuple[str | None, str | None]:
    """O período de submissão de um ato: (início, prazo), cada um AAAA-MM-DD ou None.

    Lê "entre 24 de junho e 23 de setembro de 2024", "de 1º de março a 30 de abril
    de 2027", "de 24/06/2024 a 23/09/2024" e o prazo solto ("até o dia 30 de setembro
    de 2024", "prorrogado para ..."). Só vale o que está perto de palavra de
    submissão, para não pegar a data de um decreto citado. Uma prorrogação no mesmo
    texto, depois do período, estende o prazo.
    """
    t = _dobra(texto)
    achados: list[tuple[int, int, str, str]] = []
    for regex, numerico in ((_INTERVALO_EXT, False), (_INTERVALO_NUM, True)):
        for m in regex.finditer(t):
            par = _intervalo(m, numerico)
            if par and _no_contexto(t, m.start(), m.end()):
                achados.append((m.start(), m.end(), *par))
    if achados:
        _, fim_pos, inicio, fim = min(achados)
        for m in _PRORROGACAO_ATE.finditer(t, fim_pos):
            nova = _data_do_match(m)
            if nova and nova > fim:
                fim = nova
        return inicio, fim
    for m in _ATE.finditer(t):
        prazo = _data_do_match(m)
        if prazo and _no_contexto(t, m.start(), m.end()):
            return None, prazo
    return None, None


def data_do_ato(texto: str | None, titulo: str | None = None) -> str | None:
    """Data do ato: assinatura no SEI, "Brasília, DD de MÊS de AAAA" ou a data do título."""
    t = _dobra(texto)
    m = _ASSINATURA.search(t)
    if m:
        return data_por_extenso(m.group(1))
    m = _LOCAL_DATA.search(t)
    if m:
        return _iso(int(m.group(1)), MESES[m.group(2)], int(m.group(3)))
    m = _DATA_TITULO.search(_dobra(titulo))
    return _iso(int(m.group(1)), MESES[m.group(2)], int(m.group(3))) if m else None


# ---------------------------------------------------------------------------
# Identidade do ato


def _ato(tipo: str, num: str, ano: str, orgao: str = "") -> Ato:
    partes = tipo.split("-") if "-" in tipo else tipo.split(" ")
    base = "-".join(partes[:2]) if partes[0] == "nota" else partes[0]
    numero = int(re.sub(r"\D", "", num))
    nomes = {"informe": "Informe", "nota-informativa": "Nota Informativa", "nota-tecnica": "Nota Técnica",
             "edital": "Edital", "chamamento": "Chamamento", "portaria": "Portaria", "resolucao": "Resolução"}
    if base in {"portaria", "resolucao"}:
        orgao = "-".join(re.findall(r"[a-z]+", orgao))
        numero_br = f"{numero:,}".replace(",", ".")
        rotulo = " ".join(x for x in (nomes[base], orgao.upper().replace("-", "/"), f"nº {numero_br}/{ano}") if x)
        return Ato("-".join(x for x in (base, orgao, str(numero), ano) if x), int(ano), rotulo)
    return Ato(f"{base}-{numero:02d}-{ano}", int(ano), f"{nomes.get(base, base)} nº {numero:02d}/{ano}")


def ato_do_endereco(url: str | None) -> Ato | None:
    """O ato pelo endereço do Plone: `informe-no-02-2024-sectics-ms` → `informe-02-2024`."""
    m = _ATO_SLUG.search(_id_plone(url or "") or "")
    if not m:
        return None
    tipo = m.group("tipo")
    orgao = ""
    for base in ("portaria", "resolucao"):
        if tipo.startswith(base):
            orgao, tipo = tipo[len(base):], base
    return _ato(tipo, m.group("num"), m.group("ano"), orgao)


def atos_do_texto(texto: str | None) -> list[Ato]:
    """Os atos citados num texto ("Informe Técnico nº 03/2027", "Portaria GM/MS nº 10.154, de ...")."""
    t = _dobra(texto)
    achados = [(m.start(), _ato(m.group("tipo"), m.group("num"), m.group("ano"))) for m in _ATO_TEXTO.finditer(t)]
    achados += [
        (m.start(), _ato(m.group("tipo"), m.group("num"), m.group("ano"), m.group("orgao")))
        for m in _PORTARIA_TEXTO.finditer(t)
    ]
    vistos: dict[str, Ato] = {}
    for _, ato in sorted(achados, key=lambda x: x[0]):
        vistos.setdefault(ato.id, ato)
    return list(vistos.values())


def _id_plone(url: str) -> str | None:
    """O id do objeto no Plone (último trecho do caminho), quando o ato não tem número."""
    caminho = urlsplit(url).path.rstrip("/").removesuffix("/view")
    nome = caminho.rsplit("/", 1)[-1].removesuffix(".pdf")
    return nome or None


# ---------------------------------------------------------------------------
# RSS


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _data_plone(valor: str | None) -> str | None:
    m = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", valor or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def ler_rss(corpo: bytes) -> list[Entrada]:
    """Itens de um RSS 1.0 (RDF) ou 2.0 do Plone, pelo nome local das marcas."""
    raiz = ET.fromstring(corpo)
    entradas = []
    for item in raiz.iter():
        if _local(item.tag) != "item":
            continue
        campos = {_local(filho.tag): (filho.text or "").strip() for filho in item}
        link = campos.get("link") or item.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or ""
        if not link:
            continue
        entradas.append(Entrada(
            titulo=clean_text(campos.get("title")), link=link.strip(),
            descricao=clean_text(campos.get("description")), tipo=campos.get("type", ""),
            data=_data_plone(campos.get("date")),
        ))
    return entradas


# ---------------------------------------------------------------------------


class PdilAdapter(BaseAdapter):
    id = "pdil"
    name = "PDIL — Ministério da Saúde"
    url = BASE

    def __init__(self, *, fixture: str | None = None, today: date | None = None):
        super().__init__(fixture=fixture, today=today)
        # Com fixture (uma pasta com `mapa.json`), nada sai para a rede e não há pausa.
        self.intervalo_s = 0.0 if fixture else INTERVALO_S
        self._mapa: dict[str, Any] | None = None
        self._reiniciar()

    def _reiniciar(self) -> None:
        self.pedidos = 0
        self.urls_pedidas: list[str] = []
        self.registro: list[str] = []
        self._falhas: list[str] = []  # o que pode esconder item: vira leitura parcial
        self._bloqueados: set[str] = set()
        self._ultimo_por_host: dict[str, float] = {}

    # -- rede -----------------------------------------------------------------

    def _registrar(self, mensagem: str, *, falha: bool = False) -> None:
        hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.registro.append(f"{hora} {mensagem}")
        if falha:
            self._falhas.append(mensagem)
            log.warning("pdil: %s", mensagem)
        else:
            log.info("pdil: %s", mensagem)

    @staticmethod
    def permitido(url: str) -> bool:
        partes = urlsplit(url)
        prefixo = HOSTS_PERMITIDOS.get(partes.netloc.lower())
        return partes.scheme == "https" and prefixo is not None and partes.path.startswith(prefixo)

    def _transporte(self, url: str) -> Resposta:
        if self.fixture:
            return self._transporte_fixture(url)
        pedido = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with _ABRIDOR.open(pedido, timeout=45) as resposta:
                return Resposta(resposta.status, resposta.read(), None, resposta.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as erro:
            try:
                corpo = erro.read() or b""
            except OSError:
                corpo = b""
            cabecalhos = erro.headers or {}
            return Resposta(erro.code, corpo, cabecalhos.get("Location"), cabecalhos.get("Content-Type", ""))
        except (urllib.error.URLError, TimeoutError, OSError) as erro:
            self._registrar(f"falha de rede em {url}: {erro}")
            return Resposta(0)

    def _transporte_fixture(self, url: str) -> Resposta:
        pasta = Path(self.fixture or "")
        if self._mapa is None:
            self._mapa = json.loads((pasta / "mapa.json").read_text(encoding="utf-8"))
        alvo = self._mapa.get(url)
        if alvo is None:
            return Resposta(404, b"", None, "text/html")
        corpo = (pasta / alvo["arquivo"]).read_bytes() if alvo.get("arquivo") else b""
        return Resposta(int(alvo.get("status", 200)), corpo, alvo.get("local"), alvo.get("tipo", ""))

    def _pedir(self, url: str) -> Resposta:
        if not self.permitido(url):
            raise ValueError(f"fora do alcance do radar: {url}")
        host = urlsplit(url).netloc.lower()
        if host in self._bloqueados:
            raise Bloqueio(f"{host} já bloqueou nesta rodada")
        if self.pedidos >= ORCAMENTO_PEDIDOS:
            raise _SemOrcamento(f"orçamento de {ORCAMENTO_PEDIDOS} pedidos esgotado antes de {url}")
        espera = self.intervalo_s - (time.monotonic() - self._ultimo_por_host.get(host, -1e9))
        if espera > 0:
            time.sleep(espera)
        self.pedidos += 1
        self.urls_pedidas.append(url)
        resposta = self._transporte(url)
        self._ultimo_por_host[host] = time.monotonic()
        if self._bloqueada(resposta):
            self._bloqueados.add(host)
            raise Bloqueio(f"HTTP {resposta.status} em {url}")
        return resposta

    @staticmethod
    def _bloqueada(resposta: Resposta) -> bool:
        if resposta.status in (403, 429):
            return True
        if "html" not in resposta.tipo.lower() and resposta.status < 400:
            return False
        amostra = resposta.corpo[:20000].decode("utf-8", errors="replace").lower()
        marcas = _MARCAS_BLOQUEIO_ERRO if resposta.status >= 400 else _MARCAS_BLOQUEIO
        return any(marca in amostra for marca in marcas)

    # -- leitura --------------------------------------------------------------

    def _ler_feed(self, url: str) -> list[Entrada] | None:
        try:
            resposta = self._pedir(url)
        except (Bloqueio, _SemOrcamento) as erro:
            self._registrar(f"RSS não lido ({erro}): {url}", falha=True)
            return None
        if resposta.status != 200:
            self._registrar(f"RSS respondeu HTTP {resposta.status}: {url}", falha=True)
            return None
        try:
            return ler_rss(resposta.corpo)
        except ET.ParseError as erro:
            self._registrar(f"RSS ilegível ({erro}): {url}", falha=True)
            return None

    def _pastas_novas(self, entradas: list[Entrada]) -> list[str]:
        """RSS de pastas novas na raiz do PDIL que tenham cara de janela ("Processo seletivo")."""
        novas = []
        for entrada in entradas:
            nome = _id_plone(entrada.link) or ""
            if (
                entrada.tipo not in {"Folder", "Collection"}
                or not entrada.link.startswith(BASE + "/")
                or nome in PASTAS_CONHECIDAS
            ):
                continue
            self._registrar(f"pasta nova no PDIL: {entrada.titulo} ({entrada.link})")
            if _ABERTURA.search(_dobra(f"{entrada.titulo} {entrada.descricao} {nome.replace('-', ' ')}")):
                novas.append(entrada.link.rstrip("/") + "/RSS")
        return novas

    def _texto(self, resposta: Resposta) -> str:
        if resposta.corpo[:5] == b"%PDF-":
            leitor = PdfReader(io.BytesIO(resposta.corpo))
            return "\n".join(pagina.extract_text() or "" for pagina in leitor.pages)
        bruto = resposta.corpo.decode("utf-8", errors="replace")
        if "html" in resposta.tipo.lower():
            sopa = BeautifulSoup(bruto, "html.parser")
            for marca in sopa(["script", "style", "noscript", "nav", "header", "footer"]):
                marca.decompose()
            miolo = sopa.find(id="content") or sopa.find("article") or sopa.find("main") or sopa
            return miolo.get_text(" ")
        # Texto puro: é o `.txt` com o trecho do PDF nas fixtures.
        return bruto

    def _ler_documento(self, candidato: Candidato) -> None:
        """Lê o texto do ato. File serve o PDF sem `/view`; Link responde 302 e o
        destino só é seguido dentro do gov.br/saude e do Saúde Legis."""
        url = candidato.entrada.link.rstrip("/").removesuffix("/view")
        if not self.permitido(url):
            candidato.falha, candidato.motivo = "permanente", f"fora do alcance ({urlsplit(url).netloc})"
            self._registrar(f"documento fora do alcance do radar: {url}")
            return
        try:
            resposta = self._pedir(url)
            saltos = 0
            while resposta.status in REDIRECIONAMENTOS and resposta.local and saltos < 2:
                destino = urljoin(url, resposta.local)
                if not self.permitido(destino) or re.search(r"login|acl_users|require_login", destino):
                    # O DOU deu 403 ao radar no estudo; o vigia do DOU fica fora desta versão.
                    candidato.falha, candidato.motivo = "permanente", f"aponta para {urlsplit(destino).netloc}"
                    self._registrar(f"Link não seguido: {url} → {destino}")
                    return
                url, saltos = destino, saltos + 1
                resposta = self._pedir(url)
        except _SemOrcamento as erro:
            # O orçamento corta os atos mais velhos primeiro. Ato de dois anos atrás ou
            # mais tem a janela encerrada, e a encerrada fica no catálogo sem ser
            # reemitida: só se registra. Ato recente vira leitura parcial.
            candidato.falha, candidato.motivo = "orcamento", str(erro)
            recente = candidato.ato is None or candidato.ato.ano >= self.today.year - 1
            self._registrar(f"documento não lido ({erro}): {candidato.entrada.link}", falha=recente)
            return
        except Bloqueio as erro:
            candidato.falha, candidato.motivo = "passageira", str(erro)
            self._registrar(f"documento não lido ({erro}): {candidato.entrada.link}", falha=True)
            return
        if resposta.status == 200:
            try:
                candidato.texto = self._texto(resposta)
            except Exception as erro:  # noqa: BLE001 — PDF corrompido não derruba a rodada
                candidato.falha, candidato.motivo = "permanente", f"texto ilegível ({type(erro).__name__})"
                self._registrar(f"texto ilegível em {url}: {erro}")
            return
        if resposta.status in (401, 404, 410) or resposta.status in REDIRECIONAMENTOS:
            candidato.falha, candidato.motivo = "permanente", f"HTTP {resposta.status}"
            self._registrar(f"documento com HTTP {resposta.status}: {url}")
            return
        candidato.falha, candidato.motivo = "passageira", f"HTTP {resposta.status}"
        self._registrar(f"documento com HTTP {resposta.status}: {url}", falha=True)

    # -- classificação ----------------------------------------------------------

    @staticmethod
    def _prioridade_link(entrada: Entrada) -> int:
        """Entre cópias do mesmo ato, a que custa menos pedidos e não cai em pasta restrita."""
        if "/publicacoes-1/" in entrada.link:
            return 9  # pasta restrita: redireciona para login
        if entrada.tipo in {"File", "Arquivo"} or entrada.link.endswith(".pdf/view"):
            return 0
        if entrada.link.startswith(BASE + "/"):
            return 1
        return 2

    def _candidatos(self, entradas: list[Entrada]) -> list[Candidato]:
        piso = self.today - timedelta(days=RETENCAO_ENCERRADAS_DIAS)
        por_chave: dict[str, Candidato] = {}
        for entrada in entradas:
            texto = _dobra(f"{entrada.titulo} {entrada.descricao}")
            if not _PDIL.search(texto) or _EXCLUI.search(texto):
                continue
            if _PRORROGA.search(texto):
                papel = "prorrogacao"
            elif _ABERTURA.search(texto):
                papel = "abertura"
            else:
                continue
            noticia = ascii_fold(entrada.tipo) in _TIPOS_NOTICIA or "/noticias" in entrada.link
            ato = ato_do_endereco(entrada.link)
            if ato is None:
                citados = atos_do_texto(f"{entrada.titulo} {entrada.descricao}")
                ato = citados[0] if citados else None
            if ato is None and not noticia and entrada.link.startswith("https://www.gov.br/saude/"):
                nome = _id_plone(entrada.link)
                ato = Ato(nome, int((entrada.data or str(self.today.year))[:4]), entrada.titulo) if nome else None
            ano = ato.ano if ato else int((entrada.data or str(self.today.year))[:4])
            # Uma janela aberta por ato do ano A fecha até o fim de A+1; além dos 24
            # meses o pipeline a corta, então nem se gasta pedido com ela.
            if date(ano + 1, 12, 31) < piso:
                continue
            chave = ato.id if ato else entrada.link
            novo = Candidato(entrada, papel, ato, noticia)
            atual = por_chave.get(chave)
            if atual is None or (
                (atual.noticia, self._prioridade_link(atual.entrada))
                > (novo.noticia, self._prioridade_link(novo.entrada))
            ):
                por_chave[chave] = novo
        # Os mais novos primeiro, e o ato antes da notícia: se o orçamento acabar,
        # fica de fora o mais velho.
        return sorted(
            por_chave.values(),
            key=lambda c: (c.noticia, -(c.ato.ano if c.ato else int((c.entrada.data or "0")[:4] or 0)),
                           c.papel != "abertura", self._prioridade_link(c.entrada)),
        )

    # -- coleta -------------------------------------------------------------------

    def collect(self) -> list[dict[str, Any]]:
        self._reiniciar()
        entradas: list[Entrada] = []
        lidos = 0
        fila = list(FEEDS)
        while fila:
            feed = fila.pop(0)
            lote = self._ler_feed(feed)
            if lote is None:
                continue
            lidos += 1
            entradas.extend(lote)
            if feed == RAIZ_RSS:
                fila[:0] = self._pastas_novas(lote)
        if not lidos:
            raise RuntimeError("PDIL: nenhum RSS respondeu (" + "; ".join(self._falhas)[:400] + ")")

        candidatos = self._candidatos(entradas)
        for candidato in candidatos:
            self._ler_documento(candidato)

        atos_lidos = {c.ato.id for c in candidatos if c.ato and not c.noticia}
        aberturas: list[Candidato] = []
        prorrogacoes: list[Candidato] = []
        for candidato in candidatos:
            if candidato.noticia:
                # Notícia só vale com o número do ato; sem ele, o id sairia do título.
                citados = atos_do_texto(" ".join(x for x in (candidato.entrada.titulo, candidato.texto) if x))
                if not citados:
                    if candidato.texto is not None:
                        self._registrar(f"notícia do PDIL sem número de ato, não emitida: {candidato.entrada.link}")
                    continue
                # O ato que abre vem antes da portaria que institui o programa, citada de passagem.
                candidato.ato = candidato.ato or next(
                    (a for a in citados if not a.id.startswith(("portaria", "resolucao"))), citados[0])
                if candidato.ato.id in atos_lidos:
                    continue
            (prorrogacoes if candidato.papel == "prorrogacao" else aberturas).append(candidato)

        janelas = [j for j in (self._janela(c) for c in aberturas) if j]
        for candidato in prorrogacoes:
            if candidato.texto:
                self._prorrogar(janelas, candidato)
        # Uma prorrogação não lida pode mudar o prazo de uma janela do mesmo ano (ou
        # reabri-la). A encerrada desse período não é emitida com prazo talvez errado:
        # o pipeline guarda a versão anterior.
        nao_lidas = [c.ato.ano if c.ato else self.today.year for c in prorrogacoes if not c.texto]
        corte = (date(min(nao_lidas), 1, 1) - timedelta(days=120)).isoformat() if nao_lidas else None

        itens = []
        for janela in janelas:
            status = self._situacao(janela)
            if corte and status == "closed" and janela["fim"] >= corte:
                continue
            janela["status"] = status
            itens.append(janela)
        itens = self._sem_anuncio_superado(itens)
        return [self._oportunidade(janela) for janela in itens]

    def _janela(self, candidato: Candidato) -> dict[str, Any] | None:
        if candidato.falha in {"passageira", "orcamento"} or candidato.ato is None:
            return None  # o item anterior segue (leitura parcial ou encerrada já guardada)
        texto = candidato.texto or ""
        recente = candidato.ato.ano >= self.today.year - 1
        if candidato.falha and not recente:
            return None
        inicio, fim = periodo_por_extenso(texto) if texto else (None, None)
        anuncio = bool(texto) and bool(_ANUNCIO.search(_dobra(texto)))
        if not fim and not recente:
            return None
        rotulo = candidato.ato.rotulo if candidato.noticia else candidato.entrada.titulo
        return {
            "candidato": candidato, "inicio": inicio, "fim": fim, "fim_original": fim,
            "anuncio": anuncio, "rotulo": rotulo, "prorrogacoes": [],
            # Notícia não tem assinatura no SEI; a data do Plone é a da publicação.
            "publicado": data_do_ato(texto or None, candidato.entrada.titulo)
            or (candidato.entrada.data if candidato.noticia else None),
        }

    def _prorrogar(self, janelas: list[dict[str, Any]], candidato: Candidato) -> None:
        """Prorrogação atualiza o prazo do mesmo item. Liga pela citação do ato de
        abertura e, sem ela (a Nota 5/2024 não cita o Informe), pela janela cujo
        prazo vem até 120 dias antes da nova data."""
        _, nova = periodo_por_extenso(candidato.texto)
        if not nova:
            self._registrar(f"prorrogação sem data lida: {candidato.entrada.link}")
            return
        citados = {ato.id for ato in atos_do_texto(candidato.texto)}
        alvos = [j for j in janelas if j["candidato"].ato.id in citados and j["fim"]]
        if not alvos:
            limite = (date.fromisoformat(nova) - timedelta(days=120)).isoformat()
            alvos = [
                j for j in janelas
                if j["fim"] and limite <= j["fim"] <= nova and (j["inicio"] or j["fim"]) <= nova
            ]
            alvos = sorted(alvos, key=lambda j: j["fim"])[-1:]
        if not alvos:
            self._registrar(f"prorrogação sem janela correspondente: {candidato.entrada.link}")
            return
        for janela in alvos:
            if nova > janela["fim"]:
                janela["fim"] = nova
                janela["prorrogacoes"].append((candidato, nova))

    def _situacao(self, janela: dict[str, Any]) -> str:
        hoje = self.today.isoformat()
        if janela["fim"]:
            if janela["fim"] < hoje:
                return "closed"
            if janela["inicio"] and janela["inicio"] > hoje:
                return "scheduled"
            return "open"
        if janela["candidato"].texto and janela["anuncio"]:
            return "scheduled"
        return "unknown"

    @staticmethod
    def _sem_anuncio_superado(janelas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Um anúncio sem data sai quando aparece o ato com datas da mesma rodada ou de
        uma posterior; senão ficariam dois itens para a mesma janela."""
        datadas = [j for j in janelas if j["fim"]]
        resultado = []
        for janela in janelas:
            if janela["status"] == "scheduled" and not janela["fim"]:
                referencia = janela["publicado"] or f"{janela['candidato'].ato.ano}-01-01"
                if any((d["inicio"] or d["publicado"] or d["fim"]) >= referencia for d in datadas):
                    continue
            resultado.append(janela)
        return resultado

    def _oportunidade(self, janela: dict[str, Any]) -> dict[str, Any]:
        candidato: Candidato = janela["candidato"]
        texto = _dobra(candidato.texto)
        regime_novo = bool(_LEI_15471.search(texto))
        proponentes = PROPONENTES_LEI_15471 if regime_novo else PROPONENTES_VIGENTES
        parceiras = [] if regime_novo else PARCEIRAS_VIGENTES
        temas = TEMAS_BASE + (TEMAS_MATRIZ if _MATRIZ.search(texto) else [])
        url_ato = candidato.entrada.link.rstrip("/").removesuffix("/view")
        documentos = [{"title": clean_text(janela["rotulo"]), "url": url_ato, "published": janela["publicado"]}]
        partes = []
        if janela["fim"]:
            original = janela["fim_original"]
            if janela["inicio"]:
                partes.append(f"Período de submissão: de {_br(janela['inicio'])} a {_br(original)}")
            else:
                partes.append(f"Prazo de submissão: {_br(original)}")
            partes[-1] += f" ({janela['rotulo']})."
            for prorrogacao, nova in janela["prorrogacoes"]:
                rotulo = prorrogacao.entrada.titulo or (prorrogacao.ato.rotulo if prorrogacao.ato else "")
                partes.append(f"Prorrogado até {_br(nova)} pela {rotulo}.")
                documentos.append({
                    "title": clean_text(rotulo) or "Prorrogação",
                    "url": prorrogacao.entrada.link.rstrip("/").removesuffix("/view"),
                    "published": data_do_ato(prorrogacao.texto),
                })
        elif janela["status"] == "scheduled":
            partes.append(f"Anúncio oficial de submissão ao PDIL, sem prazo lido ({janela['rotulo']}).")
        else:
            motivo = candidato.motivo or "sem data no texto"
            partes.append(f"Ato oficial sobre submissão ao PDIL; o radar não leu o prazo ({motivo}).")
        partes.append(REGRA_LEI_15471 if regime_novo else REGRA_VIGENTE)
        status = janela["status"]
        return opportunity(
            source_id=self.id, source_name=self.name, source_url=url_ato, checked_at=self.checked_at,
            external_id=candidato.ato.id,
            title=f"PDIL — submissão de propostas de projeto ({clean_text(janela['rotulo'])})",
            funder="Ministério da Saúde", status=status,
            published=janela["publicado"], deadline=janela["fim"],
            organization_types=proponentes, partner_types=parceiras, partners=["SECTICS/MS"],
            geography=["BR"], themes=temas,
            # Convênio, TED, encomenda tecnológica, CPSI ou compensação: nenhum valor do
            # enum cobre todos ("contratacao_pdi" só serviria à encomenda e ao CPSI).
            instrument_type="outros", repayable=False,
            documents=documentos, description=" ".join(partes),
            closed=janela["fim"] if status == "closed" else None,
        )

    def snapshot(self) -> dict[str, Any]:
        snapshot = super().snapshot()
        if self._falhas:
            # Leitura parcial: os itens anteriores que não voltaram seguem como estavam
            # (todos os itens do PDIL têm geografia "BR").
            snapshot["failed_scopes"] = ["BR"]
            snapshot["error"] = ("leitura parcial: " + "; ".join(self._falhas))[:500]
        return snapshot


def _br(iso: str | None) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else ""
