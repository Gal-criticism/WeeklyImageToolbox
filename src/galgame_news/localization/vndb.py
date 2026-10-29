"""VNDB image adapter; only identifiers present in explicit VNDB URLs seed lookup."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlsplit

from ..domain import (CollectionContext, CollectionResult, FailureRecord, FailureStage,
                      ImageEvidence, LocalizationProvenance, ReviewReason, SourceType)

_ID = re.compile(r"^(v|r)\d+$", re.I)


class VNDBLocalizationAdapter:
    def __init__(self, client):
        self.client = client

    def _screenshot_page_fallback(self, page_url):
        response = self.client.http.get(page_url)
        status = int(getattr(response, "status_code", 200))
        if not 200 <= status < 300:
            raise RuntimeError(f"VNDB page returned HTTP {status}")
        final_url = str(getattr(response, "url", page_url) or page_url)
        if (urlsplit(final_url).hostname or "").casefold() not in {"vndb.org", "www.vndb.org"}:
            raise RuntimeError("VNDB screenshot fallback redirected off-domain")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(getattr(response, "text", "") or "", "html.parser")
        shots = []
        containers = list(soup.select('#screenshots, .screenshots'))
        for heading in soup.find_all(re.compile(r"^h[1-6]$")):
            if heading.get_text(" ", strip=True).casefold() == "screenshots":
                containers.append(heading.find_parent(class_="mainbox") or heading.parent)
        anchors = {id(anchor): anchor for container in containers
                   for anchor in container.select('a[href*="/sf/"]')}
        for anchor in anchors.values():
            image_url = urljoin(final_url, anchor.get("href", ""))
            if "/sf/" not in urlsplit(image_url).path:
                continue
            image = anchor.find("img")
            thumbnail = urljoin(final_url, image.get("src", "")) if image else None
            parts = urlsplit(image_url)
            known_full = parts.hostname in {"images.vndb.org", "t.vndb.org", "s.vndb.org"} and parts.path.startswith("/sf/")
            shots.append({"id": "", "url": image_url, "dims": [], "thumbnail": thumbnail,
                          "fallback": not known_full, "method": "dom", "alt": image.get("alt", "") if image else ""})
        return shots

    def _release_page_fallback(self, release_id):
        page_url = f"https://vndb.org/{release_id}"
        response = self.client.http.get(page_url)
        status = int(getattr(response, "status_code", 200))
        if not 200 <= status < 300:
            raise RuntimeError(f"VNDB release page returned HTTP {status}")
        final_url = str(getattr(response, "url", page_url) or page_url)
        if (urlsplit(final_url).hostname or "").casefold() not in {"vndb.org", "www.vndb.org"}:
            raise RuntimeError("VNDB release page fallback redirected off-domain")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(getattr(response, "text", "") or "", "html.parser")
        linked = []
        for row in soup.select('tr'):
            cells = row.find_all(['th', 'td'], recursive=False)
            if not cells or cells[0].get_text(" ", strip=True).casefold() not in {"relation", "relations", "visual novel", "visual novels"}:
                continue
            for anchor in row.select('a[href]'):
                parts = urlsplit(urljoin(final_url, anchor.get("href", "")))
                match = re.fullmatch(r"/((?:v)\d+)/?", parts.path, re.I)
                if match and parts.hostname in {"vndb.org", "www.vndb.org"}:
                    linked.append({"id": match.group(1).lower(), "title": anchor.get_text(" ", strip=True), "alttitle": ""})
        return _valid_vns(linked)

    def collect(self, news, source, context: CollectionContext) -> CollectionResult:
        loc = news.localization_context
        urls = [source.url, *news.source_urls, *(loc.source_urls if loc else [])]
        explicit = []
        for value in urls:
            try:
                parts = urlsplit(value)
            except ValueError:
                continue
            if (parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443}
                    or (parts.hostname or "").casefold() not in {"vndb.org", "www.vndb.org"}):
                continue
            match = re.fullmatch(r"/(v\d+|r\d+)/?", parts.path, re.I)
            if match:
                explicit.append(match.group(1).lower())
        # Context IDs are accepted only when tied to an explicit VNDB URL.
        vn_ids, release_ids = _unique(x for x in explicit if x.startswith("v")), _unique(x for x in explicit if x.startswith("r"))
        candidates, failures, review = [], [], []
        try:
            work_bindings: list[tuple[str, str, str | None]] = []
            release_linked_ids: set[str] = set()
            release_source_by_work: dict[str, list[str]] = {}
            for vn_id in vn_ids:
                work_bindings.append((vn_id, "confirmed" if len(vn_ids) == 1 else "reference", None))
            for release_id in release_ids:
                if self.client.remaining(news.id or "") <= 0:
                    break
                try:
                    release = self.client.release(release_id, news.id or "")
                    if not isinstance(release, dict):
                        failures.append(_not_found(news, source, release_id))
                        linked = []
                    else:
                        linked = _valid_vns(release.get("vns", []))
                except Exception as exc:
                    _raise_if_cancelled(exc)
                    failures.append(_api_failure(news, source, release_id, exc))
                    linked = []
                if not linked and self.client.remaining(news.id or "") > 0:
                    try:
                        self.client.reserve_page(news.id or "")
                        linked = self._release_page_fallback(release_id)
                        if not linked:
                            failures.append(_not_found(news, source, release_id))
                    except Exception as exc:
                        _raise_if_cancelled(exc)
                        failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                            code="vndb_release_page_fallback_failed", message=f"VNDB release 页面回退失败（{type(exc).__name__}）。",
                            source_url=f"https://vndb.org/{release_id}", retryable=True))
                binding = _bindings(linked, news.title)
                if len(linked) > 1 and binding and all(kind == "reference" for _, kind in binding):
                    review.append(ReviewReason.UNCERTAIN_MATCH)
                work_bindings.extend((target, kind, None) for target, kind in binding)
                release_linked_ids.update(target for target, _ in binding)
                for target, _ in binding:
                    release_source_by_work.setdefault(target, []).append(f"https://vndb.org/{release_id}")
            release_works = release_linked_ids
            # Deterministic ID ordering and no duplicate work queries.
            primary = {}
            for work_id, binding, rel_id in work_bindings:
                primary[work_id] = ("confirmed" if work_id in primary and "confirmed" in {primary[work_id], binding}
                                    else binding if work_id not in primary else "reference")
            conflict_ids = (set(vn_ids) | release_works) if vn_ids and release_works and not (set(vn_ids) & release_works) else set()
            for work_id in conflict_ids:
                if work_id in primary:
                    primary[work_id] = "uncertain"
            if conflict_ids:
                review.append(ReviewReason.UNCERTAIN_MATCH)
            primary_data = {}
            for work_id in sorted(primary):
                if self.client.remaining(news.id or "") <= 0:
                    break
                data = None
                try:
                    data = self.client.vn(work_id, news.id or "")
                    if not isinstance(data, dict):
                        failures.append(_not_found(news, source, work_id))
                except Exception as exc:
                    _raise_if_cancelled(exc)
                    failures.append(_api_failure(news, source, work_id, exc))
                if not isinstance(data, dict) or not data.get("screenshots"):
                    try:
                        self.client.reserve_page(news.id or "")
                        fallback = self._screenshot_page_fallback(f"https://vndb.org/{work_id}")
                        if isinstance(data, dict):
                            data = {**data, "screenshots": fallback or data.get("screenshots", [])}
                        elif fallback:
                            data = {"id": work_id, "screenshots": fallback, "relations": []}
                        elif data is None:
                            data = {"id": work_id, "screenshots": [], "relations": []}
                        if not fallback and not data.get("screenshots"):
                            failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                                code="vndb_no_screenshots", message=f"VNDB {work_id} API 与页面均未提供截图证据。",
                                source_url=f"https://vndb.org/{work_id}", retryable=False))
                    except Exception as exc:
                        _raise_if_cancelled(exc)
                        failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                            code="vndb_page_fallback_failed", message=f"VNDB 页面截图回退失败（{type(exc).__name__}）。",
                            source_url=f"https://vndb.org/{work_id}", retryable=True))
                        if data is None:
                            data = {"id": work_id, "screenshots": [], "relations": []}
                if not isinstance(data, dict):
                    continue
                primary_data[work_id] = data
            if len(vn_ids) > 1:
                matched = [work_id for work_id in vn_ids if work_id in primary_data
                           and _title_matches(news.title, primary_data[work_id])]
                if len(matched) == 1:
                    primary[matched[0]] = "confirmed"
                else:
                    review.append(ReviewReason.UNCERTAIN_MATCH)
            relation_seen = set(primary_data)
            for work_id in sorted(primary_data):
                data = primary_data[work_id]
                release_urls = release_source_by_work.get(work_id, [])
                chain = list(dict.fromkeys([*(loc.source_urls if loc else []), *news.source_urls,
                                            source.url, *release_urls, f"https://vndb.org/{work_id}"]))
                review.extend(_collect_screenshots(candidates, news, source, chain, context, work_id,
                    primary[work_id], data, binding_conflict=work_id in conflict_ids))
            # Target-bound VN screenshots are retained before any relation-only references.
            for work_id in sorted(primary_data):
                data = primary_data[work_id]
                chain = list(dict.fromkeys([*(loc.source_urls if loc else []), *news.source_urls,
                    source.url, *release_source_by_work.get(work_id, []), f"https://vndb.org/{work_id}"]))
                # Follow direct relations exactly one hop, in stable type/id order.
                relations = sorted(_valid_vns(data.get("relations", [])), key=lambda row: (row[3], row[0]))
                for relation_id, _, _, relation_type, relation_official in relations:
                    if self.client.remaining(news.id or "") <= 0 or len(candidates) >= context.max_candidates:
                        break
                    if relation_id in relation_seen:
                        continue
                    relation_seen.add(relation_id)
                    related = None
                    try:
                        related = self.client.vn(relation_id, news.id or "")
                    except Exception as exc:
                        _raise_if_cancelled(exc)
                        failures.append(_api_failure(news, source, relation_id, exc))
                    if isinstance(related, dict):
                        related_chain = list(dict.fromkeys([*chain, f"https://vndb.org/{relation_id}"]))
                        review.extend(_collect_screenshots(candidates, news, source, related_chain, context, relation_id,
                            "reference", related, relation_of=work_id, relation_type=relation_type,
                            relation_official=relation_official))
                        review.append(ReviewReason.UNCERTAIN_MATCH)
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError) or type(exc).__name__ in {"CancellationRequested", "CancelledError"}:
                raise
            failures.append(FailureRecord(stage=FailureStage.COLLECT, news_id=news.id,
                code="vndb_lookup_failed", message=f"VNDB 查询失败（{type(exc).__name__}）。",
                source_url=source.url, retryable=not ("HTTP 400" in str(exc) or "HTTP 404" in str(exc))))
        return CollectionResult(candidates=candidates[:context.max_candidates], failures=failures,
                                manual_review_reasons=list(dict.fromkeys(review)))


def _valid_vns(values):
    result = []
    for row in values if isinstance(values, list) else []:
        if isinstance(row, dict) and _ID.fullmatch(str(row.get("id", ""))) and str(row["id"]).lower().startswith("v"):
            result.append((str(row["id"]).lower(), str(row.get("title") or ""), str(row.get("alttitle") or ""),
                           str(row.get("relation") or ""), bool(row.get("relation_official", False))))
    return sorted({row[0]: row for row in result}.values(), key=lambda row: (row[3], row[0]))


def _bindings(linked, news_title):
    if len(linked) == 1:
        return [(linked[0][0], "confirmed")]
    if not linked:
        return []
    title = _norm(news_title)
    matches = []
    for row in linked:
        vn_id, *aliases = row
        aliases = [_norm(x) for x in row[1:3] if _norm(x)]
        if any(alias == title or (len(alias) >= 4 and alias in title) for alias in aliases):
            matches.append(vn_id)
    if len(matches) == 1:
        return [(matches[0], "confirmed")]
    return [(row[0], "reference") for row in linked]


def _title_matches(news_title, data):
    title = _norm(news_title)
    aliases = [data.get("title", ""), data.get("alttitle", "")]
    aliases.extend(row.get("title", "") for row in data.get("titles", []) if isinstance(row, dict))
    aliases.extend(row.get("latin", "") for row in data.get("titles", []) if isinstance(row, dict))
    aliases = [_norm(alias) for alias in aliases if _norm(alias)]
    return any(alias == title or (len(alias) >= 4 and alias in title) for alias in aliases)


def _collect_screenshots(out, news, source, source_chain, context, work_id, binding, data,
                         relation_of=None, relation_type="", relation_official=False, binding_conflict=False):
    review = []
    page = f"https://vndb.org/{work_id}"
    shots = [shot for shot in data.get("screenshots", []) if isinstance(shot, dict) and shot.get("url")]
    variants = []
    for shot in shots:
        full_url = str(shot["url"])
        fallback = bool(shot.get("fallback"))
        variants.append((shot, full_url, "unknown" if fallback else "full",
                         shot.get("dims") if isinstance(shot.get("dims"), list) else [], None))
    for shot in shots:
        thumb = shot.get("thumbnail")
        if thumb and str(thumb) != str(shot["url"]):
            variants.append((shot, str(thumb), "thumbnail",
                shot.get("thumbnail_dims") if isinstance(shot.get("thumbnail_dims"), list) else [], str(shot["url"])))
    for shot, image_url, resolution, dims, variant_of in variants:
        if not isinstance(shot, dict) or not shot.get("url"):
            continue
        sexual = shot.get("sexual")
        # VNDB sexual rating is per screenshot. Explicitly rated sexual images
        # are omitted; missing ratings remain review-only below.
        if sexual is not None and str(sexual).isdigit() and int(sexual) > 0:
            continue
        release_id = (shot.get("release") or {}).get("id", "") if isinstance(shot.get("release"), dict) else ""
        if len(out) >= context.max_candidates:
            break
        candidate = _candidate(news, image_url, source, context)
        explicit_links = set(news.source_urls)
        candidate.news_source_url = (source.root_url if source.root_url in explicit_links else
            source.url if source.url in explicit_links else source.url)
        candidate.source_type = SourceType.UNVERIFIED
        candidate.expected_width = _dim(dims[0]) if len(dims) > 0 else None
        candidate.expected_height = _dim(dims[1]) if len(dims) > 1 else None
        candidate.evidence = [ImageEvidence(page_url=page, container="vndb:screenshots", item_id=str(shot.get("id") or release_id),
            role="gameplay_screenshot", method=shot.get("method", "api"),
            relationship="VNDB page screenshot fallback" if shot.get("method") == "dom" else "VNDB screenshot; release.id identifies its release",
            variant_of=variant_of)]
        binding_value = "uncertain" if binding_conflict else binding
        candidate.localization_provenance = LocalizationProvenance(source="vndb", work_id=work_id,
            binding=binding_value, resolution=resolution, screenshot=True, native=(resolution == "full"), source_chain=source_chain)
        candidate.signals.update({"localization_source": "vndb", "localization_work_id": work_id,
            "localization_screenshot_release_id": str(release_id),
            "localization_binding": binding_value, "localization_resolution": resolution,
            "localization_screenshot": True, "localization_native": resolution == "full"})
        candidate.signals["localization_screenshot_sexual"] = sexual if sexual is not None else "unknown"
        if shot.get("violence") is not None:
            candidate.signals["localization_screenshot_violence"] = shot["violence"]
        if data.get("rating") is not None:
            candidate.signals["localization_work_rating"] = data["rating"]
        if data.get("votecount") is not None:
            candidate.signals["localization_work_votecount"] = data["votecount"]
        if resolution != "full" or binding != "confirmed" or binding_conflict or sexual is None:
            candidate.signals["localization_review_only"] = True
        if relation_of:
            candidate.signals["localization_relation_of"] = relation_of
            candidate.signals["localization_relation_type"] = relation_type
            candidate.signals["localization_relation_official"] = relation_official
        if binding_conflict:
            candidate.signals["localization_binding_conflict"] = True
        out.append(candidate)
    return review


def _dim(value):
    try:
        number = int(value)
        return number if number > 0 else None
    except (ValueError, TypeError):
        return None


def _unique(values):
    return list(dict.fromkeys(values))


def _norm(value):
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _candidate(news, url, source, context):
    from ..discovery.adapters.common import _candidate
    return _candidate(news, url, source, context)


def _not_found(news, source, identifier):
    return FailureRecord(stage=FailureStage.COLLECT, news_id=news.id, code="vndb_no_results",
        message=f"VNDB 查询未返回 {identifier} 的记录。", source_url=source.url, retryable=False)


def _raise_if_cancelled(error):
    if type(error).__name__ in {"CancellationRequested", "CancelledError"}:
        raise error


def _api_failure(news, source, identifier, error):
    detail = str(error).strip() or "网络或响应处理异常"
    permanent = any(f"HTTP {status}" in detail for status in (400, 401, 403, 404, 422))
    return FailureRecord(stage=FailureStage.COLLECT, news_id=news.id, code="vndb_lookup_failed",
        message=f"VNDB {identifier} 查询失败（{type(error).__name__}）：{detail}",
        source_url=source.url, retryable=not permanent)
