"Define linter aspects"

load(":hlint.bzl", "lint_hlint_aspect")
load("@aspect_rules_lint//lint:lint_test.bzl", "lint_test")
load("@aspect_rules_lint//lint:ruff.bzl", "lint_ruff_aspect")

ruff = lint_ruff_aspect(
    binary = "@multitool//tools/ruff",
    configs = [
        Label("//:pyproject.toml"),
        # if the repository has nested ruff.toml files, they must be added here as well
    ],
)

hlint = lint_hlint_aspect(
    binary = Label("@stackage-exe//hlint:hlint"),
    config = Label("//:.hlint.yaml"),
)

hlint_test = lint_test(aspect = hlint)
ruff_test = lint_test(aspect = ruff)
