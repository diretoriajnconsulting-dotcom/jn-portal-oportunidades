from __future__ import annotations

from typing import Any

from funding_intelligence.adapters.base import BaseAdapter, load_json
from funding_intelligence.models import opportunity


class TransferegovAdapter(BaseAdapter):
    id = "transferegov"
    name = "TransfereGov"
    url = "https://repositorio.dados.gov.br/seges/detru/"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.upstream_stale = False
        self.upstream_lag_days: int | None = None

    def collect(self) -> list[dict[str, Any]]:
        if not self.fixture:
            raise ValueError("TransfereGov requires --input-v1 with the validated v1 catalog")
        payload = load_json(self.fixture)
        self.upstream_stale = bool(payload.get("origem", {}).get("defasada"))
        self.upstream_lag_days = payload.get("origem", {}).get("defasagem_dias")
        found = []
        for item in payload.get("oportunidades", []):
            nature = (item.get("natureza") or "").lower()
            org_types = []
            for needle, canonical in (
                ("munic", "municipio"), ("estad", "estado"), ("consórc", "consorcio_publico"),
                ("consorc", "consorcio_publico"), ("organização", "osc"), ("organizacao", "osc"),
                ("empresa", "empresa"), ("univers", "universidade"),
            ):
                if needle in nature and canonical not in org_types:
                    org_types.append(canonical)
            normalized = opportunity(
                source_id=self.id, source_name=self.name, source_url=self.url,
                checked_at=self.checked_at, external_id=f"{item.get('id')}-{item.get('canal')}",
                title=item.get("programa") or "Programa TransfereGov", funder=item.get("orgao") or "Governo Federal",
                status="open", published=item.get("abre"), deadline=item.get("fecha"),
                organization_types=org_types or ["outros"], geography=[payload.get("uf") or "BR"],
                themes=item.get("temas") or [], instrument_type="convenio", repayable=False,
            )
            normalized["source"]["stale"] = self.upstream_stale
            found.append(normalized)
        return found

    def snapshot(self) -> dict[str, Any]:
        snapshot = super().snapshot()
        if self.upstream_stale:
            snapshot["status"] = "stale"
            snapshot["error"] = f"upstream data is {self.upstream_lag_days} days stale"
        return snapshot
