from __future__ import print_function
import logging
import re

import click

from zotero_cli.backend import ZoteroBackend

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
        ctx.obj = ZoteroBackend(api_key, library_id, library_type)
    except ValueError as e:
        ctx.fail(e.args[0])


@cli.command()
@click.pass_context
def sync(ctx):
    """ Synchronize the local search index with the online library. """
    num_items = ctx.obj.synchronize()
    click.echo("Updated {} items.".format(num_items))


@cli.command()
@click.argument("query", required=False)
@click.option("--limit", "-n", type=int, default=100)
@click.pass_context
def query(ctx, query, limit):
    """ Search for items in the Zotero database. """
    for item in ctx.obj.search(query, limit):
        out = click.style(u"[{}] ".format(item.citekey or item.key),
                          fg='green')
        if item.creator:
            out += click.style(item.creator + u': ', fg='cyan')
        out += click.style(item.title, fg='blue')
        if item.date:
            out += click.style(" ({})".format(item.date), fg='yellow')
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
        items = tuple(ctx.obj.search(item_id))
        if len(items) > 1:
            item_id = select_item(items).key
        elif items:
            item_id = items[0].key
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
        items = tuple(ctx.obj.search(item_id))
        if len(items) > 1:
            item_id = select_item(items).key
        elif items:
            item_id = items[0].key
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
        if item.creator:
            out += click.style(item.creator + u': ', fg="cyan")
        out += click.style(item.title, fg='blue')
        if item.date:
            out += click.style(" ({})".format(item.date), fg='yellow')
        click.echo(out)
    while True:
        item_idx = click.prompt("Please select an item", default=0, type=int,
                                err=True)
        if item_idx < 0 or item_idx >= len(items):
            click.echo("Value must be between 0 and {}!".format(len(items)-1),
                       err=True)
        else:
            return items[item_idx]
