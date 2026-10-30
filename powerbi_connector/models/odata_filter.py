"""A small OData v4 `$filter` reader: just what Power BI sends when it folds a query to the server.

expr  := and ('or' and)*          and  := not ('and' not)*        not := 'not' not | atom
atom  := '(' expr ')' | func '(' operand ',' operand ')' | operand op operand
op    := eq ne gt ge lt le        operand := column | 'text' | number | true | false | null | date | datetime
"""

import re

from odoo.fields import Domain

TOKEN = re.compile(
    r"""\s*(?:
        (?P<datetime>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)
      | (?P<date>\d{4}-\d{2}-\d{2})
      | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
      | (?P<string>'(?:[^']|'')*')
      | (?P<name>[A-Za-z_][A-Za-z0-9_./]*)
      | (?P<punct>[(),])
    )""",
    re.VERBOSE,
)
OPERATORS = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
FUNCTIONS = {"contains", "startswith", "endswith"}


class ODataError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def tokenize(text):
    tokens, pos = [], 0
    text = text.strip()
    while pos < len(text):
        match = TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise ODataError(f"Cannot read the filter near '{text[pos : pos + 20]}'.")
        kind = match.lastgroup
        value = match.group(kind)
        if kind == "name" and value in ("true", "false", "null"):
            kind = "const"
        tokens.append((kind, value))
        pos = match.end()
    return tokens


def like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Parser:
    def __init__(self, text, resolve):
        """`resolve(name)` -> (odoo_field, convert) where convert(value) turns a literal into what the ORM wants."""
        self.tokens = tokenize(text)
        self.pos = 0
        self.resolve = resolve

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def take(self, kind=None, value=None):
        token = self.peek()
        if token[0] is None or (kind and token[0] != kind) or (value and token[1] != value):
            raise ODataError("The filter is incomplete or not supported.")
        self.pos += 1
        return token

    def accept(self, value):
        token = self.peek()
        if token[1] == value and token[0] in ("name", "punct"):
            self.pos += 1
            return True
        return False

    def parse(self):
        domain = self.expr()
        if self.pos != len(self.tokens):
            raise ODataError("The filter has something left over that is not supported.")
        return domain

    def expr(self):
        left = self.and_expr()
        while self.accept("or"):
            left = left | self.and_expr()
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.accept("and"):
            left = left & self.not_expr()
        return left

    def not_expr(self):
        if self.accept("not"):
            return ~self.not_expr()
        return self.atom()

    def atom(self):
        kind, value = self.peek()
        if value == "(" and kind == "punct":
            self.pos += 1
            inner = self.expr()
            self.take("punct", ")")
            return inner
        if (
            kind == "name"
            and value in FUNCTIONS
            and self.pos + 1 < len(self.tokens)
            and self.tokens[self.pos + 1][1] == "("
        ):
            return self.function()
        return self.comparison()

    def function(self):
        name = self.take("name")[1]
        self.take("punct", "(")
        column = self.take("name")[1]
        self.take("punct", ",")
        kind, raw = self.take()
        if kind != "string":
            raise ODataError(f"{name}() needs a text value.")
        self.take("punct", ")")
        field, convert = self.resolve(column)
        text = convert(literal(kind, raw))
        if name == "contains":
            return Domain(field, "like", text)
        return Domain(field, "=like", like_escape(text) + "%" if name == "startswith" else "%" + like_escape(text))

    def comparison(self):
        left = self.take()
        op = self.take("name")[1]
        if op not in OPERATORS:
            raise ODataError(f"'{op}' is not supported in a filter.")
        right = self.take()
        if left[0] != "name" and right[0] == "name":  # 5 lt price -> price gt 5
            left, right = right, left
            op = {"gt": "lt", "ge": "le", "lt": "gt", "le": "ge"}.get(op, op)
        if left[0] != "name" or right[0] == "name":
            raise ODataError("A filter compares a column with a value.")
        field, convert = self.resolve(left[1])
        value = convert(literal(*right))
        if value is None:
            return Domain(field, "=" if op == "eq" else "!=", False)
        return Domain(field, OPERATORS[op], value)


def literal(kind, raw):
    if kind == "string":
        return raw[1:-1].replace("''", "'")
    if kind == "number":
        return float(raw) if any(c in raw for c in ".eE") else int(raw)
    if kind in ("date", "datetime"):
        return raw
    if kind == "const":
        return {"true": True, "false": False, "null": None}[raw]
    return raw


def parse_filter(text, resolve):
    return Parser(text, resolve).parse()
