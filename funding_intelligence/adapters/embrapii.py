"""EMBRAPII: chamadas de credenciamento de ICT e as quatro portas contínuas da empresa.

Estudo de 14/10/2026 (`radar-fontes-saude/estudo/embrapii.md`). O site é WordPress e o tipo
de post das chamadas (`chamadas_publicas`) não está na API REST; a página de arquivo também
não traz a lista. O caminho estável é o RSS do tipo, que lista as 31 chamadas com o id do post
no `guid` (`?post_type=chamadas_publicas&p=<id>`). O cronograma só está no detalhe, numa
tabela "Atividade × Prazos Limites".

Duas famílias de item:

1. **Chamadas** (canal `chamada_publica`). Todas as de 10/2024 para cá credenciam ICT como
   Unidade ou Centro de Competência; a empresa não propõe nem recebe nelas, só contrata
   projetos depois. Por isso o público é ICT (e hospital quando o edital aceita, como a
   Unidades 01/2025 com o MS). O `external_id` é o id do post: o número da chamada se repete
   entre séries ("Unidades 01/2025" e "CC 01/2025").
2. **Portas contínuas** (canal `fluxo_continuo`): o modelo EMBRAPII, EMBRAPII-Sebrae,
   EMBRAPII-BNDES Saúde e Alto Impacto em Saúde. Não têm edital nem prazo; o adaptador só
   confere, a cada rodada, se a página oficial ainda diz o que a descrição afirma. Se uma
   frase-âncora some, a porta não é emitida e o aviso vai no relatório da coleta.

Orçamento (5 a 7 pedidos por rodada): RSS + 4 portas + até 2 detalhes. A "memória" é o
catálogo publicado (`public/data/funding-opportunities-v2.json`, que o job já tem no
checkout): uma chamada que ele já guarda como encerrada não precisa ser relida, porque o
pipeline a mantém por 24 meses. Relemos só a nova e a viva (aberta, prevista, incerta ou
encerrada há menos de uma semana). Na carga histórica, lemos o detalhe de todas as chamadas
dos últimos 24 meses (hoje, 8).
"""
from __future__ import annotations

import copy
import http.client
import json
import re
import time
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from funding_intelligence.adapters.base import BaseAdapter
from funding_intelligence.models import ascii_fold, clean_text, opportunity


FEED_URL = "https://embrapii.org.br/chamadas-publicas/feed/"
CATALOGO_PADRAO = Path(__file__).resolve().parents[2] / "public" / "data" / "funding-opportunities-v2.json"

# A migração do site (02 e 03/10/2024) regravou o `pubDate` das 23 chamadas antigas. Esse
# `pubDate` não é a data de publicação: a chamada é anterior a ele, e fica fora da janela.
MIGRACAO_ATE = date(2024, 10, 4)
JANELA_DIAS = 730
# Uma encerrada é relida por mais uma semana, para pegar a prorrogação de última hora.
CARENCIA_DIAS = 7
DETALHES_POR_RODADA = 2
DETALHES_CARGA_HISTORICA = 25
PAUSA_S = 1.5
BRASILIA = timezone(timedelta(hours=-3))

NORTE = ["AC", "AM", "AP", "PA", "RO", "RR", "TO"]

# Linhas do cronograma, já sem acento e em minúsculas. O prazo é a primeira entrega pública
# (Etapa 1): a Etapa 2 é só para quem passou na primeira.
LINHA_ENTREGA = re.compile(r"submiss|envio|inscri")
LINHA_NAO_ENTREGA = re.compile(r"inicio|recurso|resultado|resposta|esclarec|duvida|impugna")
LINHA_ABERTURA = re.compile(r"^\W*\d*\W*(?:abertura|publicacao|lancamento)")
CHAMADA_INTERROMPIDA = re.compile(r"chamada (?:encerrada|cancelada|suspensa|revogada)")
RESULTADO_PUBLICADO = re.compile(r"\bresultado(?: final| da chamada)?\s*:|chamada encerrada")
# Linguagem de fim numa porta contínua. "até que se esgotem os recursos" é o texto normal do
# Alto Impacto; só conta o sujeito seguido do fim ("os recursos foram esgotados").
PORTA_ENCERRADA = re.compile(
    r"\b(?:recursos|chamada|programa|iniciativa|parceria|submissao|apresentacao dos projetos|projetos)"
    r"\s+(?:\S+\s+){0,3}?(?:encerrad|esgotad|suspens)\w*"
)
DESAFIO = re.compile(rb"captcha|cf-chl|just a moment|attention required|access to this site has been limited")

PARCEIROS = (
    (r"ministerio da saude|\bsectics\b", "MS"),
    (r"ministerio da ciencia, tecnologia e inovacao|\bmcti\b", "MCTI"),
    (r"ministerio da educacao|\bmec\b", "MEC"),
    (r"ministerio do desenvolvimento, industria|\bmdic\b", "MDIC"),
    (r"\bcnpq\b|conselho nacional de desenvolvimento cientifico", "CNPq"),
    (r"\bsebrae\b", "Sebrae"),
    (r"\bbndes\b", "BNDES"),
    (r"\bfinep\b", "Finep"),
    (r"\bfapesp\b", "FAPESP"),
)

TEMAS = (
    (r"saude", "saude"),
    (r"biofarmac", "biofarmacos"),
    (r"farmac", "farmacos"),
    (r"insumos? farmaceuticos? ativos?|\bifas?\b", "ifa"),
    (r"dispositivos? medicos?", "dispositivos_medicos"),
    (r"saude digital", "saude_digital"),
    (r"\brna\b|vacina", "biotecnologia"),
    (r"biodiversidade", "bioeconomia"),
    (r"hidrogenio", "hidrogenio"),
    (r"hidrogenio|descarboniz|baixa emissao", "descarbonizacao"),
    (r"inteligencia artificial", "inteligencia_artificial"),
)

# As portas contínuas. Cada fato da descrição tem a sua frase-âncora na página oficial: se a
# página muda o número, a porta sai até alguém rever o texto. As âncoras são comparadas sem
# acento, sem caixa e com os espaços colapsados.
PORTAS = (
    {
        "external_id": "modelo-continuo",
        "url": "https://embrapii.org.br/interna-embrapii-nos-estados/",
        "title": "Projeto de PD&I da empresa com uma Unidade EMBRAPII (modelo EMBRAPII, sem edital)",
        "funder": "EMBRAPII",
        "partners": [],
        "themes": ["inovacao", "inovacao_industrial"],
        "description": (
            "A empresa negocia o projeto de PD&I diretamente com uma Unidade EMBRAPII, a qualquer "
            "momento e sem edital. A EMBRAPII aporta até 1/3 do valor do projeto em recursos não "
            "reembolsáveis, que ficam na Unidade: a empresa paga a sua parte à Unidade, que executa o "
            "projeto. Fonte: EMBRAPII, página \"Como fazer parceria com a Embrapii\"."
        ),
        "ancoras": (
            "sem necessidade de um edital",
            "negocia o projeto diretamente com a Unidade",
            "até 1/3 do valor do projeto",
            "recursos estão disponíveis nas Unidades",
        ),
    },
    {
        "external_id": "sebrae",
        "url": "https://embrapii.org.br/sebrae/",
        "title": "EMBRAPII-Sebrae: projeto de inovação de micro e pequena empresa com uma Unidade EMBRAPII",
        "funder": "EMBRAPII / Sebrae",
        "partners": ["Sebrae"],
        "themes": ["inovacao", "inovacao_industrial", "empreendedorismo"],
        "description": (
            "Micro e pequenas empresas (faturamento anual de até R$ 4,8 milhões) e startups contratam o "
            "projeto de inovação com uma Unidade EMBRAPII, sem edital. A EMBRAPII aporta até 50% do "
            "projeto e o Sebrae cobre até 70% da contrapartida da empresa, com teto de R$ 150 mil a "
            "R$ 300 mil conforme a modalidade. A Unidade gere os recursos e presta contas. Fonte: "
            "EMBRAPII, página \"Sebrae\"."
        ),
        "ancoras": (
            "sem edital",
            "aporta até 50% do montante total do projeto",
            "até 70% da contrapartida",
            "faturamento anual de até R$ 4,8 milhões",
            "R$ 150 mil",
            "R$ 300 mil",
            "gerenciamento do projeto é feito pela Unidade Embrapii",
        ),
    },
    {
        "external_id": "bndes-saude",
        "url": "https://embrapii.org.br/bndes/",
        "title": "EMBRAPII-BNDES Saúde: projeto de PD&I de empresa do Complexo Industrial da Saúde",
        "funder": "EMBRAPII / BNDES (Funtec)",
        "partners": ["BNDES"],
        "themes": ["saude", "farmacos", "biofarmacos", "biotecnologia", "dispositivos_medicos", "ifa",
                   "pesquisa_clinica", "inovacao"],
        "description": (
            "Empresas do Complexo Industrial da Saúde contratam o projeto com uma Unidade EMBRAPII "
            "habilitada na parceria com o BNDES, sem edital; as Unidades habilitadas recebem recursos "
            "do Funtec. Empresa com receita operacional bruta (ROB) inferior a R$ 90 milhões recebe até "
            "50% do valor do projeto em recursos não reembolsáveis; acima de R$ 90 milhões, até 1/3. "
            "Temas: novos medicamentos, fármacos e biofármacos, dispositivos médicos e IFA; para "
            "fármacos, TRL 3 a 7 (até a conclusão da Fase 2), de preferência testes pré-clínicos e "
            "clínicos de Fase 1 e 2. Fonte: EMBRAPII, página \"BNDES\", foco temático Saúde."
        ),
        "ancoras": (
            "Empresas do Complexo Industrial da Saúde",
            "Funtec",
            "inferior a R$ 90 milhões podem receber até 50%",
            "superior a R$ 90 milhões podem receber até 1/3",
            "Fármacos e biofármacos",
            "a 7 (conclusão de estudos clínicos da Fase 2)",
        ),
    },
    {
        "external_id": "alto-impacto-saude",
        "url": "https://embrapii.org.br/projetos-de-alto-impacto/",
        "title": "Projetos de Alto Impacto em Saúde (EMBRAPII/MS): aliança de empresas com duas Unidades EMBRAPII",
        "funder": "EMBRAPII / Ministério da Saúde",
        "partners": ["MS"],
        "themes": ["saude", "dispositivos_medicos", "diagnostico", "saude_digital", "ifa",
                   "anticorpos_monoclonais", "biotecnologia", "inovacao"],
        "program_budget": 60_000_000.0,
        "inicio": r"se inicia em (\d{1,2}/\d{1,2}/\d{4})",
        "description": (
            "Fluxo contínuo desde 25/07/2025, até que se esgotem os R$ 60 milhões (EMBRAPII/Ministério "
            "da Saúde). A aliança tem uma ou mais empresas e no mínimo duas Unidades EMBRAPII; a EMBRAPII "
            "aporta R$ 5 milhões ou mais por projeto, até 60% (1 empresa e 2 Unidades) ou 70% (2 empresas "
            "e 2 Unidades), e cada empresa líder aporta ao menos 10%. Resultados entre TRL 3 e 7. Temas: "
            "dispositivos médicos e diagnóstico, tecnologias digitais em saúde, IFA e anticorpos "
            "monoclonais. A Unidade coordenadora cadastra a proposta no SRINFO. Fonte: EMBRAPII, página "
            "\"Projetos de Alto Impacto\" (Orientação Operacional 02/2025)."
        ),
        "ancoras": (
            "fluxo contínuo até que se esgotem os recursos",
            "R$ 60.000.000,00",
            "Embrapii/Ministério da Saúde",
            "igual ou superior a R$ 5 milhões",
            "02 Unidades Embrapii 60%",
            "02 Unidades Embrapii 70%",
            "devem aportar 10% cada",
            "entre 3 e 7 na escala de TRL",
            "no mínimo, duas Unidades Embrapii",
        ),
    },
)

_PADRAO = object()

# O que conta como "a página não respondeu" (e não como erro do adaptador).
FALHAS_DE_REDE = (OSError, urllib.error.URLError, http.client.HTTPException, ValueError)


class Bloqueio(Exception):
    """403, 429, CAPTCHA ou página de desafio: não se pede mais nada ao host nesta rodada."""


def _normalizar(texto: str | None) -> str:
    return re.sub(r"\s+", " ", ascii_fold(texto)).strip()


class EmbrapiiAdapter(BaseAdapter):
    id = "embrapii"
    name = "EMBRAPII"
    url = "https://embrapii.org.br/chamadas-publicas/"

    def __init__(self, *, fixture: str | None = None, today: date | None = None,
                 catalogo: Any = _PADRAO):
        super().__init__(fixture=fixture, today=today)
        # Com fixture (teste, desenvolvimento), sem memória, salvo se o teste passar uma.
        self.catalogo = (None if fixture else CATALOGO_PADRAO) if catalogo is _PADRAO else catalogo
        self.pausa = 0.0 if fixture else PAUSA_S
        self.pedidos = 0
        self.avisos: list[str] = []
        self._graves: list[str] = []
        self._ultimo: dict[str, float] = {}
        self._bloqueados: set[str] = set()

    # ------------------------------------------------------------------ coleta

    def collect(self) -> list[dict[str, Any]]:
        memoria = self._memoria()
        # Sem o RSS não há lista: a coleta falha inteira e o pipeline guarda o anterior como
        # defasado, em vez de encerrar as chamadas que não vieram.
        itens = self._ler_feed(self._get(FEED_URL, "feed.xml"))
        if not itens:
            raise ValueError("RSS das chamadas da EMBRAPII veio sem itens")
        found = self._chamadas(itens, memoria)
        found.extend(self._portas(memoria))
        return found

    def snapshot(self) -> dict[str, Any]:
        result = super().snapshot()
        result["requests"] = self.pedidos
        if self.avisos:
            result["warnings"] = list(self.avisos)
        if self._graves:
            # Fonte saudável com ressalva, como a UF que não respondeu no PPSUS: o texto vai para
            # a saúde da fonte no catálogo, para alguém conferir a página.
            result["error"] = "; ".join(self._graves)[:500]
        return result

    # ------------------------------------------------------------------ chamadas

    def _ler_feed(self, body: bytes) -> list[dict[str, Any]]:
        root = ET.fromstring(body)
        itens = []
        for node in root.iter("item"):
            guid = node.findtext("guid") or ""
            post = re.search(r"[?&](?:amp;)?p=(\d+)", guid)
            link = (node.findtext("link") or "").strip()
            if not post or not link:
                self._avisar(f"item do RSS sem id de post: {clean_text(node.findtext('title'))[:80]}", grave=False)
                continue
            published = None
            try:
                published = parsedate_to_datetime(node.findtext("pubDate") or "").astimezone(BRASILIA).date()
            except (TypeError, ValueError):
                pass
            itens.append({
                "post": post.group(1),
                "link": link,
                "title": clean_text(node.findtext("title")),
                "pub": published,
                "migrada": published is None or published <= MIGRACAO_ATE,
            })
        return itens

    def _chamadas(self, itens: list[dict[str, Any]], memoria: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        inicio = self.today - timedelta(days=JANELA_DIAS)
        janela = sorted(
            (item for item in itens if not item["migrada"] and item["pub"] >= inicio),
            key=lambda item: item["pub"], reverse=True,
        )
        if self.historical:
            fila, limite = janela, DETALHES_CARGA_HISTORICA
        else:
            # Primeiro a nova (o que ninguém viu ainda), depois a viva pelo prazo mais próximo.
            novas = [item for item in janela if item["post"] not in memoria]
            vivas = sorted(
                (item for item in janela if item["post"] in memoria and self._viva(memoria[item["post"]])),
                key=lambda item: memoria[item["post"]]["dates"].get("deadline") or "9999-12-31",
            )
            fila, limite = novas + vivas, DETALHES_POR_RODADA

        found: list[dict[str, Any]] = []
        adiadas = 0
        for posicao, item in enumerate(fila):
            anterior = memoria.get(item["post"])
            if posicao >= limite or self._bloqueados:
                adiadas += 1
                if anterior:
                    found.append(self._repetir(anterior))
                continue
            try:
                html = self._get(item["link"], f"post-{item['post']}.html").decode("utf-8", errors="replace")
            except Bloqueio:
                adiadas += 1
                if anterior:
                    found.append(self._repetir(anterior))
                continue
            except FALHAS_DE_REDE as exc:
                self._avisar(f"chamada {item['post']}: detalhe não respondeu ({type(exc).__name__})")
                if anterior:
                    found.append(self._repetir(anterior))
                continue
            found.append(self._chamada(item, html))
        if adiadas:
            # Informativo: a que já estava no catálogo seguiu como estava (defasada); a nova
            # entra numa das próximas rodadas, das mais recentes para as mais antigas.
            self._avisar(f"{adiadas} chamada(s) sem leitura do detalhe nesta rodada; ficam para a próxima", grave=False)
        return found

    def _viva(self, anterior: dict[str, Any]) -> bool:
        if anterior.get("status") != "closed":
            return True
        prazo = (anterior.get("dates") or {}).get("deadline")
        return bool(prazo) and prazo >= (self.today - timedelta(days=CARENCIA_DIAS)).isoformat()

    def _chamada(self, item: dict[str, Any], html: str) -> dict[str, Any]:
        soup = self.soup(html)
        body = soup.find("body")
        classe = " ".join(body.get("class", [])) if body else ""
        post_pagina = re.search(r"\bpostid-(\d+)\b", classe)
        if post_pagina and post_pagina.group(1) != item["post"]:
            self._avisar(f"chamada {item['post']}: a página diz postid-{post_pagina.group(1)}", grave=False)

        conteudo = soup.select_one("div.chamadas-publicas-content") or body or soup
        introducao = self._introducao(conteudo)
        intro_texto = _normalizar(" ".join(node.get_text(" ", strip=True) for node in introducao))
        # Parceiro e tema vêm da apresentação; o bloco "RESULTADO" fala das selecionadas (o
        # tema de cada Unidade não é o da chamada). Ele só serve para o público: ter
        # selecionado hospital prova que o edital aceitava hospital.
        apresentacao = re.split(r"\bresultado\b", intro_texto, maxsplit=1)[0]
        corpo = _normalizar(" ".join(
            node.get_text(" ", strip=True) for node in conteudo.find_all(recursive=False)
            if node.name != "table" and not node.find("table")
        ))

        abertura, prazo = self._cronograma(conteudo)
        published = abertura or item["pub"].isoformat()
        hoje = self.today.isoformat()
        if CHAMADA_INTERROMPIDA.search(corpo):
            status = "closed"
        elif abertura and abertura > hoje:
            status = "scheduled"
        elif prazo:
            status = "open" if prazo >= hoje else "closed"
        elif RESULTADO_PUBLICADO.search(corpo):
            status = "closed"
        else:
            status = "unknown"

        partners = [nome for padrao, nome in PARCEIROS if re.search(padrao, apresentacao)]
        title = item["title"]
        description = next(
            (clean_text(node.get_text(" ", strip=True)) for node in introducao
             if node.name == "p" and len(clean_text(node.get_text(" ", strip=True))) >= 60),
            None,
        )
        documentos = self._documentos(conteudo, item["link"])
        # O tema da chamada de Centro de Competência ("em RNA Mensageiro", "em IFA") às vezes só
        # aparece no título do edital, na lista de arquivos.
        arquivos = _normalizar(" ".join(
            [doc["title"] for doc in documentos]
            + [table.get_text(" ", strip=True) for table in conteudo.find_all("table")
               if not self._e_cronograma(table)]
        ))
        return opportunity(
            source_id=self.id, source_name=self.name, source_url=item["link"],
            checked_at=self.checked_at, external_id=item["post"], title=title,
            funder=" / ".join(["EMBRAPII", *partners]), status=status,
            published=published, deadline=prazo,
            organization_types=self._proponentes(intro_texto),
            geography=NORTE if "regiao norte" in _normalizar(title) else ["BR"],
            themes=self._temas(_normalizar(title), apresentacao + " " + arquivos),
            instrument_type="credenciamento", repayable=False,
            documents=documentos,
            description=description[:800] if description else None,
            channel="chamada_publica", partners=partners,
        )

    @staticmethod
    def _introducao(conteudo: Any) -> list[Any]:
        """Os blocos antes do cronograma: apresentação, parceria e, quando houver, o resultado."""
        blocos = []
        for node in conteudo.find_all(recursive=False):
            classes = node.get("class") or []
            if node.name in {"table", "figure"} or "cronograma-title" in classes or node.find("table"):
                break
            blocos.append(node)
        return blocos

    @staticmethod
    def _cronograma(conteudo: Any) -> tuple[str | None, str | None]:
        """(abertura, prazo) da tabela "Atividade × Prazos Limites"."""
        abertura = prazo = None
        for table in conteudo.find_all("table"):
            if not EmbrapiiAdapter._e_cronograma(table):
                continue
            for atividade, *_, valor in EmbrapiiAdapter._linhas(table)[1:]:
                rotulo = _normalizar(atividade)
                if abertura is None and LINHA_ABERTURA.search(rotulo) and "resultado" not in rotulo:
                    datas = EmbrapiiAdapter._datas(valor)
                    abertura = datas[0] if datas else None
                if prazo is None and LINHA_ENTREGA.search(rotulo) and not LINHA_NAO_ENTREGA.search(rotulo):
                    if re.search(r"a definir|a partir", _normalizar(valor)):
                        continue
                    datas = EmbrapiiAdapter._datas(valor)
                    prazo = datas[-1] if datas else None
            if abertura or prazo:
                break
        return abertura, prazo

    @staticmethod
    def _linhas(table: Any) -> list[list[str]]:
        linhas = [
            [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
            for row in table.find_all("tr")
        ]
        return [linha for linha in linhas if len(linha) >= 2]

    @staticmethod
    def _e_cronograma(table: Any) -> bool:
        """A tabela "Atividade × Prazos Limites" (ou "Etapa × Data", no modelo antigo)."""
        linhas = EmbrapiiAdapter._linhas(table)
        return bool(linhas) and bool(re.search(r"atividade|etapa", _normalizar(linhas[0][0])))

    @staticmethod
    def _datas(valor: str) -> list[str]:
        """Datas de uma célula. "08/06 a 26/06/2026": o ano do fim vale para o começo."""
        achadas = re.findall(r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{4}))?(?!\d)", valor or "")
        result: list[str] = []
        ano_seguinte = None
        for dia, mes, ano in reversed(achadas):
            ano = ano or ano_seguinte
            if not ano:
                continue
            ano_seguinte = ano
            try:
                result.append(date(int(ano), int(mes), int(dia)).isoformat())
            except ValueError:
                continue
        return list(reversed(result))

    @staticmethod
    def _proponentes(intro: str) -> list[str]:
        # Credenciamento é de ICT (pública ou privada sem fins lucrativos). A Unidades 02/2025
        # foi só para Institutos Federais, que são ICT pública.
        if "institutos federais" in intro and "privad" not in intro:
            tipos = ["ict"]
        else:
            tipos = ["ict", "ict_privada", "universidade"]
        if "hospita" in intro:
            tipos.append("hospital")
        return tipos

    @staticmethod
    def _temas(titulo: str, texto: str) -> list[str]:
        temas = ["inovacao"]
        # "Capacitação" só pelo título: o modelo de Centro de Competência cita "Formação e
        # Capacitação de RH" em toda chamada, e lá a chamada é de credenciamento.
        if "capacitacao" in titulo:
            temas.append("capacitacao")
        texto = titulo + " " + texto
        for padrao, tema in TEMAS:
            if re.search(padrao, texto) and tema not in temas:
                temas.append(tema)
        return temas

    @staticmethod
    def _documentos(conteudo: Any, base: str) -> list[dict[str, Any]]:
        documentos: list[dict[str, Any]] = []
        vistos: set[str] = set()

        def incluir(titulo: str, href: str | None) -> None:
            if not href or href.lower().startswith(("mailto:", "#", "javascript:")):
                return
            url = urljoin(base, href)
            if url in vistos or urlsplit(url).scheme not in {"http", "https"}:
                return
            vistos.add(url)
            documentos.append({"title": titulo or "Documento da chamada", "url": url, "published": None})

        for bloco in conteudo.select("div.single-item-listagem"):
            link = bloco.select_one("a[href]")
            titulo = bloco.select_one(".title-single-item-listagem")
            incluir(clean_text(titulo.get_text(" ", strip=True)) if titulo else "", link.get("href") if link else None)
        for link in conteudo.select("a[href]"):
            href = link.get("href") or ""
            if re.search(r"\.(?:pdf|docx?|xlsx?)(?:$|\?)", href, re.I):
                incluir(clean_text(link.get_text(" ", strip=True)), href)
        return documentos

    # ------------------------------------------------------------------ portas contínuas

    def _portas(self, memoria: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        found = []
        for porta in PORTAS:
            anterior = memoria.get(porta["external_id"])
            try:
                html = self._get(porta["url"], f"porta-{porta['external_id']}.html")
            except Bloqueio:
                if anterior:
                    found.append(self._repetir(anterior))
                continue
            except FALHAS_DE_REDE as exc:
                self._avisar(f"porta {porta['external_id']}: página não respondeu ({type(exc).__name__})")
                if anterior:
                    found.append(self._repetir(anterior))
                continue
            texto = self._texto_pagina(html.decode("utf-8", errors="replace"))
            faltam = [ancora for ancora in porta["ancoras"] if _normalizar(ancora) not in texto]
            if faltam:
                self._avisar(
                    f"porta {porta['external_id']} não emitida: sumiu a frase-âncora "
                    f"\"{faltam[0]}\" de {porta['url']}"
                )
                continue
            fim = PORTA_ENCERRADA.search(texto)
            if fim:
                self._avisar(f"porta {porta['external_id']} não emitida: a página fala em \"{fim.group(0)}\"")
                continue
            inicio = re.search(porta["inicio"], texto) if porta.get("inicio") else None
            published = next(iter(self._datas(inicio.group(1))), None) if inicio else None
            found.append(opportunity(
                source_id=self.id, source_name=self.name, source_url=porta["url"],
                checked_at=self.checked_at, external_id=porta["external_id"], title=porta["title"],
                funder=porta["funder"], status="open", published=published, deadline=None,
                organization_types=["empresa", "startup"], geography=["BR"], themes=porta["themes"],
                instrument_type="contratacao_pdi", repayable=False,
                program_budget=porta.get("program_budget"), description=porta["description"],
                channel="fluxo_continuo", partners=porta["partners"],
                documents=[{"title": "Página oficial da EMBRAPII", "url": porta["url"], "published": None}],
            ))
        return found

    def _texto_pagina(self, html: str) -> str:
        soup = self.soup(html)
        for node in soup(["script", "style", "noscript", "form", "select", "iframe", "svg"]):
            node.decompose()
        return _normalizar((soup.find("body") or soup).get_text(" ", strip=True))

    # ------------------------------------------------------------------ apoio

    def _memoria(self) -> dict[str, dict[str, Any]]:
        """Itens desta fonte no catálogo publicado, por `external_id`."""
        if self.catalogo is None:
            return {}
        if isinstance(self.catalogo, dict):
            data = self.catalogo
        else:
            try:
                data = json.loads(Path(self.catalogo).read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError) as exc:
                self._avisar(f"catálogo anterior ilegível ({type(exc).__name__}); sem memória", grave=False)
                return {}
        return {
            op["external_id"]: op for op in data.get("opportunities", [])
            if (op.get("source") or {}).get("id") == self.id and op.get("external_id")
        }

    @staticmethod
    def _repetir(anterior: dict[str, Any]) -> dict[str, Any]:
        """O item do catálogo, como estava, marcado defasado: a falha de hoje não o encerra."""
        item = copy.deepcopy(anterior)
        item["source"]["stale"] = True
        return item

    def _avisar(self, texto: str, *, grave: bool = True) -> None:
        self.avisos.append(texto)
        if grave:
            self._graves.append(texto)

    def _get(self, url: str, nome_fixture: str) -> bytes:
        """Um pedido educado: 1,5 s entre pedidos ao host; para no primeiro bloqueio."""
        host = urlsplit(url).netloc
        if host in self._bloqueados:
            raise Bloqueio(host)
        self.pedidos += 1
        if self.fixture:
            return (Path(self.fixture) / nome_fixture).read_bytes()
        espera = self.pausa - (time.monotonic() - self._ultimo.get(host, float("-inf")))
        if espera > 0:
            time.sleep(espera)
        try:
            body = self.fetch_bytes(url)
        except urllib.error.HTTPError as exc:
            exc.close()  # o corpo do erro não é lido; fecha a conexão já
            if exc.code in (403, 429):
                self._bloquear(host, f"HTTP {exc.code}", url)
            raise
        finally:
            self._ultimo[host] = time.monotonic()
        if DESAFIO.search(body[:4096].lower()):
            self._bloquear(host, "página de desafio", url)
        return body

    def _bloquear(self, host: str, motivo: str, url: str) -> None:
        self._bloqueados.add(host)
        quando = datetime.now(BRASILIA).strftime("%d/%m/%Y %H:%M")
        self._avisar(f"{host} bloqueou ({motivo}) em {url} às {quando}; coleta parada nesse host")
        raise Bloqueio(host)
