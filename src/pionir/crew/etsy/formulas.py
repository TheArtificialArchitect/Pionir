"""A small spreadsheet-formula evaluator for exactly the formulas the makers write.

Two jobs: the preview images show each workbook's example sheet WITH its computed values
(read back from the real .xlsx, so the picture is the file), and the tests prove every
formula computes and refers to the cells it should. A spreadsheet app computes them again
when the buyer opens the file; nothing here is shipped to the buyer.

Supported, and nothing else (an unknown function or token raises ``FormulaError`` - the
maker's tests then fail rather than ship a formula nobody checked): numbers, "strings",
cell references (``B5``, ``$B$2``), ranges inside a function (``B5:AF5``), ``+ - * /``,
unary minus, comparisons ``= <> < > <= >=``, and ``SUM``, ``COUNTIF`` (equality with a
literal), ``COUNTA``, ``IF``, ``IFERROR``, ``ROUND``, ``MAX``, ``MIN``.
"""
from __future__ import annotations

import re
from collections.abc import Callable

FUNCTIONS = frozenset({"SUM", "COUNTIF", "COUNTA", "IF", "IFERROR", "ROUND", "MAX", "MIN"})
_TOKEN = re.compile(r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<str>\"[^\"]*\")|"
                    r"(?P<range>\$?[A-Z]{1,3}\$?\d+:\$?[A-Z]{1,3}\$?\d+)|"
                    r"(?P<ref>\$?[A-Z]{1,3}\$?\d+)|(?P<fn>[A-Z]+)\(|"
                    r"(?P<op><=|>=|<>|[-+*/=<>(),]))")


class FormulaError(ValueError):
    pass


class CellError:
    """A spreadsheet error value (#DIV/0!); arithmetic on it stays an error."""

    def __init__(self, code: str) -> None:
        self.code = code

    def __repr__(self) -> str:
        return self.code

    def __eq__(self, other) -> bool:
        return isinstance(other, CellError) and other.code == self.code

    def __hash__(self) -> int:
        return hash(self.code)


DIV0 = CellError("#DIV/0!")


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def col_letters(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _split_ref(ref: str) -> tuple[str, int]:
    m = re.fullmatch(r"\$?([A-Z]{1,3})\$?(\d+)", ref)
    if not m:
        raise FormulaError(f"not a cell reference: {ref!r}")
    return m.group(1), int(m.group(2))


def expand(range_ref: str) -> list[str]:
    a, b = range_ref.split(":")
    (ca, ra), (cb, rb) = _split_ref(a), _split_ref(b)
    return [f"{col_letters(c)}{r}" for r in range(min(ra, rb), max(ra, rb) + 1)
            for c in range(min(col_index(ca), col_index(cb)),
                           max(col_index(ca), col_index(cb)) + 1)]


def references(formula: str) -> set[str]:
    """Every cell a formula reads (ranges expanded), without ``$``."""
    body = formula.removeprefix("=")
    out: set[str] = set()
    for m in _TOKEN.finditer(body):
        if m.group("range"):
            out.update(expand(m.group("range").replace("$", "")))
        elif m.group("ref"):
            out.add(m.group("ref").replace("$", ""))
    return out


def _tokens(formula: str) -> list[tuple[str, str]]:
    body = formula.removeprefix("=")
    pos, out = 0, []
    body = body.rstrip()
    while pos < len(body):
        m = _TOKEN.match(body, pos)
        if not m or m.end() == pos:
            raise FormulaError(f"cannot read {body[pos:pos + 12]!r} in {formula!r}")
        kind = m.lastgroup
        value = m.group(kind)
        if kind == "fn" and value not in FUNCTIONS:
            raise FormulaError(f"unsupported function {value} in {formula!r}")
        out.append((kind, value))
        pos = m.end()
    return out


def _num(v):
    if isinstance(v, CellError):
        return v
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return CellError("#VALUE!")


class Evaluator:
    """Evaluate the formulas of one sheet. ``cell(ref)`` gives a cell's raw content (a
    value, a "=formula" string or None); formulas are computed on demand, with a guard
    against circular references."""

    def __init__(self, cell: Callable[[str], object]) -> None:
        self._cell = cell
        self._cache: dict[str, object] = {}
        self._busy: set[str] = set()

    def value(self, ref: str):
        ref = ref.replace("$", "")
        if ref in self._cache:
            return self._cache[ref]
        raw = self._cell(ref)
        if isinstance(raw, str) and raw.startswith("="):
            if ref in self._busy:
                raise FormulaError(f"circular reference at {ref}")
            self._busy.add(ref)
            try:
                out = self.evaluate(raw)
            finally:
                self._busy.discard(ref)
        else:
            out = raw
        self._cache[ref] = out
        return out

    def evaluate(self, formula: str):
        # A reference inside this formula may evaluate another formula: the parser's
        # position is saved and restored around each one (they nest like a stack).
        saved = (getattr(self, "_toks", None), getattr(self, "_i", 0),
                 getattr(self, "_formula", None))
        try:
            self._toks = _tokens(formula)
            self._i = 0
            self._formula = formula
            out = self._compare()
            if self._i != len(self._toks):
                raise FormulaError(f"unexpected {self._toks[self._i][1]!r} in {formula!r}")
            return out
        finally:
            self._toks, self._i, self._formula = saved

    # ---- a recursive-descent parser that evaluates as it goes --------------------------
    def _peek(self):
        return self._toks[self._i] if self._i < len(self._toks) else (None, None)

    def _take(self, value=None):
        tok = self._peek()
        if tok[0] is None or (value is not None and tok[1] != value):
            raise FormulaError(f"expected {value or 'more'} in {self._formula!r}")
        self._i += 1
        return tok

    def _compare(self):
        left = self._additive()
        while self._peek()[1] in ("=", "<>", "<", ">", "<=", ">="):
            op = self._take()[1]
            right = self._additive()
            left = self._cmp(op, left, right)
        return left

    @staticmethod
    def _cmp(op, a, b):
        if isinstance(a, CellError):
            return a
        if isinstance(b, CellError):
            return b
        if isinstance(a, str) or isinstance(b, str):
            a, b = str(a or "").lower(), str(b or "").lower()
        else:
            a, b = _num(a), _num(b)
        return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b,
                ">=": a >= b}[op]

    def _additive(self):
        left = self._term()
        while self._peek()[1] in ("+", "-"):
            op = self._take()[1]
            right = self._term()
            a, b = _num(left), _num(right)
            if isinstance(a, CellError) or isinstance(b, CellError):
                left = a if isinstance(a, CellError) else b
            else:
                left = a + b if op == "+" else a - b
        return left

    def _term(self):
        left = self._unary()
        while self._peek()[1] in ("*", "/"):
            op = self._take()[1]
            right = self._unary()
            a, b = _num(left), _num(right)
            if isinstance(a, CellError) or isinstance(b, CellError):
                left = a if isinstance(a, CellError) else b
            elif op == "*":
                left = a * b
            else:
                left = DIV0 if b == 0 else a / b
        return left

    def _unary(self):
        if self._peek()[1] == "-":
            self._take()
            v = _num(self._unary())
            return v if isinstance(v, CellError) else -v
        return self._atom()

    def _atom(self):
        kind, value = self._take()
        if kind == "num":
            return float(value) if "." in value else int(value)
        if kind == "str":
            return value[1:-1]
        if kind == "ref":
            return self.value(value)
        if kind == "fn":
            return self._call(value)
        if value == "(":
            out = self._compare()
            self._take(")")
            return out
        raise FormulaError(f"unexpected {value!r} in {self._formula!r}")

    def _args(self) -> list:
        """Arguments as values; a range argument is a list of its cells' values."""
        args: list = []
        if self._peek()[1] == ")":
            self._take(")")
            return args
        while True:
            kind, value = self._peek()
            if kind == "range":
                self._take()
                args.append([self.value(r) for r in expand(value.replace("$", ""))])
            else:
                args.append(self._compare())
            if self._peek()[1] == ",":
                self._take(",")
                continue
            self._take(")")
            return args

    def _call(self, name: str):
        if name in ("IF", "IFERROR"):
            return self._lazy(name)
        args = self._args()
        flat = [x for a in args for x in (a if isinstance(a, list) else [a])]
        if name == "SUM":
            nums = [_num(x) for x in flat if x not in (None, "") and not isinstance(x, str)]
            err = next((x for x in nums if isinstance(x, CellError)), None)
            return err if err is not None else sum(nums)
        if name in ("MAX", "MIN"):
            nums = [_num(x) for x in flat if x not in (None, "") and not isinstance(x, str)]
            err = next((x for x in nums if isinstance(x, CellError)), None)
            if err is not None:
                return err
            return (max if name == "MAX" else min)(nums) if nums else 0
        if name == "COUNTA":
            return sum(1 for x in flat if x not in (None, ""))
        if name == "COUNTIF":
            if len(args) != 2 or not isinstance(args[0], list):
                raise FormulaError("COUNTIF takes a range and a literal")
            want = str(args[1]).lower()
            return sum(1 for x in args[0] if x is not None and str(x).lower() == want)
        if name == "ROUND":
            if len(args) != 2:
                raise FormulaError("ROUND takes two arguments")
            v, d = _num(args[0]), _num(args[1])
            if isinstance(v, CellError):
                return v
            return round(v, int(d))
        raise FormulaError(f"unsupported function {name}")

    def _lazy(self, name: str):
        """IF and IFERROR evaluate every argument (they have no side effects), then pick."""
        args = []
        while True:
            args.append(self._compare())
            if self._peek()[1] == ",":
                self._take(",")
                continue
            self._take(")")
            break
        if name == "IFERROR":
            if len(args) != 2:
                raise FormulaError("IFERROR takes two arguments")
            return args[1] if isinstance(args[0], CellError) else args[0]
        if len(args) not in (2, 3):
            raise FormulaError("IF takes two or three arguments")
        cond = args[0]
        if isinstance(cond, CellError):
            return cond
        return args[1] if cond else (args[2] if len(args) == 3 else False)
