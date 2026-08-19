#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radar de Oportunidades TransfereGov — Ponte Estruturação de Projetos
Download resiliente e processamento analítico com DuckDB para a Paraíba (PB).
"""

import os
import re
import sys
import ssl
import json
import zipfile
import shutil
import urllib.request
import urllib.parse
from datetime import datetime, date
import duckdb

BASE_PORTAL = "https://api-publica.transferegov.gestao.gov.br"
DOWNLOADS_PAGE = f"{BASE_PORTAL}/downloads"
DATA_DIR = "dados_siconv"
OUTPUT_DIR = "public/data"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Contexto SSL flexível para servidores governamentais
SSL_CTX = ssl._create_unverified_context()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

KEYWORDS_CARTEIRA = {
    "Regularização Fundiária": [
        "regularização fundiária", "reurb", "fundiária", "matrícula",
        "cartório", "assentamento urbano", "núcleo urbano informal"
    ],
    "ATHIS / Habitação": [
        "athis", "habitação", "moradia", "interesse social",
        "minha casa", "melhorias habitacionais", "assistência técnica"
    ],
    "Socioassistencial (MROSC)": [
        "assistência social", "socioassistencial", "mrosc", "cras", "creas",
        "vulnerabilidade", "idoso", "criança", "adolescente",
        "segurança alimentar", "cozinha comunitária"
    ],
    "Cultura": [
        "cultura", "patrimônio", "preservação", "acervo",
        "audiovisual", "artes", "museu", "memória", "aldir blanc", "pnab"
    ],
    "Inovação": [
        "inovação", "tecnologia", "transformação digital", "p&d", "ict",
        "pesquisa", "startup", "hub", "ciência", "digital"
    ]
}

def categorizar_programa(nome_prog: str) -> str:
    texto = (nome_prog or "").lower()
    for cat, kws in KEYWORDS_CARTEIRA.items():
        if any(kw in texto for kw in kws):
            return cat
    return "Multisetorial / Geral"

def obter_links_da_pagina():
    """Varre a página de downloads para descobrir os links reais dos arquivos."""
    links_descobertos = []
    try:
        req = urllib.request.Request(DOWNLOADS_PAGE, headers=HEADERS)
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            # Busca todos os links href apontando para zips ou arquivos
            matches = re.findall(r'href=[\'\"]([^\'\"]+)[\'\"]', html)
            for m in matches:
                url_completa = urllib.parse.urljoin(BASE_PORTAL, m)
                links_descobertos.append(url_completa)
    except Exception as e:
        print(f"Aviso ao consultar página de downloads: {e}", flush=True)
    return links_descobertos

def baixar_e_extrair(base_name: str):
    # 1. Busca links na página oficial
    links_pagina = obter_links_da_pagina()
    links_especificos = [l for l in links_pagina if base_name in l]

    # 2. Lista de URLs candidatas (descobertas + rotas padrão + fallback)
    candidatos = links_especificos + [
        f"{BASE_PORTAL}/downloads/{base_name}.zip",
        f"{BASE_PORTAL}/downloads/arquivos/{base_name}.zip",
        f"{BASE_PORTAL}/arquivos/{base_name}.zip",
        f"{BASE_PORTAL}/dados/{base_name}.zip",
        f"https://repositorio.dados.gov.br/seges/detru/{base_name}.csv.zip",
        f"https://repositorio.dados.gov.br/seges/detru/{base_name}.zip"
    ]

    sucesso = False
    for url in candidatos:
        filename = os.path.basename(urllib.parse.urlparse(url).path) or f"{base_name}.zip"
        zip_dest = os.path.join(DATA_DIR, filename)
        print(f"-> Tentando download de {url}...", flush=True)

        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=120) as response, open(zip_dest, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)

            tamanho = os.path.getsize(zip_dest)
            if tamanho > 1000: # Verifica se não foi baixada página de erro HTML
                print(f"✓ Download concluído: {filename} ({tamanho} bytes)", flush=True)
                with zipfile.ZipFile(zip_dest, 'r') as zip_ref:
                    zip_ref.extractall(DATA_DIR)
                print(f"✓ Arquivos extraídos com sucesso em {DATA_DIR}", flush=True)
                sucesso = True
                break
            else:
                os.remove(zip_dest)
        except Exception as e:
            print(f"Aviso: Falha em {url}: {e}", flush=True)

    if not sucesso:
        print(f"Atenção: Não foi possível obter {base_name} automaticamente.", flush=True)

def processar_radar(uf="PB"):
    hoje = datetime.now().date()
    hoje_str = hoje.strftime("%Y-%m-%d")

    csv_candidates = [
        os.path.join(DATA_DIR, "siconv_programa.csv"),
        os.path.join(DATA_DIR, "siconv_programa.csv.zip"),
        os.path.join(DATA_DIR, "siconv_programa.zip")
    ]

    csv_path = None
    for p in csv_candidates:
        if os.path.exists(p):
            csv_path = p
            break

    if not csv_path:
        print("Erro: Arquivo siconv_programa não encontrado para processamento.", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect()

    query = f"""
    WITH programas AS (
        SELECT
            COD_PROGRAMA,
            NOME_PROGRAMA,
            COD_ORGAO_SUP_PROGRAMA,
            DESC_ORGAO_SUP_PROGRAMA,
            SIT_PROGRAMA,
            TRY_STRPTIME(NULLIF(DT_PROG_INI_RECEB_PROP, ''), '%d/%m/%Y') as dt_ini_prop,
            TRY_STRPTIME(NULLIF(DT_PROG_FIM_RECEB_PROP, ''), '%d/%m/%Y') as dt_fim_prop,
            TRY_STRPTIME(NULLIF(DT_PROG_INI_EMENDA_PAR, ''), '%d/%m/%Y') as dt_ini_emenda,
            TRY_STRPTIME(NULLIF(DT_PROG_FIM_EMENDA_PAR, ''), '%d/%m/%Y') as dt_fim_emenda
        FROM read_csv('{csv_path}',
                      delim=';',
                      header=True,
                      all_varchar=True)
        WHERE SIT_PROGRAMA IN ('DISPONIBILIZADO', 'CADASTRADO')
          AND (
              (TRY_STRPTIME(NULLIF(DT_PROG_FIM_RECEB_PROP, ''), '%d/%m/%Y') >= DATE '{hoje_str}')
              OR
              (TRY_STRPTIME(NULLIF(DT_PROG_FIM_EMENDA_PAR, ''), '%d/%m/%Y') >= DATE '{hoje_str}')
          )
    )
    SELECT
        NOME_PROGRAMA,
        DESC_ORGAO_SUP_PROGRAMA as orgao,
        SIT_PROGRAMA as status,
        MIN(dt_fim_prop) as prazo_proposta,
        MIN(dt_fim_emenda) as prazo_emenda,
        STRING_AGG(DISTINCT CAST(COD_PROGRAMA AS VARCHAR), ', ') as codigos_programa,
        COUNT(COD_PROGRAMA) as qtd_codigos
    FROM programas
    GROUP BY NOME_PROGRAMA, DESC_ORGAO_SUP_PROGRAMA, SIT_PROGRAMA
    ORDER BY COALESCE(prazo_proposta, prazo_emenda) ASC;
    """

    print("Executando consulta analítica via DuckDB...", flush=True)
    df = con.execute(query).df()
    print(f"Total de programas com janelas abertas encontrados: {len(df)}")

    lista_oportunidades = []
    for _, row in df.iterrows():
        prazo_ativo = row['prazo_proposta'] if row['prazo_proposta'] and not (row['prazo_proposta'] < hoje) else row['prazo_emenda']

        if prazo_ativo:
            prazo_date = prazo_ativo if isinstance(prazo_ativo, date) else prazo_ativo.date()
            dias_restantes = (prazo_date - hoje).days
            prazo_fmt = prazo_date.strftime("%d/%m/%Y")
        else:
            dias_restantes = 999
            prazo_fmt = "A definir"

        carteira = categorizar_programa(row['NOME_PROGRAMA'])

        lista_oportunidades.append({
            "id": str(row['codigos_programa']).split(',')[0].strip(),
            "codigo_programa": str(row['codigos_programa']),
            "nome": row['NOME_PROGRAMA'],
            "orgao": row['orgao'] or "Órgão Concedente Federal",
            "status": row['status'],
            "carteira": carteira,
            "publico_alvo": "Administração Pública / OSCs / Consórcios",
            "prazo_fim": prazo_fmt,
            "dias_restantes": dias_restantes,
            "link_transferegov": "https://portal.transferegov.sistema.gov.br/"
        })

    estado_ant_file = os.path.join(OUTPUT_DIR, "estado.json")
    programas_anteriores = set()
    if os.path.exists(estado_ant_file):
        try:
            with open(estado_ant_file, "r", encoding="utf-8") as f:
                prev_data = json.load(f)
                programas_anteriores = set(p.get("nome") for p in prev_data.get("programas", []))
        except Exception:
            pass

    novos_programas = [p for p in lista_oportunidades if p["nome"] not in programas_anteriores]

    resultado_final = {
        "metadata": {
            "ultima_atualizacao": datetime.now().isoformat(),
            "data_formatada": hoje.strftime("%d/%m/%Y"),
            "uf": uf,
            "total_programas_abertos": len(lista_oportunidades),
            "total_urgentes": len([p for p in lista_oportunidades if p["dias_restantes"] <= 15]),
            "novos_nesta_execucao": len(novos_programas)
        },
        "programas": lista_oportunidades
    }

    with open(os.path.join(OUTPUT_DIR, "oportunidades.json"), "w", encoding="utf-8") as f:
        json.dump(resultado_final, f, ensure_ascii=False, indent=2)

    with open(os.path.join(OUTPUT_DIR, "estado.json"), "w", encoding="utf-8") as f:
        json.dump(resultado_final, f, ensure_ascii=False, indent=2)

    print(f"✓ Processamento concluído com sucesso: {len(lista_oportunidades)} oportunidades salvas em public/data/oportunidades.json", flush=True)

if __name__ == "__main__":
    uf_param = "PB"
    if "--uf" in sys.argv:
        try:
            uf_param = sys.argv[sys.argv.index("--uf") + 1]
        except IndexError:
            pass

    baixar_e_extrair("siconv_programa")
    processar_radar(uf=uf_param)
