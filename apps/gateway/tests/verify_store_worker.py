"""Offline worker checks; database and provisioning operations are fixtures."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import time
from unittest.mock import patch
from uuid import uuid4

from app import store_lease_runner as lease
from app import store_worker as worker
from app.store_job_guard import CheckedStoreJob, StoreJobClaimLost
from app.store_jobs import StoreJobClaim


class Fixture:
    def __init__(self, *, number=3, limit=2, actions=None):
        self.stop = asyncio.Event()
        self.limit = limit
        self.claims = []
        self.status = {}
        self.started, self.ended, self.allow = {}, {}, {}
        self.completions, self.releases = [], []
        self.all_done = asyncio.Event()
        self.fail_work = False
        self.claim_fail_once = False
        self.stop_after_claim = False
        self.stop_after_release = False
        self.release_fail = False

        for index in range(number):
            now = datetime.now(timezone.utc)
            identity = uuid4()
            kind = actions[index] if actions else "provision"
            claim = StoreJobClaim(
                operation_id=uuid4(), store_id=identity, owner_id=index + 1,
                action="delete" if kind == "delete" else "provision",
                generation=1, attempt=1,
                namespace="urumi-store-" + identity.hex, release_name="store",
                name="Fixture", slug="fixture" + str(index),
                url="http://fixture.localtest.me",
                deadline_at=now + timedelta(
                    seconds=-1 if kind == "overdue" else 1200,
                ),
                checked_at=now, lease_expires_at=now + timedelta(seconds=60),
                lease_token=uuid4(),
            )
            key = claim.operation_id
            self.claims.append(claim)
            self.status[key] = "Pending"
            self.started[key] = asyncio.Event()
            self.ended[key] = asyncio.Event()
            self.allow[key] = asyncio.Event()

    def factory(self):
        raise AssertionError("Offline checks must not open a database.")

    async def claim(self, factory):
        assert factory == self.factory
        if self.claim_fail_once:
            self.claim_fail_once = False
            raise RuntimeError("PRIVATE QUEUE DIAGNOSTIC")
        if sum(value == "Running" for value in self.status.values()) >= self.limit:
            return None
        for claim in self.claims:
            key = claim.operation_id
            if self.status[key] == "Pending":
                self.status[key] = "Running"
                if self.stop_after_claim:
                    self.stop.set()
                return claim
        return None

    async def release(self, factory, claim, *, retry_after_seconds):
        assert factory == self.factory and retry_after_seconds == 5
        key = claim.operation_id
        assert not self.started[key].is_set() or self.ended[key].is_set()
        self.releases.append(key)
        owned = self.status[key] == "Running"
        if owned:
            self.status[key] = "Deferred"
        if self.stop_after_release:
            self.stop.set()
        if self.release_fail:
            raise RuntimeError("PRIVATE RELEASE DIAGNOSTIC")
        return owned

    async def require(self, factory, claim, *, for_cleanup=False):
        assert factory == self.factory
        if self.status[claim.operation_id] != "Running":
            raise StoreJobClaimLost("Fixture ownership was lost.")
        if not for_cleanup and claim.deadline_reached:
            raise StoreJobClaimLost("Fixture deadline elapsed.")
        return CheckedStoreJob(claim, None, for_cleanup, time.monotonic() + 60)

    async def renew(self, factory, claim):
        assert factory == self.factory
        if self.status[claim.operation_id] != "Running":
            return None
        return replace(
            claim, lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )

    async def work(self, claim, *, for_cleanup):
        key = claim.operation_id
        self.started[key].set()
        try:
            await self.allow[key].wait()
            if self.fail_work:
                raise ValueError("PRIVATE ACTION DIAGNOSTIC")
            return for_cleanup
        finally:
            self.ended[key].set()

    async def finish(self, claim, result, *, for_cleanup):
        assert self.ended[claim.operation_id].is_set()
        assert result == for_cleanup
        assert for_cleanup == (claim.action == "delete" or claim.deadline_reached)
        self.status[claim.operation_id] = "Done"
        self.completions.append(claim.operation_id)
        if len(self.completions) == len(self.claims):
            self.all_done.set()

    @contextmanager
    def installed(self):
        with (
            patch.object(worker, "claim_next", self.claim),
            patch.object(worker, "release_claim", self.release),
            patch.object(lease, "require_claim", self.require),
            patch.object(lease, "renew_claim", self.renew),
        ):
            yield

    async def run(self, **options):
        with self.installed():
            return await worker.run_store_worker(
                self.factory, self.work, self.finish,
                stop_event=self.stop, poll_seconds=0.05, **options,
            )


async def drain(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def check():
    baseline = asyncio.all_tasks()

    fixture = Fixture()
    first, second, third = [claim.operation_id for claim in fixture.claims]
    task = asyncio.create_task(fixture.run())
    try:
        await asyncio.gather(
            fixture.started[first].wait(), fixture.started[second].wait(),
        )
        assert not fixture.started[third].is_set()
        fixture.limit = 3
        await fixture.started[third].wait()
        for event in fixture.allow.values():
            event.set()
        await fixture.all_done.wait()
        fixture.stop.set()
        await task
        assert len(fixture.completions) == 3
        assert sorted(fixture.releases) == sorted([first, second, third])
        assert all(value == "Done" for value in fixture.status.values())
    finally:
        await drain(task)
    print("Parallel jobs and increased scheduler capacity apply without restarting")

    fixture = Fixture(number=1, limit=1)
    fixture.stop_after_claim = True
    await fixture.run()
    key = fixture.claims[0].operation_id
    assert not fixture.started[key].is_set()
    assert fixture.releases == [key]
    print("Shutdown after claiming releases an unstarted job without running its handler")

    fixture = Fixture(limit=100)
    first, second, third = [claim.operation_id for claim in fixture.claims]
    task = asyncio.create_task(fixture.run(max_jobs=2))
    try:
        await asyncio.gather(
            fixture.started[first].wait(), fixture.started[second].wait(),
        )
        assert not fixture.started[third].is_set()
        fixture.allow[first].set()
        await fixture.started[third].wait()
        fixture.stop.set()
        await task
        assert all(
            not fixture.started[key].is_set() or fixture.ended[key].is_set()
            for key in (first, second, third)
        )
    finally:
        await drain(task)
    print("Local task capacity stays bounded; graceful shutdown drains work before release")

    fixture = Fixture(number=1)
    fixture.fail_work = True
    fixture.claim_fail_once = True
    fixture.release_fail = True
    fixture.stop_after_release = True
    for event in fixture.allow.values():
        event.set()
    with patch.object(worker._LOG, "warning") as warning:
        await fixture.run()
        assert warning.call_count >= 3
        assert "PRIVATE" not in str(warning.call_args_list)
    assert not fixture.completions and len(fixture.releases) == 1
    print("Queue, action and release failures never report success or leak raw diagnostics")

    fixture = Fixture(actions=["provision", "delete", "overdue"], limit=100)
    for event in fixture.allow.values():
        event.set()
    task = asyncio.create_task(fixture.run())
    try:
        await fixture.all_done.wait()
        fixture.stop.set()
        await task
    finally:
        await drain(task)
    print("Provisioning, deletion and overdue cleanup receive the correct mode")

    fixture = Fixture(number=1)
    started, cleaning, resume = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_cleanup(claim, *, for_cleanup):
        key = claim.operation_id
        fixture.started[key].set()
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await resume.wait()
            fixture.ended[key].set()

    fixture.work = slow_cleanup
    task = asyncio.create_task(fixture.run())
    try:
        await started.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not fixture.releases
        resume.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Worker cancellation was swallowed.")
        assert fixture.releases == [fixture.claims[0].operation_id]
    finally:
        resume.set()
        await drain(task)
    print("Repeated cancellation waits for request cleanup before releasing the job")

    fixture = Fixture()
    for options in (
        {"max_jobs": 0}, {"max_jobs": 101}, {"max_jobs": True},
        {"poll_seconds": 0}, {"poll_seconds": float("nan")},
        {"poll_seconds": float("inf")}, {"poll_seconds": True},
        {"stop_event": None}, {"work": None}, {"finish": None},
    ):
        arguments = {
            "session_factory": fixture.factory,
            "work": fixture.work, "finish": fixture.finish,
            "stop_event": fixture.stop,
        }
        arguments.update(options)
        try:
            await worker.run_store_worker(**arguments)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid worker configuration was accepted.")

    assert not (asyncio.all_tasks() - baseline)
    print("Invalid configuration is rejected; no worker tasks remain")
    print("Worker verification passed. No live services were called.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 15))
