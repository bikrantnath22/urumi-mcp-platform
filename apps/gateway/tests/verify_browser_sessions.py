"""Verify browser-session security in a disposable schema, without live accounts.

Run from apps/gateway with the local environment sourced. --sqlite runs local
portable checks; default PostgreSQL additionally checks real row-lock concurrency.
"""
import argparse
import asyncio
from datetime import timedelta
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

args = argparse.ArgumentParser()
args.add_argument('--sqlite', action='store_true')
args = args.parse_args()
if not (Path.cwd()/'app/browser_sessions.py').is_file():
    raise SystemExit('Run from apps/gateway after applying the session update.')
if args.sqlite:
    from cryptography.fernet import Fernet
    os.environ['PGPASSWORD'] = 'isolated-test-not-used'
    os.environ['JWT_SECRET'] = 'isolated-session-test-' * 3
    os.environ['FERNET_KEY'] = Fernet.generate_key().decode()
    os.environ['COOKIE_SECURE'] = 'false'
sys.path.insert(0, str(Path.cwd()))

import httpx
from fastapi import FastAPI
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from app import auth, browser_sessions
from app import rate_limits
from app.models import UsageLimitSettings
from app.config import database_url
from app.database import engine, get_session
from app.models import Base, BrowserSession, User
from app.security import create_session_token, hash_password, read_session_token, token_hash


async def check():
    schema = 'urumi_session_test_' + uuid4().hex
    isolated = None
    created = False
    try:
        if args.sqlite:
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy.ext.compiler import compiles
            compiles(JSONB, 'sqlite')(lambda element, compiler, **kw: 'JSON')
            compiles(BigInteger, 'sqlite')(lambda element, compiler, **kw: 'INTEGER')
            isolated = create_async_engine('sqlite+aiosqlite:///:memory:')
        else:
            async with engine.begin() as connection:
                await connection.execute(CreateSchema(schema))
            created = True
            isolated = create_async_engine(database_url(), hide_parameters=True,
                connect_args={'options': f'-c search_path={schema} -c timezone=UTC'})
        factory = async_sessionmaker(isolated, expire_on_commit=False)
        async with isolated.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        password = 'fixture-pass-123'
        async with factory() as database:
            database.add(UsageLimitSettings(id=1, login_account_limit=100, login_ip_limit=1000))
            database.add(User(id=60101, email='session@fixture.invalid', role='member', active=True,
                password_hash=await hash_password(password)))
            await database.commit()
        async def dependency():
            async with factory() as database:
                yield database
        app = FastAPI()
        app.include_router(auth.router)
        app.dependency_overrides[get_session] = dependency
        def client():
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8003')
        async def expect(response, code):
            assert response.status_code == code, (response.status_code, response.text)
            return response.json() if response.content else None
        async def login(instance):
            await expect(await instance.post('/api/auth/login', json={'email':'session@fixture.invalid','password':password}), 200)
            instance.headers['X-CSRF-Token'] = instance.cookies['urm_csrf']
            _, _, nonce = read_session_token(instance.cookies['urm_session'], include_session=True)
            return token_hash(nonce)
        with patch.object(auth, 'Session', factory), patch.object(rate_limits, 'Session', factory):
            async with client() as member:
                session_id = await login(member)
                token = member.cookies['urm_session']
                csrf = member.cookies['urm_csrf']
                async with factory() as database:
                    record = await database.get(BrowserSession, session_id)
                    start = browser_sessions.aware(record.created_at)
                    absolute = browser_sessions.aware(record.absolute_expires_at)
                    assert absolute-start == timedelta(hours=8)
                    assert browser_sessions.aware(record.idle_expires_at)-start == timedelta(minutes=30)
                    _, _, nonce = read_session_token(token, include_session=True)
                    assert record.id == token_hash(nonce) and record.id != nonce
                await expect(await member.post('/api/auth/renew', json={}, headers={'X-CSRF-Token':''}), 403)
                await expect(await member.post('/api/auth/renew', json={}, headers={'Origin':'https://untrusted.invalid'}), 403)
                await expect(await member.post('/api/auth/renew', json={'user_id':60102}), 422)
                print('Renewal: authentication, CSRF, origin and caller-override checks passed')
                # Genuine renewal keeps work alive beyond the former one-hour limit.
                for minute in (20,40,60,80):
                    with patch.object(browser_sessions, 'utcnow', return_value=start+timedelta(minutes=minute)):
                        await expect(await member.post('/api/auth/renew', json={}), 200)
                    async with factory() as database:
                        record = await database.get(BrowserSession, session_id)
                        assert browser_sessions.aware(record.idle_expires_at) == start+timedelta(minutes=minute+30)
                        assert browser_sessions.aware(record.absolute_expires_at) == absolute
                async with factory() as database:
                    before = browser_sessions.aware((await database.get(BrowserSession, session_id)).idle_expires_at)
                with patch.object(browser_sessions, 'utcnow', return_value=start+timedelta(minutes=90)):
                    await expect(await member.get('/api/auth/me'), 200)
                    await expect(await member.get('/api/auth/session'), 200)
                    assert 'no-store' in (await member.get('/api/auth/session')).headers['cache-control']
                async with factory() as database:
                    assert browser_sessions.aware((await database.get(BrowserSession, session_id)).idle_expires_at) == before
                with patch.object(browser_sessions, 'utcnow', return_value=start+timedelta(minutes=110)):
                    await expect(await member.get('/api/auth/me'), 401)
                    await expect(await member.post('/api/auth/renew', json={}), 401)
                print('Active work renews past one hour; reads/polling do not renew; exact 30-minute idle expiry cannot revive')
                session_id = await login(member)
                async with factory() as database:
                    record = await database.get(BrowserSession, session_id)
                    start = browser_sessions.aware(record.created_at)
                    record.absolute_expires_at = start+timedelta(minutes=1)
                    record.idle_expires_at = record.absolute_expires_at
                    await database.commit()
                with patch.object(browser_sessions, 'utcnow', return_value=start+timedelta(seconds=50)):
                    await expect(await member.post('/api/auth/renew', json={}), 200)
                async with factory() as database:
                    record = await database.get(BrowserSession, session_id)
                    assert browser_sessions.aware(record.idle_expires_at) == start+timedelta(minutes=1)
                with patch.object(browser_sessions, 'utcnow', return_value=start+timedelta(minutes=1)):
                    await expect(await member.post('/api/auth/renew', json={}), 401)
                print('Absolute expiry caps renewal and cannot be extended')
                session_id = await login(member)
                token, csrf = member.cookies['urm_session'], member.cookies['urm_csrf']
                key = await expect(await member.post('/api/keys', json={'name':'Session-independent fixture'}), 201)
                await expect(await member.post('/api/auth/logout'), 204)
                async with client() as replay:
                    replay.cookies.set('urm_session', token)
                    replay.cookies.set('urm_csrf', csrf)
                    replay.headers['X-CSRF-Token'] = csrf
                    await expect(await replay.get('/api/auth/me'), 401)
                    await expect(await replay.post('/api/auth/renew', json={}), 401)
                    await expect(await replay.get('/api/key-check', headers={'Authorization':'Bearer '+key['key']}), 200)
                print('Logout revokes the database session; cookie replay rejected; API key remains independent')
                session_id = await login(member)
                async with factory() as database:
                    user = await database.get(User, 60101)
                    user.active = False; user.session_version += 1
                    await database.commit()
                await expect(await member.post('/api/auth/renew', json={}), 401)
                async with factory() as database:
                    user = await database.get(User, 60101)
                    user.active = True; user.session_version += 1
                    await database.commit()
                await expect(await member.get('/api/auth/me'), 401)
                await expect(await member.post('/api/auth/renew', json={}), 401)
                print('Disable and re-enable do not revive old browser sessions')
                async with client() as legacy:
                    legacy.cookies.set('urm_session', create_session_token(60101, 2))
                    await expect(await legacy.get('/api/auth/me'), 401)
                # A new login replaces the prior session in this browser.
                first = await login(member)
                old_token, old_csrf = member.cookies['urm_session'], member.cookies['urm_csrf']
                second = await login(member)
                assert second != first
                async with client() as replay:
                    replay.cookies.set('urm_session', old_token)
                    replay.cookies.set('urm_csrf', old_csrf)
                    replay.headers['X-CSRF-Token'] = old_csrf
                    await expect(await replay.get('/api/auth/me'), 401)
                print('Legacy JWTs rejected; repeated login invalidates the previous browser session')
                if not args.sqlite:
                    token, csrf = member.cookies['urm_session'], member.cookies['urm_csrf']
                    async with client() as parallel:
                        parallel.cookies.set('urm_session', token)
                        parallel.cookies.set('urm_csrf', csrf)
                        parallel.headers['X-CSRF-Token'] = csrf
                        responses = await asyncio.gather(member.post('/api/auth/renew', json={}), parallel.post('/api/auth/logout'))
                        assert responses[0].status_code in (200,401) and responses[1].status_code == 204
                        async with factory() as database:
                            assert (await database.get(BrowserSession, second)).revoked_at is not None
                    print('PostgreSQL concurrent logout/renewal: revoked session cannot be resurrected')
        print('Session verification passed. No live accounts or upstream data changed.')
    finally:
        if isolated is not None:
            await isolated.dispose()
        if created:
            assert schema.startswith('urumi_session_test_')
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
            print('Temporary test schema removed.')
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(check())
