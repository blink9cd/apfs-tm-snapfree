"""The numbered step list every mode prints as a dry run and performs with --apply.

A mode builds its whole plan before acting, so the dry run and the real run are the
same list. Each step's display is the exact command line, copy-pasteable, and each may
carry the command that undoes it. A failed step stops the run and prints the recovery
commands of the steps already completed, most recent first, for a person to judge;
none is ever run automatically, because an undo that guesses at a half-finished state
can do more harm than the failure.

Values found only while applying
--------------------------------
Some steps need a value an earlier step discovers, above all the new volume's UUID,
which exists only once ``addVolume`` has run. A ``Slot`` holds such a value. An argv,
a display or a recovery command may contain slots: the dry run shows each unset slot's
placeholder, and the real run reads each one at the moment its step prints and runs.
A step's confirm callback fills the slot, once it has read the value back::

    uuid = Slot("<new-volume-uuid>")

    def found() -> None:
        container = sf_macos.container_volumes(host, "disk3")
        (volume,) = container.volumes_named(name)  # exactly one, or the step fails
        uuid.set(volume.uuid)

    steps = [
        command_step(host, ["sudo", "diskutil", "apfs", "addVolume", "disk3", "APFS",
                            name, "-quota", "40g", "-nomount"], COMMAND_TIMEOUT,
                     recover=["sudo", "diskutil", "apfs", "deleteVolume", uuid],
                     confirm=found),
        command_step(host, ["sudo", "diskutil", "mount", "-mountOptions", "nobrowse",
                            "-mountPoint", path, uuid],
                     COMMAND_TIMEOUT, recover=["sudo", "diskutil", "unmount", path]),
    ]

The dry run prints ``... -mountPoint /p <new-volume-uuid>``; the real run prints and
runs the UUID itself, and a failure after the first step names that UUID in the
``deleteVolume`` recovery line. A recovery line is rendered only when it prints, so it
always shows what was learned by then, and an unset slot prints its placeholder rather
than breaking the recovery block.

A step whose command ran but whose confirm then failed is not done, yet its command
may well have changed something: setup's ``addVolume`` may have made a volume the
confirm could not find. Such a step raises ``ConfirmFailed``, and its own recovery
line prints first in the block, marked as a step whose command ran.

Entry point assumptions: steps run in order on the caller's thread; output goes to the
stream handed in.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, TextIO, Union

from sf_errors import EXIT_FAIL, EXIT_OK, ToolError
from sf_host import Host, run_checked

DRY_RUN_FOOTER = (
    "Dry run: nothing was changed. Re-run with --apply to perform these steps."
)
_RECOVERY_HEADER = "Recovery, most recent first:"
_NOTHING_TO_UNDO = "  (no completed step needs undoing)"
#: Said instead of the usual header when a final cleanup step fails.
_FINAL_HEADER = (
    "Recovery: every step before this one completed and needs no undo; retry this one:"
)
#: Marks the recovery of the step that failed after its command ran.
_RAN_NOTE = "  # step {number}'s command ran"
_NOT_UNDONE = (
    "Nothing was undone automatically: check the state, then run the recovery "
    "commands above that apply."
)


class ConfirmFailed(ToolError):
    """A step's command ran, but what it should have changed could not be seen.

    Unlike a command that failed, this step may have changed something, so its own
    recovery prints along with the completed steps'.
    """


def confirmed(confirm: Callable[[], None]) -> None:
    """Run a step's *confirm*, its failure raised as ``ConfirmFailed``.

    A step's action calls this once its command has run, so that whatever the read-
    back raises (a ``ToolError``, or an ``OSError`` from a stat) says the command ran.
    """
    try:
        confirm()
    except (ToolError, OSError) as exc:
        raise ConfirmFailed(str(exc)) from exc


class Slot:
    """A value one step discovers and later steps use, shown as a placeholder until set."""

    def __init__(self, placeholder: str) -> None:
        """Name the slot by the *placeholder* a dry run shows, such as ``<uuid>``."""
        self.placeholder = placeholder
        self._value: Optional[str] = None

    @property
    def is_set(self) -> bool:
        """Whether a step has filled this slot yet."""
        return self._value is not None

    def set(self, value: str) -> None:
        """Fill the slot; a confirm callback calls this once it has read *value* back."""
        self._value = value

    def get(self) -> str:
        """The value; a ``ToolError`` if no step has filled it, which is a plan defect."""
        if self._value is None:
            raise ToolError(f"{self.placeholder} was needed before any step found it")
        return self._value

    def text(self) -> str:
        """The value if set, else the placeholder."""
        return self._value if self._value is not None else self.placeholder


#: One argv item: a literal, or a value a slot supplies.
Arg = Union[str, Slot]
#: A recovery: a fixed line, an argv that may hold slots, or a function giving the line.
Recover = Union[None, str, Sequence[Arg], Callable[[], Optional[str]]]


def _render_item(item: Arg) -> str:
    """One argv item, shell-quoted; an unset slot's placeholder is left bare."""
    if isinstance(item, Slot) and not item.is_set:
        return item.placeholder
    return shlex.quote(item.text() if isinstance(item, Slot) else item)


def render(argv: Sequence[Arg]) -> str:
    """*argv* as one copy-pasteable line, each slot as its value or placeholder."""
    return " ".join(_render_item(item) for item in argv)


#: The highest C0 control character, and DEL: each ends or corrupts a printed line.
_LAST_C0 = 0x1F
_DEL = 0x7F


def comment_text(value: object) -> str:
    r"""*value* as text safe inside a printed ``#`` recovery comment.

    A shell comment ends at a newline whatever quoting surrounds it, so a folder named
    with one would turn the rest of the comment into a command a pasted recovery block
    runs as root. Every control character is shown as ``\xNN`` instead; spaces and
    quotes, which a comment needs no protection from, are left as they are.
    """
    return "".join(
        f"\\x{ord(char):02x}" if ord(char) <= _LAST_C0 or ord(char) == _DEL else char
        for char in str(value)
    )


def resolve(argv: Sequence[Arg]) -> list[str]:
    """*argv* with every slot read; an unset one is a ``ToolError``."""
    return [item.get() if isinstance(item, Slot) else item for item in argv]


@dataclass(frozen=True)
class Step:
    """One numbered step: what it shows, what it does, and how to undo it.

    *display* is a line, an argv that may hold slots, or a function giving the text
    (for a display that depends on what is there when it prints); each is rendered
    when the step prints.
    *recover* is a line, an argv rendered the same way, a function returning the line
    (or None), or None for a step that needs no undoing.

    *final_cleanup* marks a last step that only tidies up once the work is done and
    proven (teardown deleting the emptied volume): when it fails, the steps before it
    need no undo, and undoing them would take the finished work apart, so its
    *recover* is printed alone, as the retry.
    """

    display: Union[str, Sequence[Arg], Callable[[], str]]
    action: Callable[[], None]
    recover: Recover = None
    final_cleanup: bool = False

    def shown(self) -> str:
        """The display as it prints now: slots rendered as far as they are known."""
        if isinstance(self.display, str):
            return self.display
        if callable(self.display):
            return self.display()
        return render(self.display)

    def recovery_line(self) -> Optional[str]:
        """The recovery command as it prints now, or None if none is needed.

        Never raises for a slot that is still unset: a recovery block that broke would
        hide the very commands a person needs after a failure.
        """
        if self.recover is None or isinstance(self.recover, str):
            return self.recover
        if callable(self.recover):
            return _call_recover(self.recover)
        return render(self.recover)


def _call_recover(recover: Callable[[], Optional[str]]) -> Optional[str]:
    """A callable recovery's line, or a note saying why it could not be worked out.

    A recovery decided as it prints may read the disk (whether a folder is still a
    mount point), and a read that fails must not end the block in a traceback.
    """
    try:
        return recover()
    except (ToolError, OSError) as exc:
        # A comment, so a pasted block runs none of the error text, which may hold a path.
        return f"# a recovery command could not be worked out: {comment_text(exc)}"


def command_step(
    host: Host,
    argv: Sequence[Arg],
    timeout: float,
    recover: Recover = None,
    confirm: Optional[Callable[[], None]] = None,
    *,
    hint: Optional[str] = None,
    final_cleanup: bool = False,
) -> Step:
    """A step that runs *argv* on *host*, then *confirm*, which raises if it did not take.

    A command that exits zero has only been dispatched; *confirm* reads the state back
    so the step is done only once the result is seen, and may fill a ``Slot`` with what
    it read; its failure is a ``ConfirmFailed``. Slots in *argv* are read when the step
    runs. *hint*, if given, follows the error on its own line when the command
    itself fails (not its confirm): what the operator can do about it.
    """
    command = tuple(argv)

    def act() -> None:
        try:
            run_checked(host, resolve(command), timeout)
        except ToolError as exc:
            if hint is None:
                raise
            raise ToolError(f"{exc}\n{hint}") from exc
        if confirm is not None:
            confirmed(confirm)

    return Step(command, act, recover, final_cleanup)


def _print_dry_run(steps: Sequence[Step], out: TextIO) -> int:
    """List every step, numbered, and say nothing was done."""
    for number, step in enumerate(steps, start=1):
        print(f"  {number}. {step.shown()}", file=out)
    print(DRY_RUN_FOOTER, file=out)
    return EXIT_OK


def _ran_line(step: Step, number: int) -> Optional[str]:
    """The recovery of failed step *number*, whose command ran, marked as such."""
    line = step.recovery_line()
    return None if line is None else line + _RAN_NOTE.format(number=number)


def _print_retry(step: Step, out: TextIO) -> None:
    """A failed final cleanup's recovery: its own line only, as the retry."""
    print(_FINAL_HEADER, file=out)
    line = step.recovery_line()
    print(f"  {line if line is not None else '(nothing to retry)'}", file=out)
    print(_NOT_UNDONE, file=out)


def _print_recovery(done: Sequence[Step], ran: Optional[str], out: TextIO) -> None:
    """The recovery commands, most recent first.

    *ran* is the failed step's own line when its command ran; the completed steps'
    lines follow it.
    """
    print(_RECOVERY_HEADER, file=out)
    rendered = (step.recovery_line() for step in reversed(done))
    lines = [line for line in (ran, *rendered) if line is not None]
    if not lines:
        print(_NOTHING_TO_UNDO, file=out)
    for line in lines:
        print(f"  {line}", file=out)
    print(_NOT_UNDONE, file=out)


def run_plan(steps: Sequence[Step], apply: bool, out: TextIO) -> int:
    """Print *steps* as a dry run, or perform them in order with *apply*.

    Returns ``EXIT_OK`` when every step was printed or completed, and ``EXIT_FAIL``
    after a step raised ``ToolError`` or ``OSError``, once the recovery is printed;
    a ``ConfirmFailed`` step's own recovery is printed with it. Any other exception
    is a defect and propagates.
    """
    if not apply:
        return _print_dry_run(steps, out)
    done: list[Step] = []
    for number, step in enumerate(steps, start=1):
        print(f"  {number}. {step.shown()}", file=out)
        try:
            step.action()
        except (ToolError, OSError) as exc:
            print(f"FAILED at step {number}: {exc}", file=out)
            if step.final_cleanup:
                _print_retry(step, out)
                return EXIT_FAIL
            ran = _ran_line(step, number) if isinstance(exc, ConfirmFailed) else None
            _print_recovery(done, ran, out)
            return EXIT_FAIL
        done.append(step)
    print(f"Done: all {len(steps)} steps completed.", file=out)
    return EXIT_OK
