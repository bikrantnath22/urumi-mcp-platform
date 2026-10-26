"""Explicit worker entry point; imports and package checks start no services."""

import argparse
import asyncio
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

from app.store_cluster_scope import StoreClusterScope
from app.store_credentials import _EMAIL
from app.store_worker_health import WorkerHealth


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    scope: StoreClusterScope
    admin_email: str
    max_jobs: int = 100
    poll_seconds: float = 1.0

    def __post_init__(self):
        if (
            not isinstance(self.scope, StoreClusterScope)
            or not isinstance(self.admin_email, str) or len(self.admin_email) > 254
            or not _EMAIL.fullmatch(self.admin_email)
            or type(self.max_jobs) is not int or not 1 <= self.max_jobs <= 100
            or type(self.poll_seconds) not in (int, float)
            or not math.isfinite(self.poll_seconds) or not 0.05 <= self.poll_seconds <= 30
        ):
            raise ValueError("Invalid worker configuration.")

    @classmethod
    def from_environment(cls, env):
        enabled = env.get("STORE_WORKER_ENABLED", "false")
        if enabled not in ("true", "false"):
            raise ValueError("Worker enablement must be explicit.")
        if enabled == "false":
            return None
        scope = StoreClusterScope(
            env.get("STORE_PLATFORM_NAMESPACE", ""),
            env.get("STORE_PLATFORM_RELEASE", ""),
        )
        email = env.get("STORE_ADMIN_EMAIL", "")
        jobs = env.get("STORE_WORKER_MAX_JOBS", "100")
        interval = env.get("STORE_WORKER_POLL_SECONDS", "1")
        if (
            not isinstance(email, str) or len(email) > 254 or not _EMAIL.fullmatch(email)
            or not isinstance(jobs, str) or not jobs.isascii()
            or not jobs.isdecimal() or len(jobs) > 3
            or not 1 <= int(jobs) <= 100
            or not isinstance(interval, str) or len(interval) > 16
        ):
            raise ValueError("Invalid worker configuration.")
        try:
            seconds = float(interval)
        except ValueError:
            raise ValueError("Invalid worker poll interval.") from None
        if not math.isfinite(seconds) or not 0.05 <= seconds <= 30:
            raise ValueError("Invalid worker poll interval.")
        return cls(scope, email, int(jobs), seconds)


def check_package(*, profile=False):
    root = "/opt/urumi/charts/store/"
    required = [
        "/usr/local/bin/helm", root + "Chart.yaml", root + "values.yaml",
        root + "values-managed.yaml",
        *[root + "files/" + name for name in (
            "bootstrap.sh", "seed.sh", "keys.php", "media.php", "wordpress-setup.php",
        )],
        *[root + "templates/" + name for name in (
            "mysql.yaml", "wordpress.yaml", "woo-mcp.yaml", "mcp-analytics.yaml",
            "bootstrap-job.yaml", "bootstrap-configmap.yaml", "store-guardrails.yaml", "wordpress-setup.yaml",
            "service-accounts.yaml", "default-network-policies.yaml",
            "woo-mcp-network-policy.yaml", "provisioner-network-policy.yaml",
        )],
    ]
    if profile:
        required.extend((
            "/etc/urumi/store-values.yaml",
            "/var/run/secrets/kubernetes.io/serviceaccount/token",
            "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        ))
    if any(not Path(name).is_file() or not os.access(name, os.R_OK) for name in required):
        raise RuntimeError("The worker package or mounted store profile is unavailable.")
    if not os.access("/usr/local/bin/helm", os.X_OK):
        raise RuntimeError("The worker Helm executable is unavailable.")


def _dependencies():
    # Database configuration is evaluated only after explicit enablement.
    from app.database import Session, engine
    from app.store_execution import StoreWorkerHandlers
    from app.store_kube_connection import KubernetesConnection
    from app.store_worker import run_store_worker
    return SimpleNamespace(
        factory=Session, engine=engine, connection=KubernetesConnection,
        handlers=StoreWorkerHandlers, worker=run_store_worker,
    )


async def _check_database(factory):
    from sqlalchemy import select
    from app.models import Store, StoreOperation, StoreProvisioningSettings, StoreVolume
    async with factory() as db:
        for model in (Store, StoreOperation, StoreVolume, StoreProvisioningSettings):
            await db.execute(select(model).limit(0))
        if await db.scalar(select(StoreProvisioningSettings.id).where(
            StoreProvisioningSettings.id == 1,
        )) != 1:
            raise RuntimeError("Store worker settings are unavailable.")


async def _close(connection, engine):
    async def close_all():
        try:
            if connection is not None:
                async with asyncio.timeout(10):
                    await connection.aclose()
        finally:
            if engine is not None:
                async with asyncio.timeout(10):
                    await engine.dispose()
    task = asyncio.create_task(close_all())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError()


async def serve(config):
    if type(config) is not WorkerConfig:
        raise ValueError("A validated worker configuration is required.")
    check_package(profile=True)
    health = WorkerHealth.from_environment()
    health.clear()
    loop, stop = asyncio.get_running_loop(), asyncio.Event()
    installed = []
    connection = engine = None
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(number, stop.set)
            installed.append(number)
        dependencies = _dependencies()
        engine = dependencies.engine
        connection = dependencies.connection.in_cluster()
        handlers = dependencies.handlers(
            dependencies.factory, connection, config.scope,
            admin_email=config.admin_email,
        )
        await _check_database(dependencies.factory)
        await dependencies.worker(
            dependencies.factory, handlers.work, handlers.finish,
            stop_event=stop, max_jobs=config.max_jobs, poll_seconds=config.poll_seconds,
        )
    finally:
        stop.set()
        try:
            try:
                health.clear()
            finally:
                await _close(connection, engine)
        finally:
            for number in installed:
                loop.remove_signal_handler(number)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the private managed-store worker.")
    parser.add_argument("--check-package", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check_package:
            check_package()
            print("Worker package checked. No services contacted.")
            return 0
        config = WorkerConfig.from_environment(os.environ)
        if config is None:
            print("Store worker disabled.")
            return 0
    except Exception:
        print("Invalid worker configuration or unavailable package.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(serve(config))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Store worker stopped unexpectedly; saved jobs remain recoverable.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
