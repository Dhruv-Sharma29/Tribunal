"""Configuration: models, efforts, thresholds, budgets, sandbox limits, and the price table.

Everything tunable lives here so that a trace header can snapshot it (docs/06-observability.md
§ Principles: self-describing) and so that threshold tuning in Phase 5 touches one file.

Loaded from, in increasing precedence: defaults below, a `tribunal.toml` in the working
directory, `TRIBUNAL_*` environment variables, then explicit CLI flags.
"""

from __future__ import annotations

import tomllib
import warnings
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tribunal.contracts import Dimension, Severity
from tribunal.llm.base import Effort, ProviderName

SandboxMode = Literal["subprocess", "docker"]

# --------------------------------------------------------------------------------------------
# Price table
# --------------------------------------------------------------------------------------------

#: Stamped into every trace header so a trace stays interpretable after prices change
#: (docs/10-cost-and-limits.md § Price table). Bump when any row below changes.
PRICE_TABLE_VERSION = "2026-09-16"

#: Fallback ratios, used only where a provider does not publish an explicit cached-input
#: rate. Anthropic documents ~0.1x for reads and ~1.25x for writes; OpenAI and Google both
#: publish cached-input rates directly, and those are pinned per model instead.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


class ModelPrice(BaseModel):
    """One model's pinned commercial terms, with provenance.

    `source` and `intro_until` are not decoration. An eval result table quotes a cost row, and
    a cost row computed from an unattributed or silently-expired rate is a number nobody can
    defend. `price_for` warns once when a row's introductory window has passed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ProviderName
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    #: Published cached-input rate. None means "derive from CACHE_READ_MULTIPLIER".
    cached_input_per_mtok: float | None = None
    #: Published cache-*write* rate. Only Anthropic charges one.
    cache_write_per_mtok: float | None = None
    context: int | None = None
    #: `not_token_metered` covers NVIDIA's free developer tier and self-hosted NIM, where
    #: there is no per-token price at all. It is a distinct state from "we forgot to pin a
    #: price", and it keeps a legitimate $0.00 from looking like the silent-zero bug that
    #: `price_for` exists to prevent.
    billing: Literal["token_metered", "not_token_metered"] = "token_metered"
    source: str = ""
    #: Date (ISO) after which an introductory rate is superseded.
    intro_until: str | None = None
    standard_after_intro: tuple[float, float] | None = None

    @property
    def cached_input(self) -> float:
        if self.cached_input_per_mtok is not None:
            return self.cached_input_per_mtok
        return self.input_per_mtok * CACHE_READ_MULTIPLIER

    @property
    def cache_write(self) -> float:
        if self.cache_write_per_mtok is not None:
            return self.cache_write_per_mtok
        return self.input_per_mtok * CACHE_WRITE_MULTIPLIER

    def intro_expired(self, today: date | None = None) -> bool:
        if self.intro_until is None:
            return False
        return (today or date.today()) > date.fromisoformat(self.intro_until)


_ANTHROPIC_SOURCE = "anthropic first-party API rates"
_OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
_GEMINI_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
_NIM_SOURCE = "https://build.nvidia.com"

#: USD per million tokens. Keys are the exact model id strings each provider accepts --
#: never a date-suffixed variant, which Anthropic rejects.
PRICES: dict[str, ModelPrice] = {
    # -- Anthropic -------------------------------------------------------------------------
    "claude-opus-5": ModelPrice(
        provider=ProviderName.ANTHROPIC, input_per_mtok=5.00, output_per_mtok=25.00,
        context=1_000_000, source=_ANTHROPIC_SOURCE,
    ),
    "claude-sonnet-5": ModelPrice(
        provider=ProviderName.ANTHROPIC, input_per_mtok=2.00, output_per_mtok=10.00,
        context=1_000_000, source=_ANTHROPIC_SOURCE,
    ),
    "claude-haiku-4-5": ModelPrice(
        provider=ProviderName.ANTHROPIC, input_per_mtok=1.00, output_per_mtok=5.00,
        context=200_000, source=_ANTHROPIC_SOURCE,
    ),
    "claude-opus-4-8": ModelPrice(
        provider=ProviderName.ANTHROPIC, input_per_mtok=5.00, output_per_mtok=25.00,
        context=1_000_000, source=_ANTHROPIC_SOURCE,
    ),
    # -- OpenAI ----------------------------------------------------------------------------
    "gpt-6-astra": ModelPrice(
        provider=ProviderName.OPENAI, input_per_mtok=10.00, output_per_mtok=50.00,
        cached_input_per_mtok=1.00, source=_OPENAI_SOURCE,
    ),
    "gpt-5.6-sol": ModelPrice(
        provider=ProviderName.OPENAI, input_per_mtok=4.00, output_per_mtok=20.00,
        cached_input_per_mtok=0.40, context=1_050_000, source=_OPENAI_SOURCE,
        intro_until="2026-11-21", standard_after_intro=None,
    ),
    "gpt-5.6-terra": ModelPrice(
        provider=ProviderName.OPENAI, input_per_mtok=2.00, output_per_mtok=12.00,
        cached_input_per_mtok=0.20, context=1_050_000, source=_OPENAI_SOURCE,
    ),
    "gpt-5.6-luna": ModelPrice(
        provider=ProviderName.OPENAI, input_per_mtok=0.20, output_per_mtok=1.20,
        cached_input_per_mtok=0.02, context=1_050_000, source=_OPENAI_SOURCE,
    ),
    "gpt-5.5": ModelPrice(
        provider=ProviderName.OPENAI, input_per_mtok=5.00, output_per_mtok=30.00,
        cached_input_per_mtok=0.50, context=272_000, source=_OPENAI_SOURCE,
    ),
    # -- Google Gemini ---------------------------------------------------------------------
    # Introductory rates. Google publishes the post-intro standard rate up front, so it is
    # pinned here too rather than discovered when the bill doubles on 1 January.
    "gemini-3.8-flash": ModelPrice(
        provider=ProviderName.GEMINI, input_per_mtok=0.75, output_per_mtok=3.75,
        cached_input_per_mtok=0.075, source=_GEMINI_SOURCE,
        intro_until="2026-12-31", standard_after_intro=(1.50, 7.50),
    ),
    "gemini-3.7-flash": ModelPrice(
        provider=ProviderName.GEMINI, input_per_mtok=0.75, output_per_mtok=3.75,
        cached_input_per_mtok=0.075, source=_GEMINI_SOURCE,
        intro_until="2026-12-31", standard_after_intro=(1.50, 7.50),
    ),
    "gemini-3.5-flash": ModelPrice(
        provider=ProviderName.GEMINI, input_per_mtok=1.50, output_per_mtok=9.00,
        cached_input_per_mtok=0.15, source=_GEMINI_SOURCE,
    ),
    "gemini-3.5-flash-lite": ModelPrice(
        provider=ProviderName.GEMINI, input_per_mtok=0.30, output_per_mtok=2.50,
        source=_GEMINI_SOURCE,
    ),
    # -- NVIDIA NIM ------------------------------------------------------------------------
    # The hosted developer endpoint is credit-based, not per-token, and self-hosted NIM has
    # no per-token price at all -- so these are `not_token_metered` rather than priced at 0.
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": ModelPrice(
        provider=ProviderName.NIM, input_per_mtok=0.0, output_per_mtok=0.0,
        context=256_000, billing="not_token_metered", source=_NIM_SOURCE,
    ),
    # The default. Dense 49B: better at the in-context judgement calls this system is made
    # of than the 3B-active nano model, and correspondingly slower.
    # [verify] the context window against build.nvidia.com before quoting it anywhere.
    "nvidia/llama-3.3-nemotron-super-49b-v1.5": ModelPrice(
        provider=ProviderName.NIM, input_per_mtok=0.0, output_per_mtok=0.0,
        context=128_000, billing="not_token_metered", source=_NIM_SOURCE,
    ),
    # Successor to the 49B model. 120B MoE with ~12B active parameters per token.
    # The 49B reached EOL on 2026-08-26; this is its replacement on the hosted endpoint.
    "nvidia/nemotron-3-super-120b-a12b": ModelPrice(
        provider=ProviderName.NIM, input_per_mtok=0.0, output_per_mtok=0.0,
        context=128_000, billing="not_token_metered", source=_NIM_SOURCE,
    ),
}

#: Model ids that appear in the docs or in the wild but are not the canonical string.
#: Anthropic rejects date-suffixed ids outright, so the dated form maps to the plain one.
MODEL_ALIASES = {
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "claude-opus-5-20260401": "claude-opus-5",
    # NVIDIA's self-hosted artefacts carry a precision suffix; the hosted endpoint does not.
    "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16": (
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    ),
    "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4": (
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    ),
    "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8": (
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    ),
}

_warned_expired: set[str] = set()


def resolve_model(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


def price_for(model: str) -> ModelPrice:
    """Look up a model's price, raising rather than guessing.

    A silent 0.0 here would make every cost number in the eval quietly wrong, which is worse
    than a crash at the first request. `billing="not_token_metered"` is the one legitimate
    way to cost nothing.
    """
    resolved = resolve_model(model)
    price = PRICES.get(resolved)
    if price is None:
        raise KeyError(
            f"no price pinned for model {model!r}; add it to config.PRICES and bump "
            f"PRICE_TABLE_VERSION (currently {PRICE_TABLE_VERSION}), or set "
            f"[prices.{model}] in tribunal.toml"
        )
    if price.intro_expired() and resolved not in _warned_expired:
        _warned_expired.add(resolved)
        warnings.warn(
            f"{resolved}: pinned introductory rate expired {price.intro_until}"
            + (
                f"; standard rate is {price.standard_after_intro}"
                if price.standard_after_intro
                else ""
            )
            + ". Update config.PRICES before publishing any cost number.",
            stacklevel=2,
        )
    return price


def provider_for(model: str) -> ProviderName:
    """Which provider serves this model. Used to default an agent's provider from its model."""
    return price_for(model).provider


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> float:
    """Compute request cost at trace-write time. Rounded to 6dp; sub-microdollar is noise."""
    p = price_for(model)
    total = (
        input_tokens * p.input_per_mtok
        + cache_read_input_tokens * p.cached_input
        + cache_creation_input_tokens * p.cache_write
        + output_tokens * p.output_per_mtok
    ) / 1_000_000
    return round(total, 6)


# --------------------------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------------------------


class HardBlockRule(BaseModel):
    """A dimension/severity/confidence triple that auto-rejects (decision table row 7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: Dimension
    severity: Severity
    min_confidence: float = Field(ge=0.0, le=1.0)


class PolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_rounds: int = Field(default=3, ge=1)
    patch_attempts_per_round: int = Field(default=2, ge=1)
    #: Pressure below this accepts. Not zero, on purpose: 4.0 tolerates one full-confidence
    #: MEDIUM or four LOWs. A system requiring zero open issues never terminates on real code
    #: (docs/04-arbitration.md § row 11 vs row 12).
    accept_threshold: float = 4.0
    #: `no_progress` needs two consecutive non-improving rounds; one bad round is normal.
    no_progress_epsilon: float = 2.0
    #: Asymmetric by design: a security HIGH auto-rejects, a performance HIGH does not. An
    #: exploitable vulnerability has unbounded downside; a perf regression is recoverable.
    #: Stating that asymmetry in config beats pretending the system is neutral.
    hard_block: tuple[HardBlockRule, ...] = (
        HardBlockRule(dimension=Dimension.SECURITY, severity=Severity.HIGH, min_confidence=0.6),
    )
    #: Reject patches touching more than this many hunks -- a signal the Coder is rewriting
    #: rather than fixing (docs/03-agents.md § Coder).
    max_hunks: int = Field(default=8, ge=1)


class BudgetConfig(BaseModel):
    """Breach transitions the FSM to ESCALATE, never a hard abort: an exceeded budget still
    produces a trace, a report, and the best patch seen so far."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_usd: float = Field(default=2.00, gt=0)
    max_tokens: int = Field(default=400_000, gt=0)
    max_wall_seconds: int = Field(default=600, gt=0)
    max_rounds: int = Field(default=3, ge=1)


# --------------------------------------------------------------------------------------------
# Sandbox
# --------------------------------------------------------------------------------------------


class SandboxConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Layer 1: never execute by default. `--allow-exec` is required to run anything at all.
    allow_exec: bool = False
    mode: SandboxMode = "subprocess"
    cpu_seconds: int = Field(default=10, gt=0)
    address_space_bytes: int = Field(default=512 << 20, gt=0)
    max_processes: int = Field(default=64, gt=0)
    max_file_bytes: int = Field(default=16 << 20, gt=0)
    wall_timeout_seconds: int = Field(default=30, gt=0)
    output_capture_bytes: int = Field(default=64 << 10, gt=0)
    docker_image: str = "tribunal-sandbox:latest"
    docker_memory: str = "512m"
    docker_cpus: str = "1"
    docker_pids_limit: int = 128


# --------------------------------------------------------------------------------------------
# The interactive coding agent
# --------------------------------------------------------------------------------------------


class CodeConfig(BaseModel):
    """Limits for `tribunal code`, the interactive terminal agent.

    Separate from `SandboxConfig` on purpose, and the distinction is the whole safety story
    of this surface. The sandbox exists to run *model-written code from an untrusted input
    file* with a scrubbed environment and no credentials reachable. `tribunal code` is the
    opposite situation: a developer sitting at their own repository, who wants `pytest` and
    `git` to work with their own environment and their own `PATH`. Confining that to the
    sandbox would make the tool useless, so it is not confined -- and the protection is a
    human approval per command instead, plus the limits below. Nothing here claims
    isolation; see `approval.py` for what is actually enforced.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Tool calls per user turn before the agent is stopped and asked to summarise. A stop,
    #: not a crash: the transcript survives and the next prompt continues from it.
    max_steps: int = Field(default=40, ge=1)
    #: Spend cap for one *session*, not one turn -- a loop that burns the budget in eight
    #: cheap turns is the case worth catching.
    max_usd: float = Field(default=5.00, gt=0)
    command_timeout_seconds: int = Field(default=120, gt=0)
    #: Per-observation cap. Bigger than the grounding layer's because a test run's tail is
    #: often the whole answer, and the model cannot ask for "the rest".
    max_output_chars: int = Field(default=8_000, gt=0)
    max_read_lines: int = Field(default=800, gt=0)
    max_file_bytes: int = Field(default=2 << 20, gt=0)
    #: Transcript budget. Older steps are elided from the middle when this is exceeded, so a
    #: long session degrades by forgetting rather than by failing a context-length check.
    max_history_chars: int = Field(default=60_000, gt=0)
    max_grep_matches: int = Field(default=80, ge=1)
    max_glob_results: int = Field(default=200, ge=1)
    #: Never walked, never grepped, never listed. A `.venv` is the single fastest way to
    #: spend a context window on nothing.
    ignore_dirs: tuple[str, ...] = (
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        "target",
        "traces",
    )


# --------------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------------


class AgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = "claude-opus-5"
    #: None means "derive from the model", so pointing an agent at another provider is a
    #: one-line model change rather than two fields that can disagree. Set it explicitly only
    #: for a model that is not in `PRICES` (a self-hosted NIM, say).
    provider: ProviderName | None = None
    effort: Effort = "high"
    max_tokens: int = Field(default=16_000, gt=0)
    #: Round >= 2 rounds are the hard ones, so the Coder escalates effort there.
    effort_late_round: Effort | None = None

    def resolved_provider(self) -> ProviderName:
        if self.provider is not None:
            return self.provider
        return provider_for(self.model)

    def effort_for_round(self, round_: int) -> Effort:
        if round_ >= 2 and self.effort_late_round is not None:
            return self.effort_late_round
        return self.effort


class AgentsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    coder: AgentConfig = AgentConfig(effort="high", effort_late_round="xhigh")
    redteam: AgentConfig = AgentConfig(effort="high")
    profiler: AgentConfig = AgentConfig(effort="high")
    #: The hardest reasoning in the system: synthesising two independent critiques into one
    #: non-contradictory instruction set.
    arbiter: AgentConfig = AgentConfig(effort="xhigh")
    #: Detector 2's affirmation, which is a classification rather than the synthesis: are
    #: these two remedies actually opposed? Separate from `arbiter` so it does not inherit
    #: `xhigh`. It fires once per same-span candidate pair, which is the most frequent call
    #: in the system after the critics, and it answers a yes/no question.
    arbiter_affirm: AgentConfig = AgentConfig(effort="medium", max_tokens=4_000)
    #: Summarisation from a trace, not reasoning.
    postmortem: AgentConfig = AgentConfig(effort="medium")
    #: `tribunal code`. Not a tribunal member either: it holds the tools and the transcript,
    #: and it can *call* the tribunal on a file. `max_tokens` is low because one turn emits
    #: one tool call, and the one field that can be large -- a whole file in `content` -- is
    #: the case worth making the model think twice about rather than budgeting for.
    code: AgentConfig = AgentConfig(effort="medium", max_tokens=8_000)
    #: Not a tribunal member: the eval's judge, which never runs during a review. It lives here
    #: because it is an agent with a model and an effort and the machinery is shared.
    #: docs/07 § LLM-as-judge is explicit that this one does not get a cheaper model --
    #: "judging is harder than critiquing, and a weak judge silently caps the eval's
    #: resolution", in a way no results table shows.
    judge: AgentConfig = AgentConfig(effort="high")


# --------------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------------


class ProviderSettings(BaseModel):
    """Per-provider connection settings. Credentials are never stored here.

    Only the *name* of the environment variable lives in config, so a config snapshot can go
    into a trace header -- which gets pasted into issue reports and demo GIFs -- without any
    chance of carrying a key with it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_env: str
    base_url: str | None = None
    timeout_seconds: float = Field(default=180.0, gt=0)
    max_retries: int = Field(default=2, ge=0)


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    anthropic: ProviderSettings = ProviderSettings(key_env="ANTHROPIC_API_KEY")
    openai: ProviderSettings = ProviderSettings(key_env="OPENAI_API_KEY")
    gemini: ProviderSettings = ProviderSettings(key_env="GEMINI_API_KEY")
    #: NVIDIA NIM is OpenAI-compatible; the hosted endpoint is the default and a self-hosted
    #: NIM container is reached by overriding `base_url` (e.g. http://localhost:8000/v1).
    nim: ProviderSettings = ProviderSettings(
        key_env="NVIDIA_API_KEY", base_url="https://integrate.api.nvidia.com/v1"
    )

    def for_provider(self, provider: ProviderName) -> ProviderSettings:
        return getattr(self, provider.value)


CassetteMode = Literal["live", "record", "replay"]


class LLMConfig(BaseModel):
    """How the client talks to providers, and whether it talks to them at all.

    `replay` is what makes docs/09-roadmap.md's "the test suite makes zero API calls" true
    across four providers rather than one: a cassette is keyed by the full request including
    the provider and the adapted schema, so a recorded Anthropic run can never be silently
    served to a Gemini request.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: CassetteMode = "live"
    cassette_dir: Path = Path("tests/cassettes")
    #: One repair retry on schema-validation failure (docs/03-agents.md § Shared base).
    repair_retries: int = Field(default=1, ge=0)
    #: Quantise an off-grid confidence locally instead of spending a repair retry on it. No
    #: provider enforces `multipleOf`, so without this every critic costs an extra call
    #: roughly whenever the model picks 0.85 over 0.8 (see llm/schema.py).
    repair_quantisation_locally: bool = True


# --------------------------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------------------------


class GroundingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Static tools. These never execute the target, so they always run.
    static_tools: tuple[str, ...] = ("bandit", "ruff", "radon", "astgate")
    tool_timeout_seconds: int = Field(default=60, gt=0)
    #: radon reports every function's cyclomatic complexity; only rank C and worse becomes a
    #: finding, otherwise a clean file produces dozens of A-rank findings as noise.
    radon_min_rank: str = "C"
    #: ruff rule selection for grounding. Broader than the project's own lint config: the
    #: critics want signal, including rules we would not enforce on ourselves.
    ruff_select: tuple[str, ...] = ("E", "F", "B", "S", "C90", "PERF", "SIM", "RUF")
    ruff_max_complexity: int = 10


# --------------------------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------------------------

TraceLevel = Literal["default", "full"]


class Settings(BaseSettings):
    """Root configuration. Snapshotted into the trace header."""

    model_config = SettingsConfigDict(
        env_prefix="TRIBUNAL_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    policy: PolicyConfig = PolicyConfig()
    budget: BudgetConfig = BudgetConfig()
    sandbox: SandboxConfig = SandboxConfig()
    agents: AgentsConfig = AgentsConfig()
    code: CodeConfig = CodeConfig()
    grounding: GroundingConfig = GroundingConfig()
    providers: ProvidersConfig = ProvidersConfig()
    llm: LLMConfig = LLMConfig()
    #: Prices for models not in `config.PRICES`, e.g. a self-hosted NIM with a negotiated
    #: rate. Merged into the table at load time so `price_for` never has to guess.
    prices: dict[str, ModelPrice] = {}

    trace_dir: Path = Path("traces")
    #: `full` stores rendered prompt bodies. The default stores the prompt version plus an
    #: input hash, because traces get pasted into issue reports and demo GIFs.
    trace_level: TraceLevel = "default"

    @classmethod
    def load(cls, path: Path | None = None, **overrides: Any) -> Settings:
        """Load defaults <- TOML file <- environment <- explicit overrides."""
        data: dict[str, Any] = {}
        if path is None:
            candidate = Path("tribunal.toml")
            path = candidate if candidate.is_file() else None
        if path is not None:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        merged = {**data, **{k: v for k, v in overrides.items() if v is not None}}
        settings = cls(**merged)
        # Registering user-supplied prices at load time keeps `price_for` a pure lookup, so
        # every caller -- including the trace writer -- sees the same table.
        for model, price in settings.prices.items():
            PRICES.setdefault(model, price)
        return settings

    def snapshot(self) -> dict[str, Any]:
        """Config as it goes into the trace header, with the price table version stamped."""
        snapshot = {
            "price_table_version": PRICE_TABLE_VERSION,
            **self.model_dump(mode="json"),
        }
        # Resolve each agent's provider explicitly. A header saying `provider: null` would
        # make a trace uninterpretable without re-running the derivation.
        for role, agent in snapshot["agents"].items():
            agent["provider"] = getattr(self.agents, role).resolved_provider().value
        return snapshot
