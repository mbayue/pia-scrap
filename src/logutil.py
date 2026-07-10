"""Central logging setup for pia_scrap.

Replaces ad-hoc ``print()`` calls with the standard :mod:`logging` module while
keeping output behaviour identical for callers/tests:

* The package logger emits to **stdout** (not stderr) using a ``%(message)s``
  formatter, so existing ``capsys``-based assertions on ``[info]``/``[warn]``/
  ``[debug]`` text keep working.
* The handler writes through ``tqdm.write()`` instead of a raw stream write.
  ``tqdm`` keeps its progress bar on-screen with bare ``\\r`` (carriage return)
  redraws and no trailing newline; a plain ``stream.write()`` from a log call
  lands wherever the bar's cursor last stopped, so warnings/info lines appear
  glued onto the end of the progress bar instead of starting their own line.
  ``tqdm.write()`` clears any active bars, writes the message on a clean line,
  then lets them redraw -- and falls back to a normal write when no bar is
  active, so this is a no-op cost outside of fetch loops.
* ``tqdm.write()`` reads ``sys.stdout`` at call time, so test capture fixtures
  that swap ``sys.stdout`` (e.g. ``pytest``'s ``capsys``) still receive output.
* Default level is ``DEBUG``; callers gate noisy output themselves (e.g. an
  ``if self.debug_dump:`` guard) before calling ``logger.debug(...)``.
"""

import logging
import sys

from tqdm import tqdm

_PACKAGE_LOGGER_NAME = "pia_scrap"


class _TqdmAwareStreamHandler(logging.StreamHandler):
    """StreamHandler that writes through ``tqdm.write()`` instead of raw I/O.

    See the module docstring for why: a plain ``stream.write()`` corrupts an
    active ``tqdm`` progress bar's line, and ``tqdm.write()`` is the library's
    documented way to print alongside a running bar without that corruption.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record), file=sys.stdout)
        except Exception:  # noqa: BLE001 - match logging.Handler's own error policy
            self.handleError(record)


def _configure_package_logger() -> logging.Logger:
    logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
    if not logger.handlers:
        handler = _TqdmAwareStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    return logger


_configure_package_logger()


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of the ``pia_scrap`` package logger."""
    return logging.getLogger(f"{_PACKAGE_LOGGER_NAME}.{name}")
