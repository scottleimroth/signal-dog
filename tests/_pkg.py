"""Loads the plugin folder as a package, the way Hermes does, so tests can import its modules."""
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load():
    if "signal_dog" in sys.modules:
        return sys.modules["signal_dog"]
    spec = importlib.util.spec_from_file_location("signal_dog", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["signal_dog"] = mod
    spec.loader.exec_module(mod)
    # The suite behaves the same on every system: the plugin is told it is on Linux, so switching on works under the test
    # stubs on a Mac too. The tests about a Mac set "Darwin" themselves.
    mod.platform_mod = types.SimpleNamespace(system=lambda: "Linux")
    return mod
