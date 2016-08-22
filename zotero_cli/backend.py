import codecs
import json
import logging
import os
import re
import tempfile
import time
import urllib
try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse
try:
    urlencode = urllib.urlencode
except AttributeError:
    urlencode = urlparse.urlencode
import zipfile
try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO


import click
import pypandoc
import requests
from pathlib import Path
from pyzotero.zotero import Zotero
from rauth import OAuth1Service

from zotero_cli.common import APP_NAME, Item, load_config
from zotero_cli.index import SearchIndex


TEMP_DIR = Path(tempfile.mkdtemp(prefix='zotcli'))
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
CLIENT_KEY = 'c7d12bbd2c829823ddbc'
CLIENT_SECRET = 'c1ffe13aaeaa59ebf293'
REQUEST_TOKEN_URL = 'https://www.zotero.org/oauth/request'
AUTH_URL = 'https://www.zotero.org/oauth/authorize'
ACCESS_TOKEN_URL = 'https://www.zotero.org/oauth/access'
BASE_URL = 'https://api.zotero.org'


def encode_blob(data):
    """ Encode a dictionary to a base64-encoded compressed binary blob.

    :param data:    data to encode into a blob
    :type data:     dict
    :returns:       The data as a compressed base64-encoded binary blob
    """
    blob_data = json.dumps(data).encode('utf8')
    for codec in ('zlib', 'base64'):
        blob_data = codecs.encode(blob_data, codec)
    return blob_data


def decode_blob(blob_data):
    """ Decode a base64-encoded, zlib-compressed binary blob to a dictionary.

    :param blob_data:   base64-encoded binary blob, contains zlib-compressed
                        JSON
    :type blob_data:    bytes
    :returns:           The original data as a dictionary
    """
    for codec in ('base64', 'zlib'):
        blob_data = codecs.decode(blob_data, codec)
    return json.loads(blob_data.decode('utf8'))


class ZoteroBackend(object):
    @staticmethod
    def create_api_key():
        """ Interactively create a new API key via Zotero's OAuth API.

        Requires the user to enter a verification key displayed in the browser.

        :returns:   API key and the user's library ID
        """
        auth = OAuth1Service(
            name='zotero',
            consumer_key=CLIENT_KEY,
            consumer_secret=CLIENT_SECRET,
            request_token_url=REQUEST_TOKEN_URL,
            access_token_url=ACCESS_TOKEN_URL,
            authorize_url=AUTH_URL,
            base_url=BASE_URL)
        token, secret = auth.get_request_token(
            params={'oauth_callback': 'oob'})
        auth_url = auth.get_authorize_url(token)
        auth_url += '&' + urlencode({
            'name': 'zotero-cli',
            'library_access': 1,
            'notes_access': 1,
            'write_access': 1,
            'all_groups': 'read'})
        click.echo("Opening {} in browser, please confirm.".format(auth_url))
        click.launch(auth_url)
        verification = click.prompt("Enter verification code")
        token_resp = auth.get_raw_access_token(
            token, secret, method='POST',
            data={'oauth_verifier': verification})
        if not token_resp:
            logging.debug(token_resp.content)
            click.fail("Error during API key generation.")
        access = urlparse.parse_qs(token_resp.text)
        return access['oauth_token'][0], access['userID'][0]

    def __init__(self, api_key=None, library_id=None, library_type='user'):
        """ Service class for communicating with the Zotero API.

        This is mainly a thin wrapper around :py:class:`pyzotero.zotero.Zotero`
        that handles things like transparent HTML<->[edit-formt] conversion.

        :param api_key:     API key for the Zotero API, will be loaded from
                            the configuration if not specified
        :param library_id:  Zotero library ID the API key is valid for, will
                            be loaded from the configuration if not specified
        :param library_type: Type of the library, can be 'user' or 'group'
        """
        self._logger = logging.getLogger()
        idx_path = os.path.join(click.get_app_dir(APP_NAME), 'index.sqlite')
        self.config = load_config()
        self.note_format = self.config['zotcli.note_format']
        self.storage_dir = self.config.get('zotcli.storage_dir')

        api_key = api_key or self.config.get('zotcli.api_key')
        library_id = library_id or self.config.get('zotcli.library_id')

        if not api_key or not library_id:
            raise ValueError(
                "Please set your API key and library ID by running "
                "`zotcli configure` or pass them as command-line options.")
        self._zot = Zotero(library_id=library_id, api_key=api_key,
                           library_type=library_type)
        self._index = SearchIndex(idx_path)
        sync_interval = self.config.get('zotcli.sync_interval', 300)
        since_last_sync = int(time.time()) - self._index.last_modified
        if since_last_sync >= int(sync_interval):
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
            note['data']['note'] = self._make_note(note)
            yield note

    def attachments(self, item_id):
        """ Get a list of all attachments for a given item.

        If a zotero profile directory is specified in the configuration,
        a resolved local file path will be included, if the file exists.

        :param item_id:     ID/key of the item to get attachments for
        :returns:           Attachments for item
        """
        attachments = self._zot.children(item_id, itemType="attachment")
        if self.storage_dir:
            for att in attachments:
                if not att['data']['linkMode'].startswith("imported"):
                    continue
                fpath = os.path.join(self.storage_dir, att['key'],
                                     att['data']['filename'])
                if not os.path.exists(fpath):
                    continue
                att['data']['path'] = fpath
        return attachments

    def get_attachment_path(self, attachment):
        if not attachment['data']['linkMode'].startswith("imported"):
            raise ValueError(
                "Attachment is not stored on server, cannot download!")
        storage_method = self.config['zotcli.sync_method']
        if storage_method == 'local':
            return Path(attachment['data']['path'])
        out_path = TEMP_DIR/attachment['data']['filename']
        if out_path.exists():
            return out_path
        if storage_method == 'zotero':
            self._zot.dump(attachment['key'], path=unicode(TEMP_DIR))
            return out_path
        elif storage_method == 'webdav':
            user = self.config['zotcli.webdav_user']
            password = self.config['zotcli.webdav_pass']
            location = self.config['zotcli.webdav_path']
            zip_url = "{}/zotero/{}.zip".format(
                location, attachment['key'])
            resp = requests.get(zip_url, auth=(user, password))
            zf = zipfile.ZipFile(StringIO(resp.content))
            zf.extractall(str(TEMP_DIR))
        return out_path

    def _make_note(self, note_data):
        """ Converts a note from HTML to the configured markup.

        If the note was previously edited with zotcli, the original markup
        will be restored. If it was edited with the Zotero UI, it will be
        converted from the HTML via pandoc.

        :param note_html:       HTML of the note
        :param note_version:    Library version the note was last edited
        :returns:               Dictionary with markup, format and version
        """
        data = None
        note_html = note_data['data']['note']
        note_version = note_data['version']
        blobs = DATA_PAT.findall(note_html)
        # Previously edited with zotcli
        if blobs:
            data = decode_blob(blobs[0])
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
        extra_data = DATA_TMPL.format(data=encode_blob(note_data))
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
        try:
            self._zot.create_items([note], item_id)
        except Exception as e:
            self._logger.error(e)
            with open("note_backup.txt", "w") as fp:
                fp.write(note_data)
            self._logger.warn(
                "Could not upload note to Zotero. You can find the note "
                "markup in 'note_backup.txt' in the current directory")

    def save_note(self, note):
        """ Update an existing note.

        :param note:        The updated note
        """
        raw_data = note['data']['note']
        raw_data['version'] += 1
        note['data']['note'] = self._make_note_html(raw_data)
        try:
            self._zot.update_item(note)
        except Exception as e:
            self._logger.error(e)
            with open("note_backup.txt", "w") as fp:
                fp.write(raw_data['text'])
            self._logger.warn(
                "Could not upload note to Zotero. You can find the note "
                "markup in 'note_backup.txt' in the current directory")
