from __future__ import annotations

import os
import ssl


def build_ssl_context() -> ssl.SSLContext:
    if _bool_env("AI_INVESTING_SSL_NO_VERIFY", False):
        return ssl._create_unverified_context()

    ca_bundle = os.getenv("AI_INVESTING_CA_BUNDLE", "").strip()
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)

    return ssl.create_default_context()


def tls_help_message() -> str:
    if _bool_env("AI_INVESTING_SSL_NO_VERIFY", False):
        return (
            "TLS verification is disabled by AI_INVESTING_SSL_NO_VERIFY=1. "
            "Use this only for local paper testing."
        )
    ca_bundle = os.getenv("AI_INVESTING_CA_BUNDLE", "").strip()
    if ca_bundle:
        return (
            "Using CA bundle from AI_INVESTING_CA_BUNDLE. "
            "Verify that the file exists and contains the required issuer certificates."
        )
    return (
        "If you are on macOS with the python.org build, install the bundled certificates "
        "or set AI_INVESTING_CA_BUNDLE to a valid PEM bundle. "
        "As a last resort for local paper testing only, set AI_INVESTING_SSL_NO_VERIFY=1."
    )


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
