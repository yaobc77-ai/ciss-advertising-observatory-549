"""Customer holdout material is accessible only in an explicit local evaluation.

The normal research service never enters this context. This is a programmatic
workflow boundary, not a filesystem security sandbox or an unseen-data claim.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_PURPOSE = ContextVar('observatory_evaluation_purpose', default=None)
CLIENT24_SHA256 = 'bc94302a3e6d72636d4e9a02cc9597c98920117501a3749d8bcd1499d2515466'
_ROOT = Path(__file__).resolve().parents[2]


class EvaluationOnlyError(PermissionError):
    """Raised before held-out question or reference content enters development."""


@contextmanager
def evaluation_session(*, purpose: str):
    if purpose not in {'customer_test', 'evaluation'}:
        raise EvaluationOnlyError('Customer questions are permitted only for test/evaluation.')
    token = _PURPOSE.set(purpose)
    try:
        yield
    finally:
        _PURPOSE.reset(token)


def evaluation_active() -> bool:
    return _PURPOSE.get() in {'customer_test', 'evaluation'}


def require_evaluation() -> None:
    if not evaluation_active():
        raise EvaluationOnlyError('Customer holdout access requires an explicit local evaluation session.')


def customer_question_texts() -> tuple[str, ...]:
    require_evaluation()
    data = (_ROOT / 'eval/customer_metrics/content_questions.v1.json').read_bytes()
    if hashlib.sha256(data).hexdigest() != CLIENT24_SHA256:
        raise EvaluationOnlyError('Frozen customer question bytes changed.')
    document = json.loads(data)
    rows = sorted(document['questions'], key=lambda row: row['question_id'])
    if len(rows) != 24 or [row['question_id'] for row in rows] != [f'Q{i:02d}' for i in range(1, 25)]:
        raise EvaluationOnlyError('Frozen customer question roster changed.')
    return tuple(row['question_exact'] for row in rows)
