"""Fail-closed execution backend for native laptops without an OS sandbox."""

from typing import Any

from .base import Sandbox, SandboxResult


class DisabledSandbox(Sandbox):
    name = "disabled"
    provides_isolation = False

    @classmethod
    def available(cls) -> bool:
        return True

    async def run(self, argv: list[str], **kwargs: Any) -> SandboxResult:
        return SandboxResult(
            exit_code=126,
            stderr="Code execution is disabled: install the offline Docker sandbox or Linux bubblewrap. File tools and the bounded calculator remain available.",
            backend=self.name,
        )
