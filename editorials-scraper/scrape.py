"""
Editorials aggregator — E1 scaffold.

Daily 7 AM IST cron via .github/workflows/fetch-editorials.yml.
Source set: 4 papers × 2 streams (editorial + opinion):
  - The Hindu (editorial + lead)
  - Deccan Chronicle (dc-edit + columnist)
  - Deccan Herald (editorial + opinion)
  - The New Indian Express (editorial + column)

Output: editorials.md at the repo root (consumed by joey.editorials plugin).
Rolling 90-day window on the live file. Items older than 90 days move to
archive/editorials-YYYY-MM.md keyed to article publish-month.

This file is the E1 skeleton. Per-source scrapers land at E2; markdown
writer + dedup lands at E3; rolling archive lands at E4.
See docs in nirmit/docs/brainstorms/plugins.md § "Editorials plugin expansion".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

OUTPUT_FILE = Path(__file__).resolve().parent.parent / "editorials.md"


def write_stub() -> None:
    """Write a placeholder editorials.md so app-fetch is reachable at E1.

    The body of the file matches the structure E3 will produce so the app's
    parser can be developed against it: an H1 header + an explanatory note,
    no items yet.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    content = (
        "# Editorials\n\n"
        f"_Last updated: {now}_\n\n"
        "This file is the aggregated daily editorial / opinion feed for "
        "the Joey app's `joey.editorials` plugin. It is regenerated daily "
        "by `.github/workflows/fetch-editorials.yml`.\n\n"
        "**Scaffold only (Layer E1).** Per-source scrapers land at E2.\n"
    )
    OUTPUT_FILE.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT_FILE} ({len(content)} bytes)")


def main() -> None:
    write_stub()


if __name__ == "__main__":
    main()
