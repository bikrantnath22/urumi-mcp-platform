"""Offline lease lifecycle checks; no database or external service calls."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app import store_job_guard as guard
from app import store_lease_runner as runner
from app.store_job_guard import CheckedStoreJob, StoreJobClaimLost
from app.store_jobs import StoreJobClaim


class Fixture:
    def __init__(self, *, duration=1200, cleanup=False):
        self.now = 100.0
        self.deadline = self.now + duration
        self.lease_until = self.now + 60
        self.renewals = 0
        self.notifications = asyncio.Queue()
        self.fail_after = None
        self.failure = "lost"
        self.live = True
        self.change_identity = False
        now = datetime.now(timezone.utc)
        identity = uuid4()
        self.claim = StoreJobClaim(
            operation_id=uuid4(), store_id=identity, owner_id=1,
            action="delete" if cleanup else "provision",
            generation=1, attempt=1,
            namespace="urumi-store-" + identity.hex,
            release_name="store", name="Fixture", slug="fixture",
            url="http://fixture.localtest.me",
            deadline_at=now + timedelta(seconds=duration),
            checked_at=now, lease_expires_at=now + timedelta(seconds=60),
            lease_token=uuid4(),
        )

    def factory(self):
        raise AssertionError("Offline checks must not open a database.")

    async def require(self, factory, claim, *, for_cleanup=False):
        assert factory == self.factory
        if self.claim.action == "delete" and not for_cleanup:
            raise ValueError("Cleanup required.")
        fields = (
            "operation_id", "store_id", "owner_id", "action", "generation",
            "attempt", "namespace", "release_name", "name", "slug", "url",
            "deadline_at", "lease_token",
        )
        if not self.live or any(
            getattr(claim, key) != getattr(self.claim, key) for key in fields
        ):
            raise StoreJobClaimLost("Fixture ownership changed.")
        stop = self.lease_until
        if not for_cleanup:
            stop = min(stop, self.deadline)
        if stop <= self.now:
            raise StoreJobClaimLost("Fixture allowance expired.")
        return CheckedStoreJob(self.claim, None, for_cleanup, stop)

    async def renew(self, factory, claim):
        assert factory == self.factory
        self.renewals += 1
        if self.fail_after is not None and self.renewals >= self.fail_after:
            if self.failure == "error":
                raise RuntimeError("PRIVATE STORAGE DIAGNOSTIC")
            if self.failure == "stall":
                await asyncio.Future()
            return None
        self.lease_until = self.now + 60
        self.claim = replace(
            self.claim,
            lease_expires_at=self.claim.checked_at
            + timedelta(seconds=self.now - 100 + 60),
        )
        if self.change_identity:
            self.claim = replace(self.claim, owner_id=2)
        self.notifications.put_nowait(self.renewals)
        return self.claim

    async def wait_for_renewal(self, target):
        while await self.notifications.get() < target:
            pass

    async def run(self, work, *, cleanup=False, timeout=10):
        with (
            patch.object(runner, "require_claim", self.require),
            patch.object(runner, "renew_claim", self.renew),
            patch.object(runner, "_RENEW_EVERY_SECONDS", 0.01),
            patch.object(runner, "_RENEW_TIMEOUT_SECONDS", timeout),
            patch.object(guard, "time", SimpleNamespace(monotonic=lambda: self.now)),
        ):
            return await runner.run_with_lease(
                self.factory, self.claim, work, for_cleanup=cleanup,
            )


async def expect_error(expected, operation):
    try:
        await operation
    except expected as error:
        return error
    raise AssertionError("Expected " + expected.__name__)


async def check():
    baseline = asyncio.all_tasks()
    fixture = Fixture()
    original = fixture.claim

    async def normal(claim):
        assert fixture.renewals == 1
        assert claim.lease_token == original.lease_token
        for target in (2, 3, 4):
            fixture.now += 20
            await fixture.wait_for_renewal(target)
        return "saved"

    assert await fixture.run(normal) == "saved"
    assert fixture.now >= 160 and fixture.lease_until > fixture.now
    assert fixture.claim.deadline_at == original.deadline_at
    assert fixture.claim.lease_token == original.lease_token
    assert fixture.claim.attempt == original.attempt
    count = fixture.renewals
    await asyncio.sleep(0.03)
    assert fixture.renewals == count
    print("Immediate and repeated renewal preserve the token, attempt and original deadline")

    for failure in ("lost", "error", "stall"):
        fixture = Fixture()
        fixture.fail_after = 2
        fixture.failure = failure
        stopped = asyncio.Event()

        async def waiting(claim):
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                stopped.set()

        error = await expect_error(
            runner.StoreLeaseStopped, fixture.run(waiting, timeout=0.2),
        )
        assert stopped.is_set()
        assert "PRIVATE" not in str(error)
    print("Lost, unavailable and stalled renewal stop and await work without leaking diagnostics")

    fixture = Fixture(duration=30)
    stopped = asyncio.Event()

    async def expired(claim):
        fixture.now = fixture.deadline + 1
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    await expect_error(runner.StoreLeaseStopped, fixture.run(expired))
    assert stopped.is_set()

    fixture = Fixture(duration=-1, cleanup=True)

    async def cleanup(claim):
        fixture.now += 20
        await fixture.wait_for_renewal(2)
        return "cleanup"

    assert await fixture.run(cleanup, cleanup=True) == "cleanup"
    print("Provisioning stops at its deadline; overdue cleanup still needs a live lease")

    fixture = Fixture()
    fixture.change_identity = True

    async def forbidden(claim):
        raise AssertionError("The callback must not start.")

    await expect_error(runner.StoreLeaseStopped, fixture.run(forbidden))

    fixture = Fixture()

    async def lost_at_end(claim):
        fixture.live = False
        return "unverified"

    await expect_error(StoreJobClaimLost, fixture.run(lost_at_end))

    fixture = Fixture()

    async def failed(claim):
        raise ValueError("Fixture action failed.")

    error = await expect_error(ValueError, fixture.run(failed))
    assert str(error) == "Fixture action failed."
    print("Changed identities and lost final ownership reject success; action errors are preserved")

    for repeated in (False, True):
        fixture = Fixture()
        started, cleaning = asyncio.Event(), asyncio.Event()
        finished, resume = asyncio.Event(), asyncio.Event()

        async def cancellable(claim):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await resume.wait()
                finished.set()

        task = asyncio.create_task(fixture.run(cancellable))
        try:
            await started.wait()
            task.cancel()
            await cleaning.wait()
            if repeated:
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
            resume.set()
            await expect_error(asyncio.CancelledError, task)
            assert finished.is_set()
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert not (asyncio.all_tasks() - baseline)
    print("Single and repeated cancellation await cleanup; no worker tasks remain")
    print("Lease runner verification passed. No live services were called.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 15))
