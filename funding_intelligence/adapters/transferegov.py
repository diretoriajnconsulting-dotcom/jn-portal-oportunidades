from __future__ import annotations

import hashlib
from typing import Any

from funding_intelligence.adapters.base import BaseAdapter, load_json
from funding_intelligence.models import opportunity


def identidade_janela(item: dict[str, Any]) -> str:
    """Identidade de uma janela do TransfereGov, estável entre prorrogações.

    NÃO reaproveita o `id` do catálogo v1.1. Aquele id é
    ``sha1(programa | órgão | natureza | canal | fecha)`` (``scripts/publicar.py``),
    e o ``fecha`` dentro do hash faz toda prorrogação trocar o id. No v2 isso
    virava, para 87% do catálogo, uma janela "encerrada" mais uma "nova" onde o
    que houve foi prazo estendido — e deixava este radar cego às próprias
    prorrogações: das alterações de prazo registradas em ``changes[]`` no
    catálogo de 10/09/2026, nenhuma era do TransfereGov.

    ``canal | natureza | códigos`` é a identidade que o Mapa de Oportunidades
    (etl-emendas/web/src/lib/oportunidades/diff.ts) já usa para os avisos:
    zero colisões em 12 publicações entre 19/08 e 11/09/2026, e zero no
    catálogo de 12/09/2026. Os códigos são ordenados porque a posição no array
    não carrega significado.
    """
    codigos = ",".join(sorted(str(c) for c in (item.get("codigos") or [])))
    base = "|".join([item.get("canal") or "", item.get("natureza") or "", codigos])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


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
        vistas: dict[str, str] = {}
        for item in payload.get("oportunidades", []):
            ident = identidade_janela(item)
            # Colisão aqui significa que a identidade deixou de ser única — o
            # contrato v1.1 mudou. `pipeline.deduplicate` descarta em silêncio o
            # item repetido, então seguir em frente faria uma janela de
            # financiamento sumir do catálogo sem ninguém saber. Falhar alto é
            # o que o diff do site também faz no mesmo caso.
            if ident in vistas:
                raise ValueError(
                    f"identidade de janela duplicada no TransfereGov: {ident} "
                    f"({vistas[ident]!r} e {item.get('programa')!r})"
                )
            vistas[ident] = item.get("programa") or ""
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
                checked_at=self.checked_at, external_id=f"{ident}-{item.get('canal')}",
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
