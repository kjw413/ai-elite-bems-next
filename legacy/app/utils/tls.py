"""TLS verification helpers for outbound HTTP clients."""
from __future__ import annotations

import logging
import os
import ssl
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def _insecure_tls_enabled() -> bool:
    return os.getenv("HTTP_INSECURE_SKIP_VERIFY", "").strip().lower() in _TRUE_VALUES


@lru_cache(maxsize=1)
def _truststore_context() -> Any:
    """Return a truststore SSLContext when available, otherwise httpx default verify=True."""
    if _insecure_tls_enabled():
        logger.warning("HTTP_INSECURE_SKIP_VERIFY is enabled; outbound TLS verification is disabled.")
        return False
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception as exc:
        logger.info("truststore SSLContext unavailable; falling back to default certifi/ssl verification: %s", exc)
        return True


@lru_cache(maxsize=1)
def _inject_truststore() -> bool:
    if _insecure_tls_enabled():
        logger.warning("HTTP_INSECURE_SKIP_VERIFY is enabled; outbound TLS verification is disabled.")
        return False
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except Exception as exc:
        logger.info("truststore injection unavailable; falling back to default requests verification: %s", exc)
        return False


def httpx_verify() -> Any:
    """Value for httpx.Client(verify=...)."""
    return _truststore_context()


def requests_verify() -> bool:
    """Value for requests.get(..., verify=...)."""
    if _insecure_tls_enabled():
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        return False
    _inject_truststore()
    return True
