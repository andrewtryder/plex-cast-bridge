import importlib


def package():
    """Return the release_acceptance package module for test monkeypatching."""
    parts = (__package__ or __name__).split(".")
    if "release_acceptance" in parts:
        index = parts.index("release_acceptance")
        name = ".".join(parts[: index + 1])
    else:
        name = parts[0]
    return importlib.import_module(name)
