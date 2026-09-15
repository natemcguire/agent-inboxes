"""Small DB-API adapter for Durable Object SQL.

Each HTTP operation runs inside storage.transactionSync. Legacy BEGIN/COMMIT
boundaries track service nesting; any rollback aborts the entire operation.
Never use this adapter outside a transactionSync callback.
"""
import sqlite3
import re


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Cursor:
    def __init__(self, rows=(), rowcount=-1):
        self.rows = iter(rows)
        self.rowcount = rowcount

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return self.rows


class Database:
    def __init__(self, sql):
        self.sql = sql
        self.in_transaction = False
        self.rolled_back = False

    def execute(self, statement, parameters=()):
        operation = statement.strip().rstrip(';').upper()
        if operation in ('BEGIN', 'BEGIN IMMEDIATE', 'BEGIN DEFERRED'):
            if self.in_transaction:
                raise sqlite3.OperationalError('cannot start a transaction within a transaction')
            self.in_transaction = True
            return Cursor()
        if operation in ('COMMIT', 'END'):
            self.commit()
            return Cursor()
        if operation == 'ROLLBACK':
            self.rollback()
            return Cursor()
        if operation.startswith(('PRAGMA FOREIGN_KEYS', 'PRAGMA JOURNAL_MODE', 'PRAGMA BUSY_TIMEOUT')):
            return Cursor()
        if isinstance(parameters, dict):
            values = []
            def bind(match):
                values.append(parameters[match.group(1)])
                return '?'
            statement = re.sub(r':([a-zA-Z_]\w*)', bind, statement)
            parameters = values
        try:
            result = self.sql.exec(statement, *parameters)
            rows = [Row(row) for row in result.toArray()]
            count = -1
            if operation.startswith(('INSERT ', 'UPDATE ', 'DELETE ')):
                count = self.sql.exec('SELECT changes() AS n').toArray()[0]['n']
            return Cursor(rows, count)
        except Exception as exc:
            kind = sqlite3.IntegrityError if 'constraint' in str(exc).lower() else sqlite3.OperationalError
            raise kind(str(exc)) from exc

    def executescript(self, script):
        # complete_statement understands quoted semicolons and trigger bodies.
        pending = ''
        for char in script:
            pending += char
            if char == ';' and sqlite3.complete_statement(pending):
                self.execute(pending)
                pending = ''
        if pending.strip():
            self.execute(pending)

    def executemany(self, statement, parameters):
        count = 0
        for row in parameters:
            count += self.execute(statement, row).rowcount
        return Cursor(rowcount=count)

    def commit(self):
        self.in_transaction = False

    def rollback(self):
        self.in_transaction = False
        self.rolled_back = True
