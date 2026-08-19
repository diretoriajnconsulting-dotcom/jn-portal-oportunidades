from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any


ORGANIZATION_TYPES = {
    "empresa", "startup", "osc", "ict", "universidade", "municipio",
    "estado", "consorcio_publico", "cooperativa", "pesquisador",
    "pessoa_fisica", "outros",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ascii_fold(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in value if not unicodedata.combining(c)).lower()


def slug(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", ascii_fold(value)).strip("-")


def stable_id(source_id: str, external_id: str | None, title: str) -> str:
    if external_id:
        return f"{slug(source_id)}-{slug(external_id)}"
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
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
) -> dict[str, Any]:
    title = clean_text(title)
    org_types = [x for x in dict.fromkeys(organization_types or ["outros"])
                 if x in ORGANIZATION_TYPES]
    return {
        "id": stable_id(source_id, external_id, title),
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
        "matching": {"ponte_score": 0, "portfolio_tags": [], "matches": []},
    }
