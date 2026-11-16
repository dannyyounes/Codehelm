"""DesignFlow CLI. Local mockups are written immediately. The provider runs only with --execute."""

from pathlib import Path
import json

import typer

from codehelm.designflow.loop import (
    draft_screen, draft_with_provider, feedback_lines, revise_screen, send_comments,
)
from codehelm.designflow.server import serve_review
from codehelm.designflow.spec import (
    DesignFlowError, check_screen_id, parse_field_arg, parse_spec, screen_id_from_title,
    synthesized_spec,
)
from codehelm.designflow.store import DesignFlowStore, default_paths
from codehelm.providers.adapters import ProviderError

designflow_app = typer.Typer(
    help="DesignFlow screen and form mockups, inline comments, and agent revision.",
    no_args_is_help=True,
)


def _store() -> DesignFlowStore:
    database, root = default_paths()
    store = DesignFlowStore(database, root)
    store.initialize()
    return store


def _fail(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1)


@designflow_app.command("draft")
def draft(
    title: str = typer.Option("", "--title", help="Screen title"),
    brief: str = typer.Option("", "--brief", help="What the screen is for"),
    screen_id: str = typer.Option("", "--id", help="Screen id. Defaults to a slug of the title"),
    field: list[str] | None = typer.Option(None, "--field", help="uid|label|kind|required|placeholder|options|help"),
    spec: Path | None = typer.Option(None, "--spec", help="JSON screen spec to render without a provider"),
    provider: str = typer.Option("claude", help="Provider used only with --execute"),
    repo: Path = typer.Option(Path("."), help="Workspace for the provider"),
    timeout: int = typer.Option(600, min=1, max=3600),
    transport: str = typer.Option("cli", "--transport", help="Provider transport: cli (default) or http"),
    execute: bool = typer.Option(False, "--execute", help="Ask the provider for the screen JSON"),
) -> None:
    """Write a screen or form mockup from fields, a spec file, or a provider."""
    try:
        store = _store()
        if spec is not None and (execute or field):
            raise DesignFlowError("--spec cannot be combined with --field or --execute")
        if spec is not None:
            data = json.loads(spec.read_text(encoding="utf-8"))
            parsed = parse_spec(data)
            if title:
                parsed = parse_spec({**parsed.to_dict(), "title": title})
            if brief:
                parsed = parse_spec({**parsed.to_dict(), "brief": brief})
            chosen = check_screen_id(screen_id) if screen_id else screen_id_from_title(parsed.title)
            screen = draft_screen(store, chosen, parsed)
        else:
            if not title or not brief:
                raise DesignFlowError("draft needs --title and --brief, or --spec")
            hints = tuple(parse_field_arg(item) for item in (field or []))
            chosen = check_screen_id(screen_id) if screen_id else screen_id_from_title(title)
            if execute:
                screen = draft_with_provider(
                    store, chosen, title, brief, hints, provider=provider, workspace=repo,
                    timeout=timeout, transport=transport)
            else:
                parsed = parse_spec({
                    "title": title,
                    "brief": brief,
                    "fields": [item.to_dict() for item in hints],
                }) if hints else synthesized_spec(title, brief)
                screen = draft_screen(store, chosen, parsed)
    except (DesignFlowError, ProviderError, OSError, json.JSONDecodeError) as exc:
        _fail(exc)
    typer.echo(f"screen: {screen.id}")
    typer.echo(f"revision: {screen.revision}")
    typer.echo(f"mockup: {screen.html_path}")


@designflow_app.command("show")
def show(screen_id: str) -> None:
    """Print one mockup's revision, path, and open comments."""
    try:
        typer.echo(feedback_lines(_store(), screen_id))
    except DesignFlowError as exc:
        _fail(exc)


@designflow_app.command("comment")
def comment(
    screen_id: str,
    text: str = typer.Option(..., "--text", help="Comment about the selected element"),
    element: str = typer.Option("screen", "--element", help="Field uid, or screen for the whole mockup"),
) -> None:
    """Store one inline comment. This does not send it to an agent."""
    try:
        store = _store()
        stored = store.add_comment(screen_id, element, text)
    except DesignFlowError as exc:
        _fail(exc)
    typer.echo(f"screen: {screen_id}")
    typer.echo(f"comment: {stored.element_uid}\t{stored.text}")


@designflow_app.command("feedback")
def feedback(screen_id: str) -> None:
    """Print open comments for a mockup."""
    try:
        typer.echo(feedback_lines(_store(), screen_id))
    except DesignFlowError as exc:
        _fail(exc)


@designflow_app.command("send")
def send(screen_id: str) -> None:
    """Queue open comments for the agent. This does not launch a provider."""
    try:
        queued = send_comments(_store(), screen_id)
    except DesignFlowError as exc:
        _fail(exc)
    typer.echo(f"screen: {screen_id}")
    typer.echo(f"queued: {len(queued)}")
    typer.echo(f"next: codehelm designflow revise {screen_id} --execute")


@designflow_app.command("revise")
def revise(
    screen_id: str,
    provider: str = typer.Option("claude", help="Provider that revises the mockup"),
    repo: Path = typer.Option(Path("."), help="Workspace for the provider"),
    timeout: int = typer.Option(600, min=1, max=3600),
    transport: str = typer.Option("cli", "--transport", help="Provider transport: cli (default) or http"),
    execute: bool = typer.Option(False, "--execute", help="Send queued comments to the provider"),
) -> None:
    """Revise a mockup from comments that have been sent."""
    try:
        store = _store()
        screen = store.require(screen_id)
        comments = store.queued_comments(screen_id)
        if not comments:
            raise DesignFlowError("No comments have been sent to the agent. Run designflow send first.")
        if not execute:
            typer.echo(feedback_lines(store, screen_id))
            typer.echo("Dry run. Add --execute to send these comments to the agent.")
            return
        updated, addressed = revise_screen(
            store, screen_id, provider=provider, workspace=repo, timeout=timeout, transport=transport)
    except (DesignFlowError, ProviderError, OSError) as exc:
        _fail(exc)
    typer.echo(f"screen: {updated.id}")
    typer.echo(f"revision: {updated.revision}")
    typer.echo(f"addressed: {addressed}")
    typer.echo(f"mockup: {updated.html_path}")


@designflow_app.command("serve")
def serve(
    screen_id: str,
    port: int = typer.Option(8765, "--port", min=0, max=65535, help="Loopback port. 127.0.0.1 only"),
) -> None:
    """Open one mockup on 127.0.0.1 for inline comments. Does not launch a provider."""
    try:
        serve_review(_store(), screen_id, port)
    except (DesignFlowError, OSError) as exc:
        _fail(exc)
