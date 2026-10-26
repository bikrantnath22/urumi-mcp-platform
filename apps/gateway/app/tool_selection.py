"""Search the current user's permitted MCP tools without loading every schema.

Directory IDs belong only to this snapshot. Build a fresh directory from
list_gateway_tools(user); actual execution must still use execute_gateway_tool.
Tool descriptions and annotations are untrusted metadata, not authorization.
"""
import copy
import math
import re
from collections import Counter


MAX_SEARCH_RESULTS = 8
MAX_LOADED_TOOLS = 6
STOP_WORDS = frozenset(
    "a an and are as at be by for from how i in is it me my of on or "
    "please show tell that the their these this to use with without".split()
)


class ToolSelectionError(ValueError):
    pass


def _words(text):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    words = set()
    for word in re.findall(r"[^\W_]+", text.casefold()):
        if word in STOP_WORDS or word.isdecimal():
            continue
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith(
            ("ss", "us", "is")
        ):
            word = word[:-1]
        words.add(word)
    return words


def _short(text, size):
    value = " ".join((text or "").split())
    return value if len(value) <= size else value[:size - 3] + "..."


class ToolDirectory:
    def __init__(self, tools):
        self._tools = {}
        self._rows = []
        seen = set()

        for position, tool in enumerate(sorted(tools, key=lambda item: item.name)):
            if not tool.name or tool.name in seen:
                raise ToolSelectionError("Tool names must be nonempty and unique.")
            seen.add(tool.name)
            identity = f"t{position:05d}"
            snapshot = copy.deepcopy(tool)
            self._tools[identity] = snapshot
            fields = (snapshot.inputSchema or {}).get("properties") or {}
            field_names = list(fields) if isinstance(fields, dict) else []
            name_words = _words(snapshot.name)
            description_words = _words((snapshot.description or "")[:4000])
            argument_words = _words(" ".join(field_names))
            self._rows.append({
                "id": identity,
                "name": _short(snapshot.name, 120),
                "description": _short(snapshot.description, 120),
                "argument_names": [
                    _short(name, 60) for name in field_names[:8]
                ],
                "_name_words": name_words,
                "_description_words": description_words,
                "_argument_words": argument_words,
                "_all_words": name_words | description_words | argument_words,
            })

        frequency = Counter(
            word for row in self._rows for word in row["_all_words"]
        )
        count = len(self._rows)
        self._weights = {
            word: 1 + math.log((count + 1) / (occurrences + 1))
            for word, occurrences in frequency.items()
        }

    def search(self, query, *, limit=6, offset=0):
        if not isinstance(query, str) or len(query) > 4000:
            raise ToolSelectionError("Search query must contain at most 4000 characters.")
        if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ToolSelectionError("Search limit must be between 1 and 8.")
        if type(offset) is not int or offset < 0:
            raise ToolSelectionError("Search offset must be a nonnegative integer.")

        terms = _words(query)
        matches = []
        for row in self._rows:
            score = sum(
                self._weights.get(word, 1) * (
                    8 * (word in row["_name_words"])
                    + 2 * (word in row["_description_words"])
                    + (word in row["_argument_words"])
                )
                for word in terms
            )
            if score > 0 or not query.strip():
                matches.append((score, row))

        matches.sort(key=lambda item: (-item[0], item[1]["id"]))
        page = matches[offset:offset + limit]
        next_offset = offset + len(page)
        return {
            "items": [{
                key: copy.deepcopy(value)
                for key, value in row.items()
                if not key.startswith("_")
            } for score, row in page],
            "total_matches": len(matches),
            "next_offset": next_offset if next_offset < len(matches) else None,
        }

    def load(self, identities):
        if (
            not isinstance(identities, list)
            or len(identities) > MAX_LOADED_TOOLS
            or any(not isinstance(value, str) for value in identities)
        ):
            raise ToolSelectionError("Choose a list of at most six tool IDs.")
        if len(set(identities)) != len(identities):
            raise ToolSelectionError("Tool IDs must not be repeated.")
        if any(value not in self._tools for value in identities):
            raise ToolSelectionError("A selected tool is unavailable in this directory.")
        return [copy.deepcopy(self._tools[value]) for value in identities]
