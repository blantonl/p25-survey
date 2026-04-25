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
    sys.stderr.flush()
    old_fd = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(old_fd, 2)
        os.close(old_fd)
        os.close(devnull)
