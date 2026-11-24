# Web-form leads to CRM: signed webhooks, retries, no duplicates

A lead-sync service of the kind most businesses need between their website forms and their CRM. Form submissions arrive on a signed webhook, are cleaned and validated, matched to existing contacts by email, and created or updated in the CRM. It survives what real CRMs do: rate limits, random server errors and webhook re-deliveries. Running everything twice creates nothing new.

Includes a working **HubSpot** connector (CRM v3 API) and a local **test CRM**, so the whole flow runs on a laptop without any account. All people and companies in the demo are invented.

## The problem

"Send website leads into the CRM" sounds like one API call. In practice:

- the CRM answers **429 Too Many Requests** under load and sometimes **500**;
- form tools **re-deliver** the same webhook when they do not get a fast reply;
- the same person signs up twice, typing `TOM@Example.com ` the second time;
- a second form with an empty "company" field must not wipe the company already in the CRM;
- anyone can POST to a public webhook URL unless requests are verified.

Get any of these wrong and the CRM fills with duplicates, missing data or junk.

## What it does

```
form tool --signed POST--> webhook (FastAPI) --202 at once--> background worker
                               |                                  |
                         bad signature: 401              normalise + validate
                                                                  |
                                       ledger (SQLite) <-- search CRM by email --> create / update / unchanged
                                       (idempotency,              |
                                        dead letters)      429: wait Retry-After; 5xx: back off 0.5, 1, 2, 4 s
```

- **Signed webhooks:** HMAC-SHA256 of the body with a shared secret, checked in constant time. Forged requests are refused with 401.
- **Fast acknowledgement:** valid requests get 202 straight away and are processed in the background, so the form tool does not time out and re-send.
- **Idempotent:** every submission id is recorded in a ledger, so a re-delivered webhook is skipped before it reaches the CRM.
- **Clean data:** emails trimmed and lower-cased; names fixed only when typed in all caps or all lower case; phones converted to international E.164 by country (US, UK, India...). An invalid email rejects the lead with the reason; an invalid phone is dropped but the lead is kept.
- **Safe updates:** an existing contact gets only new or missing information. A blank form field never overwrites CRM data.
- **Retries:** 429 waits for the CRM's `Retry-After`; 5xx and network errors back off exponentially; after 5 attempts the lead goes to a **dead-letter** table that can be replayed later.

## Result of the demo run

`python demo.py` starts the test CRM (8 requests per second limit, 5% random server errors) and the webhook, then sends 130 submissions plus 3 forged ones, then sends the whole batch **again**:

| Check | Result |
|---|---|
| Forged requests refused | 3 of 3 |
| Webhook re-deliveries skipped | 20 |
| Leads rejected (invalid email) | 6, each with the reason |
| Contacts created | 74 |
| Contacts updated (new phone, filled blanks) | 12 |
| Repeat sign-ups with nothing new | 18 |
| 429 rate-limit answers absorbed | 22 |
| 5xx server errors absorbed | 11 |
| Leads lost (dead letters left) | 0 |
| Contacts in the CRM vs distinct valid people sent | 74 = 74 (no duplicates) |
| New contacts created by sending everything again | 0 |

The run also writes [`sample_output/crm_sync_report.xlsx`](sample_output/crm_sync_report.xlsx): summary, final CRM contacts, and a sync log with one line per submission (created / updated with the changed fields / rejected with the reason).

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (macOS / Linux: source .venv/bin/activate)
pip install -r requirements.txt

python demo.py                     # about 35 seconds, no accounts needed
pytest                             # 7 tests, no network
```

### Using HubSpot instead of the test CRM

Create a HubSpot private app with the contacts read and write scopes, then:

```python
import httpx
from sync.crm_client import HubSpotClient
from sync.pipeline import SyncPipeline

hubspot = HubSpotClient(httpx.Client(base_url='https://api.hubapi.com',
                                     headers={'Authorization': 'Bearer <private-app-token>'}))
pipeline = SyncPipeline(hubspot, 'ledger.db')
```

The HubSpot connector uses the CRM v3 contacts endpoints (search by email, create, update, paged listing) and is tested against responses in HubSpot's documented format (`tests/test_sync.py`). Other CRMs (GoHighLevel, Zoho, Pipedrive, Salesforce) need only a new client class with the same four methods.

## Files

| Path | Job |
|---|---|
| `sync/webhook.py` | Signed webhook endpoint, background processing |
| `sync/normalize.py` | Email, name and phone cleaning and validation |
| `sync/pipeline.py` | Ledger, match by email, create or safe update, dead letters and replay |
| `sync/crm_client.py` | Retry policy plus the test-CRM and HubSpot clients |
| `mock_crm/server.py` | Local CRM with auth, rate limit, random errors and pagination |
| `demo.py` | The end-to-end run and Excel report |
| `tests/` | Normalisation, retries, idempotency, no-wipe updates, dead letters, signatures, HubSpot mapping |

## License

MIT
