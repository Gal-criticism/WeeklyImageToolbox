"""Produce metadata-only rule exploration samples from the existing DOCX input."""
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from galgame_news.ingestion.docx_parser import DocxDocumentParser
from galgame_news.ingestion.analyzer import RuleBasedNewsAnalyzer
from galgame_news.domain import ImageCandidate, SourceType
from galgame_news.supplement.rules import classify_candidate


def main():
    root = Path(__file__).resolve().parents[1]
    patterns = {"cg_update": r"CG.*更新|CG.*公开", "localization": r"汉化|官中", "key_visual": r"主视觉|主視覺", "goods": r"周边|商品|特典", "event": r"抽奖|活动", "version": r"重制|重製|アフター|After|FD"}
    samples = {}
    for path in sorted((root / "input").glob("*.docx")):
        issue = RuleBasedNewsAnalyzer().analyze(DocxDocumentParser().parse(path, path.stem))
        for news in issue.news_items:
            for category, pattern in patterns.items():
                if category in samples or not re.search(pattern, news.title, re.I):
                    continue
                candidate = ImageCandidate(news_id=news.id, image_url="https://example.invalid/metadata-fixture.jpg", source_url=news.source_urls[0] if news.source_urls else "https://example.invalid/", source_type=SourceType.UNVERIFIED, nearby_text=news.title, fetched_at=datetime.now(timezone.utc))
                samples[category] = {"source_document": str(path), "news": news.model_dump(mode="json"), "verdict": classify_candidate(news, candidate), "metadata_only": True, "visual_review": False}
    output = root / "output" / "补图模块试验-20261028"
    output.mkdir(parents=True, exist_ok=True)
    (output / "rule-samples.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Metadata samples: {len(samples)}/{len(patterns)}; no image downloads or visual review")


if __name__ == "__main__":
    main()
