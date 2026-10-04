"""Draft the tenant reply and the vendor dispatch message.

Uses the Anthropic API when a key is configured; otherwise falls back to
simple templates so the pipeline works end to end with no API key.
"""
from . import config
from .triage import load_prompt


def _llm_draft(template_name: str, values: dict) -> str | None:
    import anthropic

    template = load_prompt(template_name)
    prompt = template.format(**values)
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return None


def draft_tenant_reply(tenant_name: str, summary: str, questions: list[str]) -> str:
    if config.ANTHROPIC_API_KEY:
        drafted = _llm_draft("tenant_reply.txt", {
            "tenant_name": tenant_name,
            "summary": summary,
            "questions": "; ".join(questions) if questions else "none",
            "manager_name": "Property Management",
        })
        if drafted:
            return drafted
    # template fallback
    lines = [f"Hi {tenant_name}, thanks for letting us know — {summary}"]
    for q in questions:
        lines.append(q)
    lines.append("We'll take a look and follow up with next steps.")
    lines.append("— Property Management")
    return "\n".join(lines)


def draft_vendor_message(vendor_name: str, trade: str, property_name: str,
                         property_address: str, unit_label: str,
                         summary: str) -> str:
    if config.ANTHROPIC_API_KEY:
        drafted = _llm_draft("vendor_message.txt", {
            "vendor_name": vendor_name,
            "trade": trade,
            "property_name": property_name,
            "property_address": property_address,
            "unit_label": unit_label,
            "summary": summary,
            "access_notes": "coordinate with tenant for access",
        })
        if drafted:
            return drafted
    # template fallback
    return (
        f"Hi {vendor_name}, new {trade} request (draft — awaiting manager approval):\n"
        f"{property_name}, {property_address}, Unit {unit_label}.\n"
        f"Issue: {summary}\n"
        f"Please reply with your availability. Photos available on request.\n"
        f"— Property Management"
    )
