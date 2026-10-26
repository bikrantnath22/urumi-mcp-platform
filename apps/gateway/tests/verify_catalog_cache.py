"""Isolated catalogue cache/security verification. No real upstream/model calls.

Run from apps/gateway. Default uses a temporary PostgreSQL schema.
--sqlite provides a portable supplement, not PostgreSQL contention coverage.
"""
import argparse
import asyncio
from contextlib import ExitStack
from copy import copy
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
from unittest.mock import AsyncMock, patch
from uuid import uuid4

parser = argparse.ArgumentParser()
parser.add_argument('--sqlite', action='store_true')
args = parser.parse_args()
sys.path.insert(0,str(Path.cwd()))
if args.sqlite:
    from cryptography.fernet import Fernet
    os.environ['PGPASSWORD'] = 'isolated-unused'
    os.environ['JWT_SECRET'] = 'isolated-catalog-test-' * 3
    os.environ['FERNET_KEY'] = Fernet.generate_key().decode()
    os.environ['COOKIE_SECURE'] = 'false'

from mcp.types import CallToolResult, TextContent, Tool
from sqlalchemy import BigInteger, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from app.catalog_cache import ToolCatalogCache
from app import core, registry, servers
from app.config import database_url
from app.database import engine
from app.models import AuditLog, Base, MCPServer, ToolGrant, User
from app.upstreams import UpstreamError


def definition(name='get_note', description='Read a note'):
    return Tool(name=name,description=description,inputSchema={
        'type':'object','properties':{'note_id':{'type':'integer'}},'required':['note_id']})


def source(server_id=1,slug='notes'):
    return SimpleNamespace(id=server_id,slug=slug,name='Fixture',url='http://fixture.invalid/mcp',
        enabled=True,timeout_s=5,auth_header_name=None,credential_encrypted=None)


async def primitives():
    now=[100.0]
    cache=ToolCatalogCache(clock=lambda:now[0],max_entries=2)
    server=source()
    loader=AsyncMock(return_value=[definition()])
    try:
        assert (await cache.get(server,loader)).state=='miss'
        first=await cache.get(server,loader)
        first.tools[0].inputSchema['properties'].clear()
        assert (await cache.get(server,loader)).tools[0].inputSchema['properties']
        assert loader.await_count==1
        now[0]=160.0
        assert (await cache.get(server,loader)).state=='miss' and loader.await_count==2
        loader.return_value=[definition('new_tool')]
        assert (await cache.get(server,loader,refresh=True)).tools[0].name=='new_tool'
        changed=copy(server); changed.credential_encrypted='encrypted-fixture-only'
        assert (await cache.get(changed,loader)).state=='miss'
        assert (await cache.get(changed,loader)).state=='hit'
        for slug in ('tavily','other'):
            external=source(10,slug)
            assert (await cache.get(external,loader)).state=='bypass'
            assert (await cache.get(external,loader)).state=='bypass'
        a,b,c=source(20),source(21),source(22)
        await cache.get(a,loader); await cache.get(b,loader); await cache.get(a,loader); await cache.get(c,loader)
        assert (await cache.get(b,loader)).state=='miss'
    finally:
        await cache.close()
    disabled=ToolCatalogCache(ttl_seconds=0)
    assert (await disabled.get(server,loader)).state=='bypass'
    assert (await disabled.get(server,loader)).state=='bypass'
    await disabled.close()
    tiny=ToolCatalogCache(max_bytes=1024)
    huge=AsyncMock(return_value=[definition(description='x'*2000)])
    assert (await tiny.get(server,huge)).state=='miss'
    assert (await tiny.get(server,huge)).state=='miss' and huge.await_count==2
    await tiny.close()
    cache=ToolCatalogCache()
    failed=AsyncMock(side_effect=UpstreamError('Fixture unavailable'))
    for unused in range(2):
        try:
            await cache.get(server,failed)
            raise AssertionError('Upstream failure was cached')
        except UpstreamError:
            pass
    assert failed.await_count==2
    started,release=asyncio.Event(),asyncio.Event()
    calls=0
    async def slow():
        nonlocal calls
        calls+=1; started.set(); await release.wait()
        return [definition()]
    pending=[asyncio.create_task(cache.get(server,slow)) for _ in range(12)]
    await started.wait(); await asyncio.sleep(0)
    pending[0].cancel()
    try: await pending[0]
    except asyncio.CancelledError: pass
    release.set()
    values=await asyncio.gather(*pending[1:])
    assert calls==1 and all(value.state=='shared' for value in values)
    await cache.close()
    started.clear(); release.clear()
    pending=asyncio.create_task(cache.get(server,slow))
    await started.wait(); cache.invalidate(server.id); release.set(); await pending
    assert (await cache.get(server,loader)).state=='miss'
    await cache.close()
    started.clear(); release.clear()
    pending=asyncio.create_task(cache.get(server,slow))
    await started.wait(); await cache.close()
    try: await pending
    except asyncio.CancelledError: pass
    assert not cache._tasks and not cache._entries
    print('TTL boundary, deep-copy isolation, forced refresh, identity changes, scope, capacity, error handling, concurrent misses, cancellation and shutdown passed',flush=True)


async def integration():
    schema='urumi_catalog_test_'+uuid4().hex
    isolated=None; created=False; temporary=None
    cache=ToolCatalogCache()
    try:
        if args.sqlite:
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy.ext.compiler import compiles
            compiles(JSONB,'sqlite')(lambda element,compiler,**kw:'JSON')
            compiles(BigInteger,'sqlite')(lambda element,compiler,**kw:'INTEGER')
            temporary=tempfile.TemporaryDirectory(prefix='.catalog-test-',dir=Path.cwd())
            isolated=create_async_engine('sqlite+aiosqlite:///'+str(Path(temporary.name)/'test.db'))
        else:
            async with engine.begin() as connection:
                await connection.execute(CreateSchema(schema))
            created=True
            isolated=create_async_engine(database_url(),hide_parameters=True,
                connect_args={'options':f'-c search_path={schema} -c timezone=UTC'})
        factory=async_sessionmaker(isolated,expire_on_commit=False)
        async with isolated.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as db:
            member=User(id=99101,email='member@catalog.invalid',role='member',active=True,password_hash='fixture')
            admin=User(id=99102,email='admin@catalog.invalid',role='admin',active=True,password_hash='fixture')
            db.add_all([member,admin,MCPServer(id=99101,slug='notes',name='Fixture Notes',
                url='https://notes.fixture.invalid/mcp',enabled=True,timeout_s=5,status='Unreachable')])
            await db.flush()
            db.add(ToolGrant(user_id=member.id,server_id=99101,tool_name='get_note'))
            await db.commit()
        discovery=AsyncMock(return_value=[definition(),definition('add_note')])
        execute=AsyncMock(return_value=CallToolResult(content=[TextContent(type='text',text='Fixture live result')],isError=False))

        async def edit(**changes):
            async with factory() as db:
                row=await db.get(MCPServer,99101)
                fields={key:getattr(row,key) for key in ('slug','name','url','enabled','timeout_s')}
                fields.update(changes)
                return await servers.edit_server(99101,servers.ServerBody(**fields),db)

        async def grant(enabled):
            async with factory() as db:
                await db.execute(delete(ToolGrant).where(ToolGrant.user_id==member.id))
                if enabled: db.add(ToolGrant(user_id=member.id,server_id=99101,tool_name='get_note'))
                await db.commit()

        with ExitStack() as stack:
            if args.sqlite:
                stack.enter_context(patch('app.managed_servers.lock_store_admission',AsyncMock()))
            for module in (core,registry): stack.enter_context(patch.object(module,'Session',factory))
            for module in (core,registry,servers): stack.enter_context(patch.object(module,'catalog_cache',cache))
            stack.enter_context(patch.object(core,'discover_tools',discovery))
            stack.enter_context(patch.object(registry,'discover_tools',discovery))
            stack.enter_context(patch.object(core,'call_tool',execute))
            stack.enter_context(patch.object(core,'enforce_tool',AsyncMock()))
            assert [item.name for item in await core.list_gateway_tools(member)]==['notes__get_note']
            async with factory() as db: checked=(await db.get(MCPServer,99101)).last_checked_at
            assert len(await core.list_gateway_tools(admin))==2 and discovery.await_count==1
            async with factory() as db: assert (await db.get(MCPServer,99101)).last_checked_at==checked
            for unused in range(2):
                assert not (await core.execute_gateway_tool(member,'notes__get_note',{'note_id':1},source='chat')).isError
            assert execute.await_count==2, 'Tool execution was incorrectly cached'
            await grant(False)
            assert not await core.list_gateway_tools(member)
            denied=await core.execute_gateway_tool(member,'notes__get_note',{},source='mcp')
            assert denied.isError and execute.await_count==2 and discovery.await_count==1
            await grant(True)
            await edit(enabled=False)
            assert not await core.list_gateway_tools(admin)
            assert (await core.execute_gateway_tool(member,'notes__get_note',{},source='dashboard')).isError
            await edit(enabled=True)
            assert len(await core.list_gateway_tools(admin))==2 and discovery.await_count==2
            discovery.return_value=[definition(),definition('new_note_tool')]
            refreshed=await registry.refresh_server(99101)
            assert len(refreshed['tools'])==2 and discovery.await_count==3
            assert [tool.name for tool in await core.list_gateway_tools(admin)]==['notes__get_note','notes__new_note_tool']
            assert discovery.await_count==3
            await edit(credential='Bearer fixture-only',auth_header_name='Authorization')
            assert not await core.list_gateway_tools(member), 'Credential change retained grants'
            assert len(await core.list_gateway_tools(admin))==2 and discovery.await_count==4
            assert discovery.call_args.kwargs['headers']=={'Authorization':'Bearer fixture-only'}
            await grant(True)
            execute.side_effect=UpstreamError('Fixture unavailable')
            assert (await core.execute_gateway_tool(member,'notes__get_note',{},source='chat')).isError
            await core.list_gateway_tools(admin)
            assert discovery.await_count==5, 'Connection failure did not evict metadata'
            execute.side_effect=None
            # Revoke while a warm catalogue is being returned; the final DB check must win.
            original=cache.get
            async def concurrent_revoke(*items,**kwargs):
                value=await original(*items,**kwargs)
                await grant(False)
                return value
            with patch.object(cache,'get',side_effect=concurrent_revoke):
                assert not await core.list_gateway_tools(member)
            async def concurrent_disable(*items,**kwargs):
                value=await original(*items,**kwargs)
                await edit(enabled=False)
                return value
            with patch.object(cache,'get',side_effect=concurrent_disable):
                assert not await core.list_gateway_tools(admin)
            await edit(enabled=True)
            await grant(True)
            await core.list_gateway_tools(member)
            async with factory() as db:
                current=await db.get(User,member.id); current.active=False; await db.commit()
            assert not await core.list_gateway_tools(member)
            assert (await core.execute_gateway_tool(member,'notes__get_note',{},source='mcp')).isError
            async with factory() as db:
                audits=list(await db.scalars(select(AuditLog).where(AuditLog.user_id==member.id)))
                assert {row.outcome for row in audits}=={'success','denied','error'}
                await servers.delete_server(99101,db)
            assert not await core.list_gateway_tools(admin)
        print('Warm-cache permissions, revocation, disabled/deleted servers, concurrent changes, credential edits, forced checks, health timestamps, live execution and audits passed',flush=True)
    finally:
        await cache.close()
        if isolated is not None: await isolated.dispose()
        if created:
            async with engine.begin() as connection: await connection.execute(DropSchema(schema,cascade=True))
            print('Temporary PostgreSQL schema removed.',flush=True)
        if temporary is not None: temporary.cleanup()
        await engine.dispose()


async def main():
    await primitives()
    await integration()
    print('Catalogue cache verification passed. No live accounts, models or upstream data changed.',flush=True)


asyncio.run(main())
