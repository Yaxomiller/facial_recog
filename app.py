import sys

from src.attendance import build_parser, launch_offline_native_react_app


def main() -> None:
    try:
        parser = build_parser()
        args = parser.parse_args()
        if getattr(args, "command", None) is None:
            launch_offline_native_react_app()
            return

        args.handler(args)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
