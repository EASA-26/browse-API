"""Trust the certificates the operating system trusts.

This network intercepts TLS: the corporate proxy terminates the connection
and re-signs it with its own CA. Windows trusts that CA -- browsers and
Invoke-RestMethod work -- but httpx pins verification to certifi's bundle,
which has never heard of it, so every outbound request from this service
failed with "unable to get local issuer certificate". The SerpApi
fall-through was configured correctly and could not make a single call.

Pointing OpenSSL at the Windows store instead is not enough on its own.
Python 3.13 turned on VERIFY_X509_STRICT, and the proxy's CA does not mark
Basic Constraints critical the way RFC 5280 requires, so OpenSSL rejects it
a second time for an entirely different reason -- which is the more
misleading of the two failures, because the certificate was found.

truststore side-steps both by handing verification to the platform itself,
which is what the browser does. Verification stays real: hostname, chain and
expiry are all still checked, by the same code path the rest of the machine
uses. Nothing here disables anything.

Best-effort by design. Where truststore is absent, verification is left
exactly as it was rather than weakened, and the log says which is in force.
"""
import logging

logger = logging.getLogger(__name__)


def trust_system_certificates() -> bool:
    """Verify TLS against the OS trust store. True when that is now in force."""
    try:
        import truststore
    except ImportError:
        logger.warning(
            "truststore is not installed: TLS is verified against certifi, which "
            "does not include a TLS-intercepting proxy's CA. Outbound HTTPS will "
            "fail on such a network. pip install truststore"
        )
        return False

    truststore.inject_into_ssl()
    logger.info("TLS verification delegated to the operating system trust store")
    return True
