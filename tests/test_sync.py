import json

import httpx
from fastapi.testclient import TestClient

from mock_crm.server import TOKEN, create_app as crm_app
from sync.crm_client import CRMError, HubSpotClient, MockCRMClient
from sync.normalize import normalize
from sync.pipeline import SyncPipeline
from sync.webhook import create_app as hook_app, sign


def crm(chaos=0.0):
    http = TestClient(crm_app(rate_per_sec=10_000, chaos=chaos), headers={'Authorization': f'Bearer {TOKEN}'})
    return MockCRMClient(http, sleep=lambda s: None)


def test_normalize_cleans_and_flags():
    lead, problems = normalize({'email': '  Maya.Reed@Example.COM ', 'first_name': 'MAYA', 'last_name': 'reed',
                                'phone': '(415) 555-0123', 'country': 'US'})
    assert lead['email'] == 'maya.reed@example.com' and lead['firstname'] == 'Maya' and lead['lastname'] == 'Reed'
    assert lead['phone'] == '+14155550123' and problems == []
    assert normalize({'email': '07700 900123', 'first_name': 'A'})[1] == ["invalid email '07700 900123'"]
    uk = normalize({'email': 'a@b.co', 'first_name': 'A', 'phone': '020 7946 0123', 'country': 'GB'})[0]
    assert uk['phone'] == '+442079460123'
    kept, why = normalize({'email': 'a@b.co', 'first_name': 'A', 'phone': 'call me'})
    assert kept['phone'] == '' and why == ["invalid phone 'call me' (dropped, lead kept)"]


def test_retry_honours_retry_after_then_succeeds():
    calls, waits = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': '2'})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={'results': []})
    c = MockCRMClient(httpx.Client(transport=httpx.MockTransport(handler), base_url='http://crm'), sleep=waits.append)
    assert c.find_by_email('a@b.co') is None
    assert waits == [2.0, 1.0] and c.retries == {'429': 1, '5xx': 1, 'network': 0}


def test_gives_up_after_max_attempts():
    c = MockCRMClient(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url='http://crm'),
                      sleep=lambda s: None)
    try:
        c.find_by_email('a@b.co')
        assert False
    except CRMError:
        pass


def test_pipeline_idempotent_and_never_wipes(tmp_path):
    pipe = SyncPipeline(crm(), tmp_path / 'ledger.db')
    s1 = {'submission_id': 'S1', 'email': 'tom@example.com', 'first_name': 'Tom', 'company': 'Bluefin Dental', 'phone': '4155550100'}
    assert pipe.process(s1) == 'created'
    assert pipe.process(s1).startswith('skipped')                      # webhook re-delivery
    s2 = {'submission_id': 'S2', 'email': 'TOM@example.com ', 'first_name': 'Tom', 'company': '', 'phone': '4155550199'}
    assert pipe.process(s2) == 'updated'                                # new phone, blank company ignored
    c = pipe.crm.find_by_email('tom@example.com')
    assert c['company'] == 'Bluefin Dental' and c['phone'] == '+14155550199'
    assert len(pipe.crm.list_all()) == 1
    assert pipe.process({'submission_id': 'S3', 'email': 'broken', 'first_name': 'X'}) == 'rejected'


def test_failed_lead_goes_to_dead_letters_and_replays(tmp_path):
    flaky = MockCRMClient(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url='http://crm'),
                          sleep=lambda s: None)
    pipe = SyncPipeline(flaky, tmp_path / 'ledger.db')
    assert pipe.process({'submission_id': 'S1', 'email': 'a@example.com', 'first_name': 'A'}) == 'failed'
    pipe.crm = crm()                                                     # the CRM is back
    assert pipe.replay_dead_letters() == {'S1': 'created'}


def test_webhook_rejects_bad_signatures(tmp_path):
    pipe = SyncPipeline(crm(), tmp_path / 'ledger.db')
    client = TestClient(hook_app(pipe, 'secret'))
    body = json.dumps({'submission_id': 'S9', 'email': 'zara@example.com', 'first_name': 'Zara'}).encode()
    assert client.post('/webhooks/form', content=body, headers={'X-Signature': sign('wrong', body)}).status_code == 401
    assert client.post('/webhooks/form', content=body).status_code == 401
    assert client.post('/webhooks/form', content=body, headers={'X-Signature': sign('secret', body)}).status_code == 202
    assert pipe.counts() == {'created': 1}


def test_hubspot_client_maps_the_v3_api():
    store, seen = {}, []

    def hubspot(request: httpx.Request):
        seen.append((request.method, request.url.path))
        if request.url.path == '/crm/v3/objects/contacts/search':
            email = json.loads(request.content)['filterGroups'][0]['filters'][0]['value']
            hits = [{'id': k, 'properties': v} for k, v in store.items() if v['email'] == email]
            return httpx.Response(200, json={'total': len(hits), 'results': hits})
        if request.method == 'POST' and request.url.path == '/crm/v3/objects/contacts':
            cid = str(len(store) + 51)
            store[cid] = json.loads(request.content)['properties']
            return httpx.Response(201, json={'id': cid, 'properties': store[cid]})
        if request.method == 'PATCH':
            cid = request.url.path.rsplit('/', 1)[1]
            store[cid].update(json.loads(request.content)['properties'])
            return httpx.Response(200, json={'id': cid, 'properties': store[cid]})
        if request.method == 'GET':
            after = request.url.params.get('after')
            ids = sorted(store)
            page = ids[:1] if not after else [i for i in ids if i > after][:1]
            nxt = {'paging': {'next': {'after': page[-1]}}} if page and page[-1] != ids[-1] else {}
            return httpx.Response(200, json={'results': [{'id': i, 'properties': store[i]} for i in page], **nxt})
        return httpx.Response(404)
    hs = HubSpotClient(httpx.Client(transport=httpx.MockTransport(hubspot), base_url='https://api.hubapi.com'), sleep=lambda s: None)
    a = hs.create({'email': 'a@example.com', 'firstname': 'A', 'source': 'web form'})   # unknown props are not sent
    hs.create({'email': 'b@example.com', 'firstname': 'B'})
    hs.update(a['id'], {'phone': '+14155550100'})
    assert hs.find_by_email('a@example.com')['phone'] == '+14155550100'
    assert 'source' not in store[a['id']]
    assert [c['email'] for c in hs.list_all()] == ['a@example.com', 'b@example.com']  # followed paging.next.after
    assert ('POST', '/crm/v3/objects/contacts/search') in seen
