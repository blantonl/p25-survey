"""C-level stderr suppression for noisy library banners.

GNU Radio + gr-osmosdr write banner lines directly to fd 2 from C++. They
ignore Python's `sys.stderr` redirection. The only way to silence them is
to dup2 over fd 2 itself.

Use sparingly — only around library calls whose output is known noise.
Real Python errors written via `sys.stderr.write` will *also* be suppressed
inside the context, so keep the window tight.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def suppress_c_stderr() -> Iterator[None]:
    """Silence both C-level fd 2 writes AND Python `sys.stderr` writes.

    GNU Radio + op25 emit chatter from two layers:
      - C++ fprintf(stderr, ...) writes via libc to fd 2.
      - Python `sys.stderr.write(...)` (e.g. p25_demodulator.py's
        "Using two-stage decimator..." line) writes via TextIOWrapper.

    A dup2 over fd 2 alone catches the C++ side but doesn't reliably
    redirect Python's stderr, so we also swap `sys.stderr` for a /dev/null
    file object during the context.
    """
    sys.stderr.flush()
    old_fd = os.dup(2)
    devnull_w = os.open(os.devnull, os.O_WRONLY)
    saved_py_stderr = sys.stderr
    py_devnull = open(os.devnull, "w")  # noqa: SIM115 — closed in finally
    try:
        os.dup2(devnull_w, 2)
        sys.stderr = py_devnull
        yield
    finally:
        try:
            py_devnull.flush()
            py_devnull.close()
        except Exception:
            pass
        sys.stderr = saved_py_stderr
        os.dup2(old_fd, 2)
        os.close(old_fd)
        os.close(devnull_w)
