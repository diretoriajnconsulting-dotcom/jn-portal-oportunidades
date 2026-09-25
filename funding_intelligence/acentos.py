"""Conserto dos acentos que o SICONV entrega quebrados nos nomes de programa.

O arquivo de programas traz nomes com "?" ou "�" no lugar da letra acentuada: no
catálogo de 15/09/2026, 13 das 270 janelas abertas na Paraíba, como "5663 - Autonomia
Econ?mica das Mulheres". Além de feio na tela, o nome quebrado some da busca e dos temas
da carteira: "habitaç" não casa com "Habita??o".

Porte de `painel_execucao/acentos.py` do monorepo siteponte/ponte-oportunidades, onde o
mesmo conserto recuperou 420 dos 558 nomes quebrados do arquivo de 14/09/2026. O
algoritmo é o mesmo; se mudar lá, mudar aqui.

O conserto usa só o vocabulário do próprio arquivo. Cada palavra quebrada é comparada com
as palavras íntegras de mesma forma (as mesmas letras ASCII, com uma letra não ASCII no
lugar de cada caractere quebrado) e só é trocada quando um candidato predomina com folga.
"Pr?" (pré ou pró) e "1?" (1ª ou 1º) ficam como vieram.

O "?" sozinho entre palavras é crase em "Apoio ? Implantação", mas travessão em "SENASP ?
AÇÃO 20ID". Por isso a palavra feita só de letra quebrada é decidida junto com a palavra
anterior: vira "à" só se o arquivo escreve "apoio à" íntegro em outros nomes.

O nome consertado é só texto de exibição e busca. Nenhuma identidade de janela depende
dele: ver `identidade_janela` em `adapters/transferegov.py` e `ident` em
`scripts/publicar.py`.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable

QUEBRADOS = "?�"
# O candidato mais usado precisa aparecer ao menos tantas vezes quanto o segundo, vezes isto.
PREDOMINANCIA = 10
# Proporção de letras maiúsculas a partir da qual o nome é tratado como escrito em caixa alta.
CAIXA_ALTA = 0.8

_PALAVRA = re.compile(r"(?:[^\W_]|[?�])+")
_SEGUE_PALAVRA = re.compile(r"\s*[^\W\d_]")

Indice = dict[str, Counter[str]]


def _quebrada(texto: str) -> bool:
    return any(c in QUEBRADOS for c in texto)


def _forma(palavra: str) -> str:
    """A palavra em minúsculas com "?" no lugar de toda letra não ASCII e de todo caractere quebrado."""
    return "".join("?" if c in QUEBRADOS or ord(c) > 127 else c for c in palavra.lower())


def _chave(palavra: str, anterior: str | None) -> str | None:
    """Onde procurar a palavra no índice. A que não tem letra ASCII ("à", "é") depende da anterior."""
    forma = _forma(palavra)
    if forma.strip("?"):
        return forma
    return f"{anterior.lower()} {forma}" if anterior else None


def indice_de(nomes: Iterable[tuple[str, int]]) -> Indice:
    """As palavras íntegras com acento, agrupadas pela forma, com quantas vezes aparecem."""
    indice: Indice = defaultdict(Counter)
    for nome, peso in nomes:
        anterior = None
        for palavra in _PALAVRA.findall(nome or ""):
            if not _quebrada(palavra) and any(ord(c) > 127 for c in palavra):
                chave = _chave(palavra, anterior)
                if chave:
                    indice[chave][palavra.lower()] += peso
            anterior = palavra
    return indice


def _escolha(candidatos: Counter[str] | None) -> str | None:
    if not candidatos:
        return None
    (primeiro, vezes), *resto = candidatos.most_common(2)
    if resto and vezes < PREDOMINANCIA * resto[0][1]:
        return None
    return primeiro


def _em_caixa_alta(texto: str) -> bool:
    letras = [c for c in texto if c.isalpha()]
    return bool(letras) and sum(c.isupper() for c in letras) >= CAIXA_ALTA * len(letras)


def _na_caixa(original: str, escolhida: str, *, caixa_alta: bool, depois_de_maiuscula: bool) -> str:
    """Põe as letras recuperadas na caixa da palavra: "DEFICI?NCIA" → "DEFICIÊNCIA", "Sistema ?nico" → "Único"."""
    letras = [c for c in original if c.isalpha()]
    alta = all(c.isupper() for c in letras) if len(letras) > 1 else caixa_alta
    saida = []
    for i, (o, e) in enumerate(zip(original, escolhida)):
        if o not in QUEBRADOS:
            saida.append(o)
        elif alta or (i == 0 and letras and depois_de_maiuscula):
            saida.append(e.upper())
        else:
            saida.append(e)
    return "".join(saida)


def reparar(texto: str | None, indice: Indice) -> str | None:
    """O texto com as palavras quebradas trocadas pela forma íntegra, quando o vocabulário decide."""
    if not texto or not _quebrada(texto):
        return texto
    caixa_alta = _em_caixa_alta(texto)
    partes: list[str] = []
    fim = 0
    anterior: str | None = None
    for m in _PALAVRA.finditer(texto):
        palavra = m.group()
        partes.append(texto[fim:m.start()])
        fim = m.end()
        # "?" sozinho só vira letra se vier uma palavra logo depois: no fim é pergunta, antes de "(" é travessão.
        solto = all(c in QUEBRADOS for c in palavra) and not _SEGUE_PALAVRA.match(texto, m.end())
        chave = _chave(palavra, anterior) if _quebrada(palavra) and not solto else None
        escolhida = _escolha(indice.get(chave)) if chave else None
        if escolhida is None:
            partes.append(palavra)
        else:
            depois_de_maiuscula = anterior is None or anterior[0].isupper()
            partes.append(_na_caixa(palavra, escolhida, caixa_alta=caixa_alta, depois_de_maiuscula=depois_de_maiuscula))
        anterior = palavra
    partes.append(texto[fim:])
    return "".join(partes)


def reparos_de(nomes: Iterable[tuple[str, int]]) -> dict[str, str]:
    """Nome quebrado → nome consertado, só para os que o vocabulário do próprio arquivo conserta.

    `nomes` são os nomes distintos do arquivo de programas inteiro, com quantas linhas cada um
    tem. O arquivo inteiro, e não só a UF do radar: o vocabulário da Paraíba sozinho é pequeno
    demais para decidir com folga.
    """
    nomes = list(nomes)
    indice = indice_de(nomes)
    return {nome: novo for nome, _ in nomes if nome and _quebrada(nome) and (novo := reparar(nome, indice)) != nome}
