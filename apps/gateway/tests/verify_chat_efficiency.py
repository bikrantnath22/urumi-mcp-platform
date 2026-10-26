"""Isolated chat/context/deletion checks. No live model or upstream requests.

Run from apps/gateway: uv run python tests/verify_chat_efficiency.py
Default uses a disposable PostgreSQL schema; --sqlite is a portable supplement.
"""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
from types import SimpleNamespace
import sys
import tempfile
from unittest.mock import patch
from uuid import UUID, uuid4

parser = argparse.ArgumentParser()
parser.add_argument('--sqlite', action='store_true')
args = parser.parse_args()
if not (Path.cwd() / 'app/chat_context.py').is_file():
    raise SystemExit('Run from apps/gateway after applying the chat efficiency update.')
if args.sqlite:
    from cryptography.fernet import Fernet
    os.environ['PGPASSWORD'] = 'isolated-unused'
    os.environ['JWT_SECRET'] = 'isolated-chat-test-' * 3
    os.environ['FERNET_KEY'] = Fernet.generate_key().decode()
    os.environ['COOKIE_SECURE'] = 'false'
sys.path.insert(0, str(Path.cwd()))

import httpx
from fastapi import FastAPI, HTTPException
from mcp import types
from sqlalchemy import BigInteger, delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from app import auth, chat, chats, chat_store, core, llm, rate_limits
from app.chat_context import CONTEXT_CHAR_BUDGET, model_tool_result, previous_context, standalone_tool
from app.config import database_url
from app.database import engine, get_session
from app.models import AuditLog, Base, BrowserSession, ChatConversation, ChatTurn, MCPServer, ToolGrant, UsageLimitSettings, User
from app.security import create_session_token, hash_password, token_hash


def result(value):
    return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(value, indent=2))], isError=False)


def past(message, reply='', calls=None, image_id=None):
    return SimpleNamespace(message=message, image_id=image_id,
                           response={'reply':reply, 'tool_calls':calls or []})


def model_call(name, arguments):
    identity = 'call_' + uuid4().hex
    return {'model':'fixture', 'duration_ms':1, 'usage':{'prompt_tokens':100, 'completion_tokens':5,
            'prompt_tokens_details':{'cached_tokens':40}},
        'calls':[{'id':identity, 'name':name, 'arguments':arguments}],
        'message':{'role':'assistant', 'content':None, 'tool_calls':[{'id':identity, 'type':'function',
            'function':{'name':name, 'arguments':json.dumps(arguments)}}]}}


def model_reply(text='Fixture response.'):
    return {'model':'fixture', 'duration_ms':1, 'usage':{'prompt_tokens':120, 'completion_tokens':8},
            'calls':[], 'message':{'role':'assistant', 'content':text}}


async def check():
    # Pure context and provider handling: no network, no database reads.
    created_result = result({'id':901,'name':'Assam Tea','price':'299.00','image_id':77}).model_dump(mode='json')
    old = [past('Create Assam Tea for 299', 'Created.'+' long prose'*1000,
        [{'name':'woo__create_product','status':'completed','result':created_result}]),
        past('Old unrelated question', 'old table '*1000)]
    for prompt in ('list 2 products','list five product','Read note 1. Do not change anything.',
                   'Show order counts without date filters. Do not change anything.'):
        assert standalone_tool(prompt), prompt
        assert previous_context(old, prompt) == []
    assert standalone_tool('Read note 1 and show order counts') is None
    context = previous_context(old, 'Now change its price to 300.')
    encoded = ''.join(item['content'] for item in context)
    assert len(encoded) <= CONTEXT_CHAR_BUDGET and '901' in encoded and '77' in encoded
    assert 'long prose'*80 not in encoded
    large_calls = [{'name':'woo__update_product', 'status':'completed', 'result':result({
        'id':902, 'image_id':88, 'name':'n'*200, 'title':'t'*200, 'price':'p'*200,
        'regular_price':'r'*200, 'status':'s'*200, 'result':'x'*200}).model_dump(mode='json')} for _ in range(3)]
    newest = previous_context([past('Latest product', 'reply'*1000, large_calls), *old], 'Change its price to 300')
    newest_text = ''.join(item['content'] for item in newest)
    assert len(newest_text) <= CONTEXT_CHAR_BUDGET and '902' in newest_text and '88' in newest_text
    assert 'other_fields_omitted' in newest_text
    assert json.loads(model_tool_result(result({'id':1,'name':'Tea'}).model_dump(mode='json')))['data']['id'] == 1
    assert json.loads(model_tool_result(result('x'*12000).model_dump(mode='json')))['truncated']
    title_tool = types.Tool(name='notes__add_note',description='Save a note',inputSchema={
        'title':'schema metadata','type':'object','properties':{'title':{'type':'string','maxLength':200}},'required':['title']})
    definitions, aliases = llm.build_tool_catalog([title_tool])
    assert definitions[0]['function']['parameters']['properties']['title']['maxLength'] == 200
    captured = []
    def provider_response(request):
        captured.append(json.loads(request.content))
        return httpx.Response(429, headers={'retry-after':'1.2','x-ratelimit-remaining-tokens':'5'},
            json={'error':{'message':'organization private-org: Tokens per minute (TPM): Limit 8000, Used 7000, Requested 2000'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_response), base_url='https://fixture.invalid') as client:
        try:
            await llm.request_completion(client, [{'role':'user','content':'fixture'}], definitions, aliases)
            raise AssertionError('429 not propagated')
        except llm.LLMError as error:
            assert error.retry_after == 2 and error.provider == 'groq'
            assert error.details['reason'] == 'tokens_per_minute' and error.details['requested'] == 2000
            assert 'private-org' not in json.dumps(error.details)
            assert error.request_metrics['outcome'] == 'rejected'
    from app.llm_metrics import retry_seconds
    assert retry_seconds(httpx.Response(429,headers={'retry-after':'3600'})) == 3600
    assert retry_seconds(httpx.Response(429,headers={'retry-after':'NaN'})) == 30
    print('Compact context, exact standalone-read routing, preserved IDs/schema constraints and safe provider diagnostics passed', flush=True)

    schema = 'urumi_chat_test_' + uuid4().hex
    created = False
    isolated = None
    temporary = None
    try:
        if args.sqlite:
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy.ext.compiler import compiles
            compiles(JSONB,'sqlite')(lambda element, compiler, **kw:'JSON')
            compiles(BigInteger,'sqlite')(lambda element, compiler, **kw:'INTEGER')
            temporary = tempfile.TemporaryDirectory(prefix='.chat-test-', dir=Path.cwd())
            isolated = create_async_engine('sqlite+aiosqlite:///'+str(Path(temporary.name)/'test.db'),connect_args={'timeout':30})
        else:
            async with engine.begin() as connection:
                await connection.execute(CreateSchema(schema))
            created = True
            isolated = create_async_engine(database_url(), hide_parameters=True,
                connect_args={'options':f'-c search_path={schema} -c timezone=UTC'})
        factory = async_sessionmaker(isolated, expire_on_commit=False)
        async with isolated.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        now = datetime.now(timezone.utc)
        cookies = {}
        async with factory() as database:
            hashed = await hash_password('fixture-pass-123')
            database.add_all([User(id=80101,email='owner@chat.invalid',role='member',active=True,password_hash=hashed),
                User(id=80102,email='other@chat.invalid',role='member',active=True,password_hash=hashed),
                User(id=80103,email='admin@chat.invalid',role='admin',active=True,password_hash=hashed),
                MCPServer(id=80101,slug='woo',name='Woo',url='http://woo.fixture.invalid/mcp',enabled=True,timeout_s=5,status='Connected'),
                MCPServer(id=80102,slug='notes',name='Notes',url='http://notes.fixture.invalid/mcp',enabled=True,timeout_s=5,status='Connected'),
                MCPServer(id=80103,slug='analytics',name='Analytics',url='http://analytics.fixture.invalid/mcp',enabled=True,timeout_s=5,status='Connected'),
                UsageLimitSettings(id=1,chat_per_minute=1000,chat_per_hour=10000,chat_global_per_minute=10000,tool_per_minute=1000)])
            await database.flush()
            for server, tool in ((80101,'list_products'),(80101,'create_product'),(80101,'update_product'),(80102,'get_note'),(80103,'order_counts')):
                database.add(ToolGrant(user_id=80101,server_id=server,tool_name=tool,granted_by_id=80103))
            for user_id in (80101,80102,80103):
                nonce = secrets.token_urlsafe(32)
                database.add(BrowserSession(id=token_hash(nonce),user_id=user_id,session_version=0,
                    last_activity_at=now,idle_expires_at=now+timedelta(minutes=30),absolute_expires_at=now+timedelta(hours=8)))
                cookies[user_id] = create_session_token(user_id,session_id=nonce)
            await database.commit()
        async def dependency():
            async with factory() as database:
                yield database
        app = FastAPI()
        for router in (chat.router,chats.router):
            app.include_router(router)
        app.dependency_overrides[get_session] = dependency
        app.add_exception_handler(rate_limits.RateLimitExceeded,rate_limits.limit_response)
        app.add_exception_handler(rate_limits.LimitsUnavailable,rate_limits.unavailable_response)
        def client(user_id=80101):
            instance = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8003')
            if user_id:
                instance.cookies.set('urm_session',cookies[user_id])
                instance.cookies.set('urm_csrf','fixture-csrf')
                instance.headers['X-CSRF-Token']='fixture-csrf'
            return instance
        async def expect(response,code):
            assert response.status_code == code,(code,response.status_code,response.text)
            return response.json() if response.content else None
        async def new_chat(instance,title='Fixture conversation'):
            identity = (await expect(await instance.post('/api/chats',json={'id':str(uuid4()),'title':title}),201))['id']
            if args.sqlite:
                # SQLite CURRENT_TIMESTAMP omits fractions; bind parameters have
                # six digits. Use consistent fixture timestamps for tuple paging.
                async with factory() as database:
                    record = await database.get(ChatConversation,UUID(identity))
                    record.created_at = datetime.now(timezone.utc)
                    await database.commit()
            return identity
        def tool(name):
            return types.Tool(name=name,description='Fixture '+name,inputSchema={'type':'object','properties':{}})
        async def discovery(url,**unused):
            names = ['list_products','create_product','update_product'] if 'woo.' in url else ['get_note'] if 'notes.' in url else ['order_counts']
            return [tool(name) for name in names]
        executions = []
        async def upstream(url,name,arguments,**unused):
            executions.append((name,arguments))
            if name == 'get_note':
                return result({'id':1,'title':'Fixture note','body':'Fixture content'})
            if name == 'order_counts':
                return result({'total_orders':2,'by_status':{'processing':2}})
            if name == 'list_products':
                return result([{'id':901,'name':'Assam Tea','price':'299.00'}])
            return result({'id':901,'name':'Assam Tea','price':str(arguments.get('regular_price','299'))})
        async def post(instance,conversation,message,request_id=None):
            payload = {'message':message,'request_id':str(request_id or uuid4())}
            if conversation:
                payload['conversation_id']=conversation
            return await instance.post('/api/chat',json=payload)
        with ExitStack() as stack:
            for module in (auth,chat_store,core,rate_limits):
                stack.enter_context(patch.object(module,'Session',factory))
            stack.enter_context(patch.object(core,'discover_tools',side_effect=discovery))
            stack.enter_context(patch.object(core,'call_tool',side_effect=upstream))
            # No API key is needed: create_llm_client itself is replaced by a local no-op context manager.
            stack.enter_context(patch.object(chat,'create_llm_client',side_effect=lambda:httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(500)))))
            async with client() as owner, client(80102) as other, client(80103) as admin, client(None) as anonymous:
                other_chat = await new_chat(other, 'Other owner private chat')
                conversation = await new_chat(owner)
                requests_seen = []
                async def read_completion(unused_client,messages,definitions,aliases):
                    requests_seen.append((messages.copy(),definitions.copy()))
                    if definitions:
                        assert list(aliases) == ['woo__list_products'],list(aliases)
                        assert len(messages)==3 and 'timestamp' not in messages[0]['content'].lower()
                        assert 'timestamp' in messages[1]['content'].lower()
                        return model_call('woo__list_products',{'per_page':2})
                    return model_reply('Assam Tea — 299.')
                with patch.object(chat,'request_completion',side_effect=read_completion):
                    for index in range(12):
                        await expect(await post(owner,conversation,'List 2 products.'),200)
                assert len(requests_seen)==24
                assert len({len(json.dumps(messages[0])) for messages,defs in requests_seen})==1
                assert len(executions)==12
                print('Twelve messages in one chat: standalone requests omit history, offer one permitted tool, then summarize without tool schemas',flush=True)

                follow = await new_chat(owner,'Price clarification')
                with patch.object(chat,'request_completion',return_value=model_reply('What regular price should Assam Tea have?')):
                    await expect(await post(owner,follow,'Create Assam Tea'),200)
                async def price_completion(unused_client,messages,definitions,aliases):
                    serialized=json.dumps(messages)
                    assert 'Assam Tea' in serialized and 'regular price' in serialized
                    if not any(item.get('role')=='tool' for item in messages):
                        return model_call('woo__create_product',{'name':'Assam Tea','regular_price':299})
                    return model_reply('Created Assam Tea, ID 901, price 299.')
                before=len(executions)
                with patch.object(chat,'request_completion',side_effect=price_completion):
                    await expect(await post(owner,follow,'299'),200)
                assert executions[before:] == [('create_product',{'name':'Assam Tea','regular_price':299})]
                print('Compact clarification context preserves product name; a price reply executes one creation in the controlled fixture',flush=True)

                def blocked():
                    return llm.LLMError('Fixture Groq limit.',429,13,provider='groq',
                        details={'reason':'tokens_per_minute'},request_metrics={'outcome':'rejected','usage':{},'duration_ms':2})
                read_calls=0
                async def partial_completion(unused_client,messages,definitions,aliases):
                    nonlocal read_calls
                    read_calls += 1
                    if read_calls==1:
                        return model_call('notes__get_note',{'note_id':1})
                    raise blocked()
                request_id=uuid4()
                partial = await new_chat(owner,'Partial read')
                before=len(executions)
                with patch.object(chat,'request_completion',side_effect=partial_completion) as mocked:
                    data=await expect(await post(owner,partial,'Read note 1.',request_id),429)
                    assert data['tool_calls'][0]['status']=='completed' and data['error']['phase']=='after_tools'
                    assert data['error']['provider']=='groq' and len(data['model_requests'])==2
                    replay=await expect(await post(owner,partial,'Read note 1.',request_id),429)
                    assert replay==data and mocked.call_count==2 and len(executions)==before+1
                saved=await expect(await owner.get('/api/chats/'+partial),200)
                assert saved['turns'][0]['error_provider']=='groq' and len(saved['turns'][0]['model_requests'])==2
                before=len(executions)
                with patch.object(chat,'request_completion',side_effect=blocked()):
                    data=await expect(await post(owner,conversation,'List 5 products.'),429)
                    assert data['error']['phase']=='before_tools' and data['tool_calls']==[]
                    assert len(executions)==before
                print('Provider rejection before/after tools preserves real results, persisted diagnostics and duplicate replay without repeat execution',flush=True)

                compound=await new_chat(owner,'Two upstreams')
                count=0
                async def compound_completion(unused_client,messages,definitions,aliases):
                    nonlocal count
                    count += 1
                    assert 'notes__get_note' in aliases and 'analytics__order_counts' in aliases
                    if count==1: return model_call('notes__get_note',{'note_id':1})
                    if count==2: return model_call('analytics__order_counts',{})
                    return model_reply('Fixture note and two orders.')
                with patch.object(chat,'request_completion',side_effect=compound_completion):
                    data=await expect(await post(owner,compound,'Read note 1 and show order counts.'),200)
                    assert len(data['tool_calls'])==2
                # Revoke after catalog discovery; the core must still block execution.
                async with factory() as database:
                    await database.execute(delete(ToolGrant).where(ToolGrant.user_id==80101,ToolGrant.tool_name=='get_note'))
                    user=await database.get(User,80101)
                    await database.commit()
                before=len(executions)
                denied=await core.execute_gateway_tool(user,'notes__get_note',{'note_id':1},source='chat')
                assert denied.isError and len(executions)==before
                print('Compound two-upstream requests remain supported; revoked permission blocks execution independently of model context',flush=True)

                target='/api/chats/'+partial
                await expect(await anonymous.delete(target),401)
                await expect(await other.delete(target),404)
                await expect(await admin.delete(target),404)
                owner.headers.pop('X-CSRF-Token')
                await expect(await owner.delete(target),403)
                owner.headers['X-CSRF-Token']='fixture-csrf'
                owner.headers['Origin']='http://forged.invalid'
                await expect(await owner.delete(target),403)
                owner.headers.pop('Origin')
                async with factory() as database:
                    audit_before=await database.scalar(select(func.count()).select_from(AuditLog))
                await expect(await owner.delete(target),204)
                await expect(await owner.delete(target),204)
                await expect(await owner.get(target),404)
                listing=await expect(await owner.get('/api/chats'),200)
                assert partial not in [item['id'] for item in listing['items']]
                assert other_chat not in [item['id'] for item in listing['items']]
                page=await expect(await owner.get('/api/chats?limit=1'),200)
                assert page['next_before_id']
                older=await expect(await owner.get('/api/chats?limit=1&before_id='+page['next_before_id']),200)
                assert older['items'][0]['id'] != page['items'][0]['id']
                await expect(await owner.get('/api/chats?before_id='+partial),404)
                await expect(await owner.post('/api/chats',json={'id':partial,'title':'Revive'}),404)
                before=len(executions)
                with patch.object(chat,'request_completion',side_effect=AssertionError('Deleted request reached model')):
                    await expect(await post(owner,None,'Read note 1.',request_id),404)
                    await expect(await post(owner,partial,'New message'),404)
                assert len(executions)==before
                async with factory() as database:
                    parent=await database.get(ChatConversation,UUID(partial))
                    turns=list(await database.scalars(select(ChatTurn).where(ChatTurn.conversation_id==parent.id)))
                    assert parent.title=='Deleted conversation' and parent.deleted_at
                    assert all(turn.message=='' and turn.response=={} and turn.image_id is None and turn.image_name=='' for turn in turns)
                    assert await database.scalar(select(func.count()).select_from(AuditLog))==audit_before
                print('Deletion enforces owner/CSRF/origin, erases messages/results, hides chats, prevents ID resurrection/replay and preserves audits',flush=True)

                active=await new_chat(owner,'Running request')
                entered, release = asyncio.Event(), asyncio.Event()
                async def slow_completion(*unused,**kwargs):
                    entered.set()
                    await release.wait()
                    return model_reply('Finished.')
                with patch.object(chat,'request_completion',side_effect=slow_completion):
                    task=asyncio.create_task(post(owner,active,'Fixture pending request'))
                    await asyncio.wait_for(entered.wait(),10)
                    await expect(await owner.delete('/api/chats/'+active),409)
                    release.set()
                    await expect(await task,200)
                await expect(await owner.delete('/api/chats/'+active),204)
                expired = await new_chat(owner,'Expired worker fixture')
                expired_turn = uuid4()
                async with factory() as database:
                    database.add(ChatTurn(id=expired_turn,conversation_id=UUID(expired),message='Disposable expired text',
                        image_id=77,image_name='fixture.jpg',status='running',deadline_at=datetime.now(timezone.utc)-timedelta(seconds=1),
                        response={'tool_calls':[]}))
                    await database.commit()
                await expect(await owner.delete('/api/chats/'+expired),204)
                for operation in (chat_store.finish_turn(80101,expired_turn,{'reply':'Late worker data'},200),
                                  chat_store.save_progress(80101,expired_turn,{'reply':'Late worker data'})):
                    try:
                        await operation
                        raise AssertionError('Late worker revived deleted content')
                    except RuntimeError:
                        pass
                await expect(await owner.get('/api/chats/'+expired),404)
                print('Expired work may be deleted; late progress/finalization cannot restore deleted content',flush=True)
                if not args.sqlite:
                    # Force DELETE and begin_turn to contend for the same parent row.
                    race=await new_chat(owner,'Race fixture')
                    identity=UUID(race)
                    async with factory() as lock:
                        await lock.scalar(select(ChatConversation).where(ChatConversation.id==identity).with_for_update())
                        deleting=asyncio.create_task(owner.delete('/api/chats/'+race))
                        async def begin_race():
                            try:
                                return await chat_store.begin_turn(80101,identity,uuid4(),'Fixture race',None,'')
                            except HTTPException as error:
                                return error.status_code
                        beginning=asyncio.create_task(begin_race())
                        await asyncio.sleep(0.1)
                        await lock.commit()
                    deleted_response, started = await asyncio.gather(deleting,beginning)
                    if isinstance(started,dict):
                        assert deleted_response.status_code==409
                        await chat_store.finish_turn(80101,started['turn_id'],{'reply':'Fixture'},200)
                        await expect(await owner.delete('/api/chats/'+race),204)
                    else:
                        assert started==404 and deleted_response.status_code==204
                    print('PostgreSQL parent-row contention: delete and begin cannot admit work into a deleted chat',flush=True)
                print('Running requests block deletion until completion',flush=True)
        print('Chat efficiency verification passed. No live accounts or external services changed.',flush=True)
    finally:
        if isolated is not None:
            await isolated.dispose()
        if created:
            assert schema.startswith('urumi_chat_test_')
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema,cascade=True))
            print('Temporary PostgreSQL schema removed.',flush=True)
        if temporary:
            assert Path(temporary.name).resolve().parent == Path.cwd().resolve()
            temporary.cleanup()
        await engine.dispose()


asyncio.run(check())
