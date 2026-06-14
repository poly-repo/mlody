"""API for declaring an HLint aspect that visits Haskell rules."""

load(
    "@aspect_rules_lint//lint/private:lint_aspect.bzl",
    "LintOptionsInfo",
    "filter_srcs",
    "noop_lint_action",
    "output_files",
    "patch_and_output_files",
    "should_visit",
)

_MNEMONIC = "AspectRulesLintHLint"


def _hlint_action(ctx, executable, srcs, config, stdout, exit_code = None, options = []):
    args = ctx.actions.args()
    args.add("--hint={}".format(config.path))
    args.add_all(options)
    args.add_all(srcs)

    outputs = [stdout]
    if exit_code:
        command = "{hlint} $@ >{stdout}; echo $? >" + exit_code.path
        outputs.append(exit_code)
    else:
        command = "{hlint} $@ >{stdout}"

    ctx.actions.run_shell(
        inputs = srcs + [config],
        outputs = outputs,
        command = command.format(
            hlint = executable.path,
            stdout = stdout.path,
        ),
        arguments = [args],
        mnemonic = _MNEMONIC,
        progress_message = "Linting %{label} with HLint",
        tools = [executable],
    )


def _hlint_aspect_impl(target, ctx):
    if not should_visit(ctx.rule, ctx.attr._rule_kinds):
        return []

    files_to_lint = [
        src
        for src in filter_srcs(ctx.rule)
        if src.extension in ["hs", "lhs"]
    ]
    if ctx.attr._options[LintOptionsInfo].fix:
        outputs, info = patch_and_output_files(_MNEMONIC, target, ctx)
        ctx.actions.write(outputs.patch, "")
    else:
        outputs, info = output_files(_MNEMONIC, target, ctx)

    if len(files_to_lint) == 0:
        noop_lint_action(ctx, outputs)
        return [info]

    color_option = "--color=always" if ctx.attr._options[LintOptionsInfo].color else "--color=never"

    _hlint_action(
        ctx,
        ctx.executable._hlint,
        files_to_lint,
        ctx.file._config_file,
        outputs.human.out,
        outputs.human.exit_code,
        [color_option],
    )
    _hlint_action(
        ctx,
        ctx.executable._hlint,
        files_to_lint,
        ctx.file._config_file,
        outputs.machine.out,
        outputs.machine.exit_code,
        ["--sarif", "--color=never"],
    )

    return [info]


def lint_hlint_aspect(binary, config, rule_kinds = ["haskell_binary", "haskell_library", "haskell_test"]):
    """A factory function to create an HLint aspect."""
    return aspect(
        implementation = _hlint_aspect_impl,
        attrs = {
            "_options": attr.label(
                default = "@aspect_rules_lint//lint:options",
                providers = [LintOptionsInfo],
            ),
            "_hlint": attr.label(
                default = binary,
                executable = True,
                cfg = "exec",
            ),
            "_config_file": attr.label(
                default = config,
                allow_single_file = True,
            ),
            "_rule_kinds": attr.string_list(
                default = rule_kinds,
            ),
        },
    )
