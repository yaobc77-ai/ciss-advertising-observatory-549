"""Read-only, escaped article detail pages alongside the Dash application."""

from flask import abort, render_template_string

from .analytics import sponsor_display, sponsor_metadata
from .claims_ui import COVERAGE, MEANING, public_claims
from .models import Filters
from .social_annotations import social_annotation_details

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ record.title or 'Record details' }} · Advertising Observatory</title>
<link rel="stylesheet" href="/assets/observatory.css"></head><body>
<header class="site-header"><a class="brand" href="/query"><span class="brand-text"><span class="brand-name">Advertising Observatory</span></span></a>
<div class="header-actions"><nav class="page-nav" aria-label="Main navigation"><a class="nav-link" href="/query">Query</a><a class="nav-link" href="/data">Data</a></nav>
<details id="toolbox" class="toolbox"><summary class="toolbox-trigger" title="Tools" aria-label="Tools"><img src="/assets/tools.svg" width="22" height="22" alt=""></summary>
<div class="toolbox-panel" aria-label="Research tools"><div class="toolbox-heading"><h2>Tools</h2><button type="button" class="toolbox-close" aria-label="Close tools">×</button></div>
<a class="toolbox-link" href="/query">Ask a question</a><a class="toolbox-link" href="/data">Explore data</a><a class="toolbox-link" href="/wireframe">Project wireframe</a></div></details></div></header>
<main class="page-shell record-page"><nav class="record-breadcrumb" aria-label="Breadcrumb"><a href="/data">Data</a><span aria-hidden="true">/</span><span aria-current="page">{% if social %}Social post{% else %}Article record{% endif %}</span></nav>
<section class="hero record-hero">
<h1>{{ record.title or 'Untitled record' }}</h1>
{% if social %}<dl class="record-meta"><div class="record-meta-item"><dt>Account</dt><dd>{{ account }}</dd></div><div class="record-meta-item"><dt>Platform</dt><dd>{{ platform }}</dd></div><div class="record-meta-item"><dt>Company affiliation</dt><dd>{{ affiliation }}</dd></div><div class="record-meta-item"><dt>Publication date</dt><dd>{{ record.date or 'Publication date unknown' }}</dd></div></dl>
<p class="scope-note">Account names are source channel.name values, not verified unique identities. Company affiliation is source-listed and does not establish verified paid sponsorship.</p>{% else %}<dl class="record-meta"><div class="record-meta-item"><dt>Publisher</dt><dd>{{ record.publisher or '(Unknown outlet)' }}</dd></div><div class="record-meta-item"><dt>Sponsor / organization</dt><dd>{{ sponsor }}</dd></div><div class="record-meta-item"><dt>Publication date</dt><dd>{{ record.date or 'Publication date unknown' }}</dd></div></dl>
<p class="record-collection-note"><strong>Collection search term:</strong> {{ record.keyword or '(Unknown)' }}. This describes collection, not sponsor identity or article theme.</p>
{% if sponsor_note %}<p class="scope-note">{{ sponsor_note }}</p>{% endif %}{% endif %}</section>
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
{% if social %}<section class="records-panel"><h2>Supplied social historical labels</h2>
<p class="scope-note">{{ social_annotation.note }}</p>
<p class="scope-note">According to the supplied field documentation, macro codes describe promotional, positive or neutral green or fossil-fuel messaging. Subcodes retain their original source scheme. Source True and False are historical automated outputs, not verified themes, greenwashing findings or factual judgments.</p>
<p class="record-reference">Scheme {{ social_annotation.scheme }} · Status {{ social_annotation.status }} · Binding {{ social_annotation.validation_state }}</p>
<dl class="record-meta">{% for item in social_annotation['values'] %}<div class="record-meta-item"><dt>{{ item.label }} <span class="muted">({% if item.level == 'macro' %}Macro code{% else %}Subcode{% endif %})</span><br><code>{{ item.key }}</code></dt><dd>{% if item.state == 'source_true' %}Source export: True{% elif item.state == 'source_false' %}Source export: False{% else %}Unknown — no usable source value{% endif %}</dd></div>{% endfor %}</dl>
{% if social_annotation.explanations %}<details><summary>Historical generated explanations</summary><p class="scope-note">These explanations were generated by the historical classifier. They are not original post quotations or independent verification.</p>
{% for explanation in social_annotation.explanations %}<h3>{{ explanation.label }}</h3><p>{{ explanation.text }}</p>{% if explanation.storage_note %}<p class="scope-note">{{ explanation.storage_note }}</p>{% endif %}{% if explanation.truncated %}<p class="scope-note">Explanation shortened for display.</p>{% endif %}{% endfor %}</details>{% endif %}
<details><summary>Historical annotation source binding</summary><dl class="record-reference">{% for key, value in social_annotation.provenance.items() %}<dt>{{ key }}</dt><dd>{{ value or 'Not available' }}</dd>{% endfor %}</dl></details></section>{% else %}<section class="records-panel record-claims"><h2>CLAIMS2 evidence</h2><p class="scope-note">{{ claims.note }}</p>
{% for match in claims.records %}{% for item in match.claims %}<article class="evidence-card">
<p class="scope-note">{{ item.review_label }}</p><dl><dt>{{ item.nc_id }}</dt><dd>{{ item.nc_definition }}</dd>
{% if item.sc_id %}<dt>{{ item.sc_id }}</dt><dd>{{ item.sc_definition or 'Definition unavailable in this published bundle.' }}</dd>
{% else %}<dt>Superclaim mapping</dt><dd>No superclaim is mapped in this published taxonomy.</dd>{% endif %}</dl>
<blockquote>{{ item.quote }}</blockquote><details><summary>Assignment provenance</summary><div class="record-reference">
<span>Run {{ item.run_id }}</span><span>Taxonomy {{ item.taxonomy_version }}</span><span>Review {{ item.review_version }}</span>
<span>Article version {{ item.version_id }}</span><span>Body SHA-256 {{ item.body_hash }}</span>
<span>Original character range [{{ item.start }}, {{ item.end }})</span></div></details></article>{% endfor %}{% endfor %}</section>{% endif %}
{% if social and record.social_admission %}<section class="records-panel social-admission"><h2>Company-post identity and source observations</h2>
{% set source_observations_enabled = record.retrievable and record.source_observation_retrieval and record.source_observation_retrieval.status == 'enabled_source_observations' %}
<p><strong>{{ record.social_admission.scope }}</strong>. {{ record.social_admission.paid_ad_status }}.</p>
<p>{{ record.social_admission.count_unit }}. {{ record.social_admission.member_count }} supplied source observation{% if record.social_admission.member_count != 1 %}s{% endif %}.</p>
{% if record.social_admission.conflicting_fields %}<p class="scope-note"><strong>Unresolved source differences:</strong> {{ record.social_admission.conflicting_fields | join(', ') }}. Conflicting company/account fields are unknown; {% if source_observations_enabled %}differing text is searchable as separate saved observations. The source differences have not been adjudicated.{% else %}differing text is paused for retrieval.{% endif %} These are observed source differences, not new factual judgments.</p>{% endif %}
<p class="scope-note">{% if source_observations_enabled %}<strong>Retrieval is enabled for every preserved source observation.</strong> Quotes identify their saved source observation; images, audio and referenced post contents remain unverified.{% elif record.social_admission.retrieval_status == 'enabled_source_post_text_only' %}Retrieval reads supplied post text only; images, audio and referenced posts remain separate.{% elif record.social_admission.retrieval_status == 'paused_body_disagreement' %}<strong>RAG retrieval is paused because source observations contain different text.</strong>{% else %}RAG retrieval is paused for source-text quality review.{% endif %}</p>
{% for variant in record.social_admission.variants %}<details {% if record.social_admission.conflicting_fields %}open{% endif %}><summary>Source observation {{ loop.index }} · {{ variant.source_record_id }} · {{ variant.account or 'Unknown account' }} · {{ variant.company or 'Unknown company affiliation' }}</summary>
<h3>{{ variant.title or 'Supplied post observation' }}</h3>
<dl class="record-meta"><div class="record-meta-item"><dt>Company affiliation</dt><dd>{{ variant.company or 'Unknown' }}</dd></div><div class="record-meta-item"><dt>Source account</dt><dd>{{ variant.account or 'Unknown' }}</dd></div><div class="record-meta-item"><dt>Publication date</dt><dd>{{ variant.published_at or 'Unknown' }}</dd></div></dl>
<div class="source-links">{% if variant.url %}<a href="{{ variant.url }}" target="_blank" rel="noopener noreferrer">Original post ↗</a>{% endif %}{% if variant.archive_url %}<a href="{{ variant.archive_url }}" target="_blank" rel="noopener noreferrer">Supplied archive ↗</a>{% endif %}</div>
<div class="stored-body">{{ variant.body }}</div>
<details><summary>Historical source labels · unreviewed</summary><p class="scope-note">These source-export values are retained separately and are not verified greenwashing or factual judgments.</p><dl>{% for state in variant.historical_states %}<dt>{{ state.label }}</dt><dd>{% if state.state == 'source_true' %}Source export: True{% elif state.state == 'source_false' %}Source export: False{% else %}Unknown source annotation{% endif %}</dd>{% endfor %}</dl></details>
<details><summary>Observation reference</summary><p class="record-reference">Source record {{ variant.source_record_id }} · Body SHA-256 {{ variant.body_sha256 }}</p></details></details>{% endfor %}</section>{% endif %}<section class="records-panel record-body-panel"><h2>{{ record.body_label }}</h2><p class="scope-note">{{ record.body_note }}</p>
{% if record.quality_notes %}<ul>{% for note in record.quality_notes %}<li>{{ note }}</li>{% endfor %}</ul>{% endif %}
{% if record.body %}<div class="stored-body">{{ record.body }}</div>{% else %}<p>{% if social %}No post text is available.{% else %}No article text is available.{% endif %}</p>{% endif %}
<details><summary>Technical details</summary><div class="record-reference"><span>Record {{ record.record_id }}</span><span>Version {{ record.version_id }}</span><span>Body SHA-256 {{ record.body_hash }}</span><span>{{ record.body_characters }} stored characters. {% if social %}Retrieval enabled{% else %}Eligible for retrieval{% endif %}: {{ record.retrievable }}.</span></div></details></section>
</main><script src="/assets/toolbox.js" defer></script>
</body></html>"""


def _record_claims(service, record):
    if service is None or not hasattr(service, "claims_matches"):
        return {"records": [], "note": "CLAIMS2 results are awaiting publication. " + COVERAGE}
    try:
        result = public_claims(service.claims_matches(
            Filters(dataset=record["dataset"], record_ids=[record["record_id"]]), limit=1,
        ), record=record)
        if result["state"] == "empty":
            result["note"] = "No published CLAIMS2 assignments are available for this article. " + COVERAGE
        elif result["state"] == "ready":
            result["note"] = MEANING + " " + COVERAGE
        return result
    except (TypeError, ValueError, KeyError):
        return {"records": [], "note": "CLAIMS2 evidence could not be bound to this article version. Refresh this page to try again."}
    except Exception:
        return {"records": [], "note": "CLAIMS2 assignments are temporarily unavailable. The article text remains available below."}


def register_record_page(server, details, *, service=None):
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
        social = record.get("dataset") == "social"
        annotation = record.get("social_historical_annotation") if social else None
        if social and not isinstance(annotation, dict):
            # Missing public projection is unknown. Never recover from raw
            # source fields or infer False from an absent positive label.
            annotation = social_annotation_details({
                "dataset": "social", "record_id": record.get("record_id"),
                "version_id": record.get("version_id"), "body": record.get("body"),
                "body_hash": record.get("body_hash"), "annotations": [],
            })
        return render_template_string(
            TEMPLATE,
            record=record,
            social=social, social_annotation=annotation,
            account=_social_value(record.get("account"), "Unknown account") if social else None,
            platform=_social_value(record.get("platform"), "Unknown platform") if social else None,
            affiliation=_social_value(record.get("sponsor"), "Unknown company affiliation") if social else None,
            sponsor=sponsor_display(record.get("sponsor")) if not social else None,
            sponsor_note=sponsor_metadata(record.get("sponsor"))["note"] if not social else None,
            claims=_record_claims(service, record) if not social else None,
        )


def _social_value(value, unknown):
    return value if isinstance(value, str) and value.strip() and value != "(Unknown)" else unknown
