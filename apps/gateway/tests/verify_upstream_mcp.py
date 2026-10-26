"""Real MCP SDK + SSRF transport integration; no database or external calls.

Run from apps/gateway: uv run python tests/verify_upstream_mcp.py
Only a mock MCP protocol peer is contacted. API checks use unconnected sessions.
"""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# These process-local fixture values cannot access real deployment data.
os.environ['PGPASSWORD'] = 'isolated-upstream-test-unused'
os.environ['JWT_SECRET'] = 'isolated-upstream-test-' * 3
os.environ['FERNET_KEY'] = Fernet.generate_key().decode()

from app import servers, upstreams
from app.models import MCPServer
from app.upstream_security import DestinationPolicy, PinnedUpstreamTransport


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.policy = DestinationPolicy()
        self.url = 'http://woo-mcp.store.svc.cluster.local:8000/mcp'
        self.requests = []
        self.executions = 0

    async def peer(self, request):
        self.requests.append(request)
        self.assertEqual(request.url.host, '10.43.8.12')
        self.assertEqual(request.headers['Host'], 'woo-mcp.store.svc.cluster.local:8000')
        if request.method != 'POST':
            return httpx.Response(405)
        body=json.loads(await request.aread())
        method=body['method']
        if method.startswith('notifications/'):
            return httpx.Response(202)
        if method=='initialize':
            result={'protocolVersion':body['params']['protocolVersion'],
                    'capabilities':{'tools':{'listChanged':False}},
                    'serverInfo':{'name':'Isolated fixture peer','version':'1.0'}}
        elif method=='tools/list':
            result={'tools':[{'name':'fixture_read','description':'Read an isolated fixture',
                'inputSchema':{'type':'object','properties':{'index':{'type':'integer'}}}}]}
        elif method=='tools/call':
            self.executions+=1
            result={'content':[{'type':'text','text':'Fixture read completed'}],'isError':False}
        else:
            raise AssertionError(f'Unexpected MCP method: {method}')
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})

    def make_transport(self, destination):
        return PinnedUpstreamTransport(destination, transport=httpx.MockTransport(self.peer))

    async def test_real_sdk_discovers_and_executes_through_guarded_transport(self):
        with patch.object(upstreams,'destination_policy',self.policy), \
             patch('app.upstream_security.resolve_addresses',AsyncMock(return_value=['10.43.8.12'])), \
             patch.object(upstreams,'PinnedUpstreamTransport',side_effect=self.make_transport):
            tools=await upstreams.discover_tools(self.url)
            self.assertEqual([tool.name for tool in tools],['fixture_read'])
            result=await upstreams.call_tool(self.url,'fixture_read',{'index':1})
            self.assertFalse(result.isError)
            self.assertEqual(result.content[0].text,'Fixture read completed')
            self.assertEqual(self.executions,1)

    async def test_private_dns_blocks_both_discovery_and_execution_before_http(self):
        factory=Mock(side_effect=self.make_transport)
        with patch.object(upstreams,'destination_policy',self.policy), \
             patch('app.upstream_security.resolve_addresses',AsyncMock(return_value=['127.0.0.1'])), \
             patch.object(upstreams,'PinnedUpstreamTransport',factory):
            for action in [upstreams.discover_tools('https://mcp.fixture.invalid/mcp'),
                           upstreams.call_tool('https://mcp.fixture.invalid/mcp','fixture_read',{})]:
                with self.assertRaisesRegex(upstreams.UpstreamError,'blocked'):
                    await action
        factory.assert_not_called()
        self.assertEqual(self.requests,[])
        self.assertEqual(self.executions,0)

    async def test_sdk_redirect_failure_is_safe_and_does_not_reach_second_destination(self):
        requests=[]
        def redirect(request):
            requests.append(request)
            return httpx.Response(307,headers={'Location':'http://169.254.169.254/latest/meta-data/'})
        def factory(destination):
            return PinnedUpstreamTransport(destination,transport=httpx.MockTransport(redirect))
        with patch.object(upstreams,'destination_policy',self.policy), \
             patch('app.upstream_security.resolve_addresses',AsyncMock(return_value=['10.43.8.12'])), \
             patch.object(upstreams,'PinnedUpstreamTransport',side_effect=factory):
            with self.assertRaisesRegex(upstreams.UpstreamError,'redirects'):
                await upstreams.discover_tools(self.url,headers={'Authorization':'Bearer fixture-only'})
        self.assertEqual(len(requests),1)

    async def test_admin_create_rejects_private_dns_before_save(self):
        body=servers.ServerBody(slug='fixture',name='Fixture',url='https://mcp.fixture.invalid/mcp')
        database=Mock()
        database.commit=AsyncMock()
        with patch.object(servers,'destination_policy',self.policy), \
             patch('app.upstream_security.resolve_addresses',AsyncMock(return_value=['169.254.169.254'])):
            with self.assertRaises(HTTPException) as error:
                await servers.create_server(body,database)
        self.assertEqual(error.exception.status_code,422)
        database.add.assert_not_called()
        database.commit.assert_not_awaited()

    async def test_admin_edit_rejects_private_dns_before_changing_existing_server(self):
        server=MCPServer(id=101,slug='fixture',name='Fixture',url=self.url,enabled=True,
                         timeout_s=15,status='Connected',auth_header_name=None,credential_encrypted=None)
        database=Mock()
        database.scalar=AsyncMock(return_value=server)
        database.commit=AsyncMock()
        body=servers.ServerBody(slug='fixture',name='Changed',url='https://mcp.fixture.invalid/mcp')
        with patch.object(servers,'destination_policy',self.policy), \
             patch.object(servers,'load_manual_server',AsyncMock(return_value=server)), \
             patch('app.upstream_security.resolve_addresses',AsyncMock(return_value=['10.43.0.1'])):
            with self.assertRaises(HTTPException) as error:
                await servers.edit_server(101,body,database)
        self.assertEqual(error.exception.status_code,422)
        self.assertEqual(server.url,self.url)
        self.assertEqual(server.name,'Fixture')
        database.commit.assert_not_awaited()


if __name__=='__main__':
    unittest.main(verbosity=2)
