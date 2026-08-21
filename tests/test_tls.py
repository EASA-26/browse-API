"""TLS trust delegation — see app/tls.py."""
import builtins

from app.config import Settings
from app.tls import trust_system_certificates


def test_delegates_to_the_platform_when_truststore_is_there() -> None:
    injected: list[bool] = []

    class Fake:
        @staticmethod
        def inject_into_ssl() -> None:
            injected.append(True)

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "truststore":
            return Fake
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    builtins.__import__ = fake_import  # type: ignore[assignment]
    try:
        assert trust_system_certificates() is True
    finally:
        builtins.__import__ = real_import  # type: ignore[assignment]
    assert injected == [True]


def test_a_missing_truststore_leaves_verification_alone(caplog) -> None:  # type: ignore[no-untyped-def]
    # The one thing this must never do is fall back to not checking
    # certificates. Absent truststore, certifi stays in force and the log says
    # so -- a broken outbound call is better than an unverified one.
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "truststore":
            raise ImportError("no truststore here")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    builtins.__import__ = fake_import  # type: ignore[assignment]
    try:
        assert trust_system_certificates() is False
    finally:
        builtins.__import__ = real_import  # type: ignore[assignment]
    assert "pip install truststore" in caplog.text


def test_it_is_on_by_default_and_can_be_turned_off() -> None:
    assert Settings(_env_file=None).system_trust_store is True
    assert Settings(_env_file=None, system_trust_store=False).system_trust_store is False
