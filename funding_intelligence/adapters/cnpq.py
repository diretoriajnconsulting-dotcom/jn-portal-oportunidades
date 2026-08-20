from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from funding_intelligence.adapters.base import BaseAdapter
from funding_intelligence.models import clean_text, opportunity, parse_br_date, slug


class CnpqAdapter(BaseAdapter):
    id = "cnpq"
    name = "CNPq"
    url = "https://www.gov.br/cnpq/pt-br/chamadas/abertas-para-submissao"

    def collect(self) -> list[dict[str, Any]]:
        soup = self.soup(self.fetch_html())
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for heading in soup.select("h2, h3, h4, h5"):
            title = clean_text(heading.get_text(" ", strip=True))
            external = self._external_id(title)
            if not external or external in seen:
                continue
            seen.add(external)
            parts: list[str] = []
            links: list[dict[str, Any]] = []
            container = heading.find_parent("div", class_="item")
            if container:
                parts.append(clean_text(container.get_text(" ", strip=True)))
                nodes = container.select("a[href]")
                for link in nodes:
                    links.append({
                        "title": clean_text(link.get_text(" ", strip=True)) or "Documento da chamada",
                        "url": urljoin(self.url, link.get("href")),
                        "published": None,
                    })
            else:
                for sibling in heading.next_siblings:
                    sibling_name = getattr(sibling, "name", None)
                    if sibling_name in {"h2", "h3", "h4", "h5"}:
                        break
                    if not hasattr(sibling, "get_text"):
                        continue
                    text = clean_text(sibling.get_text(" ", strip=True))
                    parts.append(text)
                    for link in sibling.select("a[href]") if hasattr(sibling, "select") else []:
                        links.append({
                            "title": clean_text(link.get_text(" ", strip=True)) or "Documento da chamada",
                            "url": urljoin(self.url, link.get("href")),
                            "published": None,
                        })
            block = re.sub(r"\s*/\s*", "/", " ".join(parts))
            date_value = r"\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})"
            dates = re.search(
                rf"Inscri[cç][oõ]es?(?:\s+\d+\S*\s+Rodada)?\s*:?\s*(?:de\s*)?({date_value})\s*(?:a|at[eé])\s*({date_value})",
                block, re.I,
            )
            published = parse_br_date(dates.group(1)) if dates else None
            deadline = parse_br_date(dates.group(2)) if dates else self._deadline(block)
            objective = re.search(r"Objetivo\s*:?\s*(.+?)(?=Inscri[cç][oõ]es|$)", block, re.I)
            source_url = links[0]["url"] if links else self.url
            found.append(opportunity(
                source_id=self.id, source_name=self.name, source_url=source_url,
                checked_at=self.checked_at, external_id=external, title=title,
                funder="CNPq", status="open" if deadline and deadline >= self.today.isoformat() else "unknown",
                published=published, deadline=deadline,
                organization_types=self._audience(title, block), geography=["BR"],
                themes=self._themes(title + " " + block), instrument_type="bolsa",
                repayable=False, documents=links, description=objective.group(1) if objective else None,
            ))
        return found

    @staticmethod
    def _external_id(title: str) -> str | None:
        if not re.search(r"\b(?:chamada|edital)\b", title, re.I):
            return None
        match = re.search(r"(\d{1,3}/\d{4})", title)
        return match.group(1).replace("/", "-") if match else None

    @staticmethod
    def _deadline(block: str) -> str | None:
        match = re.search(r"(?:at[eé]|prazo[^:]*:)\s*(\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))", block, re.I)
        return parse_br_date(match.group(1)) if match else None

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
        folded = slug(text)
        dictionary = {
            "biotecnolog": "biotecnologia", "empreendedor": "empreendedorismo",
            "inovacao": "inovacao", "africa": "cooperacao_internacional",
            "atlant": "cooperacao_internacional", "saude": "saude",
            "clima": "clima", "energia": "energia",
        }
        return [theme for needle, theme in dictionary.items() if needle in folded]
