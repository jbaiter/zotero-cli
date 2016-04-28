from __future__ import print_function
import itertools
import logging
import os
import re
import tempfile
import urllib
import urlparse
import ConfigParser

import click
import pathlib
import pypandoc
from rauth import OAuth1Service

from zotero_cli.common import APP_NAME
from zotero_cli.backend import ZoteroBackend

EXTENSION_MAP = {
    'docbook': 'dbk',
    'latex': 'tex',
}
READ_TYPES = (
    "application/pdf",
    "image/x.djvu",
    "application/epub+zip",
    "application/x-mobipocket-ebook")
ID_PAT = re.compile(r'[A-Z0-9]{8}')
PROFILE_PAT = re.compile(r'([a-z0-9]{8})\.(.*)')

CLIENT_KEY = 'c7d12bbd2c829823ddbc'
CLIENT_SECRET = 'c1ffe13aaeaa59ebf293'
REQUEST_TOKEN_URL = 'https://www.zotero.org/oauth/request'
AUTH_URL = 'https://www.zotero.org/oauth/authorize'
ACCESS_TOKEN_URL = 'https://www.zotero.org/oauth/access'
BASE_URL = 'https://api.zotero.org'


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
    # Zotero plugin
    home_dir = pathlib.Path(os.environ['HOME'])
    firefox_dir = home_dir/".mozilla"/"firefox"
    zotero_dir = home_dir/".zotero"
    candidate_iter = itertools.chain(firefox_dir.iterdir(),
                                     zotero_dir.iterdir())
    for fpath in candidate_iter:
        if not fpath.is_dir():
            continue
        match = PROFILE_PAT.match(fpath.name)
        if match:
            storage_path = fpath/"zotero"/"storage"
            if storage_path.exists():
                yield (match.group(2), storage_path)


def get_api_key():
    auth = OAuth1Service(
        name='zotero',
        consumer_key=CLIENT_KEY,
        consumer_secret=CLIENT_SECRET,
        request_token_url=REQUEST_TOKEN_URL,
        access_token_url=ACCESS_TOKEN_URL,
        authorize_url=AUTH_URL,
        base_url=BASE_URL)
    token, secret = auth.get_request_token(params={'oauth_callback': 'oob'})
    auth_url = auth.get_authorize_url(token)
    auth_url += '&' + urllib.urlencode({
        'name': 'zotero-cli',
        'library_access': 1,
        'notes_access': 1,
        'write_access': 1,
        'all_groups': 'read'})
    click.echo("Opening {} in browser, please confirm.".format(auth_url))
    click.launch(auth_url)
    verification = click.prompt("Enter verification code")
    token_resp = auth.get_raw_access_token(
        token, secret, method='POST', data={'oauth_verifier': verification})
    if not token_resp:
        logging.debug(token_resp.content)
        click.fail("Error during API key generation.")
    access = urlparse.parse_qs(token_resp.text)
    return access['oauth_token'][0], access['userID'][0]


@click.group()
@click.option('--verbose', '-v', is_flag=True)
@click.option('--api-key', default=None)
@click.option('--library-id', default=None)
@click.pass_context
def cli(ctx, verbose, api_key, library_id):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    if ctx.invoked_subcommand != 'configure':
        try:
            ctx.obj = ZoteroBackend(api_key, library_id, 'user')
        except ValueError as e:
            ctx.fail(e.args[0])


@cli.command()
def configure():
    """ Perform initial setup. """
    generate_key = not click.confirm("Do you already have an API key for "
                                     "zotero-cli?")
    if generate_key:
        api_key, library_id = get_api_key()
    else:
        api_key = click.prompt("Please enter the API key for zotero-cli")
        library_id = click.prompt("Please enter your library ID")
    storage_dirs = tuple(find_storage_directories())
    storage_dir = None
    if storage_dirs:
        for idx, sd in enumerate(storage_dirs):
            click.echo("[{}] {} ({})".format(idx, *sd))
        pick = click.prompt("Please select a storage directory "
                            "(leave empty to enter manually)", type=int,
                            default=-1)
        if pick != -1:
            storage_dir = storage_dirs[pick][1]
    if storage_dir is None:
        storage_dir = click.prompt("Please enter the path to your Zotero "
                                   "storage directory")
    markup_formats = pypandoc.get_pandoc_formats()[0]
    for idx, fmt in enumerate(markup_formats):
        click.echo("[{}] {}".format(idx, fmt))
    format_idx = click.prompt("Select markup format for notes",
                              default=markup_formats.index('markdown'),
                              type=int)
    note_format = markup_formats[format_idx]
    cfg_path = os.path.join(click.get_app_dir(APP_NAME), 'config.ini')
    cfg_dir = os.path.dirname(cfg_path)
    if not os.path.exists(cfg_dir):
        os.makedirs(cfg_dir)
    cfg = ConfigParser.SafeConfigParser()
    cfg.add_section("zotcli")
    cfg.set("zotcli", "api_key", api_key)
    cfg.set("zotcli", "library_id", library_id)
    cfg.set("zotcli", "storage_directory", storage_dir)
    cfg.set("zotcli", "note_format", note_format)
    cfg.set("zotcli", "sync_interval", "300")
    with open(cfg_path, "w") as fp:
        cfg.write(fp)
    zot = ZoteroBackend(api_key, library_id, 'user')
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
@click.argument("item-id", required=True)
@click.pass_context
def read(ctx, item_id):
    """ Read an item attachment. """
    try:
        item_id = pick_item(ctx.obj, item_id)
    except ValueError as e:
        ctx.fail(e.args[0])
    read_att = None
    for mime_type in READ_TYPES:
        for attachment in ctx.obj.attachments(item_id):
            if attachment['data']['contentType'] == mime_type:
                read_att = attachment
                break
    if not read_att:
        ctx.fail("Could not find an attachment for reading.")
    if 'path' not in read_att['data']:
        do_download = click.confirm(
            "Could not find file locally, do you want to download it?",
            default=True)
        if do_download:
            ctx.obj.download_attachment(read_att, tempfile.tempdir)
            read_att['data']['path'] = os.path.join(
                tempfile.tempdir, read_att['data']['filename'])
        else:
            return
    if os.path.exists(read_att['data']['path']):
        click.echo("Opening '{}'.".format(read_att['data']['path']))
        click.launch(read_att['data']['path'], wait=False)
    else:
        ctx.fail("Could not find file '{}'".format(read_att['data']['path']))


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
    notes = tuple(ctx.obj.notes(item_id))
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
    updated_text = click.edit(note['data']['note']['text'],
                              extension=get_extension(ctx.obj.note_format))
    if updated_text:
        note['data']['note']['text'] = updated_text
        ctx.obj.save_note(note)


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


def select(choices):
    """ Let the user pick one of several choices.


    :param choices:     Available choices along with their description
    :type choices:      iterable of (object, str) tuples
    :returns:           The object the user picked.
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
        choice_idx = click.prompt("Please choose one.", default=0, type=int,
                                  err=True)
        if choice_idx < 0 or choice_idx >= len(choices):
            click.echo(
                "Value must be between 0 and {}!".format(len(choices)-1),
                err=True)
        else:
            return choices[choice_idx][0]
