#!/usr/bin/env python3
"""
Radar de oportunidades — Transferegov / Transferências Discricionárias.

Baixa três arquivos do repositório de dados abertos, compara com o estado da
execução anterior e produz:

    painel.html   painel de oportunidades abertas, com filtros interativos
    achados.json  achados estruturados, para redigir a notificação
    estado.json   estado desta execução, insumo da próxima

Uso:
    python3 radar.py --uf PB
    python3 radar.py --uf PB --estado estado-anterior.json
    python3 radar.py --uf PB --saida ./out --temas "fundiári,habitaç,cultura"

Requer: duckdb. Rede só para o repositório oficial.
"""

import argparse
import html
import json
import os
import shutil
import ssl
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timezone

import duckdb

REPO = "https://repositorio.dados.gov.br/seges/detru"
BASE_PORTAL = "https://portal.transferegov.sistema.gov.br"
ARQUIVOS = ["siconv_programa", "siconv_programa_proposta", "siconv_proposta"]
UA = {"User-Agent": "ponte-radar-oportunidades/1.0"}

# O repositório de origem muda de rota sem aviso. Tentamos as variantes
# conhecidas em ordem, da mais provável para a menos.
def rotas_possiveis(nome):
    return [
        f"{REPO}/{nome}.csv.zip",
        f"{REPO}/{nome}.zip",
        f"{BASE_PORTAL}/downloads/{nome}.zip",
        f"{BASE_PORTAL}/downloads/arquivos/{nome}.zip",
        f"{BASE_PORTAL}/arquivos/{nome}.zip",
        f"{BASE_PORTAL}/dados/{nome}.zip",
    ]

SSL_CTX = ssl.create_default_context()

# Temas da carteira da Ponte. Casam por trecho, sem acento obrigatório —
# a comparação é feita em minúsculas contra nome do programa e órgão.
TEMAS_PADRAO = [
    "fundiári", "fundiari", "regulariza", "habitaç", "habitac", "moradia",
    "athis", "urbaniz", "assistência social", "assistencia social",
    "socioassist", "suas", "cultura", "patrimônio", "patrimonio",
    "inovaç", "inovac", "tecnologia", "saneamento", "turismo",
    "desenvolvimento regional", "economia solidária", "economia solidaria",
]

NATUREZAS = [
    "Administração Pública Municipal",
    "Administração Pública Estadual ou do Distrito Federal",
    "Organização da Sociedade Civil",
    "Consórcio Público",
    "Empresa pública/Sociedade de economia mista",
]


def log(m):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def baixar(nome, cache):
    """
    Baixa e extrai um arquivo da origem, tentando as rotas conhecidas em ordem.

    Devolve (caminho_do_csv, last_modified). Levanta RuntimeError se nenhuma
    rota funcionar — o job deve falhar alto, não publicar dado velho em silêncio.

    O Last-Modified é o que alimenta `origem.defasada` no contrato: sem ele o
    painel não consegue distinguir "nada aconteceu" de "ninguém coletou".
    """
    ultimo_erro = None
    for url in rotas_possiveis(nome):
        alvo = os.path.join(cache, os.path.basename(urllib.parse.urlparse(url).path))
        try:
            req = urllib.request.Request(url, method="HEAD", headers=UA)
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=90) as r:
                lm = r.headers.get("Last-Modified")
                tam = int(r.headers.get("Content-Length") or 0)

            if not os.path.exists(alvo) or os.path.getsize(alvo) != tam:
                log(f"  baixando {nome} de {url} ({tam/1e6:.0f} MB)")
                with urllib.request.urlopen(
                        urllib.request.Request(url, headers=UA),
                        context=SSL_CTX, timeout=1800) as r, \
                        open(alvo + ".tmp", "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                os.replace(alvo + ".tmp", alvo)
            else:
                log(f"  {nome} já em cache")

            if os.path.getsize(alvo) < 1000:
                os.remove(alvo)
                raise RuntimeError("arquivo suspeito de tão pequeno")

            with zipfile.ZipFile(alvo) as zf:
                interno = zf.namelist()[0]
                zf.extract(interno, cache)
            return os.path.join(cache, interno), lm

        except Exception as ex:
            ultimo_erro = ex
            log(f"  rota falhou ({url}): {ex}")
            continue

    raise RuntimeError(f"nenhuma rota funcionou para {nome}: {ultimo_erro}")


def carregar(con, nome, caminho, sel, where=""):
    con.execute(f"""
        CREATE OR REPLACE TABLE {nome} AS SELECT {sel}
        FROM read_csv('{caminho}', delim=';', header=true, all_varchar=true,
                      encoding='utf-8', ignore_errors=true) {where}""")
    return con.execute(f"SELECT count(*) FROM {nome}").fetchone()[0]


D = "TRY_CAST(TRY_STRPTIME(NULLIF(TRIM(\"{0}\"),''), '%d/%m/%Y') AS DATE) AS \"{0}\""
M = "TRY_CAST(REPLACE(NULLIF(TRIM(\"{0}\"),''), ',', '.') AS DOUBLE) AS \"{0}\""
I = "TRY_CAST(NULLIF(TRIM(\"{0}\"),'') AS BIGINT) AS \"{0}\""


def coletar(uf, cache):
    os.makedirs(cache, exist_ok=True)
    con = duckdb.connect(":memory:")
    lms = {}
    caminhos = {}
    for a in ARQUIVOS:
        caminhos[a], lms[a] = baixar(a, cache)

    log("  carregando programa")
    carregar(con, "programa", caminhos["siconv_programa"], f"""
        {I.format('ID_PROGRAMA')}, TRIM(COD_PROGRAMA) AS COD_PROGRAMA,
        TRIM(NOME_PROGRAMA) AS NOME_PROGRAMA,
        TRIM(SIT_PROGRAMA) AS SIT_PROGRAMA,
        TRIM(DESC_ORGAO_SUP_PROGRAMA) AS ORGAO,
        TRIM(MODALIDADE_PROGRAMA) AS MODALIDADE,
        TRIM(NATUREZA_JURIDICA_PROGRAMA) AS NATUREZA,
        TRIM(UF_PROGRAMA) AS UF,
        {D.format('DT_PROG_INI_RECEB_PROP')}, {D.format('DT_PROG_FIM_RECEB_PROP')},
        {D.format('DT_PROG_INI_EMENDA_PAR')}, {D.format('DT_PROG_FIM_EMENDA_PAR')},
        {D.format('DATA_DISPONIBILIZACAO')}
    """, f"WHERE UPPER(TRIM(UF_PROGRAMA)) = '{uf.upper()}'")

    log("  carregando programa_proposta")
    carregar(con, "prog_prop", caminhos["siconv_programa_proposta"],
             f"{I.format('ID_PROGRAMA')}, {I.format('ID_PROPOSTA')}")

    log("  carregando proposta (nacional, para medir concorrência)")
    carregar(con, "proposta", caminhos["siconv_proposta"], f"""
        {I.format('ID_PROPOSTA')}, TRIM(UF_PROPONENTE) AS UF,
        TRIM(MUNIC_PROPONENTE) AS MUNICIPIO,
        TRIM(NM_PROPONENTE) AS PROPONENTE,
        TRIM(NATUREZA_JURIDICA) AS NATUREZA,
        TRIM(SIT_PROPOSTA) AS SITUACAO,
        TRIM(OBJETO_PROPOSTA) AS OBJETO,
        {D.format('DIA_PROPOSTA')}, {M.format('VL_GLOBAL_PROP')}
    """)

    for c in caminhos.values():
        if os.path.exists(c):
            os.remove(c)
    return con, lms


# Um mesmo programa aparece com vários COD_PROGRAMA — o SICONV emite um código
# por ação orçamentária. Agrupar por nome+órgão+canal+prazo é o que torna o
# painel legível; os códigos ficam agregados na coluna `codigos`.
SQL_ABERTOS = """
WITH j AS (
  SELECT COD_PROGRAMA, NOME_PROGRAMA, ORGAO, MODALIDADE, NATUREZA,
         SIT_PROGRAMA, 'proposta' AS canal,
         DT_PROG_INI_RECEB_PROP AS abre, DT_PROG_FIM_RECEB_PROP AS fecha
  FROM programa
  WHERE SIT_PROGRAMA IN ('DISPONIBILIZADO','CADASTRADO')
    AND DT_PROG_FIM_RECEB_PROP >= current_date
  UNION ALL
  SELECT COD_PROGRAMA, NOME_PROGRAMA, ORGAO, MODALIDADE, NATUREZA,
         SIT_PROGRAMA, 'emenda',
         DT_PROG_INI_EMENDA_PAR, DT_PROG_FIM_EMENDA_PAR
  FROM programa
  WHERE SIT_PROGRAMA IN ('DISPONIBILIZADO','CADASTRADO')
    AND DT_PROG_FIM_EMENDA_PAR >= current_date
)
SELECT NOME_PROGRAMA, ORGAO, NATUREZA, canal, fecha,
       CAST(fecha - current_date AS INTEGER) AS dias,
       min(abre) AS abre,
       count(DISTINCT COD_PROGRAMA) AS n_codigos,
       list_sort(array_agg(DISTINCT COD_PROGRAMA)) AS codigos,
       string_agg(DISTINCT MODALIDADE, ' / ') AS MODALIDADE,
       string_agg(DISTINCT SIT_PROGRAMA, ' / ') AS SIT_PROGRAMA
FROM j GROUP BY NOME_PROGRAMA, ORGAO, NATUREZA, canal, fecha
ORDER BY dias, NOME_PROGRAMA
"""


def analisar(con, uf, temas, estado_ant):
    cur = con.execute(SQL_ABERTOS)
    cols = [d[0] for d in cur.description]
    abertos = [dict(zip(cols, r)) for r in cur.fetchall()]

    # concorrência: quantas propostas já entraram em cada código de programa
    conc = dict(con.execute("""
        SELECT p.COD_PROGRAMA, count(DISTINCT pp.ID_PROPOSTA)
        FROM programa p JOIN prog_prop pp ON pp.ID_PROGRAMA = p.ID_PROGRAMA
        GROUP BY 1""").fetchall())

    codigos_abertos = set()
    for a in abertos:
        a["codigos"] = list(a["codigos"])
        codigos_abertos.update(a["codigos"])
        a["propostas_no_programa"] = sum(conc.get(c, 0) for c in a["codigos"])
        # Casa SÓ contra o nome do programa. Incluir o órgão marcava como
        # aderente todo programa do "Ministério da Integração e do
        # Desenvolvimento Regional" — 23 falsos positivos na validação.
        alvo = (a["NOME_PROGRAMA"] or "").lower()
        a["temas"] = sorted({t for t in temas if t in alvo})
        a["aderente"] = bool(a["temas"])
        for k in ("abre", "fecha"):
            if isinstance(a[k], date):
                a[k] = a[k].isoformat()

    vistos = set(estado_ant.get("programas_abertos", []))
    novos = ([a for a in abertos if not set(a["codigos"]) & vistos]
             if vistos else [])
    fechados = sorted(vistos - codigos_abertos) if vistos else []

    corte = estado_ant.get("executado_em", "")[:10]
    novas_propostas = []
    if corte and codigos_abertos:
        lista = "','".join(c.replace("'", "") for c in codigos_abertos)
        cur = con.execute(f"""
            SELECT pr.DIA_PROPOSTA, p.COD_PROGRAMA, p.NOME_PROGRAMA,
                   pr.UF, pr.MUNICIPIO, pr.PROPONENTE, pr.NATUREZA,
                   pr.SITUACAO, pr.VL_GLOBAL_PROP, left(pr.OBJETO, 120) AS OBJETO
            FROM proposta pr
            JOIN prog_prop pp ON pp.ID_PROPOSTA = pr.ID_PROPOSTA
            JOIN programa p ON p.ID_PROGRAMA = pp.ID_PROGRAMA
            WHERE p.COD_PROGRAMA IN ('{lista}')
              AND pr.DIA_PROPOSTA >= CAST('{corte}' AS DATE)
            ORDER BY pr.DIA_PROPOSTA DESC, pr.VL_GLOBAL_PROP DESC
            LIMIT 400""")
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["DIA_PROPOSTA"] = (d["DIA_PROPOSTA"].isoformat()
                                 if isinstance(d["DIA_PROPOSTA"], date) else None)
            d["da_uf"] = (d["UF"] or "").upper() == uf.upper()
            novas_propostas.append(d)

    return {
        "uf": uf,
        "abertos": abertos,
        "novos": novos,
        "fechados": fechados,
        "novas_propostas": novas_propostas,
        "urgentes": [a for a in abertos if a["dias"] <= 15],
        "aderentes": [a for a in abertos if a["aderente"]],
    }


# ------------------------------------------------------------------ painel

CSS = """
:root{--bg:#FCFCFB;--wash:#F9F9F7;--ink:#2E2C27;--soft:#6B6A63;--grey:#B4B3A8;
--hair:#E4E3DC;--clay:#C6613F;--verde:#5C7A5C;}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg)}
body{font-family:-apple-system,"Segoe UI",sans-serif;color:var(--soft);line-height:1.55}
.topo{background:var(--wash);border-bottom:1px solid #E1E1DF}
.wrap{max-width:1080px;margin:0 auto;padding:44px 30px 36px}
.sup{font-size:12.5px;letter-spacing:.03em;margin:0 0 12px}
h1{font-family:Georgia,serif;font-weight:600;font-size:34px;line-height:1.2;
color:var(--ink);margin:0 0 8px;letter-spacing:-.01em}
.sub{font-size:15px;margin:0;max-width:640px}
.kpis{display:flex;gap:40px;margin-top:28px;flex-wrap:wrap}
.kpi .n{font-family:Georgia,serif;font-size:32px;color:var(--ink);line-height:1}
.kpi .l{font-size:12px;letter-spacing:.05em;text-transform:uppercase;margin-top:5px}
.kpi.alerta .n{color:var(--clay)}
h2{font-size:12.5px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;
color:var(--ink);margin:0 0 16px}
section{margin-top:44px}
.filtros{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.chip{font:inherit;font-size:13px;padding:6px 13px;border:1px solid var(--hair);
background:none;color:var(--soft);border-radius:15px;cursor:pointer}
.chip[aria-pressed="true"]{background:var(--ink);color:#FCFCFB;border-color:var(--ink)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-size:11px;letter-spacing:.07em;text-transform:uppercase;
color:var(--grey);font-weight:600;padding:0 10px 8px 0;border-bottom:1px solid var(--hair)}
td{padding:11px 10px 11px 0;border-bottom:1px solid var(--hair);vertical-align:top}
td.prog{color:var(--ink);font-weight:600;max-width:330px}
.tag{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--grey)}
.dias{font-variant-numeric:tabular-nums;white-space:nowrap}
.urg{color:var(--clay);font-weight:600}
.ader{color:var(--verde);font-weight:600}
.vazio{font-size:14px;padding:14px 0}
.nota{font-size:13px;border-left:2px solid var(--clay);padding:10px 0 10px 14px;
margin-top:34px}
@media(max-width:760px){.wrap{padding:32px 18px}h1{font-size:26px}
table,thead,tbody,tr,td,th{display:block}thead{display:none}
td{border:none;padding:2px 0}tr{border-bottom:1px solid var(--hair);padding:12px 0;display:block}}
"""

JS = """
const chips=[...document.querySelectorAll('.chip')];
function aplica(){
 const on=chips.filter(c=>c.getAttribute('aria-pressed')==='true').map(c=>c.dataset.nat);
 document.querySelectorAll('tr[data-nat]').forEach(tr=>{
   tr.style.display=(!on.length||on.includes(tr.dataset.nat))?'':'none';});
 document.querySelectorAll('table').forEach(t=>{
   const vis=[...t.querySelectorAll('tr[data-nat]')].filter(r=>r.style.display!=='none').length;
   const v=t.parentElement.querySelector('.semfiltro');
   if(v)v.style.display=vis?'none':'block';});
}
chips.forEach(c=>c.addEventListener('click',()=>{
 c.setAttribute('aria-pressed',c.getAttribute('aria-pressed')==='true'?'false':'true');
 aplica();}));
"""


def e(s):
    return html.escape(str(s if s is not None else "—"))


def brl(v):
    if v is None:
        return "—"
    return "R$ " + f"{v:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def dbr(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return "—"


def tabela_programas(itens, vazio="Nada aqui."):
    if not itens:
        return f'<p class="vazio">{vazio}</p>'
    L = ['<table><thead><tr><th>Programa</th><th>Órgão</th><th>Quem pode</th>'
         '<th>Canal</th><th>Fecha</th><th>Propostas</th></tr></thead><tbody>']
    for a in itens:
        urg = "urg" if a["dias"] <= 15 else ""
        cods = (e(a["codigos"][0]) if a["n_codigos"] == 1
                else f'{a["n_codigos"]} códigos')
        temas = (f'<div class="tag ader">aderente · {e(", ".join(a["temas"]))}</div>'
                 if a["aderente"] else "")
        L.append(
            f'<tr data-nat="{e(a["NATUREZA"])}">'
            f'<td class="prog">{e(a["NOME_PROGRAMA"])}'
            f'<div class="tag">{cods} · {e(a["MODALIDADE"])}'
            f' · {e(a["SIT_PROGRAMA"])}</div>{temas}</td>'
            f'<td>{e(a["ORGAO"])}</td>'
            f'<td>{e(a["NATUREZA"])}</td>'
            f'<td>{e(a["canal"])}</td>'
            f'<td class="dias {urg}">{dbr(a["fecha"])}<br>'
            f'<span class="tag">{a["dias"]} dias</span></td>'
            f'<td class="dias">{a["propostas_no_programa"]:,}</td></tr>')
    L.append('</tbody></table><p class="vazio semfiltro" style="display:none">'
             'Nenhum programa neste filtro.</p>')
    return "".join(L)


def tabela_propostas(itens):
    if not itens:
        return '<p class="vazio">Nenhuma proposta nova em programa aberto.</p>'
    L = ['<table><thead><tr><th>Data</th><th>Proponente</th><th>Programa</th>'
         '<th>Valor</th><th>Situação</th></tr></thead><tbody>']
    for p in itens[:60]:
        marca = ' <span class="tag ader">na sua UF</span>' if p["da_uf"] else ""
        L.append(
            f'<tr data-nat="{e(p["NATUREZA"])}">'
            f'<td class="dias">{dbr(p["DIA_PROPOSTA"])}</td>'
            f'<td class="prog">{e(p["PROPONENTE"])}{marca}'
            f'<div class="tag">{e(p["MUNICIPIO"])}/{e(p["UF"])}</div></td>'
            f'<td>{e(p["NOME_PROGRAMA"])[:80]}</td>'
            f'<td class="dias">{brl(p["VL_GLOBAL_PROP"])}</td>'
            f'<td>{e(p["SITUACAO"])}</td></tr>')
    L.append('</tbody></table><p class="vazio semfiltro" style="display:none">'
             'Nenhuma proposta neste filtro.</p>')
    return "".join(L)


def montar_painel(a, lms, gerado_em):
    dias_def = None
    lm = max([v for v in lms.values() if v], default=None)
    if lm:
        try:
            o = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z").replace(
                tzinfo=timezone.utc)
            dias_def = (datetime.now(timezone.utc) - o).days
            origem = o.strftime("%d/%m/%Y")
        except ValueError:
            origem = lm
    else:
        origem = "?"

    chips = "".join(
        f'<button class="chip" data-nat="{e(n)}" aria-pressed="false">{e(n)}</button>'
        for n in NATUREZAS)

    nota = ""
    if dias_def is not None and dias_def > 3:
        nota = (f'<p class="nota"><strong>A origem está defasada.</strong> '
                f'O repositório promete extração diária, mas os arquivos foram '
                f'atualizados pela última vez em {origem} — {dias_def} dias atrás. '
                f'Ausência de novidade abaixo pode ser pipeline parado na origem, '
                f'não ausência de fato.</p>')

    return f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radar de oportunidades — {e(a['uf'])} — {gerado_em:%d/%m/%Y}</title>
<style>{CSS}</style></head><body>
<div class="topo"><div class="wrap">
<p class="sup">Transferegov · Transferências Discricionárias · {e(a['uf'])} ·
{gerado_em:%d/%m/%Y às %H:%M}</p>
<h1>{len(a['abertos'])} janelas abertas, {len(a['urgentes'])} fechando em duas semanas.</h1>
<p class="sub">Programas que ainda aceitam entrada, por canal de proposta e de
emenda parlamentar. Use os filtros para ver só o que cabe no seu tipo de proponente.</p>
<div class="kpis">
<div class="kpi"><div class="n">{len(a['abertos'])}</div><div class="l">janelas abertas</div></div>
<div class="kpi {'alerta' if a['urgentes'] else ''}"><div class="n">{len(a['urgentes'])}</div><div class="l">fecham em 15 dias</div></div>
<div class="kpi"><div class="n">{len(a['aderentes'])}</div><div class="l">aderentes à carteira</div></div>
<div class="kpi"><div class="n">{len(a['novos'])}</div><div class="l">novos desde a última</div></div>
<div class="kpi"><div class="n">{len(a['novas_propostas'])}</div><div class="l">propostas novas</div></div>
</div></div></div>
<div class="wrap">
<div class="filtros">{chips}</div>

<section><h2>Fecham em até 15 dias</h2>{tabela_programas(a['urgentes'],
 'Nenhuma janela fecha nas próximas duas semanas.')}</section>

<section><h2>Novos desde a última execução</h2>{tabela_programas(a['novos'],
 'Nenhum programa novo. Se esta é a primeira execução, é o esperado: não há '
 'com o que comparar.')}</section>

<section><h2>Aderentes à carteira</h2>{tabela_programas(a['aderentes'],
 'Nenhum programa aberto casou com os temas configurados.')}</section>

<section><h2>Todas as janelas abertas</h2>{tabela_programas(a['abertos'])}</section>

<section><h2>Propostas novas em programas ainda abertos</h2>
<p class="sub" style="margin-bottom:16px">Quem já entrou — leitura de
concorrência e de que o programa está de fato recebendo.</p>
{tabela_propostas(a['novas_propostas'])}</section>

{f'<section><h2>Janelas que fecharam desde a última execução</h2><p class="vazio">{e(", ".join(a["fechados"][:40]))}</p></section>' if a['fechados'] else ''}

{nota}
</div><script>{JS}</script></body></html>"""


# ------------------------------------------------------------------ main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--uf", default="PB")
    p.add_argument("--estado", help="estado.json da execução anterior")
    p.add_argument("--saida", default=".")
    p.add_argument("--cache", default=".cache_radar")
    p.add_argument("--temas", help="lista separada por vírgula; vazio usa o padrão")
    a = p.parse_args()

    temas = ([t.strip().lower() for t in a.temas.split(",") if t.strip()]
             if a.temas else TEMAS_PADRAO)
    estado_ant = {}
    if a.estado and os.path.exists(a.estado):
        with open(a.estado, encoding="utf-8") as f:
            estado_ant = json.load(f)
        log(f"estado anterior: {estado_ant.get('executado_em','?')}")
    else:
        log("sem estado anterior — esta é uma execução de referência")

    os.makedirs(a.saida, exist_ok=True)
    inicio = time.time()
    try:
        con, lms = coletar(a.uf, a.cache)
    except Exception as ex:
        log(f"FALHA ao coletar: {ex}")
        return 2

    ach = analisar(con, a.uf, temas, estado_ant)
    agora = datetime.now()

    with open(os.path.join(a.saida, "painel.html"), "w", encoding="utf-8") as f:
        f.write(montar_painel(ach, lms, agora))
    with open(os.path.join(a.saida, "achados.json"), "w", encoding="utf-8") as f:
        json.dump({**ach, "origem_last_modified": lms,
                   "gerado_em": agora.isoformat()}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.saida, "estado.json"), "w", encoding="utf-8") as f:
        json.dump({"executado_em": agora.isoformat(), "uf": a.uf,
                   "programas_abertos": sorted({c for x in ach["abertos"]
                                                for c in x["codigos"]}),
                   "origem_last_modified": lms},
                  f, ensure_ascii=False, indent=1)

    log(f"{len(ach['abertos'])} abertas · {len(ach['urgentes'])} urgentes · "
        f"{len(ach['novos'])} novas · {len(ach['aderentes'])} aderentes · "
        f"{len(ach['novas_propostas'])} propostas novas  ({time.time()-inicio:.0f}s)")
    log(f"painel: {os.path.join(a.saida,'painel.html')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
