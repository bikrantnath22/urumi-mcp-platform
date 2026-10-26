"""Bounded PV inventory pagination; no HTTP, database or deletion."""

from collections.abc import Mapping
from urllib.parse import quote, unquote

from app.store_namespace import StoreNamespace, _opaque
from app.store_volume_recovery import _known_records, identify_store_volume
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeError


PAGE_SIZE = 100
MAX_PAGES = 100
_MAX_CONTINUE = 4096
_BASE = "/api/v1/persistentvolumes?limit=100"


def _invalid():
    raise StoreVolumeError("The volume inventory is incomplete or invalid.")


def _continuation(value):
    if (
        not isinstance(value, str) or len(value) > _MAX_CONTINUE
        or any(not "!" <= char <= "~" for char in value)
    ):
        _invalid()
    return value


def volume_page_path(continuation=""):
    token = _continuation(continuation)
    return _BASE + ("&continue=" + quote(token, safe="") if token else "")


def is_volume_page_path(path):
    if not isinstance(path, str):
        return False
    if path == _BASE:
        return True
    prefix = _BASE + "&continue="
    if not path.startswith(prefix) or len(path) > len(prefix) + 3 * _MAX_CONTINUE:
        return False
    try:
        token = unquote(path[len(prefix):], errors="strict")
        return bool(token) and volume_page_path(token) == path
    except (ValueError, UnicodeError):
        return False


class StoreVolumeInventory:
    """One consistent list snapshot; an empty result is not deletion proof."""

    def __init__(self, identity, *, namespace_uid, known=()):
        if not isinstance(identity, StoreNamespace) or not _opaque(namespace_uid):
            _invalid()
        self._identity = identity
        self._uid = namespace_uid
        self._known = _known_records(identity, namespace_uid, known)
        self._version = None
        self._token = ""
        self._tokens = set()
        self._names, self._uids = set(), set()
        self._matches = {}
        self._pages = 0
        self._complete = False
        self._failed = False

    @property
    def complete(self):
        return self._complete and not self._failed

    @property
    def next_path(self):
        if self._failed or self._complete:
            _invalid()
        return volume_page_path(self._token)

    @property
    def resource_version(self):
        if not self.complete:
            _invalid()
        return self._version

    def result(self):
        if not self.complete:
            _invalid()
        return tuple(
            self._matches[name] for name in STORE_CLAIM_NAMES if name in self._matches
        )

    def accept(self, document):
        try:
            if self._failed or self._complete:
                _invalid()
            self._accept(document)
            return self.complete
        except Exception:
            self._failed = True
            raise

    def _accept(self, document):
        if (
            not isinstance(document, Mapping)
            or document.get("apiVersion") != "v1"
            or document.get("kind") != "PersistentVolumeList"
        ):
            _invalid()
        metadata, items = document.get("metadata"), document.get("items")
        if (
            not isinstance(metadata, Mapping) or not isinstance(items, list)
            or len(items) > PAGE_SIZE or self._pages >= MAX_PAGES
        ):
            _invalid()
        version = metadata.get("resourceVersion")
        token = _continuation(metadata.get("continue", ""))
        remaining = metadata.get("remainingItemCount")
        if (
            not _opaque(version)
            or (self._version is not None and version != self._version)
            or (token and token in self._tokens)
            or (
                "remainingItemCount" in metadata
                and (
                    type(remaining) is not int or remaining < 0
                    or (not token and remaining != 0)
                )
            )
            or (self._pages + 1 == MAX_PAGES and token)
        ):
            _invalid()

        names, uids = set(self._names), set(self._uids)
        matches = dict(self._matches)
        for document in items:
            if not isinstance(document, Mapping):
                _invalid()
            # Typed Kubernetes lists may omit each item's type metadata.
            document = dict(document)
            document.setdefault("apiVersion", "v1")
            document.setdefault("kind", "PersistentVolume")
            binding = identify_store_volume(
                self._identity, namespace_uid=self._uid,
                document=document, known=self._known,
            )
            name, uid = document["metadata"]["name"], document["metadata"]["uid"]
            if name in names or uid in uids:
                _invalid()
            names.add(name)
            uids.add(uid)
            if binding is not None:
                if binding.claim_name in matches or any(
                    item.claim_uid == binding.claim_uid for item in matches.values()
                ):
                    _invalid()
                matches[binding.claim_name] = binding

        self._version, self._token = version, token
        self._names, self._uids, self._matches = names, uids, matches
        self._pages += 1
        if token:
            self._tokens.add(token)
        self._complete = not token
