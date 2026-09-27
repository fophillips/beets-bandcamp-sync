# beets-bandcamp-sync

A [beets](https://beets.io/) plugin that finds albums missing from your local
library, downloads them from your Bandcamp collection, and imports them through
beets. Metadata is provided by the
[`beetcamp`](https://github.com/snejus/beetcamp) plugin.

This project uses Bandcamp's undocumented web endpoints and may need updates if
Bandcamp changes them.

## Requirements

- Python 3.10 or later
- beets 2.4 or later
- A Bandcamp account with downloadable album purchases

## Installation

Install the plugin and its dependencies from PyPI:

```console
python -m pip install beets-bandcamp-sync
```

Enable both plugins and configure your Bandcamp username and cookie file in
your beets configuration:

```yaml
plugins: bandcamp bandcamp_sync

bandcamp_sync:
  username: your_bandcamp_username
  cookies: ~/.config/beets/bandcamp-cookies.txt
  format: flac
```

The cookie file must use the Netscape `cookies.txt` format. Export cookies for
`bandcamp.com` from a browser session in which you are signed in. This file
contains authentication credentials: do not share or commit it, and restrict
its permissions where supported, for example with
`chmod 600 ~/.config/beets/bandcamp-cookies.txt`.

The configured format must be one Bandcamp offers for the release, such as
`flac`, `mp3-320`, or `aiff-lossless`.

## Usage

Preview albums that would be imported:

```console
beet bandcamp-sync --pretend
```

Download and import missing albums:

```console
beet bandcamp-sync
```

The plugin considers an album present when its Bandcamp release URL is already
stored as the album ID, or when beets identifies a strong local metadata match.

## Development

Create a virtual environment with [uv](https://docs.astral.sh/uv/) and install
the project in editable mode:

```console
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest
python -m build
```

## License

Distributed under the [MIT License](LICENSE).
