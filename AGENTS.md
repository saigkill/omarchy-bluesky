# AGENTS.md

Guidance for AI agents working in this repository. The README documents *what*
the plugin does; this file records *how* to change it without breaking it.

## What this is

An Omarchy shell plugin (Quickshell/QML) with a Python helper that owns the
Bluesky session. It is modelled on `saigkill.mastodon` and follows the same
rules; where the two differ, it is because the protocols do.

## The one rule that matters most

**Never let a credential reach a process argument or the panel.**

`/proc/<pid>/cmdline` is world readable. That is why:

- the app password travels in `BLUESKY_APP_PASSWORD` on the environment, is
  cleared from the `Process` after login, and is never stored;
- the panel never sees `accessJwt` or `refreshJwt`; its view is
  `{ service, handle, did, hasSession }`;
- every API call goes through `bluesky_helper.py`, one subcommand per action.
  There is deliberately no generic `get <url>`: do not add one;
- upload paths and composed posts travel in `BLUESKY_UPLOAD_PATH` and
  `BLUESKY_POST_JSON`, and `upload`/`post` take no arguments.

The tests enforce this. If a change makes a test about credentials fail, the
change is wrong. Do not adjust the test.

## Before you change anything

```sh
node tests/test_model.js
python3 -m unittest discover -s tests
```

Both must be green before and after.

## Protocol facts that are easy to get wrong

- **Facet offsets are UTF-8 bytes**, not JS string indices. `Model.segments()`
  maps them back; `detect_facets()` produces them. Test with non-ASCII text.
- **The post limit is 300 graphemes and 3000 bytes.** Neither QML nor Python has
  a grapheme segmenter, so both sides use the same approximation
  (`graphemeCount` / `grapheme_count`), which errs towards counting more. A
  test runs both on the same strings; keep them identical.
- **A reply needs root and parent.** The root is the parent's own root if the
  parent is a reply. See `Model.replyRefFor()`.
- **Alt text is part of the post record**, not of the upload. There is no
  request to make when it is saved.
- **Blob refs come back from the panel**, so `check_blob()` rebuilds them field
  by field. Never pass a panel-supplied object into a record unchecked.
- **Deleting is by your own record URI.** `delete_own_record()` checks the
  DID and the collection; keep every `un*` subcommand going through it.
- **Refresh tokens rotate.** Refreshing outside `StateLock` would race with a
  concurrently running helper.
- **Image limit is 2,000,000 bytes** (it used to be 1 MB). Check the lexicon
  before changing limits:
  `https://github.com/bluesky-social/atproto/tree/main/lexicons`.

## Editing Panel.qml

Everything from the Mastodon client's AGENTS.md applies:

**Count the braces after every insertion.** A stray `}` closes the enclosing
`Column`; Quickshell reports one `Syntax error` and the window opens empty.

```sh
python3 - <<'EOF'
import re
depth = 0
for l in open('Panel.qml', encoding='utf-8').read().split('\n'):
    s = re.sub(r'"(\\.|[^"\\])*"', '""', l)
    s = re.sub(r'//.*', '', s)
    for c in s:
        depth += (c == '{') - (c == '}')
print(depth)  # must be 0
EOF
```

- `qmllint` is not a syntax check here: `qs.*` imports give false positives.
- An invisible item still reports `implicitHeight` to a `Column`; use
  `height: visible ? implicitHeight : 0`.
- `visible: false` does not stop a `text:` binding from being evaluated: guard
  null cases inside the binding.
- `Array.isArray()` is `false` for a list from QML; duck-type on `length`.
- Do not bind a property you also write imperatively (see `setComposerText()`).
- `KeyboardPanel` only accepts visual items as direct children; put `Process`
  objects at the `Panel` level, as they are now.
- Feed lists are replaced, never mutated in place, or QML will not notice.
  `Model.patchPost()` and `patchAuthorFollow()` return new lists for that reason.

## Editing Model.js

Pure functions, no credential, no shell. Every command builder returns an array
starting with the helper. When you add one, add it to `COMMANDS` in
`tests/test_model.js`; a test checks that builders and helper subcommands match
one to one.

## Deploying to test

```sh
ln -s "$PWD" ~/.config/omarchy/plugins/saigkill.bluesky   # once
omarchy restart shell
journalctl --user --since "-1m" | grep -i bluesky
```

Use a full restart: QML keeps components it has already compiled, and
`rescanPlugins` does not replace them.

## Testing against the real network

Do not post test posts on the user's account, and do not like, repost or follow
anything to "see if it works". Read-only checks (feeds) are fine. The user's
session in `~/.local/state/omarchy-bluesky/auth.json` must not be touched.

## Conventions

- English in code comments and the README, German in conversation.
- No dependencies beyond the Python standard library, Qt/QML and Quickshell.
  ImageMagick is used when present and never required.
- Match the surrounding comment density: explain *why*.
