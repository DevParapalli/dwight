import argparse
import sys


def run_app(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def run_target(target_args: list[str]) -> None:
    from tools.mock_target_api import main as target_main

    # Accept the conventional "main.py target -- --port 8900" separator too.
    if target_args and target_args[0] == "--":
        target_args = target_args[1:]

    sys.argv = ["tools/mock_target_api.py", *target_args]
    target_main()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Dwight app or mock target")
    subparsers = parser.add_subparsers(dest="command", required=True)

    app_parser = subparsers.add_parser("app", help="run the Dwight web application")
    app_parser.add_argument("--host", default="127.0.0.1")
    app_parser.add_argument("--port", type=int, default=8000)
    app_parser.add_argument("--reload", action="store_true")

    subparsers.add_parser(
        "target",
        help="run the mock target API (all flags are passed through)",
        add_help=False,
    )

    # Dispatched before parse_args: argparse cannot capture a passthrough tail
    # that starts with a flag, so "target" never reaches the subparser.
    if len(sys.argv) > 1 and sys.argv[1] == "target":
        run_target(sys.argv[2:])
        return

    args = parser.parse_args()
    run_app(args)


if __name__ == "__main__":
    main()
