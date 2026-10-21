#!/usr/bin/env python3
"""apfs_tm_snapfree: move a churning folder onto its own APFS volume, out of Time Machine.

Time Machine's hourly local snapshots freeze a whole volume, so a folder excluded from
backup still has every file it deleted pinned on disk until the snapshots expire. A
folder on its own volume, excluded whole, gets no local snapshots at all. This tool
sets that up (``setup``), checks it (``status``), changes its size (``resize``) and
undoes it (``teardown``). Every mode is a dry run that prints its numbered steps;
``--apply`` performs them.

This file is argument parsing and dispatch only: each mode's work lives in its own
module under ``src/``, reached through the ``MODES`` table.

Entry point assumptions: run as a CLI on macOS, as the user rather than root, with the
standard library of Python 3.9 or later; ``sudo`` prompts on the terminal for the steps
that need it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO, Union

# This project's modules live in src/, which is not what a script's sys.path[0] points
# at (that is this file's own folder). Inserted unconditionally: the suites insert the
# same folder first, and a repeated entry costs nothing.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# The shared UTF-8 stdio policy library, at ONE fixed place: the stdio_utf8/ folder beside
# this file. Appended, not inserted, so it can shadow neither src/ nor the standard library.
# Not a search of the parent directories: with this folder missing, a search would reach
# past the clone and import whatever folder of that name it found above it; a fixed path
# fails at the import instead.
sys.path.append(str(Path(__file__).resolve().parent / "stdio_utf8"))

import sf_host  # noqa: E402 - import follows the sys.path bootstrap above
import sf_resize  # noqa: E402 - import follows the sys.path bootstrap above
import sf_setup  # noqa: E402 - import follows the sys.path bootstrap above
import sf_size  # noqa: E402 - import follows the sys.path bootstrap above
import sf_status  # noqa: E402 - import follows the sys.path bootstrap above
import sf_teardown  # noqa: E402 - import follows the sys.path bootstrap above
import stdio_utf8  # noqa: E402 - import follows the sys.path bootstrap above
from sf_errors import EXIT_USAGE, ToolError  # noqa: E402 - follows the bootstrap above
from sf_fstab import DEFAULT_FSTAB  # noqa: E402 - follows the bootstrap above
from sf_host import Host  # noqa: E402 - follows the bootstrap above
from sf_options import (  # noqa: E402 - follows the bootstrap above
    DEFAULT_WARN_PERCENT,
    MODE_NAMES,
    Options,
)

#: How diagnostics name this tool on stderr: the file's own name, so a rename carries it.
PROG = Path(__file__).stem
#: The one platform this tool runs on.
_DARWIN = "darwin"
_ROOT_UID = 0
_PERCENT_RANGE = range(1, 101)
#: The modes each mode-specific option belongs to.
_QUOTA_MODES = ("setup", "resize")
_NAME_MODE = "setup"
_FINISH_MODE = "resize"
_READ_ONLY_MODE = "status"
#: The namespace attribute --warn-percent is stored under, and the suffix of the flag
#: recording that an option was given (see ``_StoreGiven``).
_WARN_PERCENT_DEST = "warn_percent"
_GIVEN_SUFFIX = "_given"

ModeHandler = Callable[[Options, Host, TextIO], int]


#: Each mode's handler: validate, plan, then print or perform the plan. The one table
#: each mode module registers itself in.
MODES: dict[str, ModeHandler] = {
    "setup": sf_setup.run_setup,
    "status": sf_status.run_status,
    "resize": sf_resize.run_resize,
    "teardown": sf_teardown.run_teardown,
}

_EPILOG = (
    "Prerequisites: macOS, with the folder on an APFS volume; run as yourself, not "
    "as root: the steps that need root run through sudo, which prompts on this "
    "terminal. The terminal app needs Full Disk Access: an applied setup, resize "
    "or teardown checks for it and refuses to start without it. "
    "Every mode only prints its numbered steps unless --apply is given; status never "
    "changes anything. Exit codes: 0 = every check passed, or every step was printed "
    "or completed; 1 = a status check needs attention (a host command failing during "
    "a check is a FAIL line), or a step failed (its recovery commands are printed, "
    "and none is run); 2 = a usage error, a folder that is not configured, or a run "
    "refused before any check or step (a host command failing while validating "
    "included)."
)


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show each optional's live default; leave required and unset options unstamped.

    Python before 3.13 stamps ``(default: None)`` on a required option, and every
    version stamps it on an optional whose default is None, which here means "derived
    at run time" (the volume name) or "not given" (the quota).
    """

    def _get_help_string(self, action: argparse.Action) -> Optional[str]:
        """*action*'s help, with the default appended only when there is one to show."""
        if action.required or action.default is None:
            return action.help
        return super()._get_help_string(action)


def _given(dest: str) -> str:
    """The namespace attribute ``_StoreGiven`` sets when option *dest* is given."""
    return f"{dest}{_GIVEN_SUFFIX}"


class _StoreGiven(argparse.Action):
    """Store the option's value, and record that it was given at all.

    An option with a default reads the same whether or not it was typed; this tells
    the two apart while --help still shows the live default.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: Optional[str] = None,
    ) -> None:
        """Set the value, then the ``<dest>_given`` flag beside it."""
        setattr(namespace, self.dest, values)
        setattr(namespace, _given(self.dest), True)


def _quota_type(text: str) -> int:
    """Read --quota for argparse."""
    try:
        return sf_size.parse_size(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _percent_type(text: str) -> int:
    """Read --warn-percent for argparse: a whole number from 1 to 100."""
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from exc
    if value not in _PERCENT_RANGE:
        raise argparse.ArgumentTypeError(f"{value} is not between 1 and 100")
    return value


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare every option, each with the modes it applies to."""
    parser.add_argument("--mode", required=True, choices=MODE_NAMES, help="What to do.")
    parser.add_argument(
        "--path",
        required=True,
        type=Path,
        help="The folder; resolved through symlinks, as the fstab and the mount see it.",
    )
    parser.add_argument(
        "--quota",
        type=_quota_type,
        help="setup and resize (required there): the volume's size ceiling, such as "
        "40g; k, m, g and t are powers of 1000, as diskutil reads them.",
    )
    parser.add_argument(
        "--name",
        help="setup: the new volume's name; snapfree- and the folder's name if omitted.",
    )
    parser.add_argument(
        "--fstab",
        type=Path,
        default=DEFAULT_FSTAB,
        help="The fstab read and, with --apply, rewritten.",
    )
    parser.add_argument(
        "--warn-percent",
        type=_percent_type,
        default=DEFAULT_WARN_PERCENT,
        action=_StoreGiven,
        help="status: use of the quota at or above this percentage needs attention.",
    )
    # False unless --warn-percent is on the command line, so a value given to another
    # mode can be refused even though the option always has its default.
    parser.set_defaults(**{_given(_WARN_PERCENT_DEST): False})
    parser.add_argument(
        "--finish",
        action="store_true",
        help="resize: delete the volume a previous resize retired.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="setup, resize and teardown: perform the steps instead of printing them.",
    )


def build_parser() -> argparse.ArgumentParser:
    """The parser: named options only, defaults shown in --help, prog from argv[0]."""
    parser = stdio_utf8.EscapingArgumentParser(
        prog=os.path.basename(sys.argv[0]),
        description=(
            "Move a folder whose contents churn onto its own APFS volume in the same "
            "container, excluded whole from Time Machine, so local snapshots stop "
            "pinning its deleted data."
        ),
        formatter_class=HelpFormatter,
        epilog=_EPILOG,
    )
    _add_arguments(parser)
    return parser


def _mode_conflict(args: argparse.Namespace) -> Optional[str]:
    """Why these options do not fit the mode, or None when they do."""
    if args.mode in _QUOTA_MODES and args.quota is None and not args.finish:
        return f"--quota is required for --mode {args.mode}"
    if args.mode not in _QUOTA_MODES and args.quota is not None:
        return f"--quota applies only to --mode {' and '.join(_QUOTA_MODES)}"
    if args.finish and args.mode != _FINISH_MODE:
        return f"--finish applies only to --mode {_FINISH_MODE}"
    if args.finish and args.quota is not None:
        return "--finish takes no --quota: it deletes the retired volume"
    if args.name is not None and args.mode != _NAME_MODE:
        return f"--name applies only to --mode {_NAME_MODE}"
    if getattr(args, _given(_WARN_PERCENT_DEST)) and args.mode != _READ_ONLY_MODE:
        return f"--warn-percent applies only to --mode {_READ_ONLY_MODE}"
    if args.apply and args.mode == _READ_ONLY_MODE:
        return f"--apply does not apply to --mode {_READ_ONLY_MODE}: it never changes anything"
    return None


def _options_from(args: argparse.Namespace) -> Options:
    """The Options record for parsed *args*, with the path resolved."""
    return Options(
        mode=args.mode,
        path=Path(os.path.realpath(args.path)),
        quota=args.quota,
        name=args.name,
        fstab=args.fstab,
        warn_percent=args.warn_percent,
        finish=args.finish,
        apply=args.apply,
    )


def parse_options(argv: Optional[Sequence[str]]) -> Union[Options, int]:
    """The parsed Options, or the exit code argparse ended with (0 for --help)."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        conflict = _mode_conflict(args)
        if conflict is not None:
            parser.error(conflict)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    return _options_from(args)


def refusal(host: Host) -> Optional[str]:
    """Why this tool will not run on *host*, or None: off macOS, or as root."""
    if host.platform != _DARWIN:
        return f"runs only on macOS (this is {host.platform})"
    if host.getuid() == _ROOT_UID:
        return (
            "run this as yourself, not as root: it runs sudo for the steps that need it"
        )
    return None


def main(argv: Optional[Sequence[str]] = None, host: Optional[Host] = None) -> int:
    """CLI entry point: parse, refuse an unsupported host, then run the mode."""
    stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr)
    opts = parse_options(argv)
    if isinstance(opts, int):
        return opts
    machine = host if host is not None else sf_host.real_host()
    reason = refusal(machine)
    if reason is not None:
        stdio_utf8.print_escaped(f"{PROG}: {reason}", sys.stderr)
        return EXIT_USAGE
    try:
        return MODES[opts.mode](opts, machine, sys.stdout)
    # OSError too: a refused stat or read outside a step (validation, a dry run's
    # listing) is the machine saying no, not a defect, so it gets one line rather
    # than a traceback. Anything else is a defect and keeps its traceback.
    except (ToolError, OSError) as exc:
        stdio_utf8.print_escaped(f"{PROG}: {exc}", sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
