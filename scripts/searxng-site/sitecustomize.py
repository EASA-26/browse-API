"""Make SearXNG trust the certificates the machine trusts.

Imported automatically by the site module at interpreter startup, before
searx imports httpx, which is the only moment early enough: searx is started
with -m and there is no entry point of ours to run first. This directory is
put on PYTHONPATH by scripts/start-searxng.ps1.

The problem is the one app/tls.py solves for browse-API. The proxy on this
network re-signs TLS with a corporate CA that certifi has never heard of, so
engine initialisation fails -- wikidata first and visibly, others quietly --
and an engine that failed to initialise never responds. Fewer engines
responding is what drops coverage below the floor, marks every answer
degraded, and sends questions to the paid provider that the free one should
have answered.

Best-effort. Without truststore installed the interpreter starts exactly as
it did, because a metasearch that will not start is worse than one whose
engines are thin.
"""
try:
    import truststore
except ImportError:  # pragma: no cover - depends on the venv it runs in
    pass
else:
    truststore.inject_into_ssl()
