"""Prepare or check a private bundle of explicitly selected reviewed captures.

No database, model, renderer or network operation is performed. This script
does not publish the bundle or approve additional archive candidates.
"""

import argparse
import json
from pathlib import Path

from observatory.asset_bundle import build_bundle, load_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Copy explicitly selected reviewed assets to a new private directory")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--destination", type=Path, required=True)
    build.add_argument("--record-id", action="append", required=True)
    check = commands.add_parser("check", help="Validate the complete mounted bundle against its expected manifest hash")
    check.add_argument("--root", type=Path, required=True)
    check.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build_bundle(args.source_root, args.destination, args.record_id)
        else:
            assets = load_bundle(args.root, args.manifest_sha256)
            result = {"status": "verified", "assets": len(assets), "records": len({a["record_id"] for a in assets})}
    except (OSError, ValueError) as error:
        # Paths or source strings in exception messages are private. Report only
        # a generic actionable failure in command output.
        raise SystemExit("Asset validation failed. Check the selected review, file hashes, paths and manifest hash.") from error
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
