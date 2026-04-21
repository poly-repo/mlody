load("@aspect_rules_py//py:defs.bzl", "py_binary", "py_library", "py_pex_binary", "py_test")

def _default_import_root():
    package = native.package_name()
    if not package:
        return "."
    depth = len([part for part in package.split("/") if part])
    return "/".join([".."] * depth)

def o_py_test(name, deps = [], imports = None, **kwargs):
    extra_deps = []
    extra_imports = imports if imports != None else [_default_import_root()]
    env = dict(kwargs.pop("env", {}))

    if "@pip//pytest" not in deps:
        extra_deps.append("@pip//pytest")

    if "@pip//debugpy" not in deps:
        extra_deps.append("@pip//debugpy")

    pytest_addopts = env.get("PYTEST_ADDOPTS", "")
    import_mode_flag = "--import-mode=importlib"
    existing_opts = [opt for opt in pytest_addopts.split(" ") if opt]
    if import_mode_flag not in existing_opts:
        env["PYTEST_ADDOPTS"] = (
            (pytest_addopts + " " + import_mode_flag).strip()
            if pytest_addopts
            else import_mode_flag
        )

    py_test(
        name = name,
        pytest_main = True,
        env = env,
        imports = extra_imports,
        deps = deps + extra_deps,
        **kwargs
    )

def o_py_library(name, imports = None, **kwargs):
    extra_imports = imports if imports != None else [_default_import_root()]
    py_library(
        name = name,
        imports = extra_imports,
        **kwargs
    )

def o_py_binary(name, imports = None, **kwargs):
    extra_imports = imports if imports != None else [_default_import_root()]
    py_binary(
        name = name,
        imports = extra_imports,
        **kwargs
    )

def o_py_pex_binary(name, **kwargs):
    py_pex_binary(
        name = name,
        **kwargs
    )
