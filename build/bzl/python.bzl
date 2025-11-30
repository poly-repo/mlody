load("@aspect_rules_py//py:defs.bzl", "py_binary", "py_library", "py_test")

def o_py_test(name, deps = [], **kwargs):
    py_test(
        name = name,
        pytest_main = True,
        deps = deps + ["@pip//pytest"] + ["@pip//debugpy"],
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
