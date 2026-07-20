from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import (
    DEFAULT_CONFIG_NAME,
    DEFAULT_DESCRIPTION,
    DEFAULT_IMAGES,
    DEFAULT_INDEX_TITLE,
    DEFAULT_NODE_NAME,
    DEFAULT_WORDS,
    DEBOUNCE_SECONDS,
    Settings,
    default_config_path,
    ensure_dirs,
    ensure_nomad_config,
    merge_cli_overrides,
    settings_from_file,
    write_sample_config,
)
from .convert import convert_all
from .daemon import NomadDaemon
from .watcher import EpubWatcher


def _default_epubs() -> Path:
    return Path.cwd() / "epubs"


def _default_data() -> Path:
    return Path.cwd() / "data" / "nomadnetwork"


def _build_settings(args: argparse.Namespace) -> Settings:
    config_arg = getattr(args, "config", None)
    config_path = Path(config_arg).expanduser() if config_arg else default_config_path()

    if config_path.exists():
        settings = settings_from_file(config_path)
    else:
        settings = Settings(
            epubs_dir=_default_epubs().resolve(),
            data_dir=_default_data().resolve(),
        )
        if getattr(args, "init_config", False) or (
            args.command == "serve" and not config_arg
        ):
            write_sample_config(default_config_path())
            logging.getLogger(__name__).info(
                "Wrote sample config %s — edit index_title / node_name / description",
                default_config_path(),
            )

    images = args.images
    if images is not None and images not in ("none", "files"):
        raise SystemExit("--images must be 'none' or 'files'")

    overrides = {}
    if getattr(args, "epubs_set", False):
        overrides["epubs_dir"] = Path(args.epubs).expanduser().resolve()
    if getattr(args, "data_dir_set", False):
        overrides["data_dir"] = Path(args.data_dir).expanduser().resolve()
    if getattr(args, "words_set", False):
        overrides["words"] = args.words
    if getattr(args, "images_set", False):
        overrides["images"] = images
    if getattr(args, "index_title_set", False):
        overrides["index_title"] = args.index_title
    if getattr(args, "node_name_set", False):
        overrides["node_name"] = args.node_name
    if getattr(args, "description_set", False):
        overrides["description"] = args.description

    if not config_path.exists():
        if "epubs_dir" not in overrides and os.environ.get("NOMADNET_EPUB_DIR"):
            overrides["epubs_dir"] = Path(os.environ["NOMADNET_EPUB_DIR"]).expanduser().resolve()
        if "data_dir" not in overrides and os.environ.get("NOMADNET_EPUB_DATA"):
            overrides["data_dir"] = Path(os.environ["NOMADNET_EPUB_DATA"]).expanduser().resolve()
        if "index_title" not in overrides and os.environ.get("NOMADNET_EPUB_INDEX_TITLE"):
            overrides["index_title"] = os.environ["NOMADNET_EPUB_INDEX_TITLE"]
        if "node_name" not in overrides and os.environ.get("NOMADNET_EPUB_NODE_NAME"):
            overrides["node_name"] = os.environ["NOMADNET_EPUB_NODE_NAME"]
        if "description" not in overrides and os.environ.get("NOMADNET_EPUB_DESCRIPTION"):
            overrides["description"] = os.environ["NOMADNET_EPUB_DESCRIPTION"]

    return merge_cli_overrides(settings, **overrides)


class _StoreAndMark(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_set", True)


def _add_shared_flags(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(
        epubs_set=False,
        data_dir_set=False,
        words_set=False,
        images_set=False,
        index_title_set=False,
        node_name_set=False,
        description_set=False,
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to TOML config (default: ./{DEFAULT_CONFIG_NAME})",
    )
    parser.add_argument(
        "--epubs",
        default=str(_default_epubs()),
        action=_StoreAndMark,
        help="Directory for .epub files",
    )
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        default=str(_default_data()),
        action=_StoreAndMark,
        help="NomadNet data directory (pages, files, runtime state)",
    )
    parser.add_argument(
        "--words",
        type=int,
        default=DEFAULT_WORDS,
        action=_StoreAndMark,
        help=f"Approx. words per .mu page (default: {DEFAULT_WORDS})",
    )
    parser.add_argument(
        "--images",
        choices=("none", "files"),
        default=DEFAULT_IMAGES,
        action=_StoreAndMark,
        help="Image handling: omit, or extract to NomadNet files",
    )
    parser.add_argument(
        "--index-title",
        dest="index_title",
        default=DEFAULT_INDEX_TITLE,
        action=_StoreAndMark,
        help="Heading on index.mu",
    )
    parser.add_argument(
        "--node-name",
        dest="node_name",
        default=DEFAULT_NODE_NAME,
        action=_StoreAndMark,
        help="NomadNet announce / network display name",
    )
    parser.add_argument(
        "--description",
        default=DEFAULT_DESCRIPTION,
        action=_StoreAndMark,
        help="Tagline under the heading on index.mu",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help=f"Write a sample {DEFAULT_CONFIG_NAME} if missing",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )


def cmd_convert(args: argparse.Namespace) -> int:
    settings = _build_settings(args)
    if args.init_config:
        write_sample_config(Path(args.config).expanduser() if args.config else default_config_path())
    ensure_dirs(settings)
    books = convert_all(settings, force=args.force)
    print(f"Converted {len(books)} book(s) into {settings.pages_dir}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _build_settings(args)
    ensure_dirs(settings)
    ensure_nomad_config(settings)

    convert_all(settings, force=args.force)

    daemon = NomadDaemon(settings.nomad_config_dir, console=True)
    restart_lock = False

    def on_change() -> None:
        nonlocal restart_lock
        if restart_lock:
            return
        restart_lock = True
        try:
            convert_all(settings, force=False)
            daemon.restart()
        finally:
            restart_lock = False

    watcher = EpubWatcher(
        settings.epubs_dir,
        on_change,
        debounce=DEBOUNCE_SECONDS,
    )

    try:
        daemon.start()
        watcher.start()
        logging.getLogger(__name__).info(
            "Serving %r (node %r) from %s — drop EPUBs in %s",
            settings.index_title,
            settings.node_name,
            settings.pages_dir,
            settings.epubs_dir,
        )
        watcher.run_forever()
    except FileNotFoundError as e:
        logging.error("%s", e)
        return 1
    except RuntimeError as e:
        logging.error("%s", e)
        return 1
    finally:
        watcher.stop()
        daemon.stop()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else default_config_path()
    if path.exists() and not args.force:
        print(f"Already exists: {path}")
        return 0
    if args.force and path.exists():
        path.unlink()
    write_sample_config(path)
    (_default_epubs()).mkdir(parents=True, exist_ok=True)
    print(f"Wrote {path}")
    print(f"Drop EPUBs in {_default_epubs().resolve()} then run: nomadnet-epub serve")
    return 0


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0].startswith("-"):
        if not any(a in ("-h", "--help", "--version") for a in raw):
            raw = ["serve", *raw]

    parser = argparse.ArgumentParser(
        prog="nomadnet-epub",
        description="Convert EPUBs to NomadNet Micron pages and serve them.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Convert, run NomadNet daemon, watch for new EPUBs")
    _add_shared_flags(serve_p)
    serve_p.add_argument("--force", action="store_true", help="Reconvert all EPUBs on startup")
    serve_p.set_defaults(func=cmd_serve)

    convert_p = sub.add_parser("convert", help="One-shot EPUB → .mu conversion (no daemon)")
    _add_shared_flags(convert_p)
    convert_p.add_argument("--force", action="store_true", help="Reconvert all EPUBs")
    convert_p.set_defaults(func=cmd_convert)

    init_p = sub.add_parser("init", help=f"Write sample {DEFAULT_CONFIG_NAME} and epubs/ dir")
    init_p.add_argument("--config", default=None, help="Config path to write")
    init_p.add_argument("--force", action="store_true", help="Overwrite existing config")
    init_p.set_defaults(func=cmd_init)

    args = parser.parse_args(raw)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
