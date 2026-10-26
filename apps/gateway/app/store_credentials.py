"""Generate and validate private store credentials without I/O."""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
import re
import secrets
import string
from types import MappingProxyType

from app.store_namespace import StoreNamespace


STORE_SECRET_KEYS = (
    "DB_ROOT_PASSWORD", "DB_PASSWORD",
    "WP_ADMIN_USER", "WP_ADMIN_PASSWORD", "WP_ADMIN_EMAIL",
    "WC_RW_KEY", "WC_RW_SECRET", "WC_RO_KEY", "WC_RO_SECRET",
    "WP_MEDIA_USER", "WP_MEDIA_APP_PASSWORD",
)
_PASSWORD_KEYS = ("DB_ROOT_PASSWORD", "DB_PASSWORD", "WP_ADMIN_PASSWORD")
_FORMATS = {
    **dict.fromkeys(_PASSWORD_KEYS, r"[A-Za-z0-9_-]{43}"),
    "WC_RW_KEY": r"ck_[0-9a-f]{40}",
    "WC_RW_SECRET": r"cs_[0-9a-f]{40}",
    "WC_RO_KEY": r"ck_[0-9a-f]{40}",
    "WC_RO_SECRET": r"cs_[0-9a-f]{40}",
    "WP_MEDIA_APP_PASSWORD": r"[A-Za-z0-9]{24}",
}
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_\x60{|}~-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)


class StoreCredentialError(ValueError):
    """Credential input is invalid; messages contain no credential values."""


@dataclass(frozen=True, slots=True)
class StoreCredentials:
    identity: StoreNamespace
    _values: Mapping = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.identity, StoreNamespace):
            raise StoreCredentialError("A valid store identity is required.")
        if (
            not isinstance(self._values, Mapping)
            or set(self._values) != set(STORE_SECRET_KEYS)
        ):
            raise StoreCredentialError("The store credential fields are invalid.")
        values = dict(self._values)
        if any(
            not isinstance(value, str) or not 1 <= len(value) <= 254
            for value in values.values()
        ):
            raise StoreCredentialError("The store credential values are invalid.")
        suffix = self.identity.store_id.hex
        if (
            values["WP_ADMIN_USER"] not in ("admin", "admin_" + suffix)
            or values["WP_MEDIA_USER"] != "media_" + suffix
            or not _EMAIL.fullmatch(values["WP_ADMIN_EMAIL"])
            or any(
                not re.fullmatch(pattern, values[key])
                for key, pattern in _FORMATS.items()
            )
            or len({values[key] for key in _FORMATS}) != len(_FORMATS)
        ):
            raise StoreCredentialError("The store credential values are invalid.")
        object.__setattr__(self, "_values", MappingProxyType(values))

    def __repr__(self):
        return "StoreCredentials(<redacted>)"

    @classmethod
    def generate(cls, identity, *, admin_email):
        """Call only when a new, unbound store Secret may be created."""
        if not isinstance(identity, StoreNamespace):
            raise StoreCredentialError("A valid store identity is required.")
        values = {key: secrets.token_urlsafe(32) for key in _PASSWORD_KEYS}
        values.update({
            "WP_ADMIN_USER": "admin",
            "WP_ADMIN_EMAIL": admin_email,
            "WC_RW_KEY": "ck_" + secrets.token_hex(20),
            "WC_RW_SECRET": "cs_" + secrets.token_hex(20),
            "WC_RO_KEY": "ck_" + secrets.token_hex(20),
            "WC_RO_SECRET": "cs_" + secrets.token_hex(20),
            "WP_MEDIA_USER": "media_" + identity.store_id.hex,
            "WP_MEDIA_APP_PASSWORD": "".join(
                secrets.choice(string.ascii_letters + string.digits)
                for _ in range(24)
            ),
        })
        return cls(identity, values)

    def secret_data(self):
        """Return a fresh Kubernetes Secret data mapping; keep it out of logs."""
        return {
            key: base64.b64encode(self._values[key].encode("ascii")).decode("ascii")
            for key in STORE_SECRET_KEYS
        }

    @classmethod
    def from_secret_data(cls, identity, data):
        """Validate stored data and preserve its original credential values."""
        if not isinstance(data, Mapping) or set(data) != set(STORE_SECRET_KEYS):
            raise StoreCredentialError("The stored credential fields are invalid.")
        values = {}
        try:
            for key in STORE_SECRET_KEYS:
                encoded = data[key]
                if not isinstance(encoded, str) or not 1 <= len(encoded) <= 512:
                    raise ValueError()
                raw = base64.b64decode(encoded, validate=True)
                if base64.b64encode(raw).decode("ascii") != encoded:
                    raise ValueError()
                values[key] = raw.decode("ascii")
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            raise StoreCredentialError(
                "The stored credential encoding is invalid.",
            ) from None
        return cls(identity, values)
