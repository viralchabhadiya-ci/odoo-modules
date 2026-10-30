from odoo.fields import Domain
from odoo.tests import BaseCase, tagged

from ..models.odata_filter import ODataError, like_escape, parse_filter, tokenize


def resolve(name):
    if name not in ("name", "qty", "price", "active", "date", "stamp", "partner"):
        raise ODataError(f"There is no column '{name}'.")
    return name, (lambda v: v)


@tagged("post_install", "-at_install")
class TestODataFilter(BaseCase):
    def parse(self, text):
        return parse_filter(text, resolve)

    def test_comparisons(self):
        self.assertEqual(self.parse("name eq 'Ann'"), Domain("name", "=", "Ann"))
        self.assertEqual(self.parse("qty ne 3"), Domain("qty", "!=", 3))
        self.assertEqual(self.parse("price ge 1.5"), Domain("price", ">=", 1.5))
        self.assertEqual(self.parse("price lt -2"), Domain("price", "<", -2))
        self.assertEqual(self.parse("active eq true"), Domain("active", "=", True))
        self.assertEqual(self.parse("active eq false"), Domain("active", "=", False))

    def test_null_means_empty(self):
        self.assertEqual(self.parse("name eq null"), Domain("name", "=", False))
        self.assertEqual(self.parse("name ne null"), Domain("name", "!=", False))

    def test_quotes_inside_text(self):
        self.assertEqual(self.parse("name eq 'O''Brien'"), Domain("name", "=", "O'Brien"))

    def test_dates_and_datetimes(self):
        self.assertEqual(self.parse("date ge 2026-10-29"), Domain("date", ">=", "2026-10-29"))
        self.assertEqual(self.parse("stamp lt 2026-10-29T10:00:00Z"), Domain("stamp", "<", "2026-10-29T10:00:00Z"))
        self.assertEqual(
            self.parse("stamp lt 2026-10-29T10:00:00.5+02:00"), Domain("stamp", "<", "2026-10-29T10:00:00.5+02:00")
        )

    def test_and_or_not_and_brackets(self):
        a, b, c = Domain("qty", ">", 1), Domain("price", "<", 5), Domain("name", "=", "x")
        self.assertEqual(self.parse("qty gt 1 and price lt 5"), a & b)
        self.assertEqual(self.parse("qty gt 1 or price lt 5"), a | b)
        self.assertEqual(self.parse("qty gt 1 or price lt 5 and name eq 'x'"), a | (b & c))
        self.assertEqual(self.parse("(qty gt 1 or price lt 5) and name eq 'x'"), (a | b) & c)
        self.assertEqual(self.parse("not (qty gt 1)"), ~a)

    def test_text_functions(self):
        self.assertEqual(self.parse("contains(name,'an')"), Domain("name", "like", "an"))
        self.assertEqual(self.parse("startswith(name,'A_')"), Domain("name", "=like", "A\\_%"))
        self.assertEqual(self.parse("endswith(name,'50%')"), Domain("name", "=like", "%50\\%"))
        self.assertEqual(
            self.parse("contains(name, 'a') and qty gt 1"), Domain("name", "like", "a") & Domain("qty", ">", 1)
        )
        self.assertEqual(like_escape("a%b_c\\"), "a\\%b\\_c\\\\")

    def test_value_on_the_left(self):
        self.assertEqual(self.parse("5 lt price"), Domain("price", ">", 5))

    def test_what_cannot_be_read_is_said(self):
        for text in (
            "nothing eq 1",
            "name eq",
            "name like 'a'",
            "name eq 'a' extra",
            "(name eq 'a'",
            "name eq 'a' and",
            "year(date) eq 2026",
            "name eq name",
            "contains(name, 3)",
            "name eq @",
        ):
            with self.assertRaises(ODataError, msg=text):
                self.parse(text)

    def test_tokens(self):
        kinds = [kind for kind, _value in tokenize("name eq 'a' and qty gt 1 or active eq true")]
        self.assertEqual(
            kinds, ["name", "name", "string", "name", "name", "name", "number", "name", "name", "name", "const"]
        )
