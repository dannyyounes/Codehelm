"""Provider-neutral planning: structured Epic/Stories and dependency validation."""
from dataclasses import dataclass
from pathlib import Path
import json
import re

from codehelm.providers.adapters import CLIProvider


class PlanningError(ValueError):
    pass


_JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9]+-[1-9][0-9]*$")


@dataclass(frozen=True)
class Story:
    id: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    jira_key: str = ""


@dataclass(frozen=True)
class Plan:
    epic_title: str
    epic_description: str
    stories: tuple[Story, ...]

    def to_dict(self) -> dict:
        return {
            "epic_title": self.epic_title,
            "epic_description": self.epic_description,
            "stories": [
                {"id": s.id, "title": s.title, "description": s.description,
                 "acceptance_criteria": list(s.acceptance_criteria),
                 "dependencies": list(s.dependencies),
                 **({"jira_key": s.jira_key} if s.jira_key else {})}
                for s in self.stories
            ],
        }


def parse_plan(raw: str) -> Plan:
    """Fail closed on malformed output, missing fields, cycles or unknown dependencies."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanningError("Planner must return a single JSON object") from exc
    if not isinstance(data, dict):
        raise PlanningError("Plan must be a JSON object")
    def nonempty(value, field):
        if not isinstance(value, str) or not value.strip():
            raise PlanningError(f"{field} must be nonempty text")
        return value.strip()
    epic_title = nonempty(data.get("epic_title"), "epic_title")
    epic_description = nonempty(data.get("epic_description"), "epic_description")
    items = data.get("stories")
    if not isinstance(items, list) or not 1 <= len(items) <= 30:
        raise PlanningError("Plan must contain between 1 and 30 stories")
    stories = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise PlanningError("Each story must be an object")
        ident = nonempty(item.get("id"), "id")
        if not re.fullmatch(r"S[1-9][0-9]*", ident) or ident in seen:
            raise PlanningError("Story IDs must be unique S1, S2, ... identifiers")
        seen.add(ident)
        criteria = item.get("acceptance_criteria")
        deps = item.get("dependencies")
        if not isinstance(criteria, list) or not criteria or not all(
            isinstance(v, str) and v.strip() for v in criteria
        ):
            raise PlanningError(f"{ident}: acceptance criteria must be nonempty strings")
        if not isinstance(deps, list) or not all(isinstance(v, str) for v in deps):
            raise PlanningError(f"{ident}: dependencies must be story ID strings")
        if len(deps) != len(set(deps)):
            raise PlanningError(f"{ident}: duplicate dependencies")
        stories.append(Story(ident, nonempty(item.get("title"), "title"),
                             nonempty(item.get("description"), "description"),
                             tuple(v.strip() for v in criteria), tuple(deps),
                             _story_jira_key(item, ident)))
    graph = {s.id: s.dependencies for s in stories}
    visiting, visited = set(), set()
    def visit(node):
        if node in visiting:
            raise PlanningError("Dependency cycle detected")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph[node]:
            if dep not in graph:
                raise PlanningError(f"Unknown dependency: {dep}")
            visit(dep)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)
    return Plan(epic_title, epic_description, tuple(stories))


def _story_jira_key(item: dict, ident: str) -> str:
    """Optional issue key. plan-publish writes it after Jira returns one."""
    if "jira_key" not in item or item["jira_key"] is None:
        return ""
    key = item["jira_key"]
    if not isinstance(key, str) or not _JIRA_KEY.fullmatch(key):
        raise PlanningError(f"{ident}: jira_key must look like DEV-123")
    return key


def generate_plan(goal: str, provider: str, workspace: Path, timeout: int = 600) -> Plan:
    if not goal.strip():
        raise PlanningError("Goal cannot be empty")
    prompt = (
        "You are a software engineering planner. Decompose the following goal "
        "into an Epic and independently verifiable implementation Stories. "
        "Include dependencies by local story ID, measurable acceptance criteria, "
        "and security/testing work where appropriate. "
        "Return ONLY a JSON object with keys epic_title, epic_description, stories. "
        "Each story has id (S1, S2, ...), title, description, "
        "acceptance_criteria (nonempty string array), dependencies (story ID array). "
        "Maximum 30 stories. No markdown or code fences. Do not edit files. "
        "Goal:\n" + goal
    )
    result = CLIProvider(provider).execute(prompt, Path(workspace), timeout)
    if result.exit_code:
        raise PlanningError(f"Planner failed: {result.error[:500]}")
    return parse_plan(result.output)


def save_plan(plan: Plan, destination: Path) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PlanningError(f"Plan already exists: {destination}")
    destination.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")


def load_plan(path: Path) -> Plan:
    return parse_plan(Path(path).read_text(encoding="utf-8"))


def assign_published_keys(plan: Plan, mapping: dict[str, str]) -> Plan:
    """Copy story keys Jira returned. A missing or malformed key is refused."""
    stories = []
    for story in plan.stories:
        key = mapping.get(story.id)
        if not isinstance(key, str) or not _JIRA_KEY.fullmatch(key):
            raise PlanningError(f"{story.id}: Jira did not return a usable issue key")
        if story.jira_key and story.jira_key != key:
            raise PlanningError(f"{story.id}: refusing to replace jira_key {story.jira_key}")
        stories.append(Story(
            story.id, story.title, story.description,
            story.acceptance_criteria, story.dependencies, key))
    return Plan(plan.epic_title, plan.epic_description, tuple(stories))


def replace_plan(plan: Plan, destination: Path) -> None:
    """Overwrite a plan file. The caller has already created the Jira issues."""
    destination = Path(destination)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def publish_plan_to_jira(plan: Plan, project: str, client) -> dict[str, str]:
    """Create Epic and Stories in dependency order; no issue transitions."""
    epic = client.create_issue(project, plan.epic_title, "Epic", plan.epic_description)
    epic_key = epic.get("key")
    if not epic_key:
        raise PlanningError("Jira did not return an Epic key; inspect Jira before retrying")
    mapping = {"epic": epic_key}
    remaining = {s.id: s for s in plan.stories}
    while remaining:
        ready = [s for s in remaining.values() if all(d in mapping for d in s.dependencies)]
        if not ready:
            raise PlanningError("Could not resolve story dependencies")
        for story in ready:
            description = story.description + "\n\nAcceptance criteria:\n" + "\n".join(
                "- " + c for c in story.acceptance_criteria
            )
            if story.dependencies:
                description += "\n\nDepends on: " + ", ".join(mapping[d] for d in story.dependencies)
            created = client.create_issue(project, story.title, "Story",
                                          description, parent=epic_key)
            key = created.get("key")
            if not key:
                raise PlanningError(f"Jira did not return a key for {story.id}")
            mapping[story.id] = key
            del remaining[story.id]
    return mapping
