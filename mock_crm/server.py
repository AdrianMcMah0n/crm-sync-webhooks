"""A small CRM API that behaves like real ones do, so the sync can be tested without an account:
bearer-token auth, search by email, create (409 on a duplicate email), update, cursor pagination,
a rate limit that answers 429 with Retry-After, and occasional 500 errors (CHAOS share of requests)."""
import itertools
import os
import random
import threading
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

TOKEN = os.environ.get('MOCK_CRM_TOKEN', 'demo-token')


def create_app(rate_per_sec: int = 8, chaos: float = 0.05, seed: int = 7) -> FastAPI:
    app = FastAPI(title='Mock CRM')
    contacts: dict[str, dict] = {}
    ids = itertools.count(1001)
    lock = threading.Lock()
    window = {'start': time.monotonic(), 'count': 0}
    rnd = random.Random(seed)
    app.state.stats = {'requests': 0, 'rate_limited': 0, 'server_errors': 0}

    @app.middleware('http')
    async def limits(request: Request, call_next):
        with lock:
            app.state.stats['requests'] += 1
            now = time.monotonic()
            if now - window['start'] >= 1:
                window['start'], window['count'] = now, 0
            window['count'] += 1
            if window['count'] > rate_per_sec:
                app.state.stats['rate_limited'] += 1
                return JSONResponse({'error': 'rate limit'}, status_code=429, headers={'Retry-After': '1'})
            if rnd.random() < chaos:
                app.state.stats['server_errors'] += 1
                return JSONResponse({'error': 'internal error'}, status_code=500)
        return await call_next(request)

    def auth(authorization: str | None):
        if authorization != f'Bearer {TOKEN}':
            raise HTTPException(401, 'bad token')

    @app.post('/v1/contacts/search')
    def search(body: dict, authorization: str | None = Header(None)):
        auth(authorization)
        email = (body.get('email') or '').lower()
        return {'results': [c for c in contacts.values() if c['email'] == email]}

    @app.post('/v1/contacts', status_code=201)
    def create(body: dict, authorization: str | None = Header(None)):
        auth(authorization)
        email = (body.get('email') or '').lower()
        if any(c['email'] == email for c in contacts.values()):
            raise HTTPException(409, 'a contact with this email already exists')
        c = {**body, 'email': email, 'id': str(next(ids))}
        contacts[c['id']] = c
        return c

    @app.patch('/v1/contacts/{cid}')
    def update(cid: str, body: dict, authorization: str | None = Header(None)):
        auth(authorization)
        if cid not in contacts:
            raise HTTPException(404, 'not found')
        contacts[cid].update({k: v for k, v in body.items() if k != 'id'})
        return contacts[cid]

    @app.get('/v1/contacts')
    def list_contacts(after: int = 0, limit: int = 50, authorization: str | None = Header(None)):
        auth(authorization)
        rows = sorted(contacts.values(), key=lambda c: int(c['id']))
        page = [c for c in rows if int(c['id']) > after][:limit]
        nxt = int(page[-1]['id']) if len(page) == limit else None
        return {'results': page, 'next_after': nxt}

    return app
