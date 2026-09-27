"""Download missing Bandcamp albums and import them into beets."""

from __future__ import annotations

import http.cookiejar
import json
import os
import tempfile
import zipfile
from copy import copy
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from beets import config, metadata_plugins, ui
from beets.autotag import AlbumInfo
from beets.autotag.distance import distance
from beets.autotag.match import assign_items
from beets.plugins import BeetsPlugin
from beets.ui import UserError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from beets.library import Album, Library


COLLECTION_URL = "https://bandcamp.com/api/fancollection/1/collection_items"
HTTP_TIMEOUT = (10, 60)
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
BANDCAMP_DATA_SOURCE = "Bandcamp"


def load_cookies(path: str) -> http.cookiejar.MozillaCookieJar:
    cookies = http.cookiejar.MozillaCookieJar(os.path.expanduser(path))
    try:
        cookies.load(ignore_discard=True, ignore_expires=True)
    except OSError as exc:
        raise UserError(f"could not read Bandcamp cookie file: {path}") from exc
    return cookies


def pagedata(text: str) -> dict[str, Any]:
    marker = 'id="pagedata"'
    start = text.find(marker)
    if start < 0:
        raise UserError("Bandcamp page did not contain collection data")
    blob_start = text.find('data-blob="', start)
    if blob_start < 0:
        raise UserError("Bandcamp page did not contain collection data")
    blob_start += len('data-blob="')
    blob_end = text.find('"', blob_start)
    if blob_end < 0:
        raise UserError("Bandcamp page contained invalid collection data")
    try:
        data = json.loads(unescape(text[blob_start:blob_end]))
    except json.JSONDecodeError as exc:
        raise UserError("Bandcamp page contained invalid collection data") from exc
    if not isinstance(data, dict):
        raise UserError("Bandcamp page contained invalid collection data")
    return data


def collection_items(
    username: str, cookies: http.cookiejar.MozillaCookieJar
) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            f"https://bandcamp.com/{username}",
            cookies=cookies,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise UserError("could not read Bandcamp collection") from exc

    page = pagedata(response.text)
    fan = page.get("fan_data")
    collection = page.get("collection_data")
    item_cache = page.get("item_cache")
    if not all(isinstance(value, dict) for value in (fan, collection, item_cache)):
        raise UserError("Bandcamp page contained invalid collection data")
    cached = item_cache.get("collection", {})
    if not isinstance(cached, dict):
        raise UserError("Bandcamp page contained invalid collection data")
    items = list(cached.values())
    urls = collection.get("redownload_urls", {}) or {}
    item_count = collection.get("item_count", len(items))
    if (
        not all(isinstance(item, dict) for item in items)
        or not isinstance(urls, dict)
        or not isinstance(item_count, int)
    ):
        raise UserError("Bandcamp page contained invalid collection data")
    token = collection.get("last_token", "")
    remaining = item_count - len(items)

    if remaining > 0:
        try:
            result = requests.post(
                COLLECTION_URL,
                json={
                    "fan_id": fan.get("fan_id"),
                    "count": remaining,
                    "older_than_token": token,
                },
                cookies=cookies,
                timeout=HTTP_TIMEOUT,
            )
            result.raise_for_status()
            data = result.json()
        except (requests.RequestException, ValueError) as exc:
            raise UserError("could not read Bandcamp collection") from exc
        if not isinstance(data, dict):
            raise UserError("Bandcamp returned invalid collection data")
        additional_items = data.get("items", [])
        additional_urls = data.get("redownload_urls", {}) or {}
        if (
            not isinstance(additional_items, list)
            or not all(isinstance(item, dict) for item in additional_items)
            or not isinstance(additional_urls, dict)
        ):
            raise UserError("Bandcamp returned invalid collection data")
        items.extend(additional_items)
        urls.update(additional_urls)

    output = []
    for item in items:
        if item.get("tralbum_type") != "a":
            continue
        key = f"{item.get('sale_item_type')}{item.get('sale_item_id')}"
        if redownload_url := urls.get(key):
            release_url = (
                item.get("item_url") or item.get("tralbum_url") or item.get("url")
            )
            if release_url:
                output.append(
                    {
                        **item,
                        "release_url": release_url,
                        "redownload_url": redownload_url,
                    }
                )
    return output


def local_match(album: AlbumInfo, albums: Iterable[Album]) -> Album | None:
    for candidate in albums:
        items = list(candidate.items())
        if not items:
            continue
        transient = []
        for item in items:
            cloned = copy(item)
            for field in ("mb_albumid", "mb_trackid", "mb_releasetrackid"):
                setattr(cloned, field, "")
            cloned.data_source = album.data_source
            cloned.media = ""
            transient.append(cloned)
        pairs, _, extra_tracks = assign_items(transient, album.tracks)
        result = distance(transient, album, pairs)
        strong_threshold = config["match"]["strong_rec_thresh"].as_number()
        if not extra_tracks and result.distance <= strong_threshold:
            return candidate
    return None


def bandcamp_album_for_id(url: str) -> AlbumInfo | None:
    if hasattr(metadata_plugins, "get_metadata_source"):
        return metadata_plugins.album_for_id(url, BANDCAMP_DATA_SOURCE)
    return metadata_plugins.album_for_id(url)


def safe_extract(
    path: str | os.PathLike[str], destination: str | os.PathLike[str]
) -> None:
    root = Path(destination).resolve()
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                target = (root / member.filename).resolve()
                if root not in target.parents and target != root:
                    raise UserError("Bandcamp archive contains an unsafe path")
            archive.extractall(root)
    except zipfile.BadZipFile as exc:
        raise UserError("Bandcamp download was not a valid ZIP archive") from exc


class BandcampSyncPlugin(BeetsPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.config.add({"username": "", "cookies": "", "format": "flac"})

    def commands(self) -> list[ui.Subcommand]:
        command = ui.Subcommand("bandcamp-sync", help=__doc__)
        command.parser.add_option(
            "--pretend",
            action="store_true",
            default=False,
            help="show missing albums without downloading or importing",
        )
        command.func = self.sync
        return [command]

    def sync(self, lib: Library, opts: Any, args: list[str]) -> None:
        del args
        username = self.config["username"].as_str()
        cookie_path = self.config["cookies"].as_str()
        if not username or not cookie_path:
            raise UserError("bandcamp_sync requires username and cookies")
        cookies = load_cookies(cookie_path)
        releases = collection_items(username, cookies)
        albums = list(lib.albums())
        for release in releases:
            url = release["release_url"]
            album = bandcamp_album_for_id(url)
            if album is None:
                self._log.warning("could not read Bandcamp release {}", url)
                continue
            description = f"{album.artist} - {album.album}"
            if any(
                item.mb_albumid == url
                for existing in albums
                for item in existing.items()
            ):
                self._log.info("already imported: {}", description)
                continue
            if local_match(album, albums):
                self._log.info("already present: {}", description)
                continue
            self._log.info("missing: {}", description)
            if not opts.pretend:
                self.download_and_import(lib, cookies, release, url)

    def download_and_import(
        self,
        lib: Library,
        cookies: http.cookiejar.MozillaCookieJar,
        release: dict[str, Any],
        url: str,
    ) -> None:
        try:
            response = requests.get(
                release["redownload_url"],
                cookies=cookies,
                timeout=HTTP_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise UserError(f"could not request Bandcamp download for {url}") from exc

        data = pagedata(response.text)
        download_items = data.get("download_items")
        if (
            not isinstance(download_items, list)
            or not download_items
            or not isinstance(download_items[0], dict)
        ):
            raise UserError("Bandcamp page contained invalid download data")
        downloads = download_items[0].get("downloads")
        if not isinstance(downloads, dict):
            raise UserError("Bandcamp page contained invalid download data")
        format_name = self.config["format"].as_str()
        download = downloads.get(format_name)
        if not download:
            self._log.warning("format {} unavailable for {}", format_name, url)
            return
        if not isinstance(download, dict) or not isinstance(download.get("url"), str):
            raise UserError("Bandcamp page contained invalid download data")

        with tempfile.TemporaryDirectory(prefix="beets-bandcamp-") as temporary:
            archive = Path(temporary) / "release.zip"
            try:
                with requests.get(
                    download["url"],
                    cookies=cookies,
                    timeout=HTTP_TIMEOUT,
                    stream=True,
                ) as result:
                    result.raise_for_status()
                    with archive.open("wb") as output:
                        for chunk in result.iter_content(DOWNLOAD_CHUNK_SIZE):
                            output.write(chunk)
            except requests.RequestException as exc:
                raise UserError(f"could not download Bandcamp release {url}") from exc
            extracted = Path(temporary) / "music"
            extracted.mkdir()
            safe_extract(archive, extracted)
            self._import(lib, os.fspath(extracted), url)

    @staticmethod
    def _import(lib: Library, path: str, release_url: str) -> None:
        try:
            from beets.ui.commands.import_ import import_files
        except ModuleNotFoundError as exc:
            if exc.name != "beets.ui.commands.import_":
                raise
            from beets.ui.commands import import_files

        previous = {
            "search_ids": config["import"]["search_ids"].get(list),
            "copy": config["import"]["copy"].get(bool),
            "move": config["import"]["move"].get(bool),
            "resume": config["import"]["resume"].get(),
        }
        try:
            config["import"]["search_ids"] = [release_url]
            config["import"]["copy"] = True
            config["import"]["move"] = False
            config["import"]["resume"] = False
            import_files(lib, [os.fsencode(path)], None)
        finally:
            for key, value in previous.items():
                config["import"][key] = value
