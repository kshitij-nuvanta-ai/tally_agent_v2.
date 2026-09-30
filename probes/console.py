"""How the runner talks to whoever performs the pauses: a person at the Tally UI, or the automated operator (S0-D9)."""
from __future__ import annotations

from typing import Protocol

from probes.actions import Action


class ProbeIO(Protocol):
    interactive: bool

    def wait(self, instruction: str, action: Action | None = None) -> None: ...

    def ask(self, prompt: str, action: Action | None = None) -> str: ...

    def say(self, message: str) -> None: ...


class ConsoleIO:
    """A person at the Tally UI. The Action is for the automated operator; a person reads the instruction."""

    run_mode = "manual"

    def __init__(self, interactive: bool = True):
        self.interactive = interactive

    def wait(self, instruction: str, action: Action | None = None) -> None:
        input(f"\n>>> {instruction}\n    Press Enter when done... ")

    def ask(self, prompt: str, action: Action | None = None) -> str:
        return input(f"\n>>> {prompt}\n    > ").strip()

    def say(self, message: str) -> None:
        print(message, flush=True)
