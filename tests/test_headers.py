import pytest

from app.sheets.headers import (
    AmbiguousColumn,
    ColumnNotFound,
    column_letter,
    find_column,
    find_duplicates,
    last_nonempty_index,
    normalize,
)


def test_normalize():
    assert normalize("  Тикер\n /  код  MOEX ") == "тикер / код moex"
    assert normalize("Ёмкость") == normalize("емкость")
    assert normalize("A B") == "a b"
    assert normalize(None) == ""


def test_column_letter():
    assert [column_letter(i) for i in (0, 25, 26, 51, 52, 701, 702)] == ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"]
    with pytest.raises(ValueError):
        column_letter(-1)


def test_exact_beats_prefix():
    headers = ["Цена к SMA200", "Цена", "Цена обновлена"]
    assert find_column(headers, "цена") == 1


def test_prefix_match():
    assert find_column(["ID", "Тикер / код MOEX (авто)"], "Тикер / код MOEX") == 1


def test_prefix_ambiguous():
    with pytest.raises(AmbiguousColumn) as e:
        find_column(["Цена к SMA200", "Цена обновлена"], "Цена")
    assert e.value.indices == [0, 1]
    assert "A, B" in str(e.value)


def test_exact_duplicate_needs_occurrence():
    headers = ["P/E", "Баллы", "P/E"]
    with pytest.raises(AmbiguousColumn):
        find_column(headers, "P/E")
    assert find_column(headers, "P/E", occurrence=1) == 0
    assert find_column(headers, "P/E", occurrence=2) == 2
    with pytest.raises(ColumnNotFound):
        find_column(headers, "P/E", occurrence=3)
    with pytest.raises(ValueError):
        find_column(headers, "P/E", occurrence=0)


def test_not_found():
    with pytest.raises(ColumnNotFound):
        find_column(["A"], "B")
    with pytest.raises(ColumnNotFound):
        find_column(["A"], "  ")


def test_duplicates():
    dups = find_duplicates(["P/E", "ROE", "", "p/e ", "ROE", "x", ""])
    assert dups == {"P/E": [0, 3], "ROE": [1, 4]}


def test_last_nonempty():
    assert last_nonempty_index(["a", "", "b", " ", ""]) == 2
    assert last_nonempty_index([]) == -1
