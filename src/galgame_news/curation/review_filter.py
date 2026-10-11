"""Keep useful alternatives visible without discarding source assets."""

from pathlib import Path
from typing import Iterable
from collections import OrderedDict
from threading import Lock

from ..domain import ImageCandidate
from .dedup import _confirmed_visual_duplicate, _original_hash, _representative_key


def is_low_resolution(candidate: ImageCandidate) -> bool:
    """Use native dimensions; missing dimensions are not proof of low quality."""
    width = candidate.original_width or candidate.width or 0
    height = candidate.original_height or candidate.height or 0
    return bool(width > 0 and height > 0 and (
        max(width, height) < 800 or min(width, height) < 600
    ))


def _available(candidate: ImageCandidate) -> bool:
    for value in (candidate.local_path, candidate.original_path):
        try:
            if value and Path(value).is_file():
                return True
        except (OSError, ValueError):
            continue
    return False


def _same_scene(left: ImageCandidate, right: ImageCandidate) -> bool:
    if left.news_id != right.news_id:
        return False
    left_hash, right_hash = _original_hash(left), _original_hash(right)
    return bool(left_hash and left_hash == right_hash) or _confirmed_visual_duplicate(left, right)


class SceneComparisonCache:
    """Cache pair confirmations, not connected groups or editorial decisions."""

    def __init__(self, max_news: int = 64, max_pairs_per_news: int = 4096):
        self.max_news, self.max_pairs_per_news = max_news, max_pairs_per_news
        self._news = OrderedDict()
        self._lock = Lock()

    def invalidate(self, news_ids):
        with self._lock:
            for news_id in news_ids:
                self._news.pop(str(news_id), None)

    @staticmethod
    def asset_key(candidate):
        path = candidate.original_path or candidate.local_path
        try:
            stat = Path(path).stat() if path else None
        except (OSError, ValueError):
            stat = None
        return (str(candidate.id), _original_hash(candidate), candidate.output_sha256,
                candidate.perceptual_hash, str(path), candidate.original_width,
                candidate.original_height, candidate.width, candidate.height,
                stat.st_mtime_ns if stat else None, stat.st_size if stat else None)

    def confirm(self, left, right, asset_keys):
        if left.news_id != right.news_id:
            return False
        news_id = str(left.news_id)
        a, b = asset_keys[id(left)], asset_keys[id(right)]
        key = frozenset((a, b))
        with self._lock:
            cache = self._news.setdefault(news_id, OrderedDict())
            self._news.move_to_end(news_id)
            while len(self._news) > self.max_news:
                self._news.popitem(last=False)
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
        confirmed = _same_scene(left, right)
        with self._lock:
            cache = self._news.setdefault(news_id, OrderedDict())
            cache[key] = confirmed
            while len(cache) > self.max_pairs_per_news:
                cache.popitem(last=False)
        return confirmed


def reviewable_pending_images(
    pending: Iterable[ImageCandidate], *, accepted: Iterable[ImageCandidate] = (),
    comparison_cache: SceneComparisonCache | None = None,
) -> list[ImageCandidate]:
    """Filter review/export only, leaving decisions, indices and originals intact.

    Accepted images take precedence. Among alternatives use the existing native
    detail ranking and confirm duplicates with hashes plus decoded pixels. Never
    suppress an alternative solely because a stale duplicate target is recorded.
    """
    values = list(pending)
    representatives = [candidate for candidate in accepted if _available(candidate)]
    asset_keys = ({id(c): comparison_cache.asset_key(c) for c in [*values, *representatives]}
                  if comparison_cache is not None else {})
    retained: set[int] = set()
    for candidate in sorted(values, key=_representative_key):
        if is_low_resolution(candidate):
            continue
        if any((comparison_cache.confirm(candidate, representative, asset_keys)
                if comparison_cache is not None else _same_scene(candidate, representative))
               for representative in representatives):
            continue
        retained.add(id(candidate))
        if _available(candidate):
            representatives.append(candidate)
    return [candidate for candidate in values if id(candidate) in retained]
