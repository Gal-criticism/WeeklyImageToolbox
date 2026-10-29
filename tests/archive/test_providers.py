from datetime import datetime, timezone
import urllib.parse

import pytest

from galgame_news.archive.models import ArchiveAsset
from galgame_news.archive.index import ArchiveIndex
from galgame_news.archive.providers import LocalArchiveProvider, NextcloudArchiveProvider


def test_remote_nextcloud_credentials_require_https():
    with pytest.raises(ValueError, match="HTTPS"):
        NextcloudArchiveProvider("http://cloud.example", "user", "secret", "/archive")


def test_loopback_http_remains_available_for_local_fixture_servers(tmp_path):
    transport = FakeTransport(Response())
    instance = NextcloudArchiveProvider("http://127.0.0.1:8080", "user", "secret", "/archive", transport=transport)
    assert instance.server_url == "http://127.0.0.1:8080"


def test_local_provider_only_reads_configured_task_roots(tmp_path):
    root = tmp_path / "task"
    media = root / "media" / "image.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"img")
    (root / "image_index.json").write_text(
        '{"assets": [{"path": "media/image.jpg", "game_names": ["Game"], "issue_number": "226", "issue_date": "2026-10-01"}]}',
        encoding="utf-8",
    )
    outside = tmp_path / "unconfigured" / "secret.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"secret")
    provider = LocalArchiveProvider([root])
    found = provider.scan(now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert len(found) == 1
    assert root in __import__("pathlib").Path(found[0].local_path).parents


def test_local_provider_reads_existing_weekly_image_index_schema(tmp_path):
    root = tmp_path / "task"
    media = root / "images" / "x1" / "Game.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")
    (root / "image_index.json").write_text(
        '{"issue_id":"226","news_items":[{"news_id":"n1","title":"Game"}],'
        '"candidates":[{"id":"c1","news_id":"n1","image_url":"https://img.example/a.jpg",'
        '"local_path":"images/x1/Game.jpg","source_url":"https://news.example/game"}]}',
        encoding="utf-8",
    )
    found = LocalArchiveProvider([root]).scan()
    assert len(found) == 1
    assert found[0].issue_number == "226"
    assert found[0].game_names == ("Game",)
    assert found[0].original_source_url == "https://news.example/game"


def test_local_provider_joins_news_game_names_from_news_id(tmp_path):
    root = tmp_path / "task"
    media = root / "images" / "x1" / "asset.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")
    (root / "image_index.json").write_text(
        '{"issue_id":"226","news_items":[{"news_id":"n1","title":"《Game》官网更新",'
        '"game_names":["Game"]}],"candidates":[{"id":"c1","news_id":"n1",'
        '"local_path":"images/x1/asset.jpg"}]}', encoding="utf-8",
    )

    found = LocalArchiveProvider([root]).scan()

    assert len(found) == 1
    assert found[0].game_names == ("Game",)
    assert found[0].news_title == "《Game》官网更新"
    assert found[0].metadata["news_id"] == "n1"
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", found)
    assert [asset.asset_id for asset in index.search(("Game",), "227", days=None)] == [found[0].asset_id]
    index.close()


def test_local_provider_preserves_ambiguous_or_missing_news_identity(tmp_path):
    root = tmp_path / "task"
    for name in ("multi.jpg", "unknown.jpg"):
        path = root / "images" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
    (root / "image_index.json").write_text(
        '{"issue_id":"226","news_items":[{"news_id":"multi","title":"A × B",'
        '"game_names":["A","B"]},{"news_id":"unknown"}],"candidates":['
        '{"id":"m","news_id":"multi","local_path":"images/multi.jpg"},'
        '{"id":"u","news_id":"unknown","local_path":"images/unknown.jpg"}]}', encoding="utf-8",
    )

    found = LocalArchiveProvider([root]).scan()

    assert len(found) == 1
    assert found[0].game_names == ("A", "B")
    assert found[0].news_title == "A × B"
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", found)
    assert index.search(("A",), "227", days=None) == []
    index.close()


def test_local_provider_refuses_to_fetch_paths_outside_configured_roots(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    provider = LocalArchiveProvider([root])
    asset = ArchiveAsset("a", "local", "image", "226", str(outside), "private.txt", local_path=str(outside))
    with pytest.raises(ValueError):
        provider.fetch(asset, tmp_path / "copy.txt")


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class Response:
    status_code = 207
    headers = {"ETag": '"dir-v1"'}
    content = b"""<?xml version='1.0'?><d:multistatus xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'>
      <d:response><d:href>/remote.php/dav/files/user/archive/</d:href><d:propstat><d:prop>
        <d:resourcetype><d:collection/></d:resourcetype><oc:fileid>dir1</oc:fileid><d:getetag>dir-v1</d:getetag>
      </d:prop></d:propstat></d:response>
      <d:response><d:href>/remote.php/dav/files/user/archive/226%20%E5%91%A8%E6%8A%A5/</d:href><d:propstat><d:prop>
        <d:resourcetype><d:collection/></d:resourcetype><oc:fileid>dir2</oc:fileid><d:getetag>dir2-v1</d:getetag>
      </d:prop></d:propstat></d:response>
    </d:multistatus>"""


def provider(tmp_path, transport):
    return NextcloudArchiveProvider("https://cloud.example", "user", "secret", "/archive", transport=transport, cache_dir=tmp_path)


def test_nextcloud_propfind_depth_one_requests_metadata_only(tmp_path):
    transport = FakeTransport(Response())
    p = provider(tmp_path, transport)
    p.scan(now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    method, url, kwargs = transport.calls[0]
    assert method == "PROPFIND"
    assert kwargs["headers"]["Depth"] == "1"
    assert "<oc:fileid/>" in kwargs["content"].decode()
    assert "<d:getetag/>" in kwargs["content"].decode()
    assert "<nc:has-preview/>" in kwargs["content"].decode()


@pytest.mark.parametrize(("filename", "expected_kind"), [("1.png", "image"), ("1.mp4", "video")])
def test_nextcloud_nested_media_uses_explicit_news_folder_as_game_and_keeps_source_unknown(
    tmp_path, filename, expected_kind
):
    import xml.etree.ElementTree as ET

    p = provider(tmp_path, FakeTransport(Response()))
    prop = ET.fromstring("<d:prop xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'><oc:fileid>f1</oc:fileid></d:prop>")
    path = p._dav_root_path + "/226期/《A》新闻/图片/" + filename

    asset = p._asset_from_prop(path, prop, 365, datetime(2026, 10, 27, tzinfo=timezone.utc))

    assert asset is not None
    assert asset.kind == expected_kind
    assert asset.game_names == ("A",)
    assert asset.news_title == "《A》新闻"
    assert asset.original_source_url is None
    assert asset.original_url.startswith("https://cloud.example/")


def test_nextcloud_skips_unproved_generic_and_multi_game_folders(tmp_path):
    import xml.etree.ElementTree as ET

    p = provider(tmp_path, FakeTransport(Response()))
    prop = ET.fromstring("<d:prop xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'><oc:fileid>f1</oc:fileid></d:prop>")
    base = p._dav_root_path + "/226期/"

    assert p._asset_from_prop(base + "图包/1.png", prop, 365, datetime(2026, 10, 27, tzinfo=timezone.utc)) is None
    ambiguous = p._asset_from_prop(
        base + "《A》×《B》联合新闻/图片/1.png", prop, 365, datetime(2026, 10, 27, tzinfo=timezone.utc)
    )
    assert ambiguous is not None
    assert ambiguous.game_names == ("A", "B")
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("nextcloud", [ambiguous])
    assert index.search(("A",), "227", days=None) == []
    index.close()

    nested_plain = p._asset_from_prop(
        base + "unlabeled-parent/random-folder/1.png", prop, 365, datetime(2026, 10, 27, tzinfo=timezone.utc)
    )
    assert nested_plain is None


def test_nextcloud_scan_handles_encoded_chinese_root_and_game_path(tmp_path):
    class EncodedResponse:
        status_code = 207
        headers = {"ETag": '"root"'}
        content = b"""<d:multistatus xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns' xmlns:nc='http://nextcloud.org/ns'>
          <d:response><d:href>/remote.php/dav/files/user/%E5%BE%80%E6%9C%9F%E5%91%A8%E6%8A%A5/256%E6%9C%9F/%E5%9B%BE%E5%8C%85/%E6%96%B0%E4%BD%9C/7_%E3%80%8A%E7%8E%8B%E6%A7%98%E6%81%8B%E6%84%9B%E3%80%8B%E5%AE%98%E7%BD%91%E6%9B%B4%E6%96%B0/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype><d:getetag>e</d:getetag></d:prop></d:propstat></d:response>
          <d:response><d:href>/remote.php/dav/files/user/%E5%BE%80%E6%9C%9F%E5%91%A8%E6%8A%A5/256%E6%9C%9F/%E5%9B%BE%E5%8C%85/%E6%96%B0%E4%BD%9C/7_%E3%80%8A%E7%8E%8B%E6%A7%98%E6%81%8B%E6%84%9B%E3%80%8B%E5%AE%98%E7%BD%91%E6%9B%B4%E6%96%B0/x7.02.png</d:href><d:propstat><d:prop><d:resourcetype/><d:getetag>f</d:getetag><d:getcontentlength>3</d:getcontentlength><d:getlastmodified>Tue, 15 Apr 2025 10:00:00 GMT</d:getlastmodified><oc:fileid>file-1</oc:fileid><nc:has-preview>1</nc:has-preview></d:prop></d:propstat></d:response>
        </d:multistatus>"""

    class EncodedTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            if method == "GET":
                class Download:
                    status_code = 200
                    headers = {"Content-Length": "4"}
                    content = b"data"
                self.calls.append((method, url, kwargs))
                return Download()
            return super().request(method, url, **kwargs)

    transport = EncodedTransport(EncodedResponse())
    p = NextcloudArchiveProvider("https://cloud.example", "user", "secret", "/往期周报/256期/图包/新作/7_《王様恋愛》官网更新", transport=transport)
    found = p.scan()
    assert len(found) == 1
    assert found[0].issue_number == "256"
    assert found[0].game_names == ("王様恋愛",)
    assert found[0].news_title == "7_《王様恋愛》官网更新"
    assert found[0].issue_date is None
    assert found[0].date_suspect is True
    assert "%E5%BE%80%E6%9C%9F%E5%91%A8%E6%8A%A5" in transport.calls[0][1]
    fetched = p.fetch(found[0], tmp_path / "image.png")
    assert fetched.read_bytes() == b"data"
    assert "%E7%8E%8B%E6%A7%98%E6%81%8B%E6%84%9B" in transport.calls[-1][1]


def test_nextcloud_scan_handles_hash_in_folder_name_without_allowing_url_fragments(tmp_path):
    issue_path = "/remote.php/dav/files/user/archive/223%E6%9C%9F/"
    encoded_game_name = "4.%E3%80%8AAMBITIOUS%20MISSION%E3%80%8B%E5%BC%80%E5%90%AF%E7%BA%BF%E4%B8%8A%E6%8A%BD%E5%A5%96%E6%B4%BB%E5%8A%A8%20%23%E5%89%A9%E6%96%97%E5%A3%AB%E6%98%9F%E6%98%9F%23"
    game_name = urllib.parse.unquote(encoded_game_name)
    game_href = issue_path + encoded_game_name + "/"
    file_href = game_href + "cg.jpg"

    def response(body):
        class FrozenDavResponse:
            status_code = 207
            headers = {}
            content = body.encode("utf-8")
        return FrozenDavResponse()

    class FrozenDavTransport:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            path = urllib.parse.urlsplit(url).path
            if path.endswith("/archive"):
                body = """<d:multistatus xmlns:d='DAV:'><d:response><d:href>/remote.php/dav/files/user/archive/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response><d:response><d:href>/remote.php/dav/files/user/archive/223%E6%9C%9F/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>"""
            elif path.endswith("/223%E6%9C%9F/"):
                body = f"""<d:multistatus xmlns:d='DAV:'><d:response><d:href>{issue_path}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response><d:response><d:href>{game_href}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>"""
            else:
                body = f"""<d:multistatus xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'><d:response><d:href>{game_href}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response><d:response><d:href>{file_href}</d:href><d:propstat><d:prop><d:resourcetype/><d:getetag>v1</d:getetag><d:getcontentlength>4</d:getcontentlength><oc:fileid>hash-folder-file</oc:fileid></d:prop></d:propstat></d:response></d:multistatus>"""
            return response(body)

    transport = FrozenDavTransport()
    p = provider(tmp_path, transport)
    found = p.scan()

    assert len(found) == 1
    assert found[0].game_names == ("AMBITIOUS MISSION",)
    assert found[0].path.endswith("/" + game_name + "/cg.jpg")
    assert "%23" in found[0].original_url
    with pytest.raises(ValueError, match="fragments"):
        p._url_for(found[0].original_url + "#external")


@pytest.mark.parametrize(
    ("section_name", "parent_segment", "expected_issue", "expected_date"),
    [
        (urllib.parse.unquote("223%E5%91%A8%E8%BE%B9"), "", "223", None),
        (urllib.parse.unquote("2026.2.21 231%E6%9C%9F%E6%96%B0%E4%BD%9C"), "", "231", "2026-02-21"),
        (urllib.parse.unquote("2026.3..7 231%E6%9C%9F%E6%96%B0%E4%BD%9C"), "", "231", None),
        (urllib.parse.unquote("223%E5%91%A8%E8%BE%B9"), "2026", "223", None),
    ],
)
def test_nextcloud_scan_extracts_labeled_issue_and_explicit_section_date(
    tmp_path, section_name, parent_segment, expected_issue, expected_date
):
    root_href = "/remote.php/dav/files/user/archive/"
    parent_href = root_href + parent_segment + "/" if parent_segment else root_href
    section_href = parent_href + urllib.parse.quote(section_name, safe="") + "/"
    game_name = urllib.parse.unquote("3.%E3%80%8AAMBITIOUS%20MISSION%E3%80%8B%E5%BC%80%E5%90%AF%E7%BA%BF%E4%B8%8A%E6%8A%BD%E5%A5%96%E6%B4%BB%E5%8A%A8%20%23%E5%89%A9%E6%96%97%E5%A3%AB%E6%98%9F%E6%98%9F%23")
    game_href = section_href + urllib.parse.quote(game_name, safe="") + "/"
    image_href = game_href + "1.png"

    def dav_response(*entries):
        body = "<d:multistatus xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'>"
        for href, collection, fileid in entries:
            resource = "<d:resourcetype><d:collection/></d:resourcetype>" if collection else "<d:resourcetype/>"
            file = f"<oc:fileid>{fileid}</oc:fileid>" if fileid else ""
            body += f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{resource}{file}</d:prop></d:propstat></d:response>"
        body += "</d:multistatus>"

        class FrozenDavResponse:
            status_code = 207
            headers = {}
            content = body.encode("utf-8")

        return FrozenDavResponse()

    class FrozenDavTransport:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            path = urllib.parse.urlsplit(url).path
            if path.endswith("/archive"):
                unrelated = (root_href + "TGGA2026/", True, None) if not parent_segment else None
                first_child_href = parent_href if parent_segment else section_href
                entries = [(root_href, True, None), (first_child_href, True, None)]
                if unrelated:
                    entries.append(unrelated)
                return dav_response(*entries)
            if parent_segment and path.endswith("/" + parent_segment + "/"):
                return dav_response((parent_href, True, None), (section_href, True, None))
            if path.endswith(urllib.parse.quote(section_name, safe="") + "/"):
                return dav_response((section_href, True, None), (game_href, True, None))
            return dav_response((game_href, True, None), (image_href, False, "file-1"))

    p = provider(tmp_path, FrozenDavTransport())
    found = p.scan(now=datetime(2026, 10, 27, tzinfo=timezone.utc))

    assert len(found) == 1
    assert found[0].issue_number == expected_issue
    assert found[0].issue_date == expected_date
    assert found[0].name == "1.png"
    assert found[0].game_names == ("AMBITIOUS MISSION",)
    assert not any("/TGGA2026/" in url for _, url, _ in p.transport.calls)


def test_archive_issue_segment_recognition_is_limited_to_labeled_sections():
    from galgame_news.archive.providers import _archive_issue_number_from_segment

    assert _archive_issue_number_from_segment(urllib.parse.unquote("223%E5%91%A8%E8%BE%B9")) == "223"
    assert _archive_issue_number_from_segment(urllib.parse.unquote("223%E6%96%B0%E4%BD%9C")) == "223"
    assert _archive_issue_number_from_segment(urllib.parse.unquote("2025.5.31 193%E6%9C%9F%E6%B1%89%E5%8C%96%2B%E5%91%A8%E8%BE%B9")) == "193"
    assert _archive_issue_number_from_segment(urllib.parse.unquote("241%E6%96%B0%E4%BD%9C7-17")) == "241"
    assert _archive_issue_number_from_segment(urllib.parse.unquote("222%E6%96%B0%E4%BD%9C%E5%90%8E4%287-10%29")) == "222"
    for name in ("TGGA2026", urllib.parse.unquote("261005%E4%B8%B4%E6%97%B6"), "MUV-LUV", urllib.parse.unquote("2024%E5%B9%B4%E6%8A%A5"), urllib.parse.unquote("7_%E3%80%8AGame%202%E3%80%8B")):
        assert _archive_issue_number_from_segment(name) == ""


def test_nextcloud_period_date_comes_from_explicit_weekly_path_not_mtime(tmp_path):
    import xml.etree.ElementTree as ET

    prop = ET.fromstring("""<d:prop xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'>
      <d:getlastmodified>Tue, 15 Apr 2025 10:00:00 GMT</d:getlastmodified><oc:fileid>f1</oc:fileid>
    </d:prop>""")
    p = provider(tmp_path, FakeTransport(Response()))
    asset = p._asset_from_prop(p._dav_root_path + "/20260406-0412/256%E6%9C%9F/Game/a.jpg", prop, 365, datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert asset.issue_date == "2026-04-06"
    assert asset.metadata["last_modified"] == "Tue, 15 Apr 2025 10:00:00 GMT"


def test_nextcloud_date_conflict_path_is_marked_suspect_without_year_rewrite(tmp_path):
    import xml.etree.ElementTree as ET

    prop = ET.fromstring("""<d:prop xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'><oc:fileid>f2</oc:fileid></d:prop>""")
    p = provider(tmp_path, FakeTransport(Response()))
    asset = p._asset_from_prop(p._dav_root_path + "/20250406-0412/256期/Game/a.jpg", prop, 365, datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert asset.issue_date == "2025-04-06"
    assert asset.date_suspect is False


def test_nextcloud_scope_rejects_parent_cross_host_and_downgrade(tmp_path):
    transport = FakeTransport(Response())
    p = provider(tmp_path, transport)
    with pytest.raises(ValueError):
        p._url_for("/remote.php/dav/files/user/archive/%2e%2e/secret.jpg")
    with pytest.raises(ValueError):
        p._url_for("https://evil.example/remote.php/dav/files/user/archive/a.jpg")
    class Downgrade:
        status_code = 302
        headers = {"Location": "http://cloud.example/remote.php/dav/files/user/archive/a.jpg"}
        content = b""
    p.transport = FakeTransport(Downgrade())
    asset = ArchiveAsset("a", "remote", "image", "226", "/archive/a.jpg", "a.jpg", source_kind="nextcloud", original_url="https://cloud.example/remote.php/dav/files/user/archive/a.jpg")
    with pytest.raises((ValueError, RuntimeError)):
        p.fetch(asset, tmp_path / "a.jpg")
    assert len(p.transport.calls) == 1


def test_email_username_resolves_canonical_nextcloud_user_id_lazily(tmp_path):
    class OCSResponse:
        status_code = 200
        headers = {}
        content = b'{"ocs":{"meta":{"status":"ok","statuscode":200},"data":{"id":"canonical-user"}}}'
    class OcsThenDav:
        def __init__(self):
            self.calls = []
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return OCSResponse() if "/ocs/" in url else Response()
    transport = OcsThenDav()
    p = NextcloudArchiveProvider("https://cloud.example", "person@example.org", "secret", "/archive", transport=transport)
    assert p.scan() == []
    assert transport.calls[0][0] == "GET"
    assert "/ocs/v2.php/cloud/user?format=json" in transport.calls[0][1]
    assert transport.calls[0][2]["headers"]["OCS-APIRequest"] == "true"
    assert "/files/canonical-user/archive" in transport.calls[1][1]


def test_request_allows_propfind_not_modified_for_cache_handling(tmp_path):
    class NotModified:
        status_code = 304
        headers = {"ETag": '"same"'}
        content = b""
    p = provider(tmp_path, FakeTransport(NotModified()))
    response = p._request("PROPFIND", p.server_url + p._dav_root)
    assert response.status_code == 304


def test_close_only_closes_provider_owned_transport(tmp_path, monkeypatch):
    class ClosableTransport(FakeTransport):
        closed = False
        def close(self):
            self.closed = True

    injected = ClosableTransport(Response())
    NextcloudArchiveProvider("https://cloud.example", "user", "secret", "/archive", transport=injected).close()
    assert injected.closed is False

    import galgame_news.archive.providers as providers_module
    owned = ClosableTransport(Response())
    monkeypatch.setattr(providers_module.httpx, "Client", lambda **kwargs: owned)
    providers_module.NextcloudArchiveProvider("https://cloud.example", "user", "secret", "/archive").close()
    assert owned.closed is True


def test_nextcloud_fetch_rejects_cross_host_redirect_before_credentials(tmp_path):
    class Redirect:
        status_code = 302
        headers = {"Location": "https://evil.example/steal"}
        content = b""

    transport = FakeTransport(Redirect())
    p = provider(tmp_path, transport)
    asset = ArchiveAsset("a", "remote", "image", "226", "/archive/a.jpg", "a.jpg", source_kind="nextcloud", original_url="https://cloud.example/remote.php/dav/files/user/archive/a.jpg")
    with pytest.raises((ValueError, RuntimeError)):
        p.fetch(asset, tmp_path / "a.jpg")
    assert len(transport.calls) == 1


def test_nextcloud_fetch_enforces_streaming_byte_limit(tmp_path):
    class Oversize:
        status_code = 200
        headers = {"Content-Length": "10"}
        content = b"0123456789"

    transport = FakeTransport(Oversize())
    p = provider(tmp_path, transport)
    asset = ArchiveAsset("a", "remote", "image", "226", "/archive/a.jpg", "a.jpg", source_kind="nextcloud", original_url="https://cloud.example/remote.php/dav/files/user/archive/a.jpg")
    with pytest.raises((ValueError, RuntimeError)):
        p.fetch(asset, tmp_path / "a.jpg", max_bytes=4)


def test_preview_returns_none_without_preview_support_and_does_not_fetch_original(tmp_path):
    transport = FakeTransport(Response())
    p = provider(tmp_path, transport)
    asset = ArchiveAsset("a", "remote", "image", "226", "/archive/a.jpg", "a.jpg", source_kind="nextcloud", original_url="https://cloud.example/remote.php/dav/files/user/archive/a.jpg", metadata={"fileid": "id-1", "has_preview": "false"})
    assert p.preview(asset, tmp_path / "preview.jpg") is None
    assert transport.calls == []


def test_preview_uses_scoped_core_preview_endpoint_and_preserves_query(tmp_path):
    class PreviewResponse:
        status_code = 200
        headers = {"Content-Length": "5"}
        content = b"thumb"
    transport = FakeTransport(PreviewResponse())
    p = provider(tmp_path, transport)
    asset = ArchiveAsset("a", "remote", "image", "226", "/archive/a.jpg", "a.jpg", source_kind="nextcloud", original_url="https://cloud.example/remote.php/dav/files/user/archive/a.jpg", metadata={"fileid": "id-1", "has_preview": "1"})
    result = p.preview(asset, tmp_path / "preview.jpg")
    assert result.read_bytes() == b"thumb"
    method, url, _ = transport.calls[0]
    assert method == "GET"
    assert "/index.php/core/preview?" in url
    assert "fileId=id-1" in url and "x=512" in url and "forceIcon=0" in url
    assert "/remote.php/dav/files/" not in url


def test_cache_files_are_namespaced_by_server_user_and_root(tmp_path):
    p1 = NextcloudArchiveProvider("https://one.example", "user", "secret", "/archive", transport=FakeTransport(Response()), cache_dir=tmp_path)
    p2_transport = FakeTransport(Response())
    p2 = NextcloudArchiveProvider("https://two.example", "user", "secret", "/archive", transport=p2_transport, cache_dir=tmp_path)
    p1.scan()
    p2.scan()
    assert "If-None-Match" not in p2_transport.calls[0][2]["headers"]


def test_nextcloud_scan_can_be_cancelled_without_index_replacement(tmp_path):
    transport = FakeTransport(Response())
    p = provider(tmp_path, transport)
    with pytest.raises((InterruptedError, RuntimeError)):
        p.scan(cancelled=lambda: True)


def test_nextcloud_asset_identity_includes_server_user_and_root(tmp_path):
    import xml.etree.ElementTree as ET

    prop = ET.fromstring("""<d:prop xmlns:d='DAV:' xmlns:oc='http://owncloud.org/ns'>
      <d:getetag>e1</d:getetag><d:getcontentlength>5</d:getcontentlength><oc:fileid>42</oc:fileid>
    </d:prop>""")
    providers = [
        NextcloudArchiveProvider("https://one.example", "alice", "secret", "/archive", transport=FakeTransport(Response())),
        NextcloudArchiveProvider("https://two.example", "alice", "secret", "/archive", transport=FakeTransport(Response())),
        NextcloudArchiveProvider("https://one.example", "bob", "secret", "/archive", transport=FakeTransport(Response())),
        NextcloudArchiveProvider("https://one.example", "alice", "secret", "/other", transport=FakeTransport(Response())),
    ]
    assets = [p._asset_from_prop(f"{p._dav_root}/2026/226/Game/a.jpg", prop, 365, datetime(2026, 10, 27, tzinfo=timezone.utc)) for p in providers]
    assert len({asset.asset_id for asset in assets}) == len(providers)


def test_local_provider_reads_authoritative_task_sqlite_without_download(tmp_path):
    from PIL import Image
    from galgame_news.tasks.repository import TaskRepository

    root = tmp_path / "task"
    image_path = root / "images" / "a.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "red").save(image_path)
    video_path = root / "images" / "a.mp4"
    video_path.write_bytes(b"video")
    repo = TaskRepository(root)
    repo.activate()
    repo.save_index(
        {
            "issue_id": "226",
            "news_items": [{"news_id": "n1", "title": "Game", "sequence": 1}],
            "candidates": [{"id": "i1", "news_id": "n1", "local_path": "images/a.png", "image_url": "https://img.example/a.png"}],
        },
        {
            "issue_id": "226",
            "videos": [{"id": "v1", "news_id": "n1", "local_path": "images/a.mp4", "video_url": "https://video.example/a.mp4"}],
        },
    )
    found = LocalArchiveProvider([root]).scan()
    assert {a.kind for a in found} == {"image", "video"}
    assert all(a.issue_number == "226" and a.game_names == ("Game",) for a in found)
