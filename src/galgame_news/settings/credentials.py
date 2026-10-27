"""Injectable keyring credential storage with redacted value objects."""

from __future__ import annotations

from typing import Any, Literal, Protocol


CredentialName = Literal["brave_api_key", "x_bearer_token", "twitterapi_api_key", "socialdata_api_key", "archive_app_password"]
_SERVICE_NAME = "galgame-news-toolbox"
_NAMES: set[str] = {"brave_api_key", "x_bearer_token", "twitterapi_api_key", "socialdata_api_key", "archive_app_password"}
_ALIASES = {
    "BRAVE_SEARCH_API_KEY": "brave_api_key",
    "X_BEARER_TOKEN": "x_bearer_token",
    "TWITTERAPI_KEY": "twitterapi_api_key",
    "TWITTERAPI_IO_KEY": "twitterapi_api_key",
    "SOCIALDATA_API_KEY": "socialdata_api_key",
}


class CredentialBackend(Protocol):
    """Small keyring seam; implementations must not persist to settings JSON."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class InMemoryCredentialBackend:
    """An intentionally tiny backend for tests and ephemeral integrations."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self._values.pop((service_name, username), None)

    def __repr__(self) -> str:
        return "InMemoryCredentialBackend(<redacted>)"

    def __getstate__(self) -> dict[str, Any]:
        raise TypeError("credential objects cannot be pickled")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("credential objects cannot be pickled")


class KeyringCredentialBackend:
    """Adapter around the optional ``keyring`` package.

    Importing this module never imports keyring.  The dependency is resolved
    only when an adapter is instantiated, preserving headless CLI imports.
    """

    def __init__(self, keyring_module: Any | None = None) -> None:
        if keyring_module is None:
            try:
                import keyring as keyring_module  # type: ignore[no-redef]
            except ImportError as exc:
                raise RuntimeError(
                    "keyring is required for desktop credential storage; install the desktop extra"
                ) from exc
        self._keyring = keyring_module

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._keyring.get_password(service_name, username)

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._keyring.set_password(service_name, username, password)

    def delete_password(self, service_name: str, username: str) -> None:
        self._keyring.delete_password(service_name, username)

    def __repr__(self) -> str:
        return "KeyringCredentialBackend(<keyring>)"

    def __getstate__(self) -> dict[str, Any]:
        raise TypeError("credential objects cannot be pickled")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("credential objects cannot be pickled")


class CredentialBundle:
    """Read-only runtime credentials whose repr and state serialization redact values."""

    __slots__ = ("_brave_api_key", "_x_bearer_token", "_twitterapi_api_key", "_socialdata_api_key")

    def __init__(
        self,
        brave_api_key: str | None,
        x_bearer_token: str | None,
        twitterapi_api_key: str | None = None,
        socialdata_api_key: str | None = None,
    ) -> None:
        self._brave_api_key = brave_api_key
        self._x_bearer_token = x_bearer_token
        self._twitterapi_api_key = twitterapi_api_key
        self._socialdata_api_key = socialdata_api_key

    @property
    def brave_api_key(self) -> str | None:
        return self._brave_api_key

    @property
    def x_bearer_token(self) -> str | None:
        return self._x_bearer_token

    @property
    def twitterapi_api_key(self) -> str | None:
        return self._twitterapi_api_key

    @property
    def socialdata_api_key(self) -> str | None:
        return self._socialdata_api_key

    def __repr__(self) -> str:
        return "CredentialBundle(brave_api_key=<redacted>, x_bearer_token=<redacted>, twitterapi_api_key=<redacted>, socialdata_api_key=<redacted>)"

    __str__ = __repr__

    def __getstate__(self) -> dict[str, str]:
        raise TypeError("credential objects cannot be pickled")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("credential objects cannot be pickled")


class CredentialStore:
    """Store the supported Brave/X secrets through an injected backend only."""

    def __init__(
        self,
        backend: CredentialBackend | None = None,
        *,
        service_name: str = _SERVICE_NAME,
    ) -> None:
        self._backend = backend if backend is not None else KeyringCredentialBackend()
        self._service_name = service_name

    @staticmethod
    def _canonical_name(name: str) -> CredentialName:
        canonical = _ALIASES.get(name, name)
        if canonical not in _NAMES:
            raise ValueError("unsupported credential name")
        return canonical  # type: ignore[return-value]

    def get(self, name: str) -> str | None:
        canonical = self._canonical_name(name)
        return self._backend.get_password(self._service_name, canonical)

    def set(self, name: str, value: str) -> None:
        canonical = self._canonical_name(name)
        if not isinstance(value, str) or not value:
            raise ValueError("credential value must be a non-empty string")
        self._backend.set_password(self._service_name, canonical, value)

    def delete(self, name: str) -> None:
        canonical = self._canonical_name(name)
        self._backend.delete_password(self._service_name, canonical)

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def set_brave_api_key(self, value: str) -> None:
        self.set("brave_api_key", value)

    def get_brave_api_key(self) -> str | None:
        return self.get("brave_api_key")

    def set_x_bearer_token(self, value: str) -> None:
        self.set("x_bearer_token", value)

    def get_x_bearer_token(self) -> str | None:
        return self.get("x_bearer_token")

    def set_twitterapi_api_key(self, value: str) -> None:
        self.set("twitterapi_api_key", value)

    def get_twitterapi_api_key(self) -> str | None:
        return self.get("twitterapi_api_key")

    def set_socialdata_api_key(self, value: str) -> None:
        self.set("socialdata_api_key", value)

    def get_socialdata_api_key(self) -> str | None:
        return self.get("socialdata_api_key")

    def read(self) -> CredentialBundle:
        return CredentialBundle(
            self.get_brave_api_key(),
            self.get_x_bearer_token(),
            self.get_twitterapi_api_key(),
            self.get_socialdata_api_key(),
        )

    def __repr__(self) -> str:
        return "CredentialStore(<redacted>)"

    def __getstate__(self) -> dict[str, Any]:
        raise TypeError("credential objects cannot be pickled")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("credential objects cannot be pickled")


class KeyringCredentialStore(CredentialStore):
    """Convenience store that lazily constructs the optional keyring adapter."""

    def __init__(
        self,
        *,
        keyring_module: Any | None = None,
        service_name: str = _SERVICE_NAME,
    ) -> None:
        super().__init__(
            KeyringCredentialBackend(keyring_module=keyring_module),
            service_name=service_name,
        )
