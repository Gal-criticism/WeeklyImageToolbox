"""Spawn target for the public DDGS provider.

Kept in a module so Windows spawn and frozen PyInstaller builds can import it.
"""

from __future__ import annotations


def ddgs_search_worker(connection, queries: tuple[str, ...], max_results: int, timeout: float) -> None:
    try:
        from ddgs import DDGS

        client = DDGS(timeout=timeout)
        results = []
        for query in queries:
            try:
                for value in list(client.text(query, max_results=max_results))[:max_results]:
                    if isinstance(value, dict):
                        results.append(value)
                    else:
                        results.append({
                            "url": str(getattr(value, "url", value)),
                            "title": str(getattr(value, "title", "")),
                            "snippet": str(getattr(value, "snippet", "")),
                        })
            except Exception:
                continue
        connection.send((True, results))
    except BaseException:
        try:
            connection.send((False, []))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()
