"""Directory-plugin entry point for Hermes Agent."""

from .hermes_lark import register

# Public surface consumed by the Hermes directory-plugin loader.
__all__ = ["register"]
