"""Shared mlody common package.

This package must only be imported through ``mlody.common``. Importing it as
bare ``common`` collides with the top-level monorepo ``common`` package.
"""

if __name__ != "mlody.common":
    raise ModuleNotFoundError(
        "Use 'mlody.common', not 'common', for the mlody common package.",
    )
