# zotero-cli

[![asciicast](http://asciinema.org/a/17n8da33w2gj67pyfwegfmfns.png)](https://asciinema.org/a/17n8da33w2gj67pyfwegfmfns)

A simple command-line interface for the Zotero API.

Currently the following features are supported:

- Search for items in the library
- Add/Edit notes for existing items
- Launch reader application for item attachments
- Edit notes with a text editor of your choice in any format supported by
  pandoc (markdown, reStructuredText, etc.)


## Installation
`zotero-cli` can be trivially installed from PyPi with `pip`:

```
$ pip install zotero-cli
```

If you want to try the bleeding-edge version:

```
$ pip install git+git://github.com/jbaiter/zotero-cli.git@master
```

## Usage

To change the editor on *nix systems, set the `VISUAL` environment variable.

If you want to use a markup format other than pandoc's `markdown`, edit
the configuration file under `~/.config/zotcli/config.ini` and set the
`note_format` field to your desired value (as seen in `pandoc --help`).

First, perform the initial configuration to generate an API key for the
application:
```
$ zotcli configure
```

Search for an item:
```
$ zotcli query "deep learning"
[F5R83K6P] Goodfellow et al.: Deep Learning
```
Query strings with whitespace must be enclosed in quotes. For details on the
supported syntax, consult the [SQLite FTS documentation](https://www.sqlite.org/fts3.html#section_3).
Briefly, supported are `AND`/`OR`/`NOT` operators and prefix-search via the Kleene
operator (e.g. `pref*`).

Read an item's attachment:
```
$ zotcli read "deep learning"
# Will launch the default PDF viewer with the item's first PDF attachment
```

Add a new note to an item using either the item's ID or a query string to
locate it:
```
$ zotcli add-note "deep learning"
# Edit note in editor, save and it will be added to the library
```
If more than one item is found for the query string, you will be prompted which
one to use.

Edit an existing note (you can use a query string instead of an ID, too):
```
$ zotcli edit-note F5R83K6P
# Edit note in editor, save and it will be updated in the library
```
