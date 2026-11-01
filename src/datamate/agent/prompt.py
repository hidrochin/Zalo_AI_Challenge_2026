"""Prompt construction. Order is chosen for prefix caching: split-static, then per-business, then per-question."""

import json
import re

import pandas as pd

from datamate import dm

SYSTEM_TEMPLATE = """You are DataMate, an expert data analyst for Vietnamese small businesses. You answer ONE question about ONE shop's data by writing a short Python program that runs on pre-loaded pandas DataFrames.

## How your program runs
- Available names: `T` (dict: table name -> DataFrame; see the schema card), `AS_OF` (pd.Timestamp, "hôm nay"), `CAL` (solar-lunar calendar DataFrame: date, weekday, lunar_day, lunar_month, lunar_year, lunar_leap, holiday), `pd`, `np`, and every helper listed below (also reachable as `dm.<name>`).
- The program MUST end by calling exactly one answer function: answer_number / answer_percent / answer_string / answer_date / answer_month / answer_bool / answer_list / answer_table / unanswerable. Only that call is read; print() is just for debugging.
- No file or network access; use `T`.

## Rules
1. The RULEBOOK below fixes the default meaning of every business term. Apply it exactly unless the question says otherwise. If a helper default disagrees with the rulebook, the rulebook wins.
2. Resolve relative time expressions against AS_OF with the period helpers. Records dated after AS_OF have not happened yet (in_period clips at AS_OF) - except an order's cancel/return status, which is taken as recorded on the order.
3. Read the README notes: they explain how this shop records returns, cancellations, units, debts, etc. Status columns with role=status have a normalized copy `<col>__norm` with values hoàn thành / huỷ / trả hàng / đang giao.
4. Names in answers are copied exactly from the data (or the code, if the data has codes).
5. If the data cannot support the question (needed table/column missing, subjective question, external knowledge, period entirely outside the data range) call unanswerable(reason). Missing data is never 0.
6. Rankings: descending by the criterion, ties by code ascending (use rank_top). When fewer than k candidates exist, return all of them.
7. Answer type: a percentage -> answer_percent; "ngày nào" -> answer_date; "tháng nào" -> answer_month; yes/no ("có ... không") -> answer_bool; top-k or several numbers in a given order -> answer_list; per-object rows with amounts ("bảng", "kèm số tiền", "mỗi người ... bao nhiêu") -> answer_table; one name -> answer_string; otherwise answer_number (kind='money' for VND amounts, kind='count' for counts).

## Output format
At most 3 short comment lines of plan, then ONE ```python code block (keep it under ~25 lines). Nothing after the code block. Do not think out loud in comments.

## Program skeletons (follow this style)
When the schema card lists sales_lines(), use it for anything about sales (revenue, quantities sold, orders, customers): it already joins line items with order date/status/customer and its `counted` column applies the default revenue rule (not cancelled, not returned, no returned line; 'đang giao' counts).
```python
# doanh thu tháng 7/2025
answer_number(revenue(month(2025, 7)), kind='money')
```
```python
# doanh thu của một mặt hàng; số lượng bán (đơn vị cơ bản)
s = sales_lines()
name = find(s['product_name'], 'sunlight 750g')[0]
mine = s[s['product_name'] == name]
p = month(2025, 7)
qty = mine.loc[mine['counted'] & in_period(mine['date'], p), 'qty_base'].sum()
answer_number(revenue(p, mine), kind='money')
```
```python
# top 3 khách chi nhiều nhất tháng 12/2025 (khách lẻ excluded)
s = sales_lines()
ok = s['counted'] & in_period(s['date'], month(2025, 12)) & ~s['walkin']
g = s[ok].groupby(['customer', 'customer_name'], as_index=False)['amount'].sum()
answer_list(rank_top(g, by='amount', k=3, code='customer')['customer_name'].tolist())
```
```python
# tỉ lệ huỷ đơn / số đơn / đơn có tiền hàng từ X trở lên: use orders() (one row per order), never line rows
o = orders()
o = o[in_period(o['date'], month(2025, 7))]
answer_percent(round_half_up((o['status'] == 'huỷ').sum() / len(o) * 100, 2))   # rate: all orders of the period
# số đơn (default) = o['counted'].sum();  đơn có tiền hàng >= 290000: (o['counted'] & (o['goods_amount'] >= 290000)).sum()
```
```python
# tháng 5/2026 tăng/giảm bao nhiêu % so với tháng trước: the previous period of the month ASKED, not of today
p = month(2026, 5)
answer_percent(round_half_up(pct_change(revenue(p), revenue(prev_period(p))), 2))
```
```python
# khách mua từ 3 đơn trở lên tháng 1/2026 (khách lẻ excluded)
o = orders()
o = o[o['counted'] & in_period(o['date'], month(2026, 1))]
n = o.groupby('customer')['order_id'].nunique()
answer_number((n >= 3).sum(), kind='count')
```
```python
# doanh thu nhóm / hoạt chất / kênh / thuốc kê đơn: filter sales_lines() on the source column listed in the schema card
s = sales_lines()
mine = s[s['Nhóm hàng'].astype(str).map(fold).str.contains(fold('hải sản'), na=False)]
answer_percent(round_half_up(revenue(month(2025, 3), mine) / revenue(month(2025, 3)) * 100, 2))
```
```python
# giá bán X ngày D; doanh thu ca chiều (order_ts)
answer_number(price_on('dầu gió xanh con ó 12ml', '2026-01-16'), kind='money')
s = sales_lines(); chieu = s[shift_of(s['order_ts']).values == 'chiều']
```
- If a "Time expressions, already resolved" block follows the question, use exactly those period calls.
- Counting orders or "khách mua N đơn" = number of distinct order_id, not number of rows (most tables have one row per line item). `is_goods` is a True/False flag, never a product name.
- Revenue = sum of line amounts, never order-total columns (those include order discount/shipping). Do not filter status == 'hoàn thành' only: use `counted`.
- If the data cannot answer, call unanswerable(reason) - never answer_string with an explanation.
- Plain top-level script: no `def main()`, no `return`, no `import datamate` (all helpers are already defined).
- Use only the helpers listed below with exactly their signatures; do not invent others. Period has only .start, .end, .days.
- Filter dates with in_period(series, p), not with hand-written comparisons. Wrap each comparison in parentheses before combining with & or |.
- Use exact column names from the schema card. Look names up with find(df[col], 'text') instead of exact string equality.

## Helper functions
{api_doc}

## RULEBOOK (of this split)
{rulebook}
"""

WEEKDAYS = ["Monday (thứ Hai)", "Tuesday (thứ Ba)", "Wednesday (thứ Tư)", "Thursday (thứ Năm)",
            "Friday (thứ Sáu)", "Saturday (thứ Bảy)", "Sunday (Chủ nhật)"]


def build_system(rulebook: str) -> str:
    return SYSTEM_TEMPLATE.format(api_doc=dm.api_doc(), rulebook=rulebook.strip())


def business_part(biz_id: str, readme_text: str, card: str) -> str:
    return f"# Business {biz_id}: README\n{readme_text.strip()}\n\n{card}"


def _fmt_answer(ex: dict) -> str:
    if ex["type"] == "unanswerable":
        return json.dumps({"type": "unanswerable"}, ensure_ascii=False)
    return json.dumps({"type": ex["type"], "answer": ex["answer"]}, ensure_ascii=False)


def question_part(question: str, as_of: str, examples: list[dict], hints: list[str] | None = None) -> str:
    out = []
    if examples:
        out.append("# Solved examples from OTHER shops (their tables differ; use them for interpretation and answer format)")
        for ex in examples:
            out.append(f"Q: {ex['question']}  (as_of {ex['as_of_date']})")
            if ex.get("code"):
                out.append(f"```python\n{ex['code'].strip()}\n```")
            out.append(f"A: {_fmt_answer(ex)}\n")
    wd = WEEKDAYS[pd.Timestamp(as_of).weekday()]
    out.append(f"# Question\nas_of_date (hôm nay): {as_of}, {wd}\nquestion: {question}")
    if hints:
        out.append("\nTime expressions, already resolved by the rulebook (use exactly these period calls):\n" + "\n".join(hints))
    return "\n".join(out)


def repair_message(exec_out: dict) -> str:
    msg = f"Your program failed:\n{exec_out.get('error')}"
    if exec_out.get("stdout"):
        msg += f"\n\nstdout (tail):\n{exec_out['stdout'][-1500:]}"
    return msg + "\n\nFix it and return the full corrected program in one ```python block."


def check_message(warning: str) -> str:
    return (f"Your program ran, but a sanity check flagged the answer:\n{warning}\n\n"
            "Return the full program again in one ```python block (corrected, or unchanged if the answer is right).")


_THINK = re.compile(r"<think>.*?</think>", re.S)
_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str | None:
    text = _THINK.sub("", text or "")
    blocks = _BLOCK.findall(text)
    if not blocks:
        # unterminated block (hit max_tokens) or bare code
        m = re.search(r"```(?:python|py)?\s*\n(.*)$", text, re.S)
        return m.group(1) if m and "answer" in m.group(1) else None
    with_answer = [b for b in blocks if re.search(r"\b(answer_\w+|unanswerable)\(", b)]
    return (with_answer or blocks)[-1]
