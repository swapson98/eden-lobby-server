"""Private social persistence. SQL identifiers are fixed; credentials never leave env.

Postgres connects lazily so unavailable social storage cannot prevent room startup.
One lobby worker remains required: invitations and UDP admission leases are transient.
"""
from contextlib import contextmanager
import sqlite3


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

    def close(self):
        if self.db is not None: self.db.close()

    @contextmanager
    def transaction(self):
        import os
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.conninfo import conninfo_to_dict
            dsn = os.environ.get('EDEN_SOCIAL_DATABASE_URL', '')
            options = conninfo_to_dict(dsn)
            local_test = self.allow_local_test and options.get('host') == '127.0.0.1'
            if not options.get('host') or (not local_test and options.get('sslmode') not in ('require', 'verify-ca', 'verify-full')):
                raise StorageUnavailable()
            with psycopg.connect(dsn, connect_timeout=3, autocommit=True,
                                 prepare_threshold=None, row_factory=dict_row) as conn:
                self.db = conn
                with conn.transaction():
                    # Transaction-local settings also work through Supavisor;
                    # avoid provider-specific startup options/session leakage.
                    conn.execute("SELECT set_config('statement_timeout','3000',true), "
                                 "set_config('lock_timeout','1000',true), "
                                 "set_config('idle_in_transaction_session_timeout','10000',true)")
                    # Serialize party/friend transitions across DB connections.
                    # Short lock timeout avoids queuing indefinitely. No write retry.
                    conn.execute('SELECT pg_advisory_xact_lock(1162102094)')
                    yield self
        except StorageUnavailable:
            raise
        except (ImportError, OSError):
            raise StorageUnavailable() from None
        except Exception as exc:
            if 'psycopg' in locals() and isinstance(exc, psycopg.Error):
                raise StorageUnavailable() from None
            raise
        finally:
            self.db = None

    def actor(self, digest):
        row = self.db.execute('SELECT player_id AS id FROM eden_social.profiles WHERE credential_hash=%s', (digest,)).fetchone()
        return row['id'] if row else None
    def profile(self, player):
        return self.db.execute('SELECT player_id AS id,display_name AS name FROM eden_social.profiles WHERE player_id=%s', (player,)).fetchone()
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
        for row in self.db.execute('SELECT party_id,leader_id FROM eden_social.parties ORDER BY created_at,party_id'):
            key = row['party_id'].hex
            parties[key] = dict(id=key, leader=row['leader_id'], members={})
        for row in self.db.execute('SELECT party_id,player_id FROM eden_social.party_members ORDER BY joined_at,player_id'):
            parties[row['party_id'].hex]['members'][row['player_id']] = False
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
