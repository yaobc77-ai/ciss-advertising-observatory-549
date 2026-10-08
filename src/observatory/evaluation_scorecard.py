"""Customer-task metric definitions; pending values are never invented scores.

This page describes the evaluation contract. It does not automatically publish
old development runs or infer approval from local tests or source quotations.
"""

from copy import deepcopy

DEFINITION_VERSION = "customer-metrics-v1"


def _metric(identifier, label, definition, numerator, denominator, formula, unit,
            requirements, method, direction="higher"):
    return {
        "id": identifier, "label": label, "definition": definition,
        "numerator_definition": numerator, "denominator_definition": denominator,
        "formula": formula, "unit": unit, "direction": direction,
        "requirements": requirements, "method": method,
    }


METRICS = (
    _metric("M01", "Research task correctness",
            "A task passes only when all required outputs, selection scope and source limits are correct.",
            "Tasks passing the full pre-agreed rubric",
            "Executed tasks with fixed references; include errors and timeouts",
            "passed tasks / evaluated tasks", "task", ["RQ1", "RQ2", "RQ3", "RQ4", "RQ5", "RQ6"],
            "Independent reference check; full result review. Pending reviews prevent a final score."),
    _metric("M02", "Statistics and scope agreement",
            "All requested totals, groups, periods, ties and share denominators match an independently enumerated record set.",
            "Statistics tasks with exact outputs and exact scope",
            "All executed statistics tasks with an agreed counting unit",
            "exact tasks / statistics tasks", "task", ["RQ1", "RQ2", "RQ3", "RQ6"],
            "Enumerate source records independently; exercise the submitted-question path, not just the database function."),
    _metric("M03", "Relationship precision",
            "Returned company-outlet relationships belong to the reviewed source-defined relationship set.",
            "Correct distinct returned relationships",
            "All distinct returned relationships",
            "TP / (TP + FP)", "typed relationship", ["RQ1", "RQ2"],
            "Compare typed source relationships under the exact filters; a recorded connection is not proof of a business contract."),
    _metric("M04", "Relationship recall",
            "All relationships required by the reviewed selection are represented.",
            "Correct distinct returned relationships",
            "All distinct reference relationships in the reviewed selection",
            "TP / (TP + FN)", "typed relationship", ["RQ1", "RQ2"],
            "Fully enumerate the reference selection; incomplete reference sets cannot support recall."),
    _metric("M05", "Graph drilldown agreement",
            "Selecting a node or edge shows the exact corresponding entities, counts and record set.",
            "Selections with correct relationships, counts and supporting records",
            "All executed reference-bound node and edge selections",
            "correct selections / evaluated selections", "selection", ["RQ1", "RQ2", "RQ3", "RQ5"],
            "Check company, outlet and relationship selections across combined filters, including zero matches."),
    _metric("M06", "Matching-ad precision",
            "Returned ads satisfy the question's reviewed meaning: occurrence, attribution or framing.",
            "Correct distinct returned advertising record IDs",
            "All distinct returned advertising record IDs",
            "TP / (TP + FP)", "advertisement", ["RQ4", "RQ6"],
            "Review full article context under the question's codebook; keyword overlap alone is not a positive match."),
    _metric("M07", "Matching-ad recall",
            "The result contains the reviewed relevant ads in the explicitly assessed collection.",
            "Correct distinct returned advertising record IDs",
            "All relevant advertising record IDs in the fully reviewed evaluation scope",
            "TP / (TP + FN)", "advertisement", ["RQ4", "RQ6"],
            "Requires a complete, reviewed membership set. Example search and top-five hits do not establish full-list recall."),
    _metric("M08", "Statement evidence support",
            "Every distinct factual statement, including summary statements, is supported by its citations and original context.",
            "Statements with sufficient source support and no unsupported additions",
            "All distinct factual statements in evaluated generated answers",
            "supported statements / factual statements", "atomic statement", ["RQ4", "RQ6"],
            "Named reviewers check citation entailment against the source. Uncited factual statements remain in the denominator."),
    _metric("M09", "Attribution and qualification fidelity",
            "Statements retain the speaker, article treatment, units, conditions, dates and planned versus achieved status.",
            "Statements preserving every relevant attribution and qualification",
            "All distinct factual statements in evaluated generated answers",
            "faithful statements / factual statements", "atomic statement", ["RQ4", "RQ6"],
            "Independent source reading; a mention is not endorsement and an advertiser's promise is not an achieved result."),
    _metric("M10", "Citation traceability",
            "A citation identifies recoverable supporting material under the checks for its source type: article quotation, structured collection result or external page.",
            "Citations passing the applicable source-identity and recovery checks",
            "All citations emitted by evaluated answers, reported separately by source type",
            "valid citations / emitted citations", "citation", ["RQ2", "RQ4", "RQ5", "RQ6"],
            "Article quotes require record/text version, exact quote and offset; statistics require the frozen selection, record set and calculation receipt; external pages require URL, source/capture identity and retrieval time. Report types separately. Traceability does not prove meaning or truth; external checks are not yet implemented."),
    _metric("M11", "Missing-information handling",
            "No evidence, ambiguity, unreviewed classifications, unavailable data and tool failures follow their pre-agreed response rules.",
            "Designated difficult tasks handled correctly with scope and external-source limits retained",
            "All executed tasks in the designated missing-information and failure strata",
            "correct handling / designated tasks", "task", ["RQ1", "RQ2", "RQ3", "RQ4", "RQ5", "RQ6"],
            "Compare the expected response state and explanation; a service error is not a correct no-evidence refusal."),
    _metric("M12", "Unnecessary refusal rate",
            "An answerable, supported task is incorrectly refused or declared to lack evidence.",
            "Answerable tasks incorrectly refused or declared insufficient",
            "All executed answerable tasks with reviewed reference answers",
            "unnecessary refusals / answerable tasks", "task", ["RQ1", "RQ2", "RQ3", "RQ4", "RQ5", "RQ6"],
            "Score alongside missing-information handling. Timeouts are operational failures and fail M01; they are not counted as no-evidence refusals.",
            direction="lower"),
    _metric("M13", "Unassisted user task completion",
            "A target user completes the defined exploration and source-access task without developer help within an agreed time limit.",
            "Successful user-task attempts meeting the rubric and time limit",
            "All attempted user-task assignments in the scheduled study",
            "successful attempts / attempted assignments", "user-task attempt", ["RQ1", "RQ2", "RQ3", "RQ4", "RQ5", "RQ6"],
            "Observe journalists, lawyers or researchers using filters, charts, graph selections, source views and exports. Report participant count and assistance separately."),
    _metric("M14", "Required answer-point coverage",
            "The response addresses the question's required points with adequate evidence; extra statements cannot increase the score.",
            "Predefined answer points correctly covered",
            "All required answer points in the evaluated task rubrics",
            "covered required points / required points", "rubric point", ["RQ4", "RQ6"],
            "Define points before execution; comparisons require each target, and relevant limitations must remain visible."),
    _metric("M15", "End-to-end waiting time",
            "Observed time from action submission to usable result or terminal failure, including tool and network waiting.",
            "Not a success ratio",
            "All timed attempts; show successful and failed attempts separately",
            "median (P50); nearest-rank P95", "milliseconds", ["RQ1", "RQ2", "RQ3", "RQ4", "RQ5", "RQ6"],
            "Measure filters, graph, statistics answers and content answers separately. Include failures, cold/warm and cache state.",
            direction="lower"),
    _metric("M16", "API cost per submitted task",
            "Settled model, query-embedding and external-search charges divided by all submitted tasks, including failures.",
            "Total settled task-associated API charges",
            "All submitted tasks in the evaluated run",
            "settled USD / submitted tasks", "USD per task", ["RQ6"],
            "Use the usage ledger. Missing charges or unresolved reservations prevent a final complete cost; report them separately.",
            direction="lower"),
)

REQUIREMENTS = (
    {"id": "RQ1", "title": "Compare ad volume by company and news outlet",
     "metric_ids": ["M01", "M02", "M03", "M04", "M05", "M11", "M12", "M13", "M15"],
     "prerequisite": "Agreed counting unit and duplicate policy; independent selection and count references."},
    {"id": "RQ2", "title": "Find each outlet's sponsors and each company's publishers",
     "metric_ids": ["M01", "M02", "M03", "M04", "M05", "M10", "M11", "M12", "M13", "M15"],
     "prerequisite": "Reviewed source-defined identities and complete relationship/record sets."},
    {"id": "RQ3", "title": "Compare dates, outlets, companies and sponsors",
     "metric_ids": ["M01", "M02", "M05", "M11", "M12", "M13", "M15"],
     "prerequisite": "Reviewed dates, unknown-date policy, all groups/ties and explicit period endpoints."},
    {"id": "RQ4", "title": "Explore supported themes and advertising claims",
     "metric_ids": ["M01", "M06", "M07", "M08", "M09", "M10", "M11", "M12", "M13", "M14", "M15"],
     "prerequisite": "Question-specific codebook, source completeness and independently reviewed ad membership/evidence."},
    {"id": "RQ5", "title": "Explore the social-media collection with useful filters and visualizations",
     "metric_ids": ["M01", "M05", "M10", "M11", "M12", "M13", "M15"],
     "prerequisite": "The Twitter export is received and staged; admission, counting rules, import and user review remain pending."},
    {"id": "RQ6", "title": "Explore both collections through source-grounded question answering",
     "metric_ids": ["M01", "M02", "M06", "M07", "M08", "M09", "M10", "M11", "M12", "M13", "M14", "M15", "M16"],
     "prerequisite": "Frozen task references, actual runs and independent reviews; social/cross-collection cases need admitted social data."},
)

METHODOLOGY = (
    "Agree the collection, counting unit, source meanings, question types and acceptance targets before testing.",
    "Prepare exact record sets and source passages. Separate mention, framing and external truth; unresolved records remain unknown.",
    "Freeze application, prompt, data, index, questions and rubric. Independently check current-corpus statistics; use unused article groups for content generalization, not just rewritten questions.",
    "Run the real user paths with a fixed budget and retry policy; retain failures, costs and timings without replacing failed attempts.",
    "Use independent human source review for meaning and usability; preserve disagreements and adjudication. Program checks handle counts and locations.",
    "Publish numerators, denominators, missing reviews, run scope and uncertainty. Report native, social and cross-collection results separately.",
)


def pending_scorecard():
    """Return the current unmeasured contract; never promote old diagnostic runs."""
    return {
        "definition_version": DEFINITION_VERSION, "status": "not_evaluated",
        "notice": (
            "Metric definitions are available. No independently reviewed customer-task "
            "results have been published. Not evaluated is not zero accuracy. "
            "Engineering test results and model self-confidence are not performance scores."
        ),
        "requirements": [{**deepcopy(row), "status": "not_evaluated"} for row in REQUIREMENTS],
        "metrics": [
            {**deepcopy(row), "result": None, "numerator": None, "denominator": None,
             "status": "not_evaluated"}
            for row in METRICS
        ],
        "methodology": list(METHODOLOGY),
    }
