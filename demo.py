"""End-to-end demo: a test CRM and the webhook run locally, 140 signed form submissions arrive (with the usual
mess: repeat sign-ups, re-delivered webhooks, bad emails and phones, forged requests), then the same batch is
sent AGAIN to prove re-runs create nothing new. Writes sample_output/crm_sync_report.xlsx.

    python demo.py
All people and companies are invented.
"""
import json
import random
import threading
import time
from pathlib import Path

import httpx
import pandas as pd
import uvicorn

from mock_crm.server import TOKEN, create_app as crm_app
from sync.crm_client import MockCRMClient
from sync.pipeline import SyncPipeline
from sync.webhook import create_app as hook_app, sign

SECRET = 'form-tool-shared-secret'
CRM_PORT, HOOK_PORT = 8801, 8802
OUT = Path('sample_output')
FIRST = ['maya', 'TOM', 'Priya', 'leo', 'Ana', 'Sam', 'Ravi', 'Grace', 'Omar', 'Chloe', 'Arjun', 'Ella', 'Noah', 'Zara']
LAST = ['Reed', 'HALL', 'kapoor', 'Brooks', 'Silva', 'Ng', 'Menon', 'Turner', 'Haddad', 'Moss', 'Iyer', 'Price']
COMPANIES = ['Bluefin Dental', 'Northgate Realty', 'Cedar & Co Accounting', 'Brightpath Tutors', 'Harbor Fitness',
             'Lumen Studio', 'Oakline Logistics', 'Pinecrest Clinic']
PHONES = {'US': '(415) 555-0{:03d}', 'GB': '020 7946 0{:03d}', 'IN': '+91 98200 0{:04d}'}


def people(rnd: random.Random, n: int = 70) -> list[dict]:
    out = []
    for i in range(n):
        f, l, country = rnd.choice(FIRST), rnd.choice(LAST), rnd.choice(['US', 'US', 'GB', 'IN'])
        out.append({'first_name': f, 'last_name': l, 'country': country, 'company': rnd.choice(COMPANIES),
                    'email': f'{f}.{l}{i}@example.com'.lower(), 'phone': PHONES[country].format(100 + i)})
    return out


def submissions(seed: int = 42) -> list[dict]:
    rnd = random.Random(seed)
    base, subs = people(rnd), []
    for i, p in enumerate(base):
        subs.append({**p, 'submission_id': f'S{i:04d}'})
    for j, p in enumerate(rnd.sample(base, 30)):   # the same people sign up again, typed differently
        again = {**p, 'submission_id': f'R{j:04d}', 'email': '  ' + p['email'].upper() + ' ', 'first_name': p['first_name'].upper()}
        if j % 3 == 0:
            again['phone'] = PHONES[p['country']].format(800 + j)   # a new phone number: should update the CRM
        if j % 4 == 0:
            again['company'] = ''                                    # blank field: must NOT wipe the CRM value
        subs.append(again)
    subs += [dict(s) for s in rnd.sample(subs, 20)]                   # webhook re-deliveries (same submission_id)
    for k in range(6):                                               # broken emails
        subs.append({'submission_id': f'B{k:04d}', 'first_name': 'Test', 'last_name': 'User', 'country': 'US',
                     'email': ['no-at-sign.example.com', 'a@b', '', 'x@@example.com', 'name@domain', 'spaces in@example.com'][k]})
    for k in range(4):                                               # bad phone: lead kept, phone dropped
        subs.append({'submission_id': f'P{k:04d}', 'first_name': 'Iris', 'last_name': f'Lane{k}', 'country': 'US',
                     'email': f'iris.lane{k}@example.com', 'phone': ['12', 'call me', '555', '+1 000'][k]})
    rnd.shuffle(subs)
    return subs


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level='warning'))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            return server
        time.sleep(0.1)
    raise RuntimeError('server did not start')


def send_all(subs, forged: int = 0) -> int:
    with httpx.Client(base_url=f'http://127.0.0.1:{HOOK_PORT}') as c:
        for s in subs:
            body = json.dumps(s).encode()
            c.post('/webhooks/form', content=body, headers={'X-Signature': sign(SECRET, body), 'Content-Type': 'application/json'})
        for k in range(forged):                                     # forged requests: wrong secret
            body = json.dumps({'submission_id': f'X{k}', 'email': 'attacker@example.com'}).encode()
            c.post('/webhooks/form', content=body, headers={'X-Signature': sign('wrong-secret', body)})
    return len(subs)


def wait_until_processed(pipe: SyncPipeline, expected: int, timeout: float = 300) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pipe.db.execute('select count(*) from ledger').fetchone()[0] >= expected:
            return
        time.sleep(0.5)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    ledger = OUT / 'ledger.db'
    ledger.unlink(missing_ok=True)
    crm_api = crm_app(rate_per_sec=8, chaos=0.05)
    serve(crm_api, CRM_PORT)
    crm = MockCRMClient(httpx.Client(base_url=f'http://127.0.0.1:{CRM_PORT}', headers={'Authorization': f'Bearer {TOKEN}'}))
    pipe = SyncPipeline(crm, ledger)
    hook = hook_app(pipe, SECRET)
    serve(hook, HOOK_PORT)

    subs = submissions()
    unique_ids = len({s['submission_id'] for s in subs})
    t0 = time.time()
    send_all(subs, forged=3)
    wait_until_processed(pipe, unique_ids)
    first = pipe.counts()
    replayed = pipe.replay_dead_letters()
    send_all(subs)                       # the whole batch again
    time.sleep(3)
    second_creates = pipe.counts().get('created', 0) - first.get('created', 0)
    contacts = crm.list_all()
    valid_people = len({s['email'].strip().lower() for s in subs if s['submission_id'][0] in 'SRP'})

    summary = [
        ('Webhook requests sent (first pass)', len(subs) + 3),
        ('Rejected: bad signature', hook.state.rejected_signatures),
        ('Re-deliveries skipped (same submission_id)', len(subs) - unique_ids),
        ('Leads rejected (invalid email / no name)', first.get('rejected', 0)),
        ('Contacts created', first.get('created', 0)),
        ('Contacts updated (new phone / filled blanks)', first.get('updated', 0)),
        ('Repeat sign-ups with nothing new', first.get('unchanged', 0)),
        ('Failed after retries (dead letters)', first.get('failed', 0)),
        ('Dead letters fixed on replay', sum(1 for v in replayed.values() if v != 'failed')),
        ('CRM 429 rate-limit answers absorbed', crm.retries['429']),
        ('CRM 5xx errors absorbed', crm.retries['5xx']),
        ('Contacts in the CRM at the end', len(contacts)),
        ('Distinct valid people sent', valid_people),
        ('New contacts created by sending everything again', second_creates),
        ('Run time (s)', round(time.time() - t0, 1)),
    ]
    for k, v in summary:
        print(f'{k:50} {v}')
    log = pd.read_sql_query('select * from ledger order by at', pipe.db)
    with pd.ExcelWriter(OUT / 'crm_sync_report.xlsx') as xl:
        pd.DataFrame(summary, columns=['Check', 'Value']).to_excel(xl, sheet_name='Summary', index=False)
        pd.DataFrame(contacts).to_excel(xl, sheet_name='CRM contacts', index=False)
        log.to_excel(xl, sheet_name='Sync log', index=False)
    print(f"Saved {OUT / 'crm_sync_report.xlsx'}")


if __name__ == '__main__':
    main()
