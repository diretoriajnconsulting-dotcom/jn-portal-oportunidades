from __future__ import annotations

import io
import json
import urllib.request
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pypdf import PdfReader

from funding_intelligence.models import utc_now


USER_AGENT = "PONTE-Funding-Intelligence/2.0 (+https://github.com/diretoriajnconsulting-dotcom/jn-portal-oportunidades)"


class BaseAdapter(ABC):
    id: str
    name: str
    url: str

    def __init__(self, *, fixture: str | None = None, today: date | None = None):
        self.fixture = fixture
        self.today = today or date.today()
        self.checked_at = utc_now()

    def request_bytes(
        self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> bytes:
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        request = urllib.request.Request(url, headers=request_headers, data=data, method=method)
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read()

    def fetch_bytes(self, url: str) -> bytes:
        return self.request_bytes(url)

    def fetch_html(self, url: str | None = None) -> str:
        if self.fixture:
            return Path(self.fixture).read_text(encoding="utf-8")
        return self.fetch_bytes(url or self.url).decode("utf-8", errors="replace")

    def soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def pdf_text(self, url: str) -> str:
        reader = PdfReader(io.BytesIO(self.fetch_bytes(urljoin(self.url, url))))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    @abstractmethod
    def collect(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def snapshot(self) -> dict[str, Any]:
        opportunities = self.collect()
        return {
            "source": {"id": self.id, "name": self.name, "url": self.url},
            "checked_at": self.checked_at,
            "status": "healthy",
            "error": None,
            "opportunities": opportunities,
        }


def load_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))
