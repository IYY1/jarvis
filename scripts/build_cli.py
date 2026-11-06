#!/usr/bin/env python3
"""Build and install the native CLI in this checkout; never starts a service."""
if __package__:
    from .native_build import build
else:
    from native_build import build


def main():
    build(__doc__, [("cli", "jarvis")],
          "run-jarvis now uses the native CLI. JARVIS_CLI=python selects the compatibility client.")


if __name__ == "__main__":
    main()
