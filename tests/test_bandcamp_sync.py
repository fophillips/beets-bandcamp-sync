import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from beets.ui import UserError

try:
    from beets.ui.commands import import_ as import_commands
except ImportError:
    from beets.ui import commands as import_commands

from beetsplug import bandcamp_sync
from beetsplug.bandcamp_sync import (
    BandcampSyncPlugin,
    bandcamp_album_for_id,
    local_match,
    pagedata,
    safe_extract,
)


def test_pagedata():
    page = '<div id="pagedata" data-blob="{&quot;fan_data&quot;: {}}"></div>'
    assert pagedata(page) == {"fan_data": {}}


@pytest.mark.parametrize(
    "page",
    [
        "<html></html>",
        '<div id="pagedata"></div>',
        '<div id="pagedata" data-blob="not-json"></div>',
        '<div id="pagedata" data-blob="[]"></div>',
    ],
)
def test_pagedata_rejects_invalid_data(page):
    with pytest.raises(UserError):
        pagedata(page)


def test_safe_extract(tmp_path):
    archive = tmp_path / "album.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Artist/Album/01 song.flac", "audio")
    destination = tmp_path / "music"
    destination.mkdir()
    safe_extract(str(archive), str(destination))
    assert (destination / "Artist/Album/01 song.flac").read_text() == "audio"


def test_safe_extract_rejects_path_escape(tmp_path):
    archive = tmp_path / "album.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside.flac", "audio")
    with pytest.raises(UserError):
        safe_extract(str(archive), str(tmp_path / "music"))


def test_safe_extract_rejects_invalid_zip(tmp_path):
    archive = tmp_path / "album.zip"
    archive.write_text("not a zip archive")

    with pytest.raises(UserError, match="valid ZIP"):
        safe_extract(str(archive), str(tmp_path / "music"))


def test_local_match_uses_current_distance_api(monkeypatch):
    item = SimpleNamespace()
    candidate = SimpleNamespace(items=lambda: [item])
    album = SimpleNamespace(data_source="Bandcamp", tracks=[])
    pairs = {}
    distance_args = []

    monkeypatch.setattr(
        bandcamp_sync,
        "assign_items",
        lambda items, tracks: (pairs, [], []),
    )

    def fake_distance(items, album_info, mapping):
        distance_args.append((items, album_info, mapping))
        return SimpleNamespace(distance=0)

    monkeypatch.setattr(bandcamp_sync, "distance", fake_distance)

    assert local_match(album, [candidate]) is candidate
    assert len(distance_args) == 1
    assert distance_args[0][1:] == (album, pairs)


def test_sync_uses_metadata_plugin_dispatch(monkeypatch):
    plugin = BandcampSyncPlugin()
    plugin.config["username"].set("listener")
    plugin.config["cookies"].set("cookies.txt")
    release_url = "https://artist.bandcamp.com/album/release"
    album = SimpleNamespace(artist="Artist", album="Release")
    requested = []

    monkeypatch.setattr(bandcamp_sync, "load_cookies", lambda path: object())
    monkeypatch.setattr(
        bandcamp_sync,
        "collection_items",
        lambda username, cookies: [{"release_url": release_url}],
    )

    def fake_album_for_id(*args):
        requested.append(args)
        return album

    monkeypatch.setattr(
        bandcamp_sync.metadata_plugins,
        "album_for_id",
        fake_album_for_id,
    )

    plugin.sync(
        SimpleNamespace(albums=list),
        SimpleNamespace(pretend=True),
        [],
    )

    expected = (
        (release_url, bandcamp_sync.BANDCAMP_DATA_SOURCE)
        if hasattr(bandcamp_sync.metadata_plugins, "get_metadata_source")
        else (release_url,)
    )
    assert requested == [expected]


def test_album_lookup_uses_current_beets_api(monkeypatch):
    calls = []
    album = object()
    monkeypatch.setattr(
        bandcamp_sync.metadata_plugins,
        "get_metadata_source",
        object(),
        raising=False,
    )
    monkeypatch.setattr(
        bandcamp_sync.metadata_plugins,
        "album_for_id",
        lambda *args: calls.append(args) or album,
    )

    assert bandcamp_album_for_id("release-url") is album
    assert calls == [("release-url", "Bandcamp")]


def test_album_lookup_uses_legacy_beets_api(monkeypatch):
    calls = []
    album = object()
    monkeypatch.delattr(
        bandcamp_sync.metadata_plugins,
        "get_metadata_source",
        raising=False,
    )
    monkeypatch.setattr(
        bandcamp_sync.metadata_plugins,
        "album_for_id",
        lambda *args: calls.append(args) or album,
    )

    assert bandcamp_album_for_id("release-url") is album
    assert calls == [("release-url",)]


def test_import_uses_beets_commands_module(monkeypatch, tmp_path):
    calls = []
    library = object()
    release_url = "https://artist.bandcamp.com/album/release"

    monkeypatch.setattr(
        import_commands,
        "import_files",
        lambda lib, paths, query: calls.append((lib, paths, query)),
    )

    BandcampSyncPlugin._import(library, str(tmp_path), release_url)

    assert calls == [(library, [os.fsencode(tmp_path)], None)]


def test_collection_requests_have_timeouts(monkeypatch):
    calls = []
    page = {
        "fan_data": {"fan_id": 1},
        "collection_data": {"item_count": 1, "last_token": "token"},
        "item_cache": {"collection": {}},
    }

    class Response:
        text = "collection page"

        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            return {
                "items": [
                    {
                        "tralbum_type": "a",
                        "sale_item_type": "a",
                        "sale_item_id": 1,
                        "item_url": "https://artist.bandcamp.com/album/release",
                    }
                ],
                "redownload_urls": {"a1": "https://bandcamp.com/download"},
            }

    def fake_get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return Response()

    def fake_post(url, **kwargs):
        calls.append(("post", url, kwargs))
        return Response()

    monkeypatch.setattr(bandcamp_sync, "pagedata", lambda text: page)
    monkeypatch.setattr(bandcamp_sync.requests, "get", fake_get)
    monkeypatch.setattr(bandcamp_sync.requests, "post", fake_post)

    releases = bandcamp_sync.collection_items("listener", object())

    assert len(releases) == 1
    assert [call[2]["timeout"] for call in calls] == [
        bandcamp_sync.HTTP_TIMEOUT,
        bandcamp_sync.HTTP_TIMEOUT,
    ]
    assert calls[1][2]["json"] == {
        "fan_id": 1,
        "count": 1,
        "older_than_token": "token",
    }


def test_collection_wraps_network_errors(monkeypatch):
    def fail(*args, **kwargs):
        raise bandcamp_sync.requests.ConnectionError("offline")

    monkeypatch.setattr(bandcamp_sync.requests, "get", fail)

    with pytest.raises(UserError, match="could not read Bandcamp collection"):
        bandcamp_sync.collection_items("listener", object())


def test_download_is_streamed(monkeypatch):
    calls = []
    extracted = []
    imported = []
    cookies = object()

    class PageResponse:
        text = "download page"

        @staticmethod
        def raise_for_status():
            pass

    class DownloadResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def iter_content(chunk_size):
            assert chunk_size == bandcamp_sync.DOWNLOAD_CHUNK_SIZE
            yield b"first"
            yield b"second"

        @property
        def content(self):
            raise AssertionError("streamed downloads must not use response.content")

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if kwargs.get("stream"):
            return DownloadResponse()
        return PageResponse()

    def fake_extract(path, destination):
        extracted.append(Path(path).read_bytes())

    plugin = BandcampSyncPlugin()
    monkeypatch.setattr(bandcamp_sync.requests, "get", fake_get)
    monkeypatch.setattr(
        bandcamp_sync,
        "pagedata",
        lambda text: {
            "download_items": [
                {"downloads": {"flac": {"url": "https://example.com/album"}}}
            ]
        },
    )
    monkeypatch.setattr(bandcamp_sync, "safe_extract", fake_extract)
    monkeypatch.setattr(
        plugin,
        "_import",
        lambda lib, path, url: imported.append((lib, path, url)),
    )

    plugin.download_and_import(
        object(),
        cookies,
        {"redownload_url": "https://bandcamp.com/redownload"},
        "https://artist.bandcamp.com/album/release",
    )

    assert extracted == [b"firstsecond"]
    assert len(imported) == 1
    assert calls[0][1] == {
        "cookies": cookies,
        "timeout": bandcamp_sync.HTTP_TIMEOUT,
    }
    assert calls[1][1] == {
        "cookies": cookies,
        "timeout": bandcamp_sync.HTTP_TIMEOUT,
        "stream": True,
    }


def test_download_rejects_empty_download_data(monkeypatch):
    class Response:
        text = "download page"

        @staticmethod
        def raise_for_status():
            pass

    plugin = BandcampSyncPlugin()
    monkeypatch.setattr(
        bandcamp_sync.requests,
        "get",
        lambda *args, **kwargs: Response(),
    )
    monkeypatch.setattr(
        bandcamp_sync,
        "pagedata",
        lambda text: {"download_items": []},
    )

    with pytest.raises(UserError, match="invalid download data"):
        plugin.download_and_import(
            object(),
            object(),
            {"redownload_url": "https://bandcamp.com/redownload"},
            "https://artist.bandcamp.com/album/release",
        )
