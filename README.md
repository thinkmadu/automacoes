# automacoes

Workflows reutilizáveis do GitHub Actions para os meus repositórios. A lógica
mora aqui; cada repositório guarda só um arquivo curto que chama o workflow.
Uma correção feita aqui chega a todos na próxima execução, sem tocar neles.

| Workflow | O que faz |
|---|---|
| [`ci-python.yml`](.github/workflows/ci-python.yml) | Lint, formatação, tipos e testes com uv (padrão: ruff e pytest) |
| [`ci-node.yml`](.github/workflows/ci-node.yml) | Lint, tipos, testes e build com npm (padrão: scripts do `package.json`) |
| [`add-to-project.yml`](.github/workflows/add-to-project.yml) | Coloca issue e pull request novos num GitHub Project |
| [`fechar-issues.yml`](.github/workflows/fechar-issues.yml) | Fecha, no merge na `develop`, as issues que a descrição do pull request cita com palavra de fechamento |
| [`release-please.yml`](.github/workflows/release-please.yml) | Pull request de release, tag e CHANGELOG a partir de Conventional Commits |

Os arquivos prontos para copiar estão em [`exemplos/`](exemplos/).

## Como chamar

```yaml
# .github/workflows/ci.yml no repositório que usa
name: CI
on:
  push:
    branches: [main, develop]
  pull_request:
permissions:
  contents: read
jobs:
  ci:
    uses: thinkmadu/automacoes/.github/workflows/ci-python.yml@main
    with:
      typecheck: uv run pyright
```

Os repositórios chamam pela `main`, que é a branch estável: trabalho novo
entra pela `develop` e só chega à `main` por pull request, com os checks
`actionlint` e `zizmor` exigidos por um ruleset. Sem esse ruleset, um push
direto na `main` roda em todos os repositórios que chamam daqui.

### Comandos das etapas

Nos dois workflows de CI cada etapa é um comando de shell, com um padrão que
serve à maioria dos projetos. Para trocar, passe outro comando; para pular,
passe string vazia. Exemplo de um backend com `requirements.txt` e flake8:

```yaml
    with:
      working-directory: backend
      install: uv pip install -r requirements.txt flake8
      lint: flake8 . --extend-exclude .venv --select=E9,F63,F7,F82
      format: ""
      test: ""
```

Os comandos rodam com `eval`: nunca passe dado de evento (título de issue ou
de pull request, nome de branch) nesses inputs, porque ele seria executado.
No `ci-python.yml` a `.venv` fica dentro de `working-directory` e já está
ativada, então `flake8`, `pytest` e afins rodam sem `uv run` na frente. Um
linter que não ignora `.venv` sozinho (flake8, por exemplo) precisa excluí-la.

### Segredos e permissões

| Workflow | O repositório que chama precisa de |
|---|---|
| `add-to-project.yml` | Segredo com PAT classic passado como `project-token`, só com o escopo `project` (mais `repo` apenas se o repositório for privado) |
| `fechar-issues.yml` | No job, `issues: write`; o caller dispara em `pull_request` do tipo `closed` |
| `release-please.yml` | No job, `contents`, `issues` e `pull-requests` com `write`; em Settings > Actions > General, "Allow GitHub Actions to create and approve pull requests" ligado |

O GitHub só lê `Closes #n` na descrição de pull request que mira a branch
padrão ([documentação](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)).
Como os pull requests daqui miram `develop`, é o `fechar-issues.yml` que fecha a
issue no merge. Ele aceita as palavras do GitHub (`closes`, `fixes`, `resolves` e
flexões) e `fecha` e `corrige`, só para issue do mesmo repositório (`#12`, não
`dona/repo#12`). Ponha a mesma linha na mensagem do commit: se o caller ainda não
existir quando o pull request for mesclado, a issue fecha quando o commit chegar
à `main`.

Com o `GITHUB_TOKEN`, o pull request de release não dispara os outros
workflows, e o CI não roda nele. Se a `main` exige o CI para mesclar, passe um
PAT como segredo `token` do `release-please.yml`.

Os segredos vão nomeados (`secrets: project-token: ...`), nunca com
`secrets: inherit`: o workflow chamado recebe só o que precisa.

## Versões

Toda action fica fixada por SHA de commit, com a tag ao lado. O Dependabot
abre um pull request por semana com as versões novas, depois de uma semana de
espera para cada release; a imagem do actionlint em
[`validar.yml`](.github/workflows/validar.yml) é a única que se atualiza à mão.

## Validação

[`validar.yml`](.github/workflows/validar.yml) roda actionlint e zizmor em todo
push e pull request. Localmente:

```bash
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 -color
uvx zizmor --persona=pedantic .
```
