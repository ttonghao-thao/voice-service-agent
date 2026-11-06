"""Bind pronouns only to unique, authorized catalog names in actual user input.

Model-generated queries and assistant replies never authorize an entity ID.
No fuzzy matching: ASR aliases must be curated in CueKB's entity catalog.
"""
import re
from uuid import UUID

from app.task_context import REFERENCE, SPECULATIVE


def normalized(text):
    return ' '.join(re.findall(r'[a-z0-9]+', text.casefold()))


def context_binding(question, recent_inputs, entities):
    if not REFERENCE.search(question):
        return []
    rows = [question, *[row['text'] for row in reversed(recent_inputs) if row['text'] != question]]
    for text in rows:
        if SPECULATIVE.search(text) or re.search(r"\b(?:not|wrong|forget|no longer|don't|do not)\b", text, re.I):
            return []
        haystack = ' ' + normalized(text) + ' '
        matches = set()
        for entity in entities:
            names = [entity['name'], *entity.get('aliases', [])]
            if entity.get('product_model'):
                names.append(entity['product_model'])
            if any(normalized(name) and ' ' + normalized(name) + ' ' in haystack for name in names):
                matches.add(str(UUID(entity['id'])))
        if matches:
            return ([{'entity_id': next(iter(matches)), 'source': 'confirmed_conversation_context'}]
                    if len(matches) == 1 else [])
        # An unrelated explicit topic or unknown replacement breaks the chain.
        # Reference-only follow-ups can retain a previous explicit subject.
        if not REFERENCE.search(text):
            return []
    return []
