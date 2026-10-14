from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# Por onde a proposta entra. Os três primeiros são as três janelas de datas do
# SICONV (RECEB_PROP, EMENDA_PAR, BENEF_ESP); `chamada_publica` é o edital aberto
# que as demais fontes publicam. Contrato 2.1.
# `fluxo_continuo` (contrato 2.2) é a porta sem edital nem prazo: um produto de
# crédito, o modelo EMBRAPII de projeto com uma Unidade. Vai com `status: "open"`
# e prazo nulo, e o site mostra num bloco próprio, fora da contagem de janelas.
CHANNELS = {"voluntaria", "emenda_parlamentar", "beneficiario_especifico", "chamada_publica", "fluxo_continuo"}

# Contrato 2.2: `hospital`, `fundacao_apoio` e `ict_privada` (sem fins lucrativos,
# Lei 10.973/2004, art. 2º, V). Desde então `ict` quer dizer ICT pública.
ORGANIZATION_TYPES = {
    "empresa", "startup", "osc", "ict", "ict_privada", "universidade", "hospital",
    "fundacao_apoio", "municipio", "estado", "consorcio_publico", "cooperativa",
    "pesquisador", "pessoa_fisica", "outros",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ascii_fold(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in value if not unicodedata.combining(c)).lower()


def slug(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", ascii_fold(value)).strip("-")


def stable_id(
    source_id: str, external_id: str | None, title: str,
    source_url: str | None = None,
) -> str:
    if external_id:
        return f"{slug(source_id)}-{slug(external_id)}"
    identity = title
    if source_url:
        parsed = urlsplit(source_url)
        query = urlencode(sorted(
            (key, value) for key, value in parse_qsl(parsed.query)
            if not key.lower().startswith("utm_")
        ))
        identity = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), query, ""))
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"{slug(source_id)}-{digest}"


def parse_br_date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})", value)
    if not match:
        return None
    day, month, year = map(int, match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" \n\t:-")


THEME_ALIASES = (
    (r"inteligencia artificial|\bia\b", "inteligencia_artificial"),
    (r"tecnolog(?:ia|ias) digitais|\btic\b|conectividade", "tic"),
    (r"tecnolog(?:ia|ias) digitais|digitalizacao", "transformacao_digital"),
    (r"nuvem", "nuvem"),
    (r"industria 4|semicondutor", "industria_4_0"),
    (r"economia circular", "economia_circular"),
    (r"cidade(?:s)? (?:inteligente|sustentavel)", "cidades_sustentaveis"),
    (r"sustentab|meio ambiente", "sustentabilidade"),
    (r"descarbon|transicao energetica", "descarbonizacao"),
    (r"energia", "energia"),
    (r"energia renovavel", "energia_renovavel"),
    (r"clima", "clima"),
    (r"seguranca hidrica|\bagua\b|saneamento", "seguranca_hidrica"),
    (r"saneamento", "saneamento"),
    (r"mobilidade|logistica", "mobilidade"),
    (r"bioeconomia", "bioeconomia"),
    (r"biotecnolog", "biotecnologia"),
    (r"agritech", "agritech"),
    (r"agro|foodtech", "agroindustria"),
    (r"saude|farmaco", "saude"),
    (r"terapias? avancadas?|terapia celular|celulas[ -]tronco|terapia genica|medicina regenerativa", "terapias_avancadas"),
    (r"ensaios? clinicos?|pesquisa clinica", "pesquisa_clinica"),
    (r"empreendedor", "empreendedorismo"),
    (r"cultura|audiovisual", "cultura"),
    (r"educacao|escola", "educacao"),
    (r"desenvolvimento regional|celso furtado", "desenvolvimento_regional"),
)


def canonical_themes(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = slug(value).replace("-", "_")
        if normalized and normalized not in result:
            result.append(normalized)
        folded = ascii_fold(value)
        for pattern, canonical in THEME_ALIASES:
            if re.search(pattern, folded) and canonical not in result:
                result.append(canonical)
    return result


def opportunity(
    *, source_id: str, source_name: str, source_url: str, checked_at: str,
    external_id: str | None, title: str, funder: str, status: str,
    published: str | None = None, deadline: str | None = None,
    organization_types: list[str] | None = None,
    geography: list[str] | None = None, themes: list[str] | None = None,
    instrument_type: str = "outros", repayable: bool | None = None,
    program_budget: float | None = None, documents: list[dict[str, Any]] | None = None,
    description: str | None = None,
    channel: str = "chamada_publica",
    partner_types: list[str] | None = None,
    partners: list[str] | None = None,
    closed: str | None = None,
) -> dict[str, Any]:
    """Uma oportunidade no contrato v2.

    Contrato 2.2 (campos opcionais, só aparecem quando há valor):
    - `partner_types`: quem entra só como parceira, sem propor nem receber. É o
      caso da empresa no PPSUS, no PDIL de 2024 e na Chamada CNPq 30/2025;
    - `partners`: os parceiros do edital (Decit/MS, CNPq, SES, Sebrae);
    - `closed`: a data em que encerrou, para guardar a encerrada por 24 meses.
    """
    if channel not in CHANNELS:
        raise ValueError(f"canal desconhecido: {channel!r}")
    title = clean_text(title)
    org_types = [x for x in dict.fromkeys(organization_types or ["outros"])
                 if x in ORGANIZATION_TYPES]
    partner_org_types = [x for x in dict.fromkeys(partner_types or [])
                         if x in ORGANIZATION_TYPES and x not in org_types]
    item = {
        "id": stable_id(source_id, external_id, title, source_url),
        "source": {
            "id": source_id,
            "name": source_name,
            "official": True,
            "url": source_url,
            "checked_at": checked_at,
            "stale": False,
        },
        "external_id": external_id,
        "title": title,
        "description": clean_text(description) or None,
        "funder": clean_text(funder),
        "instrument": {"type": instrument_type, "repayable": repayable},
        "channel": channel,
        "status": status,
        "dates": {"published": published, "deadline": deadline},
        "eligibility": {
            "organization_types": org_types or ["outros"],
            "geography": list(dict.fromkeys(geography or ["BR"])),
        },
        "themes": canonical_themes([x for x in themes or [] if x]),
        "funding": {"program_budget": program_budget},
        "documents": documents or [],
        "changes": [],
        "change_status": "baseline",
        "matching": {"eligible": False, "ponte_score": 0, "portfolio_tags": [], "matches": []},
    }
    if partner_org_types:
        item["eligibility"]["partner_types"] = partner_org_types
    partner_names = [clean_text(x) for x in dict.fromkeys(partners or []) if clean_text(x)]
    if partner_names:
        item["partners"] = partner_names
    if closed:
        item["dates"]["closed"] = closed
    return item
