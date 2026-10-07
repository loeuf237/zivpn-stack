"""Bounded Telegram transport and ordered, deduplicated background tasks."""
import os
import sqlite3
import threading
import time
from collections import deque, Counter
import requests


class Transport:
    def __init__(self, base, session_factory=requests.Session, sleep=time.sleep):
        self.base, self.factory, self.sleep = base, session_factory, sleep
        self.local = threading.local()
        self.stats_lock = threading.Lock()
        self.stats = Counter()

    def record(self, method, outcome):
        # Method and outcome are fixed internal labels, never payload/description.
        with self.stats_lock:
            self.stats[method + ':' + outcome] += 1

    def snapshot(self):
        with self.stats_lock:
            return dict(self.stats)

    @staticmethod
    def rejection_reason(result):
        text = str(result.get('description', '')).lower()
        for fragment, reason in (
            ('message is not modified', 'unchanged'),
            ("can't parse entities", 'invalid_format'),
            ('message to edit not found', 'message_missing'),
            ("message can't be edited", 'message_not_editable'),
            ('query is too old', 'callback_expired'),
            ('query id is invalid', 'callback_expired'),
            ('chat not found', 'chat_missing'),
            ('bot was blocked', 'bot_blocked'),
            ('method not found', 'unsupported_method'),
            ('too many requests', 'rate_limit'),
        ):
            if fragment in text:
                return reason
        return 'server_error' if isinstance(result.get('error_code'), int) and result['error_code'] >= 500 else 'api_rejection'


    def call(self, method, payload=None, files=None):
        if not hasattr(self.local, 'session'):
            self.local.session = self.factory()
        safe = method in ('getUpdates', 'editMessageText', 'answerCallbackQuery', 'sendMessageDraft')
        for attempt in range(3):
            try:
                if method == 'getUpdates':
                    response = self.local.session.get(self.base+'/'+method, params=payload, timeout=(5, 35))
                else:
                    kwargs = {'data': payload, 'files': files} if files else {'json': payload}
                    response = self.local.session.post(self.base+'/'+method, timeout=(5, 5 if method in ('sendMessageDraft', 'answerCallbackQuery') else 15), **kwargs)
                result = response.json()
                if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
                    raise ValueError('Invalid Telegram response')
            except Exception as error:
                self.record(method, 'transport_error')
                print(f'[Telegram Transport] method={method} error={type(error).__name__} attempt={attempt+1} retry={safe and attempt<2}', flush=True)
                if safe and attempt < 2:
                    self.sleep(attempt+1)
                    continue
                if not safe:
                    self.record(method, 'delivery_uncertain')
                return {'ok': False, 'delivery_uncertain': not safe, 'transport_error': True, 'failure_reason': 'transport_error'}
            if result.get('ok'):
                self.record(method, 'success')
                return result
            code = result.get('error_code', response.status_code)
            if method == 'editMessageText' and code == 400 and 'message is not modified' in result.get('description', '').lower():
                self.record(method, 'unchanged')
                return {'ok': True, 'unchanged': True}
            reason = self.rejection_reason(result)
            self.record(method, reason)
            result = dict(result, failure_reason=reason)
            if not safe and isinstance(code, int) and code >= 500:
                result['delivery_uncertain'] = True
                self.record(method, 'delivery_uncertain')
            delay = result.get('parameters', {}).get('retry_after')
            if code == 429 and isinstance(delay, (int, float)) and 0 <= delay <= 10 and attempt < 2 and not files:
                self.sleep(delay + 0.1)
                continue
            if safe and isinstance(code, int) and code >= 500 and attempt < 2:
                self.sleep(attempt+1)
                continue
            print(f'[Telegram Rejection] method={method} code={code} reason={reason} uncertain={result.get("delivery_uncertain", False)}', flush=True)
            return result


class Tasks:
    """Persist IDs/status only: never passwords, command arguments or Telegram tokens.

    Interrupted tasks are never replayed automatically: a mutation may have completed.
    FIFO per chat, three independent chats at once, bounded backlog.
    """
    def __init__(self, path, handler, failure, workers=3, capacity=64):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, chat INTEGER, state TEXT, created REAL)')
        self.db.execute("UPDATE tasks SET state='interrupted' WHERE state IN ('queued','running')")
        self.db.execute('DELETE FROM tasks WHERE created < ?', (time.time()-7*86400,))
        self.db.commit()
        self.handler, self.failure, self.capacity = handler, failure, capacity
        self.condition, self.pending, self.busy = threading.Condition(), deque(), set()
        for _ in range(workers):
            threading.Thread(target=self._run, daemon=True).start()

    def submit(self, update):
        uid = update['update_id']
        body = update.get('message') or update.get('callback_query', {}).get('message', {})
        chat = body.get('chat', {}).get('id', 0)
        with self.condition:
            if self.db.execute('SELECT 1 FROM tasks WHERE id=?', (uid,)).fetchone():
                return True
            if len(self.pending)+len(self.busy) >= self.capacity:
                return False
            self.db.execute('DELETE FROM tasks WHERE created < ? AND state NOT IN (?,?)',
                            (time.time()-7*86400, 'queued', 'running'))
            self.db.execute('INSERT INTO tasks VALUES (?,?,?,?)', (uid, chat, 'queued', time.time()))
            self.db.commit()
            self.pending.append((uid, chat, update))
            self.condition.notify_all()
            return True

    def status(self, chat):
        with self.condition:
            rows = self.db.execute('SELECT id,state FROM tasks WHERE chat=? ORDER BY id DESC LIMIT 8', (chat,)).fetchall()
        labels = dict(queued='en attente', running='en cours', done='terminée', failed='en erreur', interrupted='interrompue ; vérifier le résultat avant de relancer')
        return '\n'.join(f'{uid} : {labels[state]}' for uid, state in rows) or 'Aucune tâche récente.'

    def _run(self):
        while True:
            with self.condition:
                while True:
                    item = next((item for item in self.pending if item[1] not in self.busy), None)
                    if item:
                        self.pending.remove(item)
                        uid, chat, update = item
                        self.busy.add(chat)
                        self.db.execute("UPDATE tasks SET state='running' WHERE id=?", (uid,))
                        self.db.commit()
                        break
                    self.condition.wait()
            state = 'done'
            try:
                self.handler(update)
            except Exception as error:
                state = 'failed'
                print(f'[Telegram Task] id={uid} error={type(error).__name__}', flush=True)
                try:
                    self.failure(chat, uid)
                except Exception as error:
                    print(f'[Telegram Task Notice] error={type(error).__name__}', flush=True)
            finally:
                with self.condition:
                    self.db.execute('UPDATE tasks SET state=? WHERE id=?', (state, uid))
                    self.db.commit()
                    self.busy.remove(chat)
                    self.condition.notify_all()
