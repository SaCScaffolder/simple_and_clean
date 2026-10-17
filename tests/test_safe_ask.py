"""Tests for the module-level `_safe_ask` abort / EOF / KeyboardInterrupt
helper. These pin the contract that every interactive surface in cli.py
flows through.

Background
----------
Before the questionary migration, the CLI had function-scoped
`_safe_prompt` / `_safe_confirm` closures in `_prompt_missing_init_values`
and `_prompt_refresh_overrides` that caught abort and printed "aborted."
on the way out. The questionary migration collapsed both into a single
module-level `_safe_ask(questionary_fn, *args, **kwargs)` that calls
`questionary_fn(...).ask()` and translates `None` (the value
questionary returns on Ctrl-C / EOF) into the same "aborted." exit
pattern.

Two subtle behaviors are pinned here:

1. We pass `kbi_msg=""` to `.ask()` to suppress questionary's default
   `"\nCancelled by user\n"` message. Without it, a user hitting Ctrl-C
   would see two abort messages -- questionary's, then ours. With it,
   the single "aborted." line is the canonical one across the whole
   CLI (matches the pre-questionary behavior exactly).

2. The KeyboardInterrupt + EOFError catch in `_safe_ask` is defensive:
   questionary's `.ask()` already catches KeyboardInterrupt internally
   and returns None. The explicit catch is for the case where
   `questionary_fn(...)` itself (not `.ask()`) raises -- e.g. if the
   user constructs an invalid Choice or the prompt-toolkit layer
   raises an OSError mid-init.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from servicectl.cli import _safe_ask


class TestSafeAsk:
    """The `_safe_ask(questionary_fn, *args, **kwargs)` helper.

    Tests use `sys.exit` is fine because Click's test runner catches
    SystemExit; CliRunner isn't strictly needed for these (we just
    call _safe_ask directly and assert on SystemExit), but CliRunner
    is the convention so the tests fit in with the rest of the suite.
    """

    def test_returns_value_when_questionary_returns_value(self):
        """The happy path: questionary returns the chosen value, and
        _safe_ask passes it through verbatim."""
        result = _safe_ask(lambda *a, **kw: _FakePrompt("alpha"))
        assert result == "alpha"

    def test_kbi_msg_is_passed_empty_to_suppress_default_message(self):
        """questionary's default Ctrl-C message is `\\nCancelled by user\\n`.
        _safe_ask must pass `kbi_msg=""` to `.ask()` so the user only
        sees our single `aborted.` line, not two stacked messages.
        Regression-tested because the default is silent in CliRunner
        (stdout is captured) and easy to break without noticing."""
        with patch("servicectl.cli.questionary") as mock_questionary:
            mock_prompt = _FakePrompt("anything")
            mock_select = mock_questionary.select  # any function will do
            mock_select.return_value = mock_prompt
            _safe_ask(mock_select, "Pick one:", choices=["a", "b"])
        # .ask() must have been called with kbi_msg=""
        assert mock_prompt.ask_calls == [""]
        # ...and NOT with the questionary default (which is "\nCancelled by user\n")
        for call in mock_prompt.ask_calls:
            assert call != "\nCancelled by user\n"

    def test_none_return_triggers_aborted_message_and_exit(self):
        """questionary returns None on Ctrl-C / EOF. _safe_ask must
        print the canonical "aborted." line and call sys.exit(1), the
        same exit code the pre-questionary `_safe_prompt` used."""
        runner = CliRunner()
        with patch("sys.exit") as mock_exit:
            with runner.isolated_filesystem():
                with patch("servicectl.cli.err_console") as mock_err:
                    result = _safe_ask(lambda *a, **kw: _FakePrompt(None))
        # Result is irrelevant -- _safe_ask doesn't return when result is None,
        # it sys.exits. So we just assert the side effects.
        mock_err.print.assert_called_once()
        printed = mock_err.print.call_args.args[0]
        assert "aborted" in printed.lower()
        mock_exit.assert_called_once_with(1)

    def test_keyboard_interrupt_is_caught_and_prints_aborted(self):
        """Defensive: if `questionary_fn(...)` itself (not `.ask()`)
        raises KeyboardInterrupt -- e.g. if a questionary internal
        somehow propagates it -- we still print `aborted.` and exit."""
        def _raise_kbi(*a, **kw):
            raise KeyboardInterrupt
        with patch("sys.exit") as mock_exit:
            with patch("servicectl.cli.err_console") as mock_err:
                _safe_ask(_raise_kbi)
        mock_err.print.assert_called_once()
        assert "aborted" in mock_err.print.call_args.args[0].lower()
        mock_exit.assert_called_once_with(1)

    def test_eoferror_is_caught_and_prints_aborted(self):
        """Defensive: EOF on stdin before the user answers -- the
        `piped stdin` / `git push | refresh` case -- is treated the
        same as Ctrl-C: `aborted.` and exit 1. (Refresh's actual
        piped-stdin behavior is to skip the prompt entirely, not to
        print `aborted.` -- this catch is for the case where the
        prompt is *already* asking and the pipe ends mid-prompt.)"""
        def _raise_eof(*a, **kw):
            raise EOFError
        with patch("sys.exit") as mock_exit:
            with patch("servicectl.cli.err_console") as mock_err:
                _safe_ask(_raise_eof)
        mock_err.print.assert_called_once()
        assert "aborted" in mock_err.print.call_args.args[0].lower()
        mock_exit.assert_called_once_with(1)

    def test_oserror_is_caught_and_prints_aborted(self):
        """Defensive: when questionary's prompt-toolkit layer raises
        OSError on a stdin pipe / detached console (e.g. `[Errno 22]
        Invalid argument` from the Windows console buffer probe when
        stdin is a closed pipe -- the `git push | refresh` case where
        the pipe ends before questionary has finished initializing),
        we still print `aborted.` and exit cleanly. Without this
        catch the user sees a stack trace for an unrecoverable
        environment-level failure they cannot fix."""
        def _raise_oserror(*a, **kw):
            raise OSError(22, "Invalid argument")
        with patch("sys.exit") as mock_exit:
            with patch("servicectl.cli.err_console") as mock_err:
                _safe_ask(_raise_oserror)
        # Only one print: the canonical `aborted.` line. OSError
        # gets the same cheap treatment as KeyboardInterrupt / EOF
        # (no diagnostic dump) because it's an environment-level
        # failure, not a user-fixable config error.
        mock_err.print.assert_called_once()
        assert "aborted" in mock_err.print.call_args.args[0].lower()
        mock_exit.assert_called_once_with(1)

    def test_invalid_default_raises_valueerror_at_construct_is_caught(self):
        """questionary.select(choices=..., default=X) raises
        ValueError at construct time if X is not in the choices. This
        happens when a manually-edited `.servicectl.json` has a
        stale value (e.g. `deploy_target: "kubernetes"` after that
        option was removed from `_DEPLOY_TARGET_CHOICES`). Without
        this catch the user sees a stack trace; with it, they see
        the ValueError message + `aborted.` and a clean exit."""
        def _raise_invalid_default(*a, **kw):
            raise ValueError(
                "Invalid `default` value passed. The value "
                "(`kubernetes`) does not exist in the set of choices."
            )
        with patch("sys.exit") as mock_exit:
            with patch("servicectl.cli.err_console") as mock_err:
                _safe_ask(_raise_invalid_default)
        # First print: the error message itself
        first_print = mock_err.print.call_args_list[0].args[0]
        assert "kubernetes" in first_print
        assert "default" in first_print.lower()
        # Second print: the canonical `aborted.` line
        second_print = mock_err.print.call_args_list[1].args[0]
        assert "aborted" in second_print.lower()
        # Clean exit
        mock_exit.assert_called_once_with(1)


class _FakePrompt:
    """A minimal stand-in for a questionary prompt object.

    `questionary.select("msg", choices=[...])` returns an instance whose
    `.ask(kbi_msg="")` returns the chosen value (or None on Ctrl-C).
    This stand-in captures the `kbi_msg` argument so tests can assert
    on it.
    """

    def __init__(self, return_value: object) -> None:
        self.return_value = return_value
        self.ask_calls: list[object] = []

    def ask(self, *args, **kwargs) -> object:
        self.ask_calls.append(kwargs.get("kbi_msg", "<unset>"))
        return self.return_value
