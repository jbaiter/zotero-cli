import json
import logging
import os
import pkg_resources
import re
import shutil
import time
import ConfigParser

import click
import pypandoc
from pyzotero.zotero import Zotero

from zotero_cli.common import Item
from zotero_cli.index import SearchIndex


APP_NAME = "zotcli"

DATA_PAT = re.compile(
    r'<div class="zotcli-note">.*<p .*title="([A-Za-z0-9+/=\n ]+)">.*</div>',
    flags=re.DOTALL | re.MULTILINE)
CITEKEY_PAT = re.compile(r'^bibtex: (.*)$', flags=re.MULTILINE)
DATA_TMPL = """
    <div class="zotcli-note">
        <p xmlns="http://www.w3.org/1999/xhtml"
        id="zotcli-data" style="color: #cccccc;"
        xml:base="http://www.w3.org/1999/xhtml"
        title="{data}">
        (hidden zotcli data)
        </p>
    </div>
"""


def load_config(cfg_path):
    """ Load (and create default config if it doesn't exist) configuration
        from application directory.

    :param cfg_path:    Path to configuration file
    :type cfg_path:     str/unicode
    :returns:           Configuration
    :rtype:             (flat) dict
    """
    if not os.path.exists(cfg_path):
        if not os.path.exists(os.path.dirname(cfg_path)):
            os.makedirs(os.path.dirname(cfg_path))
        default_cfg = pkg_resources.resource_filename(
            __name__, 'data/config.ini.default')
        shutil.copyfile(default_cfg, cfg_path)
    parser = ConfigParser.RawConfigParser()
    parser.read([cfg_path])
    rv = {}
    for section in parser.sections():
        for key, value in parser.items(section):
            rv['%s.%s' % (section, key)] = value
    return rv


class ZoteroCli(object):
    def __init__(self, api_key=None, library_id=None, library_type=None):
        """ Service class for communicating with the Zotero API.

        This is mainly a thin wrapper around :py:class:`pyzotero.zotero.Zotero`
        that handles things like transparent HTML<->[edit-formt] conversion.

        :param api_key:     API key for the Zotero API, will be loaded from
                            the configuration if not specified
        :param library_id:  Zotero library ID the API key is valid for, will
                            be loaded from the configuration if not specified
        :param library_type: Type of the library, can be 'user' or 'group',
                             will also be loaded from the configuration if
                             not specified
        """
        self._logger = logging.getLogger()
        cfg_path = os.path.join(click.get_app_dir(APP_NAME), 'config.ini')
        idx_path = os.path.join(click.get_app_dir(APP_NAME), 'index.sqlite')
        self.config = load_config(cfg_path)
        self.note_format = self.config['zotcli.note_format']

        api_key = api_key or self.config.get('zotcli.api_key')
        library_id = library_id or self.config.get('zotcli.library_id')
        library_type = (library_type or
                        self.config.get('zotcli.library_type') or
                        'user')
        if not api_key or not library_id:
            raise ValueError(
                "Please set your API key and library ID in the configuration "
                "file ({}) or pass them as command-line options.\nIf you do "
                "not have these, please go to "
                "https://www.zotero.org/settings/keys to retrieve them."
                .format(cfg_path))
        self._zot = Zotero(library_id=library_id, api_key=api_key,
                           library_type=library_type)
        self._index = SearchIndex(idx_path)
        sync_interval = self.config.get('main.sync_interval', 300)
        since_last_sync = int(time.time()) - self._index.last_modified
        if since_last_sync >= sync_interval:
            self._logger.info("{} seconds since last sync, synchronizing."
                              .format(since_last_sync))
            self.synchronize()

    def synchronize(self):
        """ Update the local index to the latest library version. """
        new_items = tuple(self.items(since=self._index.library_version))
        version = int(self._zot.request.headers.get('last-modified-version'))
        self._index.index(new_items, version)
        return len(new_items)

    def search(self, query, limit=None):
        """ Search the local index for items.

        :param query:   A sqlite FTS4 query
        :param limit:   Maximum number of items to return
        :returns:       Generator that yields matching items.
        """
        return self._index.search(query, limit=limit)

    def items(self, query=None, limit=None, recursive=False, since=0):
        """ Get a list of all items in the library matching the arguments.

        :param query:   Filter items by this query string (targets author and
                        title fields)
        :type query:    str/unicode
        :param limit:   Limit maximum number of returned items
        :type limit:    int
        :param recursive: Include non-toplevel items (attachments, notes, etc)
                          in output
        :type recursive: bool
        :returns:       Generator that yields items
        """
        query_args = {'since': since}
        if query:
            query_args['q'] = query
        if limit:
            query_args['limit'] = limit
        query_fn = self._zot.items if recursive else self._zot.top
        items = self._zot.makeiter(query_fn(**query_args))
        for chunk in items:
            for it in chunk:
                matches = CITEKEY_PAT.finditer(it['data'].get('extra', ''))
                citekey = next((m.group(1) for m in matches), None)
                yield Item(key=it['data']['key'],
                           creator=it['meta'].get('creatorSummary'),
                           title=it['data'].get('title', "Untitled"),
                           date=it['data'].get('date'),
                           citekey=citekey)

    def notes(self, item_id):
        """ Get a list of all notes for a given item.

        :param item_id:     ID/key of the item to get notes for
        :returns:           Notes for item
        """
        notes = self._zot.children(item_id, itemType="note")
        for note in notes:
            note['data']['note'] = self._make_note_data(
                note_html=note['data']['note'],
                note_version=note['version'])
        return notes

    def _make_note_data(self, note_html, note_version):
        """ Converts a note from HTML to the configured markup.

        If the note was previously edited with zotcli, the original markup
        will be restored. If it was edited with the Zotero UI, it will be
        converted from the HTML via pandoc.

        :param note_html:       HTML of the note
        :param note_version:    Library version the note was last edited
        :returns:               Dictionary with markup, format and version
        """
        data = None
        blobs = DATA_PAT.findall(note_html)
        # Previously edited with zotcli
        if blobs:
            data = json.loads(blobs[0].decode('base64').decode('zlib'))
            if 'version' not in data:
                data['version'] = note_version
            note_html = DATA_PAT.sub("", note_html)
        # Not previously edited with zotcli or updated from the Zotero UI
        if not data or data['version'] < note_version:
            if data['version'] < note_version:
                self._logger.info("Note changed on server, reloading markup.")
            note_format = data['format'] if data else self.note_format
            data = {
                'format': note_format,
                'text': pypandoc.convert(
                    note_html, note_format, format='html'),
                'version': note_version}
        return data

    def _make_note_html(self, note_data):
        """ Converts the note's text to HTML and adds a dummy element that
            holds the original markup.

        :param note_data:   dict with text, format and version of the note
        :returns:           Note as HTML
        """
        extra_data = DATA_TMPL.format(
            data=json.dumps(note_data).encode('zlib').encode('base64'))
        html = pypandoc.convert(note_data['text'], 'html',
                                format=note_data['format'])
        return html + extra_data

    def create_note(self, item_id, note_text):
        """ Create a new note for a given item.

        :param item_id:     ID/key of the item to create the note for
        :param note_text:   Text of the note
        """
        note = self._zot.item_template('note')
        note_data = {'format': self.note_format,
                     'text': note_text,
                     'version': self._zot.last_modified_version(limit=1)+2}
        note['note'] = self._make_note_html(note_data)
        self._zot.create_items([note], item_id)

    def save_note(self, note):
        """ Update an existing note.

        :param note:        The updated note
        """
        note['data']['note']['version'] += 1
        note['data']['note'] = self._make_note_html(note['data']['note'])
        self._zot.update_item(note)
