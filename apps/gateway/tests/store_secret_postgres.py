"""Real PostgreSQL Secret-stage checks; all Kubernetes HTTP is mocked."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import re
import ssl
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import store_jobs as jobs
from app import store_secret_stage as stage
from app.models import SecurityEvent, Store, StoreOperation
from app.store_cluster_scope import StoreClusterScope
from app.store_credentials import StoreCredentials
from app.store_guarded_kubernetes import StoreKubernetesStopped
from app.store_job_guard import StoreJobClaimLost
from app.store_kube_connection import KubernetesAPIError, KubernetesConnection
from app.store_namespace import StoreNamespaceError, namespace_for
from app.store_secret import StoreSecretError
from app.store_secret_binding import SecretBindingUnavailable


async def verify_secret_stage(factory, fresh, change, snapshot, unlocked):
    async with factory() as db:
        schema = await db.scalar(select(func.current_schema()))
        assert re.fullmatch(r"urumi_guard_test_[0-9a-f]{32}", schema)
    scope = StoreClusterScope("platform", "platform")
    tls = ssl.create_default_context()

    class Peer:
        def __init__(self, claim):
            self.claim = claim
            self.identity = namespace_for(claim)
            self.namespace = scope.namespace_manifest(self.identity)
            self.namespace["metadata"].update(uid=str(uuid4()), resourceVersion="17")
            self.namespace["status"] = {"phase": "Active"}
            self.secret = None
            self.calls, self.read_bindings = [], []
            self.after_create = self.after_read = None
            self.started = self.resume = None
            self.closed = False

        @property
        def namespace_item(self):
            return "/api/v1/namespaces/" + self.identity.name

        @property
        def collection(self):
            return self.namespace_item + "/secrets"

        @property
        def item(self):
            return self.collection + "/store-secrets"

        @property
        def writes(self):
            return sum(method == "POST" for method, _ in self.calls)

        async def __call__(self, request):
            await unlocked(self.claim)
            async with factory.begin() as db:
                saved = await db.scalar(
                    select(Store).where(Store.id == self.claim.store_id)
                    .with_for_update(nowait=True)
                )
                saved_namespace_uid = saved.namespace_uid
                saved_secret_uid = saved.credentials_secret_uid
            method, path = request.method, request.url.path
            self.calls.append((method, path))
            if method == "GET" and path == self.namespace_item:
                return httpx.Response(
                    200 if self.namespace is not None else 404,
                    json=deepcopy(self.namespace or {}),
                )
            if method == "GET" and path == self.item:
                self.read_bindings.append(saved_secret_uid)
                result = deepcopy(self.secret)
                if self.after_read is not None:
                    callback, self.after_read = self.after_read, None
                    await callback()
                return httpx.Response(
                    200 if result is not None else 404, json=result or {},
                )
            if method == "POST" and path == self.collection:
                assert saved_namespace_uid == self.namespace["metadata"]["uid"]
                assert saved_secret_uid is None
                if self.secret is not None:
                    return httpx.Response(409, json={})
                self.secret = json.loads(request.content)
                self.secret["metadata"].update(
                    uid=str(uuid4()), resourceVersion="5",
                )
                if self.after_create is not None:
                    await self.after_create()
                if self.started is not None:
                    self.started.set()
                    await self.resume.wait()
                return httpx.Response(201, json=deepcopy(self.secret))
            raise AssertionError("Unexpected mocked Kubernetes request.")

    async def fresh_peer(*, seconds=1200):
        peer = Peer(await fresh(seconds=seconds))
        await change(
            Store, peer.claim.store_id,
            namespace_uid=peer.namespace["metadata"]["uid"],
        )
        return peer

    async def prepare(peer, claim=None):
        raw = KubernetesConnection(
            lambda: "fixture.worker.signature", tls,
            transport=httpx.MockTransport(peer),
        )
        try:
            return await stage.prepare_secret(
                factory, claim or peer.claim, raw, scope,
                admin_email="owner@example.test",
            )
        finally:
            await raw.aclose()
            peer.closed = raw._client.is_closed

    async def state(peer):
        async with factory() as db:
            store = await db.get(Store, peer.claim.store_id)
            operation = await db.get(StoreOperation, peer.claim.operation_id)
            events = (await db.scalars(
                select(SecurityEvent).order_by(SecurityEvent.id)
            )).all()
            return store, operation, events

    async def rejected(expected, operation, *, uncertain=None):
        try:
            await operation
        except expected as error:
            assert "fixture-private-diagnostic" not in str(error)
            if uncertain is not None:
                assert error.write_may_have_applied is uncertain
            return
        raise AssertionError(f"Expected {expected.__name__}.")

    peer = await fresh_peer()
    before = await snapshot()
    uid = await prepare(peer)
    assert isinstance(uid, str) and uid == peer.secret["metadata"]["uid"]
    store, operation, events = await state(peer)
    assert store.credentials_secret_uid == uid and store.status == "Provisioning"
    assert operation.status == "Running" and operation.deadline_at == peer.claim.deadline_at
    assert len(events) == 1 and events[0].action == "store.credentials_bound"
    assert peer.read_bindings == [None, uid]
    after = await snapshot()
    assert before["store_operations"] == after["store_operations"]
    credentials = StoreCredentials.from_secret_data(peer.identity, peer.secret["data"])
    saved_text = json.dumps(after, default=str)
    assert all(value not in saved_text for value in credentials._values.values())
    with patch.object(
        StoreCredentials, "generate",
        side_effect=AssertionError("Replay must reuse credentials."),
    ):
        assert await prepare(peer) == uid
    assert await snapshot() == after and peer.writes == 1 and peer.closed
    print("Real guards release database locks before HTTP; the UID commits before bound readback")
    print("Replay preserves credentials, deadline and one audit event; stored rows contain no passwords")

    peer = await fresh_peer()
    old_claim = peer.claim

    async def expire_lease():
        await change(
            StoreOperation, old_claim.operation_id,
            lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
        )

    peer.after_create = expire_lease
    await rejected(StoreKubernetesStopped, prepare(peer), uncertain=True)
    assert peer.secret is not None and peer.writes == 1
    assert (await state(peer))[0].credentials_secret_uid is None
    calls = len(peer.calls)
    await rejected(StoreJobClaimLost, prepare(peer))
    assert len(peer.calls) == calls
    recovered = await jobs.claim_next(factory)
    assert recovered is not None and recovered.lease_token != old_claim.lease_token
    assert recovered.deadline_at == old_claim.deadline_at
    peer.claim, peer.after_create = recovered, None
    with patch.object(
        StoreCredentials, "generate",
        side_effect=AssertionError("Recovery must reuse the accepted Secret."),
    ):
        assert await prepare(peer) == peer.secret["metadata"]["uid"]
    assert peer.writes == 1 and len((await state(peer))[2]) == 1
    print("An accepted Secret survives lease loss; a replacement worker binds it without recreating it")

    peer = await fresh_peer()
    normal_flush = AsyncSession.flush

    async def fail_after_flush(db, *args, **kwargs):
        await normal_flush(db, *args, **kwargs)
        raise SQLAlchemyError("fixture-private-diagnostic")

    with patch.object(AsyncSession, "flush", fail_after_flush):
        await rejected(SecretBindingUnavailable, prepare(peer))
    store, _, events = await state(peer)
    assert peer.secret is not None and store.credentials_secret_uid is None
    assert not events and peer.writes == 1
    saved = deepcopy(peer.secret)
    with patch.object(
        StoreCredentials, "generate",
        side_effect=AssertionError("Storage recovery must reuse the Secret."),
    ):
        assert await prepare(peer) == saved["metadata"]["uid"]
    assert peer.secret == saved and peer.writes == 1
    assert len((await state(peer))[2]) == 1
    print("Failed binding rolls back the UID and event; retry reuses the existing Secret")

    peer = await fresh_peer()
    peer.started, peer.resume = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(prepare(peer))
    try:
        await asyncio.wait_for(peer.started.wait(), 5)
        saved = deepcopy(peer.secret)
        task.cancel()
        await rejected(asyncio.CancelledError, task)
        assert peer.closed and (await state(peer))[0].credentials_secret_uid is None
        old_claim = peer.claim
        assert await jobs.release_claim(factory, old_claim, retry_after_seconds=0)
        recovered = await jobs.claim_next(factory)
        assert recovered is not None and recovered.lease_token != old_claim.lease_token
        assert recovered.deadline_at == old_claim.deadline_at
        peer.claim, peer.started = recovered, None
        with patch.object(
            StoreCredentials, "generate",
            side_effect=AssertionError("Cancellation recovery must reuse the Secret."),
        ):
            assert await prepare(peer) == saved["metadata"]["uid"]
        assert peer.secret == saved and peer.writes == 1
    finally:
        peer.resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    print("Cancellation drains HTTP before release; recovery preserves the accepted credentials")

    peer = await fresh_peer()
    uid = await prepare(peer)
    original = deepcopy(peer.secret)
    for replacement in (None, deepcopy(original)):
        if replacement is not None:
            replacement["metadata"]["uid"] = str(uuid4())
        peer.secret = replacement
        before = await snapshot()
        with patch.object(
            StoreCredentials, "generate",
            side_effect=AssertionError("A bound Secret must never be regenerated."),
        ):
            await rejected(
                StoreSecretError if replacement is None else KubernetesAPIError,
                prepare(peer),
            )
        assert await snapshot() == before and peer.writes == 1
    peer.secret = original

    async def replace_binding():
        await change(
            Store, peer.claim.store_id, credentials_secret_uid=str(uuid4()),
        )

    peer.after_read = replace_binding
    await rejected(StoreKubernetesStopped, prepare(peer), uncertain=False)
    store, _, events = await state(peer)
    assert store.credentials_secret_uid != uid
    assert len(events) == 1 and peer.writes == 1
    print("Missing/replaced Secrets and concurrent binding changes stop continuation without rotation")

    peer = await fresh_peer()
    original_bind = stage.bind_store_secret

    async def remove_after_commit(*args, **kwargs):
        uid = await original_bind(*args, **kwargs)
        peer.secret = None
        return uid

    with patch.object(stage, "bind_store_secret", remove_after_commit):
        await rejected(StoreSecretError, prepare(peer))
    store, _, events = await state(peer)
    assert store.credentials_secret_uid is not None and len(events) == 1
    await rejected(StoreSecretError, prepare(peer))
    assert peer.writes == 1
    print("A failed readback keeps the committed UID and prevents credential replacement")

    peer = Peer(await fresh())
    await rejected(StoreNamespaceError, prepare(peer))
    assert not peer.calls
    peer = await fresh_peer(seconds=-1)
    before = await snapshot()
    await rejected(StoreJobClaimLost, prepare(peer))
    assert not peer.calls and await snapshot() == before
    print("Missing namespace ownership and overdue provisioning send no HTTP")
    print("PostgreSQL Secret stage verification passed. Live accounts/stores/settings were unchanged.")
    print("No Kubernetes, model or WordPress calls were made.")


if __name__ == "__main__":
    raise SystemExit("Run: uv run python tests/verify_store_job_guard.py")
