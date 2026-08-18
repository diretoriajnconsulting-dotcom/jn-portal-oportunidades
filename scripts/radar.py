#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import json
import urllib.request
from datetime import datetime, date
import duckdb

BASE_URL = "https://api-publica.transferegov.gestao.gov.br/downloads"
FILES = [
    "siconv_programa.csv.zip",
    "siconv_programa_proposta.csv.zip",
    "siconv_proposta.csv.zip"
]

DATA_DIR = "dados_siconv"
OUTPUT_DIR = "public/data"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

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

def baixar_arquivos():
    print("Iniciando download dos arquivos do TransfereGov...")
    for filename in FILES:
        url = f"{BASE_URL}/{filename}"
        dest = os.path.join(DATA_DIR, filename)
        print(f"-> Baixando {filename}...")
        try:
            urllib.request.urlretrieve(url, dest)
            print(f"✓ {filename} baixado com sucesso.")
        except Exception as e:
            print(f"Erro ao baixar {filename}: {e}", file=sys.stderr)
            raise

def processar_radar():
    hoje = datetime.now().date()
    hoje_str = hoje.strftime("%Y-%m-%d")
    con = duckdb.connect()

    prog_path = os.path.join(DATA_DIR, "siconv_programa.csv.zip")
    
    query = f"""
    WITH programas AS (
        SELECT 
            COD_PROGRAMA,
            NOME_PROGRAMA,
            COD_ORGAO_SUP_PROGRAMA,
            DESC_ORGAO_SUP_PROGRAMA,
            SIT_PROGRAMA,
            STRPTIME(DT_PROG_INI_RECEB_PROP, '%d/%m/%Y') as dt_ini_prop,
            STRPTIME(DT_PROG_FIM_RECEB_PROP, '%d/%m/%Y') as dt_fim_prop,
            STRPTIME(DT_PROG_INI_EMENDA_PAR, '%d/%m/%Y') as dt_ini_emenda,
            STRPTIME(DT_PROG_FIM_EMENDA_PAR, '%d/%m/%Y') as dt_fim_emenda
        FROM read_csv('{prog_path}', 
                      delim=';', 
                      header=True, 
                      encoding='UTF-8-BOM', 
                      dateformat='%d/%m/%Y')
        WHERE SIT_PROGRAMA IN ('DISPONIBILIZADO', 'CADASTRADO')
          AND (
              (dt_fim_prop >= DATE '{hoje_str}')
              OR 
              (dt_fim_emenda >= DATE '{hoje_str}')
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

    df = con.execute(query).df()
    
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
            "uf": "PB",
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

    print(f"✓ Processamento concluído: {len(lista_oportunidades)} programas abertos mapeados.")

if __name__ == "__main__":
    baixar_arquivos()
    processar_radar()
