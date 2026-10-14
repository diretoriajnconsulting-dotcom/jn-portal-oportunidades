"""PPSUS (Programa Pesquisa para o SUS: gestão compartilhada em saúde) nas 27 UFs.

O programa é um só (Decit/SECTICS/MS e CNPq, com a FAP e a SES de cada UF), mas
cada FAP publica o seu edital no seu site. Daí o desenho, saído do estudo de
14/10/2026 (`radar-fontes-saude/estudo/ppsus_*.md`):

1. **Sentinela no SISC&T** (`sisct.saude.gov.br/sisct/`). Toda chamada PPSUS
   recebe propostas no SISC&T, e a página pública lista o que está aberto agora,
   com a sigla da UF. É Wicket: sem cookie de sessão a página entra em laço de
   302. Por isso a leitura usa um `CookieJar` em HTTP simples, sem navegador.
   O SISC&T não traz prazo nem histórico, e o número no título pode divergir do
   edital (RJ: "10/2025" no SISC&T, 11/2025 no edital). Ele só **dispara** a
   confirmação na FAP; o item sai do edital.
2. **Tabela por UF** (`ppsus_ufs.py`) com leitores por plataforma (API de mídia do
   WordPress, RSS, HTML de listagem, sitemap). O leitor acha os documentos do
   PPSUS; a regra de número, prazo e situação é uma só, aqui.
3. **AP e MT** não têm leitura da FAP (robots). Entram pelo PDF do edital que o
   próprio SISC&T serve na sessão pública e, na carga histórica, por sementes.

Identidade: `external_id = "<uf>-<ano>-<número>"`, com o número do edital oficial
(o cabeçalho do PDF ou da página da FAP), nunca do título nem do SISC&T.

Situação:
- `open` só com prazo de submissão lido e ≥ hoje;
- `closed` com o prazo lido < hoje (a 8ª edição inteira), ou sem prazo quando a
  própria fonte já publicou resultado ou marcou "encerrado";
- `unknown` quando não dá para saber (por exemplo, a última retificação é
  PDF-imagem). Nada especulativo sobre a 9ª edição.

Orçamento: no dia a dia, até 70 pedidos (≈ 2 por UF: `robots.txt` e a listagem);
PDF só para candidato possivelmente aberto. Na carga histórica (`historical`),
até 200 pedidos, com os PDFs das edições desde 24 meses atrás.

Uma UF que falha (503, 403, timeout, orçamento) vai para `failed_scopes` e não
derruba as outras: o pipeline mantém os itens anteriores dela como defasados.
"""

from __future__ import annotations

import html as html_lib
import http.cookiejar
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from pypdf import PdfReader

from funding_intelligence.adapters.base import USER_AGENT, BaseAdapter
from funding_intelligence.adapters.ppsus_ufs import SIGLAS_UF, UFS, ConfigUF
from funding_intelligence.models import THEME_ALIASES, ascii_fold, clean_text, opportunity


SISCT_URL = "https://sisct.saude.gov.br/sisct/"

LIMITE_PEDIDOS_DIA = 70
LIMITE_PEDIDOS_HISTORICO = 200
INTERVALO_HOST_S = 1.1
RETENCAO_DIAS = 730

# Leitura por documento: no dia, o edital e as duas retificações mais novas (a
# mais nova pode mexer só no resultado, como a 2ª da FAPT); na carga histórica, até
# três. Sempre da mais nova para a mais velha, e para na primeira com a linha de
# submissão.
RETIFICACOES_DIA = 2
RETIFICACOES_HISTORICO = 3

ORGANIZACOES = ["pesquisador", "ict", "ict_privada", "universidade"]

# Leitores cuja página (ou item do RSS) é o próprio edital: dela saem o número e o
# prazo sem PDF. Nos demais (pasta da FAPT, página do Matriz), a página só aponta
# os documentos, e o menu lateral pode trazer número de outro edital.
PAGINA_E_EDITAL = {"rss", "faperj", "fapesp", "fapemig"}


# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------

class FalhaUF(RuntimeError):
    """A casa (UF) não respondeu como devia: HTTP de erro, timeout, robots."""


class Bloqueio(FalhaUF):
    """403, 429, CAPTCHA ou Cloudflare: para naquele host e registra."""


class OrcamentoEsgotado(FalhaUF):
    """O teto de pedidos da rodada acabou; as UFs que faltam ficam como falha."""


# --------------------------------------------------------------------------
# Texto: dobra, datas e números
# --------------------------------------------------------------------------

def dobrar(texto: str | None) -> str:
    """Minúsculas, sem acento e com espaço simples, para as expressões regulares.

    O NFKD de `ascii_fold` também troca "º" por "o" e "⁰" por "0"
    ("Edital N⁰ 02/2025" na FAPESQ), e as regras abaixo contam com isso.
    """
    return " ".join(ascii_fold(texto).split())


MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}

# Data numérica com o mesmo separador e sem espaço: o "5.6.7.2. 19/12/2025" da FAPERN
# não pode virar 07/02/2019. Com ponto, só ano de quatro dígitos: o item "3.1.12." do
# edital da FAPESC não é 03/01/2012.
_DATA = re.compile(
    r"(?<![\d./])(\d{1,2})([/.])(\d{1,2})\2(\d{4}|(?<=/\d/)\d{2}|(?<=/\d\d/)\d{2})(?![\d/.]?\d)"
    r"|(?<!\d)(\d{1,2})(?:o|°)?\s+de\s+(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-z]*\.?\s+de\s+(\d{4})"
)


def _iso(dia: int, mes: int, ano: int) -> str | None:
    if ano < 100:
        ano += 2000
    try:
        return date(ano, mes, dia).isoformat()
    except ValueError:
        return None


def datas(texto_dobrado: str) -> list[tuple[int, str]]:
    """Datas de um texto já dobrado, com a posição: "21/03/2025", "30 de mai. de 2025"."""
    achadas = []
    for m in _DATA.finditer(texto_dobrado):
        if m.group(1):
            iso = _iso(int(m.group(1)), int(m.group(3)), int(m.group(4)))
        else:
            iso = _iso(int(m.group(5)), MESES[m.group(6)], int(m.group(7)))
        if iso:
            achadas.append((m.start(), iso))
    return achadas


# Número no cabeçalho do edital: "CHAMADA PÚBLICA FAPEMA/.../SESMA nº 02/2025",
# "EDITAL FAPERR Nº 004/2025", "Chamada PPSUS- EDITAL Nº 1/2025/FAPERO-DC",
# "…/FAPESP Nº 023869/2024", "CHAMADA 05/2024 - Programa…", "CP 08/25",
# "…/ FAPT 01/2025-8º EDIÇÃO". Entre a palavra "chamada" e o número não pode haver
# outro ato ("RETIFICAÇÃO N. 002/2025" da FAPT, o convênio "n.º 968082/2024" do
# Transferegov): esse número não é o do edital.
_OUTRO_ATO = r"retifica|adendo|errata|aditivo|portaria|resolucao|\bato\b|decisao|oficio|processo|convenio|transferegov|\bsei\b"
# Ano com quatro dígitos aceita um hífen depois ("2025-8º"); com dois, não pode ser
# o meio de uma data ("17/02/2025" não é o número 17 de 2002).
_ANO = r"(\d{4}(?!\d)|\d{2}(?![\d/.-]?\d))"
_NUM_COM_N = re.compile(
    rf"(?:edital|chamada|chamamento|\bcp)\b(?:(?!{_OUTRO_ATO}).){{0,160}}?\bn(?:\s*[.o°]|0(?=\s))*\s*(?<!\d)(\d{{1,6}})\s*[/._-]\s*{_ANO}"
)
_NUM_SEM_N = re.compile(
    rf"(?:edital|chamada|chamamento|\bcp)\b(?:(?!{_OUTRO_ATO})[^\d]){{0,120}}?(?<!\d)(\d{{1,6}})\s*[/._-]\s*{_ANO}"
)
# Número em rótulo de documento ou nome de arquivo: "02-2025", "02.2025", "Nº 17/2024".
_NUM_ROTULO = re.compile(r"(?<!\d[/._-])(?<!\d)(\d{1,4})\s*[/._-]\s*(20\d{2})(?![\d/._-]?\d)")


@dataclass(frozen=True)
class Numero:
    numero: int
    ano: int
    texto: str  # como a fonte escreve: "02/2025", "004/2025", "1/2025"

    @property
    def chave(self) -> tuple[int, int]:
        return self.ano, self.numero


def _numero(m: re.Match[str] | None, hoje: date) -> Numero | None:
    if not m:
        return None
    bruto, ano_txt = m.group(1), m.group(2)
    ano = int(ano_txt) + (2000 if len(ano_txt) == 2 else 0)
    if not 2000 <= ano <= hoje.year + 1 or int(bruto) == 0:
        return None
    return Numero(int(bruto), ano, f"{bruto}/{ano}")


def numero_do_cabecalho(texto: str, hoje: date) -> Numero | None:
    """O número do edital oficial, no cabeçalho (primeiros 2.500 caracteres)."""
    cabeca = dobrar(texto)[:2500]
    achados = [m for m in (_NUM_COM_N.search(cabeca), _NUM_SEM_N.search(cabeca)) if m]
    if not achados:
        return None
    return _numero(min(achados, key=lambda m: m.start(1)), hoje)


_ATO = re.compile(r"\b(?:ato(?:\s+defa)?|portaria|resolucao|decisao|oficio|aditivo|sei|processo|convenio|do)\b")
_CHAMADA = re.compile(r"\b(?:edital|chamada|chamamento|cp)\b")


def numero_do_rotulo(rotulo: str, hoje: date) -> Numero | None:
    """Número do edital no rótulo do documento. "Ato DEFA 115/2025: Prorrogação…" é o
    número do ato, não do edital: vale o número mais perto de "edital/chamada/CP"."""
    dobrado = dobrar(rotulo)
    for m in _NUM_ROTULO.finditer(dobrado):
        antes = dobrado[max(0, m.start() - 40):m.start()]
        ato = [a.end() for a in _ATO.finditer(antes)]
        chamada = [c.end() for c in _CHAMADA.finditer(antes)]
        if ato and (not chamada or ato[-1] > chamada[-1]):
            continue
        numero = _numero(m, hoje)
        if numero:
            return numero
    # Ano com dois dígitos só depois de "edital/chamada/CP": "CP 08/25", "FAPT 01/25".
    return _numero(re.search(
        r"(?:edital|chamada|chamamento|\bcp)\b[^\d]{0,40}?(?<!\d)(\d{1,3})\s*[/._-]\s*(\d{2})(?![\d/])", dobrado,
    ), hoje)


# --------------------------------------------------------------------------
# Classificação de documentos
# --------------------------------------------------------------------------

# Nada disso é edital do PPSUS estadual. A chamada nacional CNPq 10/2025 (PPSUS
# Inovação) é do adaptador do CNPq; bolsas de IC para projetos já aprovados
# (FAPITEC 02/2025) não são a chamada de pesquisa.
_EXCLUIR = re.compile(
    r"ppsus\s*-?\s*inovacao|inovacao\s*-?\s*ppsus|ppsus inova|cnpq\s*n?o?\s*10\s*/?\s*2025|chamada\s+cnpq"
    r"|concessao\s+de\s+bolsas|bolsas?\s+de\s+iniciacao|seminario|marco\s+zero|oficina|workshop|treinamento"
    r"|tutorial|manual|diretriz|apresentacao|\blogo|video|modelo\s+de|formulario|\banexos?\b|declaracao"
    r"|termo\s+de\s+(?:compromisso|outorga|anuencia|aceitacao)|convenio|planilha|prestacao\s+de\s+contas"
    r"|relatorio|e-?book|duvidas|linhas\s+de\s+pesquisa|eixos\s+tematicos"
)
_OUTRO = re.compile(
    r"resultado|homolog|classifica|convoca|portaria|comunicado|nota\s+tecnica|enquadra|selecionad|aprovad"
    r"|contemplad|excedente|recurso|parecer|julgamento|elegive|esclarec|extrato|\bdo\s?\d{4,}|diario\s+oficial"
    r"|\baviso\b|propostas\s+aptas|inclusao\s+de|pre\s*-?\s*selecao|admissibilidade"
)
_RETIFICACAO = re.compile(r"retifica|errata|altera|prorroga|cronograma|adendo|aditivo")
_EDITAL = re.compile(r"\b(?:edital|chamada|chamamento|cp|minuta)\b")
_NAO_PDF = re.compile(r"\.(?:docx?|xlsx?|pptx?|odt|zip|rar|jpe?g|png|webp|gif|mp4)$")
_PPSUS = re.compile(r"ppsus|pesquisa\s+para\s+o\s+(?:sus|sistema\s+unico)")


def classificar(rotulo: str) -> str:
    """`edital`, `retificacao`, `outro` (resultado, extrato…) ou `excluir`."""
    dobrado = dobrar(re.sub(r"[_]+|(?<=\w)[-.](?=\w)", " ", rotulo))
    if _NAO_PDF.search(dobrado.strip()) or _EXCLUIR.search(dobrado):
        return "excluir"
    if _OUTRO.search(dobrado):
        return "outro"
    if _RETIFICACAO.search(dobrado):
        return "retificacao"
    if _EDITAL.search(dobrado):
        return "edital"
    return "outro"


def e_ppsus(texto: str) -> bool:
    return bool(_PPSUS.search(dobrar(texto)))


# --------------------------------------------------------------------------
# Prazo de submissão, valor e elegibilidade
# --------------------------------------------------------------------------

# "Submissão eletrônica das propostas", "Prazo limite para submissão", e também o
# texto colado do PDF da Fundação Araucária ("Submissão eletrônica daspropostas").
_GATILHO = re.compile(
    r"(?:submissao|submissoes|inscricao|inscricoes|envio|recebimento|cadastramento)\s+"
    r"(?:\w+\s+){0,2}?(?:d[aeo]s?\s*|de\s+)?(?:propostas?|projetos?)"
    r"|(?:periodo|prazo|data(?:\s+final|\s+limite)?|limite)\s+(?:final\s+)?(?:para|de)\s+(?:a\s+|as\s+)?(?:submiss|inscric)\w*"
    r"|\binscricoes\s*:"
)
# Onde a linha do cronograma acaba: a próxima atividade.
_PARADA = re.compile(
    r"\b(?:resultado|divulgacao|interposicao|recursos?|homologacao|assinatura|contratacao|enquadramento|analise"
    r"|julgamento|selecao|pre-?selecao|avaliacao|convocacao|realizacao|impugnacao"
    r"|previsao|seminario|outorga|liberacao|anuncio|governo do estado"
    r"|prazo\s+(?:para|de|final)|data\s+(?:final|limite)\s+para)\b"
)
# Entre duas datas da mesma linha: "De 17/02/2025 até 21/03/2025", "30/04/2025 até às
# 23h59 do dia 27/06/2025", "19/12/2025 a 27/02/2026", e datas riscadas em sequência.
_ENTRE_DATAS = re.compile(r"\s*(?:-|–|a|ate|e)?\s*(?:as\s+\d{1,2}h\d{0,2}(?:min)?\s*,?\s*)?(?:\(?horario[^)]{0,30}\)?\s*,?\s*)?(?:do\s+dia\s+)?$")


def _data_da_linha(janela: str) -> str | None:
    """A data final da linha de submissão: a primeira data, ou o fim do intervalo que
    ela abre. "Até 16/06/2025 4.3 Conferência… até 23/06/2025" fica em 16/06."""
    achadas = datas(janela)
    if not achadas:
        return None
    escolhida = achadas[0]
    for anterior, seguinte in zip(achadas, achadas[1:]):
        if anterior is not escolhida:
            break
        entre = janela[anterior[0]:seguinte[0]]
        entre = _DATA.sub("", entre, count=1)
        if len(entre) <= 40 and _ENTRE_DATAS.match(entre):
            escolhida = seguinte
    return escolhida[1]
_EXCLUI_ANTES = re.compile(r"comprovante|parecer|\bcep\b|etica")
_EXCLUI_JANELA = re.compile(r"aprovad|2a\s+fase|segunda\s+fase|2a\s+etapa")
_LEIA_SE = re.compile(r"leia-?\s?se")
_ONDE_SE_LE = re.compile(r"onde\s+se\s+le")
# Quadro do cronograma da chamada ("4. CRONOGRAMA Atividades Data"). "Cronograma de
# atividades aprovado" e "cronograma do projeto" (FAPESC) são do projeto, não da chamada.
_CRONOGRAMA = re.compile(
    r"cronograma(?!\s*\))(?!\s+d[aeo]s?\b)\W{0,8}(?:\w+\W+){0,3}?(?:atividades?|etapas?|eventos?|datas?|fases?|acoes|descricao)\b"
)
_PRORROGA = re.compile(
    r"prorroga\w*[^.;]{0,160}?(?:submiss|inscric)\w*[^.;]{0,100}?\b(?:para|ate)\s+(?:o\s+dia\s+)?(?:as\s+\d+h\d*\s+(?:do\s+dia\s+)?)?"
)


def prazo_submissao(texto: str) -> str | None:
    """O prazo final de submissão, lido da linha do cronograma.

    Regras (estudo, seção 5.3):
    - só a linha de submissão/inscrição/envio de propostas, nunca "a maior data";
    - na retificação, vale o trecho depois de "leia-se" (o "onde se lê" é o antigo);
    - havendo quadro de cronograma, vale a primeira linha de submissão depois dele;
    - "submissão das propostas aprovadas" (2ª fase, FAPESC) não é prazo da chamada;
    - "comprovante de submissão ao CEP" também não.
    """
    dobrado = dobrar(texto)
    candidatos: list[tuple[int, str]] = []
    for m in _GATILHO.finditer(dobrado):
        if _EXCLUI_ANTES.search(dobrado[max(0, m.start() - 40):m.start()]):
            continue
        # 260 caracteres: a FAPEAM põe os dois endereços de submissão antes da data.
        janela = dobrado[m.end():m.end() + 260]
        parada = _PARADA.search(janela)
        if parada:
            janela = janela[:parada.start()]
        if _EXCLUI_JANELA.search(janela[:60]):
            continue
        if m.group(0).startswith(("envio", "recebimento")) and re.search(r"document", janela[:50]):
            continue  # "envio da proposta e documentos complementares" é outra linha (FAPESB)
        data = _data_da_linha(janela)
        if data:
            candidatos.append((m.start(), data))
    for m in _PRORROGA.finditer(dobrado):
        achadas = datas(dobrado[m.end():m.end() + 40])
        if achadas and achadas[0][0] < 5:
            candidatos.append((m.start(), achadas[0][1]))
    if not candidatos:
        return None
    candidatos.sort()

    leia = [m.end() for m in _LEIA_SE.finditer(dobrado)]
    if leia:
        onde = [m.start() for m in _ONDE_SE_LE.finditer(dobrado)]
        regioes = []
        for inicio in leia:
            proximo = min([o for o in onde if o > inicio], default=len(dobrado))
            regioes.append((inicio, proximo))
        # A última região "leia-se" que tem linha de submissão; dentro dela, a primeira.
        for a, b in reversed(regioes):
            dentro = [c for c in candidatos if a <= c[0] < b]
            if dentro:
                return dentro[0][1]
    # O primeiro quadro de cronograma que tem a linha de submissão logo abaixo.
    for quadro in (m.start() for m in _CRONOGRAMA.finditer(dobrado)):
        depois = [c for c in candidatos if quadro < c[0] <= quadro + 1500]
        if depois:
            return depois[0][1]
    return candidatos[0][1]


def data_publicacao(texto: str) -> str | None:
    dobrado = dobrar(texto)
    m = re.search(
        r"(?:lancamento|publicacao|anuncio)\s+d[aoe]\s+(?:chamada|edital)[^0-9]{0,140}?(?=\d)|publicad[ao]\s+em\s+(?=\d)", dobrado,
    )
    if not m:
        return None
    achadas = datas(dobrado[m.end():m.end() + 30])
    return achadas[0][1] if achadas and achadas[0][0] < 3 else None


def _reais(numero: str, escala: str | None) -> float | None:
    bruto = numero.strip(".,")
    if "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    elif bruto.count(".") > 1 or (escala is None and re.fullmatch(r"\d{1,3}(?:\.\d{3})+", bruto)):
        bruto = bruto.replace(".", "")
    try:
        valor = float(bruto)
    except ValueError:
        return None
    if escala and escala.startswith("milh"):
        valor *= 1_000_000
    elif escala == "mil":
        valor *= 1_000
    return valor if valor >= 10_000 else None


def valor_global(texto: str) -> float | None:
    """'O valor global de R$ 1.800.000,00…', 'Valor da chamada: R$ 11,8 milhões' (FAPEMIG)
    ou 'O total de recursos financeiros destinados a esta Chamada é de R$…' (FAPEAM).

    Nunca o "valor total por faixa" do quadro de faixas, que é só uma parte.
    """
    dobrado = dobrar(texto)
    for padrao in (
        r"valor\s+global\b[^$]{0,60}?r\$\s*([\d.,]+)\s*(milhoes|milhao|mil)?\b",
        r"valor\s+da\s+chamada\s*:?\s*r\$\s*([\d.,]+)\s*(milhoes|milhao|mil)?\b",
        r"total\s+de\s+recursos\b[^$]{0,80}?r\$\s*([\d.,]+)\s*(milhoes|milhao|mil)?\b",
    ):
        m = re.search(padrao, dobrado)
        if m:
            valor = _reais(m.group(1), m.group(2))
            if valor:
                return valor
    return None


def aceita_servico_saude(texto: str) -> bool:
    """Executora "(e) serviços de saúde" na lista do edital (FAPESB 03/2025)."""
    return bool(re.search(r"\(?\b[a-h]\)\s*(?:os\s+)?servicos?\s+de\s+saude", dobrar(texto)))


def preve_empresa_parceira(texto: str) -> bool:
    return bool(re.search(
        r"participacao\s+de\s+empresa|empresas?\s+(?:privadas?\s+)?parceiras?|termo\s+de\s+compromisso\s+d[ae]\s+empresa"
        r"|participacao\s+do\s+setor\s+privado",
        dobrar(texto),
    ))


def edicao(texto: str) -> str | None:
    m = re.search(r"\b(\d{1,2})\s*(?:a|o)?\s+edicao\b", dobrar(texto)[:3000])
    return f"{int(m.group(1))}ª" if m and 1 <= int(m.group(1)) <= 20 else None


_TEMAS_ESPECIFICOS = {"terapias_avancadas", "pesquisa_clinica", "biotecnologia"}


def temas(texto: str) -> list[str]:
    dobrado = dobrar(texto)
    extras = [canonical for pattern, canonical in THEME_ALIASES
              if canonical in _TEMAS_ESPECIFICOS and re.search(pattern, dobrado)]
    return list(dict.fromkeys(["saude", "pesquisa_sus", *extras]))


def _nao_e_edital(texto: str) -> bool:
    """O documento lido não é a chamada: chamada de excedentes, resultado, convocação."""
    cabeca = dobrar(texto)[:700]
    return bool(re.search(r"chamada\s+de\s+excedentes|\bresultado\s+(?:final|preliminar|parcial)|\bconvocacao\b", cabeca))


def _e_inovacao_nacional(texto: str) -> bool:
    cabeca = dobrar(texto)[:1500]
    return bool(re.search(r"ppsus\s*-?\s*inovacao|chamada\s+cnpq\b.{0,60}10/2025", cabeca))


# --------------------------------------------------------------------------
# Estruturas
# --------------------------------------------------------------------------

@dataclass
class Documento:
    titulo: str
    url: str
    data: str | None = None
    seq: int = 0  # ordem de recência quando a fonte não dá data (maior = mais novo)
    # Data tirada da pasta do upload ("/uploads/2025/09/"): serve para ordenar, não
    # para dizer quando a chamada saiu (a FAPPR reenvia o edital meses depois).
    aproximada: bool = False
    tipo: str = ""
    numero: Numero | None = None

    def chave_tempo(self) -> tuple[str, int]:
        return self.data or "", self.seq


@dataclass
class Candidato:
    """Uma chamada PPSUS achada na listagem da FAP, ainda sem confirmação."""

    uf: str
    rotulo: str
    url: str
    data: str | None = None
    numero: Numero | None = None
    documentos: list[Documento] = field(default_factory=list)
    pagina: str | None = None   # página de detalhe a ler (documentos e texto)
    texto: str | None = None    # texto que já traz o cronograma (FAPERJ, RSS da FAPEAM)
    aberto: bool | None = None  # a própria listagem diz "aberto"/"encerrado"
    origem: str = "fap"         # "fap", "sisct" ou "semente"
    ano_listagem: int | None = None  # "Edital 2025" na FAPESP: só para decidir o que ler
    data_aproximada: bool = False    # `data` veio da pasta do upload (serve para decidir, não para publicar)

    @property
    def ano(self) -> int | None:
        if self.numero:
            return self.numero.ano
        if self.data:
            return int(self.data[:4])
        return self.ano_listagem


@dataclass
class Leitura:
    numero: Numero
    prazo: str | None
    publicado: str | None
    texto: str
    documentos: list[Documento]
    url: str
    ilegivel: bool = False
    tem_resultado: bool = False
    encerrado_na_fonte: bool = False
    avisos: list[str] = field(default_factory=list)


def _documento(titulo: str, url: str, *, data: str | None = None, seq: int = 0, aproximada: bool = False,
               rotulo: str | None = None, hoje: date) -> Documento:
    rotulo = rotulo or titulo
    return Documento(
        titulo=clean_text(titulo) or "Documento do edital", url=url, data=data, seq=seq, aproximada=aproximada,
        tipo=classificar(rotulo), numero=numero_do_rotulo(rotulo, hoje),
    )


def agrupar(uf: str, documentos: list[Documento]) -> list[Candidato]:
    """Documentos soltos (mídia do WordPress, busca do Plone) → uma chamada por edição.

    - número explícito no rótulo junta documentos ("02-2025", "Nº 02/2025");
    - edital sem número entra na edição numerada que começou até 300 dias antes,
      ou abre uma edição própria (o número sai depois, do PDF);
    - retificação e resultado sem número vão para a edição mais recente que já
      tinha começado na data deles;
    - edição só com resultados, sem edital nem retificação, não vira chamada.
    """
    docs = [d for d in documentos if d.tipo != "excluir"]
    principais = sorted([d for d in docs if d.tipo == "edital"], key=Documento.chave_tempo)
    if not principais:
        principais = sorted([d for d in docs if d.tipo == "retificacao"], key=Documento.chave_tempo)

    grupos: list[dict[str, Any]] = []

    def grupo_do_numero(numero: Numero | None) -> dict[str, Any] | None:
        if not numero:
            return None
        return next((g for g in grupos if g["numero"] and g["numero"].chave == numero.chave), None)

    for doc in principais:
        grupo = grupo_do_numero(doc.numero)
        if grupo is None and doc.numero is None and grupos:
            anteriores = [g for g in grupos if _dentro(g["inicio"], doc, 300)]
            grupo = anteriores[-1] if anteriores else None
        if grupo is None and doc.numero is not None:
            sem_numero = [g for g in grupos if g["numero"] is None and _dentro(g["inicio"], doc, 300)]
            grupo = sem_numero[-1] if sem_numero else None
            if grupo is not None:
                grupo["numero"] = doc.numero
        if grupo is None:
            grupo = {"numero": doc.numero, "inicio": doc, "docs": []}
            grupos.append(grupo)
        grupo["docs"].append(doc)

    ja_agrupados = {id(d) for g in grupos for d in g["docs"]}
    for doc in docs:
        if id(doc) in ja_agrupados:
            continue
        grupo = grupo_do_numero(doc.numero)
        if grupo is None and grupos:
            if doc.data:
                anteriores = [g for g in grupos if (g["inicio"].data or "") <= doc.data]
                grupo = anteriores[-1] if anteriores else grupos[0]
            elif doc.numero is None:
                grupo = grupos[-1]
        if grupo is not None:
            grupo["docs"].append(doc)

    candidatos = []
    for grupo in grupos:
        inicio: Documento = grupo["inicio"]
        candidatos.append(Candidato(
            uf=uf, rotulo=inicio.titulo, url=inicio.url, data=inicio.data,
            numero=grupo["numero"], documentos=sorted(grupo["docs"], key=Documento.chave_tempo),
        ))
    return candidatos


def _dentro(inicio: Documento, doc: Documento, dias: int) -> bool:
    if not inicio.data or not doc.data:
        return True
    delta = (date.fromisoformat(doc.data) - date.fromisoformat(inicio.data)).days
    return -60 <= delta <= dias


# --------------------------------------------------------------------------
# Adaptador
# --------------------------------------------------------------------------

class _ContaRedirecionamento(urllib.request.HTTPRedirectHandler):
    def __init__(self, contar: Callable[[str], None]):
        self.contar = contar

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - API do urllib
        self.contar(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _SessaoSisct:
    """Sessão com cookie no SISC&T, contando cada redirecionamento no orçamento."""

    def __init__(self, adaptador: "PpsusAdapter"):
        self.adaptador = adaptador
        self.host = urlsplit(SISCT_URL).netloc
        if not adaptador.fixture:
            adaptador._robots_permite(SISCT_URL)
            self.opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                _ContaRedirecionamento(adaptador._conta),
            )
            self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def abrir(self, url: str) -> tuple[bytes, str]:
        adaptador = self.adaptador
        if adaptador.fixture:
            return adaptador._baixar(url), url
        adaptador._conta(url)
        adaptador._pausa(self.host)
        try:
            with self.opener.open(adaptador._ascii(url), timeout=ESPERA_PEDIDO_S) as resposta:
                return resposta.read(), resposta.geturl()
        except urllib.error.HTTPError as erro:
            if erro.code in (403, 429):
                adaptador._bloqueados.add(self.host)
                raise Bloqueio(f"HTTP {erro.code} no SISC&T") from erro
            raise FalhaUF(f"HTTP {erro.code} no SISC&T") from erro
        except (urllib.error.URLError, TimeoutError, ConnectionError) as erro:
            raise FalhaUF(f"SISC&T: {type(erro).__name__}: {erro}") from erro
        finally:
            adaptador._marca(self.host)


# Prazo total da coleta e espera máxima por pedido (14/10/2026). Na 1ª carga histórica no GitHub, o
# job estourou os 15 min sem terminar: hosts que respondem devagar vistos de fora do país, com duas
# tentativas de 45 s cada. Esgotado o prazo, as UFs que faltam vão para `failed_scopes` (os itens
# anteriores delas ficam) e o que foi lido é publicado.
PRAZO_DIA_S = 300
PRAZO_HISTORICO_S = 600
ESPERA_PEDIDO_S = 20


class PpsusAdapter(BaseAdapter):
    id = "ppsus"
    name = "PPSUS (Decit/MS + FAPs)"
    url = SISCT_URL

    def __init__(self, *, fixture: str | None = None, today: date | None = None,
                 ufs: list[str] | None = None, historical: bool | None = None):
        super().__init__(fixture=fixture, today=today)
        if historical is not None:
            self.historical = historical
        self.ufs = [uf for uf in (ufs or list(UFS)) if uf in UFS]
        self.pedidos = 0
        self.limite = LIMITE_PEDIDOS_HISTORICO if self.historical else LIMITE_PEDIDOS_DIA
        self.prazo_s = PRAZO_HISTORICO_S if self.historical else PRAZO_DIA_S
        self._relogio = time.monotonic
        self._inicio = self._relogio()
        self.failed_scopes: set[str] = set()
        self.avisos: list[str] = []
        self.pedidos_por_uf: dict[str, int] = {}
        self.sisct_ok = False
        self.sisct_ufs: dict[str, list[tuple[str, str]]] = {}
        self._uf_atual = "BR"
        self._ultimo_host: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._bloqueados: set[str] = set()
        self._mapa_fixture: dict[str, Any] | None = None
        self._textos_sisct: dict[str, str] = {}
        # A edição mais antiga que ainda cabe nos 24 meses, e a folga para editais
        # publicados antes do piso que encerraram depois dele.
        self.piso = self.today - timedelta(days=RETENCAO_DIAS)
        self.piso_listagem = (self.piso - timedelta(days=180)).isoformat()

    # ------------------------------------------------------------------ rede

    @property
    def mapa(self) -> dict[str, Any]:
        """Com `fixture` (um JSON de cenário), cada URL aponta para um arquivo ao lado."""
        if self._mapa_fixture is None:
            self._mapa_fixture = json.loads(Path(self.fixture).read_text(encoding="utf-8")) if self.fixture else {}
        return self._mapa_fixture

    def _conta(self, url: str) -> None:
        if self.pedidos >= self.limite:
            raise OrcamentoEsgotado(f"orçamento de {self.limite} pedidos esgotado antes de {url}")
        if self._relogio() - self._inicio > self.prazo_s:
            raise OrcamentoEsgotado(f"prazo de {self.prazo_s} s da coleta esgotado antes de {url}")
        self.pedidos += 1
        self.pedidos_por_uf[self._uf_atual] = self.pedidos_por_uf.get(self._uf_atual, 0) + 1

    def _pausa(self, host: str) -> None:
        if self.fixture:
            return
        espera = INTERVALO_HOST_S - (time.monotonic() - self._ultimo_host.get(host, -10.0))
        if espera > 0:
            time.sleep(espera)

    def _marca(self, host: str) -> None:
        self._ultimo_host[host] = time.monotonic()

    def request_bytes(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
                      data: bytes | None = None) -> bytes:
        """O GET da base, com a espera máxima do PPSUS no lugar dos 45 s."""
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **(headers or {})}, data=data, method=method,
        )
        with urllib.request.urlopen(request, timeout=ESPERA_PEDIDO_S) as response:
            return response.read()

    @staticmethod
    def _ascii(url: str) -> str:
        return quote(url, safe=":/?&=%#+;,@!$'()*~[]")

    def _do_fixture(self, url: str) -> bytes:
        entrada = self.mapa.get(url)
        if entrada is None:
            raise FalhaUF(f"sem fixture para {url}")
        status = entrada.get("status")
        if status in (403, 429):
            raise Bloqueio(f"HTTP {status} em {url}")
        if status and status != 200:
            raise FalhaUF(f"HTTP {status} em {url}")
        if "texto" in entrada:
            return entrada["texto"].encode("utf-8")
        if "json" in entrada:
            return json.dumps(entrada["json"], ensure_ascii=False).encode("utf-8")
        return (Path(self.fixture).parent / entrada["arquivo"]).read_bytes()

    def _robots_permite(self, url: str) -> None:
        partes = urlsplit(url)
        host = partes.netloc.lower()
        if host not in self._robots:
            robots_url = f"{partes.scheme}://{partes.netloc}/robots.txt"
            if self.fixture and robots_url not in self.mapa:
                self._robots[host] = None
            else:
                self._robots[host] = self._ler_robots(robots_url, host)
        regras = self._robots[host]
        if regras is not None and not regras.can_fetch(USER_AGENT, url):
            raise FalhaUF(f"robots.txt de {host} não permite {partes.path}")

    def _ler_robots(self, robots_url: str, host: str) -> RobotFileParser:
        regras = RobotFileParser(robots_url)
        try:
            corpo = self._baixar(robots_url, checar_robots=False)
        except Bloqueio:
            raise
        except FalhaUF as erro:
            if "HTTP 404" in str(erro) or "HTTP 410" in str(erro):
                regras.parse([])  # sem robots.txt: tudo liberado
                return regras
            raise FalhaUF(f"robots.txt de {host} indisponível: {erro}") from erro
        regras.parse(corpo.decode("utf-8", errors="replace").splitlines())
        return regras

    def _baixar(self, url: str, *, checar_robots: bool = True) -> bytes:
        """GET com User-Agent da PONTE, robots, 1 pedido/s por host e orçamento."""
        host = urlsplit(url).netloc.lower()
        if host in self._bloqueados:
            raise Bloqueio(f"{host} bloqueou um pedido anterior nesta rodada")
        if checar_robots:
            self._robots_permite(url)
        self._conta(url)
        if self.fixture:
            corpo = self._do_fixture(url)
        else:
            corpo = self._baixar_rede(url, host)
        if re.search(rb"Just a moment|cf-chl|g-recaptcha|hcaptcha|Attention Required", corpo[:4000]):
            self._bloqueados.add(host)
            raise Bloqueio(f"desafio anti-robô em {url}")
        return corpo

    def _baixar_rede(self, url: str, host: str) -> bytes:
        for tentativa in (1, 2):
            self._pausa(host)
            try:
                corpo = self.request_bytes(self._ascii(url))
                self._marca(host)
                return corpo
            except urllib.error.HTTPError as erro:
                self._marca(host)
                if erro.code in (403, 429):
                    self._bloqueados.add(host)
                    raise Bloqueio(f"HTTP {erro.code} em {url}") from erro
                raise FalhaUF(f"HTTP {erro.code} em {url}") from erro
            except (urllib.error.URLError, TimeoutError, ConnectionError) as erro:
                self._marca(host)
                if tentativa == 2:
                    raise FalhaUF(f"{type(erro).__name__} em {url}: {erro}") from erro
                # Uma nova tentativa só para timeout e conexão (hosts lentos: FAPESPA,
                # FAPEAL, RSS da FAPEAM). Conta no orçamento.
                self._conta(url)
        raise AssertionError("inalcançável")

    def _html(self, url: str) -> str:
        return self._baixar(url).decode("utf-8", errors="replace")

    def _json(self, url: str) -> Any:
        return json.loads(self._baixar(url).decode("utf-8-sig", errors="replace"))

    def _texto_pdf(self, url: str) -> str:
        """Texto do PDF em memória. Na fixture, o PDF é um .txt com o trecho extraído."""
        if self.fixture:
            entrada = self.mapa.get(url) or {}
            if str(entrada.get("arquivo", "")).endswith(".txt") or "texto" in entrada:
                return self._baixar(url).decode("utf-8")
        corpo = self._baixar(url)
        if corpo[:5] != b"%PDF-":
            # Página HTML (o documento do Plone, por exemplo): vale o texto dela.
            return clean_text(self.soup(corpo.decode("utf-8", errors="replace")).get_text(" ", strip=True))
        try:
            leitor = PdfReader(io.BytesIO(corpo))
            return "\n".join(pagina.extract_text() or "" for pagina in leitor.pages)
        except Exception as erro:  # pypdf levanta tipos variados para PDF quebrado
            raise FalhaUF(f"PDF ilegível em {url}: {type(erro).__name__}") from erro

    # --------------------------------------------------------------- SISC&T

    def _ler_sisct(self) -> None:
        """Lista o que está aberto no SISC&T e guarda o texto dos PDFs de AP e MT.

        A página pública é Wicket: sem cookie de sessão, 302 em laço. A sessão é um
        `CookieJar` comum (HTTP simples, verificação de TLS ligada); o link do PDF
        (`login.html?0-IResourceListener-…-linkEdital`) só vale nessa sessão, então o
        PDF de AP e MT é baixado logo em seguida, na mesma sessão.
        """
        self._uf_atual = "SISCT"
        try:
            sessao = _SessaoSisct(self)
            pagina, final = sessao.abrir(SISCT_URL)
            self.sisct_ufs = self.editais_sisct(pagina.decode("utf-8", errors="replace"), final)
            self.sisct_ok = True
            for uf, editais in self.sisct_ufs.items():
                if uf not in self.ufs or UFS[uf].leitor != "sisct":
                    continue
                for _titulo, link in editais:
                    try:
                        corpo, _ = sessao.abrir(link)
                        self._textos_sisct[link] = self._texto_de_bytes(corpo)
                    except (FalhaUF, ValueError) as erro:
                        if isinstance(erro, OrcamentoEsgotado):
                            raise
                        self.avisos.append(f"SISC&T {uf}: PDF do edital não foi lido ({erro})")
        except FalhaUF as erro:
            self.sisct_ok = False
            self.avisos.append(f"SISC&T indisponível: {erro}")

    @staticmethod
    def _texto_de_bytes(corpo: bytes) -> str:
        if corpo[:5] != b"%PDF-":
            return corpo.decode("utf-8", errors="replace")
        try:
            leitor = PdfReader(io.BytesIO(corpo))
            return "\n".join(pagina.extract_text() or "" for pagina in leitor.pages)
        except Exception as erro:  # pypdf levanta tipos variados para PDF quebrado
            raise ValueError(f"PDF ilegível: {type(erro).__name__}") from erro

    @staticmethod
    def editais_sisct(pagina: str, base: str) -> dict[str, list[tuple[str, str]]]:
        """'Editais - Contratação': itens "UF - TÍTULO.pdf". Só a UF e o link (efêmero).

        O SISC&T também recebe chamadas do Decit que não são PPSUS estadual: entram só
        as que têm a sigla de uma UF na frente e o PPSUS (ou Decit/SES) no título. O
        número do título não serve de identidade (RJ 10/2025 × 11/2025).
        """
        soup = BeautifulSoup(pagina, "html.parser")
        achados: dict[str, list[tuple[str, str]]] = {}
        vistos: set[str] = set()
        for link in soup.select(".editaisProgramas a[href], #sisct a[href*=linkEdital]"):
            href = urljoin(base, link["href"])
            if href in vistos:
                continue
            vistos.add(href)
            titulo = clean_text(link.get_text(" ", strip=True))
            m = re.match(r"([A-Z]{2})\s*-\s*(.+)", titulo)
            if not m or m.group(1) not in SIGLAS_UF:
                continue
            dobrado = dobrar(m.group(2))
            if re.search(r"ppsus\s*-?\s*inovacao", dobrado):
                continue
            if not (_PPSUS.search(dobrado) or re.search(r"decit|sectics|sctie|gestao compartilhada", dobrado)):
                continue
            achados.setdefault(m.group(1), []).append((titulo, href))
        return achados

    # -------------------------------------------------------------- leitores

    def _leitores(self) -> dict[str, Callable[[ConfigUF], list[Candidato]]]:
        return {
            "wp_media": self._ler_wp_media, "rss": self._ler_rss, "facepe": self._ler_facepe,
            "fapitec": self._ler_fapitec, "montenegro": self._ler_montenegro, "fapt": self._ler_fapt,
            "fapdf": self._ler_fapdf, "fapes": self._ler_fapes,
            "fapemig": self._ler_fapemig, "fapesp": self._ler_fapesp, "faperj": self._ler_faperj,
            "fappr": self._ler_fappr, "matriz": self._ler_matriz, "links": self._ler_links,
            "sisct": self._ler_sisct_uf,
        }

    def _doc(self, titulo: str, url: str, **kwargs: Any) -> Documento:
        return _documento(titulo, url, hoje=self.today, **kwargs)

    def _ler_wp_media(self, cfg: ConfigUF) -> list[Candidato]:
        """API de mídia do WordPress: os PDFs com "ppsus" no título, com a data da API.

        A data vem da API, nunca do caminho do arquivo (a FAPESB põe tudo em 2019/08).
        """
        base = (cfg.url or "").rstrip("/")
        por_pagina = 100 if self.historical else 20
        url = (f"{base}/wp-json/wp/v2/media?search=ppsus&per_page={por_pagina}"
               f"&_fields=id,date,title,source_url,mime_type")
        if self.historical:
            url += f"&after={self.piso_listagem}T00:00:00"
        linhas = self._json(url)
        if not isinstance(linhas, list):
            raise FalhaUF(f"{cfg.uf}: resposta inesperada da API de mídia")
        documentos = []
        for linha in linhas:
            fonte = linha.get("source_url") or ""
            tipo = linha.get("mime_type") or ""
            if "pdf" not in tipo and not fonte.lower().split("?")[0].endswith(".pdf"):
                continue
            titulo = html_lib.unescape((linha.get("title") or {}).get("rendered") or "")
            arquivo = unquote(fonte.rsplit("/", 1)[-1])
            documentos.append(self._doc(titulo or arquivo, fonte, data=(linha.get("date") or "")[:10] or None,
                                        rotulo=f"{titulo} {arquivo}"))
        return agrupar(cfg.uf, documentos)

    def _ler_rss(self, cfg: ConfigUF) -> list[Candidato]:
        """RSS de editais (FAPEAM): o item traz o texto integral do edital."""
        candidatos = []
        paginas = 9 if self.historical else 1
        for pagina in range(1, paginas + 1):
            url = cfg.url if pagina == 1 else f"{cfg.url}?paged={pagina}"
            try:
                raiz = ET.fromstring(self._baixar(url))
            except ET.ParseError as erro:
                raise FalhaUF(f"{cfg.uf}: RSS inválido ({erro})") from erro
            itens = list(raiz.iter("item"))
            mais_velho = None
            for item in itens:
                titulo = clean_text(item.findtext("title"))
                link = clean_text(item.findtext("link"))
                try:
                    publicado = parsedate_to_datetime(item.findtext("pubDate") or "").date().isoformat()
                except (TypeError, ValueError):
                    publicado = None
                mais_velho = min(filter(None, [mais_velho, publicado]), default=None)
                conteudo = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
                if not e_ppsus(titulo) or classificar(titulo) not in ("edital", "retificacao"):
                    continue
                soup = self.soup(conteudo)
                documentos = [self._doc(titulo, link, data=publicado)]
                for a in soup.select("a[href]"):
                    href = urljoin(link, a["href"])
                    if href.lower().split("?")[0].endswith(".pdf"):
                        documentos.append(self._doc(a.get_text(" ", strip=True) or titulo, href, data=publicado,
                                                    rotulo=f"{a.get_text(' ', strip=True)} {href.rsplit('/', 1)[-1]}"))
                candidatos.append(Candidato(
                    uf=cfg.uf, rotulo=titulo, url=link, data=publicado,
                    numero=numero_do_rotulo(titulo, self.today), documentos=documentos,
                    # O número está no título do post, não no começo do texto (que
                    # cita antes o convênio do Transferegov, "n.º 968082/2024").
                    texto=f"{titulo}. {clean_text(soup.get_text(' ', strip=True))}",
                ))
            if not itens or (mais_velho and mais_velho < self.piso_listagem):
                break
        return candidatos

    def _ler_facepe(self, cfg: ConfigUF) -> list[Candidato]:
        """Busca do WordPress 3.8 da FACEPE: os itens do tipo `edital` apontam o PDF e trazem
        o número no rótulo ("07/2025 - RESULTADO FINAL PPSUS-PE 2024")."""
        documentos: list[Documento] = []
        paginas = 8 if self.historical else 1
        for pagina in range(1, paginas + 1):
            url = cfg.url if pagina == 1 else cfg.url.replace("/?s=", f"/page/{pagina}/?s=")
            soup = self.soup(self._html(url))
            artigos = soup.select("article")
            datas_pagina = []
            for artigo in artigos:
                classes = artigo.get("class") or []
                titulo = artigo.select_one(".entry-title a, h2 a")
                quando = artigo.select_one("time[datetime]")
                data = (quando.get("datetime") or "")[:10] if quando else None
                if data:
                    datas_pagina.append(data)
                if "type-edital" not in classes or not titulo:
                    continue
                rotulo = re.sub(r"\s*\(Edital\)\s*$", "", clean_text(titulo.get_text(" ", strip=True)))
                href = urljoin(url, titulo.get("href"))
                if not e_ppsus(f"{rotulo} {href}"):
                    continue
                documentos.append(self._doc(rotulo, href, data=data,
                                            rotulo=f"{rotulo} {unquote(href.rsplit('/', 1)[-1])}"))
            if not artigos or (datas_pagina and min(datas_pagina) < self.piso_listagem):
                break
        return agrupar(cfg.uf, documentos)

    def _ler_fapitec(self, cfg: ConfigUF) -> list[Candidato]:
        """Busca HTML da FAPITEC: os editais são tipos próprios (`editais-abertos`,
        `editais-em-andamento`, `editais-encerrados`), sem data na listagem."""
        soup = self.soup(self._html(cfg.url))
        candidatos = []
        for artigo in soup.select("article"):
            classes = " ".join(artigo.get("class") or [])
            situacao = re.search(r"\b(editais-(?:abertos|em-andamento|encerrados))\b", classes)
            titulo = artigo.select_one(".entry-title a, h2 a")
            if not situacao or not titulo:
                continue
            rotulo = clean_text(titulo.get_text(" ", strip=True))
            if not e_ppsus(rotulo) or classificar(rotulo) != "edital":
                continue
            candidatos.append(Candidato(
                uf=cfg.uf, rotulo=rotulo, url=titulo["href"], pagina=titulo["href"],
                numero=numero_do_rotulo(rotulo, self.today),
                aberto=True if situacao.group(1) == "editais-abertos" else (
                    False if situacao.group(1) == "editais-encerrados" else None),
            ))
        return candidatos

    def _ler_montenegro(self, cfg: ConfigUF) -> list[Candidato]:
        """Tabela da plataforma Montenegro (FUNCAP): linha de título do edital, depois as
        linhas de documento com a data de publicação."""
        soup = self.soup(self._html(cfg.url))
        candidatos: list[Candidato] = []
        atual: Candidato | None = None
        for linha in soup.find_all("tr"):
            classes = linha.get("class") or []
            if "linha-titulo-edital" in classes:
                negrito = linha.find("b")
                titulo = clean_text((negrito or linha).get_text(" ", strip=True)).lstrip("- ")
                atual = None
                if e_ppsus(titulo) and not _EXCLUIR.search(dobrar(titulo)):
                    atual = Candidato(uf=cfg.uf, rotulo=titulo, url=cfg.url,
                                      numero=numero_do_rotulo(titulo, self.today))
                    candidatos.append(atual)
            elif "linha-documento-edital" in classes and atual is not None:
                link = linha.find("a", href=True)
                if not link:
                    continue
                celulas = linha.find_all("td")
                data = None
                if len(celulas) > 1:
                    achadas = datas(dobrar(celulas[-1].get_text(" ", strip=True)))
                    data = achadas[0][1] if achadas else None
                rotulo = clean_text(link.get_text(" ", strip=True)).lstrip("- ")
                atual.documentos.append(self._doc(rotulo, urljoin(cfg.url, link["href"]), data=data))
        for candidato in candidatos:
            datas_docs = [d.data for d in candidato.documentos if d.data]
            candidato.data = min(datas_docs) if datas_docs else None
            editais = [d for d in candidato.documentos if d.tipo == "edital"]
            if editais:
                candidato.url = editais[0].url
        return candidatos

    def _ler_fapt(self, cfg: ConfigUF) -> list[Candidato]:
        """Portal do Tocantins: índices de editais → pasta da edição (o id muda a cada edição)."""
        indices = ["01-editais-abertos/5sy36y0lf49g"]
        if self.historical or cfg.uf in self.sisct_ufs:
            indices += ["02-editais-em-andamento/jsiwbfyh088", "03-editais-finalizados/5iv88yy5aru4"]
        candidatos: dict[str, Candidato] = {}
        for indice in indices:
            url = urljoin(cfg.url or "", indice)
            soup = self.soup(self._html(url))
            for link in soup.select("a[href]"):
                rotulo = clean_text(link.get_text(" ", strip=True))
                href = urljoin(url, link["href"])
                if "/fapt/" not in href or not e_ppsus(rotulo) or href in candidatos:
                    continue
                candidatos[href] = Candidato(
                    uf=cfg.uf, rotulo=re.sub(r"^link\s+", "", rotulo), url=href, pagina=href,
                    aberto=True if indice.startswith("01-") else None,
                )
        return list(candidatos.values())

    def _ler_fapdf(self, cfg: ConfigUF) -> list[Candidato]:
        """Página fixa do PPSUS na FAPDF: a lista começa pela versão mais nova.

        O cabeçalho e o rodapé do Liferay também apontam `/documents/` (organograma,
        planejamento): só entram os documentos da chamada.
        """
        soup = self.soup(self._html(cfg.url or ""))
        documentos = []
        links = [a for a in soup.select("a[href]") if "/documents/" in a["href"]]
        for indice, link in enumerate(links):
            rotulo = clean_text(link.get_text(" ", strip=True))
            href = urljoin(cfg.url or "", link["href"])
            completo = f"{rotulo} {href.rsplit('/', 1)[-1]}"
            if not (e_ppsus(completo) or re.search(r"chamada|edital|extrato|resultado|retifica", dobrar(completo))):
                continue
            documentos.append(self._doc(rotulo, href, seq=-indice, rotulo=completo))
        return agrupar(cfg.uf, documentos)

    def _ler_fapes(self, cfg: ConfigUF) -> list[Candidato]:
        """FAPES: página de abertos no dia; na carga histórica, também a de encerrados (2,3 MB)."""
        urls = [cfg.url or ""]
        if self.historical:
            urls.append("https://fapes.es.gov.br/Editais/Encerrados")
        documentos: list[Documento] = []
        vistos: set[str] = set()
        for url in urls:
            soup = self.soup(self._html(url))
            for indice, link in enumerate(soup.select("a[href]")):
                rotulo = clean_text(link.get("title") or link.get_text(" ", strip=True))
                rotulo = re.sub(r"^Ver:\s*", "", rotulo).split(",")[0] if rotulo.startswith("Ver:") else rotulo
                href = urljoin(url, link["href"])
                if href in vistos or rotulo.lower() == "baixar" or not href.lower().split("?")[0].endswith(".pdf"):
                    continue
                if not e_ppsus(f"{rotulo} {href}"):
                    continue
                vistos.add(href)
                documentos.append(self._doc(rotulo, href, seq=-indice))
        candidatos = agrupar(cfg.uf, documentos)
        return [c for c in candidatos if not c.numero or c.numero.ano >= self.piso.year - 1]

    def _ler_fapemig(self, cfg: ConfigUF) -> list[Candidato]:
        """Sitemap das chamadas da FAPEMIG; o número e o ano vêm do slug, e a página SSR
        traz o resumo pronto ("Limite para submissão", "Valor da chamada")."""
        corpo = self._baixar(cfg.url or "").decode("utf-8", errors="replace")
        candidatos = []
        for m in re.finditer(r"<loc>\s*([^<]+?)\s*</loc>", corpo):
            loc = m.group(1)
            slug = loc.rstrip("/").rsplit("/", 1)[-1]
            if not re.search(r"ppsus|pesquisa-para-o-sus", slug):
                continue
            numero = _numero(re.search(r"chamada-(?:fapemig-|publica-)?(\d{1,3})-(\d{4})", slug), self.today)
            candidatos.append(Candidato(uf=cfg.uf, rotulo=slug.replace("-", " "), url=loc, pagina=loc, numero=numero))
        return candidatos

    def _ler_fapesp(self, cfg: ConfigUF) -> list[Candidato]:
        """Página do programa na FAPESP: "Chamada de propostas - Edital 2025" → fapesp.br/<id>."""
        soup = self.soup(self._html(cfg.url or ""))
        candidatos = []
        for link in soup.select("a[href]"):
            if clean_text(link.get_text(" ", strip=True)).lower() != "chamada de propostas":
                continue
            contexto = clean_text(link.parent.get_text(" ", strip=True))
            ano = re.search(r"Edital\s+(20\d{2})", contexto)
            if not ano:
                # As edições antigas (2004 a 2013) ficam em blocos sem "Edital AAAA": fora dos 24 meses.
                continue
            href = urljoin(cfg.url or "", link["href"])
            candidatos.append(Candidato(
                uf=cfg.uf, rotulo=f"Chamada PPSUS FAPESP – {contexto[:80]}", url=href, pagina=href,
                ano_listagem=int(ano.group(1)),
            ))
        return candidatos

    def _ler_faperj(self, cfg: ConfigUF) -> list[Candidato]:
        """Lista de editais da FAPERJ: o cronograma está no HTML, com as datas antigas riscadas.

        As datas riscadas (`line-through`) saem antes da leitura; a última que sobra é a vigente.
        """
        soup = self.soup(self._html(cfg.url or ""))
        for riscado in soup.select('[style*="line-through"], s, strike, del'):
            riscado.decompose()
        candidatos = []
        for link in soup.select("a[href]"):
            rotulo = clean_text(link.get_text(" ", strip=True))
            if not e_ppsus(rotulo) or classificar(rotulo) != "edital":
                continue
            bloco = link
            for _ in range(6):
                if bloco.parent is None:
                    break
                bloco = bloco.parent
                if len(bloco.get_text(" ", strip=True)) > 300:
                    break
            texto = clean_text(bloco.get_text(" ", strip=True))
            href = urljoin(cfg.url or "", link["href"])
            documentos = [self._doc(rotulo, href)]
            for outro in bloco.select("a[href]"):
                alvo = urljoin(cfg.url or "", outro["href"])
                if alvo != href and alvo.startswith("http") and alvo.lower().split("?")[0].endswith(".pdf"):
                    documentos.append(self._doc(outro.get_text(" ", strip=True) or "Documento", alvo))
            candidatos.append(Candidato(
                uf=cfg.uf, rotulo=rotulo, url=href, numero=numero_do_rotulo(rotulo, self.today),
                data=data_publicacao(texto), texto=f"{rotulo} {texto}", documentos=documentos,
            ))
        return candidatos

    def _anos(self, cfg: ConfigUF) -> list[int]:
        if self.historical:
            return list(range(self.piso.year, self.today.year + 1))
        if cfg.uf in self.sisct_ufs or self.today.month <= 3:
            return [self.today.year - 1, self.today.year]
        return [self.today.year]

    def _ler_fappr(self, cfg: ConfigUF) -> list[Candidato]:
        """Fundação Araucária: uma página de programas por ano; o bloco começa num
        `<h3>CP 08/25. PPSUS</h3>` e traz o edital e os atos (prorrogação, resultado)."""
        candidatos = []
        for ano in self._anos(cfg):
            url = (cfg.url or "").format(ano=ano)
            try:
                soup = self.soup(self._html(url))
            except FalhaUF as erro:
                if "HTTP 404" in str(erro):
                    continue  # a página do ano ainda não existe
                raise
            for titulo in soup.find_all("h3"):
                rotulo = clean_text(titulo.get_text(" ", strip=True))
                if not e_ppsus(rotulo):
                    continue
                documentos, partes = [], [rotulo]
                for irmao in titulo.find_next_siblings():
                    if irmao.name == "h3":
                        break
                    partes.append(clean_text(irmao.get_text(" ", strip=True)))
                    for link in irmao.select("a[href]"):
                        href = urljoin(url, link["href"])
                        mes = re.search(r"/documento/(\d{4})-(\d{2})/", href)
                        documentos.append(self._doc(
                            link.get_text(" ", strip=True), href,
                            data=f"{mes.group(1)}-{mes.group(2)}-01" if mes else None, aproximada=True,
                        ))
                editais = [d for d in documentos if d.tipo == "edital"]
                candidatos.append(Candidato(
                    uf=cfg.uf, rotulo=rotulo, url=editais[0].url if editais else url,
                    numero=numero_do_rotulo(rotulo, self.today) or (editais[0].numero if editais else None),
                    documentos=documentos, data=min((d.data for d in documentos if d.data), default=None),
                    data_aproximada=True,
                ))
        return candidatos

    def _ler_matriz(self, cfg: ConfigUF) -> list[Candidato]:
        """CMS Matriz da FAPERGS: a página do ano aponta o serviço JSON da lista (o id muda por ano)."""
        candidatos = []
        for ano in self._anos(cfg):
            url = (cfg.url or "").format(ano=ano)
            try:
                pagina = self._html(url)
            except FalhaUF as erro:
                if "HTTP 404" in str(erro):
                    continue
                raise
            fonte = re.search(r'data-matriz-source-uri="([^"]+)"', pagina)
            if not fonte:
                raise FalhaUF(f"{cfg.uf}: página {ano} sem a lista do Matriz")
            servico = urljoin(url, html_lib.unescape(fonte.group(1)))
            servico += ("&" if "?" in servico else "?") + "currentPage=1&pageSize=300"
            dados = self._json(servico)
            soup = self.soup(dados.get("body") or "")
            for artigo in soup.select("article"):
                link = artigo.select_one("h2 a[href]")
                if not link:
                    continue
                rotulo = clean_text(link.get_text(" ", strip=True))
                if not e_ppsus(rotulo) or classificar(rotulo) != "edital":
                    continue
                selo = artigo.select_one(".lista-categoria a")
                situacao = dobrar(selo.get_text(" ", strip=True)) if selo else ""
                href = urljoin(url, link["href"])
                candidatos.append(Candidato(
                    uf=cfg.uf, rotulo=rotulo, url=href, pagina=href, numero=numero_do_rotulo(rotulo, self.today),
                    aberto=True if situacao.startswith("abert") else (False if situacao.startswith("encerrad") else None),
                ))
        return candidatos

    def _ler_links(self, cfg: ConfigUF) -> list[Candidato]:
        """Página HTML qualquer (FAPERN): os links do PPSUS viram documentos."""
        soup = self.soup(self._html(cfg.url or ""))
        documentos = []
        for indice, link in enumerate(soup.select("a[href]")):
            rotulo = clean_text(link.get_text(" ", strip=True))
            href = urljoin(cfg.url or "", link["href"])
            if e_ppsus(f"{rotulo} {href}"):
                documentos.append(self._doc(rotulo or href, href, seq=-indice,
                                            rotulo=f"{rotulo} {unquote(href.rsplit('/', 1)[-1])}"))
        return agrupar(cfg.uf, documentos)

    def _ler_sisct_uf(self, cfg: ConfigUF) -> list[Candidato]:
        """AP e MT: o edital que o SISC&T lista, lido do PDF baixado na sessão pública."""
        if not self.sisct_ok:
            raise FalhaUF(f"{cfg.uf}: sem leitura da FAP e o SISC&T não respondeu")
        candidatos = []
        for titulo, link in self.sisct_ufs.get(cfg.uf, []):
            if link not in self._textos_sisct:
                raise FalhaUF(f"{cfg.uf}: o SISC&T lista um edital, mas o PDF não foi lido")
            candidatos.append(Candidato(
                uf=cfg.uf, rotulo=titulo, url=SISCT_URL, origem="sisct", aberto=True,
                documentos=[Documento(titulo=titulo, url=link, tipo="edital")],
                texto=self._textos_sisct[link],
            ))
        return candidatos

    def _sementes(self, cfg: ConfigUF) -> list[Candidato]:
        return [Candidato(
            uf=cfg.uf, rotulo=titulo, url=url, origem="semente",
            documentos=[self._doc(titulo, url)],
        ) for url, titulo in cfg.sementes]

    # -------------------------------------------------------- confirmação

    def _deve_inspecionar(self, cfg: ConfigUF, candidato: Candidato) -> bool:
        """Quem merece PDF. Na carga histórica, toda edição dentro dos 24 meses; no dia,
        só o que pode estar aberto (SISC&T, listagem "aberto" ou publicação recente)."""
        if candidato.origem in ("sisct", "semente"):
            return True
        referencia = candidato.data or (f"{candidato.ano}-12-31" if candidato.ano else None)
        if self.historical:
            return referencia is None or referencia >= self.piso_listagem
        hoje = self.today
        # O SISC&T lista toda chamada PPSUS aberta (a submissão passa por ele): a UF
        # que aparece lá é lida a fundo; a que não aparece, só se a FAP publicou algo
        # há menos de 30 dias (o formulário pode atrasar o lançamento).
        if cfg.uf in self.sisct_ufs:
            return referencia is None or referencia >= (hoje - timedelta(days=400)).isoformat()
        if candidato.aberto is not None:
            return candidato.aberto
        if candidato.data and candidato.data >= (hoje - timedelta(days=30)).isoformat():
            return True
        if not self.sisct_ok:
            # Sem a sentinela, a rede fica mais larga (e o orçamento decide o resto).
            if candidato.data:
                return candidato.data >= (hoje - timedelta(days=180)).isoformat()
            return candidato.ano is not None and candidato.ano >= hoje.year
        return False

    def _confirmar(self, cfg: ConfigUF, candidato: Candidato) -> Leitura | None:
        """Lê o que for preciso (página, retificações, edital) e devolve número e prazo.

        "Prazo de PDF só quando necessário": se a página da FAP já traz o número e a
        linha de submissão (FAPERJ, FAPESP, FAPEMIG, RSS da FAPEAM), nenhum PDF é lido.
        """
        avisos: list[str] = []
        documentos = list(candidato.documentos)
        texto_pagina = candidato.texto or ""
        url_principal = candidato.url
        encerrado_na_fonte = candidato.aberto is False

        if candidato.pagina:
            texto_pagina, extras, url_principal = self._ler_pagina(cfg, candidato.pagina)
            conhecidos = {d.url for d in documentos}
            documentos += [d for d in extras if d.url not in conhecidos]
        if texto_pagina:
            encerrado_na_fonte = encerrado_na_fonte or bool(re.search(
                r"prazo\s+de\s+submissao\s+encerrado|inscricoes\s+encerradas", dobrar(texto_pagina)))
        documentos = [d for d in documentos if d.tipo != "excluir"]
        if candidato.origem == "sisct":
            # O link do SISC&T é efêmero (sessão Wicket): o documento publicado é a página.
            documentos = [Documento(titulo=candidato.rotulo, url=SISCT_URL, tipo="edital")]
        tem_resultado = any(re.search(r"resultado|homolog|excedente", dobrar(d.titulo)) for d in documentos)

        # A página só vale como texto do edital nos leitores em que ela É o edital.
        pagina_e_edital = candidato.origem == "sisct" or cfg.leitor in PAGINA_E_EDITAL
        numero = numero_do_cabecalho(texto_pagina, self.today) if pagina_e_edital and texto_pagina else None
        prazo = prazo_submissao(texto_pagina) if pagina_e_edital and texto_pagina else None
        textos = [texto_pagina] if pagina_e_edital and texto_pagina else []
        ilegivel = False

        editais = sorted([d for d in documentos if d.tipo == "edital"], key=Documento.chave_tempo)
        retificacoes = [d for d in documentos if d.tipo == "retificacao"]
        ano_minimo = self.piso.year - 1 if self.historical else self.today.year - 1
        numerados = [d for d in editais if d.numero]
        if numerados and all(d.numero.ano < ano_minimo for d in numerados):
            return None  # edição antiga (a pasta "Editais PPSUS" de 2018 da FAPT, por exemplo)

        if candidato.origem != "sisct" and not (numero and prazo):
            principais = editais or sorted(retificacoes, key=Documento.chave_tempo)[:1]
            # Versões republicadas do edital (PI, AL, GO) contam como retificação para o prazo.
            posteriores = sorted(
                [d for d in retificacoes + editais[1:] if not principais or d is not principais[0]],
                key=Documento.chave_tempo, reverse=True,
            )
            # 1) Da retificação mais nova para a mais velha, até achar a linha de submissão.
            limite = RETIFICACOES_HISTORICO if self.historical else RETIFICACOES_DIA
            prazo_retificacao = None
            for retificacao in posteriores[:limite]:
                try:
                    texto = self._texto_pdf(retificacao.url)
                except (Bloqueio, OrcamentoEsgotado):
                    raise
                except FalhaUF as erro:
                    avisos.append(f"{cfg.uf}: retificação não lida ({erro})")
                    ilegivel = True
                    break
                if len(dobrar(texto)) < 200:
                    ilegivel = True  # PDF-imagem (a FAPESPA publicou assim a 1ª alteração)
                    break
                if _e_inovacao_nacional(texto):
                    retificacao.tipo = "excluir"
                    continue
                if _nao_e_edital(texto) and not re.search(r"retifica|altera|prorroga|errata|adendo",
                                                          dobrar(texto)[:400]):
                    retificacao.tipo = "outro"  # a "chamada" de 2026 da FAPEMA era de excedentes
                    continue
                numero = numero or numero_do_cabecalho(texto, self.today)
                prazo_retificacao = prazo_submissao(texto)
                if prazo_retificacao:
                    textos.append(texto)
                    break
            # 2) O edital: número oficial, elegibilidade e valor global.
            principal_lido = None
            for principal in principais:
                texto = self._texto_pdf(principal.url)
                if _e_inovacao_nacional(texto):
                    return None
                if _nao_e_edital(texto):
                    principal.tipo = "outro"
                    numero = numero or numero_do_cabecalho(texto, self.today)
                    continue
                principal_lido = principal
                textos.insert(0, texto)
                numero = numero_do_cabecalho(texto, self.today) or numero
                prazo = prazo_retificacao or prazo or prazo_submissao(texto)
                if not candidato.pagina or not pagina_e_edital:
                    url_principal = principal.url
                break
            if principal_lido is None:
                if not textos:
                    # Só resultados ou chamada de excedentes: não há chamada a publicar.
                    return None
                prazo = prazo_retificacao or prazo

        texto_edital = " ".join(textos)
        if numero is None and candidato.numero is not None:
            numero = candidato.numero
            avisos.append(f"{cfg.uf}: número do rótulo ({numero.texto}); o cabeçalho do edital não trouxe")
        if numero is None:
            self.avisos.append(f"{cfg.uf}: chamada sem número legível ({candidato.rotulo[:80]})")
            return None
        if candidato.numero and candidato.numero.chave != numero.chave:
            avisos.append(f"{cfg.uf}: rótulo diz {candidato.numero.texto}, o edital diz {numero.texto}; vale o edital")
        if _e_inovacao_nacional(texto_edital):
            return None

        # Publicação: a data do primeiro documento na listagem; sem ela, o "lançamento"
        # do cronograma do próprio edital (nunca o de uma retificação).
        datas_docs = [d.data for d in documentos
                      if d.data and not d.aproximada and d.tipo in ("edital", "retificacao")]
        publicado = min(datas_docs) if datas_docs else (None if candidato.data_aproximada else candidato.data)
        publicado = publicado or (data_publicacao(textos[0]) if textos else None)
        return Leitura(
            numero=numero, prazo=prazo, publicado=publicado, texto=texto_edital,
            documentos=[d for d in documentos if d.tipo != "excluir"],
            url=url_principal, ilegivel=ilegivel, tem_resultado=tem_resultado,
            encerrado_na_fonte=encerrado_na_fonte, avisos=avisos,
        )

    def _ler_pagina(self, cfg: ConfigUF, url: str) -> tuple[str, list[Documento], str]:
        """Página de detalhe: o texto e os PDFs. Na FAPESP, segue o meta-refresh."""
        pagina = self._html(url)
        final = url
        refresh = re.search(r"""http-equiv=["']refresh["'][^>]*content=["'][^"']*url=['"]?([^'">]+)""", pagina, re.I)
        if refresh:
            final = urljoin(url, refresh.group(1).strip())
            pagina = self._html(final)
        soup = self.soup(pagina)
        for lixo in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            lixo.decompose()
        principal = soup.select_one("main, article, #content, .entry-content, #conteudo") or soup
        documentos = []
        for indice, link in enumerate(principal.select("a[href]")):
            href = urljoin(final, link["href"])
            rotulo = clean_text(link.get_text(" ", strip=True)).removeprefix("file_open").strip()
            consulta = parse_qs(urlsplit(href).query)
            if "url" in consulta:  # FAPEMIG: /files/<nome>?title=…&url=<pdf>
                href = consulta["url"][0]
                rotulo = consulta.get("title", [rotulo])[0]
            alvo = href.lower().split("?")[0]
            if not (alvo.endswith(".pdf") or "central.to.gov.br/download" in alvo):
                continue
            # Recência: o id de download da FAPT cresce; no WordPress, a pasta do upload
            # dá o mês. A ordem da página não serve (a FAPITEC põe o edital antes do aditivo).
            seq = int(re.search(r"/download/(\d+)", href).group(1)) if "/download/" in href else -indice
            mes = re.search(r"/uploads/(?:sites/\d+/)?(20\d{2})/(\d{2})/", href) or re.search(
                r"/arquivos/(20\d{2})(\d{2})/", href)
            data = f"{mes.group(1)}-{mes.group(2)}-01" if mes else None
            # Rótulo genérico ("Baixar", "EDITAL") ganha o nome do arquivo para classificar,
            # mas o rótulo curto e específico vale sozinho ("EDITAL" × "Retificado-Ed…pdf").
            arquivo = unquote(href.rsplit("/", 1)[-1])
            generico = len(rotulo) < 4 or dobrar(rotulo) in {"baixar", "download", "clique aqui", "aqui", "documento"}
            documentos.append(self._doc(rotulo or arquivo, href, seq=seq, data=data, aproximada=True,
                                        rotulo=f"{rotulo} {arquivo}" if generico else rotulo))
        return clean_text(principal.get_text(" ", strip=True)), documentos, final

    # --------------------------------------------------------------- item

    def _item(self, cfg: ConfigUF, leitura: Leitura) -> dict[str, Any]:
        numero = leitura.numero
        prazo = leitura.prazo
        hoje = self.today.isoformat()
        nota = []
        if prazo:
            status = "open" if prazo >= hoje else "closed"
            recente = (self.today - date.fromisoformat(prazo)).days <= 60
            if leitura.ilegivel and (status == "open" or recente):
                # A retificação mais nova não tem texto (PDF-imagem): o prazo pode ter mudado.
                nota.append(f"O cronograma lido marca {date.fromisoformat(prazo):%d/%m/%Y}, mas há retificação "
                            "posterior sem texto legível.")
                status, prazo = "unknown", None
        elif leitura.tem_resultado or leitura.encerrado_na_fonte:
            status = "closed"
        else:
            status = "unknown"

        texto = leitura.texto
        organizacoes = list(ORGANIZACOES)
        if cfg.aceita_servico_saude or aceita_servico_saude(texto):
            organizacoes.append("hospital")
        parceira = preve_empresa_parceira(texto)
        ed = edicao(texto)
        titulo = (f"Chamada PPSUS {cfg.fap} nº {numero.texto} ({cfg.uf}) – Programa Pesquisa para o SUS: "
                  f"gestão compartilhada em saúde" + (f", {ed} edição" if ed else ""))
        descricao = [
            f"Auxílio à pesquisa do PPSUS na UF {cfg.uf}, com recursos do Decit/MS (repassados pelo CNPq) e da "
            f"{cfg.fap}, em linhas definidas com a {cfg.ses}.",
            "Propõe o pesquisador com vínculo numa executora sediada na UF: IES, ICT pública ou privada sem fins "
            "lucrativos, centro de P&D sem fins lucrativos ou empresa pública"
            + (", e também serviço de saúde." if "hospital" in organizacoes else "."),
        ]
        if parceira:
            descricao.append("A empresa privada entra só como parceira, com termo de compromisso e contrapartida.")
        descricao += nota
        documentos = [{"title": d.titulo, "url": self._ascii(d.url) if d.url.startswith("http") else self.url,
                       "published": None if d.aproximada else d.data} for d in leitura.documentos]
        if not documentos:
            documentos = [{"title": "Edital", "url": self._ascii(leitura.url), "published": leitura.publicado}]
        return opportunity(
            source_id=self.id, source_name=self.name, source_url=self._ascii(leitura.url),
            checked_at=self.checked_at, external_id=f"{cfg.uf.lower()}-{numero.ano}-{numero.numero}",
            title=titulo, funder=f"Decit/MS e {cfg.fap}", status=status,
            published=leitura.publicado, deadline=prazo,
            organization_types=organizacoes, partner_types=["empresa"] if parceira else None,
            partners=["Decit/MS", "CNPq", cfg.fap, cfg.ses],
            geography=[cfg.uf], themes=temas(texto), instrument_type="auxilio_pesquisa", repayable=False,
            program_budget=valor_global(texto), documents=documentos, description=" ".join(descricao),
            closed=prazo if status == "closed" and prazo else None,
        )

    # ------------------------------------------------------------- coleta

    def _coletar_uf(self, cfg: ConfigUF) -> list[dict[str, Any]]:
        self._uf_atual = cfg.uf
        candidatos: list[Candidato] = []
        falhou: FalhaUF | None = None
        if self.historical and cfg.sementes:
            candidatos += self._sementes(cfg)
        try:
            candidatos += self._leitores()[cfg.leitor](cfg)
        except FalhaUF as erro:
            falhou = erro
        leituras: dict[tuple[int, int], Leitura] = {}
        for candidato in candidatos:
            if not self._deve_inspecionar(cfg, candidato):
                continue
            try:
                leitura = self._confirmar(cfg, candidato)
            except OrcamentoEsgotado:
                raise
            except FalhaUF as erro:
                falhou = falhou or erro
                continue
            if leitura is None:
                continue
            self.avisos += leitura.avisos
            chave = leitura.numero.chave
            if chave in leituras:
                # Mesma chamada achada por dois caminhos: junta os documentos, fica o prazo lido.
                anterior = leituras[chave]
                conhecidos = {d.url for d in anterior.documentos}
                anterior.documentos += [d for d in leitura.documentos if d.url not in conhecidos]
                anterior.prazo = anterior.prazo or leitura.prazo
                continue
            leituras[chave] = leitura
        if falhou is not None:
            self.failed_scopes.add(cfg.uf)
            self.avisos.append(f"{cfg.uf}: {falhou}")
        itens = []
        for leitura in leituras.values():
            # Fora dos 24 meses não entra (a pasta de 2018 da FAPT, sem prazo legível,
            # entraria como encerrada sem data). Sem prazo nem publicação, vale o ano do número.
            referencia = leitura.prazo or leitura.publicado
            if referencia and referencia < self.piso.isoformat():
                if leitura.numero.ano >= self.piso.year:
                    # Número recente com prazo antigo é sinal de leitura errada: avisa.
                    self.avisos.append(f"{cfg.uf}: {leitura.numero.texto} lido com prazo {referencia}, fora dos 24 meses")
                continue
            if not referencia and leitura.numero.ano < self.piso.year:
                continue
            itens.append(self._item(cfg, leitura))
        return itens

    def collect(self) -> list[dict[str, Any]]:
        self._ler_sisct()
        itens: list[dict[str, Any]] = []
        ordem = sorted(self.ufs, key=lambda uf: (uf not in self.sisct_ufs, uf))
        for uf in ordem:
            cfg = UFS[uf]
            antes_pedidos, antes_tempo = self.pedidos, self._relogio()
            novos: list[dict[str, Any]] = []
            try:
                novos = self._coletar_uf(cfg)
            except OrcamentoEsgotado as erro:
                self.failed_scopes.add(uf)
                self.avisos.append(f"{uf}: {erro}")
            itens += novos
            if not self.fixture:
                # Uma linha por UF no log da Action: é o que mostra qual FAP demora ou falha.
                situacao = " · sem resposta" if uf in self.failed_scopes else ""
                print(f"ppsus {uf}: {len(novos)} itens, {self.pedidos - antes_pedidos} pedidos, "
                      f"{self._relogio() - antes_tempo:.0f} s{situacao}", file=sys.stderr, flush=True)
        if self.ufs and self.failed_scopes >= set(self.ufs):
            raise FalhaUF("nenhuma UF respondeu: " + "; ".join(self.avisos[-5:]))
        return itens

    def snapshot(self) -> dict[str, Any]:
        itens = self.collect()
        resultado: dict[str, Any] = {
            "source": {"id": self.id, "name": self.name, "url": self.url},
            "checked_at": self.checked_at,
            "status": "healthy",
            "error": None,
            "opportunities": itens,
        }
        if self.failed_scopes:
            resultado["failed_scopes"] = sorted(self.failed_scopes)
        # Para quem lê o artefato da coleta (o pipeline ignora estas chaves).
        resultado["requests"] = self.pedidos
        resultado["sisct_open"] = sorted(self.sisct_ufs)
        resultado["warnings"] = self.avisos[:60]
        return resultado
