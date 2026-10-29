"""Private social persistence. SQL identifiers are fixed; credentials never leave env.

Postgres connects lazily so unavailable social storage cannot prevent room startup.
One lobby worker remains required: invitations and UDP admission leases are transient.
"""
from contextlib import contextmanager
import sqlite3
import hashlib
import threading


class StorageUnavailable(Exception):
    """Safe exception: never include driver messages/DSNs in client responses."""


class SQLiteStorage:
    persistent_parties = False

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS players(id TEXT PRIMARY KEY,name TEXT NOT NULL,secret TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS friends(a TEXT NOT NULL,b TEXT NOT NULL,PRIMARY KEY(a,b));
            CREATE TABLE IF NOT EXISTS requests(a TEXT NOT NULL,b TEXT NOT NULL,PRIMARY KEY(a,b));
        ''')
        self.db.commit()

    def close(self): self.db.close()
    def transaction(self): return self.db
    def actor(self, digest):
        row = self.db.execute('SELECT id FROM players WHERE secret=?', (digest,)).fetchone()
        return row['id'] if row else None
    def profile(self, player):
        row = self.db.execute('SELECT id,name FROM players WHERE id=?', (player,)).fetchone()
        return dict(row) if row else None
    def profiles(self, players):
        ids=list(set(players))
        if not ids:return {}
        query='SELECT id,name FROM players WHERE id IN ('+','.join('?' for _ in ids)+')'
        return {row['id']:dict(row) for row in self.db.execute(query,ids)}
    def relations(self, player):return self.friends(player),self.requests(player)
    def register(self, player, name, digest):
        self.db.execute('INSERT INTO players VALUES(?,?,?)', (player, name, digest))
    def friends(self, player):
        return [r['b'] if r['a'] == player else r['a'] for r in self.db.execute('SELECT a,b FROM friends WHERE a=? OR b=?', (player, player))]
    def requests(self, player):
        return [dict(r) for r in self.db.execute('SELECT a,b FROM requests WHERE a=? OR b=?', (player, player))]
    def search(self, query):
        return [r['id'] for r in self.db.execute('SELECT id FROM players WHERE id=? OR instr(lower(name),lower(?))>0 LIMIT 20', (query, query))]
    def has_friend(self, pair):
        return bool(self.db.execute('SELECT 1 FROM friends WHERE a=? AND b=?', pair).fetchone())
    def pending(self, a, b):
        return bool(self.db.execute('SELECT 1 FROM requests WHERE a=? AND b=?', (a, b)).fetchone())
    def request_count(self, player):
        return self.db.execute('SELECT count(*) FROM requests WHERE a=?', (player,)).fetchone()[0]
    def request(self, a, b): self.db.execute('INSERT INTO requests VALUES(?,?)', (a, b))
    def resolve(self, a, b, accept):
        self.db.execute('DELETE FROM requests WHERE a=? AND b=?', (a, b))
        if accept: self.db.execute('INSERT OR IGNORE INTO friends VALUES(?,?)', tuple(sorted((a, b))))
    def remove(self, pair): self.db.execute('DELETE FROM friends WHERE a=? AND b=?', pair)


class PostgresStorage:
    persistent_parties = True

    def __init__(self, *, allow_local_test=False):
        # Read the secret only when connecting; do not retain it in object reprs.
        self.db = None
        self.allow_local_test = allow_local_test
        self._connection = None
        self._dsn_key = None
        self._transaction_lock = threading.RLock()

    def _discard_connection(self):
        if self._connection is not None:
            self._connection.close()
        self._connection = None
        self._dsn_key = None

    def close(self):
        with self._transaction_lock:self._discard_connection()

    @contextmanager
    def transaction(self):
        import os
        # The directory serializes social writes. Keep a single bounded connection,
        # and guard direct storage callers too. Never replay a failed transaction.
        with self._transaction_lock:
            try:
                import psycopg
                from psycopg.rows import dict_row
                from psycopg.conninfo import conninfo_to_dict
                dsn = os.environ.get('EDEN_SOCIAL_DATABASE_URL', '')
                options = conninfo_to_dict(dsn)
                local_test = self.allow_local_test and options.get('host') == '127.0.0.1'
                if not options.get('host') or (not local_test and options.get('sslmode') not in ('require', 'verify-ca', 'verify-full')):
                    raise StorageUnavailable()
                key=hashlib.sha256(dsn.encode()).digest()
                if self._connection is not None and (self._dsn_key!=key or self._connection.closed or self._connection.broken):
                    self._discard_connection()
                if self._connection is None:
                    self._connection=psycopg.connect(dsn, connect_timeout=3, autocommit=True,
                                                     prepare_threshold=None, row_factory=dict_row)
                    self._dsn_key=key
                conn=self._connection
                self.db=conn
                with conn.transaction():
                    conn.execute("SELECT set_config('statement_timeout','3000',true), "
                                 "set_config('lock_timeout','1000',true), "
                                 "set_config('idle_in_transaction_session_timeout','10000',true)")
                    conn.execute('SELECT pg_advisory_xact_lock(1162102094)')
                    yield self
            except StorageUnavailable:
                self._discard_connection()
                raise
            except (ImportError, OSError):
                self._discard_connection()
                raise StorageUnavailable() from None
            except Exception as exc:
                if 'psycopg' in locals() and isinstance(exc, psycopg.Error):
                    self._discard_connection()
                    raise StorageUnavailable() from None
                raise
            finally:
                self.db=None

    def actor(self, digest):
        row = self.db.execute('SELECT player_id AS id FROM eden_social.profiles WHERE credential_hash=%s', (digest,)).fetchone()
        return row['id'] if row else None
    def profile(self, player):
        return self.db.execute('SELECT player_id AS id,display_name AS name FROM eden_social.profiles WHERE player_id=%s', (player,)).fetchone()
    def profiles(self, players):
        ids=list(set(players))
        if not ids:return {}
        rows=self.db.execute('SELECT player_id AS id,display_name AS name FROM eden_social.profiles WHERE player_id=ANY(%s::text[])',(ids,))
        return {row['id']:dict(row) for row in rows}
    def relations(self, player):
        rows=self.db.execute('SELECT player_a,player_b,status,requested_by FROM eden_social.friendships WHERE player_a=%s OR player_b=%s',(player,player))
        friends=[];requests=[]
        for row in rows:
            if row['status']=='accepted':friends.append(row['player_b'] if row['player_a']==player else row['player_a'])
            elif row['status']=='pending':requests.append(dict(a=row['requested_by'],b=row['player_b'] if row['requested_by']==row['player_a'] else row['player_a']))
        return friends,requests
    def register(self, player, name, digest):
        self.db.execute('INSERT INTO eden_social.profiles(player_id,display_name,credential_hash) VALUES(%s,%s,%s)', (player, name, digest))
    def friends(self, player):
        rows = self.db.execute("SELECT player_a,player_b FROM eden_social.friendships WHERE status='accepted' AND (player_a=%s OR player_b=%s)", (player, player))
        return [r['player_b'] if r['player_a'] == player else r['player_a'] for r in rows]
    def requests(self, player):
        rows = self.db.execute("SELECT player_a,player_b,requested_by FROM eden_social.friendships WHERE status='pending' AND (player_a=%s OR player_b=%s)", (player, player))
        return [dict(a=r['requested_by'], b=r['player_b'] if r['requested_by'] == r['player_a'] else r['player_a']) for r in rows]
    def search(self, query):
        return [r['id'] for r in self.db.execute('SELECT player_id AS id FROM eden_social.profiles WHERE player_id=%s OR strpos(lower(display_name),lower(%s))>0 ORDER BY player_id LIMIT 20', (query, query))]
    def has_friend(self, pair):
        return bool(self.db.execute("SELECT 1 FROM eden_social.friendships WHERE player_a=%s AND player_b=%s AND status='accepted'", pair).fetchone())
    def pending(self, a, b):
        return bool(self.db.execute("SELECT 1 FROM eden_social.friendships WHERE player_a=%s AND player_b=%s AND status='pending' AND requested_by=%s", (*sorted((a, b)), a)).fetchone())
    def request_count(self, player):
        return self.db.execute("SELECT count(*) AS n FROM eden_social.friendships WHERE requested_by=%s AND status='pending'", (player,)).fetchone()['n']
    def request(self, a, b):
        self.db.execute("INSERT INTO eden_social.friendships(player_a,player_b,status,requested_by) VALUES(%s,%s,'pending',%s)", (*sorted((a, b)), a))
    def resolve(self, a, b, accept):
        pair = tuple(sorted((a, b)))
        if accept:
            self.db.execute("UPDATE eden_social.friendships SET status='accepted',updated_at=now() WHERE player_a=%s AND player_b=%s AND status='pending' AND requested_by=%s", (*pair, a))
        else:
            self.db.execute("DELETE FROM eden_social.friendships WHERE player_a=%s AND player_b=%s AND status='pending' AND requested_by=%s", (*pair, a))
    def remove(self, pair):
        self.db.execute("DELETE FROM eden_social.friendships WHERE player_a=%s AND player_b=%s AND status='accepted'", pair)

    def load_parties(self):
        parties = {}
        rows=self.db.execute('SELECT p.party_id,p.leader_id,m.player_id FROM eden_social.parties p LEFT JOIN eden_social.party_members m ON m.party_id=p.party_id ORDER BY p.created_at,p.party_id,m.joined_at,m.player_id')
        for row in rows:
            key=row['party_id'].hex
            party=parties.setdefault(key,dict(id=key,leader=row['leader_id'],members={}))
            if row['player_id'] is not None:party['members'][row['player_id']]=False
        for party in parties.values():
            if not 1 <= len(party['members']) <= 4 or party['leader'] not in party['members']:
                raise StorageUnavailable()
        return parties

    def save_parties(self, before, after):
        from uuid import UUID
        for key in before.keys() - after.keys():
            self.db.execute('DELETE FROM eden_social.parties WHERE party_id=%s', (UUID(hex=key),))
        for key, party in after.items():
            old = before.get(key)
            if old and old['leader'] == party['leader'] and set(old['members']) == set(party['members']): continue
            if not 1 <= len(party['members']) <= 4 or party['leader'] not in party['members']: raise StorageUnavailable()
            uid = UUID(hex=key)
            self.db.execute('INSERT INTO eden_social.parties(party_id,leader_id) VALUES(%s,%s) ON CONFLICT(party_id) DO UPDATE SET leader_id=EXCLUDED.leader_id', (uid, party['leader']))
            self.db.execute('DELETE FROM eden_social.party_members WHERE party_id=%s', (uid,))
            for player in party['members']:
                self.db.execute('INSERT INTO eden_social.party_members(party_id,player_id) VALUES(%s,%s)', (uid, player))
