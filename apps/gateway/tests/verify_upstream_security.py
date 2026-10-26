"""Run with `uv run python tests/verify_upstream_security.py`.

No database, real credentials, external network calls or live account changes.
Fake DNS/HTTP tests and a disposable localhost TLS server exercise the policy.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.upstream_security import (
    DEFAULT_INTERNAL_URLS,
    DestinationPolicy,
    PinnedUpstreamTransport,
    UpstreamPolicyError,
    parse_endpoint,
    policy_failure,
    resolve_addresses,
)


class DestinationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.policy = DestinationPolicy()
        self.public_dns = AsyncMock(return_value=["93.184.216.34", "2606:4700:4700::1111"])

    async def test_project_services_require_exact_endpoint_and_service_network(self):
        for url in DEFAULT_INTERNAL_URLS:
            destination = await self.policy.prepare(url, resolver=AsyncMock(return_value=["10.43.8.12"]))
            self.assertEqual(destination.addresses, ("10.43.8.12",))
            with self.assertRaises(UpstreamPolicyError):
                await self.policy.prepare(url, resolver=AsyncMock(return_value=["169.254.169.254"]))
            with self.assertRaises(UpstreamPolicyError):
                await self.policy.prepare(url, resolver=AsyncMock(return_value=["93.184.216.34"]))
            for invalid in [url.replace("/mcp", "/admin"), url.replace(":8000", ":443"), url.replace("http:", "https:")]:
                with self.assertRaises(UpstreamPolicyError):
                    await self.policy.prepare(invalid, resolver=self.public_dns)

    async def test_unregistered_internal_endpoints_and_public_http_are_blocked(self):
        urls = [
            "http://127.0.0.1:8003/mcp", "https://127.0.0.1/mcp",
            "https://localhost/mcp", "https://localhost.localdomain/mcp",
            "https://kubernetes.default.svc/mcp", "https://postgres.platform.svc.cluster.local/mcp",
            "https://10.43.0.1/mcp", "https://192.168.1.1/mcp", "https://172.20.0.2/mcp",
            "http://mcp.example.com/mcp", "https://mcp.example.com:8443/mcp",
            "https://2130706433/mcp", "https://0x7f000001/mcp",
            "https://[::1]/mcp", "https://[fe80::1%25eth0]/mcp",
            "https://metadata.google.internal/mcp",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(UpstreamPolicyError):
                await self.policy.prepare(url, resolver=self.public_dns)
        self.public_dns.assert_not_awaited()

    async def test_metadata_and_special_addresses_are_blocked_even_if_allowlisted(self):
        addresses = ["169.254.169.254", "169.254.170.2", "100.100.100.200", "168.63.129.16",
                     "0.0.0.0", "224.0.0.1", "255.255.255.255", "fd00:ec2::254",
                     "::", "ff02::1", "fe80::1", "::ffff:169.254.169.254",
                     "64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::1"]
        for value in addresses:
            host = f"[{value}]" if ":" in value else value
            url = f"http://{host}:8000/mcp"
            policy = DestinationPolicy(internal_urls=[url], internal_cidrs=["0.0.0.0/1", "128.0.0.0/1", "::/1", "8000::/1"])
            with self.subTest(address=value), self.assertRaises(UpstreamPolicyError):
                await policy.prepare(url)

    async def test_dns_private_mixed_and_noncanonical_ipv4_answers_are_blocked(self):
        for answers in [["127.0.0.1"], ["10.43.0.1"], ["100.64.0.1"], ["169.254.169.254"],
                        ["fd00::1"], ["93.184.216.34", "::1"], ["93.184.216.34", "10.0.0.1"],
                        ["93.184.216.34", "fd00::1"], ["not-an-address"], []]:
            with self.subTest(answers=answers), self.assertRaises(UpstreamPolicyError):
                await self.policy.prepare("https://mcp.example.com/mcp", resolver=AsyncMock(return_value=answers))
        for spelling in ["127.1", "0177.0.0.1", "0x7f.0.0.1"]:
            with self.subTest(spelling=spelling), self.assertRaises(UpstreamPolicyError):
                await self.policy.prepare(f"https://{spelling}/mcp", resolver=AsyncMock(return_value=["127.0.0.1"]))

    async def test_valid_external_https_and_numeric_public_ips(self):
        destination = await self.policy.prepare("https://MCP.Example.com:443/mcp", resolver=self.public_dns)
        self.assertEqual(destination.endpoint.host, "mcp.example.com")
        self.assertEqual(destination.addresses[0], "93.184.216.34")
        numeric = await self.policy.prepare("https://93.184.216.34/mcp", resolver=self.public_dns)
        self.assertEqual(numeric.addresses, ("93.184.216.34",))
        ipv6 = await self.policy.prepare("https://[2606:4700:4700::1111]/mcp")
        self.assertEqual(ipv6.addresses, ("2606:4700:4700::1111",))

    async def test_strict_url_syntax(self):
        urls = ["file:///etc/passwd", "ftp://example.com/mcp", "https://user:secret@example.com/mcp",
                "https://example.com/mcp?key=secret", "https://example.com/mcp?",
                "https://example.com/mcp#fragment", "https://example.com/mcp#",
                "https://example.com\\@127.0.0.1/mcp", "https://%31%32%37.0.0.1/mcp",
                "https://example.com:99999/mcp", "https://example.com/a\nb", "https://bad_host/mcp",
                "https://[::1/mcp", "https:///mcp"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(UpstreamPolicyError):
                parse_endpoint(url)

    async def test_local_development_exception_is_explicit_and_still_endpoint_scoped(self):
        url = "http://127.0.0.1:8085/mcp"
        policy = DestinationPolicy(internal_urls=[url], internal_cidrs=["127.0.0.1/32"])
        self.assertEqual((await policy.prepare(url)).addresses, ("127.0.0.1",))
        for other in ["http://127.0.0.1:8003/mcp", "http://127.0.0.1:8085/admin"]:
            with self.assertRaises(UpstreamPolicyError):
                await policy.prepare(other)
        with self.assertRaises(UpstreamPolicyError):
            await policy.prepare("https://mcp.example.com/mcp", resolver=AsyncMock(return_value=["127.0.0.1"]))

    async def test_dns_failure_empty_answers_and_timeout_fail_closed(self):
        with patch('app.upstream_security.asyncio.get_running_loop') as loop:
            loop.return_value.getaddrinfo = AsyncMock(side_effect=socket.gaierror('fixture'))
            with self.assertRaisesRegex(UpstreamPolicyError, 'DNS'):
                await resolve_addresses('example.com', 443)
            loop.return_value.getaddrinfo = AsyncMock(side_effect=TimeoutError)
            with self.assertRaisesRegex(UpstreamPolicyError, 'DNS'):
                await resolve_addresses('example.com', 443)

    async def test_transport_pins_ip_and_preserves_host_tls_name_and_body(self):
        destination = await self.policy.prepare("https://mcp.example.com/mcp", resolver=self.public_dns)
        seen = []
        async def handler(request):
            seen.append(request)
            self.assertEqual(request.url.host, "93.184.216.34")
            self.assertEqual(request.headers['Host'], 'mcp.example.com')
            self.assertEqual(request.extensions['sni_hostname'], 'mcp.example.com')
            self.assertNotIn('target', request.extensions)
            self.assertEqual(request.headers['Authorization'], 'Bearer fixture-only')
            self.assertEqual(await request.aread(), b'{"fixture":true}')
            return httpx.Response(200, json={'ok': True})
        transport = PinnedUpstreamTransport(destination, transport=httpx.MockTransport(handler))
        # If DNS changes after validation, it must not be consulted again by HTTPX.
        with patch('socket.getaddrinfo', side_effect=AssertionError('Unexpected second DNS resolution')):
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.post('https://mcp.example.com/mcp',
                    headers={'Authorization':'Bearer fixture-only','Host':'wrong.example'},
                    content=b'{"fixture":true}', extensions={'target':b'http://127.0.0.1/admin'})
                self.assertEqual(response.json(), {'ok':True})
                self.assertEqual(str(response.request.url), 'https://mcp.example.com/mcp')
        self.assertEqual(len(seen), 1)

    async def test_redirects_are_rejected_even_if_the_client_follows_them(self):
        destination = await self.policy.prepare("https://mcp.example.com/mcp", resolver=self.public_dns)
        for status in [301,302,303,307,308]:
            for target in ['http://169.254.169.254/latest/meta-data/', '/other', '/mcp']:
                calls=[]
                def handler(request):
                    calls.append(request)
                    return httpx.Response(status, headers={'Location':target})
                transport=PinnedUpstreamTransport(destination,transport=httpx.MockTransport(handler))
                async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
                    with self.subTest(status=status,target=target), self.assertRaisesRegex(UpstreamPolicyError,'redirects'):
                        await client.post('https://mcp.example.com/mcp',content=b'fixture')
                self.assertEqual(len(calls),1)

    async def test_transport_blocks_new_origins_paths_and_tunnel_methods_before_io(self):
        destination=await self.policy.prepare("https://mcp.example.com/mcp",resolver=self.public_dns)
        sender=AsyncMock(return_value=httpx.Response(200))
        transport=PinnedUpstreamTransport(destination,transport=httpx.MockTransport(sender))
        async with httpx.AsyncClient(transport=transport) as client:
            for url in ['https://other.example.com/mcp','https://mcp.example.com/admin',
                        'http://mcp.example.com/mcp','https://mcp.example.com:8443/mcp']:
                with self.subTest(url=url),self.assertRaises(UpstreamPolicyError):
                    await client.get(url)
            with self.assertRaises(UpstreamPolicyError):
                await client.request('CONNECT','https://mcp.example.com/mcp')
        sender.assert_not_awaited()

    async def test_connection_fallback_never_retries_after_write_or_read_failure(self):
        destination=await self.policy.prepare("https://mcp.example.com/mcp",resolver=self.public_dns)
        calls=[]
        async def connect_then_succeed(request):
            calls.append(request.url.host)
            if len(calls)==1: raise httpx.ConnectError('fixture connect failure')
            return httpx.Response(200)
        transport=PinnedUpstreamTransport(destination,transport=httpx.MockTransport(connect_then_succeed))
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertEqual((await client.post('https://mcp.example.com/mcp')).status_code,200)
        self.assertEqual(len(calls),2)
        for error_type in [httpx.ReadError,httpx.WriteError,httpx.ReadTimeout,httpx.RemoteProtocolError]:
            sender=AsyncMock(side_effect=error_type('fixture failure'))
            transport=PinnedUpstreamTransport(destination,transport=httpx.MockTransport(sender))
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(error_type):
                    await client.post('https://mcp.example.com/mcp',content=b'fixture')
            self.assertEqual(sender.await_count,1)

    async def test_real_tls_connection_uses_pinned_ip_and_verifies_original_hostname(self):
        # A disposable localhost HTTPS server verifies the actual HTTPX/HTTPCore
        # connection, certificate verification, SNI, Host and response streaming.
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'mcp.fixture.invalid')])
        now=datetime.now(timezone.utc)
        certificate=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('mcp.fixture.invalid')]),critical=False)
            .sign(key,hashes.SHA256()))
        received=[];server_names=[]
        async def handle(reader,writer):
            try:
                headers=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),5)
                received.append(headers.decode('ascii'))
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        with tempfile.TemporaryDirectory(prefix='.upstream-tls-test-',dir=Path.cwd()) as folder:
            certfile=Path(folder)/'fixture-cert.pem';keyfile=Path(folder)/'fixture-key.pem'
            certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(certfile,keyfile)
            server_context.set_servername_callback(lambda connection,name,context:server_names.append(name))
            client_context=ssl.create_default_context(cafile=str(certfile))
            server=await asyncio.start_server(handle,'127.0.0.1',0,ssl=server_context)
            try:
                port=server.sockets[0].getsockname()[1]
                url=f'https://mcp.fixture.invalid:{port}/mcp'
                policy=DestinationPolicy(internal_urls=[url],internal_cidrs=['127.0.0.1/32'])
                destination=await policy.prepare(url,resolver=AsyncMock(return_value=['127.0.0.1']))
                transport=PinnedUpstreamTransport(destination,transport=httpx.AsyncHTTPTransport(
                    verify=client_context,trust_env=False,retries=0))
                original=socket.getaddrinfo
                def numeric_only(host,*args,**kwargs):
                    self.assertIn(host,['127.0.0.1',b'127.0.0.1'])
                    return original(host,*args,**kwargs)
                with patch('socket.getaddrinfo',side_effect=numeric_only):
                    async with httpx.AsyncClient(transport=transport,timeout=5,trust_env=False) as client:
                        response=await client.get(url)
                        self.assertEqual(response.json(),{'ok':True})
                        self.assertEqual(str(response.request.url),url)
                self.assertEqual(server_names,['mcp.fixture.invalid'])
                self.assertIn(f'Host: mcp.fixture.invalid:{port}',received[0])
            finally:
                server.close()
                await server.wait_closed()

    async def test_only_safe_policy_messages_escape_mcp_task_groups(self):
        reason=UpstreamPolicyError('Upstream destination is blocked by gateway policy.')
        grouped=ExceptionGroup('fixture secret must not escape',[RuntimeError('fixture secret'),ExceptionGroup('nested',[reason])])
        self.assertEqual(policy_failure(grouped),str(reason))
        self.assertIsNone(policy_failure(RuntimeError('fixture secret')))


if __name__=='__main__':
    unittest.main(verbosity=2)
