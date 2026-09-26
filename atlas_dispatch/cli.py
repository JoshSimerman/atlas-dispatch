"""Console entry point for the ``atlas-dispatch`` command."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    from atlas_dispatch.dispatcher import main as dispatcher_main

    args = list(sys.argv[1:] if argv is None else argv)
    return int(dispatcher_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
