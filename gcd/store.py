import logging
import threading
import random
import re
import time
import json
import psycopg2

from itertools import chain
from datetime import datetime
from unittest import TestCase
from psycopg2.pool import ThreadedConnectionPool

from gcd.etc import snippet, chunks, as_many
from gcd.nix import sh

logger = logging.getLogger(__name__)


def execute(sql, args=(), cursor=None, values=False):
    return _execute('execute', sql, args, cursor, values)


def executemany(sql, args, cursor=None):
    return _execute('executemany', sql, args, cursor)


class PgConnectionPool:

    def __init__(self, *args, min_conns=1, keep_conns=10, max_conns=10,
                 **kwargs):
        self._pool = ThreadedConnectionPool(
            min_conns, max_conns, *args, **kwargs)
        self._keep_conns = keep_conns

    def acquire(self):
        pool = self._pool
        conn = pool.getconn()
        pool.minconn = min(self._keep_conns, len(pool._used))
        return conn

    def release(self, conn):
        self._pool.putconn(conn)

    def close(self):
        self._pool.closeall()


class Transaction:

    pool = None

    _local = threading.local()

    def active():
        return getattr(Transaction._local, 'active', None)

    def __init__(self, conn_or_pool=None):
        conn_or_pool = conn_or_pool or Transaction.pool
        self._pool = self._conn = None
        if hasattr(conn_or_pool, 'cursor'):
            self._conn = conn_or_pool
        else:
            self._pool = conn_or_pool

    def __enter__(self):
        active = Transaction.active()
        if active:
            return active
        else:
            Transaction._local.active = self
            if self._pool:
                self._conn = self._pool.acquire()
            self._cursors = []
            return self

    def cursor(self, *args, **kwargs):
        cursor = self._conn.cursor(*args, **kwargs)
        self._cursors.append(cursor)
        return cursor

    def __exit__(self, type_, value, traceback):
        active = Transaction.active()
        if active != self:
            return
        try:
            for cursor in self._cursors:
                try:
                    if not getattr(cursor, 'withhold', False):
                        cursor.close()
                except Exception:
                    logger.exception('Error closing cursor')
            if type_ is None:
                self._conn.commit()
            else:
                logger.error('Transaction rollback',
                             exc_info=(type_, value, traceback))
                self._conn.rollback()
        finally:
            Transaction._local.active = None
            if self._pool:
                self._pool.release(self._conn)
                self._conn = None


class Store:

    def __init__(self, conn_or_pool=None):
        self._conn_or_pool = conn_or_pool

    def transaction(self):
        return Transaction(self._conn_or_pool)


class PgRecordStore(Store):

    def __init__(self, obj_class, conn=None, record='record'):
        super().__init__(conn)
        self._obj_class = obj_class
        self._record = record

    def add(self, batch):  # (time, obj)...
        for chunk in chunks(batch, 1000):
            with self.transaction():
                chunk = ((datetime.fromtimestamp(t), json.dumps(o.flatten()))
                         for t, o in chunk)
                execute('INSERT INTO %s (time, data) %%s' % self._record,
                        chunk, values=True)

    def get(self, from_time=None, to_time=None, where='true'):
        where = as_many(where, list)
        cond, args = where[0], where[1:]
        if from_time:
            cond += ' AND time >= %s'
            args.append(datetime.fromtimestamp(from_time))
        if to_time:
            cond += ' AND time < %s'
            args.append(datetime.fromtimestamp(to_time))
        unflatten = self._obj_class.unflatten
        with self.transaction() as tx:
            # Here I prefer a fast start plan over an overall faster one.
            # (Maybe cursor_tuple_fraction would be a better way?)
            execute('SET LOCAL enable_seqscan = false')
            cursor = tx.cursor('record_cursor')
            cursor.itersize = 1000
            for t, o in execute(
                    'SELECT time, data from %s WHERE %s ORDER BY time' %
                    (self._record, cond), tuple(args), cursor):
                yield t.timestamp(), unflatten(o)

    def create(self):
        with self.transaction():
            execute("""
                    DROP TABLE IF EXISTS %(record)s;
                    CREATE TABLE %(record)s (
                      time timestamp,
                      data jsonb
                    );
                    CREATE INDEX %(record)s_time_index ON %(record)s(time);
                    """ % {'record': self._record})
        return self


class PgTestCase(TestCase):

    db = 'test'

    def setUp(self):
        sh('dropdb --if-exists %s &> /dev/null' % self.db)
        sh('createdb %s' % self.db)
        self.pool = PgConnectionPool(dbname=self.db)

    def tearDown(self):
        self.pool.close()
        sh('dropdb %s' % self.db)


def _execute(attr, sql, args, cursor, values=False):
    if cursor is None:
        cursor = Transaction.active().cursor()
    fun = getattr(cursor, attr)
    if values:
        sql, args = _values(sql, args)
    if logger.isEnabledFor(logging.DEBUG):
        _debugged(fun, sql, args)
    else:
        fun(sql, args)
    return cursor


def _values(sql, args):  # args can be any iterable.
    args_iter = iter(args)
    arg = next(args_iter)
    args_iter = chain((arg,), args_iter)
    args = tuple(v for a in args_iter for v in a)
    value_sql = '(' + ','.join(['%s'] * len(arg)) + ')'
    values_sql = 'VALUES ' + ','.join([value_sql] * (len(args) // len(arg)))
    sql %= values_sql
    return sql, args


def _debugged(fun, sql, args):
    query_id = random.randint(0, 10000)
    log_sql = snippet(re.sub(r'[\n\t ]+', ' ', sql[:500]).strip(), 100)
    log_args = snippet(str(args[:20]), 100)
    logger.debug(dict(query=query_id, sql=log_sql, args=log_args))
    try:
        start_time = time.time()
        fun(sql, args)
        logger.debug(dict(query=query_id, time=time.time() - start_time))
    except:
        logger.exception(dict(query=query_id))
