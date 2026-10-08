from ._logging import get_verbosity, set_verbosity

# User-friendly default
set_verbosity("normal")

__all__ = [
    "set_verbosity",
    "get_verbosity",
]

# Users can set verbosity with
# import lca_modeller
# lca_modeller.set_verbosity("debug")  # or "quiet", "normal", "verbose"