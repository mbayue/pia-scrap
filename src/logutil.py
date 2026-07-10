"""Central logging setup for pia_scrap.

Replaces ad-hoc ``print()`` calls with the standard :mod:`logging` module while
keeping output behaviour identical for callers/tests:

* The package logger emits to **stdout** (not stderr) using a ``%(message)s``
  formatter, so existing ``capsys``-based assertions on ``[info]``/``[warn]``/
  ``[debug]`` text keep working.
* The handler reads ``sys.stdout`` at *emit* time so test capture fixtures that
  swap ``sys.stdout`` (e.g. ``pytest``'s ``capsys``) still receive the output.
* Default level is ``DEBUG``; callers gate noisy output themselves (e.g. an
  ``if self.debug_dump:`` guard) before calling ``logger.debug(...)``.
"""

import logging
import sys

_PACKAGE_LOGGER_NAME = "pia_scrap"


class _StdoutStreamHandler(logging.StreamHandler):
    """StreamHandler bound to the *current* ``sys.stdout`` at emit time.

    ``logging.StreamHandler`` captures the stream object at construction, which
    breaks under pytest's ``capsys`` (it replaces ``sys.stdout`` afterwards).
    Re-binding here keeps emitted text inside the active capture fixture.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def _configure_package_logger() -> logging.Logger:
    logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
    if not logger.handlers:
        handler = _StdoutStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    return logger


_configure_package_logger()


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of the ``pia_scrap`` package logger."""
    return logging.getLogger(f"{_PACKAGE_LOGGER_NAME}.{name}")
