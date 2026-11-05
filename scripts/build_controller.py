#!/usr/bin/env python3
"""Build the Rust background controller and its dictation text helper.

Never changes or starts a service."""
if __package__:
    from .native_build import build
else:
    from native_build import build


def main():
    build(__doc__, [("text-input", "jarvis-type"), ("controller", "jarvis-controller")],
          "Run it directly, or select MURMUR_CONTROLLER=rust with the existing daemon launcher.")


if __name__ == "__main__":
    main()
