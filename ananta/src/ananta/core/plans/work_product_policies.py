"""Work-product domain policies — slot mappings for domain adapters.

Each domain (audio/MIDI, text artifacts, images) provides a policy
that declares which process output/input slots the platform should own
during WBS execution.  The core register and execution integration are
domain-general; only the slot mapping changes per domain.

## How it works:

1. **Schema stripping (pre-inference):** During WBS execution, output
   and composed-input slots listed in the active policy are removed
   from the model-visible argument schema.  The model never sees or
   provides these fields.

2. **Value injection (post-inference):** After the model emits actions,
   the platform validates WBS-authored semantic names for owned output
   slots and passes them through.  Input slots resolved via ``Composed:``
   get values from the work-product register.

3. **Recording (post-execution):** After successful action execution,
   work products are recorded in the register with provenance.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutputSlotPolicy:
    """Policy for a single process output that the platform should own.

    Attributes:
        output_slot: argument name for the output (e.g. "output_audio_file")
        input_slots: argument names that consume this output type
            (e.g. ("input_audio_file", "input_audio_files"))
        stem_or_filename: whether to inject "stem" (extensionless) or
            "filename" (with extension)
        extension_source: where to get the extension — "argument:<arg_name>"
            reads from another argument, "literal:<ext>" uses a fixed value
    """

    output_slot: str
    input_slots: tuple[str, ...]
    stem_or_filename: str  # "stem" | "filename"
    extension_source: str  # "argument:<arg_name>" | "literal:<ext>"
    default_extension: str = ""  # fallback when argument is absent

    def resolve_extension(self, arguments: dict[str, object]) -> str:
        """Resolve the file extension from the policy source.

        Falls back to ``default_extension`` when the source argument
        is absent (e.g. musical_synthesis_plugin processes always
        output WAV but don't have an ``output_audio_format`` parameter).
        """
        if self.extension_source.startswith("argument:"):
            arg_name = self.extension_source[len("argument:"):]
            value = arguments.get(arg_name)
            if value:
                return str(value).lower().lstrip(".")
            return self.default_extension
        if self.extension_source.startswith("literal:"):
            return self.extension_source[len("literal:"):]
        return self.default_extension


def get_audio_midi_policy() -> list[OutputSlotPolicy]:
    """Audio/MIDI domain policy — the first adapter."""
    return [
        OutputSlotPolicy(
            output_slot="output_audio_file",
            input_slots=("input_audio_file", "input_audio_files"),
            stem_or_filename="filename",
            extension_source="argument:output_audio_format",
            default_extension="wav",
        ),
        OutputSlotPolicy(
            output_slot="output_midi_file",
            input_slots=("input_midi_file",),
            stem_or_filename="filename",
            extension_source="literal:mid",
        ),
    ]


def get_all_owned_output_slots(
    policies: list[OutputSlotPolicy],
) -> set[str]:
    """Collect all output slot names owned by the platform."""
    return {p.output_slot for p in policies}


def get_all_owned_input_slots(
    policies: list[OutputSlotPolicy],
) -> set[str]:
    """Collect all input slot names that consume platform-owned outputs."""
    result: set[str] = set()
    for p in policies:
        result.update(p.input_slots)
    return result


def get_all_owned_slots(
    policies: list[OutputSlotPolicy],
) -> set[str]:
    """Collect all slot names (output + input) owned by the platform."""
    return get_all_owned_output_slots(policies) | get_all_owned_input_slots(policies)


def find_policy_for_slot(
    policies: list[OutputSlotPolicy],
    slot_name: str,
) -> OutputSlotPolicy | None:
    """Find the policy that owns a given output or input slot."""
    for policy in policies:
        if policy.output_slot == slot_name:
            return policy
        if slot_name in policy.input_slots:
            return policy
    return None
