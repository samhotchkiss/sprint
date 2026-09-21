"""Opt-in server-side quality gate; user writes never pass through a model."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from .jev import JevClient, load_api_key
from .message_quality import assess_message


class PublicationRejected(Exception):
    def __init__(self, decision):
        self.decision = decision
        super().__init__(decision.get('reason', 'message_needs_revision'))


def check_publication(data_dir, *, actor, surface, text, detail=None, context=None,
                      client=None):
    if actor not in ('session', 'worker'):
        return
    root = Path(data_dir)
    config_path = root / 'message-quality.json'
    if not config_path.exists():
        return
    config = json.loads(config_path.read_text())
    if config.get('enabled') is not True:
        return
    state = {'surface': surface, 'visible': text, 'detail': detail,
             'outcome': 'Assess only the proposed visible message for useful, concise communication. It may report newly completed work rather than answer the last chat. Context can contain unrelated topics: do not require this update to resolve or repeat those topics. Do not score historical context text as part of the draft.',
             'context': json.dumps(context or {}, ensure_ascii=False)}
    fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    db = sqlite3.connect(root / 'message-quality.sqlite', timeout=5)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS checks (fingerprint TEXT PRIMARY KEY, at REAL, decision TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, calls INTEGER)')
        cached = db.execute('SELECT decision FROM checks WHERE fingerprint=? AND at>?',
                            (fingerprint, time.time()-300)).fetchone()
        if cached:
            decision = json.loads(cached[0])
        else:
            day = time.strftime('%Y-%m-%d', time.gmtime())
            db.execute('BEGIN IMMEDIATE')
            calls = db.execute('SELECT calls FROM usage WHERE day=?', (day,)).fetchone()
            if (calls[0] if calls else 0) >= int(config.get('daily_max_calls', 200)):
                db.rollback()
                raise PublicationRejected({'action':'unavailable','reason':'message_quality_daily_cap',
                                           'verified':False})
            db.execute('INSERT INTO usage VALUES (?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1',(day,))
            db.commit()
            if client is None:
                try:
                    key = load_api_key(secret_file=Path(config.get('key_file','~/.config/sprint/typesafe.env')))
                    client = JevClient(key, timeout=5, max_attempts=1)
                except Exception:
                    raise PublicationRejected({'action':'unavailable','reason':'message_quality_key_unavailable',
                                               'verified':False}) from None
            decision = assess_message(client=client, **state).as_dict()
            # Keep only judgments, never message text or credentials.
            db.execute('INSERT OR REPLACE INTO checks VALUES (?,?,?)',
                       (fingerprint,time.time(),json.dumps(decision)))
            db.execute('DELETE FROM checks WHERE at<?',(time.time()-86400,))
            db.commit()
        if decision.get('action') == 'revise':
            # Uncertain style judgments must not silence useful project updates.
            # Enforce only a confident failure; record uncertainty as unverified.
            scores = decision.get('dimensions') or {}
            if scores and min(scores.values()) > 0.2:
                decision = dict(decision, action='unavailable', reason='message_quality_uncertain')
        if decision.get('action') != 'accept' or decision.get('verified') is not True:
            raise PublicationRejected(decision)
    finally:
        db.close()
