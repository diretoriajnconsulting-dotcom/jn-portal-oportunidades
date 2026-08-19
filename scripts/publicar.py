#!/usr/bin/env python3
"""
Transforma achados.json (saída interna do radar) em oportunidades.json — o
CONTRATO PÚBLICO que o site consome.

    python3 publicar.py --entrada saida/achados.json --saida web/oportunidades.json

Por que existe uma camada separada: `achados.json` é estrutura interna e pode
mudar quando o radar mudar. `oportunidades.json` é contrato versionado com o
front. Quebrar um não quebra o outro.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone

VERSAO = "1.0"
URGENTE_DIAS = 15


def ident(o):
    """ID estável entre execuções: permite ao front animar entrada/saída de
    linhas e montar link direto para uma oportunidade."""
    base = "|".join([o.get("NOME_PROGRAMA") or "", o.get("ORGAO") or "",
                     o.get("NATUREZA") or "", o.get("canal") or "",
                     o.get("fecha") or ""])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def defasagem(lms):
    validos = [v for v in (lms or {}).values() if v]
    if not validos:
        return None, None
    try:
        o = max(datetime.strptime(v, "%a, %d %b %Y %H:%M:%S %Z").replace(
            tzinfo=timezone.utc) for v in validos)
    except ValueError:
        return None, None
    return o, (datetime.now(timezone.utc) - o).days


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--entrada", required=True)
    p.add_argument("--saida", required=True)
    a = p.parse_args()

    with open(a.entrada, encoding="utf-8") as f:
        ach = json.load(f)

    novos = {ident(x) for x in ach.get("novos", [])}
    origem_dt, dias = defasagem(ach.get("origem_last_modified"))

    ops = []
    for o in ach.get("abertos", []):
        i = ident(o)
        ops.append({
            "id": i,
            "programa": o.get("NOME_PROGRAMA"),
            "orgao": o.get("ORGAO"),
            "natureza": o.get("NATUREZA"),
            "canal": o.get("canal"),
            "modalidade": o.get("MODALIDADE"),
            "situacao": o.get("SIT_PROGRAMA"),
            "abre": o.get("abre"),
            "fecha": o.get("fecha"),
            "dias_restantes": o.get("dias"),
            "urgente": (o.get("dias") or 999) <= URGENTE_DIAS,
            "nova": i in novos,
            "aderente": bool(o.get("aderente")),
            "temas": o.get("temas") or [],
            "codigos": o.get("codigos") or [],
            "propostas_recebidas": o.get("propostas_no_programa") or 0,
        })
    ops.sort(key=lambda x: (x["dias_restantes"] if x["dias_restantes"] is not None
                            else 9999, x["programa"] or ""))

    recentes = [{
        "data": r.get("DIA_PROPOSTA"),
        "programa": r.get("NOME_PROGRAMA"),
        "proponente": r.get("PROPONENTE"),
        "municipio": r.get("MUNICIPIO"),
        "uf": r.get("UF"),
        "natureza": r.get("NATUREZA"),
        "situacao": r.get("SITUACAO"),
        "valor_global": r.get("VL_GLOBAL_PROP"),
        "objeto": r.get("OBJETO"),
        "mesma_uf": bool(r.get("da_uf")),
    } for r in ach.get("novas_propostas", [])]

    doc = {
        "versao": VERSAO,
        "gerado_em": ach.get("gerado_em"),
        "uf": ach.get("uf"),
        "origem": {
            "repositorio": "https://repositorio.dados.gov.br/seges/detru/",
            "modulo": "Transferências Discricionárias (SICONV)",
            "atualizada_em": origem_dt.isoformat() if origem_dt else None,
            "defasagem_dias": dias,
            # o front DEVE exibir o aviso quando isto for true
            "defasada": bool(dias is not None and dias > 3),
        },
        "resumo": {
            "abertas": len(ops),
            "urgentes": sum(1 for o in ops if o["urgente"]),
            "aderentes": sum(1 for o in ops if o["aderente"]),
            "novas": sum(1 for o in ops if o["nova"]),
            "propostas_recentes": len(recentes),
            "encerradas": len(ach.get("fechados", [])),
        },
        # eixos de filtro, já deduplicados e ordenados por frequência:
        # o front NÃO deve derivar isto por conta própria
        "filtros": {
            "naturezas": [n for n, _ in sorted(
                {o["natureza"]: sum(1 for x in ops if x["natureza"] == o["natureza"])
                 for o in ops}.items(), key=lambda kv: -kv[1]) if n],
            "canais": sorted({o["canal"] for o in ops if o["canal"]}),
            "orgaos": [n for n, _ in sorted(
                {o["orgao"]: sum(1 for x in ops if x["orgao"] == o["orgao"])
                 for o in ops}.items(), key=lambda kv: -kv[1]) if n],
        },
        "oportunidades": ops,
        "propostas_recentes": recentes,
        "encerradas": ach.get("fechados", []),
    }

    with open(a.saida, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"{a.saida}: {len(ops)} oportunidades, {len(recentes)} propostas, "
          f"defasagem {dias} dias")
    return 0


if __name__ == "__main__":
    sys.exit(main())
