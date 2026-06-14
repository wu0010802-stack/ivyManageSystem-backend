import re
from random import Random

from scripts.seedgen.fake import Faker


def _seq(seed):
    """用同一個 seed 產生 name/phone/id_number 的固定序列,供決定論比對。"""
    f = Faker(Random(seed))
    return [(f.name("M"), f.phone(), f.id_number("M")) for _ in range(10)]


def test_same_seed_same_sequence():
    assert _seq(42) == _seq(42)


def test_different_seed_differs():
    assert _seq(42) != _seq(43)


def test_phone_format():
    f = Faker(Random(1))
    for _ in range(50):
        assert re.match(r"^09\d{8}$", f.phone())


def test_id_number_format():
    f = Faker(Random(1))
    for gender in ("M", "F"):
        for _ in range(50):
            assert re.match(r"^[A-Z][12]\d{8}$", f.id_number(gender))


def test_id_number_gender_digit():
    f = Faker(Random(1))
    assert f.id_number("M")[1] == "1"
    assert f.id_number("F")[1] == "2"


def test_name_length():
    f = Faker(Random(7))
    for gender in ("M", "F"):
        for _ in range(50):
            n = f.name(gender)
            assert 2 <= len(n) <= 3
