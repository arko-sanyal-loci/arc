"""Every module in Arc's packages must import.

Eight of the eleven loci/ modules used bare imports (``from slp_encoding import``)
that only work when loci/ itself is on sys.path, so ``import loci.car_front``
failed. This test catches that class of break.
"""
import importlib
import pkgutil

import pytest

PACKAGES = ["app", "core", "loci"]


def _modules():
    for name in PACKAGES:
        pkg = importlib.import_module(name)
        yield name
        for mod in pkgutil.walk_packages(pkg.__path__, prefix=f"{name}."):
            yield mod.name


@pytest.mark.parametrize("module", list(_modules()))
def test_module_imports(module):
    importlib.import_module(module)
