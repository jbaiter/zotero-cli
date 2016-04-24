from __future__ import print_function
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
        cfg_path = os.path.join(click.get_app_dir(APP_NAME), 'config.ini')
        self.config = load_config(cfg_path)
        self.note_format = self.config['zotcli.note_format']

        api_key = api_key or self.config.get('zotcli.api_key')
        library_id = library_id or self.config.get('zotcli.library_id')
        library_type = (library_type or self.config.get('zotcli.library_type')
                        or 'user')
        if not api_key or not library_id:
            raise ValueError(
                "Please set your API key and library ID in the configuration file "
                "({}) or pass them as command-line options.\nIf you do not have "
                "these, please go to https://www.zotero.org/settings/keys to "
                "retrieve them.".format(cfg_path))
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
        items = self._zot.everything(query_fn(**query_args))
        for it in items:
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
            note['data']['note'] = pypandoc.convert(
                note['data']['note'], self.note_format, format='html')
        return notes

    def create_note(self, item_id, note_text):
        """ Create a new note for a given item.

        :param item_id:     ID/key of the item to create the note for
        :param note_text:   Text of the note
        """
        note = self._zot.item_template('note')
        note['note'] = pypandoc.convert(
            note_text, 'html', format=self.note_format)
        self._zot.create_items([note], item_id)

    def save_note(self, note):
        """ Update an existing note.

        :param note:        The updated note
        """
        note_html = pypandoc.convert(
            note['data']['note'], 'html', format=self.note_format)
        note['data']['note'] = note_html
        self._zot.update_item(note)


@click.group()
@click.option('--api-key', default=None)
@click.option('--library-id', default=None)
@click.option('--library-type', type=click.Choice(['user', 'group']),
              default=None)
@click.pass_context
def cli(ctx, api_key, library_id, library_type):
    try:
        ctx.obj = ZoteroCli(api_key, library_id, library_type)
    except ValueError as e:
        ctx.fail(e.args[0])


@cli.command()
@click.argument("query", required=False)
@click.option("--limit", type=int, default=100)
@click.pass_context
def query(ctx, query, limit):
    """ Search for items in the Zotero database. """
    for item in ctx.obj.items(query, limit):
        click.echo(
            u"{key} {creator}{title}{date}".format(
                key=click.style(u"[{}]".format(item['key']), fg='green'),
                creator=(click.style(item['creator'] + u': ', fg='cyan')
                            if item['creator'] else ''),
                title=click.style(item['title'], fg='blue'),
                date=(click.style(" ({})".format(item['date']),
                                    fg='yellow')
                        if item['date'] else '')))


@cli.command("add-note")
@click.argument("item-id", required=True)
@click.pass_context
def add_note(ctx, item_id):
    """ Add a new note to an existing item. """
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
    updated_note = click.edit(note['data']['note'],
                              extension=get_extension(ctx.obj.note_format))
    if updated_note:
        note['data']['note'] = updated_note
        ctx.obj.save_note(note)


def select_note(notes):
    for idx, note in enumerate(notes):
        words = u" ".join(
            re.sub("[^\w]", " ", note['data']['note'].split('\n')[0])
              .split()[:5])
        click.echo(
            u"{key} {words}".format(
                key=click.style(u"[{}]".format(idx), fg='green'),
                words=click.style(words, fg='blue')))
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
        click.echo(
            u"{key} {creator}{title}{date}".format(
                key=click.style(u"[{}]".format(idx, fg='green')),
                creator=item['creator'] + u': ' if item['creator'] else '',
                title=click.style(item['title'], fg='blue'),
                date=(click.style(" ({})".format(item['date'], fg='yellow'))
                      if item['date'] else '')))
    while True:
        item_idx = click.prompt("Please select an item", default=0, type=int,
                                err=True)
        if item_idx < 0 or item_idx >= len(items):
            click.echo("Value must be between 0 and {}!".format(len(items)-1),
                       err=True)
        else:
            return items[item_idx]
