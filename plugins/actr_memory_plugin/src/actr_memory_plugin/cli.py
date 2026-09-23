"""ACT-R Memory Plugin CLI.

Provides command-line access to memory operations.
"""

import argparse
import sys


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="ACT-R Memory System CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  actr_memory remember "Important fact to remember"
  actr_memory recall "what did I learn about X"
  actr_memory learn ~/docs/knowledge-base --pattern "*.md" --memorize
  actr_memory stats
  actr_memory list --type episodic --limit 10
  actr_memory export --output ~/backup.json
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # remember command
    remember_parser = subparsers.add_parser("remember", help="Store a new memory")
    remember_parser.add_argument("content", help="Content to remember")
    remember_parser.add_argument("--tags", nargs="*", help="Tags for organization")

    # recall command
    recall_parser = subparsers.add_parser("recall", help="Search memories")
    recall_parser.add_argument("query", help="Search query")
    recall_parser.add_argument("--top-k", type=int, default=5, help="Number of results")
    recall_parser.add_argument(
        "--type", choices=["all", "episodic", "semantic_l1", "semantic_l2"], default="all"
    )
    recall_parser.add_argument("--include-archived", action="store_true")

    # forget command
    forget_parser = subparsers.add_parser("forget", help="Archive a memory")
    forget_parser.add_argument("memory_id", help="Memory ID to archive")

    # memorize command
    memorize_parser = subparsers.add_parser("memorize", help="Add to spaced repetition queue")
    memorize_parser.add_argument("--id", dest="memory_id", help="Existing memory ID")
    memorize_parser.add_argument("--content", help="Content to memorize")

    # stop-memorizing command
    stop_parser = subparsers.add_parser("stop-memorizing", help="Remove from memorization queue")
    stop_parser.add_argument("memory_id", help="Memory ID to stop memorizing")

    # list-memorizing command
    list_mem_parser = subparsers.add_parser("list-memorizing", help="List memorization queue")
    list_mem_parser.add_argument("--include-completed", action="store_true")

    # learn command
    learn_parser = subparsers.add_parser("learn", help="Ingest knowledge from files")
    learn_parser.add_argument("path", help="File or directory path")
    learn_parser.add_argument("--pattern", default="*.md", help="Glob pattern for files")
    learn_parser.add_argument(
        "--no-recursive", action="store_true", help="Don't search recursively"
    )
    learn_parser.add_argument("--memorize", action="store_true", help="Also add to memorization")
    learn_parser.add_argument("--tags", nargs="*", help="Tags to apply")

    # consolidate command
    consolidate_parser = subparsers.add_parser("consolidate", help="Run memory consolidation")
    consolidate_parser.add_argument("--dry-run", action="store_true", help="Preview only")

    # stats command
    subparsers.add_parser("stats", help="Show memory statistics")

    # list command
    list_parser = subparsers.add_parser("list", help="List memories")
    list_parser.add_argument("--type", choices=["episodic", "semantic_l1", "semantic_l2"])
    list_parser.add_argument("--status", choices=["active", "archived"], default="active")
    list_parser.add_argument("--tag", help="Filter by tag")
    list_parser.add_argument(
        "--sort", choices=["strength", "created_at", "retrieval_count"], default="strength"
    )
    list_parser.add_argument("--limit", type=int, default=20)

    # export command
    export_parser = subparsers.add_parser("export", help="Export memories to JSON")
    export_parser.add_argument("--output", help="Output file path")
    export_parser.add_argument("--include-archived", action="store_true")

    # import command
    import_parser = subparsers.add_parser("import", help="Import memories from JSON")
    import_parser.add_argument("file", help="Input file path")

    # process-queue command
    subparsers.add_parser("process-queue", help="Process memorization queue")

    # recompute command
    subparsers.add_parser("recompute", help="Recompute all memory strengths")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    # For now, just print what would be done
    # In production, this would connect to the running Ananta system
    print(f"Command: {args.command}")
    print(f"Arguments: {vars(args)}")
    print("\nNote: CLI requires running Ananta system. Use plugin actions via console.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
