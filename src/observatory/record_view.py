"""Read-only, escaped article detail pages alongside the Dash application."""

from flask import abort, render_template_string

from .analytics import sponsor_display, sponsor_metadata

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ record.title or 'Record details' }} · Advertising Observatory</title>
<link rel="stylesheet" href="/assets/observatory.css"></head><body>
<header class="site-header"><a class="brand" href="/query"><span class="brand-text"><span class="brand-name">Advertising Observatory</span></span></a>
<div class="header-actions"><nav class="page-nav" aria-label="Main navigation"><a class="nav-link" href="/query">Query</a><a class="nav-link" href="/data">Data</a></nav>
<details id="toolbox" class="toolbox"><summary class="toolbox-trigger" title="Tools" aria-label="Tools"><img src="/assets/tools.svg" width="22" height="22" alt=""></summary>
<div class="toolbox-panel" aria-label="Research tools"><div class="toolbox-heading"><h2>Tools</h2><button type="button" class="toolbox-close" aria-label="Close tools">×</button></div>
<a class="toolbox-link" href="/query">Ask a question</a><a class="toolbox-link" href="/data">Explore data</a><a class="toolbox-link" href="/wireframe">Project wireframe</a></div></details></div></header>
<main class="page-shell record-page"><nav class="record-breadcrumb" aria-label="Breadcrumb"><a href="/data">Data</a><span aria-hidden="true">/</span><span aria-current="page">Article record</span></nav>
<section class="hero record-hero">
<h1>{{ record.title or 'Untitled record' }}</h1>
<dl class="record-meta"><div class="record-meta-item"><dt>Publisher</dt><dd>{{ record.publisher or '(Unknown outlet)' }}</dd></div><div class="record-meta-item"><dt>Sponsor / organization</dt><dd>{{ sponsor }}</dd></div><div class="record-meta-item"><dt>Publication date</dt><dd>{{ record.date or 'Publication date unknown' }}</dd></div></dl>
<p class="record-collection-note"><strong>Collection search term:</strong> {{ record.keyword or '(Unknown)' }}. This describes collection, not sponsor identity or article theme.</p>
{% if sponsor_note %}<p class="scope-note">{{ sponsor_note }}</p>{% endif %}</section>
<section class="records-panel record-sources"><h2>Sources and archived materials</h2>
<div class="source-links">{% if record.url %}<a href="{{ record.url }}" target="_blank" rel="noopener noreferrer">Original source ↗</a>{% endif %}
{% if record.archive_url %}<a href="{{ record.archive_url }}" target="_blank" rel="noopener noreferrer">Online archive ↗</a>{% endif %}</div>
<p><strong>{{ record.archive_status }}</strong></p><p>{{ record.archive_note }}</p>
{% for asset in record.attachments %}<article class="snapshot-card"><h3>{{ asset.label }}</h3>
<p>{{ asset.identity_note }}</p><details><summary>Snapshot limitations</summary>
{% if asset.limitations is string %}<p>{{ asset.limitations }}</p>{% else %}<ul>{% for note in asset.limitations %}<li>{{ note }}</li>{% endfor %}</ul>{% endif %}</details>
<p><a href="{{ asset.url }}" target="_blank" rel="noopener">Open local PDF ↗</a> · <a href="{{ asset.download_url }}">Download PDF</a></p>
{% if asset.preview_url %}<figure><img class="snapshot-image" src="{{ asset.preview_url }}" alt="Page 1 of the reviewed source PDF"><figcaption>Page 1 of {{ asset.pages }} · Preview rendered from the archived PDF. Open or download the PDF for all pages.</figcaption></figure>{% else %}<p>If your browser cannot display the PDF below, use Open local PDF or Download PDF.</p>
<iframe class="snapshot-preview" src="{{ asset.url }}" title="{{ asset.label }} PDF preview"></iframe>{% endif %}
<details><summary>Attachment verification</summary><p class="record-reference">SHA-256 {{ asset.sha256 }}</p></details></article>{% endfor %}
{% if not record.attachments %}<p>No verified PDF or screenshot is attached to this record.</p>{% endif %}</section>
<section class="records-panel record-body-panel"><h2>{{ record.body_label }}</h2><p class="scope-note">{{ record.body_note }}</p>
{% if record.quality_notes %}<ul>{% for note in record.quality_notes %}<li>{{ note }}</li>{% endfor %}</ul>{% endif %}
{% if record.body %}<div class="stored-body">{{ record.body }}</div>{% else %}<p>No article text is available.</p>{% endif %}
<details><summary>Technical details</summary><div class="record-reference"><span>Record {{ record.record_id }}</span><span>Version {{ record.version_id }}</span><span>Body SHA-256 {{ record.body_hash }}</span><span>{{ record.body_characters }} stored characters. Eligible for retrieval: {{ record.retrievable }}.</span></div></details></section>
</main><script src="/assets/toolbox.js" defer></script>
</body></html>"""


def register_record_page(server, details):
    @server.get("/records/<record_id>")
    def record_page(record_id):
        if details is None:
            abort(404)
        try:
            record = details.get(record_id)
        except Exception:
            return (
                "Record temporarily unavailable. Please return to Data and try again.",
                503,
            )
        if record is None:
            abort(404)
        return render_template_string(
            TEMPLATE,
            record=record,
            sponsor=sponsor_display(record.get("sponsor")),
            sponsor_note=sponsor_metadata(record.get("sponsor"))["note"],
        )
