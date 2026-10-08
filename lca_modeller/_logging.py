"""
Logging configuration for LCA-Modeller.

This module provides the public ``lca_modeller.set_verbosity()`` API and
controls how much output from LCA-Modeller and lca-algebraic is displayed.

Verbosity levels
----------------
quiet
    LCA-Modeller warnings/errors only.
    Dependency errors only.

normal
    LCA-Modeller progress information and warnings.
    lca-algebraic INFO messages are hidden.
    Known non-actionable lca-algebraic warnings are hidden.

verbose
    LCA-Modeller progress information and warnings.
    lca-algebraic INFO messages are shown, except known internal noise.

debug
    Show all available diagnostic output, including normally hidden
    lca-algebraic implementation messages.
"""

from __future__ import annotations

import logging
from typing import Literal


Verbosity = Literal["quiet", "normal", "verbose", "debug"]

_LCA_MODELLER_LOGGER = "lca_modeller"
_LCA_ALGEBRAIC_LOGGER = "lca_algebraic"

_CURRENT_VERBOSITY: Verbosity = "normal"


class _LCAAlgebraicNoiseFilter(logging.Filter):
    """
    Hide known non-actionable lca-algebraic implementation messages.

    The filter is deliberately narrow: unknown lca-algebraic warnings are
    allowed through so that potentially important issues are not hidden.

    The filter is disabled entirely in debug mode.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Never interfere with messages from other libraries.
        if not record.name.startswith(_LCA_ALGEBRAIC_LOGGER):
            return True

        message = record.getMessage()

        # lca-algebraic deliberately creates activities in its own proxy
        # databases. These databases are implementation details and the
        # warning is not actionable for LCA-Modeller users.
        if (
            "You are creating activity in background DB" in message
            and "-proxy" in message
            and "Use copyActivity()" in message
        ):
            return False

        # LCA-Modeller deliberately resets foreground/user databases when
        # rebuilding a model. lca-algebraic reports this as a warning.
        #
        # Match both the current typo "Reseting" and a possible future
        # correction to "Resetting".
        if (
            message.startswith("Db ")
            and "was here." in message
            and (
                "Reseting it" in message
                or "Resetting it" in message
            )
        ):
            return False

        # Parameters are intentionally recreated when rebuilding the
        # foreground model.
        if (
            "[ParamRegistry]" in message
            and "was already defined" in message
            and "overriding" in message.lower()
        ):
            return False

        # Internal cache invalidation after database changes.
        # This covers e.g.:
        #   Db changed recently, clearing cache expr
        #   Db changed recently, clearing cache lcia
        if message.startswith(
            "Db changed recently, clearing cache "
        ):
            return False

        # Internal MultiLCA/background-cache computation progress.
        if (
            message.startswith("Computing LCA for ")
            and " background acts" in message
        ):
            return False

        return True


class _LCAModellerFormatter(logging.Formatter):
    """
    Simple user-facing formatter for LCA-Modeller messages.

    INFO messages are displayed without an ``[INFO]`` prefix so normal
    execution reads like application output instead of library diagnostics.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)

        if record.levelno >= logging.ERROR:
            return f"ERROR: {message}"

        if record.levelno >= logging.WARNING:
            return f"WARNING: {message}"

        if record.levelno <= logging.DEBUG:
            return f"[debug] {message}"

        return message


_LCA_ALGEBRAIC_NOISE_FILTER = _LCAAlgebraicNoiseFilter()


def _get_lca_modeller_handler() -> logging.Handler:
    """
    Return the console handler managed by LCA-Modeller.

    The marker attribute prevents duplicate handlers if ``set_verbosity()``
    is called several times, which is common in Jupyter notebooks.
    """

    logger = logging.getLogger(_LCA_MODELLER_LOGGER)

    for handler in logger.handlers:
        if getattr(handler, "_lca_modeller_managed", False):
            return handler

    handler = logging.StreamHandler()
    handler._lca_modeller_managed = True  # type: ignore[attr-defined]

    handler.setFormatter(
        _LCAModellerFormatter("%(message)s")
    )

    logger.addHandler(handler)

    # Prevent LCA-Modeller messages from also being printed by the root
    # logger, which would result in duplicated output in notebooks.
    logger.propagate = False

    return handler


def _iter_lca_algebraic_loggers():
    """
    Yield lca-algebraic's logger and any already-created child loggers.
    """

    main_logger = logging.getLogger(_LCA_ALGEBRAIC_LOGGER)
    yield main_logger

    prefix = f"{_LCA_ALGEBRAIC_LOGGER}."

    for name, obj in logging.root.manager.loggerDict.items():
        if (
            name.startswith(prefix)
            and isinstance(obj, logging.Logger)
        ):
            yield obj


def _iter_lca_algebraic_handlers():
    """
    Yield handlers which can directly handle lca-algebraic records.

    We don't modify root-handler levels because doing so would affect
    unrelated libraries.
    """

    seen: set[int] = set()

    for logger in _iter_lca_algebraic_loggers():
        for handler in logger.handlers:
            handler_id = id(handler)

            if handler_id not in seen:
                seen.add(handler_id)
                yield handler


def _iter_filter_handlers():
    """
    Yield handlers on which the lca-algebraic noise filter should be placed.

    In addition to handlers installed directly by lca-algebraic, root
    handlers are included because lca-algebraic records may propagate there.

    Adding the filter to a root handler is safe because the filter itself
    immediately accepts any record whose logger is not ``lca_algebraic``.
    """

    seen: set[int] = set()

    for handler in _iter_lca_algebraic_handlers():
        handler_id = id(handler)

        if handler_id not in seen:
            seen.add(handler_id)
            yield handler

    for handler in logging.getLogger().handlers:
        handler_id = id(handler)

        if handler_id not in seen:
            seen.add(handler_id)
            yield handler


def _set_lca_algebraic_noise_filter(enabled: bool) -> None:
    """
    Enable or disable filtering of known lca-algebraic noise.

    Filtering is enabled for quiet, normal and verbose modes and disabled
    completely in debug mode.
    """

    agb_logger = logging.getLogger(_LCA_ALGEBRAIC_LOGGER)

    # Covers records emitted directly on the main lca_algebraic logger.
    if enabled:
        if _LCA_ALGEBRAIC_NOISE_FILTER not in agb_logger.filters:
            agb_logger.addFilter(_LCA_ALGEBRAIC_NOISE_FILTER)
    else:
        if _LCA_ALGEBRAIC_NOISE_FILTER in agb_logger.filters:
            agb_logger.removeFilter(_LCA_ALGEBRAIC_NOISE_FILTER)

    # Handler filters are required to cover messages emitted by
    # lca_algebraic child loggers and propagated to other handlers.
    for handler in _iter_filter_handlers():
        if enabled:
            if _LCA_ALGEBRAIC_NOISE_FILTER not in handler.filters:
                handler.addFilter(_LCA_ALGEBRAIC_NOISE_FILTER)
        else:
            if _LCA_ALGEBRAIC_NOISE_FILTER in handler.filters:
                handler.removeFilter(_LCA_ALGEBRAIC_NOISE_FILTER)


def set_verbosity(level: Verbosity = "normal") -> None:
    """
    Configure console verbosity for LCA-Modeller and lca-algebraic.

    Parameters
    ----------
    level :
        One of:

        ``"quiet"``
            Show LCA-Modeller warnings/errors and dependency errors only.

        ``"normal"``
            Show LCA-Modeller progress and actionable warnings.
            Hide lca-algebraic INFO output and known implementation noise.

        ``"verbose"``
            Also show lca-algebraic INFO messages, except known
            implementation noise.

        ``"debug"``
            Show all diagnostic output, including the lca-algebraic
            messages normally filtered out.

    Raises
    ------
    ValueError
        If ``level`` is not a supported verbosity level.
    """

    global _CURRENT_VERBOSITY

    valid_levels = {
        "quiet",
        "normal",
        "verbose",
        "debug",
    }

    if level not in valid_levels:
        raise ValueError(
            f"Unknown verbosity level {level!r}. "
            f"Expected one of: {', '.join(sorted(valid_levels))}."
        )

    _CURRENT_VERBOSITY = level

    # ------------------------------------------------------------------
    # LCA-Modeller
    # ------------------------------------------------------------------

    lm_logger = logging.getLogger(_LCA_MODELLER_LOGGER)
    lm_handler = _get_lca_modeller_handler()

    # ------------------------------------------------------------------
    # lca-algebraic
    # ------------------------------------------------------------------

    if level == "quiet":
        lm_level = logging.WARNING
        agb_level = logging.ERROR

    elif level == "normal":
        lm_level = logging.INFO
        agb_level = logging.WARNING

    elif level == "verbose":
        lm_level = logging.INFO
        agb_level = logging.INFO

    else:  # debug
        lm_level = logging.DEBUG
        agb_level = logging.DEBUG

    # Configure LCA-Modeller.
    lm_logger.setLevel(lm_level)
    lm_handler.setLevel(lm_level)

    # Configure lca-algebraic loggers.
    for logger in _iter_lca_algebraic_loggers():
        logger.setLevel(agb_level)

    # If lca-algebraic installed its own handlers, make sure their level
    # doesn't prevent verbose/debug records from being displayed.
    #
    # Do not modify root handlers: that would affect unrelated libraries.
    for handler in _iter_lca_algebraic_handlers():
        handler.setLevel(agb_level)

    # Known lca-algebraic lifecycle/cache messages are hidden everywhere
    # except explicit debug mode.
    _set_lca_algebraic_noise_filter(
        enabled=(level != "debug")
    )


def get_verbosity() -> Verbosity:
    """Return the currently configured LCA-Modeller verbosity level."""

    return _CURRENT_VERBOSITY