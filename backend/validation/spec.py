"""What a validation is made of. Plain data: callers build it, validation.engine runs it."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    """How hard to look. The bars are fractions, not counts, so run counts can change without moving them."""
    reference_runs: int = 4        # fresh re-runs of the original; with its recorded answer, the reference set
    candidate_runs: int = 5        # answers per candidate per request at most: `first_runs` first, the rest only when
    first_runs: int = 3            # one of those isn't a pass (a clear pass needs no more; breadth beats depth)
    votes: int = 3                 # judge calls per candidate run when the first isn't a pass; majority wins
    coherence_floor: float = 0.6   # the original must agree with itself at least this often, else nothing is certified
    margin: float = 0.2            # a candidate may trail the original's self-consistency by this much and still fit
    judge_model: str = "claude-sonnet-5"
    judge_max_tokens: int = 0            # 0: the judge model's maximum output
    max_parallel: int = 8


@dataclass(frozen=True)
class Candidate:
    label: str                     # the caller's name for it (a router, a release, "cheaper"); opaque to the engine
    model: str


@dataclass
class Item:
    """One request to validate: its original model and recorded answer, and the candidates to hold against it."""
    id: str
    request: dict                  # a contract trace: input_messages, tools_defined, tool_choice, max_output_tokens, usage
    model: str
    candidates: list               # [Candidate]
    recorded: str = None           # the original's recorded behaviour (text + tool calls); None -> re-run it once more
