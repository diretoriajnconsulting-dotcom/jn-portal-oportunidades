"""Programas com prazo aberto nas APIs públicas novas do TransfereGov.

Três módulos, três adaptadores. Cada janela de recebimento de um programa vira uma
oportunidade (um programa com duas janelas abertas vira duas, como no SICONV):

  · Transferências especiais: a janela de ciência da indicação e cadastro do plano
    de ação, aberta a todo ente com emenda individual indicada;
  · Fundo a fundo: as janelas de plano de ação para beneficiários de emenda,
    voluntários e específicos;
  · Parcerias: as mesmas três janelas (espontâneo, emenda e específico) dos programas
    do módulo de gestão de parcerias, que inclui o fundo a fundo do SUAS.

O que fica de fora, e por quê:

  · programa que não está disponibilizado, e programa de mais de um ano atrás. No
    fundo a fundo, programas de 2020 a 2022 mantêm janela aberta até 31/12/2026 para
    reformulação de quem já aderiu; publicar isso como oportunidade engana;
  · TED (termo de execução descentralizada): só órgão federal recebe. Decisão do
    titular em 15/09/2026, para não confundir prefeitura e OSC.

As APIs são FastAPI com `pagina`/`tamanho_da_pagina` (máximo 200 em especiais e
parcerias, 1.000 no fundo a fundo), sem ordenação e ignorando parâmetro desconhecido.
A leitura confere o total anunciado contra o que chegou.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
from datetime import date
from pathlib import Path
from typing import Any

from funding_intelligence.adapters.base import BaseAdapter
from funding_intelligence.models import THEME_ALIASES, ascii_fold, clean_text, opportunity

API = "https://api-publica.transferegov.gestao.gov.br"
ESPERAS_S = (10, 30, 90)


def numero(valor: Any) -> float | None:
    try:
        return float(valor) if valor not in (None, "") else None
    except (TypeError, ValueError):
        return None


class LeituraIncompleta(RuntimeError):
    """A API anunciou um total e entregou outro, ou mudou o total no meio da leitura."""


def temas_do_texto(*textos: str | None) -> list[str]:
    """Só os temas canônicos que o texto menciona.

    `canonical_themes` também guarda o próprio texto como tema; com o nome inteiro
    de um programa isso viraria um "tema" por programa, que o site ignora.
    """
    dobrado = ascii_fold(" ".join(t for t in textos if t))
    return list(dict.fromkeys(canonical for pattern, canonical in THEME_ALIASES if re.search(pattern, dobrado)))


def janela(inicio: str | None, fim: str | None, hoje: date) -> str | None:
    """`open`, `scheduled` ou None (sem data, ou já encerrada)."""
    if not fim:
        return None
    fim_d = date.fromisoformat(str(fim)[:10])
    if fim_d < hoje:
        return None
    if inicio and date.fromisoformat(str(inicio)[:10]) > hoje:
        return "scheduled"
    return "open"


class _ApiTransferegov(BaseAdapter):
    """Leitura paginada e conferida de um recurso. Com `fixture`, lê as linhas de um JSON."""

    modulo: str
    recurso: str
    tamanho_pagina = 200

    def linhas(self) -> list[dict[str, Any]]:
        if self.fixture:
            return json.loads(Path(self.fixture).read_text(encoding="utf-8"))
        return self.ler_paginas()

    def buscar(self, url: str) -> dict[str, Any]:
        for tentativa in range(len(ESPERAS_S) + 1):
            try:
                return json.loads(self.request_bytes(url, headers={"Accept": "application/json"}).decode("utf-8"))
            except urllib.error.HTTPError as erro:
                # 403 e 429 são bloqueio passageiro (em 15/09/2026 o TransfereGov devolveu
                # 403 a runner do GitHub por minutos); outro 4xx é erro de chamada.
                if (erro.code not in (403, 429) and erro.code < 500) or tentativa == len(ESPERAS_S):
                    raise
            except (urllib.error.URLError, TimeoutError):
                if tentativa == len(ESPERAS_S):
                    raise
            time.sleep(ESPERAS_S[tentativa])
        raise AssertionError("inalcançável")

    def ler_paginas(self) -> list[dict[str, Any]]:
        base = f"{API}/{self.modulo}/{self.recurso}?tamanho_da_pagina={self.tamanho_pagina}&pagina="
        primeira = self.buscar(base + "1")
        total = int(primeira.get("total_items") or 0)
        paginas = int(primeira.get("total_pages") or 0)
        linhas = list(primeira.get("data") or [])
        for pagina in range(2, paginas + 1):
            corpo = self.buscar(base + str(pagina))
            if int(corpo.get("total_items") or 0) != total:
                raise LeituraIncompleta(f"{self.recurso}: total mudou durante a leitura")
            linhas.extend(corpo.get("data") or [])
        if len(linhas) != total:
            raise LeituraIncompleta(f"{self.recurso}: leu {len(linhas)} de {total} linhas anunciadas")
        return linhas

    @staticmethod
    def abertura(inicio: str | None, ano: Any) -> str | None:
        """Data de abertura, ou None quando é anterior ao ano do programa menos um.

        O programa 111 de parcerias (2026) abre em 06/02/2016: erro de digitação na fonte.
        Publicar "aberto desde 2016" seria afirmar o que o dado não sustenta.
        """
        if not inicio:
            return None
        try:
            return str(inicio)[:10] if int(str(inicio)[:4]) >= int(ano) - 1 else None
        except (TypeError, ValueError):
            return None

    def recente(self, ano: Any) -> bool:
        try:
            return int(ano) >= self.today.year - 1
        except (TypeError, ValueError):
            return False


class EspeciaisAdapter(_ApiTransferegov):
    id = "transferegov-especiais"
    name = "TransfereGov · Transferências especiais"
    url = "https://especiais.transferegov.sistema.gov.br/"
    modulo = "especiais"
    recurso = "programas-especiais"

    def collect(self) -> list[dict[str, Any]]:
        found = []
        for p in self.linhas():
            status = janela(p.get("data_inicio_ciencia_programa"), p.get("data_fim_ciencia_programa"), self.today)
            if not status:
                continue
            codigo = str(p.get("codigo_programa") or "")
            ciclo = codigo.rsplit("-", 1)[1] if "-" in codigo else "1"
            found.append(opportunity(
                source_id=self.id, source_name=self.name, source_url=self.url, checked_at=self.checked_at,
                external_id=f"programa-{p['id_programa']}",
                title=f"Transferências especiais {p.get('ano_programa')} · {ciclo}º ciclo · ciência e plano de ação",
                funder=p.get("nome_orgao_superior_programa") or "Governo Federal",
                status=status, published=self.abertura(p.get("data_inicio_ciencia_programa"), p.get("ano_programa")),
                deadline=str(p["data_fim_ciencia_programa"])[:10],
                organization_types=["municipio", "estado"], geography=["BR"], themes=[],
                instrument_type="outros", repayable=False,
                program_budget=numero(p.get("valor_necessidade_financeira_programa")),
                description=(
                    "Prazo para o ente com emenda individual de transferência especial (emenda Pix) dar ciência da "
                    "indicação e cadastrar o plano de ação. Sem ciência no prazo, o plano fica impedido."
                ),
                channel="emenda_parlamentar",
            ))
        return found


# Janela da API → canal do contrato 2.1.
CANAIS_FUNDO = (("voluntarios", "voluntaria"), ("emendas", "emenda_parlamentar"), ("especificos", "beneficiario_especifico"))


class FundoAFundoAdapter(_ApiTransferegov):
    id = "transferegov-fundoafundo"
    name = "TransfereGov · Fundo a fundo"
    url = "https://fundos.transferegov.sistema.gov.br/"
    modulo = "fundoafundo"
    recurso = "programas"
    tamanho_pagina = 1000

    def collect(self) -> list[dict[str, Any]]:
        found = []
        for p in self.linhas():
            if p.get("situacao_programa") != "DISPONIBILIZADO" or not self.recente(p.get("ano_programa")):
                continue
            for sufixo, canal in CANAIS_FUNDO:
                inicio = p.get(f"data_inicio_recebimento_planos_acao_beneficiarios_{sufixo}")
                fim = p.get(f"data_fim_recebimento_planos_acao_beneficiarios_{sufixo}")
                status = janela(inicio, fim, self.today)
                if not status:
                    continue
                nome = clean_text(p.get("nome_programa")) or "Programa fundo a fundo"
                found.append(opportunity(
                    source_id=self.id, source_name=self.name, source_url=self.url, checked_at=self.checked_at,
                    external_id=f"{p['id_programa']}-{canal}",
                    title=nome, funder=p.get("nome_orgao_superior_programa") or "Governo Federal",
                    status=status, published=self.abertura(inicio, p.get("ano_programa")), deadline=str(fim)[:10],
                    organization_types=["municipio", "estado"], geography=["BR"],
                    themes=temas_do_texto(nome, p.get("nome_fundo_programa")),
                    instrument_type="outros", repayable=False, program_budget=numero(p.get("valor_global_programa")),
                    description=p.get("objetivo_programa"), channel=canal,
                ))
        return found


CANAIS_PARCERIAS = (("espontaneo", "voluntaria"), ("emenda", "emenda_parlamentar"), ("especifico", "beneficiario_especifico"))

# `programa_atende_a` da API → tipo de organização do contrato.
ATENDE_A = {
    "terceiro setor": "osc",
    "administracao publica municipal": "municipio",
    "administracao publica estadual ou do distrito federal": "estado",
    "consorcio publico": "consorcio_publico",
}

# Quando o programa não diz a quem atende, o instrumento diz.
POR_INSTRUMENTO = (
    ("fundo a fundo", ["municipio", "estado"]),
    ("lei de incentivo", ["osc"]),
    ("contrato de gestao", ["osc"]),
)

UFS = {
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR",
    "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
}


class ParceriasAdapter(_ApiTransferegov):
    id = "transferegov-parcerias"
    name = "TransfereGov · Parcerias"
    url = "https://parcerias.transferegov.sistema.gov.br/"
    modulo = "parcerias"
    recurso = "programa"

    @staticmethod
    def tipos(p: dict[str, Any]) -> list[str]:
        tipos = [ATENDE_A[chave] for a in p.get("programa_atende_a") or []
                 if (chave := ascii_fold(a.get("tp_atende_a"))) in ATENDE_A]
        if tipos:
            return list(dict.fromkeys(tipos))
        instrumento = ascii_fold(p.get("tp_instrumento"))
        for trecho, por_instrumento in POR_INSTRUMENTO:
            if trecho in instrumento:
                return por_instrumento
        return ["outros"]

    @staticmethod
    def geografia(p: dict[str, Any]) -> list[str]:
        ufs = sorted({u.get("sg_uf") for u in p.get("ufs_habilitadas") or []} & UFS)
        return ufs if ufs and len(ufs) < len(UFS) else ["BR"]

    def collect(self) -> list[dict[str, Any]]:
        found = []
        for p in self.linhas():
            if ascii_fold(p.get("situacao_programa")) != "disponibilizado" or not self.recente(p.get("ano_programa")):
                continue
            for sufixo, canal in CANAIS_PARCERIAS:
                inicio = p.get(f"dt_inicio_beneficiario_{sufixo}")
                fim = p.get(f"dt_fim_beneficiario_{sufixo}")
                status = janela(inicio, fim, self.today)
                if not status:
                    continue
                nome = clean_text(p.get("nm_programa")) or "Programa de parceria"
                found.append(opportunity(
                    source_id=self.id, source_name=self.name, source_url=self.url, checked_at=self.checked_at,
                    external_id=f"{p['id_programa']}-{canal}",
                    title=nome, funder=p.get("nm_ente_superior") or "Governo Federal",
                    status=status, published=self.abertura(inicio, p.get("ano_programa")), deadline=str(fim)[:10],
                    organization_types=self.tipos(p), geography=self.geografia(p),
                    themes=temas_do_texto(nome, p.get("tp_instrumento")),
                    instrument_type="outros", repayable=False, program_budget=numero(p.get("nr_vlr_global")),
                    description=p.get("ds_objetivo"), channel=canal,
                ))
        return found
