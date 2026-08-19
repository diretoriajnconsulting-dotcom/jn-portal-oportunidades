from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import urljoin

from funding_intelligence.adapters.base import BaseAdapter
from funding_intelligence.models import clean_text, opportunity, parse_br_date, slug


class FinepAdapter(BaseAdapter):
    id = "finep"
    name = "Finep"
    url = "https://www.finep.gov.br/oportunidades"
    api_url = "https://www.finep.gov.br/o/c/chamadapublicas?sort=dataDePublicacao:desc"
    detail_url = "https://www.finep.gov.br/e/chamada-publica/222684/{id}"

    def collect(self) -> list[dict[str, Any]]:
        if not self.fixture:
            return self._collect_api()
        return self._collect_legacy_html()

    def _token(self) -> str:
        client, secret = self._oauth_credentials()
        credentials = base64.b64encode(f"{client}:{secret}".encode()).decode()
        payload = self.request_bytes(
            "https://www.finep.gov.br/o/oauth2/token", method="POST",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=b"grant_type=client_credentials",
        )
        return json.loads(payload)["access_token"]

    def _oauth_credentials(self) -> tuple[str, str]:
        soup = self.soup(self.fetch_html(self.url))
        script = next((tag.get("src") for tag in soup.select("script[src]")
                       if "finep-busca-chamadas-publicas" in (tag.get("src") or "")), None)
        if not script:
            raise ValueError("Finep public search client script not found")
        javascript = self.fetch_bytes(urljoin(self.url, script)).decode("utf-8", errors="replace")
        return self._extract_oauth_credentials(javascript)

    @staticmethod
    def _extract_oauth_credentials(javascript: str) -> tuple[str, str]:
        match = re.search(
            r'const\s+\w+="([^"]+)",\w+="([^"]+)",\w+=async\(\)=>\{const\s+\w+=btoa',
            javascript,
        )
        if not match:
            raise ValueError("Finep public OAuth client configuration not found")
        return match.group(1), match.group(2)

    def _collect_api(self) -> list[dict[str, Any]]:
        token = self._token()
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            separator = "&" if "?" in self.api_url else "?"
            payload = json.loads(self.request_bytes(
                f"{self.api_url}{separator}page={page}&pageSize=250",
                headers={"Authorization": f"Bearer {token}"},
            ))
            batch = payload.get("items", [])
            items.extend(batch)
            if not batch or len(items) >= payload.get("totalCount", 0):
                break
            page += 1
        return self.parse_api_items(items)

    def parse_api_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found = []
        for item in items:
            deadline = self._iso_date(item.get("prazoProposto"))
            source_status = (item.get("situacao") or {}).get("key", "").lower()
            if source_status != "aberta" or not deadline or deadline < self.today.isoformat():
                continue
            source_url = self.detail_url.format(id=item["id"])
            description_html = item.get("descricao") or ""
            description_soup = self.soup(description_html)
            documents = []
            seen_urls = set()
            for link in description_soup.select("a[href]"):
                href = urljoin(source_url, link.get("href"))
                if href in seen_urls or href.lower().startswith("mailto:"):
                    continue
                seen_urls.add(href)
                documents.append({
                    "title": clean_text(link.get_text(" ", strip=True)) or "Documento da chamada",
                    "url": href,
                    "published": None,
                })
            audience_text = " ".join(
                entry.get("name", "") for entry in item.get("publicoAlvo") or []
            ) + " " + (item.get("descricaoRawText") or "")
            themes = [entry.get("taxonomyCategoryName") for entry in item.get("taxonomyCategoryBriefs") or []]
            themes.extend(re.split(r";|,", item.get("tema") or ""))
            themes.append(item.get("titulo") or "")
            instrument_type, repayable = self._instrument(item, description_soup.get_text(" ", strip=True))
            found.append(opportunity(
                source_id=self.id, source_name=self.name, source_url=source_url,
                checked_at=self.checked_at, external_id=str(item["id"]),
                title=item.get("titulo") or f"Chamada Finep {item['id']}",
                funder="Finep / FNDCT", status="open",
                published=self._iso_date(item.get("dataDePublicacao")), deadline=deadline,
                organization_types=self._audience(audience_text),
                geography=self._geography((item.get("regiao") or {}).get("name")),
                themes=[theme for theme in themes if clean_text(theme)],
                instrument_type=instrument_type, repayable=repayable,
                program_budget=self._budget(item.get("descricaoRawText") or ""),
                documents=documents, description=item.get("descricaoRawText"),
            ))
        return found

    def _collect_legacy_html(self) -> list[dict[str, Any]]:
        soup = self.soup(self.fetch_html())
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for heading in soup.select("h2, h3, h4"):
            link = heading.find("a", href=re.compile(r"chamadapublica/\d+"))
            if not link:
                continue
            title = clean_text(link.get_text(" ", strip=True))
            detail_url = urljoin(self.url, link.get("href"))
            external_match = re.search(r"chamadapublica/(\d+)", detail_url)
            if not title or not external_match or external_match.group(1) in seen:
                continue
            seen.add(external_match.group(1))
            block_parts: list[str] = []
            for sibling in heading.next_siblings:
                sibling_name = getattr(sibling, "name", None)
                if sibling_name in {"h2", "h3", "h4"}:
                    break
                text = clean_text(sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling))
                if text:
                    block_parts.append(text)
            block = " ".join(block_parts)
            published = self._label_date(block, "Data de publicação")
            deadline = self._label_date(block, "Prazo para envio de propostas até")
            audience = self._label(block, r"P[uú]blico-alvo")
            themes = self._split(self._label(block, r"Tema(?:\(s\))?"))
            funder = self._label(block, "Fonte de Recurso") or "Finep / FNDCT"
            status = "open" if deadline and deadline >= self.today.isoformat() else "unknown"
            found.append(opportunity(
                source_id=self.id, source_name=self.name, source_url=detail_url,
                checked_at=self.checked_at, external_id=external_match.group(1),
                title=title, funder=funder, status=status, published=published,
                deadline=deadline, organization_types=self._audience(audience),
                geography=["BR"], themes=themes, instrument_type="subvencao_economica",
                repayable=False,
            ))
        return found

    @staticmethod
    def _iso_date(value: str | None) -> str | None:
        return value[:10] if value and re.match(r"\d{4}-\d{2}-\d{2}", value) else None

    @staticmethod
    def _geography(value: str | None) -> list[str]:
        folded = slug(value)
        if "paraiba" in folded:
            return ["PB", "BR"]
        if "nordeste" in folded:
            return ["NE", "BR"]
        return ["BR"]

    @staticmethod
    def _instrument(item: dict[str, Any], description: str) -> tuple[str, bool | None]:
        key = slug((item.get("tipoDeOportunidade") or {}).get("key"))
        text = slug(description)
        if "subvencao" in text:
            return "subvencao_economica", False
        if "credito" in key or "reembolsavel" in key and "naoreembolsavel" not in key:
            return "credito", True
        if "invest" in key:
            return "investimento", None
        if "premi" in key:
            return "premio", False
        if "naoreembolsavel" in key:
            return "contratacao_pdi", False
        return "outros", None

    @staticmethod
    def _budget(text: str) -> float | None:
        match = re.search(r"(?:or[cç]amento|recursos?)[^R$]{0,80}R\$\s*([\d.,]+)\s*(milh(?:[aã]o|[oõ]es)|bilh(?:[aã]o|[oõ]es)|mil)?", text, re.I)
        if not match:
            return None
        number = float(match.group(1).replace(".", "").replace(",", "."))
        unit = slug(match.group(2))
        if unit.startswith("bilh"):
            number *= 1_000_000_000
        elif unit.startswith("milh"):
            number *= 1_000_000
        elif unit == "mil":
            number *= 1_000
        return number

    @staticmethod
    def _label(block: str, label: str) -> str | None:
        labels = r"Data de publica[cç][aã]o|Prazo para envio de propostas at[eé]|Fonte de Recurso|P[uú]blico-alvo|Tema(?:\(s\))?|Situa[cç][aã]o"
        match = re.search(rf"{label}\s*:?\s*(.+?)(?=\s+(?:{labels})\s*:|$)", block, re.I)
        return clean_text(match.group(1)) if match else None

    def _label_date(self, block: str, label: str) -> str | None:
        return parse_br_date(self._label(block, label))

    @staticmethod
    def _split(value: str | None) -> list[str]:
        return [slug(x).replace("-", "_") for x in re.split(r";|,", value or "") if clean_text(x)]

    @staticmethod
    def _audience(value: str | None) -> list[str]:
        folded = slug(value)
        result = []
        for needle, canonical in (
            ("startup", "startup"), ("empresa", "empresa"), ("cooperativ", "cooperativa"),
            ("ict", "ict"), ("universidade", "universidade"), ("instituicoes-de-pesquisa", "ict"),
        ):
            if needle in folded and canonical not in result:
                result.append(canonical)
        return result or ["outros"]
