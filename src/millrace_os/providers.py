"""Small in-process provider boundaries.

The public OS contracts intentionally do not describe provider-private facts.
Adapters expose their own read models behind these structural protocols.  A
read-only provider can be recreated and reconciled without owning the process
or session it observes.
"""

from typing import Protocol

from .contracts import ActionInput, ActionOutput, ProviderInfo


class Provider(Protocol):
    """A built-in adapter reports its own authority and typed availability.

    Concrete read DTOs and event cursors belong to their separately qualified
    adapter. OS event cursors must never be substituted for provider cursors.
    """

    def health(self) -> ProviderInfo: ...
    def invoke(self, action_id: str, request: ActionInput) -> ActionOutput: ...


class ReadOnlyProvider(Protocol):
    """Provider lifecycle used by adapters that only observe an authority.

    ``reconcile`` must perform a fresh bounded read.  ``reset`` only drops
    replaceable adapter state; it must not mutate the observed authority.
    Concrete return types remain adapter-owned and are deliberately typed as
    ``object`` at this shared boundary.
    """

    def health(self) -> object: ...
    def reconcile(self, *, hint: str | None = None) -> object: ...
    def reset(self) -> None: ...
