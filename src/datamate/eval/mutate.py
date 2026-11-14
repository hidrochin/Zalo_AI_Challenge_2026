"""Stress test for unseen shops: change train shops on purpose, then measure what ingestion still recovers.

Each mode changes one thing the private test could change (headers, README, cell formats, status words, name spellings,
subtotal rows), so a drop points at one weakness. The contracts of eval/semantic.py are the yardstick: they have gold
answers, and a mutation changes how a shop is written, not what it sold. Modes that change values and rulebook variants
(rules.py) are reported apart, because they measure different abilities.
"""

import random
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import pandas as pd

from datamate.agent.columns import propose_roles
from datamate.agent.statuses import ask_labels
from datamate.canon import build_sales
from datamate.eval.semantic import contract_items, empty_report, score_contracts
from datamate.ingest import load_calendar
from datamate.ingest.business import Business, apply_status, assemble, extract_pieces, read_sheets
from datamate.ingest.parsers import _is_null, parse_date, parse_number
from datamate.ingest.readme import Readme, parse_readme
from datamate.ingest.roles import assign_roles, normalize_status
from datamate.ingest.tables import RawSheet, detect_header
from datamate.rules import rule_gate
from datamate.text import fold

# Headers no rule in ingest/roles.py knows.
UNSEEN = {"order_id": "Số bill", "order_date": "Ngày chứng từ", "order_ts": "Mốc tạo bill", "status": "Tình hình xử lý",
          "product_id": "Mã vật tư", "product_name": "Diễn giải", "qty": "Lượng xuất", "qty_base": "Lượng quy đổi",
          "unit": "Cách tính", "base_unit": "Đơn vị gốc", "unit_price": "Giá niêm yết", "line_amount": "Doanh số dòng",
          "customer_id": "Mã người mua", "customer_name": "Người mua"}
# Status words the rules do not know, by rulebook status.
STATUS_WORDS = {"hoàn thành": "Chốt đơn xong", "huỷ": "Khách bom hàng", "trả hàng": "Khách gửi lại đồ", "đang giao": "Shipper đang cầm"}
SUBTOTAL_LABEL = "Cộng phát sinh {:02d}/{:02d}"

Pieces = list[tuple[RawSheet, pd.DataFrame]]


def _roles(sh: RawSheet, df: pd.DataFrame, readme: Readme) -> dict[str, str]:
    return assign_roles(list(df.columns), readme.descs_for(sh.file, sh.sheet))


def _map_role(pieces: Pieces, readme: Readme, roles: tuple, fn) -> Pieces:
    """Apply fn to the cells of the columns with these roles."""
    out = []
    for sh, df in pieces:
        df = df.copy()
        for c, r in _roles(sh, df, readme).items():
            if r in roles:
                df[c] = df[c].map(lambda v: v if _is_null(v) else fn(v))
        out.append((sh, df))
    return out


def untouched(pieces: Pieces, readme: Readme, rng: random.Random):
    """With model steps on, shows what they change on a shop the rules already read."""
    return pieces, readme, {}


def shuffle(pieces: Pieces, readme: Readme, rng: random.Random):
    out = []
    for sh, df in pieces:
        cols = list(df.columns)
        rng.shuffle(cols)
        out.append((sh, df[cols]))
    return out, readme, {}


def no_readme(pieces: Pieces, readme: Readme, rng: random.Random):
    return pieces, Readme("", readme.data_end), {}


def rename_headers(pieces: Pieces, readme: Readme):
    """Headers of the mapped columns become UNSEEN ones; README sections follow."""
    out, renamed = [], {}
    for sh, df in pieces:
        new = {c: UNSEEN[r] for c, r in _roles(sh, df, readme).items() if r in UNSEEN}
        renamed.update(new)
        out.append((sh, df.rename(columns=new)))
    columns = {k: {renamed.get(c, c): d for c, d in cols.items()} for k, cols in readme.columns.items()}
    return out, replace(readme, columns=columns), renamed


def rename(pieces: Pieces, readme: Readme, rng: random.Random):
    return rename_headers(pieces, readme)


def rename_no_readme(pieces: Pieces, readme: Readme, rng: random.Random):
    pieces, readme, renamed = rename_headers(pieces, readme)
    return pieces, Readme("", readme.data_end), renamed


def _date_text(v):
    if isinstance(v, str) and "/" in v and v.count("/") == 1:  # no year in the cell: the sheet gives it
        return v
    d = parse_date(v, 2000, True)
    return v if d is None else f"{d.day} thg {d.month} {d.year}"


def _money_text(v):
    n = parse_number(v)
    return v if n is None or n != int(n) else f"{int(n):,}".replace(",", ".") + "đ"


def formats(pieces: Pieces, readme: Readme, rng: random.Random):
    pieces = _map_role(pieces, readme, ("order_date",), _date_text)
    return _map_role(pieces, readme, ("line_amount", "unit_price"), _money_text), readme, {}


def status_words(pieces: Pieces, readme: Readme, rng: random.Random):
    return _map_role(pieces, readme, ("status",), lambda v: STATUS_WORDS.get(normalize_status(v), v)), readme, {}


def name_spellings(pieces: Pieces, readme: Readme, rng: random.Random):
    """A third of the rows lose accents and case in their names, as in a second person's notebook. Keyed on the row,
    so the identical rows of an export bug stay identical."""
    out = []
    for sh, df in pieces:
        df = df.copy()
        cols = [c for c, r in _roles(sh, df, readme).items() if r in ("product_name", "customer_name")]
        hit = [random.Random(str(row)).random() < 0.33 for row in df.itertuples(index=False, name=None)]
        for c in cols:
            df.loc[hit, c] = df.loc[hit, c].map(lambda v: fold(v).upper() if isinstance(v, str) else v)
        out.append((sh, df))
    return out, readme, {}


def subtotal_rows(sheets: list[RawSheet], readme: Readme, rng: random.Random, every: int = 25) -> list[RawSheet]:
    """Rows with a label the rules do not know and the block's total in the amount column."""
    out = []
    for sh in sheets:
        h = None if sh.has_header else detect_header(sh.df, readme.known_names())
        header = [] if h is None else ["" if _is_null(v) else str(v).strip() for v in sh.df.iloc[h].tolist()]
        roles = assign_roles(header, readme.descs_for(sh.file, sh.sheet))
        amount = next((header.index(c) for c, r in roles.items() if r == "line_amount"), None)
        if amount is None or len(sh.df) - h <= every:
            out.append(sh)
            continue
        rows, total = [], 0.0
        for i, row in enumerate(sh.df.itertuples(index=False, name=None)):
            rows.append(row)
            total += (parse_number(row[amount]) or 0.0) if i > h else 0.0
            if i > h and (i - h) % every == 0:
                extra = [None] * len(row)
                extra[0 if amount else 1] = SUBTOTAL_LABEL.format(rng.randint(1, 28), rng.randint(1, 12))
                extra[amount] = total
                rows.append(tuple(extra))
                total = 0.0
        out.append(replace(sh, df=pd.DataFrame(rows, columns=sh.df.columns)))
    return out


def _readme_without(readme: Readme, drop: set[str], rename: dict | None = None) -> Readme:
    columns = {k: {(rename or {}).get(c, c): d for c, d in cols.items() if c not in drop} for k, cols in readme.columns.items()}
    return replace(readme, columns=columns)


def status_in_notes(pieces: Pieces, readme: Readme, rng: random.Random):
    """No status column: a cancelled, returned or undelivered order says so in its note, as a hand-kept book does (slot
    `status_text`)."""
    out, dropped = [], set()
    for sh, df in pieces:
        col = next((c for c, r in _roles(sh, df, readme).items() if r == "status"), None)
        if col is None:
            out.append((sh, df))
            continue
        note = "Ghi chú đơn" if "Ghi chú" in df.columns else "Ghi chú"
        said = df[col].map(lambda v: None if _is_null(v) or normalize_status(v) in (None, "hoàn thành") else str(v))
        out.append((sh, df.drop(columns=[col]).assign(**{note: said})))
        dropped.add(col)
    return out, _readme_without(readme, dropped), {}


def rename_order_key(pieces: Pieces, readme: Readme, rng: random.Random):
    """The order code of the sales lines under a header no rule knows and no README line describes (slot `order_key`)."""
    out, renamed, dropped = [], {}, set()
    for sh, df in pieces:
        roles = _roles(sh, df, readme)
        col = next((c for c, r in roles.items() if r == "order_id"), None)
        if col is None or "line_amount" not in roles.values():
            out.append((sh, df))
            continue
        renamed[col] = UNSEEN["order_id"]
        dropped.add(col)
        out.append((sh, df.rename(columns={col: UNSEEN["order_id"]})))
    return out, _readme_without(readme, dropped, renamed), renamed


def amount_before_discount(pieces: Pieces, readme: Readme, rng: random.Random):
    """Line amounts written before the line discount (slot `line_amount`)."""
    from datamate.audit import _LINE_DISCOUNT
    from datamate.ingest.roles import header_key

    out = []
    for sh, df in pieces:
        roles = {r: c for c, r in _roles(sh, df, readme).items()}
        disc = next((c for c in df.columns if not roles.get(c) and _LINE_DISCOUNT.match(header_key(c))), None)
        if disc is None or not {"qty", "unit_price", "line_amount"} <= set(roles):
            out.append((sh, df))
            continue
        df = df.copy()
        df[roles["line_amount"]] = df[roles["line_amount"]].astype(object)
        d, q, p = (df[c].map(parse_number) for c in (disc, roles["qty"], roles["unit_price"]))
        hit = d.fillna(0).gt(0) & q.notna() & p.notna()
        df.loc[hit, roles["line_amount"]] = (q * p)[hit]
        out.append((sh, df))
    return out, readme, {}


def _short(name: str) -> str:
    """'Coca Cola 330ml' -> 'Coca C330ml': the first word, then initials, words with a digit kept whole."""
    words = str(name).split()
    return " ".join([words[0], "".join(w if any(ch.isdigit() for ch in w) else w[0] for w in words[1:])]).strip()


def abbrev_names(pieces: Pieces, readme: Readme, rng: random.Random):
    """Four sales lines in five name the product short, and a two-column sheet lists each short form next to the full
    name, as a hand-kept book does (slot `name_map`). Keyed on the row, so identical rows stay identical."""
    full: dict[str, str] = {}
    for sh, df in pieces:
        roles = _roles(sh, df, readme)
        col = next((c for c, r in roles.items() if r == "product_name"), None)
        if col and "line_amount" in roles.values():
            for v in df[col].dropna().unique():
                if isinstance(v, str) and len(v.split()) >= 2:
                    full.setdefault(fold(v).strip(), v.strip())
    if not full:
        return pieces, readme, {}
    short, seen = {}, Counter()
    for k, v in sorted(full.items()):
        s = _short(v)
        seen[fold(s)] += 1
        short[k] = s if seen[fold(s)] == 1 else f"{s} {seen[fold(s)]}"
    out = []
    for sh, df in pieces:
        roles = _roles(sh, df, readme)
        col = next((c for c, r in roles.items() if r == "product_name"), None)
        if col and "line_amount" in roles.values():
            df = df.copy()
            hit = [random.Random(str(row)).random() < 0.8 for row in df.itertuples(index=False, name=None)]
            df.loc[hit, col] = df.loc[hit, col].map(lambda v: short.get(fold(v).strip(), v) if isinstance(v, str) else v)
        out.append((sh, df))
    sheet = pd.DataFrame({"Ghi tắt": [short[k] for k in sorted(full)], "Tên hàng": [full[k] for k in sorted(full)]})
    out.append((RawSheet("GhiTat.xlsx", "Ghi tắt", sheet, True), sheet))
    return out, readme, {}


PIECE_MODES = {"untouched": untouched, "shuffle": shuffle, "no_readme": no_readme, "rename": rename, "rename_no_readme": rename_no_readme,
               "formats": formats, "status_words": status_words, "name_spellings": name_spellings,
               "status_in_notes": status_in_notes, "rename_order_key": rename_order_key,
               "amount_before_discount": amount_before_discount, "abbrev_names": abbrev_names}
SLOT_MODES = ["rename_order_key", "status_in_notes", "abbrev_names", "amount_before_discount"]  # one per slot they test
MODES = [*(m for m in PIECE_MODES if m not in SLOT_MODES), "subtotal_rows"]


def mutated(biz_id: str, readme: Readme, sheets: list[RawSheet], pieces: Pieces, mode: str, seed: int):
    """(shop, renamed columns, rebuild(extra_roles) -> shop)."""
    rng = random.Random(f"{seed}:{biz_id}:{mode}")
    warn: list[str] = []
    renamed: dict = {}
    if mode == "subtotal_rows":
        new_pieces, new_readme = extract_pieces(subtotal_rows(sheets, readme, rng), readme.known_names(), warn), readme
    else:
        new_pieces, new_readme, renamed = PIECE_MODES[mode](pieces, readme, rng)

    def rebuild(extra_roles: dict | None = None) -> Business:
        return assemble(biz_id, "train", new_readme, new_pieces, list(warn), extra_roles=extra_roles)
    return rebuild(), renamed, rebuild


def model_steps(generate):
    """The ingest steps that call the model, for shops held in memory."""
    def after(biz: Business, rebuild) -> Business:
        roles = propose_roles(biz, generate)
        biz = rebuild(roles) if roles else biz
        labels = ask_labels(biz, generate)
        if labels:
            apply_status(biz, labels)
        return biz
    return after


def slot_steps(generate, rulebook: str, enabled=None):
    """The slots (slots/base.py) on shops held in memory: biz.slots set, the order code as a role."""
    from datamate.slots.base import ORDER, run_slots

    def after(biz: Business, rebuild) -> Business:
        applied, record = run_slots(biz, rulebook, generate, rebuild, enabled or ORDER)
        key = applied.get("order_key")
        out = rebuild({**biz.extra_roles, key["table"]: {**biz.extra_roles.get(key["table"], {}), key["column"]: "order_id"}}) if key else biz
        out.slots = {k: v for k, v in applied.items() if k != "order_key"}
        print(f"  {biz.id}: " + "; ".join(f"{k} {r['result']}" for k, r in record.items()), flush=True)
        return out
    return after


def recovered_roles(base: Business, biz: Business, renamed: dict) -> Counter:
    """Per role: columns that kept it ('role') and columns that lost it ('role!') or got another ('role?')."""
    c: Counter = Counter()
    for name, ti in base.info.items():
        now = biz.info[name].roles if name in biz.info else {}
        for col, role in ti.roles.items():
            got = now.get(renamed.get(col, col))
            c[role if got == role else f"{role}!" if got is None else f"{role}?"] += 1
    return c


def _entities(biz: Business) -> dict[str, int]:
    sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()}, biz.slots)
    return {k: int(sales[k].nunique()) for k in ("product_key", "customer_key") if sales is not None and k in sales.columns}


def _names(biz: Business) -> Counter:
    sales, _ = build_sales(biz.tables, {n: ti.roles for n, ti in biz.info.items()}, biz.slots)
    return Counter() if sales is None or "product_name" not in sales else Counter(sales["product_name"].dropna().map(fold))


def names_kept(base: Counter, now: Counter) -> tuple[int, int]:
    """(lines whose product name is the one the untouched shop has, lines): names as a multiset, rows need not align."""
    return sum(min(n, now[k]) for k, n in base.items()), sum(base.values())


def run_mutations(data_root: Path, modes: list[str] | None = None, shops: list[str] | None = None, seed: int = 0, after=None) -> dict:
    """after(biz, rebuild) -> biz runs on each mutated shop, so the model steps can be measured against the plain rules."""
    train = Path(data_root) / "train"
    cal = load_calendar(train)
    checks, todo, gold = contract_items(train)
    reps = {m: empty_report(checks) for m in ["baseline", *(modes or MODES)]}
    roles: dict[str, Counter] = defaultdict(Counter)
    entities: dict[str, Counter] = defaultdict(Counter)
    names: dict[str, Counter] = defaultdict(Counter)
    for biz_id in sorted(shops or todo):
        bdir = train / "data" / biz_id
        readme = parse_readme((bdir / "README.md").read_text(encoding="utf-8") if (bdir / "README.md").exists() else "")
        warn: list[str] = []
        sheets = read_sheets(bdir, warn)
        pieces = extract_pieces(sheets, readme.known_names(), warn)
        base = assemble(biz_id, "train", readme, pieces, warn)
        score_contracts(base, todo[biz_id], cal, gold, reps["baseline"])
        ents = _entities(base)
        entities["baseline"].update(ents)
        base_names = _names(base)
        for mode in reps:
            if mode == "baseline":
                continue
            biz, renamed, rebuild = mutated(biz_id, readme, sheets, pieces, mode, seed)
            biz = after(biz, rebuild) if after else biz
            roles[mode].update(recovered_roles(base, biz, renamed))
            score_contracts(biz, todo[biz_id], cal, gold, reps[mode], max_misses=2)
            entities[mode].update(_entities(biz))
            kept_n, all_n = names_kept(base_names, _names(biz))
            names[mode].update({"kept": kept_n, "lines": all_n})
        print(f"{biz_id} done", flush=True)
    out = {}
    for mode, rep in reps.items():
        kept = sum(n for r, n in roles[mode].items() if r[-1] not in "!?")
        total = sum(roles[mode].values())
        out[mode] = {"exact": sum(r["exact"] for r in rep.values()), "matched": sum(r["matched"] for r in rep.values()),
                     "by_check": {k: [r["exact"], r["matched"]] for k, r in rep.items()},
                     "skipped": dict(sum((Counter(r["skipped"]) for r in rep.values()), Counter())),
                     "roles_kept": [kept, total], "lost": {r: n for r, n in roles[mode].most_common() if r[-1] in "!?"},
                     "entities": dict(entities[mode]), "names_kept": [names[mode]["kept"], names[mode]["lines"]], "misses": [m for r in rep.values() for m in r["misses"]][:6]}
    return out


# (clause, new text or None to delete it, does a number change?)
RULE_VARIANTS = [
    ("Đơn tính vào doanh thu", "đơn **không huỷ**. Đơn trả một phần vẫn tính phần còn lại; đơn đang giao chưa tính", True),
    ("Doanh thu một đơn", "tổng tiền cả đơn sau giảm giá trên đơn, đã gồm phí giao hàng", True),
    ("Đơn thuộc kỳ nào", "theo **ngày giao hàng**, không theo ngày đặt", True),
    ("Số đơn", "tính mọi đơn đã đặt, kể cả đơn huỷ và đơn trả", True),
    ("Đơn bị huỷ", "đơn có trạng thái huỷ hoặc trả hàng", True),
    ("Tỉ lệ đơn huỷ trong kỳ", "phần trăm đơn huỷ trong các đơn đã hoàn thành của kỳ", True),
    ("Giá trị trung bình một đơn", "làm tròn xuống đến nghìn đồng", True),
    ("Đơn tính vào doanh thu", None, True),
    ("Đơn thuộc kỳ nào", "theo **ngày đặt hàng**; ngày giao và ngày thu tiền không dùng để xếp đơn vào kỳ", False),
    ("Đơn bị huỷ", "đơn mang trạng thái huỷ. Đơn trả hàng thì **không** được xem là đơn huỷ", False),
]


def run_rule_variants(rulebook: str, cache_dir: Path, generate) -> dict:
    """Does the gate turn a definition off for each changed clause, and leave it on for each rewording?"""
    rows = []
    for i, (name, text, changed) in enumerate(RULE_VARIANTS):
        out = []
        for line in rulebook.splitlines():
            if line.startswith(f"| {name} |"):
                line = None if text is None else f"| {name} | {text} |"
            if line is not None:
                out.append(line)
        off = rule_gate("\n".join(out), Path(cache_dir) / f"variant{i}.json", generate)
        rows.append({"clause": name, "changed": changed, "off": sorted(off), "ok": bool(off) == changed})
    return {"caught": [sum(r["ok"] for r in rows if r["changed"]), sum(r["changed"] for r in rows)],
            "kept": [sum(r["ok"] for r in rows if not r["changed"]), sum(not r["changed"] for r in rows)], "rows": rows}
