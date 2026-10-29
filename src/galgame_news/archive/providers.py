"""Read-only metadata providers for configured task folders and Nextcloud."""

from __future__ import annotations

import json
import hashlib
import ipaddress
import re
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import httpx

from .index import normalize_issue_number
from .models import ArchiveAsset


Cancelled = Callable[[], bool] | threading.Event | None


def _cancelled(cancelled: Cancelled) -> bool:
    if cancelled is None:
        return False
    if isinstance(cancelled, threading.Event):
        return cancelled.is_set()
    return bool(cancelled())


def _check_cancel(cancelled: Cancelled) -> None:
    if _cancelled(cancelled):
        raise InterruptedError("archive operation cancelled")


def _date_suspect(issue_date: str | None, now: datetime, _days: int) -> bool:
    if not issue_date:
        return True
    try:
        parsed = date.fromisoformat(issue_date[:10])
    except (TypeError, ValueError):
        return True
    return parsed > now.date()


def _issue_date_from_path(path: str) -> str | None:
    for segment in reversed(path.split("/")):
        period = re.search(r"(?<!\d)(20\d{6})[-_](?:20\d{6}|\d{4})(?!\d)", segment)
        if period:
            try:
                return datetime.strptime(period.group(1), "%Y%m%d").date().isoformat()
            except ValueError:
                continue
        explicit = re.search(r"(?<!\d)(20\d{2})[./_-](\d{1,2})[./_-](\d{1,2})(?!\d)", segment)
        if explicit:
            try:
                return date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3))).isoformat()
            except ValueError:
                continue
    return None


def _archive_issue_number_from_segment(segment: str) -> str:
    issue = normalize_issue_number(segment)
    if issue:
        return issue
    issue_text = re.sub(r"^\s*20\d{2}[./_-][^\s]+\s+", "", segment)
    match = re.fullmatch(
        r"\s*(\d{2,4})\s*(?:期\s*)?(?:周边|新作|汉化(?:\s*[+＋]\s*周边)?)(?:后.*|\d{1,2}[-./]\d{1,2})?\s*",
        issue_text,
    )
    return str(int(match.group(1))) if match else ""


_GENERIC_ARCHIVE_FOLDERS = {
    "图包", "图片", "图片包", "视频", "视频包", "周边", "新作", "汉化", "媒体", "素材",
    "images", "image", "pictures", "picture", "videos", "video", "media", "assets",
    "weeklyintro", "weeklyoutro", "视频成品及其工程",
}
_MEDIA_SUBFOLDERS = {"图包", "图片", "图片包", "视频", "视频包", "images", "image", "pictures", "picture", "videos", "video", "media"}


def _game_names_from_folder(folder: str) -> tuple[str, ...]:
    quoted = []
    for pattern in (r"《([^《》]+)》", r"「([^「」]+)」"):
        quoted.extend(match.strip() for match in re.findall(pattern, folder) if match.strip())
    if quoted:
        return tuple(dict.fromkeys(quoted))
    match = re.match(r"^\d+[_\-\s]+(.+)$", folder)
    name = match.group(1).strip() if match else folder.strip()
    return (name,) if name else ()


def _archive_game_assignment(folder_segments: list[str]) -> tuple[tuple[str, ...], str]:
    # Explicitly quoted game titles are reliable even when nested beneath media folders.
    marked = [(segment, _game_names_from_folder(segment)) for segment in folder_segments]
    explicit = [(segment, names) for segment, names in marked if re.search(r"[《「][^》」]+[》」]", segment)]
    if explicit:
        names = tuple(dict.fromkeys(name for _segment, values in explicit for name in values))
        return names, " / ".join(segment for segment, _values in explicit)

    if len(folder_segments) == 1:
        segment = folder_segments[0]
        names = _game_names_from_folder(segment)
        if names and names[0].casefold() not in _GENERIC_ARCHIVE_FOLDERS:
            return names, segment

    # A plain named work folder is accepted only when the next level explicitly
    # identifies itself as an image/video directory. Other arbitrary ancestors
    # (for example issue categories or project folders) are not game evidence.
    if len(folder_segments) >= 2 and folder_segments[-1].casefold() in _MEDIA_SUBFOLDERS:
        segment = folder_segments[-2]
        names = _game_names_from_folder(segment)
        if names and names[0].casefold() not in _GENERIC_ARCHIVE_FOLDERS:
            return names, segment
    return (), ""


def _local_asset(root: Path, source_id: str, item: dict, kind: str, index_file: Path) -> ArchiveAsset | None:
    relative = item.get("local_path") or item.get("path") or item.get("file") or item.get("filename")
    if not relative:
        return None
    path = Path(str(relative))
    candidates = (path,) if path.is_absolute() else (root / path, index_file.parent / path)
    resolved = None
    for candidate in candidates:
        try:
            checked = candidate.resolve(strict=True)
            checked.relative_to(root.resolve())
            resolved = checked
            break
        except (OSError, ValueError):
            continue
    if resolved is None:
        return None
    if not resolved.is_file():
        return None
    issue = str(item.get("issue_number") or item.get("issue") or item.get("weekly_issue") or item.get("_index_issue") or "")
    issue = normalize_issue_number(issue)
    issue_date = item.get("issue_date") or item.get("date") or item.get("published_at")
    games = (item.get("game_names") or item.get("games") or item.get("game")
             or item.get("_news_game_names") or item.get("_news_title") or ())
    if isinstance(games, str):
        games = (games,)
    if not issue or not games:
        return None
    stat_size = resolved.stat().st_size
    name = str(item.get("name") or resolved.name)
    identity = str(item.get("asset_id") or resolved.as_posix())
    return ArchiveAsset(
        asset_id=f"{source_id}:{identity}", source_id=source_id, kind=kind,
        issue_number=normalize_issue_number(issue), path=resolved.as_posix(), name=name,
        game_names=tuple(str(value) for value in games), issue_date=str(issue_date)[:10] if issue_date else None,
        news_title=str(item.get("news_title") or item.get("title") or item.get("_news_title") or ""),
        revision=str(item.get("revision")) if item.get("revision") is not None else None,
        size=int(item.get("size") or stat_size), source_kind="local", local_path=str(resolved),
        original_source_url=item.get("source_url") or item.get("original_source_url"),
        preview_url=item.get("preview_url"), version=item.get("version"),
        date_suspect=bool(item.get("date_suspect", False)), metadata=item,
    )


class LocalArchiveProvider:
    """Indexes only the explicitly configured task directories and their index files."""

    def __init__(self, roots: Iterable[str | Path], source_id: str = "local"):
        self.roots = tuple(Path(root).resolve() for root in roots)
        self.source_id = source_id

    def scan(self, days: int = 365, now: datetime | None = None, cancelled: Cancelled = None) -> list[ArchiveAsset]:
        now = now or datetime.now(timezone.utc)
        assets: list[ArchiveAsset] = []
        for root in self.roots:
            _check_cancel(cancelled)
            if not root.is_dir():
                continue
            from galgame_news.tasks.repository import TaskRepository
            from galgame_news.media_sync import reconcile_task
            reconcile_task(root, phase="archive", cancelled=lambda: _cancelled(cancelled), timeout=60)

            if TaskRepository.is_authoritative(root):
                # Reconcile explicitly configured task media before exporting the
                # repository view, so manually added review files are searchable.
                image_payload, video_payload, _, _ = TaskRepository(root, create=False).export_review_payloads()
                for payload, kind in ((image_payload, "image"), (video_payload, "video")):
                    assets.extend(self._assets_from_index_payload(root, self.source_id, payload, kind, root / "task_state.sqlite3", days, now))
                continue
            # These are the known index locations; never recursively search a task tree.
            for filename, kind in (("image_index.json", "image"), ("video_index.json", "video")):
                candidates = (root / filename, root / "raw" / filename)
                for index_file in candidates:
                    _check_cancel(cancelled)
                    if not index_file.is_file():
                        continue
                    try:
                        payload = json.loads(index_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError, UnicodeError):
                        continue
                    assets.extend(self._assets_from_index_payload(root, self.source_id, payload, kind, index_file, days, now))
            overlay_file = root / "media_sync_overlay.json"
            if overlay_file.is_file():
                try:
                    payload = json.loads(overlay_file.read_text(encoding="utf-8"))
                    assets.extend(self._assets_from_index_payload(root, self.source_id, payload, "image", overlay_file, days, now))
                except (OSError, ValueError, UnicodeError):
                    pass
        by_id = {asset.asset_id: asset for asset in assets}
        return list(by_id.values())

    @staticmethod
    def _assets_from_index_payload(root: Path, source_id: str, payload, kind: str, index_file: Path, days: int, now: datetime) -> list[ArchiveAsset]:
        if isinstance(payload, list):
            records = payload
            issue_id = None
            news_titles = {}
            news_contexts = {}
        else:
            records = payload.get("assets", payload.get("items", []))
            if not records:
                records = payload.get("candidates" if kind == "image" else "videos", [])
            issue_id = payload.get("issue_id") or payload.get("issue_number")
            news_contexts = {
                str(entry.get("news_id")): entry
                for entry in payload.get("news_items", [])
                if isinstance(entry, dict) and entry.get("news_id") is not None
            }
            news_titles = {key: str(value.get("title") or "") for key, value in news_contexts.items()}
        result = []
        for entry in records if isinstance(records, list) else ():
            if not isinstance(entry, dict):
                continue
            news = news_contexts.get(str(entry.get("news_id")), {})
            item = {
                **entry,
                "_index_issue": issue_id,
                "_news_title": news_titles.get(str(entry.get("news_id")), ""),
                "_news_game_names": news.get("game_names") or news.get("games") or news.get("game") or (),
            }
            asset = _local_asset(root, source_id, item, kind, index_file)
            if asset:
                suspect = asset.date_suspect or _date_suspect(asset.issue_date, now, days)
                result.append(ArchiveAsset.from_dict({**asset.to_dict(), "date_suspect": suspect}))
        return result

    def fetch(self, asset: ArchiveAsset, target: str | Path, max_bytes: int = 100 * 1024 * 1024, cancelled: Cancelled = None) -> Path:
        return self._copy_local(asset, target, max_bytes, cancelled)

    def preview(self, asset: ArchiveAsset, target: str | Path, max_bytes: int = 20 * 1024 * 1024, cancelled: Cancelled = None) -> Path:
        preview = asset.metadata.get("local_preview_path") if asset.metadata else None
        return self._copy_local(asset, target, max_bytes, cancelled, Path(preview) if preview else None)

    def test_connection(self) -> bool:
        return all(root.is_dir() for root in self.roots)

    def _copy_local(self, asset: ArchiveAsset, target: str | Path, maximum: int, cancelled: Cancelled, source_override: Path | None = None) -> Path:
        source = source_override or Path(asset.local_path or asset.path)
        if source.is_absolute():
            resolved = source.resolve(strict=True)
        else:
            resolved = next((candidate.resolve(strict=True) for root in self.roots for candidate in (root / source,) if candidate.exists()), None)
            if resolved is None:
                raise FileNotFoundError(source)
        if not any(resolved.is_relative_to(root) for root in self.roots):
            raise ValueError("local asset is outside configured roots")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return self._copy_path(resolved, target, maximum, cancelled)

    @staticmethod
    def _copy_path(source: Path, target: str | Path, maximum: int, cancelled: Cancelled) -> Path:
        source_size = source.stat().st_size
        if source_size > maximum:
            raise ValueError("asset exceeds byte limit")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with source.open("rb") as src, target.open("wb") as dst:
            while True:
                _check_cancel(cancelled)
                chunk = src.read(min(1024 * 1024, maximum - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > maximum:
                    target.unlink(missing_ok=True)
                    raise ValueError("asset exceeds byte limit")
                dst.write(chunk)
        return target


class NextcloudArchiveProvider:
    """Nextcloud WebDAV reader restricted to a configured host and root path."""

    _PROPS = b"""<?xml version='1.0' encoding='utf-8'?>
    <d:propfind xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns' xmlns:nc='http://nextcloud.org/ns'>
      <d:prop><d:resourcetype/><d:getetag/><d:getcontentlength/><d:getlastmodified/>
      <d:getcontenttype/><oc:fileid/><nc:has-preview/><oc:has-preview/></d:prop>
    </d:propfind>"""

    def __init__(self, server_url: str, username: str, password: str, root_path: str, transport=None, cache_dir: str | Path | None = None):
        parsed = urllib.parse.urlsplit(server_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("server_url must be an absolute HTTP(S) origin without embedded credentials")
        if parsed.scheme != "https":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname.casefold() == "localhost"
            if not loopback:
                raise ValueError("Nextcloud credentials require HTTPS except on a loopback development server")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("server_url must contain only the origin")
        self.server_url = f"{parsed.scheme}://{parsed.netloc}"
        self.scheme = parsed.scheme
        self.host = parsed.netloc.casefold()
        self.username, self.password = username, password
        self.root_path = "/" + "/".join(part for part in urllib.parse.unquote(root_path).split("/") if part)
        self._owns_transport = transport is None
        self.transport = transport or httpx.Client(auth=(username, password), timeout=30.0, follow_redirects=False)
        self.canonical_user_id = None if "@" in username else username
        self._dav_root_path = ""
        self._dav_root = ""
        self._set_dav_root(self.canonical_user_id) if self.canonical_user_id else None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
    def _set_dav_root(self, user_id: str) -> None:
        self.canonical_user_id = user_id
        self._dav_root_path = f"/remote.php/dav/files/{user_id}" + self.root_path
        self._dav_root = urllib.parse.quote(self._dav_root_path, safe="/")

    def _ensure_dav_root(self) -> None:
        if self._dav_root_path:
            return
        endpoint = self.server_url + "/ocs/v2.php/cloud/user?format=json"
        response = self._request("GET", endpoint, headers={"OCS-APIRequest": "true", "Accept": "application/json"})
        try:
            payload = json.loads(response.content)
            meta = payload["ocs"]["meta"]
            user_id = payload["ocs"]["data"]["id"]
            if int(meta.get("statuscode", 0)) not in {100, 200} or not isinstance(user_id, str) or not user_id.strip():
                raise ValueError("OCS response did not contain a canonical user id")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("Could not resolve the Nextcloud account id; configure the canonical userid instead of an email address") from exc
        self._set_dav_root(user_id.strip())

    def _canonical_scoped_url(self, value: str, *, allow_query: bool = True) -> str:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme != self.scheme or parsed.netloc.casefold() != self.host:
                raise ValueError("refusing a different host or HTTPS downgrade")
            raw_path = parsed.path
        else:
            raw_path = value
        decoded = urllib.parse.unquote(raw_path)
        if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
            raise ValueError("WebDAV path contains a traversal segment")
        root = self._dav_root_path.rstrip("/")
        if not (decoded == root or decoded.startswith(root + "/")):
            raise ValueError("WebDAV path escapes the configured host or root")
        url = self.server_url + urllib.parse.quote(decoded, safe="/")
        if allow_query and parsed.query:
            url += "?" + parsed.query
        if parsed.fragment:
            raise ValueError("URL fragments are not allowed")
        return url

    def _url_for(self, path: str) -> str:
        self._ensure_dav_root()
        if urllib.parse.urlsplit(path).scheme:
            absolute = path
        else:
            dav_path = path if path.startswith("/") else "/" + path
            absolute = self.server_url + urllib.parse.quote(dav_path, safe="/%")
        return self._canonical_scoped_url(absolute)

    def _request(self, method: str, url: str, **kwargs):
        parsed = urllib.parse.urlsplit(url)
        if parsed.netloc.casefold() != self.host or parsed.scheme != self.scheme:
            raise ValueError("refusing a different host or HTTPS downgrade")
        if parsed.path.startswith("/remote.php/dav/files/"):
            self._canonical_scoped_url(url)
        response = self.transport.request(method, url, follow_redirects=False, **kwargs)
        location = response.headers.get("Location")
        if 300 <= response.status_code < 400:
            if location:
                target = urllib.parse.urlsplit(urllib.parse.urljoin(url, location))
                if target.netloc.casefold() != self.host or target.scheme != self.scheme:
                    raise ValueError("refusing a different-host or HTTPS-downgrade redirect")
            if response.status_code == 304 and method.upper() == "PROPFIND":
                return response
            raise RuntimeError(f"unexpected redirect response {response.status_code}")
        return response

    def scan(self, days: int = 365, now: datetime | None = None, cancelled: Cancelled = None) -> list[ArchiveAsset]:
        self._ensure_dav_root()
        now = now or datetime.now(timezone.utc)
        queue = [self._dav_root_path]
        root_path = self._dav_root_path.rstrip("/")
        root_is_issue_subtree = any(
            _archive_issue_number_from_segment(part)
            for part in self.root_path.split("/")
            if part
        )
        visited: set[str] = set()
        records: list[ArchiveAsset] = []
        while queue:
            _check_cancel(cancelled)
            path = queue.pop(0)
            if path in visited:
                continue
            visited.add(path)
            url = self._url_for(path)
            cache_file = self._cache_path(path)
            cached_xml = None
            if cache_file and cache_file.is_file():
                try:
                    cached_xml = json.loads(cache_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    cached_xml = None
            headers = {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}
            if cached_xml and cached_xml.get("etag"):
                headers["If-None-Match"] = cached_xml["etag"]
            response = self._request("PROPFIND", url, headers=headers, content=self._PROPS)
            if response.status_code == 304:
                if not cached_xml:
                    raise RuntimeError("Nextcloud returned 304 without cached directory metadata")
                body = cached_xml["body"].encode()
            elif response.status_code in {200, 207}:
                body = response.content
            else:
                raise RuntimeError(f"Nextcloud PROPFIND failed: {response.status_code}")
            root = ET.fromstring(body)
            if cache_file and response.status_code != 304:
                etag = response.headers.get("ETag") or self._directory_etag(root, path)
                cache_file.write_text(json.dumps({"etag": etag, "body": body.decode("utf-8")}, ensure_ascii=False), encoding="utf-8")
            root_collections: list[str] = []
            for item in root.findall("{DAV:}response"):
                _check_cancel(cancelled)
                href = item.findtext("{DAV:}href")
                if not href:
                    continue
                absolute = urllib.parse.urljoin(self.server_url, href)
                try:
                    canonical_url = self._canonical_scoped_url(absolute)
                except ValueError:
                    continue
                child_path = urllib.parse.unquote(urllib.parse.urlsplit(canonical_url).path)
                prop = item.find("{DAV:}propstat/{DAV:}prop")
                if prop is None:
                    continue
                if prop.find("{DAV:}resourcetype/{DAV:}collection") is not None:
                    if child_path.rstrip("/") != path.rstrip("/"):
                        if path.rstrip("/") == root_path and not root_is_issue_subtree:
                            root_collections.append(child_path)
                        else:
                            queue.append(child_path)
                    continue
                asset = self._asset_from_prop(child_path, prop, days, now)
                if asset:
                    records.append(asset)
            if path.rstrip("/") == root_path and not root_is_issue_subtree:
                issue_collections = [
                    child for child in root_collections
                    if _archive_issue_number_from_segment(urllib.parse.unquote(child.rstrip("/").rsplit("/", 1)[-1]))
                ]
                queue.extend(issue_collections or root_collections)
        return records

    def _directory_etag(self, root: ET.Element, path: str) -> str | None:
        for item in root.findall("{DAV:}response"):
            href = item.findtext("{DAV:}href")
            if not href:
                continue
            absolute = urllib.parse.urljoin(self.server_url, href)
            try:
                canonical_url = self._canonical_scoped_url(absolute)
            except ValueError:
                continue
            if urllib.parse.unquote(urllib.parse.urlsplit(canonical_url).path).rstrip("/") != path.rstrip("/"):
                continue
            return item.findtext("{DAV:}propstat/{DAV:}prop/{DAV:}getetag")
        return None

    def _cache_path(self, path: str) -> Path | None:
        if not self.cache_dir:
            return None
        self._ensure_dav_root()
        context = "\0".join((self.host.casefold(), self.canonical_user_id or "", self.root_path, path))
        return self.cache_dir / (hashlib.sha256(context.encode("utf-8")).hexdigest() + ".json")

    def _asset_from_prop(self, path: str, prop: ET.Element, days: int, now: datetime) -> ArchiveAsset | None:
        name = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
        suffix = Path(name).suffix.casefold()
        kind = "image" if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"} else "video" if suffix in {".mp4", ".mkv", ".webm", ".mov"} else None
        if not kind:
            return None
        segments = [urllib.parse.unquote(part) for part in path.split("/") if part]
        issue_locations = [(index, _archive_issue_number_from_segment(part)) for index, part in enumerate(segments[:-1])]
        issue_locations = [(index, issue_number) for index, issue_number in issue_locations if issue_number]
        if not issue_locations:
            return None
        issue_index, issue = issue_locations[-1]
        if not issue:
            return None
        last_modified = prop.findtext("{DAV:}getlastmodified")
        issue_date = _issue_date_from_path(path)
        folder_segments = segments[issue_index + 1:-1]
        games, game_folder = _archive_game_assignment(folder_segments)
        if not games or any(game.casefold() in {"weeklyintro", "weeklyoutro"} for game in games):
            return None
        fileid = prop.findtext("{http://owncloud.org/ns}fileid") or path
        identity_context = "\0".join((self.host.casefold(), self.canonical_user_id or self.username, self.root_path))
        server_scope_id = hashlib.sha256(identity_context.encode("utf-8")).hexdigest()[:20]
        etag = prop.findtext("{DAV:}getetag")
        content_length = prop.findtext("{DAV:}getcontentlength")
        preview = prop.findtext("{http://nextcloud.org/ns}has-preview") or prop.findtext("{http://owncloud.org/ns}has-preview")
        original = self._url_for(path)
        preview_available = str(preview).casefold() in {"1", "true"}
        return ArchiveAsset(
            asset_id=f"nextcloud:{server_scope_id}:{fileid}", source_id="nextcloud", kind=kind, issue_number=issue,
            path=path, name=name, game_names=games, issue_date=issue_date, news_title=game_folder, revision=etag,
            size=int(content_length) if content_length and content_length.isdigit() else None,
            source_kind="nextcloud", original_url=original, original_source_url=None,
            preview_url=self._preview_endpoint(fileid) if preview_available else None,
            date_suspect=_date_suspect(issue_date, now, days), metadata={"fileid": fileid, "etag": etag, "has_preview": preview, "last_modified": last_modified},
        )

    def fetch(self, asset: ArchiveAsset, target: str | Path, max_bytes: int = 100 * 1024 * 1024, cancelled: Cancelled = None) -> Path:
        self._ensure_dav_root()
        original_url = self._canonical_scoped_url(asset.original_url or "")
        return self._download(original_url, target, max_bytes, cancelled)

    def preview(self, asset: ArchiveAsset, target: str | Path, max_bytes: int = 20 * 1024 * 1024, cancelled: Cancelled = None) -> Path | None:
        self._ensure_dav_root()
        self._canonical_scoped_url(asset.original_url or "")
        has_preview = str((asset.metadata or {}).get("has_preview", "")).casefold() in {"1", "true"}
        fileid = (asset.metadata or {}).get("fileid")
        if not has_preview or not fileid:
            return None
        return self._download(self._preview_endpoint(str(fileid)), target, max_bytes, cancelled)

    def _preview_endpoint(self, fileid: str) -> str:
        query = urllib.parse.urlencode({"fileId": fileid, "x": 512, "y": 512, "a": 1, "forceIcon": 0})
        return self.server_url + "/index.php/core/preview?" + query

    def _download(self, url: str | None, target: str | Path, maximum: int, cancelled: Cancelled) -> Path:
        if not url:
            raise ValueError("asset has no Nextcloud URL")
        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.netloc.casefold() != self.host or parsed_url.scheme != self.scheme:
            raise ValueError("refusing a different host or HTTPS downgrade")
        if hasattr(self.transport, "stream"):
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.transport.stream("GET", url, headers={"Accept": "application/octet-stream"}, follow_redirects=False) as response:
                    self._check_stream_redirect(response, url)
                    self._check_download_response(response, maximum)
                    self._write_chunks(response.iter_bytes(1024 * 1024), target, maximum, cancelled)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            return target
        response = self._request("GET", url, headers={"Accept": "application/octet-stream"})
        if response.status_code != 200:
            raise RuntimeError(f"Nextcloud GET failed: {response.status_code}")
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > maximum:
            raise ValueError("asset exceeds byte limit")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            chunks = response.iter_bytes(1024 * 1024) if hasattr(response, "iter_bytes") else (response.content,)
            self._write_chunks(chunks, target, maximum, cancelled)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    @staticmethod
    def _check_download_response(response, maximum: int) -> None:
        if 300 <= response.status_code < 400:
            raise ValueError("refusing redirected Nextcloud download")
        if response.status_code != 200:
            raise RuntimeError(f"Nextcloud GET failed: {response.status_code}")
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > maximum:
            raise ValueError("asset exceeds byte limit")

    @staticmethod
    def _write_chunks(chunks, target: Path, maximum: int, cancelled: Cancelled) -> int:
        written = 0
        with target.open("wb") as stream:
            for chunk in chunks:
                _check_cancel(cancelled)
                written += len(chunk)
                if written > maximum:
                    raise ValueError("asset exceeds byte limit")
                stream.write(chunk)
        return written

    def test_connection(self) -> bool:
        self._ensure_dav_root()
        response = self._request("PROPFIND", self._url_for(self._dav_root_path), headers={"Depth": "0", "Content-Type": "application/xml"}, content=self._PROPS)
        return response.status_code in {200, 207}

    @staticmethod
    def _check_stream_redirect(response, url: str) -> None:
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            if location:
                target = urllib.parse.urlsplit(urllib.parse.urljoin(url, location))
                source = urllib.parse.urlsplit(url)
                if target.netloc.casefold() != source.netloc.casefold() or target.scheme != source.scheme:
                    raise ValueError("refusing a different-host or HTTPS-downgrade redirect")
            raise RuntimeError(f"unexpected redirect response {response.status_code}")

    def close(self) -> None:
        if self._owns_transport:
            self.transport.close()
