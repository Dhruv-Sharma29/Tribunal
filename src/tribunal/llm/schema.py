"""JSON Schema dialect adaptation.

Pydantic emits one schema; the four providers accept four different subsets of JSON Schema.
Sending an unadapted schema does not fail loudly -- it fails by being *ignored*, which shows
up much later as an unexplained parse-retry rate. So adaptation is explicit, and every
constraint that had to be dropped is reported.

What each provider needs:

| Provider | `$ref` | `additionalProperties` | `required` | Value constraints |
|---|---|---|---|---|
| Anthropic | kept | as emitted | as emitted | not enforced |
| OpenAI `strict` | kept | **forced `false`** | **every key** | dropped |
| Gemini | **inlined** | **stripped** | as emitted | dropped |
| NIM | kept | as emitted | every key | dropped (hint only) |

## The constraint that no provider can enforce

`Confidence` is `multiple_of=0.05` (docs/02-contracts.md design rule 3). **No provider's
constrained decoding enforces `multipleOf`.** That is not a bug in the adaptation -- it is a
property of every current structured-output implementation, and it means the quantisation rule
is a *validation* guard, not a *generation* guard.

The consequence is concrete: a model returning `confidence: 0.07` produces a schema-valid-
looking payload that our `Critique` rejects. Burning a full repair retry on that is waste, so
`dropped_constraints` is surfaced on the adapted schema and the agent layer applies
`contracts.quantise_confidence` locally before deciding a retry is warranted.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

#: Keywords that describe structure. Every provider understands these.
STRUCTURAL = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "prefixItems",
        "anyOf",
        "oneOf",
        "enum",
        "const",
        "title",
        "description",
        "$ref",
        "$defs",
        "additionalProperties",
        "nullable",
    }
)

#: Keywords that constrain a *value* rather than its shape. These are the ones providers
#: silently ignore, so they are dropped explicitly and reported.
VALUE_CONSTRAINTS = frozenset(
    {
        "multipleOf",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)

#: Gemini documents support for array bounds and `format`, so those survive there.
GEMINI_KEEPS = frozenset({"minItems", "maxItems", "format"})


@dataclass
class AdaptedSchema:
    """A schema rewritten for one provider, plus what had to be given up to get there."""

    schema: dict[str, Any]
    #: e.g. {"confidence": ["multipleOf"], "title": ["maxLength"]} -- the guarantees that are
    #: now validation-only. Never silently empty when something was stripped.
    dropped_constraints: dict[str, list[str]] = field(default_factory=dict)

    @property
    def dropped_summary(self) -> str:
        if not self.dropped_constraints:
            return "none"
        return ", ".join(
            f"{path}:{'+'.join(kws)}" for path, kws in sorted(self.dropped_constraints.items())
        )


class SchemaTooDeep(ValueError):
    """A recursive `$ref`, which cannot be inlined.

    docs/02-contracts.md § Getting the model to honour these schemas says to keep the model
    tree shallow because deeply recursive models do not generate valid schemas. This raises
    rather than looping, so violating that rule is a build-time failure, not a hang.
    """


def inline_defs(schema: dict[str, Any], max_depth: int = 8) -> dict[str, Any]:
    """Replace every `$ref` with a copy of its definition and drop `$defs`.

    For Gemini, whose documented keyword list does not include `$ref`. Copies rather than
    shares, so a later per-node rewrite cannot mutate two places at once.
    """
    defs = schema.get("$defs", {})
    if not defs:
        return copy.deepcopy(schema)

    def resolve(node: Any, seen: tuple[str, ...]) -> Any:
        # `seen` counts *$ref expansions*, not structural traversal. Counting the latter
        # would trip on `properties -> issues -> items -> $ref`, which is one level of model
        # nesting but four dict hops.
        if len(seen) > max_depth:
            raise SchemaTooDeep(
                f"$ref expansion exceeded {max_depth} levels: {' -> '.join(seen)}"
            )
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            if name in seen:
                raise SchemaTooDeep(f"recursive $ref: {' -> '.join((*seen, name))}")
            if name not in defs:
                raise SchemaTooDeep(f"dangling $ref: {ref}")
            target = resolve(defs[name], (*seen, name))
            # Sibling keys next to a $ref (Pydantic emits `description`, `default`) win over
            # the definition's own, which is what JSON Schema 2020-12 says.
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            return {**target, **resolve(siblings, seen)} if siblings else target
        return {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}

    return resolve({k: v for k, v in schema.items() if k != "$defs"}, ())


def require_every_property(schema: dict[str, Any]) -> dict[str, Any]:
    """Force every object's `required` to list all of its properties, recursively.

    OpenAI's `strict: true` demands this. It is not the semantic change it looks like:
    optionality is expressed by a nullable union (`anyOf: [T, null]`), not by omission from
    `required`. Our one affected field, `Critique.positive_notes`, has a `default_factory`
    rather than a `None` default -- so requiring it means the model must emit `[]` explicitly,
    which is a better outcome than it silently omitting the key.
    """
    out = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict) and props:
            node["required"] = sorted(props)
        for value in node.values():
            walk(value)

    walk(out)
    return out


def set_additional_properties(schema: dict[str, Any], value: bool | None) -> dict[str, Any]:
    """Force `additionalProperties` on every object, or strip it when `value` is None."""
    out = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        is_object = node.get("type") == "object" or "properties" in node
        if is_object:
            if value is None:
                node.pop("additionalProperties", None)
            else:
                node["additionalProperties"] = value
        for child in list(node.values()):
            walk(child)

    walk(out)
    return out


def drop_value_constraints(
    schema: dict[str, Any], keep: frozenset[str] = frozenset()
) -> AdaptedSchema:
    """Remove constraints the provider will ignore, recording each one against its field.

    Reporting them is the point. A dropped `multipleOf` is the difference between "the model
    cannot emit 0.07" and "the model can emit 0.07 and we will reject it" -- and the agent
    layer needs to know which world it is in before it spends a retry.
    """
    out = copy.deepcopy(schema)
    dropped: dict[str, list[str]] = {}

    removable = VALUE_CONSTRAINTS - keep

    def walk(node: Any, path: str) -> None:
        """Descend into every sub-schema, not a hand-picked list of keywords.

        The first version only followed `properties`/`items`/`anyOf`, so `$defs` -- where
        Pydantic puts every nested model -- was never visited and `Issue.confidence`'s
        `multipleOf` survived untouched. Walking everything and naming paths off `properties`
        and `$defs` is both shorter and correct.
        """
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return
        for keyword in sorted(removable):
            if keyword in node:
                node.pop(keyword)
                dropped.setdefault(path or "<root>", []).append(keyword)
        for key, value in node.items():
            if key in ("properties", "$defs") and isinstance(value, dict):
                for name, subschema in value.items():
                    walk(subschema, f"{path}.{name}" if path else name)
            else:
                walk(value, path)

    walk(out, "")
    return AdaptedSchema(schema=out, dropped_constraints=dropped)


# --------------------------------------------------------------------------------------------
# Per-provider entry points
# --------------------------------------------------------------------------------------------


def for_anthropic(schema: dict[str, Any]) -> AdaptedSchema:
    """Anthropic accepts the schema essentially as Pydantic emits it.

    `extra="forbid"` plus non-optional fields already gives the `additionalProperties: false`
    and complete `required` list that JSON-schema mode wants (docs/02-contracts.md). Value
    constraints are still not enforced by decoding, so they are reported as dropped even
    though they are left in the document -- the caller needs the same warning either way.
    """
    reported = drop_value_constraints(schema)
    return AdaptedSchema(
        schema=copy.deepcopy(schema), dropped_constraints=reported.dropped_constraints
    )


def for_openai(schema: dict[str, Any]) -> AdaptedSchema:
    """`strict: true` requires `additionalProperties: false` and a complete `required` list."""
    adapted = drop_value_constraints(schema)
    out = require_every_property(adapted.schema)
    out = set_additional_properties(out, False)
    return AdaptedSchema(schema=out, dropped_constraints=adapted.dropped_constraints)


def for_gemini(schema: dict[str, Any]) -> AdaptedSchema:
    """Gemini needs refs inlined and `additionalProperties` gone."""
    inlined = inline_defs(schema)
    adapted = drop_value_constraints(inlined, keep=GEMINI_KEEPS)
    out = set_additional_properties(adapted.schema, None)
    return AdaptedSchema(schema=out, dropped_constraints=adapted.dropped_constraints)


def for_nim(schema: dict[str, Any]) -> AdaptedSchema:
    """NIM is OpenAI-compatible, so send the OpenAI shape and hope for enforcement.

    The hosted endpoint documents JSON output but not strict schema validation, so the
    provider reports `StructureMode.NATIVE_JSON` and the schema travels as a *hint*. Sending
    it costs nothing and helps when the backend does honour it; relying on it would be wrong,
    which is why the mode is recorded on the response.
    """
    return for_openai(schema)
