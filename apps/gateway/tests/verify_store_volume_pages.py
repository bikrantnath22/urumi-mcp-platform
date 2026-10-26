"""Pure page/path checks; no Kubernetes, database or network access."""

from copy import deepcopy
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import store_volume_pages as module
from app.store_claims import StoreClaimIdentity
from app.store_volume_recovery import identify_store_volume
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeError
import verify_store_volumes as data


IDENTITY = data.IDENTITY


def volume(name):
    _, item = data.pair(name)
    item["metadata"].update(name="pv-" + name, uid="volume-" + name)
    item["spec"]["claimRef"]["uid"] = "claim-" + name
    return item


def page(items=(), *, token="", version="42", **metadata):
    return {
        "apiVersion": "v1", "kind": "PersistentVolumeList",
        "metadata": {"resourceVersion": version, "continue": token, **metadata},
        "items": deepcopy(list(items)),
    }


def inventory(**options):
    return module.StoreVolumeInventory(
        IDENTITY, namespace_uid="namespace-uid", **options,
    )


def error(callback):
    try:
        callback()
    except StoreVolumeError:
        return
    raise AssertionError("Expected an inventory validation error.")


def failed(scan, document):
    error(lambda: scan.accept(document))
    assert not scan.complete
    error(scan.result)
    error(lambda: scan.resource_version)
    error(lambda: scan.next_path)


def run():
    for token in ("", "abc_123-==", "a/b+c=xyz", "&watch=true#fragment", "%2F"):
        path = module.volume_page_path(token)
        assert module.is_volume_page_path(path)
        split = urlsplit(path)
        assert split.path == "/api/v1/persistentvolumes"
        query = parse_qs(split.query)
        assert query["limit"] == ["100"]
        assert query.get("continue", [""])[0] == token
        assert set(query) <= {"limit", "continue"}
    for value in (None, 1, "bad token", "bad\nvalue", "é", "a" * 4097):
        error(lambda: module.volume_page_path(value))
    for path in (
        None, "/api/v1/persistentvolumes", "https://other/api/v1/persistentvolumes?limit=100",
        "/api/v1/secrets?limit=100", "/api/v1/persistentvolumes?limit=101",
        "/api/v1/persistentvolumes?limit=100&watch=true",
        "/api/v1/persistentvolumes?limit=100&continue=",
        "/api/v1/persistentvolumes?limit=100&continue=a&continue=b",
        "/api/v1/persistentvolumes?limit=100&continue=%0A",
        "/api/v1/persistentvolumes?limit=100&continue=%2f",
    ):
        assert not module.is_volume_page_path(path)
    print("Fixed page routes encode opaque tokens; broader queries, other endpoints and malformed tokens are rejected")

    mysql, wordpress = (volume(name) for name in STORE_CLAIM_NAMES)
    known = identify_store_volume(
        IDENTITY, namespace_uid="namespace-uid", document=mysql,
    )
    pending = StoreClaimIdentity(
        IDENTITY, "namespace-uid", known.claim_name, known.claim_uid,
    )
    scan = inventory(known=(pending,))
    error(scan.result)
    error(lambda: scan.resource_version)
    original = page([wordpress], token="next/token=")
    before = deepcopy(original)
    assert not scan.accept(original)
    assert original == before and not scan.complete
    error(scan.result)
    assert scan.next_path == module.volume_page_path("next/token=")
    assert scan.accept(page([mysql], remainingItemCount=0))
    result = scan.result()
    assert tuple(item.claim_name for item in result) == STORE_CLAIM_NAMES
    assert scan.resource_version == "42"
    error(lambda: scan.next_path)
    assert scan.result() == result
    print("All pages share one resource version; only complete scans return immutable store identities in fixed order")

    unrelated = volume("wordpress-data")
    unrelated["metadata"].update(name="unrelated", uid="unrelated")
    unrelated["spec"]["claimRef"].update(namespace="another-store", uid="other-uid")
    scan = inventory()
    assert not scan.accept(page([], token="empty-page"))
    assert not scan.accept(page([unrelated], token="last-page"))
    assert scan.accept(page())
    assert scan.result() == ()
    scan = inventory()
    assert scan.accept(page())
    assert scan.result() == ()
    print("Empty and unrelated pages still follow continuation; completed empty inventories remain observations only")

    for mutate in (
        lambda p: p.update(kind="Other"),
        lambda p: p.update(apiVersion="other/v1"),
        lambda p: p.update(metadata=[]),
        lambda p: p.update(items=None),
        lambda p: p["metadata"].update(resourceVersion=""),
        lambda p: p["metadata"].update(continue_=None),
        lambda p: p["metadata"].update(remainingItemCount=True),
        lambda p: p["metadata"].update(remainingItemCount=-1),
        lambda p: p["metadata"].update(remainingItemCount=1),
    ):
        scan = inventory()
        document = page()
        mutate(document)
        if "continue_" in document["metadata"]:
            document["metadata"]["continue"] = document["metadata"].pop("continue_")
        failed(scan, document)
    scan = inventory()
    failed(scan, None)
    scan = inventory()
    failed(scan, page([mysql] * (module.PAGE_SIZE + 1)))
    print("Malformed list responses, invalid counts and oversized pages invalidate the whole scan")

    for issue in ("version", "token", "name", "uid", "claim"):
        scan = inventory()
        assert not scan.accept(page([mysql], token="page-two"))
        second = deepcopy(wordpress)
        next_page = page([second])
        if issue == "version":
            next_page["metadata"]["resourceVersion"] = "43"
        elif issue == "token":
            next_page["metadata"]["continue"] = "page-two"
        elif issue == "name":
            next_page["items"][0]["metadata"]["name"] = mysql["metadata"]["name"]
        elif issue == "uid":
            next_page["items"][0]["metadata"]["uid"] = mysql["metadata"]["uid"]
        else:
            next_page["items"][0]["spec"]["claimRef"]["uid"] = mysql["spec"]["claimRef"]["uid"]
        failed(scan, next_page)
    scan = inventory()
    assert not scan.accept(page([mysql], token="second"))
    second = deepcopy(mysql)
    second["metadata"].update(name="another-mysql", uid="another-volume")
    failed(scan, page([second]))
    print("Version changes, repeated tokens and duplicate PV/PVC identities cannot yield partial success")

    with patch.object(module, "MAX_PAGES", 2):
        scan = inventory()
        assert not scan.accept(page(token="page-two"))
        assert scan.accept(page())
        scan = inventory()
        assert not scan.accept(page(token="page-two"))
        failed(scan, page(token="page-three"))
    scan = inventory()
    assert scan.accept(page([mysql]))
    failed(scan, page([wordpress]))
    scan = inventory(known=(known,))
    changed = deepcopy(mysql)
    changed["metadata"]["uid"] = "replacement"
    failed(scan, page([changed]))
    print("Page-count exhaustion, reuse after completion and changed saved identities fail without cleanup claims")
    def listed(item):
        item = deepcopy(item)
        item.pop("apiVersion")
        item.pop("kind")
        return item

    raw_mysql, raw_wordpress = listed(mysql), listed(wordpress)
    replies = [
        page([raw_wordpress], token="raw-next"),
        page([raw_mysql, listed(unrelated)]),
    ]
    before = deepcopy(replies)
    scan = inventory(known=(pending,))
    assert not scan.accept(replies[0])
    assert scan.accept(replies[1])
    expected = tuple(
        identify_store_volume(
            IDENTITY, namespace_uid="namespace-uid", document=item,
        ) for item in (mysql, wordpress)
    )
    assert scan.result() == expected and replies == before
    for field in ("apiVersion", "kind"):
        item = deepcopy(mysql)
        item.pop(field)
        scan = inventory(known=(known,))
        assert scan.accept(page([item])) and scan.result() == (known,)
    error(lambda: identify_store_volume(
        IDENTITY, namespace_uid="namespace-uid", document=raw_mysql,
    ))
    print("Typed list items may omit type fields; replies stay unchanged and standalone reads stay strict")

    for field, value in (
        ("kind", "Pod"), ("kind", None),
        ("apiVersion", "apps/v1"), ("apiVersion", None),
    ):
        item = deepcopy(raw_mysql)
        item[field] = value
        failed(inventory(), page([item]))
        reply = page([deepcopy(raw_mysql)])
        reply[field] = value
        failed(inventory(), reply)
    for value in (None, [], "invalid"):
        failed(inventory(), page([value]))
    failed(inventory(), page([raw_mysql, raw_mysql]))
    for mutate in (
        lambda item: item["metadata"].update(uid="replacement"),
        lambda item: item["spec"]["claimRef"].update(uid="replacement"),
        lambda item: item["spec"]["claimRef"].update(namespace="another-store"),
    ):
        item = deepcopy(raw_mysql)
        mutate(item)
        failed(inventory(known=(known,)), page([item]))
    print("Explicit type conflicts, invalid lists, duplicates and changed ownership still reject the whole scan")
    print("Volume pagination verification passed. No database or Kubernetes calls made.")


if __name__ == "__main__":
    run()
