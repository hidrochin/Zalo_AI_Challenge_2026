"""Second agent: does the shop's data hold what the question needs?

The solver's UNANSWERABLE costs −1 when the data can answer, and an answer to an unanswerable question costs −1 too.
The judge reads the question, the shop's README and schema card and the rulebook (its rules on unanswerable questions
included) with the solver's verdict, and says whether the data can answer. The pipeline combines both: they agree, the
verdict stands; the judge finds the data, the solver gets one more try with the judge's pointer; they still disagree,
the question abstains. A solver that answers but writes UNANSWERABLE beside its query is judged too: a judge that
also finds the data missing makes it UNANSWERABLE.
"""

import json
import re

SYSTEM = """You are the second reviewer of a data-analysis agent for small Vietnamese shops. You decide ONE thing: can
this shop's data answer the question, under the rulebook below?

The question is UNANSWERABLE when the data lacks what it needs: a field the files do not record (and the rulebook gives
no way to derive it), a period entirely outside the data range, a subjective judgement, or knowledge from outside the
data. A period that only partly overlaps the data is answerable. A rare value, an empty filter result or a hard
computation is NOT a reason: the data still answers it.

Look for the needed fields in the schema card: canonical views, raw tables (with sample values), shop views and macros.
Reply with one JSON object and nothing else:
{{"answerable": true or false, "needs": "what the question needs", "where": "the tables and columns that hold it, or
what is missing"}}

## RULEBOOK
{rulebook}"""

_VERDICT = re.compile(r'"answerable"\s*:\s*(true|false)', re.I)


def judge_messages(rulebook: str, biz_part: str, question: str, normalized: str | None, solver: str) -> tuple[str, list[str]]:
    q = f"Question: {question}" + (f"\nNormalized: {normalized}" if normalized and normalized != question else "")
    return SYSTEM.format(rulebook=rulebook.strip()), [biz_part, q, f"The first agent's verdict:\n{solver}"]


def parse_verdict(reply: str) -> dict | None:
    """{'answerable': bool, 'needs': str, 'where': str}, or None when the reply has no verdict."""
    m = _VERDICT.search(reply or "")
    if not m:
        return None
    out = {"answerable": m.group(1).lower() == "true", "needs": "", "where": ""}
    obj = re.search(r"\{.*\}", reply, re.S)
    if obj:
        try:
            d = json.loads(obj.group(0))
            out["needs"], out["where"] = str(d.get("needs", "")), str(d.get("where", ""))
        except (json.JSONDecodeError, AttributeError):
            pass
    return out


_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)
_DOUBT = re.compile(r"^\W*UNANSWERABLE\W*:?\s*(.*)$", re.M)


def doubt(reply: str) -> str | None:
    """The UNANSWERABLE line a reply writes outside its code blocks next to a query: the solver answered but doubts the
    data. None when it does not."""
    text = _THINK.sub("", reply or "")
    if "```" not in text:
        return None
    m = _DOUBT.search(_FENCE.sub("", text))
    return (m.group(1).strip() or "the data may not hold it") if m else None


def solver_text(final: dict, code: str | None, doubt: str | None = None) -> str:
    if final.get("type") == "unanswerable":
        return f"UNANSWERABLE: {final.get('reason', '')}"
    out = f"It answered {json.dumps(final.get('answer'), ensure_ascii=False)[:300]} with:\n{code or ''}"
    return out + (f"\nbut it also wrote: UNANSWERABLE: {doubt}" if doubt else "")


def retry_note(verdict: dict) -> str:
    return ("A second reviewer says the data CAN answer this question. It needs: " + verdict["needs"]
            + ". Where: " + verdict["where"] + ". Write the query. Reply UNANSWERABLE again only if those tables or "
            "columns do not hold what the question needs.")
