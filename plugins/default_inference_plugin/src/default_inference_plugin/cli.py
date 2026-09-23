import argparse
import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .logging_setup import setup_logging
from .utils import parse_json_string, validate_json_file

__version__ = "0.1.0"


def _create_plugin_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Default Inference Plugin CLI")
    parser.add_argument(
        "--params", required=True, help="JSON string or path to JSON file with params"
    )
    parser.add_argument(
        "--state", required=True, help="JSON string or path to JSON file with state"
    )
    parser.add_argument("--directory", required=True, help="Directory for log files")
    parser.add_argument(
        "--log-file", help="Log file name (defaults to inference.log)", default=None
    )
    parser.add_argument(
        "--log-level",
        help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        default="INFO",
    )
    return parser


def _create_framework_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ananta Framework - A platform for building AI-driven applications where "
            "decision logic is delegated to language models through structured prompts."
        )
    )
    parser.add_argument(
        "--APP_FOLDER",
        default="app",
        help="Path to the application-specific folder containing actions (Default: 'app')",
    )
    parser.add_argument(
        "--schedule_interval",
        type=int,
        default=3,
        help="Time interval in seconds between execution cycles (Default: 3)",
    )
    parser.add_argument(
        "--starting_prompt",
        default="plan_agenda",
        help="The name of the first prompt to query in each cycle (Default: 'plan_agenda')",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Ananta Framework v{__version__}",
        help="Show version information and exit",
    )
    return parser


def load_json(json_arg: str) -> dict[str, Any]:
    try:
        return parse_json_string(json_arg)
    except ValidationError:
        try:
            return validate_json_file(json_arg)
        except ValidationError as e:
            raise ValueError(f"Failed to load JSON from argument or file: {str(e)}") from e


def validate_app_folder(app_folder: str) -> bool:
    app_path = Path(app_folder)

    if not app_path.exists():
        logging.error(f"Application folder '{app_folder}' does not exist")
        return False

    actions_path = app_path / "actions"
    if not actions_path.exists():
        logging.error(f"Actions folder '{actions_path}' does not exist")
        return False

    action_files = list(actions_path.glob("*.json"))
    if not action_files:
        logging.error(f"No action files (*.json) found in {actions_path}")

    state_file = app_path / "data" / "state.json"
    state_dir = os.path.dirname(state_file)
    if not os.path.exists(state_dir):
        os.makedirs(state_dir, exist_ok=True)

    if not state_file.exists():
        try:
            with open(state_file, "w") as f:
                f.write("{}")
        except Exception as e:
            logging.error(f"Error creating state file: {e}")
            return False

    return True


def main() -> int:
    args = _create_plugin_arg_parser().parse_args()

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logger = setup_logging(log_file=args.log_file, log_level=log_level)

    # Validate that params and state can be loaded (preserves arg validation)
    load_json(args.params)
    load_json(args.state)

    # REMOVED: Standalone CLI execution is no longer supported.
    # The Plugin requires framework injection (orchestrator, services)
    # and cannot be used outside the Ananta framework context.
    # Use the framework's inference service instead.
    logger.error("Standalone CLI execution removed - use Ananta framework inference service")
    return 1


async def main_async() -> None:
    args = _create_framework_arg_parser().parse_args()

    setup_logging(log_level=logging.DEBUG if args.verbose else logging.INFO)

    os.environ["APP_FOLDER"] = args.APP_FOLDER
    os.environ["STARTING_PROMPT"] = args.starting_prompt

    if not validate_app_folder(args.APP_FOLDER):
        logging.error(f"Invalid application folder: {args.APP_FOLDER}. Exiting.")
        sys.exit(1)

    try:
        from ananta.core.orchestrator import Orchestrator  # type: ignore[import-not-found]

        Orchestrator.SCHEDULE_INTERVAL = args.schedule_interval

        orchestrator = Orchestrator(starting_prompt=args.starting_prompt)
        await orchestrator.schedule_next_run()
    except KeyboardInterrupt:
        pass
    except ImportError as e:
        logging.error(f"Error importing Orchestrator: {e}")
        logging.error("Please make sure Ananta is installed correctly")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Error running orchestrator: {e}")
        traceback.print_exc()
        sys.exit(1)


def cli_main() -> int:
    try:
        asyncio.run(main_async())
        return 0
    except KeyboardInterrupt:
        print("\nShutting down Ananta framework...")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
