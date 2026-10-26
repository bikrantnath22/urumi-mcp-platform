"""Verify in an isolated schema; no real accounts, LLM or upstream calls.

Run from apps/gateway: uv run python tests/verify_usage_limits.py
--sqlite enables portable verification; PostgreSQL additionally proves its own
row-lock/upsert concurrency. aiosqlite is needed only for --sqlite.
"""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from unittest.mock import AsyncMock, patch
from uuid import uuid4

parser = argparse.ArgumentParser()
parser.add_argument('--sqlite', action='store_true')
args = parser.parse_args()
if not (Path.cwd()/'app/rate_limits.py').is_file():
    raise SystemExit('Run from apps/gateway after applying this update.')
if args.sqlite:
    from cryptography.fernet import Fernet
    os.environ['PGPASSWORD'] = 'isolated-unused'
    os.environ['JWT_SECRET'] = 'isolated-usage-test-'*3
    os.environ['FERNET_KEY'] = Fernet.generate_key().decode()
    os.environ['COOKIE_SECURE'] = 'false'
sys.path.insert(0, str(Path.cwd()))

import httpx
print('Loading gateway verification modules', flush=True)
from fastapi import FastAPI
from mcp import types
from sqlalchemy import BigInteger, delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.requests import Request
from starlette.routing import Route
from app import auth, chat, chat_store, core, mcp_api, rate_limits, tools, usage_limits
from app.config import database_url
from app.database import engine, get_session
from app.models import APIKey, AuditLog, Base, BrowserSession, ChatTurn, MCPServer, RateLimitBucket, SecurityEvent, ToolGrant, User
from app.security import create_session_token, hash_password, token_hash


async def check():
    print('Creating isolated verification data', flush=True)
    schema = 'urumi_usage_test_'+uuid4().hex
    created = False
    isolated = None
    temporary = None
    try:
        if args.sqlite:
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy.ext.compiler import compiles
            compiles(JSONB, 'sqlite')(lambda element, compiler, **kw: 'JSON')
            compiles(BigInteger, 'sqlite')(lambda element, compiler, **kw: 'INTEGER')
            temporary = tempfile.TemporaryDirectory(prefix='.usage-test-', dir=Path.cwd())
            isolated = create_async_engine('sqlite+aiosqlite:///'+str(Path(temporary.name)/'test.db'),
                connect_args={'timeout':30})
        else:
            async with engine.begin() as connection:
                await connection.execute(CreateSchema(schema))
            created = True
            isolated = create_async_engine(database_url(), hide_parameters=True,
                connect_args={'options':f'-c search_path={schema} -c timezone=UTC'})
        factory = async_sessionmaker(isolated, expire_on_commit=False)
        async with isolated.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        print('Temporary tables ready', flush=True)
        password = 'fixture-pass-123'
        hashed = await hash_password(password)
        raw_keys = ["urm_"+secrets.token_urlsafe(32) for _ in range(2)]
        cookies = {}
        async with factory() as database:
            database.add_all([User(id=70101,email='admin@usage.invalid',role='admin',active=True,password_hash=hashed),
                User(id=70102,email='member@usage.invalid',role='member',active=True,password_hash=hashed),
                User(id=70103,email='admin-b@usage.invalid',role='admin',active=True,password_hash=hashed),
                MCPServer(id=70101,slug='fixture',name='Fixture',url='http://fixture.invalid/mcp',
                    enabled=True,timeout_s=5,status='Connected')])
            await database.flush()
            database.add(ToolGrant(user_id=70102,server_id=70101,tool_name='read',granted_by_id=70101))
            for index, key in enumerate(raw_keys):
                database.add(APIKey(id=70101+index,user_id=70102,name='Fixture',prefix=key[:8],key_hash=token_hash(key)))
            for user_id in (70101,70102,70103):
                nonce = secrets.token_urlsafe(32)
                now = datetime.now(timezone.utc)
                database.add(BrowserSession(id=token_hash(nonce),user_id=user_id,session_version=0,
                    last_activity_at=now,idle_expires_at=now+timedelta(minutes=30),absolute_expires_at=now+timedelta(hours=8)))
                cookies[user_id] = create_session_token(user_id,session_id=nonce)
            await database.commit()

        async def dependency():
            async with factory() as database:
                yield database
        app = FastAPI()
        for router in (auth.router, usage_limits.router, tools.router, chat.router):
            app.include_router(router)
        app.add_exception_handler(rate_limits.RateLimitExceeded, rate_limits.limit_response)
        app.add_exception_handler(rate_limits.LimitsUnavailable, rate_limits.unavailable_response)
        app.router.routes.append(Route('/mcp',endpoint=mcp_api.mcp_app))
        app.dependency_overrides[get_session] = dependency
        def client(user_id=None):
            result = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8003')
            if user_id:
                result.cookies.set('urm_session',cookies[user_id])
                result.cookies.set('urm_csrf','fixture-csrf')
                result.headers['X-CSRF-Token']='fixture-csrf'
            return result
        async def expect(response, code):
            assert response.status_code == code, (response.status_code,response.text)
            return response.json() if response.content else None
        async def clear():
            async with factory() as database:
                await database.execute(delete(RateLimitBucket))
                await database.commit()
        async def save(admin, **changes):
            data = await expect(await admin.get('/api/admin/usage-limits'),200)
            payload = {name:data[name] for name in rate_limits.DEFAULTS}
            payload.update(revision=data['revision'],**changes)
            return await expect(await admin.put('/api/admin/usage-limits',json=payload),200)
        fake_result = types.CallToolResult(content=[types.TextContent(type='text',text='fixture result')],isError=False)
        upstream = AsyncMock(return_value=fake_result)
        async def fake_completion(*unused, **kwargs):
            return {'model':'fixture','duration_ms':0,'usage':{},'calls':[],
                'message':{'role':'assistant','content':'Fixture reply'}}
        with ExitStack() as stack:
            for module in (auth, chat_store, core, mcp_api, rate_limits):
                stack.enter_context(patch.object(module,'Session',factory))
            stack.enter_context(patch.object(core,'call_tool',upstream))
            stack.enter_context(patch.object(chat,'list_gateway_tools',AsyncMock(return_value=[])))
            completion = stack.enter_context(patch.object(chat,'request_completion',AsyncMock(side_effect=fake_completion)))
            stack.enter_context(patch.object(chat,'create_llm_client',lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(500)))))
            print('Isolated fixture ready; checking admin settings API', flush=True)
            async with client(70101) as admin, client(70102) as member, client() as anon:
                endpoint='/api/admin/usage-limits'
                await expect(await anon.get(endpoint),401)
                await expect(await member.get(endpoint),403)
                original=await expect(await admin.get(endpoint),200)
                body={name:original[name] for name in rate_limits.DEFAULTS}; body['revision']=original['revision']
                await expect(await member.put(endpoint,json=body),403)
                await expect(await admin.put(endpoint,json=body,headers={'X-CSRF-Token':''}),403)
                await expect(await admin.put(endpoint,json=body,headers={'Origin':'https://invalid.example'}),403)
                await expect(await admin.put(endpoint,json={**body,'actor_id':70102}),422)
                await expect(await admin.put(endpoint,json={**body,'chat_per_minute':0}),422)
                await expect(await admin.put(endpoint,json={**body,'tool_per_minute':True}),422)
                await save(admin,tool_per_minute=2)
                await expect(await admin.put(endpoint,json=body),409)
                async with factory() as database:
                    event=await database.scalar(select(SecurityEvent).where(SecurityEvent.action=='usage_limits.updated'))
                    assert event.actor_id==70101 and event.details['after']['tool_per_minute']==2
                print('Admin page API: anonymous/member access, CSRF, origin, validation, stale edits and change auditing passed')
                if not args.sqlite:
                    data=await expect(await admin.get(endpoint),200)
                    edit={name:data[name] for name in rate_limits.DEFAULTS}
                    edit.update(revision=data['revision'],tool_per_minute=3)
                    async with client(70103) as other_admin:
                        edits=await asyncio.gather(admin.put(endpoint,json=edit),other_admin.put(endpoint,json={**edit,'tool_per_minute':4}))
                    assert sorted(item.status_code for item in edits)==[200,409]
                    await save(admin,tool_per_minute=2)
                    print('PostgreSQL concurrent admin edits: one save succeeds and the stale save is rejected')

                # Two API keys and three entry points share one user bucket.
                await clear()
                payload={'name':'fixture__read','arguments':{}}
                await expect(await member.post('/api/tools/call',json=payload),200)
                rpc={'jsonrpc':'2.0','id':2,'method':'tools/call','params':payload}
                async with mcp_api.mcp_sessions.run():
                    response=await anon.post('/mcp',headers={'Authorization':'Bearer '+raw_keys[0],
                        'Accept':'application/json, text/event-stream'},json=rpc)
                    assert not (await expect(response,200))['result']['isError']
                    response=await anon.post('/mcp',headers={'Authorization':'Bearer '+raw_keys[1],
                        'Accept':'application/json, text/event-stream'},json={**rpc,'id':3})
                    result=(await expect(response,200))['result']
                    assert result['isError'] and result['structuredContent']['rate_limited']
                    assert result['structuredContent']['executed'] is False
                limited=await member.post('/api/tools/call',json=payload)
                await expect(limited,429); assert int(limited.headers['Retry-After'])>0
                async with factory() as database:
                    user=await database.get(User,70102); database.expunge(user)
                try:
                    await core.execute_gateway_tool(user,'fixture__read',{},source='chat')
                    raise AssertionError('Chat tool limit bypassed')
                except rate_limits.RateLimitExceeded:
                    pass
                assert upstream.await_count==2
                async with factory() as database:
                    denied=list(await database.scalars(select(AuditLog).where(AuditLog.outcome=='denied')))
                    assert {'mcp','dashboard','chat'} <= {row.source for row in denied}
                    assert all(row.user_id==70102 for row in denied)
                await save(admin,tool_per_minute=3)
                await expect(await member.post('/api/tools/call',json=payload),200)
                assert upstream.await_count==3
                await expect(await admin.post('/api/tools/call',json=payload),200)
                await expect(await admin.post('/api/tools/call',json=payload),200)
                await expect(await admin.post('/api/tools/call',json=payload),200)
                await expect(await admin.post('/api/tools/call',json=payload),429)
                await save(admin,tool_per_minute=1)
                await expect(await member.post('/api/tools/call',json=payload),429)
                print('Dashboard/chat/MCP share user quota across keys; admin is limited separately; denied calls audited; raised limit applies immediately')

                await clear()
                await save(admin,login_account_limit=2,login_ip_limit=4)
                with patch.object(auth,'verify_password',AsyncMock(return_value=False)) as verify:
                    for email in ('victim@invalid.test',' VICTIM@invalid.test '):
                        await expect(await anon.post('/api/auth/login',json={'email':email,'password':'wrong'}),401)
                    response=await anon.post('/api/auth/login',json={'email':'victim@invalid.test','password':'wrong'})
                    await expect(response,429)
                    assert verify.await_count==2, 'Limited attempts must stop before password hashing'
                    await expect(await anon.post('/api/auth/login',json={'email':'other@invalid.test','password':'wrong'}),401)
                    await expect(await anon.post('/api/auth/login',json={'email':'another@invalid.test','password':'wrong'},
                        headers={'X-Forwarded-For':'203.0.113.99'}),429)
                    assert verify.await_count==3
                await expect(await member.get('/api/auth/me'),200)
                print('Login account/IP windows, normalized email, forged-header resistance, early hash rejection and existing sessions passed')

                await clear()
                await save(admin,chat_per_minute=1,chat_per_hour=100,chat_global_per_minute=30)
                request={'message':'Fixture message','request_id':str(uuid4())}
                first=await expect(await member.post('/api/chat',json=request),200)
                cached=await expect(await member.post('/api/chat',json=request),200)
                assert cached==first and completion.await_count==1
                response=await member.post('/api/chat',json={'message':'A second message','request_id':str(uuid4())})
                data=await expect(response,429)
                assert data['request_not_started'] and completion.await_count==1
                async with factory() as database:
                    assert await database.scalar(select(func.count()).select_from(ChatTurn))==1
                await save(admin,chat_per_minute=2)
                await expect(await member.post('/api/chat',json={'message':'A second message','request_id':str(uuid4())}),200)
                assert completion.await_count==2
                print('Chat rate rejection runs no model/tool and creates no turn; duplicate completion replay is free; admin edit applies without restart')

                await save(admin,chat_per_hour=2)
                with patch.object(rate_limits,'database_now',AsyncMock(return_value=datetime.now(timezone.utc)+timedelta(seconds=61))):
                    await expect(await member.post('/api/chat',json={'message':'Hourly test','request_id':str(uuid4())}),429)
                await clear()
                await save(admin,chat_per_minute=10,chat_per_hour=100,chat_global_per_minute=1)
                await expect(await member.post('/api/chat',json={'message':'Shared test','request_id':str(uuid4())}),200)
                await expect(await admin.post('/api/chat',json={'message':'Shared test','request_id':str(uuid4())}),429)
                print('Hourly chat allowance survives a minute reset; shared chat allowance spans users')

                await clear()
                await save(admin,chat_global_per_minute=30,tool_per_minute=1)
                fixture_tool = types.Tool(
                    name="fixture__read",
                    description="Read isolated verification data.",
                    inputSchema={
                        "type": "object",
                        "properties": {"index": {"type": "integer", "minimum": 1}},
                        "required": ["index"],
                        "additionalProperties": False,
                    },
                )

                def tool_completion(index):
                    identity = f"fixture-{index}"
                    arguments = {"index": index}
                    return {
                        "model": "fixture", "duration_ms": 0, "usage": {},
                        "calls": [{
                            "id": identity, "name": "fixture__read",
                            "arguments": arguments,
                        }],
                        "message": {
                            "role": "assistant", "content": None,
                            "tool_calls": [{
                                "id": identity, "type": "function",
                                "function": {
                                    "name": "fixture__read",
                                    "arguments": json.dumps(arguments),
                                },
                            }],
                        },
                    }

                before=upstream.await_count
                partial={'message':'Two fixture reads','request_id':str(uuid4())}
                with (
                    patch.object(chat, "list_gateway_tools", AsyncMock(return_value=[fixture_tool])),
                    patch.object(
                        chat, "request_completion",
                        AsyncMock(side_effect=[tool_completion(1), tool_completion(2)]),
                    ) as partial_model,
                ):
                    response=await member.post('/api/chat',json=partial)
                    data=await expect(response,429)
                    assert [item['status'] for item in data['tool_calls']]==['completed','rate_limited']
                    assert data['tool_calls'][1]['result']['structuredContent']['executed'] is False
                    assert upstream.await_count==before+1
                    assert data.get('request_not_started') is not True
                    cached=await member.post('/api/chat',json=partial)
                    assert (await expect(cached,429))==data and cached.headers['Retry-After']
                    assert upstream.await_count==before+1
                    assert partial_model.await_count == 2
                    assert [
                        item["arguments"] for item in data["tool_calls"]
                    ] == [{"index": 1}, {"index": 2}]
                    for request_call in partial_model.await_args_list:
                        assert "fixture__read" in request_call.args[3].values()
                print('Mid-chat tool limit preserves completed activity, marks the blocked tool unexecuted, and cached replay never repeats actions')

                before=upstream.await_count
                with patch.object(rate_limits,'consume',AsyncMock(side_effect=rate_limits.SQLAlchemyError('fixture'))):
                    data=await expect(await member.post('/api/tools/call',json=payload),503)
                    assert data['request_not_started'] is True and upstream.await_count==before
                    await expect(await anon.post('/api/auth/login',json={'email':'other@invalid.test','password':'wrong'}),503)
                await expect(await member.get('/api/auth/me'),200)
                await clear()
                denied=await expect(await member.post('/api/tools/call',json={'name':'fixture__write','arguments':{}}),200)
                assert denied['isError'] and upstream.await_count==before
                await expect(await member.post('/api/tools/call',json=payload),429)
                print('Unavailable quota storage fails closed before execution; denied tool attempts count; read-only session checks remain available')

                # Shorter windows use a deterministic DB clock; no real sleeps.
                await clear()
                start=datetime(2026,1,1,tzinfo=timezone.utc)
                rule=rate_limits.Rule('boundary','fixture',2,60,'Fixture')
                async def take(rules):
                    async with factory() as database:
                        await rate_limits.consume(database,rules)
                        await database.commit()
                with patch.object(rate_limits,'database_now',AsyncMock(return_value=start)):
                    await take([rule]); await take([rule])
                with patch.object(rate_limits,'database_now',AsyncMock(return_value=start+timedelta(seconds=59))):
                    try:
                        await take([rule]); raise AssertionError('Window allowed too early')
                    except rate_limits.RateLimitExceeded as exc:
                        assert exc.retry_after==1
                with patch.object(rate_limits,'database_now',AsyncMock(return_value=start+timedelta(seconds=60))):
                    await take([rule])
                await clear()
                shared=rate_limits.Rule('concurrent','all',3,60,'Fixture')
                async def race():
                    try:
                        await take([shared]); return True
                    except rate_limits.RateLimitExceeded:
                        return False
                results=await asyncio.gather(*(race() for _ in range(12)))
                assert sum(results)==3, results
                print('Exact window expiry and 12 independent-session concurrent attempts: exactly 3 admitted')

                # All quota rows in a rejected multi-rule transaction roll back.
                await clear()
                full=rate_limits.Rule('full','a',1,60,'Full')
                spare=rate_limits.Rule('spare','b',5,60,'Spare')
                await take([full])
                try:
                    await take([spare,full]); raise AssertionError('Full quota bypassed')
                except rate_limits.RateLimitExceeded:
                    pass
                async with factory() as database:
                    assert await database.get(RateLimitBucket,spare.key) is None
                    ids=list(await database.scalars(select(RateLimitBucket.id)))
                    assert all(len(value)==64 for value in ids)
                def request(peer, forwarded):
                    return Request({'type':'http','method':'POST','path':'/',
                        'headers':[(b'x-forwarded-for',forwarded.encode())],'client':(peer,123)})
                with patch.object(rate_limits,'TRUSTED_PROXY_NETWORKS',(ipaddress.ip_network('10.42.0.0/16'),)):
                    assert rate_limits.client_ip(request('198.51.100.10','203.0.113.9'))=='198.51.100.10'
                    assert rate_limits.client_ip(request('10.42.0.2','203.0.113.9, 198.51.100.10'))=='198.51.100.10'
                    assert rate_limits.client_ip(request('10.42.0.2','bad-header'))=='10.42.0.2'
                print('Atomic multi-window rollback, opaque bucket IDs and right-to-left trusted-proxy IP handling passed')
                async with factory() as database:
                    database.add(RateLimitBucket(id='f'*64,scope='expired-fixture',used=1,
                        reset_at=datetime.now(timezone.utc)-timedelta(days=2)))
                    await database.commit()
                with patch.object(rate_limits.asyncio,'sleep',AsyncMock(side_effect=asyncio.CancelledError)):
                    try:
                        await rate_limits.cleanup_loop()
                    except asyncio.CancelledError:
                        pass
                async with factory() as database:
                    assert await database.get(RateLimitBucket,'f'*64) is None
                    assert await database.get(RateLimitBucket,full.key) is not None
                print('Bounded cleanup removes retained expired buckets and preserves active allowances')
        print('Usage verification passed. No live accounts or external services changed.')
    finally:
        if isolated is not None:
            await isolated.dispose()
        if created:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema,cascade=True))
            print('Temporary PostgreSQL schema removed.')
        if temporary is not None:
            temporary.cleanup()
            print('Temporary SQLite data removed; PostgreSQL deployment verification remains required.')
        await engine.dispose()


asyncio.run(asyncio.wait_for(check(), 180))
