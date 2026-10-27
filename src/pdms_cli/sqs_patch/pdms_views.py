"""Loaded by this folder's sitecustomize.py in services run by pdms with ``PDMS_VIEW_MAP``: their queries read a test
copy of a materialized view instead of the real one (``pdms view``, Data → Test views).

``PDMS_VIEW_MAP`` is ``real=test,real=test``. Every statement SQLAlchemy sends gets each real name replaced by its
test one, as a whole identifier only (``pdms_report_document_view_expression`` stays), whether it came from the ORM, a
``text()`` or a ``REFRESH MATERIALIZED VIEW``. Only the services pdms starts see it: the deployed ones keep the real
view.
"""

import os
import re

NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def view_map():
    """``{real: test}`` from ``PDMS_VIEW_MAP``, keeping only well-formed names."""
    found = {}
    for pair in os.environ.get("PDMS_VIEW_MAP", "").split(","):
        real, _, test = pair.partition("=")
        real, test = real.strip(), test.strip()
        if NAME.match(real) and NAME.match(test) and real != test:
            found[real] = test
    return found


def rewriter(mapping):
    """A function giving a statement with every real name (a whole identifier) replaced by its test one."""
    if not mapping:
        return lambda statement: statement
    pattern = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, sorted(mapping, key=len, reverse=True)))
                         + r")(?![A-Za-z0-9_])")
    return lambda statement: pattern.sub(lambda match: mapping[match.group(1)], statement)


def install():
    mapping = view_map()
    if not mapping:
        return
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except ImportError:  # not a service with SQLAlchemy (poetry itself...)
        return
    rewrite = rewriter(mapping)

    def before(conn, cursor, statement, parameters, context, executemany):
        return rewrite(statement), parameters

    event.listen(Engine, "before_cursor_execute", before, retval=True)
