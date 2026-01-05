load("@aspect_rules_py//py:defs.bzl", "py_binary", "py_library", "py_test")

def o_py_test(name, deps = [], **kwargs):
    extra_deps = []

    if "@pip//pytest" not in deps:
        extra_deps.append("@pip//pytest")

    if "@pip//debugpy" not in deps:
        extra_deps.append("@pip//debugpy")

    py_test(
        name = name,
        pytest_main = True,
        deps = deps + extra_deps,
        **kwargs
    )

def o_py_library(name, **kwargs):
    py_library(
        name = name,
        **kwargs
    )

def o_py_binary(name, **kwargs):
    py_binary(
        name = name,
        **kwargs
    )
