"""One lead in, one CRM action out, recorded in a SQLite ledger.

- Same submission_id again (webhook re-delivery) -> skipped, nothing sent to the CRM.
- Invalid lead -> rejected with the reasons, kept in the ledger.
- Email already in the CRM -> update, but only fields that are empty in the CRM or genuinely new;
  a blank form field never wipes good CRM data.
- CRM still failing after retries -> dead letter, replayable later with replay_dead_letters().
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .crm_client import CRMError
from .normalize import normalize

SCHEMA = """
create table if not exists ledger (submission_id text primary key, email text, action text, crm_id text,
                                   detail text, at text);
create table if not exists dead_letters (submission_id text primary key, payload text, error text, at text);
"""


class SyncPipeline:
    def __init__(self, crm, db_path: Path):
        self.crm = crm
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()  # one lead at a time: no two writers race on the same email

    def _log(self, sid, email, action, crm_id=None, detail=''):
        self.db.execute('insert or replace into ledger values (?,?,?,?,?,?)',
                        (sid, email, action, crm_id, detail, datetime.now().isoformat(timespec='seconds')))
        self.db.commit()
        return action

    def process(self, submission: dict) -> str:
        with self.lock:
            return self._process(submission)

    def _process(self, submission: dict) -> str:
        sid = submission['submission_id']
        if self.db.execute("select 1 from ledger where submission_id=? and action!='failed'", (sid,)).fetchone():
            return 'skipped (already processed)'
        lead, problems = normalize(submission)
        if any(not p.startswith('invalid phone') for p in problems):
            return self._log(sid, lead['email'], 'rejected', detail='; '.join(problems))
        try:
            existing = self.crm.find_by_email(lead['email'])
            if existing is None:
                c = self.crm.create(lead)
                return self._log(sid, lead['email'], 'created', c['id'], '; '.join(problems))
            changes = {k: v for k, v in lead.items() if v and v != existing.get(k) and k != 'email'
                       and (not existing.get(k) or k in ('phone', 'company'))}
            if not changes:
                return self._log(sid, lead['email'], 'unchanged', existing['id'])
            self.crm.update(existing['id'], changes)
            return self._log(sid, lead['email'], 'updated', existing['id'], 'changed: ' + ', '.join(sorted(changes)))
        except (CRMError, Exception) as e:  # anything the retries could not fix
            self.db.execute('insert or replace into dead_letters values (?,?,?,?)',
                            (sid, json.dumps(submission), str(e)[:300], datetime.now().isoformat(timespec='seconds')))
            return self._log(sid, lead['email'], 'failed', detail=str(e)[:300])

    def replay_dead_letters(self) -> dict:
        done = {}
        for sid, payload in self.db.execute('select submission_id, payload from dead_letters').fetchall():
            result = self.process(json.loads(payload))
            if result != 'failed':
                self.db.execute('delete from dead_letters where submission_id=?', (sid,))
            done[sid] = result
        self.db.commit()
        return done

    def counts(self) -> dict:
        return dict(self.db.execute('select action, count(*) from ledger group by action').fetchall())
