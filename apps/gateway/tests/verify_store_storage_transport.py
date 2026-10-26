"""Storage transport checks using mock HTTP; no live cluster access."""

import asyncio
from pathlib import Path
import ssl
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.store_kube_connection import KubernetesAPIError, KubernetesConnection


async def check():
    calls = []
    response = {"status": 200, "document": {"kind": "fixture"}}

    def handler(request):
        calls.append(request)
        assert str(request.url).startswith("https://kubernetes.default.svc/")
        assert request.method == "GET" and not request.content
        return httpx.Response(response["status"], json=response["document"])

    connection = KubernetesConnection(
        lambda: "fixture.storage.signature", ssl.create_default_context(),
        transport=httpx.MockTransport(handler),
    )
    namespace = "urumi-store-" + "1" * 32
    claims = f"/api/v1/namespaces/{namespace}/persistentvolumeclaims"
    volumes = "/api/v1/persistentvolumes"
    targets = [
        claims + "/data-mysql-0", claims + "/wordpress-data",
        volumes + "/pvc-12345678-abcd-1234-abcd-123456789abc",
        volumes + "/pv.example",
    ]
    try:
        for target in targets:
            before = len(calls)
            assert await connection.request("GET", target) == response["document"]
            assert len(calls) == before + 1
            assert calls[-1].url.raw_path.decode("ascii") == target

        forbidden = [
            (method, target, None)
            for method in ("POST", "PUT", "PATCH", "DELETE")
            for target in targets
        ]
        forbidden += [
            ("GET", target, None) for target in (
                claims, volumes, claims + "/another-claim",
                claims.replace(namespace, "store") + "/wordpress-data",
                claims.replace(namespace, "platform") + "/wordpress-data",
                claims.replace("1" * 32, "0" * 32) + "/wordpress-data",
                volumes + "/", volumes + "/../namespaces/platform",
                volumes + "/pv%2Fother", volumes + "/Uppercase",
                volumes + "/" + "x" * 64,
                volumes + "/" + ".".join(["x" * 63] * 4),
                volumes + "/pv?watch=1", volumes + "/pv/status",
                "https://example.test/api/v1/persistentvolumes/pv",
            )
        ]
        forbidden += [("GET", target, {}) for target in targets]
        for method, target, body in forbidden:
            before = len(calls)
            try:
                await connection.request(method, target, body=body)
            except ValueError:
                pass
            else:
                raise AssertionError("An unsafe storage request was accepted.")
            assert len(calls) == before
        print("Fixed store PVCs and named PVs accept GET; unbounded lists, writes and unsafe paths are blocked")

        for target in targets[:3]:
            response["status"] = 404
            before = len(calls)
            assert await connection.request("GET", target) is None
            assert len(calls) == before + 1
            for status in (301, 403, 429, 500):
                response.update(status=status, document={"private": "diagnostic"})
                before = len(calls)
                try:
                    await connection.request("GET", target)
                except KubernetesAPIError as error:
                    assert error.status_code == status
                    assert not error.write_may_have_applied
                    assert "diagnostic" not in str(error)
                else:
                    raise AssertionError("An unsuccessful read was accepted.")
                assert len(calls) == before + 1
        print("Missing resources remain explicit; denied and failed reads return safe errors without retries")
    finally:
        await connection.aclose()
    assert connection._client.is_closed
    print("Storage transport verification passed. No Kubernetes permissions or resources changed.")


if __name__ == "__main__":
    asyncio.run(check())
