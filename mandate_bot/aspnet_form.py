"""Helpers for driving an ASP.NET WebForms __doPostBack pager with requests."""
from __future__ import annotations

from bs4 import BeautifulSoup


def extract_form_fields(soup: BeautifulSoup, form_id: str = "aspnetForm") -> dict:
    """Collect every input/select/textarea value from the page's postback form,
    exactly as a browser would submit it (viewstate, event validation, etc.)."""
    form = soup.find("form", id=form_id)
    fields: dict[str, str] = {}

    for tag in form.find_all(["input", "textarea"]):
        name = tag.get("name")
        if not name:
            continue
        tag_type = (tag.get("type") or "text").lower()
        if tag_type in ("checkbox", "radio") and not tag.has_attr("checked"):
            continue
        if tag_type in ("submit", "button", "image"):
            continue
        fields[name] = tag.get("value", "")

    for tag in form.find_all("select"):
        name = tag.get("name")
        if not name:
            continue
        selected = tag.find("option", selected=True) or tag.find("option")
        fields[name] = selected.get("value", "") if selected else ""

    return fields


def build_postback_payload(soup: BeautifulSoup, event_target: str, event_argument: str = "") -> dict:
    fields = extract_form_fields(soup)
    fields["__EVENTTARGET"] = event_target
    fields["__EVENTARGUMENT"] = event_argument
    return fields
