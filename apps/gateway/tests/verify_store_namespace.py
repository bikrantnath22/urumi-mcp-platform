"""Verify namespace ownership and deletion preconditions without external calls."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from uuid import UUID, uuid4

if not Path("app/store_namespace.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

from app.store_namespace import (
    MANAGED_BY_LABEL, OWNER_ID_LABEL, RELEASE_ANNOTATION, STORE_ID_LABEL,
    StoreNamespace, StoreNamespaceError, namespace_for,
)


class NamespaceTests(unittest.TestCase):
    def setUp(self):
        self.identity = StoreNamespace(uuid4(), 98401)
        self.document = self.identity.manifest()
        self.uid = str(uuid4())
        self.document["metadata"].update(uid=self.uid, resourceVersion="opaque-v7")
        self.document["status"] = {"phase": "Active"}

    def claim(self, **changes):
        fields = dict(
            store_id=self.identity.store_id, owner_id=self.identity.owner_id,
            namespace=self.identity.name, release_name="store",
            name="Fixture tea", slug="assam-tea",
        )
        fields.update(changes)
        return SimpleNamespace(**fields)

    def test_claim_identity(self):
        self.assertEqual(namespace_for(self.claim()), self.identity)
        for changes in (
            {"store_id": str(uuid4())}, {"store_id": UUID(int=0)},
            {"owner_id": True}, {"owner_id": 0}, {"owner_id": -1},
            {"owner_id": 2147483648}, {"namespace": "store"},
            {"namespace": "platform"}, {"namespace": "urumi-store-" + uuid4().hex},
            {"release_name": "another-release"},
        ):
            with self.subTest(changes=changes), self.assertRaises(StoreNamespaceError):
                namespace_for(self.claim(**changes))
        with self.assertRaises(FrozenInstanceError):
            self.identity.owner_id = 1

    def test_manifests_are_independent(self):
        first = self.identity.manifest()
        first["metadata"]["labels"][OWNER_ID_LABEL] = "someone-else"
        self.assertEqual(self.identity.labels[OWNER_ID_LABEL], "98401")
        self.assertEqual(
            self.identity.check(self.document, expected_uid=self.uid),
            (self.uid, "opaque-v7"),
        )
        self.document["metadata"]["labels"]["unrelated-label"] = "allowed"
        self.identity.check(self.document, expected_uid=self.uid)

    def test_wrong_objects_and_ownership_are_rejected(self):
        changes = (
            ("apiVersion", "v2"), ("kind", "Secret"), ("metadata", None),
            ("status", "invalid"),
        )
        for key, value in changes:
            document = deepcopy(self.document)
            document[key] = value
            with self.subTest(key=key), self.assertRaises(StoreNamespaceError):
                self.identity.check(document)
        for key, value in (
            ("name", "store"), ("name", "platform"), ("namespace", "platform"),
            ("labels", {}), ("annotations", {}), ("uid", ""),
            ("resourceVersion", 12), ("resourceVersion", ""),
        ):
            document = deepcopy(self.document)
            document["metadata"][key] = value
            with self.subTest(key=key), self.assertRaises(StoreNamespaceError):
                self.identity.check(document)
        for key in (MANAGED_BY_LABEL, STORE_ID_LABEL, OWNER_ID_LABEL):
            document = deepcopy(self.document)
            document["metadata"]["labels"][key] = "different"
            with self.subTest(label=key), self.assertRaises(StoreNamespaceError):
                self.identity.check(document)
        document = deepcopy(self.document)
        document["metadata"]["annotations"][RELEASE_ANNOTATION] = "different"
        with self.assertRaises(StoreNamespaceError):
            self.identity.check(document)

    def test_deletion_requires_uid_and_version(self):
        options = self.identity.delete_options(self.document, expected_uid=self.uid)
        self.assertEqual(
            options["preconditions"],
            {"uid": self.uid, "resourceVersion": "opaque-v7"},
        )
        self.assertEqual(options["propagationPolicy"], "Foreground")
        for expected in (None, "", str(uuid4())):
            with self.subTest(uid=expected), self.assertRaises(StoreNamespaceError):
                self.identity.delete_options(self.document, expected_uid=expected)
        replacement = deepcopy(self.document)
        replacement["metadata"]["uid"] = str(uuid4())
        with self.assertRaises(StoreNamespaceError):
            self.identity.delete_options(replacement, expected_uid=self.uid)
        self.document["metadata"]["resourceVersion"] = "new-opaque-version"
        self.assertEqual(
            self.identity.delete_options(self.document, expected_uid=self.uid)
            ["preconditions"]["resourceVersion"],
            "new-opaque-version",
        )

    def test_terminating_namespaces_cannot_be_used_for_provisioning(self):
        self.document["metadata"]["deletionTimestamp"] = "2026-10-17T00:00:00Z"
        with self.assertRaises(StoreNamespaceError):
            self.identity.check(self.document)
        self.identity.delete_options(self.document, expected_uid=self.uid)
        del self.document["metadata"]["deletionTimestamp"]
        self.document["status"]["phase"] = "Terminating"
        with self.assertRaises(StoreNamespaceError):
            self.identity.check(self.document)
        self.identity.check(self.document, allow_terminating=True)
        with self.assertRaises(StoreNamespaceError):
            self.identity.check(self.document, allow_terminating="yes")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(NamespaceTests)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("Namespace verification passed. No database or Kubernetes calls made.")
