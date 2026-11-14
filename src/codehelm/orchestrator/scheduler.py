"""Dependency-aware bounded batch dispatch; never equates agent exit with completion."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from codehelm.planning.planner import Plan, Story


@dataclass(frozen=True)
class DispatchResult:
    story_id: str
    task_id: str
    provider: str
    status: str
    detail: str = ""


def ready_stories(plan: Plan, completed: set[str], dispatched: set[str]) -> list[Story]:
    """Only explicitly completed prerequisite stories unlock dependents."""
    known = {s.id for s in plan.stories}
    if not completed <= known or not dispatched <= known:
        raise ValueError("Unknown story ID in completed or dispatched set")
    if completed & dispatched:
        raise ValueError("Completed and dispatched sets overlap")
    return [s for s in plan.stories if s.id not in completed | dispatched
            and set(s.dependencies) <= completed]


def dispatch_batch(
    plan: Plan, completed: set[str], dispatched: set[str],
    providers: list[str], task_prefix: str, max_parallel: int,
    runner: Callable[[str, str, str], object],
) -> list[DispatchResult]:
    """Dispatch one dependency-ready wave. Runner invokes the existing lifecycle."""
    if not 1 <= max_parallel <= 8:
        raise ValueError("max_parallel must be between 1 and 8")
    if not providers or any(not name.strip() for name in providers):
        raise ValueError("At least one provider is required")
    if not task_prefix or not task_prefix.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid task prefix")
    ready = ready_stories(plan, completed, dispatched)
    selected = ready[:max_parallel]
    def launch(index: int, story: Story) -> DispatchResult:
        provider = providers[index % len(providers)]
        task_id = f"{task_prefix}-{story.id}"
        prompt = (f"Implement story {story.id}: {story.title}\n"
                  f"{story.description}\nAcceptance criteria:\n" +
                  "\n".join(f"- {criterion}" for criterion in story.acceptance_criteria) +
                  "\nWork only on this story. Do not bypass review or quality gates.")
        try:
            result = runner(task_id, prompt, provider)
            return DispatchResult(story.id, task_id, provider,
                                  "awaiting_validation" if result.exit_code == 0 else "failed",
                                  result.error[:500])
        except Exception as exc:
            return DispatchResult(story.id, task_id, provider, "failed", str(exc)[:500])
    results = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = [pool.submit(launch, i, story) for i, story in enumerate(selected)]
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda result: [s.id for s in selected].index(result.story_id))
