from __future__ import print_function
import json
import logging
import os
import pkg_resources
import re
import shutil
import ConfigParser

import click
import pypandoc
from pyzotero.zotero import Zotero

APP_NAME = "zotcli"
EXTENSION_MAP = {
    'docbook': 'dbk',
    'latex': 'tex',
}
ID_PAT = re.compile(r'[A-Z0-9]{8}')
DATA_PAT = re.compile(
    r'<div class="zotcli-note">.*<p .*title="([A-Za-z0-9+/=\n ]+)">.*</div>',
    flags=re.DOTALL | re.MULTILINE)
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


def get_extension(pandoc_fmt):
    """ Get the file extension for a given pandoc format.

    :param pandoc_fmt:  A format as supported by (py)pandoc
    :returns:           The file extension with leading dot
    """
    if 'mark' in pandoc_fmt:
        return '.md'
    elif pandoc_fmt in EXTENSION_MAP:
        return EXTENSION_MAP[pandoc_fmt]
    else:
        return '.' + pandoc_fmt


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

    def items(self, query=None, limit=None, recursive=False):
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
        query_args = {}
        if query:
            query_args['q'] = query
        if limit:
            query_args['limit'] = limit
        query_fn = self._zot.items if recursive else self._zot.top
        items = self._zot.makeiter(query_fn(**query_args))
        for chunk in items:
            for it in chunk:
                yield {'key': it['data']['key'],
                       'creator': it['meta'].get('creatorSummary'),
                       'title': it['data'].get('title', "Untitled"),
                       'date': it['data'].get('date'),
                       'has_children': it['meta'].get('numChildren', 0) > 0}

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
                     'version': self._zot.last_modified_version()+2}
        note['note'] = self._make_note_html(note_data)
        self._zot.create_items([note], item_id)

    def save_note(self, note):
        """ Update an existing note.

        :param note:        The updated note
        """
        note['data']['note']['version'] += 1
        note['data']['note'] = self._make_note_html(note['data']['note'])
        self._zot.update_item(note)


@click.group()
@click.option('--verbose', '-v', is_flag=True)
@click.option('--api-key', default=None)
@click.option('--library-id', default=None)
@click.option('--library-type', type=click.Choice(['user', 'group']),
              default=None)
@click.pass_context
def cli(ctx, verbose, api_key, library_id, library_type):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    try:
        ctx.obj = ZoteroCli(api_key, library_id, library_type)
    except ValueError as e:
        ctx.fail(e.args[0])


@cli.command()
@click.argument("query", required=False)
@click.option("--limit", "-n", type=int, default=100)
@click.pass_context
def query(ctx, query, limit):
    """ Search for items in the Zotero database. """
    for item in ctx.obj.items(query, limit):
        out = click.style(u"[{}] ".format(item['key']), fg='green')
        if item['creator']:
            out += click.style(item['creator'] + u': ', fg='cyan')
        out += click.style(item['title'], fg='blue')
        if item['date']:
            out += click.style(" ({})".format(item['date']), fg='yellow')
        click.echo(out)


@cli.command("add-note")
@click.argument("item-id", required=True)
@click.option("--note-format", "-f", required=False,
              help=("Markup format for editing notes, see the pandoc docs for "
                    "possible values"))
@click.pass_context
def add_note(ctx, item_id, note_format):
    """ Add a new note to an existing item. """
    if note_format:
        ctx.obj.note_format = note_format
    if not ID_PAT.match(item_id):
        items = tuple(ctx.obj.items(item_id))
        if len(items) > 1:
            item_id = select_item(items)['key']
        elif items:
            item_id = items[0]['key']
        else:
            ctx.fail("Could not find any items for the query.")
    note_body = click.edit(extension=get_extension(ctx.obj.note_format))
    if note_body:
        ctx.obj.create_note(item_id, note_body)


@cli.command("edit-note")
@click.argument("item-id", required=True)
@click.argument("note-num", required=False, type=int)
@click.pass_context
def edit_note(ctx, item_id, note_num):
    """ Edit a note. """
    if not ID_PAT.match(item_id):
        items = tuple(ctx.obj.items(item_id))
        if len(items) > 1:
            item_id = select_item(items)['key']
        elif items:
            item_id = items[0]['key']
        else:
            ctx.fail("Could not find any items for the query.")
    notes = ctx.obj.notes(item_id)
    if not notes:
        ctx.fail("The item does not have any notes.")
    if note_num is None:
        if len(notes) > 1:
            note = select_note(notes)
        else:
            note = notes[0]
    else:
        note = notes[note_num]
    updated_text = click.edit(note['data']['note']['text'],
                              extension=get_extension(ctx.obj.note_format))
    if updated_text:
        note['data']['note']['text'] = updated_text
        ctx.obj.save_note(note)


def select_note(notes):
    for idx, note in enumerate(notes):
        note_text = note['data']['note']['text']
        first_line = re.sub("[^\w]", " ", note_text.split('\n')[0])
        click.echo(
            u"{key} {words}".format(
                key=click.style(u"[{}]".format(idx), fg='green'),
                words=click.style(first_line, fg='blue')))
    while True:
        note_id = click.prompt("Please select a note.", default=0, type=int,
                               err=True)
        if note_id < 0 or note_id >= len(notes):
            click.echo("Value must be between 0 and {}!".format(len(notes)-1),
                       err=True)
        else:
            return notes[note_id]


def select_item(items):
    for idx, item in enumerate(items):
        out = click.style(u"[{}] ".format(idx), fg='green')
        if item['creator']:
            out += click.style(item['creator'] + u': ', fg="cyan")
        out += click.style(item['title'], fg='blue')
        if item['date']:
            out += click.style(" ({})".format(item['date']), fg='yellow')
        click.echo(out)
    while True:
        item_idx = click.prompt("Please select an item", default=0, type=int,
                                err=True)
        if item_idx < 0 or item_idx >= len(items):
            click.echo("Value must be between 0 and {}!".format(len(items)-1),
                       err=True)
        else:
            return items[item_idx]
