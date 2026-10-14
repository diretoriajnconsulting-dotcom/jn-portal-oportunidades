"""BNDES: produtos contínuos do catálogo e chamadas de seleção de fundos da BNDESPAR.

Estudo de 14/10/2026 (`radar-fontes-saude/estudo/bndes.md`). O BNDES não tem uma página única
de chamadas; o adaptador lê duas portas:

1. **Produtos contínuos** (canal `fluxo_continuo`): a API JSON que alimenta o Navegador de
   Financiamentos (`/apoioFinanceiro/rest/linhasProgramas`, 171 produtos, sem autenticação). Um
   pedido por rodada. Só entra o que está em `PRODUTOS` (a regra está no comentário da tabela).
2. **Chamadas com prazo**: a listagem de chamadas públicas para seleção de fundos, com as seções
   "Chamadas em andamento" e "Chamadas encerradas". "Em andamento" **não** quer dizer aberta: em
   14/10/2026 a FIP IA 2026 seguia lá com o prazo vencido em 28/05/2026. Por isso o detalhe de cada
   uma é lido, e a situação sai do prazo lido.

Fica de fora, de propósito:
- as chamadas públicas para contratação de inovação (CPSI, LC 182/2021): é o BNDES comprando
  solução para si, não fomento;
- a parceria EMBRAPII + BNDES (Funtec) Saúde: é contínua e entra pela fonte EMBRAPII, que tem a
  página dela. A API do BNDES não a lista como produto; lista o BNDES Funtec, que entra aqui;
- o BNDES Garagem: as páginas Tração e Criação não trazem prazo (só o PDF do edital traz);
- os anexos em `/wps/wcm/connect` (os PDFs dos editais): o robots.txt libera a pasta numa regra
  sem a barra inicial (`Allow: wps/wcm/connect/site`), que é ambígua. As datas estão no HTML.

Orçamento: até 10 pedidos na rodada diária e até 40 na carga histórica, com 2 s entre pedidos
(o `Crawl-delay: 2` do robots.txt).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from funding_intelligence.adapters.base import BaseAdapter
from funding_intelligence.models import ascii_fold, clean_text, opportunity, parse_br_date
from funding_intelligence.pipeline import RETENCAO_ENCERRADAS_DIAS


HOST = "www.bndes.gov.br"
BASE = f"https://{HOST}"
API_PRODUTOS = f"{BASE}/apoioFinanceiro/rest/linhasProgramas"
PAGINA_PRODUTO = f"{BASE}/wps/portal/site/home/financiamento/produto/"
LISTAGEM_FUNDOS = (
    f"{BASE}/wps/portal/site/home/mercado-de-capitais/fundos-de-investimentos/"
    "chamadas-publicas-para-selecao-de-fundos"
)
# O caminho WCM não é igual ao do portal: as chamadas moram em `chamadas-publicas-para-fundos`,
# e a página amigável, em `.../chamadas-publicas-para-selecao-de-fundos/<slug>`.
PASTA_WCM_FUNDOS = "chamadas-publicas-para-fundos"


class Ficha(NamedTuple):
    """Como um produto do catálogo vira oportunidade. `motivo` documenta por que ele entra."""

    motivo: str
    instrumento: str
    reembolsavel: bool | None
    proponentes: tuple[str, ...]
    temas: tuple[str, ...]
    parceiras: tuple[str, ...] = ()
    financiador: str = "BNDES"


# Regra de seleção dos produtos contínuos (14/10/2026). A API traz 171 produtos e não dá para
# publicar todos, nem para filtrar pelos campos dela: o setor "Saúde" vem em 27 produtos, entre
# eles o FUST (telecomunicações) e o Finame (máquinas), e a finalidade "Inovação" vem no crédito
# de capital de giro. A regra é esta:
#   1. só entra produto vigente (`expirado` falso e sem aviso de expiração ou suspensão);
#   2. entra o que está nesta tabela, que é a lista fechada dos que servem a PD&I, à saúde e ao
#      complexo industrial da saúde, a fármacos, a MPME e startups, e a capital (participação);
#   3. o resto fica de fora. Os vigentes que falam de saúde, inovação, pesquisa ou capital e não
#      entram estão em `FORA`, com o motivo;
#   4. produto novo, fora das duas listas, que fale desses temas não é publicado sozinho: vira
#      aviso no snapshot (`avisos`), para alguém classificar.
# Quem toma cada produto foi lido nas abas "Quem pode solicitar" no estudo de 14/10/2026.
# "Entes públicos" vira municipio e estado; "fundações e associações", osc e fundacao_apoio.
PRODUTOS: dict[str, Ficha] = {
    "bndes-funtec": Ficha(
        "PD&I não reembolsável, executada por instituição tecnológica com empresa parceira",
        "subvencao_economica", False, ("ict", "ict_privada", "universidade"),
        ("inovacao", "pesquisa_e_desenvolvimento"), parceiras=("empresa",),
    ),
    "programa-bndes-mais-inovacao-investimento": Ficha(
        "PD&I nas missões do CNDI (a Missão 2 é o complexo da saúde), crédito em TR",
        "credito", True,
        ("empresa", "cooperativa", "osc", "hospital", "ict_privada", "fundacao_apoio",
         "municipio", "estado"),
        ("inovacao", "transformacao_digital"),
    ),
    "programa-bndes-mais-inovacao-centros-pdi": Ficha(
        "implantação e ampliação de centros de PD&I", "credito", True,
        ("empresa", "osc", "hospital", "ict_privada"),
        ("inovacao", "pesquisa_e_desenvolvimento"),
    ),
    "bndes-inovacao": Ficha(
        "P&D, ensaios, certificação e assuntos regulatórios (Finem Inovação)", "credito", True,
        ("empresa", "cooperativa", "osc", "hospital", "ict_privada", "fundacao_apoio",
         "municipio", "estado"),
        ("inovacao", "pesquisa_e_desenvolvimento"),
    ),
    "bndes-producao-farmacos": Ficha(
        "produção de fármacos, medicamentos e dispositivos; adequação à Anvisa (antigo Profarma)",
        "credito", True, ("empresa", "osc", "fundacao_apoio", "municipio", "estado"),
        ("saude", "farmacos", "dispositivos_medicos"),
    ),
    "bndes-finem-educacao-saude": Ficha(
        "expansão de serviços de saúde privados e filantrópicos", "credito", True,
        ("empresa", "hospital", "osc", "municipio", "estado"),
        ("saude", "educacao", "assistencia_social"),
    ),
    "bndes-fornecedores-sus": Ficha(
        "crédito atrelado a fornecer dispositivos de saúde ao SUS", "credito", True,
        ("empresa",), ("saude", "dispositivos_medicos"),
    ),
    "cartao-bndes": Ficha(
        "MPME e MEI: compra de equipamentos credenciados", "credito", True,
        ("empresa", "startup", "osc"), ("mpme", "equipamentos"),
    ),
    "bndes-credito-pequenas-e-medias-empresas": Ficha(
        "MPME: crédito por banco credenciado", "credito", True,
        ("empresa", "startup"), ("mpme", "capital_de_giro"),
    ),
    "bndes-subscricao-de-valores-mobiliarios": Ficha(
        "participação acionária da BNDESPAR", "investimento", None,
        ("empresa",), ("mercado_de_capitais",), financiador="BNDESPAR",
    ),
    "fundos-ativos-investimento": Ficha(
        "carteira de fundos da BNDESPAR em período de investimento: a porta de startups ao capital",
        "investimento", None, ("empresa", "startup"), ("capital_de_risco", "startups"),
        financiador="BNDESPAR",
    ),
    "bndes-fundo-socioambiental": Ficha(
        "não reembolsável para projetos sociais, saúde entre os temas", "outros", False,
        ("osc", "municipio", "estado"),
        ("impacto_social", "saude", "educacao", "meio_ambiente", "desenvolvimento_regional"),
    ),
}

# Vigentes que falam de saúde, inovação, pesquisa ou capital e ficam de fora, com o motivo.
FORA: dict[str, str] = {
    "programa-bndes-mais-inovacao": "guarda-chuva das linhas Mais Inovação; as de PD&I entram uma a uma",
    "programa-bndes-mais-inovacao-aquisicao-de-bens": "compra de bens da Indústria 4.0: difusão, não PD&I",
    "programa-bndes-mais-inovacao-difusao-tecnologica": "difusão de tecnologia pronta, não PD&I",
    "bndes-mais-inovacao-investimento-automatico": "digitalização (Brasil Mais Produtivo), não PD&I",
    "bndes-credito-servicos-4.0": "digitalização de serviços, não PD&I",
    "fiis-apoio-saude": "FIIS: opera por edital da Casa Civil, com janela; não é porta contínua",
    "fiis-apoio-bens": "FIIS: opera por edital da Casa Civil, com janela; não é porta contínua",
    "fundos-de-credito-corporativo": "fundos de crédito para empresas maiores, não capital de risco",
    "bndes-debentures-oferta-publica": "compra de debêntures em oferta pública, não porta para projeto",
    "bndes-estruturacao-de-projetos": "estruturação de concessões para o poder público",
    "inovagro": "setorial: agropecuária",
    "bndes-saneamento-para-todos": "setorial: saneamento",
    "bndes-finem-conteudos-culturais": "setorial: cultura",
    "bndes-fsa-audiovisual-inovacao": "setorial: audiovisual",
    "bndes-finem-minerais-criticos-e-estrategicos": "setorial: mineração",
    "bndes-finem-producao-bk": "setorial: bens de capital",
    "fust-bndes": "setorial: telecomunicações",
    "funttel": "setorial: telecomunicações",
    "programa-prioritario-bndes-rota-2030": "setorial: mobilidade e automotivo",
}

# Vocabulário que faz um produto novo virar aviso (texto sem acento, em minúsculas).
VOCABULARIO = re.compile(
    r"saude|farmac|medicament|biotec|inova|pesquisa|p&d|pd&i|startup|cientific|tecnologic|"
    r"hospital|\bsus\b|fundos? de investimento|participac|subscric"
)
# Avisos do catálogo que tiram o produto de circulação. Há 18 produtos com `expirado: false` e o
# aviso "Este programa se encontra expirado": o campo sozinho não basta.
AVISO_FORA = re.compile(r"expirad|nao est(?:a|amos) mais|suspens|encerrad|sera comunicad")
AVISO_DATA = re.compile(r"(?:encerr(?:ad[ao]|ou)|expirad[ao]|ate)\s+(?:em\s+)?(\d{1,2}[./]\d{1,2}[./]\d{4})")

DATA = r"\d{1,2}[./]\d{1,2}[./]\d{4}"
# "até as 18h00min do dia", "até às 18h de", "até 18h00 do dia", "até 12.08.2026, às 18h".
HORA = r"(?:(?:as|às)\s+)?(?:\d{1,2}h\w*\s+(?:\([^)]*\)\s+)?(?:do\s+dia|de)\s+)?"
JANELA = re.compile(rf"(?:a\s+partir\s+de|\bde)\s+({DATA})\s*,?\s*(?:at[ée]|a)\s+{HORA}({DATA})", re.I)
# Prazo sem data de início, em página antiga ("As Propostas deverão ser encaminhadas ... até às 18h
# do dia 17/12/2021") ou em aviso de prorrogação ("o prazo para recebimento de propostas foi
# postergado para até as 18h do dia 21.10.2025").
SO_PRAZO = re.compile(rf"\bpropostas?\b[^.;]{{0,200}}?\bat[ée]\s+{HORA}({DATA})", re.I)
# Uma data só vale como prazo se a pista mais próxima antes dela for de envio de proposta. A Chamada
# de Clima tem "Apresentação das propostas ao Comitê de Seleção: de 15.12.2025 a 19.12.2025", e a
# de Transição Energética, "Divulgação das propostas qualificadas ... Até 19.09.2025": nas duas a
# palavra "propostas" aparece, mas a pista mais próxima é de outra etapa.
PISTA = re.compile(
    r"(?P<envio>encaminhad|encaminhament|enviad|\benvio\b|recebimento|inscri[çc]|submiss|protocol)|"
    r"(?P<outra>apresenta[çc]|apresentar|d[úu]vida|esclareciment|resultado|feedback|reuni[ãa]o|"
    r"comit[êe]|convoca|divulga[çc]|an[áa]lise|dilig[êe]ncia)",
    re.I,
)
PUBLICACAO = (
    re.compile(rf"lan[çc]ou,?\s+em\s+({DATA})", re.I),
    # "Em 07.04.2025, a BNDESPAR ... lançou": "Em" maiúsculo, abrindo a frase do lançamento.
    re.compile(rf"\bEm\s+({DATA})\s*,\s+(?:a|o)\s"),
    re.compile(rf"(?:divulgad|lan[çc]ad|publicad)[oa]\s+em\s+({DATA})", re.I),
)
PARCERIA = re.compile(r"em parceria com\s+(.{0,250}?)(?:,?\s+lan[çc]ou|,\s+partes|\.\s)", re.I)
PARCEIROS_CONHECIDOS = (
    ("Finep", re.compile(r"\bFinep\b|Financiadora de Estudos e Projetos", re.I)),
    ("Fundação Butantan", re.compile(r"Butantan", re.I)),
    ("Petrobras", re.compile(r"Petrobras|Petr[óo]leo Brasileiro", re.I)),
    ("Vale", re.compile(r"\bVale\b")),
)
SIGLA = re.compile(r"\(\s*[“\"']?([A-Z][A-Za-z]{1,14})[”\"']?\s*\)")
CAPITAL = re.compile(r"capital m[íi]nimo de R\$\s*([\d.,]+)\s*(milh|bilh)", re.I)
DESAFIO = re.compile(r"captcha|just a moment|cf-chl|attention required|cf-browser-verification", re.I)
TEMAS_CHAMADA = (
    ("saude", "saude"), ("biotecnolog", "biotecnologia"), ("startup", "startups"),
    ("inteligencia artificial", "inteligencia_artificial"), ("clima", "clima"),
    ("transicao energetica", "descarbonizacao"), ("descarboniz", "descarbonizacao"),
    ("inovac", "inovacao"), ("infraestrutura", "infraestrutura"), ("mineracao", "mineracao"),
    ("impacto", "impacto_social"), ("capital semente", "capital_de_risco"),
    ("venture", "capital_de_risco"),
)
# A listagem de encerradas vai da mais nova para a mais velha. Na carga histórica, três seguidas
# mais velhas que 24 meses encerram a leitura.
ANTIGAS_SEGUIDAS_PARA_PARAR = 3


class FonteIndisponivel(Exception):
    """A casa não respondeu como esperado: erro HTTP, bloqueio, desafio ou estrutura mudada."""


class OrcamentoEsgotado(FonteIndisponivel):
    """Acabou o orçamento de pedidos da rodada."""


class Entrada(NamedTuple):
    secao: str  # "andamento" ou "encerrada"
    slug: str
    url: str
    titulo: str


@dataclass
class Chamada:
    titulo: str | None = None
    inicio: str | None = None
    fim: str | None = None
    publicado: str | None = None
    resultado: bool = False
    parceiros: list[str] = field(default_factory=list)
    capital: float | None = None
    documentos: list[dict[str, Any]] = field(default_factory=list)
    resumo: str | None = None


def permitido(url: str) -> bool:
    """O robots.txt do BNDES, mais a decisão de não ler a pasta de anexos WCM.

    `Disallow: /wps` com `Allow: /wps/portal/site/home` (menos `/no-index`); `/apoioFinanceiro/`
    é livre. A regra `Allow: wps/wcm/connect/site` está sem a barra inicial, e a pasta fica fora.
    """
    partes = urlsplit(url)
    if partes.scheme != "https" or partes.netloc != HOST:
        return False
    caminho = partes.path
    if caminho.startswith(("/wps/wcm/connect", "/wps/portal/site/home/no-index", "/SiteBNDES",
                           "/arquivos/remuneracao", "/wps/contenthandler")):
        return False
    if caminho.startswith("/wps") and not caminho.startswith("/wps/portal/site/home"):
        return False
    return True


def normalizar(href: str, base: str = LISTAGEM_FUNDOS) -> tuple[str, str] | None:
    """(slug, URL estável) de um link de chamada do portal WebSphere.

    A mesma página chega em três formas: amigável, por caminho WCM (`?1dmy&urile=wcm:path:...`)
    e com o estado do portlet (`.../!ut/p/z1/<blob>/`). O sufixo `/!ut/` muda entre pedidos e
    nunca entra na URL nem no id; o slug é o último segmento do caminho.
    """
    absoluto = urljoin(base, href)
    partes = urlsplit(absoluto)
    if partes.netloc and partes.netloc != HOST:
        return None
    urile = dict(parse_qsl(partes.query, keep_blank_values=True)).get("urile", "")
    if urile.lower().startswith("wcm:path:"):
        caminho = urile[len("wcm:path:"):]
        segmentos = [s for s in caminho.split("/") if s]
        if "fundos-de-investimentos" not in segmentos or len(segmentos) < 2:
            return None
        slug_ = segmentos[-1]
        if segmentos[-2] == PASTA_WCM_FUNDOS:
            return slug_, f"{LISTAGEM_FUNDOS}/{slug_}"
        # Chamada fora da pasta comum (Criatec, Fundo Nordeste): fica a forma WCM, que é estável.
        return slug_, f"{LISTAGEM_FUNDOS}?1dmy&urile={quote('wcm:path:' + caminho, safe='')}"
    caminho = partes.path.split("/!ut/")[0].rstrip("/")
    segmentos = [s for s in caminho.split("/") if s]
    if "fundos-de-investimentos" not in segmentos or segmentos[-1] in {
        "fundos-de-investimentos", "chamadas-publicas-para-selecao-de-fundos",
    }:
        return None
    return segmentos[-1], f"{BASE}{caminho}"


def _de_envio(texto: str, posicao: int) -> bool:
    """A pista mais próxima antes de `posicao` (até 250 caracteres) é de envio de proposta?"""
    pistas = list(PISTA.finditer(texto[max(0, posicao - 250):posicao]))
    return bool(pistas) and pistas[-1].group("envio") is not None


def ler_janela(texto: str) -> tuple[str | None, str | None]:
    """(início, fim) da janela de envio de propostas, no formato AAAA-MM-DD.

    Entre os prazos de envio achados, fica o mais tardio: numa prorrogação a página costuma manter
    a frase antiga e acrescentar a nova ("prazo prorrogado", "postergado para até ...").
    """
    candidatos: list[tuple[str, str | None]] = []
    for match in JANELA.finditer(texto):
        fim = parse_br_date(match.group(2))
        if fim and _de_envio(texto, match.start()):
            candidatos.append((fim, parse_br_date(match.group(1))))
    for match in SO_PRAZO.finditer(texto):
        fim = parse_br_date(match.group(1))
        if fim and _de_envio(texto, match.start(1)):
            candidatos.append((fim, None))
    if not candidatos:
        return None, None
    fim = max(c[0] for c in candidatos)
    inicio = max((c[1] for c in candidatos if c[0] == fim and c[1]), default=None)
    if inicio is None:
        # O prazo veio de um aviso de prorrogação: o início é o da janela original.
        inicio = min((c[1] for c in candidatos if c[1]), default=None)
    return inicio, fim


def ler_publicacao(texto: str) -> str | None:
    """Data de lançamento do edital: a primeira menção no texto."""
    achados = []
    for padrao in PUBLICACAO:
        match = padrao.search(texto)
        if match:
            achados.append((match.start(1), parse_br_date(match.group(1))))
    achados = [a for a in achados if a[1]]
    return min(achados)[1] if achados else None


def ler_parceiros(texto: str) -> list[str]:
    match = PARCERIA.search(texto)
    if not match:
        return []
    trecho = match.group(1)
    achados = []  # (posição no texto, nome), para manter a ordem em que o edital cita
    for nome, padrao in PARCEIROS_CONHECIDOS:
        encontrado = padrao.search(trecho)
        if encontrado:
            achados.append((encontrado.start(), nome))
    conhecidos = {ascii_fold(nome) for _, nome in achados}
    for sigla in SIGLA.finditer(trecho):
        if ascii_fold(sigla.group(1)) not in conhecidos:
            achados.append((sigla.start(), sigla.group(1)))
            conhecidos.add(ascii_fold(sigla.group(1)))
    return [nome for _, nome in sorted(achados)]


def ler_capital(texto: str) -> float | None:
    """Capital mínimo do fundo ("O FIP terá capital mínimo de R$ 200 milhões")."""
    match = CAPITAL.search(texto)
    if not match:
        return None
    try:
        valor = float(match.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None
    return valor * (1_000_000_000 if match.group(2).lower().startswith("bilh") else 1_000_000)


def _ano(texto: str) -> int | None:
    match = re.search(r"(?<!\d)(20\d{2})(?!\d)", texto or "")
    return int(match.group(1)) if match else None


def _sem_html(valor: str | None) -> str:
    if not valor:
        return ""
    if "<" in valor:
        valor = BeautifulSoup(valor, "html.parser").get_text(" ", strip=True)
    return clean_text(valor)


def _limpar_paragrafo(texto: str) -> str:
    """Tira as etiquetas de anexo "(PDF - 102 Kb)" e o espaço que o HTML deixa antes da vírgula."""
    texto = re.sub(r"\(\s*(?:PDF|DOCX?|XLSX?)\s*-\s*[\d.,]+\s*[kKmM][bB]\s*\)", "", texto)
    return clean_text(re.sub(r"\s+([,.;:])", r"\1", texto))


def _encurtar(texto: str, limite: int = 500) -> str:
    if len(texto) <= limite:
        return texto
    corte = texto[:limite].rsplit(" ", 1)[0]
    return corte.rstrip(" ,;.") + "…"


class BndesAdapter(BaseAdapter):
    id = "bndes"
    name = "BNDES"
    # A página "Solicite seu financiamento" é a raiz do apoio financeiro e não muda há anos; o
    # Navegador de Financiamentos (app Angular) e a API mudam com mais facilidade.
    url = f"{BASE}/wps/portal/site/home/financiamento"

    pausa = 2.0
    orcamento_diario = 10
    orcamento_historico = 40

    def __init__(self, *, fixture: str | None = None, today=None):
        super().__init__(fixture=fixture, today=today)
        self._zerar()

    def _zerar(self) -> None:
        self.pedidos = 0
        self.falhas: list[str] = []
        self.avisos: list[str] = []
        self._bloqueados: set[str] = set()
        self._ultimo_pedido: dict[str, float] = {}

    @property
    def orcamento(self) -> int:
        return self.orcamento_historico if self.historical else self.orcamento_diario

    # ------------------------------------------------------------------ coleta

    def snapshot(self) -> dict[str, Any]:
        """Snapshot com falha parcial.

        Se uma parte não respondeu (o catálogo, a listagem ou o detalhe de uma chamada em
        andamento), o resto sai normalmente e `failed_scopes: ["BR"]` faz o pipeline manter os
        itens anteriores que não vieram hoje, em vez de dá-los por encerrados. `avisos` (produto
        novo a classificar, orçamento curto) é só para quem lê o snapshot; o pipeline ignora.
        """
        opportunities = self.collect()
        snapshot = {
            "source": {"id": self.id, "name": self.name, "url": self.url},
            "checked_at": self.checked_at,
            "status": "healthy",
            "error": None,
            "opportunities": opportunities,
        }
        if self.falhas:
            snapshot["failed_scopes"] = ["BR"]
            snapshot["error"] = "; ".join(self.falhas)[:500]
        if self.avisos:
            snapshot["avisos"] = list(self.avisos)
        return snapshot

    def collect(self) -> list[dict[str, Any]]:
        self._zerar()
        itens: list[dict[str, Any]] = []
        partes_lidas = 0
        try:
            itens.extend(self.produtos(self._json(API_PRODUTOS)))
            partes_lidas += 1
        except FonteIndisponivel as exc:
            self.falhas.append(f"catálogo de produtos: {exc}")
        try:
            itens.extend(self.chamadas_de_fundos())
            partes_lidas += 1
        except FonteIndisponivel as exc:
            self.falhas.append(f"chamadas de fundos: {exc}")
        if not partes_lidas:
            # Nada respondeu: é falha da fonte inteira, e o pipeline mantém o catálogo anterior.
            raise FonteIndisponivel("; ".join(self.falhas))
        return itens

    # ------------------------------------------------------------------ produtos

    def produtos(self, registros: Any) -> list[dict[str, Any]]:
        if not isinstance(registros, list) or not registros:
            raise FonteIndisponivel("catálogo vazio ou fora do formato esperado")
        por_slug: dict[str, dict[str, Any]] = {}
        for registro in registros:
            if isinstance(registro, dict) and registro.get("nome"):
                por_slug.setdefault(str(registro["nome"]).strip(), registro)
        if not set(PRODUTOS) & set(por_slug):
            # A API mudou de forma ou de nomes: tratar como degradada, não como zero produtos.
            raise FonteIndisponivel("nenhum produto da lista no catálogo")
        for slug_ in sorted(set(PRODUTOS) - set(por_slug)):
            self.avisos.append(f"produto da lista sumiu do catálogo: {slug_}")

        corte = (self.today - timedelta(days=RETENCAO_ENCERRADAS_DIAS)).isoformat()
        itens = []
        for slug_, registro in por_slug.items():
            aviso = ascii_fold(_sem_html(registro.get("aviso")))
            fora_de_circulacao = bool(registro.get("expirado")) or bool(AVISO_FORA.search(aviso))
            ficha = PRODUTOS.get(slug_)
            if ficha is None:
                texto = ascii_fold(f"{registro.get('titulo') or ''} {_sem_html(registro.get('infoBasicas'))}")
                if not fora_de_circulacao and slug_ not in FORA and VOCABULARIO.search(texto):
                    self.avisos.append(
                        f"produto novo no catálogo, a classificar: {slug_} ({clean_text(registro.get('titulo'))})"
                    )
                continue
            status, encerrado = "open", None
            if fora_de_circulacao:
                # Só volta como encerrado se o aviso disser quando; sem data, sai do snapshot e o
                # pipeline o encerra na data da rodada.
                data = AVISO_DATA.search(aviso)
                encerrado = parse_br_date(data.group(1)) if data else None
                if not encerrado or encerrado < corte:
                    continue
                status = "closed"
            itens.append(self._item_produto(slug_, registro, ficha, status, encerrado))
        return itens

    def _item_produto(
        self, slug_: str, registro: dict[str, Any], ficha: Ficha, status: str, encerrado: str | None,
    ) -> dict[str, Any]:
        caminho = str(registro.get("url") or "").split("/!ut/")[0]
        url = urljoin(BASE, caminho) if caminho.startswith("/") else f"{PAGINA_PRODUTO}{slug_}"
        return opportunity(
            source_id=self.id, source_name=self.name, source_url=url, checked_at=self.checked_at,
            external_id=f"produto-{slug_}", title=clean_text(registro.get("titulo")) or slug_,
            funder=ficha.financiador, status=status, deadline=None,
            organization_types=list(ficha.proponentes), partner_types=list(ficha.parceiras),
            geography=["BR"], themes=list(ficha.temas), instrument_type=ficha.instrumento,
            repayable=ficha.reembolsavel, description=_encurtar(_sem_html(registro.get("infoBasicas"))),
            channel="fluxo_continuo", closed=encerrado,
        )

    # ------------------------------------------------------------------ chamadas de fundos

    def chamadas_de_fundos(self) -> list[dict[str, Any]]:
        entradas = self.ler_listagem(self._texto(LISTAGEM_FUNDOS))
        itens: list[dict[str, Any]] = []
        vistos: set[str] = set()
        for entrada in entradas:
            if entrada.secao != "andamento" or entrada.slug in vistos:
                continue
            vistos.add(entrada.slug)
            try:
                chamada = self.ler_chamada(self._texto(entrada.url))
            except FonteIndisponivel as exc:
                # Sem o detalhe não há como saber se abriu: o item anterior fica como estava.
                self.falhas.append(f"chamada {entrada.slug}: {exc}")
                continue
            itens.append(self._item_chamada(entrada, chamada))
        if self.historical:
            itens.extend(self._encerradas([e for e in entradas if e.secao == "encerrada"], vistos))
        return itens

    def ler_listagem(self, html: str) -> list[Entrada]:
        soup = self.soup(html)
        entradas: list[Entrada] = []
        vistos: set[tuple[str, str]] = set()
        achou_secao = False
        for cabecalho in soup.find_all(re.compile(r"^(?:h[1-6]|strong|b)$")):
            rotulo = ascii_fold(cabecalho.get_text(" ", strip=True))
            if re.search(r"chamadas? em andamento", rotulo):
                secao = "andamento"
            elif re.search(r"chamadas? encerradas", rotulo):
                secao = "encerrada"
            else:
                continue
            achou_secao = True
            for elemento in cabecalho.find_all_next():
                if re.fullmatch(r"h[1-6]", elemento.name or ""):
                    break
                if elemento.name != "a" or not elemento.get("href") or not elemento.find_parent("li"):
                    continue
                normalizado = normalizar(elemento["href"])
                if not normalizado or (secao, normalizado[0]) in vistos:
                    continue
                vistos.add((secao, normalizado[0]))
                entradas.append(Entrada(secao, normalizado[0], normalizado[1],
                                        clean_text(elemento.get_text(" ", strip=True))))
        if not achou_secao:
            raise FonteIndisponivel("listagem sem as seções 'em andamento' e 'encerradas'")
        return entradas

    def ler_chamada(self, html: str) -> Chamada:
        soup = self.soup(html)
        titulo = soup.select_one("h1.tituloConteudoInterna")
        caixa = titulo.parent if titulo else (soup.body or soup)
        texto = re.sub(r"\s+", " ", caixa.get_text(" ", strip=True))
        inicio, fim = ler_janela(texto)
        resultado = any(
            re.match(r"\s*resultado", cabeca.get_text(" ", strip=True), re.I)
            for cabeca in caixa.find_all(re.compile(r"^(?:h[2-6]|strong|b)$"))
        ) or bool(re.search(r"resultado final", texto, re.I))
        documentos, urls = [], set()
        for link in caixa.select("a[href]"):
            href = urljoin(BASE, link["href"])
            if "/wps/wcm/connect/" not in href:
                continue
            # O `CVID` muda a cada versão do anexo; sem ele, o WCM serve a versão vigente.
            partes = urlsplit(href)
            consulta = "&".join(f"{k}={v}" for k, v in parse_qsl(partes.query) if k.upper() != "CVID")
            href = f"{partes.scheme}://{partes.netloc}{partes.path}" + (f"?{consulta}" if consulta else "")
            if href in urls:
                continue
            urls.add(href)
            documentos.append({
                "title": clean_text(link.get_text(" ", strip=True)) or "Documento da chamada",
                "url": href,
                "published": None,
            })
        resumo = next((
            _limpar_paragrafo(p.get_text(" ", strip=True)) for p in caixa.find_all("p")
            if len(clean_text(p.get_text(" ", strip=True))) >= 80
        ), None)
        return Chamada(
            titulo=clean_text(titulo.get_text(" ", strip=True)) if titulo else None,
            inicio=inicio, fim=fim, publicado=ler_publicacao(texto), resultado=resultado,
            parceiros=ler_parceiros(texto), capital=ler_capital(texto), documentos=documentos,
            resumo=resumo,
        )

    def _item_chamada(self, entrada: Entrada, chamada: Chamada) -> dict[str, Any]:
        hoje = self.today.isoformat()
        encerrada = None
        if entrada.secao == "encerrada":
            status = "closed"
            encerrada = chamada.fim if chamada.fim and chamada.fim <= hoje else None
        elif chamada.fim:
            if chamada.inicio and chamada.inicio > hoje:
                status = "scheduled"
            elif chamada.fim >= hoje:
                status = "open"
            else:
                status, encerrada = "closed", chamada.fim
        else:
            # "Em andamento" sem prazo legível: só a seção de resultado diz que acabou.
            status = "closed" if chamada.resultado else "unknown"
        titulo = chamada.titulo or entrada.titulo
        dobrado = ascii_fold(titulo)
        temas = ["fundos_de_investimento"]
        temas += [tema for agulha, tema in TEMAS_CHAMADA if agulha in dobrado]
        descricao = (
            "Seleção de fundo e gestor pela BNDESPAR: quem propõe é a gestora do fundo; as empresas "
            "que o fundo vai investir não se inscrevem nesta chamada."
        )
        if chamada.resumo:
            descricao += " " + _encurtar(chamada.resumo, 400)
        return opportunity(
            source_id=self.id, source_name=self.name, source_url=entrada.url,
            checked_at=self.checked_at, external_id=f"fundos-{entrada.slug}", title=titulo,
            funder="BNDESPAR", status=status, published=chamada.publicado or chamada.inicio,
            deadline=chamada.fim,
            # A gestora de fundos não tem tipo próprio no contrato: vai como "outros", para o
            # filtro por empresa ou startup não mostrar a chamada como se elas pudessem propor.
            organization_types=["outros"], geography=["BR"], themes=temas,
            instrument_type="investimento", repayable=None, program_budget=chamada.capital,
            documents=chamada.documentos, description=descricao, channel="chamada_publica",
            partners=chamada.parceiros, closed=encerrada,
        )

    def _encerradas(self, entradas: list[Entrada], vistos: set[str]) -> list[dict[str, Any]]:
        """Carga histórica: as encerradas dos últimos 24 meses, da mais nova para a mais velha."""
        corte = self.today - timedelta(days=RETENCAO_ENCERRADAS_DIAS)
        itens = []
        antigas_seguidas = 0
        for entrada in entradas:
            if entrada.slug in vistos:
                continue
            vistos.add(entrada.slug)
            ano = _ano(entrada.slug) or _ano(entrada.titulo)
            if ano and ano < corte.year:
                antigas_seguidas += 1
            else:
                try:
                    chamada = self.ler_chamada(self._texto(entrada.url))
                except OrcamentoEsgotado:
                    self.avisos.append(f"orçamento de {self.orcamento} pedidos esgotado nas encerradas")
                    break
                except FonteIndisponivel as exc:
                    self.avisos.append(f"encerrada {entrada.slug}: {exc}")
                    if HOST in self._bloqueados:
                        break
                    continue
                referencia = chamada.fim or chamada.publicado
                if not referencia:
                    self.avisos.append(f"encerrada {entrada.slug} sem data legível: não entra")
                    continue
                if referencia < corte.isoformat():
                    antigas_seguidas += 1
                else:
                    antigas_seguidas = 0
                    itens.append(self._item_chamada(entrada, chamada))
            if antigas_seguidas >= ANTIGAS_SEGUIDAS_PARA_PARAR:
                break
        return itens

    # ------------------------------------------------------------------ rede

    def _texto(self, url: str) -> str:
        return self._pedir(url).decode("utf-8", errors="replace")

    def _json(self, url: str) -> Any:
        try:
            return json.loads(self._pedir(url).decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise FonteIndisponivel(f"JSON inválido em {url}: {exc}") from exc

    def _pedir(self, url: str) -> bytes:
        if not permitido(url):
            raise FonteIndisponivel(f"fora do permitido pelo robots.txt: {url}")
        host = urlsplit(url).netloc
        if host in self._bloqueados:
            raise FonteIndisponivel(f"{host} bloqueou a leitura nesta rodada")
        if self.pedidos >= self.orcamento:
            raise OrcamentoEsgotado(f"orçamento de {self.orcamento} pedidos esgotado")
        self.pedidos += 1
        if self.fixture:
            return self._ler_fixture(url)
        self._esperar(host)
        try:
            corpo = self.fetch_bytes(url)
        except urllib.error.HTTPError as exc:
            exc.close()  # a resposta de erro também é um arquivo aberto
            if exc.code in (403, 429):
                self._bloqueados.add(host)
                self.falhas.append(f"HTTP {exc.code} em {url}: leitura parada em {host}")
                raise FonteIndisponivel(f"HTTP {exc.code} (bloqueio)") from exc
            raise FonteIndisponivel(f"HTTP {exc.code} em {url}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise FonteIndisponivel(f"{type(exc).__name__} em {url}: {exc}") from exc
        finally:
            self._ultimo_pedido[host] = time.monotonic()
        if DESAFIO.search(corpo[:20000].decode("utf-8", errors="replace")):
            self._bloqueados.add(host)
            self.falhas.append(f"página de desafio (CAPTCHA ou Cloudflare) em {url}: leitura parada em {host}")
            raise FonteIndisponivel("página de desafio")
        return corpo

    def _esperar(self, host: str) -> None:
        ultimo = self._ultimo_pedido.get(host)
        if ultimo is None or not self.pausa:
            return
        falta = self.pausa - (time.monotonic() - ultimo)
        if falta > 0:
            time.sleep(falta)

    def _ler_fixture(self, url: str) -> bytes:
        """A fixture é uma pasta: o catálogo, a listagem e uma página por chamada (`chamada_<slug>.html`)."""
        pasta = Path(self.fixture)
        if url == API_PRODUTOS:
            nome = "linhas_programas.json"
        elif url == LISTAGEM_FUNDOS:
            nome = "chamadas_fundos.html"
        else:
            normalizado = normalizar(url)
            nome = f"chamada_{normalizado[0]}.html" if normalizado else ""
        caminho = pasta / nome
        if not nome or not caminho.is_file():
            raise FonteIndisponivel(f"sem fixture para {url}")
        return caminho.read_bytes()
