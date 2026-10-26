"""Private Helm observations; these never authorize work or declare readiness."""

from dataclasses import dataclass
import json
import math

from app.store_helm_plan import StoreHelmPlan
from app.store_namespace import StoreNamespace


class StoreReleaseError(ValueError):
    """The release cannot be safely matched to this store."""


@dataclass(frozen=True, slots=True)
class StoreRelease:
    identity: StoreNamespace
    state: str
    bootstrap_seconds: int | None = None
    revision: int = 1
    managed_bootstrap: bool = False


# Leave room for credentials and other chart data under the 12-Secret quota.
MAX_RELEASE_REVISIONS = 8


def _invalid():
    raise StoreReleaseError("The stored Helm release could not be verified.")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _number(value):
    result = float(value)
    if not math.isfinite(result):
        _invalid()
    return result


def _decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 1024 * 1024:
        _invalid()
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs,
            parse_constant=lambda value: _invalid(), parse_float=_number,
        )
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise StoreReleaseError(
            "The stored Helm release could not be verified.",
        ) from None


def _contains(actual, expected):
    if type(expected) is dict:
        return type(actual) is dict and all(
            key in actual and _contains(actual[key], value)
            for key, value in expected.items()
        )
    return type(actual) is type(expected) and actual == expected


def inspect_store_release(list_output, status_output, plan):
    """Inspect fixed list/status JSON under a separately checked live claim.

    Helm 3 status omits chart metadata and release labels. The list supplies
    chart identity; status supplies saved values. Extra profile values remain
    private and are not returned. Actual workloads still need readiness checks.
    """
    if not isinstance(plan, StoreHelmPlan) or not isinstance(plan.identity, StoreNamespace):
        _invalid()
    listing = _decode(list_output)
    if type(listing) is not list or len(listing) > 1:
        _invalid()
    if not listing:
        if status_output is not None:
            _invalid()
        return StoreRelease(plan.identity, "absent")

    item = listing[0]
    if type(item) is not dict or any(
        item.get(key) != value for key, value in {
            "name": plan.identity.release_name,
            "namespace": plan.identity.name,
            "chart": "store-0.1.0",
            "app_version": "1.0",
        }.items()
    ):
        _invalid()
    revision = item.get("revision")
    if (
        type(revision) is not str
        or revision not in tuple(str(number) for number in range(1, MAX_RELEASE_REVISIONS + 1))
    ):
        _invalid()
    states = {"deployed", "pending-install", "pending-upgrade", "failed"}
    initial = item.get("status")
    if type(initial) is not str or initial not in states:
        _invalid()

    release = _decode(status_output)
    if type(release) is not dict or (
        release.get("name") != plan.identity.release_name
        or release.get("namespace") != plan.identity.name
        or type(release.get("version")) is not int
        or release["version"] != int(revision)
        or type(release.get("info")) is not dict
    ):
        _invalid()
    state = release["info"].get("status")
    if type(state) is not str or state not in states:
        _invalid()
    if state != initial and initial not in ("pending-install", "pending-upgrade"):
        _invalid()

    values = release.get("config")
    expected = plan.values()
    # This was fixed at installation; recovery must not replace it with the
    # smaller remaining time observed after a restart.
    expected.pop("bootstrap")
    if not _contains(values, expected):
        _invalid()
    bootstrap = values.get("bootstrap")
    if type(bootstrap) is not dict:
        _invalid()
    seconds = bootstrap.get("activeDeadlineSeconds")
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        _invalid()
    managed = bootstrap.get("managedJob", False)
    if type(managed) is not bool or (int(revision) > 1 and not managed):
        _invalid()
    return StoreRelease(plan.identity, state, seconds, int(revision), managed)
