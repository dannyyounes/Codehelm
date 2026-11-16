"""Validated screen specs for DesignFlow mockups.

A provider may propose a spec. The renderer never inserts provider text as HTML.
"""

from dataclasses import dataclass
import json
import re

KINDS = (
    "heading", "note", "text", "email", "password",
    "textarea", "select", "checkbox", "button",
)
INPUT_KINDS = {"text", "email", "password", "textarea"}
SPEC_KEYS = {"title", "brief", "fields"}
FIELD_KEYS = {"uid", "kind", "label", "placeholder", "required", "options", "help"}
_UID = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_SCREEN_ID = _UID


class DesignFlowError(ValueError):
    """The screen spec, comment, or agent response cannot be used."""


@dataclass(frozen=True)
class Field:
    uid: str
    kind: str
    label: str
    placeholder: str = ""
    required: bool = False
    options: tuple[str, ...] = ()
    help: str = ""

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "kind": self.kind,
            "label": self.label,
            "placeholder": self.placeholder,
            "required": self.required,
            "options": list(self.options),
            "help": self.help,
        }


@dataclass(frozen=True)
class ScreenSpec:
    title: str
    brief: str
    fields: tuple[Field, ...]

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "brief": self.brief,
            "fields": [field.to_dict() for field in self.fields],
        }


@dataclass(frozen=True)
class MockupComment:
    id: int
    screen_id: str
    revision: int
    element_uid: str
    text: str
    created_at: str
    queued_at: str | None
    consumed_revision: int | None

    @property
    def queued(self) -> bool:
        return self.queued_at is not None and self.consumed_revision is None


@dataclass(frozen=True)
class Screen:
    id: str
    revision: int
    spec: ScreenSpec
    html_path: str
    updated_at: str


def screen_id_from_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    if not slug or not slug[0].isalpha():
        slug = ("screen-" + slug).strip("-")
    slug = slug[:40].strip("-")
    if not _SCREEN_ID.fullmatch(slug):
        raise DesignFlowError("Screen title must produce an id of letters, numbers, and dashes")
    return slug


def check_screen_id(screen_id: str) -> str:
    if not isinstance(screen_id, str) or not _SCREEN_ID.fullmatch(screen_id):
        raise DesignFlowError("Screen id must be 1 to 41 characters of lowercase letters, numbers, and dashes")
    return screen_id


def _text(value, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise DesignFlowError(f"{field} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise DesignFlowError(f"{field} must be nonempty text")
    if len(cleaned) > limit:
        raise DesignFlowError(f"{field} is longer than {limit} characters")
    return cleaned


def _optional_text(value, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DesignFlowError(f"{field} must be text")
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise DesignFlowError(f"{field} is longer than {limit} characters")
    return cleaned


def parse_field(data: dict) -> Field:
    if not isinstance(data, dict):
        raise DesignFlowError("Each field must be an object")
    extra = set(data) - FIELD_KEYS
    if extra:
        raise DesignFlowError(f"Unknown field keys: {', '.join(sorted(extra))}")
    uid = data.get("uid")
    if not isinstance(uid, str) or not _UID.fullmatch(uid) or uid == "screen":
        raise DesignFlowError("Field uid must be lowercase letters, numbers, and dashes, and cannot be 'screen'")
    kind = data.get("kind")
    if kind not in KINDS:
        raise DesignFlowError(f"Field kind must be one of: {', '.join(KINDS)}")
    label = _text(data.get("label"), "label", 200)
    placeholder = _optional_text(data.get("placeholder", ""), "placeholder", 200)
    help_text = _optional_text(data.get("help", ""), "help", 500)
    required = data.get("required", False)
    if not isinstance(required, bool):
        raise DesignFlowError("required must be true or false")
    options = data.get("options", [])
    if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
        raise DesignFlowError("options must be a list of strings")
    cleaned_options = tuple(item.strip() for item in options)
    if any(not item for item in cleaned_options):
        raise DesignFlowError("options must be nonempty strings")
    if any(len(item) > 80 for item in cleaned_options):
        raise DesignFlowError("each option is longer than 80 characters")
    if len(cleaned_options) > 20:
        raise DesignFlowError("a field can have at most 20 options")
    if kind == "select":
        if len(cleaned_options) < 1:
            raise DesignFlowError(f"{uid}: a select field needs at least one option")
        if placeholder:
            raise DesignFlowError(f"{uid}: a select field cannot have a placeholder")
    elif cleaned_options:
        raise DesignFlowError(f"{uid}: only a select field can have options")
    if placeholder and kind not in INPUT_KINDS:
        raise DesignFlowError(f"{uid}: {kind} cannot have a placeholder")
    if required and kind not in INPUT_KINDS | {"select", "checkbox"}:
        raise DesignFlowError(f"{uid}: {kind} cannot be required")
    return Field(uid, kind, label, placeholder, required, cleaned_options, help_text)


def parse_spec(data: dict) -> ScreenSpec:
    if not isinstance(data, dict):
        raise DesignFlowError("Screen spec must be a JSON object")
    extra = set(data) - SPEC_KEYS
    if extra:
        raise DesignFlowError(f"Unknown screen keys: {', '.join(sorted(extra))}")
    title = _text(data.get("title"), "title", 120)
    brief = _text(data.get("brief"), "brief", 4000)
    fields = data.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 40:
        raise DesignFlowError("Screen must contain between 1 and 40 fields")
    parsed = tuple(parse_field(item) for item in fields)
    seen = set()
    for field in parsed:
        if field.uid in seen:
            raise DesignFlowError(f"Duplicate field uid: {field.uid}")
        seen.add(field.uid)
    return ScreenSpec(title, brief, parsed)


def parse_model_spec(raw: str) -> ScreenSpec:
    """Accept only a single JSON object. Fenced or prose output is refused."""
    text = (raw or "").strip()
    if not text:
        raise DesignFlowError("Model output must be a single JSON object")
    if text.startswith("```"):
        raise DesignFlowError("Model output must be raw JSON, not a code fence")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DesignFlowError("Model output must be a single JSON object") from exc
    return parse_spec(data)


def parse_field_arg(text: str) -> Field:
    """Parse uid|label|kind|required|placeholder|options|help."""
    parts = text.split("|", 6)
    if len(parts) < 3:
        raise DesignFlowError("A field needs uid|label|kind")
    while len(parts) < 7:
        parts.append("")
    uid, label, kind, required, placeholder, options, help_text = parts
    flag = required.strip().lower()
    if flag not in {"", "required", "optional"}:
        raise DesignFlowError("Field required flag must be required, optional, or empty")
    option_list = [item.strip() for item in options.split(",")] if options.strip() else []
    return parse_field({
        "uid": uid.strip(),
        "label": label.strip(),
        "kind": kind.strip(),
        "required": flag == "required",
        "placeholder": placeholder.strip(),
        "options": option_list,
        "help": help_text.strip(),
    })


def synthesized_spec(title: str, brief: str) -> ScreenSpec:
    """A reviewable screen when the caller did not list fields."""
    return parse_spec({
        "title": title,
        "brief": brief,
        "fields": [
            {"uid": "title", "kind": "heading", "label": title},
            {"uid": "brief", "kind": "note", "label": "About this screen", "help": brief[:500]},
        ],
    })


def clean_comment_text(text: str) -> str:
    if not isinstance(text, str):
        raise DesignFlowError("Comment text must be text")
    cleaned = " ".join(text.replace("\x00", "").split())
    if not cleaned:
        raise DesignFlowError("Comment text must not be empty")
    if len(cleaned) > 2000:
        raise DesignFlowError("Comment text is longer than 2000 characters")
    return cleaned
