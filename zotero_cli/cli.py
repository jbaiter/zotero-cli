from __future__ import print_function
import itertools
import logging
import os
import re

import click
import pathlib
import pypandoc
import requests

from zotero_cli.common import save_config
from zotero_cli.backend import ZoteroBackend

EXTENSION_MAP = {
    'docbook': 'dbk',
    'latex': 'tex',
}

ID_PAT = re.compile(r'[A-Z0-9]{8}')
PROFILE_PAT = re.compile(r'([a-z0-9]{8})\.(.*)')


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


def find_storage_directories():
    import pdb; pdb.set_trace()
    home_dir = pathlib.Path(os.environ['HOME'])
    candidates = []
    firefox_dir = home_dir/".mozilla"/"firefox"
    if firefox_dir.exists():
        candidates.append(firefox_dir.iterdir())
    zotero_dir = home_dir/".zotero"
    if zotero_dir.exists():
        candidates.append(zotero_dir.iterdir())
    candidate_iter = itertools.chain.from_iterable(candidates)
    for fpath in candidate_iter:
        if not fpath.is_dir():
            continue
        match = PROFILE_PAT.match(fpath.name)
        if match:
            storage_path = fpath/"zotero"/"storage"
            if storage_path.exists():
                yield (match.group(2), storage_path)


@click.group(context_settings={'help_option_names': ['-h', '--help']})
@click.option('--verbose', '-v', is_flag=True)
@click.option('--api-key', default=None)
@click.option('--library-id', default=None)
@click.pass_context
def cli(ctx, verbose, api_key, library_id):
    """ Command-line access to your Zotero library. """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    if ctx.invoked_subcommand != 'configure':
        try:
            ctx.obj = ZoteroBackend(api_key, library_id, 'user')
        except ValueError as e:
            ctx.fail(e.args[0])


@cli.command()
def configure():
    """ Perform initial setup. """
    config = {
        'sync_interval': 300
    }
    generate_key = not click.confirm("Do you already have an API key for "
                                     "zotero-cli?")
    if generate_key:
        (config['api_key'],
         config['library_id']) = ZoteroBackend.create_api_key()
    else:
        config['api_key'] = click.prompt(
            "Please enter the API key for zotero-cli")
        config['library_id'] = click.prompt("Please enter your library ID")
    sync_method = select(
        [("local", "Local Zotero storage"),
            ("zotcoud", "Use Zotero file cloud"),
            ("webdav", "Use WebDAV storage")],
        default=1, required=True,
        prompt="How do you want to access files for reading?")
    if sync_method == "local":
        storage_dirs = tuple(find_storage_directories())
        if storage_dirs:
            options = [(name, "{} ({})".format(click.style(name, fg="cyan"),
                                               path))
                       for name, path in storage_dirs]
            config['storage_dir'] = select(
                options, required=False,
                prompt="Please select a storage directory (-1 to enter "
                       "manually)")
        if config.get('storage_dir') is None:
            click.echo(
                "Could not automatically locate a Zotero storage directory.")
            while True:
                storage_dir = click.prompt(
                    "Please enter the path to your Zotero storage directory",
                    default='')
                if not storage_dir:
                    storage_dir = None
                    break
                elif not os.path.exists(storage_dir):
                    click.echo("Directory does not exist!")
                elif not re.match(r'.*storage/?', storage_dir):
                    click.echo("Path must point to a `storage` directory!")
                else:
                    config['storage_dir'] = storage_dir
                    break
    elif sync_method == "webdav":
        while True:
            if not config.get('webdav_path'):
                config['webdav_path'] = click.prompt(
                    "Please enter the WebDAV URL (without '/zotero'!)")
            if not config.get('webdav_user'):
                config['webdav_user'] = click.prompt(
                    "Please enter the WebDAV user name")
                config['webdav_pass'] = click.prompt(
                    "Please enter the WebDAV password")
            try:
                test_resp = requests.get(
                    config['webdav_path'],
                    auth=(config['webdav_user'],
                          config['webdav_pass']))
            except requests.ConnectionError:
                click.echo("Invalid WebDAV URL, could not reach server.")
                config['webdav_path'] = None
                continue
            if test_resp.status_code == 501:
                break
            elif test_resp.status_code == 404:
                click.echo("Invalid WebDAV path, does not exist.")
                config['webdav_path'] = None
            elif test_resp.status_code == 401:
                click.echo("Bad credentials.")
                config['webdav_user'] = None
            else:
                click.echo("Unknown error, please check your settings.")
                config['webdav_path'] = None
                config['webdav_user'] = None
    config['sync_method'] = sync_method

    markup_formats = pypandoc.get_pandoc_formats()[0]
    config['note_format'] = select(zip(markup_formats, markup_formats),
                                   default=markup_formats.index('markdown'),
                                   prompt="Select markup format for notes")
    save_config(config)
    zot = ZoteroBackend(config['api_key'], config['library_id'], 'user')
    click.echo("Initializing local index...")
    num_synced = zot.synchronize()
    click.echo("Synchronized {} items.".format(num_synced))


@cli.command()
@click.pass_context
def sync(ctx):
    """ Synchronize the local search index. """
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


@cli.command()
@click.option("--with-note", '-n', required=False, is_flag=True, default=False,
              help="Open the editor for taking notes while reading.")
@click.argument("item-id", required=True)
@click.pass_context
def read(ctx, item_id, with_note):
    """ Read an item attachment. """
    try:
        item_id = pick_item(ctx.obj, item_id)
    except ValueError as e:
        ctx.fail(e.args[0])
    read_att = None
    attachments = ctx.obj.attachments(item_id)
    if not attachments:
        ctx.fail("Could not find an attachment for reading.")
    elif len(attachments) > 1:
        click.echo("Multiple attachments available.")
        read_att = select([(att, att['data']['title'])
                           for att in attachments])
    else:
        read_att = attachments[0]

    att_path = ctx.obj.get_attachment_path(read_att)
    click.echo("Opening '{}'.".format(att_path))
    click.launch(str(att_path), wait=False)
    if with_note:
        existing_notes = list(ctx.obj.notes(item_id))
        if existing_notes:
            edit_existing = click.confirm("Edit existing note?")
            if edit_existing:
                note = pick_note(ctx, ctx.obj, item_id)
            else:
                note = None
        else:
            note = None
        note_body = click.edit(
            text=note['data']['note']['text'] if note else None,
            extension=get_extension(ctx.obj.note_format))
        if note_body and note is None:
            ctx.obj.create_note(item_id, note_body)
        elif note_body:
            note['data']['note']['text'] = note_body
            ctx.obj.save_note(note)


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
    try:
        item_id = pick_item(ctx.obj, item_id)
    except ValueError as e:
        ctx.fail(e.args[0])
    note_body = click.edit(extension=get_extension(ctx.obj.note_format))
    if note_body:
        ctx.obj.create_note(item_id, note_body)


@cli.command("edit-note")
@click.argument("item-id", required=True)
@click.argument("note-num", required=False, type=int)
@click.pass_context
def edit_note(ctx, item_id, note_num):
    """ Edit a note. """
    try:
        item_id = pick_item(ctx.obj, item_id)
    except ValueError as e:
        ctx.fail(e.args[0])
    note = pick_note(ctx, ctx.obj, item_id, note_num)
    updated_text = click.edit(note['data']['note']['text'],
                              extension=get_extension(ctx.obj.note_format))
    if updated_text:
        note['data']['note']['text'] = updated_text
        ctx.obj.save_note(note)


@cli.command("export-note")
@click.argument("item-id", required=True)
@click.argument("note-num", required=False, type=int)
@click.option("--output", '-o', type=click.File(mode='w'), default='-')
@click.pass_context
def export_note(ctx, item_id, note_num, output):
    """ Export a note. """
    try:
        item_id = pick_item(ctx.obj, item_id)
    except ValueError as e:
        ctx.fail(e.args[0])
    note = pick_note(ctx, ctx.obj, item_id, note_num)
    output.write(note['data']['note']['text'].encode('utf8'))


def pick_note(ctx, zot, item_id, note_num=None):
    notes = tuple(zot.notes(item_id))
    if not notes:
        ctx.fail("The item does not have any notes.")
    if note_num is None:
        if len(notes) > 1:
            note = select(
                [(n, re.sub("[^\w]", " ",
                            n['data']['note']['text'].split('\n')[0]))
                 for n in notes])
        else:
            note = notes[0]
    else:
        note = notes[note_num]
    return note


def pick_item(zot, item_id):
    if not ID_PAT.match(item_id):
        items = tuple(zot.search(item_id))
        if len(items) > 1:
            click.echo("Multiple matches available.")
            item_descriptions = []
            for it in items:
                desc = click.style(it.title, fg='blue')
                if it.creator:
                    desc = click.style(it.creator + u': ', fg="cyan") + desc
                if it.date:
                    desc += click.style(" ({})".format(it.date), fg='yellow')
                item_descriptions.append(desc)
            return select(zip(items, item_descriptions)).key
        elif items:
            return items[0].key
        else:
            raise ValueError("Could not find any items for the query.")
    return item_id


def select(choices, prompt="Please choose one", default=0, required=True):
    """ Let the user pick one of several choices.


    :param choices:     Available choices along with their description
    :type choices:      iterable of (object, str) tuples
    :param default:     Index of default choice
    :type default:      int
    :param required:    If true, `None` can be returned
    :returns:           The object the user picked or None.
    """
    choices = list(choices)
    for idx, choice in enumerate(choices):
        _, choice_label = choice
        if '\x1b' not in choice_label:
            choice_label = click.style(choice_label, fg='blue')
        click.echo(
            u"{key} {description}".format(
                key=click.style(u"[{}]".format(idx), fg='green'),
                description=choice_label))
    while True:
        choice_idx = click.prompt(prompt, default=default, type=int, err=True)
        cutoff = -1 if not required else 0
        if choice_idx < cutoff or choice_idx >= len(choices):
            click.echo(
                "Value must be between {} and {}!"
                .format(cutoff, len(choices)-1), err=True)
        elif choice_idx == -1:
            return None
        else:
            return choices[choice_idx][0]
