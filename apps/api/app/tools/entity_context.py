"""Bind pronouns to unique catalog names in source-bound user input.

Model queries and assistant replies never authorize an entity ID. Aliases are
curated by CueKB; a changed, ambiguous or retracted subject breaks inheritance.
"""
import re
from uuid import UUID

from app.task_context import REFERENCE
from pydantic import BaseModel, Field, ValidationError


class CatalogEntity(BaseModel):
    id: UUID
    kb_id: UUID
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    product_model: str | None = Field(default=None, min_length=1, max_length=200)


def catalog_rows(rows, kb_id):
    """Validate only fields used for binding; never trust cross-scope rows."""
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("invalid_entity_catalog_page")
    result = []
    for row in rows:
        try:
            item = CatalogEntity.model_validate(row)
        except ValidationError as exc:
            raise ValueError("invalid_entity_catalog_row") from exc
        if (str(item.kb_id) != kb_id or not item.name.strip()
            or any(not alias.strip() or len(alias) > 200 for alias in item.aliases)):
            raise ValueError("invalid_entity_catalog_scope_or_alias")
        result.append(item.model_dump(mode="json"))
    return result


def normalized(text):
    text = text.casefold().replace('’', "'")
    text = re.sub(r"\b([a-z]+)n't\b", r'\1 not', text)
    return ' '.join(re.findall(r'[a-z0-9]+', text))


def context_binding(question, recent_inputs, entities):
    if not REFERENCE.search(question):
        return []
    rows = [question, *[row['text'] for row in reversed(recent_inputs) if row['text'] != question]]
    for text in rows:
        # A property negation ("Can it not be shipped?") is still about the
        # confirmed subject. Reject subject retractions/comparisons instead.
        if re.search(r"\b(?:compare|comparison|versus|between)\b", text, re.I):
            return []
        matches, named = set(), False
        clauses = [' ' + normalized(clause) + ' ' for clause in re.split(r"[,.;!?]", text)]
        for entity in entities:
            names = [entity['name'], *entity.get('aliases', [])]
            if entity.get('product_model'):
                names.append(entity['product_model'])
            for name in names:
                value = normalized(name)
                if not value or value in {'a', 'an', 'the', 'it', 'its', 'this', 'that', 'same', 'one'}:
                    continue
                for clause in clauses:
                    if ' ' + value + ' ' not in clause:
                        continue
                    named = True
                    # A correction can retract one named product and explicitly
                    # confirm another. Only positive mentions authorize IDs.
                    prefix = re.split(r"\b(?:but|instead|rather)\b", clause.split(value, 1)[0])[-1]
                    if re.search(r"\b(?:not|never|without|wrong|forget|no longer|don't|do not|if|suppose|imagine)\b", prefix, re.I):
                        continue
                    matches.add(str(UUID(entity['id'])))
        if matches:
            return ([{'entity_id': next(iter(matches)), 'source': 'confirmed_conversation_context'}]
                    if len(matches) == 1 else [])
        if named:
            return []
        if (re.search(r"\b(?:wrong|forget)\s+(?:the\s+|this\s+|that\s+|my\s+)?(?:product|model|device|one)\b", text, re.I)
            or re.search(r"\b(?:another|different|other|new|replacement)\s+(?:product|model|device|one)\b", text, re.I)):
            return []
        # An explicit unrecognized topic prevents a later pronoun from reaching
        # past it and binding to an older subject.
        if not REFERENCE.search(text):
            return []
    return []
