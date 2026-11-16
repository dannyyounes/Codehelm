"""Render a DesignFlow screen spec as one reviewable HTML page.

Every caller-supplied string is escaped. The only script in the page is the
review client below.
"""

from html import escape
import json

from codehelm.designflow.spec import MockupComment, ScreenSpec

_REVIEW_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 16px/1.45 "Segoe UI", system-ui, sans-serif;
  background: #f3efe6;
  color: #1d1a16;
}
.designflow-layout {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  align-items: flex-start;
  max-width: 1080px;
  margin: 0 auto;
  padding: 32px 20px 64px;
}
.designflow-frame {
  flex: 1 1 420px;
  background: #fffdf8;
  border: 1px solid #e2d8c8;
  border-radius: 16px;
  padding: 28px 28px 32px;
  box-shadow: 0 12px 40px rgba(70, 48, 20, 0.08);
}
.designflow-kicker {
  margin: 0 0 8px;
  color: #8a5a12;
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.designflow-frame h1 { margin: 0 0 8px; font-size: 28px; }
.designflow-brief { margin: 0 0 24px; color: #5c5348; }
.designflow-field {
  margin: 0 0 16px;
  padding: 8px;
  border-radius: 10px;
  border: 1px solid transparent;
}
.designflow-field.is-selected { border-color: #c47b12; background: #fff7ea; }
.designflow-field label, .designflow-field h2, .designflow-note {
  display: block;
  margin: 0 0 6px;
  font-size: 14px;
  font-weight: 650;
}
.designflow-field .required::after { content: " *"; color: #9a3412; }
.designflow-field input, .designflow-field textarea, .designflow-field select {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #d9d0c3;
  border-radius: 8px;
  font: inherit;
  background: white;
}
.designflow-field textarea { min-height: 96px; resize: vertical; }
.designflow-help { margin: 6px 0 0; color: #6b6258; font-size: 13px; }
.designflow-check { display: flex; gap: 8px; align-items: center; font-weight: 500; }
.designflow-button {
  border: 0;
  border-radius: 8px;
  padding: 10px 16px;
  background: #1d1a16;
  color: white;
  font: inherit;
}
#designflow-panel {
  flex: 0 1 320px;
  background: white;
  border: 1px solid #e2d8c8;
  border-radius: 16px;
  padding: 16px;
}
#designflow-panel h2 { margin: 0 0 8px; font-size: 18px; }
#designflow-target { margin: 0 0 8px; color: #5c5348; }
#designflow-text {
  width: 100%;
  min-height: 88px;
  margin: 0 0 8px;
  padding: 8px;
  border: 1px solid #d9d0c3;
  border-radius: 8px;
  font: inherit;
}
.designflow-actions { display: flex; gap: 8px; margin-bottom: 12px; }
.designflow-actions button, #designflow-send {
  border: 0;
  border-radius: 8px;
  padding: 8px 12px;
  font: inherit;
  background: #c47b12;
  color: white;
}
#designflow-add { background: #1d1a16; }
#designflow-list { list-style: none; margin: 0; padding: 0; }
#designflow-list li { padding: 8px 0; border-top: 1px solid #eee4d6; }
#designflow-status { min-height: 1.4em; color: #5c5348; }
"""

_REVIEW_JS = """
(function () {
  var state = JSON.parse(document.getElementById("designflow-state").textContent);
  var selected = "screen";
  var target = document.getElementById("designflow-target");
  var box = document.getElementById("designflow-text");
  var list = document.getElementById("designflow-list");
  var status = document.getElementById("designflow-status");

  function labelFor(uid) {
    var found = state.elements.find(function (item) { return item.uid === uid; });
    return found ? found.label : uid;
  }

  function renderList() {
    list.replaceChildren();
    if (!state.comments.length) {
      var empty = document.createElement("li");
      empty.textContent = "No comments yet.";
      list.append(empty);
      return;
    }
    state.comments.forEach(function (item) {
      var row = document.createElement("li");
      var who = document.createElement("strong");
      who.textContent = item.label || item.element;
      row.append(who);
      if (item.queued) {
        var mark = document.createElement("em");
        mark.textContent = " sent";
        row.append(mark);
      }
      var body = document.createElement("div");
      body.textContent = item.text;
      row.append(body);
      list.append(row);
    });
  }

  function select(uid, node) {
    selected = uid;
    document.querySelectorAll(".is-selected").forEach(function (el) {
      el.classList.remove("is-selected");
    });
    if (node) node.classList.add("is-selected");
    target.textContent = "Commenting on " + labelFor(uid);
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest("#designflow-panel")) return;
    var node = event.target.closest("[data-designflow-uid]");
    if (!node) return;
    select(node.getAttribute("data-designflow-uid"), node);
  });

  function localOnly() { return location.protocol === "file:"; }

  function post(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
  }

  function saveDraft() {
    var text = box.value.trim();
    if (!text) return Promise.resolve();
    return post("/api/comments", {element: selected, text: text}).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error(body.error || "Could not save the comment");
        box.value = "";
        state.comments.push(body.comment);
        renderList();
      });
    });
  }

  document.getElementById("designflow-add").addEventListener("click", function () {
    if (localOnly()) {
      status.textContent = "Open this mockup with codehelm designflow serve to save comments.";
      return;
    }
    saveDraft().catch(function (error) { status.textContent = error.message; });
  });

  document.getElementById("designflow-send").addEventListener("click", function () {
    if (localOnly()) {
      status.textContent = "Open this mockup with codehelm designflow serve to send comments.";
      return;
    }
    saveDraft().then(function () {
      return post("/api/send", {});
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error(body.error || "Could not send comments");
        state.comments.forEach(function (item) { item.queued = true; });
        renderList();
        var noun = body.queued === 1 ? "comment" : "comments";
        status.textContent = "Sent " + body.queued + " " + noun
          + " to the agent queue. Run " + body.command + " and refresh this page.";
      });
    }).catch(function (error) { status.textContent = error.message; });
  });

  renderList();
  select("screen", document.querySelector("[data-designflow-uid='screen']"));
})();
"""


def _island(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _comment_payload(spec: ScreenSpec, comments: tuple[MockupComment, ...] | list[MockupComment]) -> list[dict]:
    labels = {field.uid: field.label for field in spec.fields}
    labels["screen"] = spec.title
    return [
        {
            "element": comment.element_uid,
            "label": labels.get(comment.element_uid, comment.element_uid),
            "text": comment.text,
            "queued": comment.queued,
        }
        for comment in comments
        if comment.consumed_revision is None
    ]


def render_field(field) -> str:
    uid = escape(field.uid, quote=True)
    label = escape(field.label)
    help_html = f'<p class="designflow-help">{escape(field.help)}</p>' if field.help else ""
    required = " required" if field.required else ""
    placeholder = escape(field.placeholder, quote=True)
    control_id = f"designflow-field-{uid}"
    if field.kind == "heading":
        inner = f"<h2>{label}</h2>{help_html}"
    elif field.kind == "note":
        inner = f'<p class="designflow-note">{label}</p>{help_html}'
    elif field.kind == "textarea":
        inner = (
            f'<label class="{required.strip()}" for="{control_id}">{label}</label>'
            f'<textarea id="{control_id}" placeholder="{placeholder}" autocomplete="off"></textarea>'
            f"{help_html}"
        )
    elif field.kind == "select":
        options = "".join(
            f'<option>{escape(option)}</option>' for option in field.options
        )
        inner = (
            f'<label class="{required.strip()}" for="{control_id}">{label}</label>'
            f'<select id="{control_id}">{options}</select>{help_html}'
        )
    elif field.kind == "checkbox":
        inner = (
            f'<label class="designflow-check"><input type="checkbox" autocomplete="off">'
            f"<span>{label}</span></label>{help_html}"
        )
    elif field.kind == "button":
        inner = f'<button class="designflow-button" type="button">{label}</button>{help_html}'
    else:
        inner = (
            f'<label class="{required.strip()}" for="{control_id}">{label}</label>'
            f'<input id="{control_id}" type="{escape(field.kind, quote=True)}" '
            f'placeholder="{placeholder}" autocomplete="off">{help_html}'
        )
    return (
        f'<div class="designflow-field" data-designflow-uid="{uid}" tabindex="0">'
        f"{inner}</div>"
    )


def render_screen(screen_id: str, revision: int, spec: ScreenSpec,
                  comments: tuple[MockupComment, ...] | list[MockupComment] = ()) -> str:
    open_comments = _comment_payload(spec, comments)
    elements = [{"uid": "screen", "label": spec.title}]
    elements.extend({"uid": field.uid, "label": field.label} for field in spec.fields)
    state = {
        "screen": screen_id,
        "revision": revision,
        "elements": elements,
        "comments": open_comments,
    }
    fields = "".join(render_field(field) for field in spec.fields)
    title = escape(spec.title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{_REVIEW_CSS}</style>
</head>
<body>
<div class="designflow-layout">
<article class="designflow-frame" data-designflow-uid="screen">
<p class="designflow-kicker">Screen mockup · revision {revision}</p>
<h1>{title}</h1>
<p class="designflow-brief">{escape(spec.brief)}</p>
<div class="designflow-form">{fields}</div>
</article>
<aside id="designflow-panel">
<h2>Comments</h2>
<p id="designflow-target">Click a field to comment on it.</p>
<textarea id="designflow-text" placeholder="What should change?"></textarea>
<div class="designflow-actions">
<button type="button" id="designflow-add">Add comment</button>
<button type="button" id="designflow-send">Send to agent</button>
</div>
<ul id="designflow-list"></ul>
<p id="designflow-status" aria-live="polite"></p>
</aside>
</div>
<script type="application/json" id="designflow-state">{_island(state)}</script>
<script>{_REVIEW_JS}</script>
</body>
</html>
"""
