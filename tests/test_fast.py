import pandas as pd
import pytest

from datamate.fast import fast_answer, parse_frame
from datamate.ingest.trust import Trust

TRUSTED = Trust({"revenue": "trusted", "orders": "trusted"})
AS_OF = "2025-03-31"


def sales():
    return pd.DataFrame({"date": pd.to_datetime(["2025-03-01", "2025-03-02"]), "amount": [100, 50], "counted": [True, True],
                         "order_id": ["A", "B"], "status": ["hoàn thành", "hoàn thành"], "order_ok": [True, True], "is_goods": [True, True]})


@pytest.mark.parametrize("text, name", [("doanh thu tháng 3/2025 là bao nhiêu", "revenue"), ("cho tôi biết doanh thu tháng 3/2025", "revenue"),
                                        ("tháng 3/2025 tỉ lệ đơn huỷ là bao nhiêu phần trăm", "cancel_rate")])
def test_frames_explain_every_word(text, name):
    assert parse_frame(text, AS_OF, None).check.name == name


@pytest.mark.parametrize("text", ["doanh thu tháng 3/2025 của khách Lan là bao nhiêu", "doanh thu riêng tháng 3/2025 là bao nhiêu",
                                  "doanh thu tháng 3/2025 so với tháng 2/2025", "doanh thu là bao nhiêu", "tháng 3/2025 bán bia ra sao"])
def test_any_unknown_word_goes_to_the_model(text):
    assert parse_frame(text, AS_OF, None) is None


def test_answers_a_plain_question():
    answer, frame = fast_answer("doanh thu tháng 3/2025 là bao nhiêu", AS_OF, None, sales(), TRUSTED)
    assert answer["answer"] == 150 and frame.call == "month(2025, 3)"


def test_a_definition_that_is_off_goes_to_the_model():
    assert fast_answer("doanh thu tháng 3/2025 là bao nhiêu", AS_OF, None, sales(), TRUSTED, frozenset({"revenue"})) is None


def test_a_suspect_shop_goes_to_the_model():
    assert fast_answer("doanh thu tháng 3/2025 là bao nhiêu", AS_OF, None, sales(), Trust({"revenue": "suspect"})) is None


def test_a_period_outside_the_data_goes_to_the_model():
    assert fast_answer("doanh thu tháng 1/2024 là bao nhiêu", AS_OF, None, sales(), TRUSTED) is None
