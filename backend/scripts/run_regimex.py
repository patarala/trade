"""Start the optional local Trade + Regimex stack with isolated persistent state."""

import sys

from .regimex_stack.diagnostics import doctor
from .regimex_stack.options import arguments
from .regimex_stack.runner import run
from .regimex_stack.state import status


def main(argv=None):
    args = arguments(argv)
    try:
        if args.status:
            return status(args.root)
        if args.doctor:
            return doctor(args.root)
        return run(args)
    except (OSError, ValueError, RuntimeError) as error:
        # These errors contain our own operational messages/paths, never subprocess
        # stderr or database/HTTP payloads. Child logs stay in the private root.
        print(f"Local stack: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
