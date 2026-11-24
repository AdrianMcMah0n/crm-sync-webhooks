"""CRM clients with one interface (find_by_email / create / update / list_all) and one retry policy.

Retry policy: 429 -> wait the server's Retry-After; 5xx or network error -> exponential back-off
(0.5, 1, 2, 4 s); give up after 5 attempts and raise, so the caller can dead-letter the lead.
"""
import time

import httpx


class CRMError(Exception):
    pass


class BaseClient:
    def __init__(self, http: httpx.Client, max_attempts: int = 5, sleep=time.sleep):
        self.http, self.max_attempts, self.sleep = http, max_attempts, sleep
        self.retries = {'429': 0, '5xx': 0, 'network': 0}

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(self.max_attempts):
            try:
                r = self.http.request(method, url, **kw)
            except httpx.TransportError:
                self.retries['network'] += 1
                self.sleep(0.5 * 2 ** attempt)
                continue
            if r.status_code == 429:
                self.retries['429'] += 1
                self.sleep(float(r.headers.get('Retry-After', 1)))
                continue
            if r.status_code >= 500:
                self.retries['5xx'] += 1
                self.sleep(0.5 * 2 ** attempt)
                continue
            return r
        raise CRMError(f'{method} {url} failed after {self.max_attempts} attempts')


class MockCRMClient(BaseClient):
    """For mock_crm/server.py (and any CRM with the same simple REST shape)."""

    def find_by_email(self, email: str) -> dict | None:
        r = self._request('POST', '/v1/contacts/search', json={'email': email})
        r.raise_for_status()
        res = r.json()['results']
        return res[0] if res else None

    def create(self, props: dict) -> dict:
        r = self._request('POST', '/v1/contacts', json=props)
        if r.status_code == 409:  # created by a parallel request in the meantime: treat as existing
            return self.find_by_email(props['email'])
        r.raise_for_status()
        return r.json()

    def update(self, cid: str, props: dict) -> dict:
        r = self._request('PATCH', f'/v1/contacts/{cid}', json=props)
        r.raise_for_status()
        return r.json()

    def list_all(self) -> list[dict]:
        out, after = [], 0
        while after is not None:
            r = self._request('GET', '/v1/contacts', params={'after': after, 'limit': 50})
            r.raise_for_status()
            body = r.json()
            out += body['results']
            after = body['next_after']
        return out


class HubSpotClient(BaseClient):
    """HubSpot CRM v3 contacts API (private-app token). Same interface as MockCRMClient.
    http = httpx.Client(base_url='https://api.hubapi.com', headers={'Authorization': 'Bearer <token>'})"""
    PROPS = ['email', 'firstname', 'lastname', 'phone', 'company']

    @staticmethod
    def _flat(obj: dict) -> dict:
        return {'id': obj['id'], **obj.get('properties', {})}

    def find_by_email(self, email: str) -> dict | None:
        body = {'filterGroups': [{'filters': [{'propertyName': 'email', 'operator': 'EQ', 'value': email}]}],
                'properties': self.PROPS, 'limit': 1}
        r = self._request('POST', '/crm/v3/objects/contacts/search', json=body)
        r.raise_for_status()
        res = r.json().get('results', [])
        return self._flat(res[0]) if res else None

    def create(self, props: dict) -> dict:
        r = self._request('POST', '/crm/v3/objects/contacts', json={'properties': {k: v for k, v in props.items() if k in self.PROPS}})
        if r.status_code == 409:
            return self.find_by_email(props['email'])
        r.raise_for_status()
        return self._flat(r.json())

    def update(self, cid: str, props: dict) -> dict:
        r = self._request('PATCH', f'/crm/v3/objects/contacts/{cid}', json={'properties': {k: v for k, v in props.items() if k in self.PROPS}})
        r.raise_for_status()
        return self._flat(r.json())

    def list_all(self) -> list[dict]:
        out, after = [], None
        while True:
            params = {'limit': 100, 'properties': ','.join(self.PROPS), **({'after': after} if after else {})}
            r = self._request('GET', '/crm/v3/objects/contacts', params=params)
            r.raise_for_status()
            body = r.json()
            out += [self._flat(o) for o in body.get('results', [])]
            after = body.get('paging', {}).get('next', {}).get('after')
            if not after:
                return out
