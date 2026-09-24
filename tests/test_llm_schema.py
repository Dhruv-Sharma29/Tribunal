"""Schema dialect adaptation.

These tests matter more than they look. An unadapted schema does not fail loudly -- the
provider *ignores* it -- so the failure surfaces much later as an unexplained parse-retry
rate. The assertions here are the only place that mistake is caught.
"""

from __future__ import annotations

import json

import pytest

from tribunal.contracts import ArbiterNote, Critique, PatchProposal
from tribunal.llm import schema as S

AGENT_MODELS = [Critique, PatchProposal, ArbiterNote]


@pytest.fixture
def critique_schema() -> dict:
    return Critique.model_json_schema()


# -- $ref inlining ---------------------------------------------------------------------------


def test_inlining_removes_every_ref_and_def(critique_schema):
    out = S.inline_defs(critique_schema)
    blob = json.dumps(out)
    assert "$ref" not in blob
    assert "$defs" not in blob


def test_inlining_preserves_the_nested_structure(critique_schema):
    """Inlining must not flatten the model -- the critic still has to emit nested issues."""
    out = S.inline_defs(critique_schema)
    issue = out["properties"]["issues"]["items"]
    assert "confidence" in issue["properties"]
    evidence = issue["properties"]["evidence"]["items"]
    assert sorted(evidence["properties"]) == ["excerpt", "kind", "ref"]
    assert issue["properties"]["severity"]["enum"] == ["info", "low", "medium", "high"]


def test_inlining_copies_rather_than_shares(critique_schema):
    """Two uses of one `$def` must not alias, or a later per-node rewrite hits both."""
    out = S.inline_defs(critique_schema)
    issue = out["properties"]["issues"]["items"]
    issue["properties"]["severity"]["enum"] = ["mutated"]
    fresh = S.inline_defs(critique_schema)
    assert fresh["properties"]["issues"]["items"]["properties"]["severity"]["enum"] != ["mutated"]


def test_sibling_keys_beside_a_ref_win():
    schema = {
        "$defs": {"Thing": {"type": "string", "description": "from the def"}},
        "properties": {"a": {"$ref": "#/$defs/Thing", "description": "from the use site"}},
        "type": "object",
    }
    out = S.inline_defs(schema)
    assert out["properties"]["a"]["type"] == "string"
    assert out["properties"]["a"]["description"] == "from the use site"


def test_a_recursive_ref_raises_instead_of_hanging():
    """docs/02-contracts.md says keep the model tree shallow. This makes violating that a
    build-time failure rather than a hang."""
    schema = {
        "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
        "properties": {"root": {"$ref": "#/$defs/Node"}},
        "type": "object",
    }
    with pytest.raises(S.SchemaTooDeep, match="recursive"):
        S.inline_defs(schema)


def test_a_dangling_ref_raises():
    with pytest.raises(S.SchemaTooDeep, match="dangling"):
        S.inline_defs({"properties": {"a": {"$ref": "#/$defs/Missing"}}, "$defs": {"X": {}}})


def test_depth_counts_ref_expansions_not_dict_traversal(critique_schema):
    """`properties -> issues -> items -> $ref` is one level of model nesting but four dict
    hops. Counting hops instead of expansions rejected Critique outright at the default limit.

    Critique's deepest chain is Issue -> Evidence -> EvidenceKind: exactly three expansions.
    """
    assert S.inline_defs(critique_schema, max_depth=3)
    with pytest.raises(S.SchemaTooDeep, match="exceeded 2 levels"):
        S.inline_defs(critique_schema, max_depth=2)


def test_the_default_depth_limit_accommodates_the_real_contracts(critique_schema):
    for model in AGENT_MODELS:
        assert S.inline_defs(model.model_json_schema())


# -- required / additionalProperties ---------------------------------------------------------


def test_openai_requires_every_property(critique_schema):
    """`positive_notes` has a default, so Pydantic omits it from `required` -- and OpenAI's
    strict mode rejects that."""
    assert "positive_notes" not in critique_schema["required"]
    out = S.for_openai(critique_schema).schema
    assert set(out["required"]) == set(out["properties"])


def test_openai_requires_every_property_at_every_level(critique_schema):
    out = S.for_openai(critique_schema).schema
    for name, definition in out["$defs"].items():
        if "properties" in definition:
            assert set(definition["required"]) == set(definition["properties"]), name


def test_openai_forces_additional_properties_false(critique_schema):
    out = S.for_openai(critique_schema).schema

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                yield node
            for value in node.values():
                yield from objects(value)
        elif isinstance(node, list):
            for item in node:
                yield from objects(item)

    found = list(objects(out))
    assert found
    assert all(o.get("additionalProperties") is False for o in found)


def test_gemini_strips_additional_properties(critique_schema):
    out = S.for_gemini(critique_schema).schema
    assert "additionalProperties" not in json.dumps(out)


# -- value constraints -----------------------------------------------------------------------


def test_the_unenforceable_confidence_constraint_is_reported(critique_schema):
    """No provider's constrained decoding enforces `multipleOf`. The adaptation must say so,
    because that is what turns a silent contract hole into a known one."""
    for adapt in (S.for_anthropic, S.for_openai, S.for_gemini, S.for_nim):
        dropped = adapt(critique_schema).dropped_constraints
        confidence_paths = [p for p in dropped if p.endswith("confidence")]
        assert confidence_paths, f"{adapt.__name__} did not report the confidence constraint"
        assert "multipleOf" in dropped[confidence_paths[0]]


def test_constraints_inside_defs_are_reached(critique_schema):
    """The first version of the walker only followed `properties`/`items`/`anyOf`, so `$defs`
    -- where every nested model lives -- was never visited and `Issue.confidence` kept its
    `multipleOf`."""
    out = S.for_openai(critique_schema).schema
    assert "multipleOf" not in json.dumps(out)
    assert "maxLength" not in json.dumps(out)


def test_gemini_keeps_the_constraints_it_documents(critique_schema):
    out = S.for_gemini(critique_schema).schema
    blob = json.dumps(out)
    assert "minItems" in blob  # Issue.evidence min_length=1 survives
    assert "multipleOf" not in blob


def test_anthropic_leaves_the_document_intact_but_still_warns(critique_schema):
    """Anthropic accepts the schema as emitted, so nothing is stripped -- but the caller needs
    the same warning about what decoding will not enforce."""
    adapted = S.for_anthropic(critique_schema)
    assert "multipleOf" in json.dumps(adapted.schema)
    assert adapted.dropped_constraints


def test_dropped_summary_is_readable(critique_schema):
    summary = S.for_openai(critique_schema).dropped_summary
    assert "confidence" in summary
    assert S.AdaptedSchema(schema={}).dropped_summary == "none"


# -- every agent model, every dialect --------------------------------------------------------


@pytest.mark.parametrize("model", AGENT_MODELS, ids=lambda m: m.__name__)
@pytest.mark.parametrize(
    "adapt", [S.for_anthropic, S.for_openai, S.for_gemini, S.for_nim], ids=lambda f: f.__name__
)
def test_every_agent_schema_adapts_to_every_dialect(model, adapt):
    out = adapt(model.model_json_schema()).schema
    assert out["type"] == "object"
    assert out["properties"]
    json.dumps(out)  # must stay serialisable


@pytest.mark.parametrize("model", AGENT_MODELS, ids=lambda m: m.__name__)
def test_adaptation_never_mutates_the_input(model):
    original = model.model_json_schema()
    snapshot = json.dumps(original, sort_keys=True)
    for adapt in (S.for_anthropic, S.for_openai, S.for_gemini, S.for_nim):
        adapt(original)
    assert json.dumps(original, sort_keys=True) == snapshot
