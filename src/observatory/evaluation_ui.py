"""Public definitions and explicitly selected frozen-run measurements."""

from __future__ import annotations

from dash import html

from .evaluation_scorecard import pending_scorecard


def _definition(metric):
    return html.Details([
        html.Summary("Definition and method"),
        html.P(metric["definition"]),
        html.Dl([
            html.Dt("Numerator"), html.Dd(metric["numerator_definition"]),
            html.Dt("Denominator"), html.Dd(metric["denominator_definition"]),
            html.Dt("Calculation"), html.Dd(metric["formula"]),
            html.Dt("Unit"), html.Dd(metric["unit"]),
            html.Dt("Direction"), html.Dd(metric["direction"]),
            html.Dt("Review method"), html.Dd(metric["method"]),
        ]),
    ])


def _result_text(metric, result, *, subset=False):
    if result is None:
        return "Not evaluated"
    if metric["metric_id"] == "M15":
        return html.Ul([
            html.Li(
                f"{row['execution_status']} · {row['cache_state']} · {row['temperature_state']}: "
                f"P50 {row['p50_ms']:,.1f} ms; P95 {row['p95_ms']:,.1f} ms; n={row['count']}"
            ) for row in result["strata"]
        ])
    if metric["metric_id"] == "M16":
        value = result["usd_per_submitted_task"]
        return (f"${value:.5f} per submitted task · ${result['settled_usd']:.5f} / "
                f"{result['submitted_tasks']} tasks") if value is not None else "Not applicable: no denominator"
    value = result["reviewed_rate"] if subset and "reviewed_rate" in result else result["rate"]
    if value is None and result.get("denominator") == 0:
        return "Not applicable: no denominator"
    if value is None:
        return "Not evaluated"
    numerator = result.get("numerator", result.get("passed"))
    denominator = result.get("denominator", result.get("evaluated"))
    return f"{value:.1%} · {numerator} / {denominator}"


def _measurement_section(report):
    labels = {metric["id"]: metric["label"] for metric in pending_scorecard()["metrics"]}
    return html.Section([
        html.H2("Frozen-run measurements"),
        html.P(
            "These measurements apply only to the recorded run and selection. "
            "Applicability to the current application has not been verified. "
            "Review declarations and valid arithmetic do not establish customer acceptance.",
            className="scope-note", role="status",
        ),
        html.P(f"Report: {report['report_id']} · Created: {report['created_at']} · "
               f"Declared reviewers: {report['reviewer_count']}"),
        html.P(report["sampling_description"]),
        html.Ul([html.Li(note) for note in report["limitations"]]),
        html.Div(html.Table([
            html.Caption("Measurements separated by collection, task and citation source type"),
            html.Thead(html.Tr([
                html.Th("Measure and selection", scope="col"),
                html.Th("Declared-plan result", scope="col"),
                html.Th("Coverage and failures", scope="col"),
                html.Th("Review method", scope="col"),
            ])),
            html.Tbody([
                html.Tr([
                    html.Th([
                        html.Code(metric["metric_id"]), " · ", labels[metric["metric_id"]],
                        html.Br(), f"{metric['dataset']} · {metric['task_type']}",
                        html.Br() if metric["citation_source_type"] else None,
                        metric["citation_source_type"] or "",
                    ], scope="row"),
                    html.Td([
                        _result_text(metric, metric["result"]),
                        html.P("Review or reference completion pending") if metric["result"] is None else None,
                        html.Details([
                            html.Summary("Evaluated subset only — incomplete result"),
                            _result_text(metric, metric["reviewed_subset_result"], subset=True),
                        ]) if metric["result"] is None and metric["reviewed_subset_result"] is not None else None,
                    ]),
                    html.Td([
                        html.P(f"Executed {metric['coverage']['executed_cases']} / {metric['coverage']['planned_cases']} cases; "
                               f"failed {metric['coverage']['failed_cases']}; invalid {metric['coverage']['invalid_cases']}"),
                        html.P(f"Reviewed {metric['coverage']['reviewed_units']} / {metric['coverage']['planned_units']} units; "
                               f"evaluated {metric['coverage']['evaluated_units']}; pending {metric['coverage']['pending_units']}"),
                    ]),
                    html.Td(f"{metric['review_policy']['method']} · "
                            f"{metric['review_policy']['required_reviewers']} required reviewer(s) per unit; "
                            "team methodology"),
                ], **{"data-report-metric-id": metric["metric_id"]})
                for metric in report["metrics"]
            ]),
        ]), className="statistics-table"),
        html.Details([
            html.Summary("Run and version details"),
            html.P(f"Run: {report['run_started_at']} to {report['run_ended_at']}"),
            html.P("Recorded review dates: " + (", ".join(report["reviewed_at"]) or "None")),
            html.Dl([
                item for key, value in sorted(report["versions"].items())
                for item in (html.Dt(key), html.Dd(html.Code(value)))
            ]),
            html.P("Version fields are frozen artifact fingerprints; active_profile is the retrieval identity. "
                   "Named review records remain in the audit packet. Customer acceptance is not established."),
        ]),
    ], **{"aria-label": "Frozen-run evaluation results"})


def evaluation_panel(publication=None):
    """Render the published methodology, without inventing evaluation results."""
    scorecard = pending_scorecard()
    publication = publication or {"state": "pending", "report": None}
    report = publication["report"] if publication["state"] == "available" else None
    measured_ids = {row["metric_id"] for row in report["metrics"]} if report else set()
    evaluated_ids = {row["metric_id"] for row in report["metrics"]
                     if row["coverage"]["evaluated_units"]} if report else set()
    labels = {metric["id"]: metric["label"] for metric in scorecard["metrics"]}
    return html.Div([
        html.P("Evaluation definitions", className="eyebrow"),
        html.P(
            "The configured evaluation report is unavailable or failed validation. "
            "Definitions remain available; no measurements are displayed."
            if publication["state"] == "unavailable" else
            "A frozen-run report is displayed below. Unmeasured tasks remain pending; "
            "the report does not establish current application quality or customer acceptance."
            if report else scorecard["notice"],
            className="scope-note", role="status",
        ),
        html.P([
            "Definition version: ", html.Code(scorecard["definition_version"]),
            ". ", "A selected frozen run is shown separately below. " if report else "No evaluation run is available. ",
            "The evaluated sample size, run date, ",
            "application, prompt, data and index versions, and independent reviewer ",
            "will accompany each published result.",
        ], className="scope-note"),
        html.Section([
            html.H2("Customer task coverage"),
            html.P("These six tasks come from the project requirements. Availability of a feature does not establish its measured quality.", className="scope-note"),
            html.Div(html.Table([
                html.Caption("All six customer tasks and their evaluation prerequisites"),
                html.Thead(html.Tr([
                    html.Th("Task", scope="col"),
                    html.Th("What we measure", scope="col"),
                    html.Th("Evaluation status", scope="col"),
                    html.Th("Required before evaluation", scope="col"),
                ])),
                html.Tbody([
                    html.Tr([
                        html.Th([html.Code(requirement["id"]), " · ", requirement["title"]], scope="row"),
                        html.Td(", ".join(labels[mid] for mid in requirement["metric_ids"])),
                        html.Td("Some measurements available; task acceptance pending"
                                if evaluated_ids.intersection(requirement["metric_ids"]) else "Not evaluated"),
                        html.Td(requirement["prerequisite"]),
                    ], **{"data-requirement-id": requirement["id"]})
                    for requirement in scorecard["requirements"]
                ]),
            ]), className="statistics-table"),
        ], **{"aria-label": "Customer task coverage"}),
        html.Section([
            html.H2("Measures and results"),
            html.P("Results are shown with their reviewed numerator and denominator. An unmeasured result is not a zero score. Software test counts and model self-confidence are excluded.", className="scope-note"),
            html.Div(html.Table([
                html.Caption("Metric definitions; frozen-run results are shown separately" if report
                             else "Metric definitions; independent performance results are pending"),
                html.Thead(html.Tr([
                    html.Th("Measure", scope="col"),
                    html.Th("Customer tasks", scope="col"),
                    html.Th("Result", scope="col"),
                    html.Th("Reviewed sample", scope="col"),
                    html.Th("Definition", scope="col"),
                ])),
                html.Tbody([
                    html.Tr([
                        html.Th([html.Code(metric["id"]), " · ", metric["label"]], scope="row"),
                        html.Td(", ".join(metric["requirements"])),
                        html.Td("See frozen-run report below" if metric["id"] in measured_ids else "Not evaluated"),
                        html.Td("See report coverage" if metric["id"] in measured_ids else "Not evaluated"),
                        html.Td(_definition(metric)),
                    ], **{"data-metric-id": metric["id"]})
                    for metric in scorecard["metrics"]
                ]),
            ]), className="statistics-table"),
        ], **{"aria-label": "Evaluation measures"}),
        _measurement_section(report) if report else None,
        html.Section([
            html.H2("How evaluation works"),
            html.Ol([html.Li(step) for step in scorecard["methodology"]]),
            html.P("Every release is assessed against its own data and implementation versions. Historical results remain identified as historical; they do not automatically validate a changed version.", className="scope-note"),
        ], **{"aria-label": "Evaluation methodology"}),
    ], id="evaluation-content")
