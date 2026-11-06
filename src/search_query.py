"""Shared full-text and * wildcard matching for local, cached table rows."""
from fnmatch import fnmatchcase
from functools import lru_cache
import shlex


def normalized(value):return str(value).strip().casefold().replace('ё','е')


@lru_cache(maxsize=256)
def tokens(query):
    try:
        lexer=shlex.shlex(query,posix=True);lexer.whitespace_split=True;lexer.commenters='';lexer.escape=''
        return tuple(normalized(token) for token in lexer)
    except ValueError:return tuple(normalized(token) for token in query.split())


def leaves(value):
    if isinstance(value,dict):
        for item in value.values():yield from leaves(item)
    elif isinstance(value,(list,tuple,set)):
        for item in value:yield from leaves(item)
    elif value is not None:yield normalized(value)


def matches(value,query):
    parts=tokens(str(query).strip())
    if not parts:return True
    fields=tuple(leaves(value));text=' '.join(fields);text+=' '+text.replace('"','')
    for part in parts:
        if '*' not in part:
            if part not in text:return False
        else:
            # Only * is special: brackets, ? and Windows backslashes stay literal.
            pattern=part.replace('[','[[]').replace('?','[?]')
            if not any(fnmatchcase(field,pattern) or fnmatchcase(field.replace('"',''),pattern) for field in fields):return False
    return True
