"""The exit codes every mode returns, and the one error type the library raises.

Entry point assumptions: pure definitions, imported by every other module.
"""

#: Every check passed, or every step of a plan was printed or performed.
EXIT_OK = 0
#: A status check needs attention (a host command failing during a check is one), or
#: a step of an applied plan failed.
EXIT_FAIL = 1
#: A usage error, a folder that is not configured, or a run refused before any check
#: or step, a host command failing while validating included.
EXIT_USAGE = 2


class ToolError(Exception):
    """A host command or read failed; the message is written for the operator."""
