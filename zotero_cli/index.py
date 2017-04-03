import os
import sqlite3
import time
from contextlib import contextmanager

from zotero_cli.common import Item

SCHEMA = """
    CREATE TABLE IF NOT EXISTS syncinfo (
        id          INTEGER PRIMARY KEY,
        last_sync   INTEGER,
        version     INTEGER
    );
    CREATE TABLE IF NOT EXISTS items (
        id          INTEGER PRIMARY KEY,
        key         TEXT UNIQUE,
        creator     TEXT,
        title       TEXT,
        abstract    TEXT,
        date        TEXT,
        citekey     TEXT UNIQUE
    );

    CREATE VIRTUAL TABLE items_idx USING fts4(
        key, creator, title, abstract, date, citekey,
        content="items");
    CREATE VIRTUAL TABLE items_idx_terms USING fts4aux(items_idx);

    CREATE TRIGGER items_bu BEFORE UPDATE ON items BEGIN
        DELETE FROM items_idx WHERE docid=old.rowid;
    END;
    CREATE TRIGGER items_bd BEFORE DELETE ON items BEGIN
        DELETE FROM items_idx WHERE docid=old.rowid;
    END;

    CREATE TRIGGER items_au AFTER UPDATE ON items BEGIN
        INSERT INTO items_idx(docid, key, creator, title, abstract, date, citekey)
            VALUES(new.rowid, new.key, new.creator, new.title, new.abstract, new.date,
                   new.citekey);
    END;
    CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
        INSERT INTO items_idx(docid, key, creator, title, abstract, date, citekey)
            VALUES(new.rowid, new.key, new.creator, new.title, new.abstract, new.date,
                   new.citekey);
    END;
"""
SEARCH_QUERY = """
    SELECT items.key, creator, title, abstract, date, citekey FROM items JOIN (
        SELECT key FROM items_idx
        WHERE items_idx MATCH :query) AS idx ON idx.key = items.key
        LIMIT :limit;
"""
INSERT_ITEMS_QUERY = """
    INSERT OR REPLACE INTO items (key, creator, title, abstract, date, citekey)
        VALUES (:key, :creator, :title, :abstract, :date, :citekey);
"""
INSERT_META_QUERY = """
    INSERT OR REPLACE INTO syncinfo (id, last_sync, version)
        VALUES (0, :last_sync, :version);
"""


class SearchIndex(object):
    def __init__(self, index_path):
        """ Local full-text search index using SQLite.

        :param index_path:  Path to the index file
        """
        init_db = not os.path.exists(index_path)
        self.db_path = index_path
        if init_db:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.executescript(SCHEMA)

    @property
    @contextmanager
    def _db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            yield cursor

    @property
    def last_modified(self):
        """ Get time of last index modification.

        :returns:   Epoch timestamp of last index modification
        :rtype:     int
        """
        with self._db as cursor:
            res = cursor.execute(
                "SELECT last_sync FROM syncinfo LIMIT 1").fetchone()
            if res:
                return res[0]
            else:
                return 0

    @property
    def library_version(self):
        """ Get indexed library version.

        :returns:   Version number of indexed library
        :rtype:     int
        """
        with self._db as cursor:
            res = cursor.execute(
                "SELECT version FROM syncinfo LIMIT 1").fetchone()
            if res:
                return res[0]
            else:
                return 0

    def index(self, items, version):
        """ Update the index with new items and set a new version.

        :param items:   Items to update the index with
        :type items:    iterable of :py:class:`zotero_cli.common.Item`
        """
        with self._db as cursor:
            cursor.executemany(INSERT_ITEMS_QUERY, items)
            cursor.execute(INSERT_META_QUERY, (int(time.time()), version))

    def search(self, query, limit=None):
        """ Search the index for items matching the query.

        :param query:   A sqlite FTS4 query
        :param limit:   Maximum number of items to return
        :returns:       Generator that yields matching items.
        """
        with self._db as cursor:
            query = "'{}'".format(query)
            for itm in cursor.execute(SEARCH_QUERY, (query, limit or -1)):
                yield Item(*itm)
