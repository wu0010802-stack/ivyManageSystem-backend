import pytest
from config.validators import parse_bool_env, parse_csv_list


class TestParseBoolEnv:
    @pytest.mark.parametrize(
        "v", ["1", "true", "TRUE", "True", "yes", "YES", " 1 ", " true "]
    )
    def test_truthy(self, v):
        assert parse_bool_env(v) is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "FALSE", "", "off", "enabled", "xyz", None]
    )
    def test_falsy(self, v):
        assert parse_bool_env(v) is False

    def test_bool_passthrough(self):
        assert parse_bool_env(True) is True
        assert parse_bool_env(False) is False


class TestParseCsvList:
    def test_basic(self):
        assert parse_csv_list("a,b,c") == ["a", "b", "c"]

    def test_strip_whitespace(self):
        assert parse_csv_list(" a , b , c ") == ["a", "b", "c"]

    def test_empty_filtered(self):
        assert parse_csv_list("a,,b,") == ["a", "b"]

    def test_none(self):
        assert parse_csv_list(None) == []

    def test_empty_string(self):
        assert parse_csv_list("") == []

    def test_list_passthrough(self):
        assert parse_csv_list(["a", "b"]) == ["a", "b"]
