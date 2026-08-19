from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import urljoin

from funding_intelligence.adapters.base import BaseAdapter
from funding_intelligence.models import clean_text, opportunity, parse_br_date, slug


class FapesqAdapter(BaseAdapter):
    """Reconstructs edital -> documents -> current deadline.

    The FAPESQ listing mixes original notices, retifications and results. Entries
    are therefore grouped by edital number, and "open" is only emitted when a
    current deadline is found in listing context or an official document.
    """

    id = "fapesq"
    name = "FAPESQ-PB"
    url = "https://fapesq.rpp.br/editais/2026/editais-2026"

    def collect(self) -> list[dict[str, Any]]:
        soup = self.soup(self.fetch_html())
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for link in soup.select("a[href]"):
            label = clean_text(link.get_text(" ", strip=True))
            href = urljoin(self.url, link.get("href"))
            external = self._external_id(label + " " + href)
            if not external:
                continue
            parent_text = clean_text(link.parent.get_text(" ", strip=True))
            groups[external].append({"title": label or f"Edital {external}", "url": href, "context": parent_text})

        found = []
        for external, items in sorted(groups.items()):
            primary = self._primary_document(items)
            evidence = " ".join(item["context"] for item in items)
            if not self.fixture:
                evidence, items = self._expand_detail_pages(evidence, items)
                for item in self._documents_to_inspect(items):
                    if not item["url"].lower().split("?")[0].endswith(".pdf"):
                        continue
                    try:
                        evidence += " " + self.pdf_text(item["url"])
                    except Exception:
                        # One broken attachment must not hide the remaining edital.
                        continue
            documents = [{"title": item["title"], "url": item["url"], "published": None} for item in items]
            deadline = self._deadline(evidence)
            published = self._published(evidence)
            status = "open" if deadline and deadline >= self.today.isoformat() else (
                "closed" if deadline else "unknown"
            )
            title = self._canonical_title(primary["title"], external)
            found.append(opportunity(
                source_id=self.id, source_name=self.name, source_url=primary["url"],
                checked_at=self.checked_at, external_id=external.replace("/", "-"),
                title=title, funder="FAPESQ-PB", status=status,
                published=published, deadline=deadline,
                organization_types=self._audience(evidence), geography=["PB", "BR"],
                themes=self._themes(title + " " + evidence), instrument_type=self._instrument(evidence),
                repayable=False, documents=documents,
            ))
        return found

    def _expand_detail_pages(
        self, evidence: str, items: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        expanded = list(items)
        known_urls = {item["url"] for item in expanded}
        for item in list(items):
            if item["url"].lower().split("?")[0].endswith(".pdf"):
                continue
            try:
                soup = self.soup(self.fetch_bytes(item["url"]).decode("utf-8", errors="replace"))
            except Exception:
                continue
            evidence += " " + clean_text(soup.get_text(" ", strip=True))
            for link in soup.select("a[href]"):
                href = urljoin(item["url"], link.get("href"))
                label = clean_text(link.get_text(" ", strip=True)) or "Documento do edital"
                if href in known_urls or not href.lower().split("?")[0].endswith(".pdf"):
                    continue
                known_urls.add(href)
                expanded.append({"title": label, "url": href, "context": ""})
        return evidence, expanded

    @staticmethod
    def _external_id(value: str) -> str | None:
        folded = value.replace("_", " ").replace("-", " ")
        match = re.search(r"(?:edital\D{0,12})?(\d{1,3})\s*[./ ]\s*(20\d{2})", folded, re.I)
        if not match:
            return None
        return f"{int(match.group(1)):02d}/{match.group(2)}"

    @staticmethod
    def _primary_document(items: list[dict[str, Any]]) -> dict[str, Any]:
        def rank(item: dict[str, Any]) -> tuple[int, int]:
            text = slug(item["title"])
            penalty = 2 if any(x in text for x in ("resultado", "classifica", "homologa")) else 0
            bonus = 0 if "edital" in text and "retifica" not in text else 1
            return penalty + bonus, len(text)
        return min(items, key=rank)

    @staticmethod
    def _documents_to_inspect(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        relevant = [item for item in items if any(
            word in slug(item["title"]) for word in ("edital", "retifica", "cronograma", "prorroga")
        )]
        return relevant[:3]

    @staticmethod
    def _deadline(text: str) -> str | None:
        candidates = []
        date_value = r"(?:\d{1,2}/\d{1,2}/\d{4}|\d{1,2}\s+de\s+[a-zç]+\s+de\s+\d{4})"
        patterns = (
            rf"(?:inscri.{{0,4}}es?|submiss.{{0,3}}o|envio\s+de\s+propostas|prazo)[^\n.]{{0,140}}?(?:at[eé�]|a)\s*(?:o\s+dia\s+)?({date_value})",
            rf"(?:t[eé]rmino|encerramento)[^\n.]{{0,100}}?({date_value})",
            rf"(?:{date_value})\s+(?:a|at[eé�])\s+({date_value})[^\n.]{{0,100}}?(?:per.{{0,3}}odo\s+de\s+inscri.{{0,3}}o|submiss.{{0,3}}o)",
            rf"\b\d{{1,2}}\s+a\s+({date_value})[^\n.]{{0,100}}?(?:per.{{0,3}}odo\s+de\s+inscri.{{0,3}}o|submiss.{{0,3}}o)",
        )
        for pattern in patterns:
            candidates.extend(filter(None, (FapesqAdapter._parse_date(value) for value in re.findall(pattern, text, re.I))))
        return max(candidates) if candidates else None

    @staticmethod
    def _published(text: str) -> str | None:
        date_value = r"(?:\d{1,2}/\d{1,2}/\d{4}|\d{1,2}\s+de\s+[a-zç]+\s+de\s+\d{4})"
        match = re.search(rf"(?:publica[cç][aã]o|lan[cç]amento)[^\n.]{{0,80}}?({date_value})", text, re.I)
        return FapesqAdapter._parse_date(match.group(1)) if match else None

    @staticmethod
    def _parse_date(value: str) -> str | None:
        numeric = parse_br_date(value)
        if numeric:
            return numeric
        months = {
            "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4,
            "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
            "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
        }
        folded = slug(value).replace("-de-", " ").replace("-", " ")
        match = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", folded)
        if not match or match.group(2) not in months:
            return None
        return f"{int(match.group(3)):04d}-{months[match.group(2)]:02d}-{int(match.group(1)):02d}"

    @staticmethod
    def _canonical_title(value: str, external: str) -> str:
        value = re.sub(r"\b(?:retifica[cç][aã]o|resultado|classifica[cç][aã]o|homologa[cç][aã]o).*", "", value, flags=re.I)
        value = clean_text(value)
        return value if len(value) >= 8 else f"Edital FAPESQ {external}"

    @staticmethod
    def _audience(text: str) -> list[str]:
        folded = slug(text)
        result = []
        for needle, canonical in (
            ("empresa", "empresa"), ("startup", "startup"), ("cooperativ", "cooperativa"),
            ("universidade", "universidade"), ("ict", "ict"), ("institui-cao-cientifica", "ict"),
            ("pesquisador", "pesquisador"), ("pessoa-fisica", "pessoa_fisica"),
        ):
            if needle in folded and canonical not in result:
                result.append(canonical)
        return result or ["pesquisador", "ict", "universidade"]

    @staticmethod
    def _themes(text: str) -> list[str]:
        folded = slug(text)
        dictionary = {
            "celso-furtado": "desenvolvimento_regional", "biotecnolog": "biotecnologia",
            "inovacao": "inovacao", "saude": "saude", "agro": "agroindustria",
            "clima": "clima", "empreendedor": "empreendedorismo", "tecnolog": "tecnologia",
        }
        return [theme for needle, theme in dictionary.items() if needle in folded]

    @staticmethod
    def _instrument(text: str) -> str:
        folded = slug(text)
        if "bolsa" in folded:
            return "bolsa"
        if "premio" in folded:
            return "premio"
        if "subvencao" in folded:
            return "subvencao_economica"
        return "outros"
