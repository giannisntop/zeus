import markdown

# https://stackoverflow.com/a/41831049
# from django.conf import settings
# from django.db import DEFAULT_DB_ALIAS
# from django.db.transaction import Atomic, get_connection
from zeus.utils import sanitize_html
from contextlib import contextmanager
from django.db import connection, transaction


def parse_markdown(text):
    md = markdown.Markdown(
        safe_mode='escape',
        output_format='html5',
        extensions=[])
    html = md.convert(text)
    return sanitize_html(html)


@contextmanager
def lock_atomic(lock_id=1):
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT pg_advisory_lock({})".format(lock_id))
        with transaction.atomic():
            yield
    finally:
        cursor.execute("SELECT pg_advisory_unlock({})".format(lock_id))
