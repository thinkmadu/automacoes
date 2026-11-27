#!/usr/bin/env python3
"""Trava de escopo: confere a entrega de um implementador contra a spec, sem modelo.

Roda depois do agy (ou do `implementador`) e antes do `verificador`. Compara o repositório
com o commit anotado antes da delegação e responde três perguntas por aritmética e regex:

1. Escopo: algum arquivo tocado (rastreado ou novo) está fora da lista da spec?
2. Tamanho: as linhas adicionadas passam do teto da spec?
3. Atalho: o diff baixa a régua para passar no gate? São os cinco atalhos do floor-guard
   (addyosmani/agent-skills, skill constraint-driven-development, MIT), adaptados a Python e
   JS/TS: limite rebaixado em config, teste facilitado, checador silenciado, trabalho
   inacabado e verificação tirada do CI.

Conta só linhas adicionadas, porque o vício medido é inchar o código (692 linhas onde cabiam
450, em 20/11/2026); apagar não incha. Lockfile, minificado e arquivo binário entram na
checagem de escopo, mas não na contagem. O relatório de atalho dá regra e `arquivo:linha`,
nunca o texto da linha (a linha silenciada costuma ser a que guarda um segredo).

Não enxerga: escrita fora do repositório (fica com o `agy-pre-tool.py`), arquivo novo em
pasta coberta pelo `.gitignore` e atalho que a regex não alcança (é raso de propósito).

Uso:
  conferir-escopo --base <sha> --permitidos 'src/cart.py,tests/*' --teto 450 [-d <repo>]
                  [--aceitar todo,checador-silenciado] [--excluir 'gerado/*'] [--json]
Padrão de `--permitidos`: glob do fnmatch relativo à raiz do repositório (`*` atravessa `/`,
então `src/*` cobre a árvore inteira de `src/`); termo terminado em `/` vale como pasta.
`--aceitar` libera regras de atalho que a spec autorizou (nomes em REGRAS_DE_ATALHO).

Saída: soma de 1 (escopo), 2 (tamanho) e 4 (atalho), de 0 a 7; 64 é erro de uso, e erro de
uso nunca sai como 0 nem como reprovação.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FORA_DA_CONTAGEM = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "composer.lock",
    "go.sum",
    "*.min.js",
    "*.min.css",
)
APROVADO, ESCOPO, TAMANHO, ATALHO, ERRO = 0, 1, 2, 4, 64

SILENCIADORES = re.compile(
    r"@ts-ignore|@ts-nocheck|@ts-expect-error|eslint-disable|biome-ignore|istanbul ignore"
    r"|c8 ignore|nosemgrep|gitleaks:allow|Stryker disable|#\s*noqa|#\s*type:\s*ignore"
    r"|#\s*pragma:\s*no cover|#\s*nosec\b|#\s*pylint:\s*disable|#\s*ruff:\s*noqa"
    r"|#\s*mypy:\s*ignore-errors"
)
# Silenciador com código explícito é estreito e auditável (ruff PGH003/PGH004 pedem isso):
# não conta como atalho. O genérico, que cala o checador inteiro na linha ou no arquivo, conta.
# Calibrado em 27/11/2026 nos 10 últimos merges do CareerFlow: `assert x is not None  # noqa:
# S101` (estreitamento de tipo) era o falso positivo mais comum.
SILENCIADOR_COM_CODIGO = re.compile(
    r"#\s*noqa:\s*[A-Z]+\d+(\s*,\s*[A-Z]+\d+)*|#\s*type:\s*ignore\[[\w\s,-]+\]"
    r"|#\s*pylint:\s*disable=[\w,-]+|#\s*nosec\s+B\d+"
    r"|eslint-disable-(?:next-)?line\s+[@\w/-]+|biome-ignore\s+lint/\S+"
)
TESTE_DEF = re.compile(r"^\s*(async\s+)?def\s+test_|\b(it|test)\(\s*['\"`]")
PULOS = re.compile(
    r"\b(it|test|describe)\.(skip|todo)\b|\bxit\(|\bxdescribe\(|@pytest\.mark\.(skip|skipif|xfail)\b"
    r"|\bpytest\.skip\(|@unittest\.skip|\bself\.skipTest\("
)
STUBS = re.compile(
    r"raise NotImplementedError|throw new Error\(\s*['\"`][^'\"`]*not implemented"
    r"|catch\s*(\(\s*\w*\s*\))?\s*\{\s*\}|\bexcept\b[^:]*:\s*pass\b"
)
TODO = re.compile(r"\b(TODO|FIXME|XXX)\b")
ASSERCAO = re.compile(r"\bassert\w*\b|\bexpect\(|\.should\b")
EXCEPT_SOZINHO = re.compile(r"^\s*except\b[^:]*:\s*$")
PASS_SOZINHO = re.compile(r"^\s*pass\s*$")
# Chave de limite em config: número que baixa ou some é régua rebaixada.
CHAVE_DE_LIMITE = re.compile(
    r"(fail[_-]under|cov-fail-under|coverageThreshold|threshold|branches|functions|lines"
    r"|statements|min[_-]?coverage)",
    re.IGNORECASE,
)
CHAVE_ESTRITA = re.compile(
    r"\b(strict|strictNullChecks|noImplicitAny|disallow_untyped_defs|warn_unused_ignores)\b"
)
VERIFICACAO_NO_CI = re.compile(
    r"\b(pytest|ruff|mypy|pyright|eslint|tsc|vitest|jest|npm (run )?(test|lint|typecheck)"
    r"|pnpm (run )?(test|lint)|bun (run )?(test|lint|typecheck)|uv run (pytest|ruff))\b"
)
EXTENSOES_DE_CODIGO = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".cs",
    ".swift",
    ".sh",
    ".lua",
    ".dart",
    ".vue",
    ".svelte",
}
REGRAS_DE_ATALHO = (
    "limite-rebaixado",
    "teste-facilitado",
    "teste-apagado",
    "assercao-removida",
    "checador-silenciado",
    "trabalho-inacabado",
    "todo",
    "verificacao-tirada-do-ci",
)


class ErroDeUso(Exception):
    pass


class Parser(argparse.ArgumentParser):
    """argparse sai com 2 em erro, que aqui leria como "reprovado por tamanho"."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"conferir-escopo: erro: {message}", file=sys.stderr)
        raise SystemExit(ERRO)


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    p = subprocess.run(
        ["git", "-c", "core.quotePath=false", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if p.returncode != 0:
        raise ErroDeUso(f"git {' '.join(args[:2])}: {p.stderr.strip() or 'falhou'}")
    return p.stdout


def casa(caminho: str, padroes: list[str], por_nome: bool = False) -> bool:
    """Escopo casa pelo caminho inteiro; a exclusão da contagem também pelo nome do arquivo."""
    for padrao in padroes:
        if padrao.endswith("/") and caminho.startswith(padrao):
            return True
        if fnmatch.fnmatchcase(caminho, padrao):
            return True
        if por_nome and fnmatch.fnmatchcase(Path(caminho).name, padrao):
            return True
    return False


def foto_do_worktree(repo: Path) -> str:
    """Árvore do worktree inteiro (mudança solta e não rastreado fora do .gitignore).

    Índice temporário: o índice, o branch e o stash do usuário ficam intactos. Comparar a base
    com essa árvore faz o arquivo não rastreado que já estava na base deixar de contar.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        git(repo, "read-tree", "HEAD", env=env)
        git(repo, "add", "-A", env=env)
        return git(repo, "write-tree", env=env).strip()


def eh_teste(caminho: str) -> bool:
    nome = Path(caminho).name
    return bool(
        re.search(r"\.(test|spec)\.|_test\.", nome)
        or nome.startswith("test_")
        or re.search(r"(^|/)(tests?|__tests__)/", caminho)
    )


def eh_config(caminho: str) -> bool:
    nome = Path(caminho).name
    return (
        nome
        in {
            "pyproject.toml",
            "setup.cfg",
            ".coveragerc",
            "tox.ini",
            "pytest.ini",
            "mypy.ini",
            "ruff.toml",
            ".ruff.toml",
            "package.json",
            "tsconfig.json",
            ".eslintrc",
            ".eslintrc.json",
            ".eslintrc.js",
            ".eslintrc.cjs",
            "eslint.config.js",
            "eslint.config.mjs",
            ".nycrc",
            ".c8rc",
            "biome.json",
        }
        or bool(re.match(r"(jest|vitest|vite)\.config\.", nome))
        or nome.startswith("tsconfig.")
    )


def eh_ci(caminho: str) -> bool:
    return caminho.startswith(".github/workflows/") or Path(caminho).name in {
        ".gitlab-ci.yml",
        ".pre-commit-config.yaml",
        "Makefile",
        "justfile",
    }


def ler_diff(repo: Path, base: str, arvore: str) -> tuple[dict, dict, set]:
    """Linhas adicionadas e removidas por arquivo, com número, e o conjunto de apagados."""
    saida = git(
        repo,
        "diff",
        "--unified=0",
        "-M",
        "--no-prefix",
        "--no-color",
        base,
        arvore,
        "--",
    )
    adicionadas: dict[str, list[tuple[int, str]]] = {}
    removidas: dict[str, list[tuple[int, str]]] = {}
    apagados: set[str] = set()
    arquivo = antigo = ""
    no_cabecalho = False
    n_antigo = n_novo = 0
    for linha in saida.split("\n"):
        if linha.startswith("diff --git "):
            no_cabecalho = True
        elif linha.startswith("@@"):
            no_cabecalho = False
            m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)", linha)
            if m:
                n_antigo, n_novo = int(m[1]), int(m[2])
        elif no_cabecalho:
            if linha.startswith("--- "):
                antigo = linha[4:].rstrip("\t")
            elif linha.startswith("+++ "):
                novo = linha[4:].rstrip("\t")
                arquivo = antigo if novo == "/dev/null" else novo
                if novo == "/dev/null":
                    apagados.add(arquivo)
        elif linha.startswith("+"):
            adicionadas.setdefault(arquivo, []).append((n_novo, linha[1:]))
            n_novo += 1
        elif linha.startswith("-"):
            removidas.setdefault(arquivo, []).append((n_antigo, linha[1:]))
            n_antigo += 1
    return adicionadas, removidas, apagados


def chave(texto: str) -> str | None:
    m = re.match(r'^\s*"?([\w.\-/]+)"?\s*[:=]', texto)
    return m[1] if m else None


def numeros(texto: str) -> list[float]:
    return [
        float(n) for n in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", texto.split("#")[0])
    ]


def atalhos(adicionadas: dict, removidas: dict, apagados: set) -> list[dict]:
    achados: list[dict] = []

    def marca(regra: str, onde: str) -> None:
        achados.append({"regra": regra, "onde": onde})

    def por_linha(linhas: list[tuple[int, str]]) -> dict[str, list[int]]:
        """Linhas que casam com cada regra de linha, pela ordem."""
        achou: dict[str, list[int]] = {}
        anterior = ""
        for n, texto in linhas:
            if SILENCIADORES.search(SILENCIADOR_COM_CODIGO.sub("", texto)):
                achou.setdefault("checador-silenciado", []).append(n)
            if PULOS.search(texto):
                achou.setdefault("teste-facilitado", []).append(n)
            if STUBS.search(texto) or (
                EXCEPT_SOZINHO.match(anterior) and PASS_SOZINHO.match(texto)
            ):
                achou.setdefault("trabalho-inacabado", []).append(n)
            if TODO.search(texto):
                achou.setdefault("todo", []).append(n)
            anterior = texto
        return achou

    # Linha editada que já tinha o mesmo padrão antes (skip com outra mensagem, noqa num
    # arquivo renomeado e mexido) não é atalho novo: a regra só acusa no arquivo em que o
    # número de ocorrências cresceu.
    for arquivo, linhas in adicionadas.items():
        if Path(arquivo).suffix not in EXTENSOES_DE_CODIGO:
            continue
        antes = por_linha(removidas.get(arquivo, []))
        for regra, onde_casa in por_linha(linhas).items():
            if len(onde_casa) > len(antes.get(regra, [])):
                for n in onde_casa:
                    marca(regra, f"{arquivo}:{n}")

    # Teste movido de um arquivo para outro não é teste apagado: o par só acusa quando o total
    # dos arquivos de teste cai (funções de teste ou asserções).
    def conta(regex: re.Pattern, lado: dict) -> dict[str, int]:
        return {
            a: sum(bool(regex.search(t)) for _, t in ls)
            for a, ls in lado.items()
            if eh_teste(a)
        }

    assert_sai, assert_entra = conta(ASSERCAO, removidas), conta(ASSERCAO, adicionadas)
    defs_sai, defs_entra = conta(TESTE_DEF, removidas), conta(TESTE_DEF, adicionadas)
    caiu_assercao = sum(assert_sai.values()) > sum(assert_entra.values())
    caiu_teste = sum(defs_sai.values()) > sum(defs_entra.values())

    if caiu_assercao or caiu_teste:
        for arquivo in sorted(apagados):
            if eh_teste(arquivo):
                marca("teste-apagado", f"{arquivo} (apagado)")

    for arquivo, linhas in removidas.items():
        if arquivo in apagados:
            continue
        saiu, entrou = assert_sai.get(arquivo, 0), assert_entra.get(arquivo, 0)
        if caiu_assercao and saiu > entrou:
            marca("assercao-removida", f"{arquivo} ({saiu - entrou} a menos)")
        novas = adicionadas.get(arquivo, [])
        if eh_config(arquivo):
            for n, texto in linhas:
                k = chave(texto)
                if not k or not (CHAVE_DE_LIMITE.search(texto) or CHAVE_ESTRITA.search(k)):
                    continue
                par = next((t for _, t in novas if chave(t) == k), None)
                onde = f"{arquivo}:{n} (base)"
                if par is None:
                    marca("limite-rebaixado", onde + ", chave removida")
                elif (
                    CHAVE_ESTRITA.search(k)
                    and "true" in texto.lower()
                    and "false" in par.lower()
                ):
                    marca("limite-rebaixado", onde + ", modo estrito desligado")
                elif CHAVE_DE_LIMITE.search(texto):
                    antes, depois = numeros(texto), numeros(par)
                    if any(d < a for a, d in zip(antes, depois)) or len(depois) < len(
                        antes
                    ):
                        marca("limite-rebaixado", onde)
        if eh_ci(arquivo):
            for n, texto in linhas:
                m = VERIFICACAO_NO_CI.search(texto)
                if m and not any(m[0] in t for _, t in novas):
                    marca("verificacao-tirada-do-ci", f"{arquivo}:{n} (base)")
    return achados


def conferir(
    repo: Path,
    base: str,
    permitidos: list[str],
    teto: int,
    excluir: list[str],
    aceitar: list[str] | None = None,
) -> dict:
    git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    arvore = foto_do_worktree(repo)
    adicionadas, removidas, apagados = ler_diff(repo, base, arvore)
    tocados: dict[str, int] = {c: 0 for c in removidas} | {
        c: len(v) for c, v in adicionadas.items()
    }
    for c in apagados:
        tocados.setdefault(c, 0)
    # Com -M, rename vem como "mais\tmenos\t\0antigo\0novo\0"; o caminho antigo também conta
    # como tocado (sair dele é mexer nele), com zero linha.
    campos = git(repo, "diff", "--numstat", "-z", "-M", base, arvore, "--").split("\0")
    i = 0
    while i < len(campos):
        if not campos[i]:
            i += 1
            continue
        mais, _menos, caminho = campos[i].split("\t", 2)
        if not caminho:
            antigo, caminho = campos[i + 1], campos[i + 2]
            tocados.setdefault(antigo, 0)
            i += 2
        tocados[caminho] = int(mais) if mais.isdigit() else 0  # binário: "-"
        i += 1

    fora_da_contagem = list(FORA_DA_CONTAGEM) + excluir
    intrusos = sorted(c for c in tocados if not casa(c, permitidos))
    contadas = sum(
        n for c, n in tocados.items() if not casa(c, fora_da_contagem, por_nome=True)
    )
    aceitas = set(aceitar or [])
    achados = [
        a
        for a in atalhos(adicionadas, removidas, apagados)
        if a["regra"] not in aceitas
    ]
    codigo = (
        (ESCOPO if intrusos else 0)
        + (TAMANHO if contadas > teto else 0)
        + (ATALHO if achados else 0)
    )
    return {
        "codigo": codigo,
        "arquivos": len(tocados),
        "intrusos": intrusos,
        "linhas_adicionadas": contadas,
        "teto": teto,
        "atalhos": achados,
    }


def relatorio(r: dict) -> str:
    resumo = f"{r['arquivos']} arquivo(s), {r['linhas_adicionadas']} de {r['teto']} linhas adicionadas"
    if r["codigo"] == APROVADO:
        return f"aprovado na trava: {resumo}"
    linhas = []
    if r["intrusos"]:
        linhas.append("reprovado por escopo: arquivo(s) fora da spec")
        linhas += [f"  {c}" for c in r["intrusos"]]
    if r["linhas_adicionadas"] > r["teto"]:
        linhas.append(
            f"reprovado por tamanho: {r['linhas_adicionadas']} linhas adicionadas contra teto de {r['teto']}"
        )
    if r["atalhos"]:
        linhas.append("reprovado por atalho: o diff baixa a régua para passar no gate")
        linhas += [f"  [{a['regra']}] {a['onde']}" for a in r["atalhos"]]
        linhas.append(
            "  conserte o código; se a spec autoriza, rode de novo com --aceitar <regra>"
        )
    linhas.append(f"({resumo})")
    return "\n".join(linhas)


def lista(texto: str) -> list[str]:
    return [t.strip() for t in texto.split(",") if t.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = Parser(description="Confere a entrega contra escopo, teto e atalhos da spec.")
    ap.add_argument("--base", required=True, help="commit anotado antes da delegação")
    ap.add_argument(
        "--permitidos", required=True, type=lista, help="globs separados por vírgula"
    )
    ap.add_argument(
        "--teto", required=True, type=int, help="máximo de linhas adicionadas"
    )
    ap.add_argument(
        "--excluir",
        default=[],
        type=lista,
        help="globs extras fora da contagem de linhas",
    )
    ap.add_argument(
        "--aceitar",
        default=[],
        type=lista,
        help="regras de atalho autorizadas pela spec",
    )
    ap.add_argument(
        "-d", "--dir", default=".", help="repositório (padrão: pasta atual)"
    )
    ap.add_argument("--json", action="store_true", help="saída em JSON")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return ERRO if e.code not in (0, None) else 0
    try:
        if not args.permitidos or args.teto < 0:
            raise ErroDeUso("--permitidos vazio ou --teto negativo")
        desconhecidas = set(args.aceitar) - set(REGRAS_DE_ATALHO)
        if desconhecidas:
            raise ErroDeUso(
                f"--aceitar com regra desconhecida: {', '.join(sorted(desconhecidas))}"
            )
        repo = Path(git(Path(args.dir), "rev-parse", "--show-toplevel").strip())
        r = conferir(
            repo, args.base, args.permitidos, args.teto, args.excluir, args.aceitar
        )
    except ErroDeUso as e:
        print(f"conferir-escopo: erro: {e}", file=sys.stderr)
        return ERRO
    print(json.dumps(r, ensure_ascii=False) if args.json else relatorio(r))
    return r["codigo"]


if __name__ == "__main__":
    sys.exit(main())
