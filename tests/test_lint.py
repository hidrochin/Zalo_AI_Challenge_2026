from datamate.agent.lint import mask_advice


def test_mask_used_as_table():
    code = "s = sales_lines()\nok = s['counted'] & in_period(s['date'], p)\ng = ok.groupby('customer')['order_id'].nunique()\n"
    assert "mine = s[ok]" in mask_advice(code)
    assert mask_advice("m = in_period(s['date'], p)\nx = m['amount'].sum()\n")


def test_mask_used_properly():
    assert mask_advice("ok = s['counted'] & ~s['walkin']\nmine = s[ok]\ng = mine.groupby('customer')\n") is None
    assert mask_advice("ok = s['counted']\nok = s[ok]\nx = ok['amount'].sum()\n") is None  # reassigned to rows first
    assert mask_advice("top = g[g['n'] >= 3]\nanswer_list(top['name'].tolist())\n") is None
    assert mask_advice("not python (") is None
