"""Work-product runtime integration — schema stripping and value injection.

Called by the inference plugin during WBS execution to:
1. Strip platform-owned argument slots from the output schema
2. Inject platform-computed deterministic values post-inference

Pure functions — no service dependencies.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from ananta.core.plans.work_product_policies import (
    OutputSlotPolicy,
    find_policy_for_slot,
    get_all_owned_output_slots,
    get_audio_midi_policy,
)
from ananta.core.plans.work_products import (
    WorkProduct,
    WorkProductId,
    WorkProductRegister,
)
from ananta.interfaces.attachment_schema import AttachmentFields
from ananta.utils.naming import build_filename, normalize_name

if TYPE_CHECKING:
    from ananta.core.plans.types import BoundSubStep, ComposedReference, ParsedPlan
    from ananta.core.plans.work_product_store import StateServiceProtocol
    from ananta.core.prompts.decode.step_schema import ProcessArgLookup
    from ananta.core.prompts.plan_state import PlanState

logger = logging.getLogger(__name__)

BLOB_LOCATOR_KIND: Final = "blob"

# Cache the default policy — it doesn't change at runtime.
_cached_policies: list[OutputSlotPolicy] | None = None


def _default_policies() -> list[OutputSlotPolicy]:
    global _cached_policies  # noqa: PLW0603
    if _cached_policies is None:
        _cached_policies = get_audio_midi_policy()
    return _cached_policies


def strip_owned_slots_from_arg_schema(
    arg_schema: dict[str, object],
) -> dict[str, object]:
    """Remove platform-owned output slots from an argument schema.

    Strips ``properties`` and ``required`` entries for output slots
    only.  Input slots stay visible so the model can see bound
    argument values and the constrained decoder can produce valid JSON.
    Returns a new dict (does not mutate the input).
    """
    policies = _default_policies()
    owned = get_all_owned_output_slots(policies)

    props = arg_schema.get("properties")
    if not isinstance(props, dict):
        return arg_schema

    present = owned & set(props)
    if not present:
        return arg_schema

    new_props = {k: v for k, v in props.items() if k not in owned}

    required_raw = arg_schema.get("required", [])
    new_required = (
        [r for r in required_raw if r not in owned]
        if isinstance(required_raw, list)
        else required_raw
    )

    result = dict(arg_schema)
    result["properties"] = new_props
    if new_required:
        result["required"] = new_required
    elif "required" in result:
        del result["required"]

    logger.info(
        "WORK_PRODUCTS: Stripped %d owned slot(s) from arg schema: %s",
        len(present), ", ".join(sorted(present)),
    )
    return result


def inject_owned_slot_values(
    actions: list[dict[str, Any]],
    step_number: int,
    wbs_run_id: str,
    register: WorkProductRegister,
    process_argument_slots: Mapping[str, frozenset[str]],
) -> None:
    """Inject platform-computed values for owned slots into action arguments.

    For **output slots**: computes a deterministic stem/filename from step
    coordinates for processes that declare the owned output argument.

    For **input slots**: resolves from the register when a ``Composed:``
    reference has already been resolved to a product key.

    Mutates action arguments in place.
    """
    policies = _default_policies()

    for action in actions:
        process_key = action.get("process_key")
        if not isinstance(process_key, str) or not process_key:
            raise RuntimeError(f"WORK_PRODUCTS: invalid process_key: {process_key!r}")
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            continue
        if process_key not in process_argument_slots:
            raise RuntimeError(
                f"WORK_PRODUCTS: missing process argument slots for {process_key}"
            )

        argument_slots = process_argument_slots[process_key]
        for policy in policies:
            _inject_input_slot_from_register(
                arguments, policy, register, argument_slots,
            )
            if policy.output_slot in argument_slots:
                _inject_output_slot(
                    arguments, policy, process_key, step_number,
                    wbs_run_id, register,
                )


def record_successful_action_products(
    register: WorkProductRegister,
    wbs_run_id: str,
    step_number: int,
    process_key: str,
    arguments: dict[str, object],
    attachments: list[dict[str, object]],
    process_argument_slots: frozenset[str],
) -> bool:
    """Record work products after a producing action succeeds.

    The semantic filename was set by WBS bound arguments and validated
    at injection time.  Recording updates the provenance entry with
    attachment metadata (blob locator, content type, namespace).
    """
    recorded = False
    policies = _default_policies()
    source_product_keys = _resolve_source_product_keys(arguments, policies, register)

    for policy in policies:
        if policy.output_slot not in process_argument_slots:
            continue
        attachment = _select_attachment_for_policy(
            process_key, arguments, attachments, policy,
        )
        # The semantic name is the authoritative display_name.
        # Read it from the provenance entry recorded at injection time.
        pre_registered = register.lookup_by_step_and_slot(
            step_number, policy.output_slot,
        )
        semantic_name = (
            pre_registered.display_name
            if pre_registered is not None
            else None
        )
        product = _build_product_from_attachment(
            attachment=attachment,
            policy=policy,
            process_key=process_key,
            wbs_run_id=wbs_run_id,
            step_number=step_number,
            source_product_keys=source_product_keys,
            wbs_authored_name=semantic_name,
        )
        register.record(product)
        recorded = True
        logger.info(
            "WORK_PRODUCTS: Recorded %s for %s step %d from %s",
            product.filename or product.display_name,
            wbs_run_id,
            step_number,
            process_key,
        )

    return recorded


def resolve_composed_reference_value(
    register: WorkProductRegister,
    ref: ComposedReference,
) -> str | list[str] | None:
    """Resolve a ``Composed:`` reference through registered work products."""
    policy = find_policy_for_slot(_default_policies(), ref.source_arg)
    if policy is None:
        return None

    products = register.lookup_composed_sources(ref)
    if products is None:
        return None

    product_list = [products] if isinstance(products, WorkProduct) else products
    values = [
        _apply_suffix_once(_product_argument_value(product, policy), ref.suffix)
        for product in product_list
    ]
    return values[0] if len(ref.source_steps) == 1 else values


def resolve_latest_delivery_attachment(
    register: WorkProductRegister,
    delivery_step_number: int,
) -> str | None:
    """Resolve the latest registered file product before a delivery step."""
    for step_number in range(delivery_step_number - 1, 0, -1):
        products = register.lookup_by_step(step_number)
        for product in reversed(products):
            if product.filename:
                return product.filename
    return None


def _apply_suffix_once(value: str, suffix: str) -> str:
    if not suffix or value.endswith(suffix):
        return value
    return f"{value}{suffix}"


def _inject_output_slot(
    arguments: dict[str, Any],
    policy: OutputSlotPolicy,
    process_key: str,
    step_number: int,
    wbs_run_id: str,
    register: WorkProductRegister | None = None,
) -> None:
    """Validate and pass through the WBS-authored semantic output name.

    The WBS-authored name (set by bound argument enforcement) is the
    authoritative artifact identity.  The platform validates it is
    present and records provenance in the register, but does NOT
    replace it with a platform-computed name.
    """
    slot = policy.output_slot
    semantic_name = arguments.get(slot)

    if not isinstance(semantic_name, str) or not semantic_name:
        raise RuntimeError(
            f"WORK_PRODUCTS: {process_key} missing WBS-authored value for "
            f"output slot {slot} — bound argument enforcement should have "
            f"provided it"
        )

    # Record provenance in the register
    if register is not None:
        extension = policy.resolve_extension(arguments) or policy.default_extension
        stem, _ = (
            (semantic_name, "")
            if "." not in semantic_name
            else semantic_name.rsplit(".", 1)
        )
        filename = f"{stem}.{extension}" if extension else stem
        product = WorkProduct(
            product_id=WorkProductId(
                wbs_run_id=wbs_run_id,
                step_number=step_number,
                output_slot=slot,
            ),
            process_key=process_key,
            content_type="",
            namespace="",
            display_name=semantic_name,
            stem=stem,
            extension=extension or None,
            filename=filename,
        )
        register.record(product)

    logger.info(
        "WORK_PRODUCTS: Semantic output %s=%r (step %d, %s, wbs=%s)",
        slot, semantic_name, step_number, process_key, wbs_run_id,
    )


def _select_attachment_for_policy(
    process_key: str,
    arguments: dict[str, object],
    attachments: list[dict[str, object]],
    policy: OutputSlotPolicy,
) -> dict[str, object]:
    expected_filename = _expected_output_filename(
        process_key, arguments, policy,
    )
    matches = [
        attachment
        for attachment in attachments
        if attachment.get(AttachmentFields.FILENAME) == expected_filename
    ]
    if len(matches) != 1:
        actual_filenames = [
            attachment.get(AttachmentFields.FILENAME) for attachment in attachments
        ]
        raise RuntimeError(
            f"WORK_PRODUCTS: expected exactly one attachment named "
            f"{expected_filename!r} for {process_key}, found {len(matches)}. "
            f"Actual attachment filenames: {actual_filenames}"
        )
    return matches[0]


def _expected_output_filename(
    process_key: str,
    arguments: dict[str, object],
    policy: OutputSlotPolicy,
) -> str:
    """Derive expected filename from the WBS-authored semantic name.

    The output slot contains the WBS-authored semantic name (passed
    through by bound argument enforcement).  The plugin pipeline
    normalises this name via ``naming.normalize_name`` before writing
    the blob, so we must apply the same normalisation here to produce
    the filename the attachment will actually carry.
    """
    output_value = arguments.get(policy.output_slot)
    if not isinstance(output_value, str) or not output_value:
        raise RuntimeError(
            f"WORK_PRODUCTS: {process_key} missing output slot "
            f"{policy.output_slot}"
        )
    extension = policy.resolve_extension(arguments) or policy.default_extension
    if not extension:
        raise RuntimeError(
            f"WORK_PRODUCTS: cannot resolve extension for {process_key} "
            f"{policy.output_slot}"
        )
    # Strip extension if the WBS already included it, then normalise
    # the stem the same way the plugin pipeline does.
    stem = output_value
    if stem.endswith(f".{extension}"):
        stem = stem[: -(len(extension) + 1)]
    normalised_stem = normalize_name(stem)
    return build_filename(normalised_stem, extension)


def _build_product_from_attachment(
    *,
    attachment: dict[str, object],
    policy: OutputSlotPolicy,
    process_key: str,
    wbs_run_id: str,
    step_number: int,
    source_product_keys: tuple[str, ...],
    wbs_authored_name: str | None = None,
) -> WorkProduct:
    filename = _required_attachment_str(attachment, AttachmentFields.FILENAME)
    stem, extension = _split_filename(filename)
    # Use the WBS-authored name as display_name so cross-step input
    # resolution can match against it.  Falls back to the platform
    # filename when no WBS name is available.
    display = wbs_authored_name or filename
    return WorkProduct(
        product_id=WorkProductId(
            wbs_run_id=wbs_run_id,
            step_number=step_number,
            output_slot=policy.output_slot,
        ),
        process_key=process_key,
        content_type=_required_attachment_str(attachment, AttachmentFields.MEDIA_TYPE),
        namespace=_required_attachment_str(attachment, AttachmentFields.NAMESPACE),
        display_name=display,
        locator=_required_attachment_str(attachment, AttachmentFields.BLOB_ID),
        locator_kind=BLOB_LOCATOR_KIND,
        stem=stem,
        extension=extension,
        filename=filename,
        source_product_keys=source_product_keys,
    )


def _required_attachment_str(
    attachment: dict[str, object],
    field_name: str,
) -> str:
    value = attachment.get(field_name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(
            f"WORK_PRODUCTS: attachment field {field_name} must be a non-empty string"
        )
    return value


def _split_filename(filename: str) -> tuple[str, str | None]:
    if "." not in filename:
        return filename, None
    stem, extension = filename.rsplit(".", 1)
    return stem, extension or None


def _inject_input_slot_from_register(
    arguments: dict[str, Any],
    policy: OutputSlotPolicy,
    register: WorkProductRegister,
    argument_slots: frozenset[str],
) -> None:
    """Resolve input slots from the register when products exist.

    Scans the arguments for input slot names. If a value looks like
    a platform-assigned stem/filename (from a prior step), it's already
    correct. If the model hallucinated a different name, resolve from
    the register by looking up the source step's output.

    When the input slot is ``None`` (e.g. because bound-argument
    enforcement skipped owned input slots), resolves by finding the
    most recent product for the same output slot in the register.
    """
    for input_slot in policy.input_slots:
        if input_slot not in argument_slots:
            logger.info("WORK_PRODUCTS: Input slot %s not in argument_slots, skipping", input_slot)
            continue
        current_value = arguments.get(input_slot)
        if current_value is None:
            logger.info("WORK_PRODUCTS: Input slot %s is None, skipping", input_slot)
            continue
        logger.info("WORK_PRODUCTS: Resolving input slot %s=%r from register (%d products)", input_slot, current_value, len(register.all_products()))

        # If the value is a list (e.g., input_audio_files for concatenation),
        # check each item against the register.
        if isinstance(current_value, list):
            resolved_values: list[str] = []
            for item in current_value:
                if not isinstance(item, str):
                    raise RuntimeError(
                        f"WORK_PRODUCTS: {input_slot} item must be a string: {item!r}"
                    )
                resolved_item = _resolve_single_input(item, policy, register)
                resolved_values.append(resolved_item if resolved_item else item)
            arguments[input_slot] = resolved_values
        elif isinstance(current_value, str):
            resolved_value = _resolve_single_input(current_value, policy, register)
            if resolved_value:
                arguments[input_slot] = resolved_value


def _resolve_single_input(
    value: str,
    policy: OutputSlotPolicy,
    register: WorkProductRegister,
) -> str | None:
    """Try to resolve a single input value from the register.

    Looks for a product whose display_name, stem, or filename matches.
    Returns the canonical value (stem or filename per policy) or None.
    """
    product = _find_product_for_input(value, policy, register)
    if product is not None:
        return _product_argument_value(product, policy)
    return None


def _resolve_source_product_keys(
    arguments: dict[str, object],
    policies: list[OutputSlotPolicy],
    register: WorkProductRegister,
) -> tuple[str, ...]:
    keys: list[str] = []
    for policy in policies:
        for input_slot in policy.input_slots:
            raw_value = arguments.get(input_slot)
            values = _input_values(raw_value, input_slot)
            for value in values:
                product = _find_product_for_input(value, policy, register)
                if product is not None:
                    keys.append(product.product_id.key)
                # Unregistered inputs are from prior flows — omit provenance link.
    return tuple(dict.fromkeys(keys))


def _input_values(value: object, input_slot: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise RuntimeError(
                    f"WORK_PRODUCTS: {input_slot} item must be a string: {item!r}"
                )
            values.append(item)
        return tuple(values)
    raise RuntimeError(
        f"WORK_PRODUCTS: {input_slot} must be a string or list of strings: {value!r}"
    )


def _find_product_for_input(
    value: str,
    policy: OutputSlotPolicy,
    register: WorkProductRegister,
) -> WorkProduct | None:
    """Find a registered product matching an input argument value.

    Matches against display_name, stem, and filename.  Also strips
    the extension from the input value and tries again so that
    WBS-authored names with extensions (e.g. ``foo.wav``) match
    display_names stored without extensions (e.g. ``foo``).
    """
    value_stem, _ = _split_filename(value)
    for product in register.all_products():
        if product.product_id.output_slot != policy.output_slot:
            continue
        candidates = (product.display_name, product.stem, product.filename)
        logger.info(
            "WORK_PRODUCTS: Input match check: value=%r stem=%r vs candidates=%r",
            value, value_stem, candidates,
        )
        if value in candidates or value_stem in candidates:
            return product
    return None


def _product_argument_value(
    product: WorkProduct,
    policy: OutputSlotPolicy,
) -> str:
    if policy.stem_or_filename == "stem" and product.stem:
        return product.stem
    if product.filename:
        return product.filename
    return product.display_name


# ── WBS binding orchestration (extracted from inference plugin) ───


def resolve_active_wbs_id(plan_state: PlanState) -> str | None:
    """Resolve the active WBS ID from the focused plan text."""
    plan_text = plan_state.focused_plan_text
    if not plan_text:
        return None
    from ananta.core.plans.windowing import ACTIVE_WBS_HEADER_RE

    wbs_match = ACTIVE_WBS_HEADER_RE.search(plan_text)
    return wbs_match.group(1) if wbs_match else None


def resolve_current_bound_sub_steps(
    plan_state: PlanState,
) -> tuple[list[BoundSubStep], ParsedPlan | None]:
    """Resolve bound sub-steps for the current WBS execution step.

    Returns bound sub-steps with arguments or composed references,
    and the full parsed WBS for cross-step reference resolution.
    """
    from ananta.core.plans import parse as parse_plan
    from ananta.core.prompts.plan_drivers.wbs_bindings import resolve_wbs_step_number

    wbs_text = plan_state.focused_wbs_text
    if not wbs_text:
        return [], None

    plan_text = plan_state.focused_plan_text
    if not plan_text:
        return [], None

    parsed_plan = parse_plan(plan_text)
    target = _resolve_target_step(parsed_plan)
    if target is None:
        return [], None

    wbs_step_number = resolve_wbs_step_number(target)
    if wbs_step_number is None:
        return [], None

    wbs_parsed = parse_plan(wbs_text)
    wbs_step = wbs_parsed.step_by_number(wbs_step_number)
    if wbs_step is None:
        return [], None

    bound = [
        bs for bs in wbs_step.bound_sub_steps
        if bs.arguments is not None or bs.composed_references
    ]
    return bound, wbs_parsed


def collect_bound_arg_keys(plan_state: PlanState) -> set[str]:
    """Collect argument keys committed in the WBS for the current step.

    These keys are excluded from the output schema so the model does
    not emit them.  Bound argument enforcement injects them post-inference.
    """
    bound_sub_steps, _ = resolve_current_bound_sub_steps(plan_state)
    keys: set[str] = set()
    for bs in bound_sub_steps:
        if bs.arguments is not None:
            keys.update(bs.arguments)
    return keys


def enforce_bound_argument_values(
    plan_state: PlanState,
    actions: list[dict[str, Any]],
    register: WorkProductRegister | None,
    arg_lookup: ProcessArgLookup | None = None,
) -> None:
    """Enforce WBS bound argument values on submitted actions.

    Pairs each action with its bound sub-step (by process key, in
    declaration order) and overwrites divergent argument values.
    Resolves ``Composed:`` cross-step references from the register.

    When *arg_lookup* is provided, each bound value is validated against
    the process's canonical property schema before enforcement.  Values
    that fail validation are skipped with a warning — the model's
    submitted value (or default) is used instead.
    """
    from ananta.core.plans.types import BoundSubStep as _BoundSubStep

    bound_with_args, _ = resolve_current_bound_sub_steps(plan_state)
    if not bound_with_args:
        return

    buckets: dict[str, list[_BoundSubStep]] = {}
    for bs in bound_with_args:
        buckets.setdefault(bs.process_key, []).append(bs)

    for action in actions:
        _enforce_action_bound_args(action, buckets, register, arg_lookup)


def _try_parse_json_container(stripped: str) -> object | None:
    """Parse a JSON array/object string; return the parsed value or None."""
    is_array = stripped.startswith("[") and stripped.endswith("]")
    is_obj = stripped.startswith("{") and stripped.endswith("}")
    if not (is_array or is_obj):
        return None
    try:
        parsed = json.loads(stripped)
        return _coerce_bound_value(parsed) if isinstance(parsed, (list, dict)) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _try_parse_numeric(value: str) -> int | float | None:
    """Coerce a string to int or float if possible; return None otherwise."""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return None


def _coerce_string_value(value: str) -> object:
    """Coerce a string to a native Python type when the content warrants it."""
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    container = _try_parse_json_container(value.strip())
    if container is not None:
        return container
    numeric = _try_parse_numeric(value)
    return numeric if numeric is not None else value


def _coerce_bound_value(
    value: object, schema: dict[str, object] | None = None,
) -> object:
    """Coerce string-typed numeric/boolean values from WBS JSON.

    The thinking model sometimes emits ``"0.1"`` instead of ``0.1`` in
    Arguments blocks.  This function converts string representations of
    numbers and booleans to native Python types so downstream plugin
    code does not crash on mixed-type comparisons.

    Recursively coerces lists and dicts so that nested structures
    (e.g. breakpoint arrays) are handled.

    When ``schema`` is provided and explicitly expects ``"string"``, do NOT
    coerce a string value to a number — the plugin requires the literal
    string. This prevents legitimate string-typed parameters like
    ``delays="55"`` (ffmpeg chorus filter) from being silently coerced to
    int 55, failing the STRING schema check, and being dropped.
    """
    if isinstance(value, str):
        if schema is not None and schema.get("type") == "string":
            return value
        return _coerce_string_value(value)
    if isinstance(value, list):
        return [_coerce_bound_value(item) for item in value]
    if isinstance(value, dict):
        return {k: _coerce_bound_value(v) for k, v in value.items()}
    return value


def _enforce_bound_key(
    key: str,
    bound_value: object,
    process_key: str,
    submitted_args: dict[str, object],
    prop_schemas: dict[str, dict[str, object]],
) -> None:
    """Coerce, validate, clamp, and enforce one bound argument key."""
    schema = prop_schemas.get(key) if prop_schemas else None
    coerced = _coerce_bound_value(bound_value, schema)
    if prop_schemas and key in prop_schemas:
        if not _bound_value_matches_schema(coerced, prop_schemas[key]):
            logger.warning(
                "BOUND_ARG_REJECTED: %s.%s — bound=%r does not match "
                "property schema, skipping enforcement",
                process_key, key, coerced,
            )
            return
        coerced = _clamp_to_schema_range(coerced, prop_schemas[key], process_key, key)
    submitted_value = submitted_args.get(key)
    if submitted_value != coerced:
        logger.warning(
            "BOUND_ARG_ENFORCED: %s.%s — bound=%r submitted=%r",
            process_key, key, coerced, submitted_value,
        )
        submitted_args[key] = coerced


def _enforce_action_bound_args(
    action: dict[str, Any],
    buckets: dict[str, list[Any]],
    register: WorkProductRegister | None,
    arg_lookup: ProcessArgLookup | None = None,
) -> None:
    """Enforce bound argument values from the WBS onto one action."""
    process_key = action.get("process_key", "")
    bucket = buckets.get(process_key)
    if not bucket:
        return
    bound = bucket.pop(0)
    submitted_args = action.get("arguments", {})
    if not isinstance(submitted_args, dict):
        return

    # Cache the property schemas for this process (if available).
    prop_schemas: dict[str, dict[str, object]] = {}
    if arg_lookup is not None:
        prop_schemas = arg_lookup.get_arg_properties(process_key)

    if bound.arguments is not None:
        for key, bound_value in bound.arguments.items():
            _enforce_bound_key(key, bound_value, process_key, submitted_args, prop_schemas)
    if bound.composed_references and register is not None:
        _resolve_composed(
            process_key, submitted_args, bound.composed_references, register,
        )


_SCALAR_TYPE_CHECKS: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "number": (int, float),
    "integer": int,
    "string": str,
    "boolean": bool,
}


def _array_items_match_schema(value: list[object], items_schema: dict[str, object]) -> bool:
    """Check that every element of *value* conforms to *items_schema*."""
    items_type = items_schema.get("type")
    if items_type == "object":
        return all(isinstance(el, dict) for el in value)
    if items_type == "number":
        return all(isinstance(el, int | float) for el in value)
    if items_type == "string":
        return all(isinstance(el, str) for el in value)
    return True


def _bound_value_matches_schema(
    value: object,
    prop_schema: dict[str, object],
) -> bool:
    """Check if a bound value is compatible with a property schema.

    Performs lightweight type checks — not full JSON Schema validation.
    Catches the most common WBS authoring errors (e.g. integer array
    where object array is expected) without requiring a full validator.
    """
    expected_type = prop_schema.get("type")
    if expected_type is None:
        return True

    if expected_type == "array" and isinstance(value, list):
        items_schema = prop_schema.get("items")
        if isinstance(items_schema, dict) and value:
            return _array_items_match_schema(value, items_schema)
        return True

    check = _SCALAR_TYPE_CHECKS.get(str(expected_type))
    if check is not None:
        return isinstance(value, check)
    return True


def _clamp_to_schema_range(
    value: object,
    prop_schema: dict[str, object],
    process_key: str,
    param_name: str,
) -> object:
    """Clamp a numeric value to the schema's minimum/maximum range.

    Returns the value unchanged if it is not numeric or if no range
    constraints exist in the schema.
    """
    if not isinstance(value, int | float):
        return value

    mn = prop_schema.get("minimum")
    mx = prop_schema.get("maximum")

    if isinstance(mn, int | float) and value < mn:
        clamped = type(value)(mn)
        logger.warning(
            "BOUND_ARG_CLAMPED: %s.%s — value=%r clamped to min=%r",
            process_key, param_name, value, clamped,
        )
        return clamped

    if isinstance(mx, int | float) and value > mx:
        clamped = type(value)(mx)
        logger.warning(
            "BOUND_ARG_CLAMPED: %s.%s — value=%r clamped to max=%r",
            process_key, param_name, value, clamped,
        )
        return clamped

    return value


def _resolve_composed(
    process_key: str,
    submitted_args: dict[str, Any],
    references: tuple[ComposedReference, ...],
    register: WorkProductRegister,
) -> None:
    """Resolve ``Composed:`` cross-step references into action args."""
    for ref in references:
        composed_value = resolve_composed_reference_value(register, ref)
        if composed_value is None:
            raise RuntimeError(
                f"COMPOSED_RESOLVE: no registered work product for "
                f"{process_key}.{ref.target_arg}"
            )
        existing = submitted_args.get(ref.target_arg)
        if existing != composed_value:
            logger.info(
                "COMPOSED_RESOLVE: %s.%s = %r (was %r)",
                process_key, ref.target_arg, composed_value, existing,
            )
            submitted_args[ref.target_arg] = composed_value


def _resolve_target_step(parsed_plan: Any) -> Any:
    """Find the active or next-executable step for WBS context."""
    current = parsed_plan.current_step
    if current is None:
        first_num = parsed_plan.first_executable_step_number
        if first_num is not None:
            current = parsed_plan.step_by_number(first_num)
    if current is None:
        return None
    if current.process_keys:
        return current
    for step in parsed_plan.steps:
        if step.number <= current.number:
            continue
        if step.is_completed or step.is_skipped:
            continue
        if step.process_keys:
            return step
        return None
    return current


def inject_work_product_values(
    plan_state: PlanState,
    actions: list[dict[str, Any]],
    state_service: StateServiceProtocol,
    process_arg_lookup: ProcessArgLookup,
) -> None:
    """Inject deterministic work-product filenames into action arguments.

    Platform-owned orchestration: reads WBS coordinates from
    ``plan_state``, loads the register, injects output/input slot
    values, and saves the register.

    Replaces ``Plugin._inject_work_product_values``.
    """
    if not plan_state.focused_wbs_text:
        return

    from ananta.core.plans import parse as parse_plan
    from ananta.core.plans.windowing import (
        ACTIVE_WBS_HEADER_RE,
        ACTIVE_WORK_PRODUCT_RUN_RE,
    )
    from ananta.core.prompts.plan_drivers.wbs_bindings import resolve_wbs_step_number

    plan_text = plan_state.focused_plan_text
    if not plan_text:
        return

    wbs_match = ACTIVE_WBS_HEADER_RE.search(plan_text)
    if not wbs_match:
        return
    wbs_id = wbs_match.group(1)

    parsed = parse_plan(plan_text)
    target = _resolve_target_step(parsed)
    if target is None:
        return
    wbs_step_number = resolve_wbs_step_number(target)
    if wbs_step_number is None:
        return
    plan_step_number = target.number

    from ananta.core.plans.work_product_store import WorkProductStoreAdapter

    run_match = ACTIVE_WORK_PRODUCT_RUN_RE.search(plan_text)
    run_id = run_match.group(1) if run_match else None
    store = WorkProductStoreAdapter(state_service, work_product_run_id=run_id)
    register_data = store.load_register(wbs_id)
    register = (
        WorkProductRegister.deserialize(register_data)
        if register_data
        else WorkProductRegister()
    )

    slots = _build_process_argument_slots(actions, process_arg_lookup)
    inject_owned_slot_values(actions, plan_step_number, wbs_id, register, slots)
    store.save_register(wbs_id, register.serialize())


def _build_process_argument_slots(
    actions: list[dict[str, Any]],
    process_arg_lookup: ProcessArgLookup,
) -> dict[str, frozenset[str]]:
    """Map action process keys to their declared argument property names."""
    from ananta.core.prompts.decode.action_schema import (
        _FUNCTION_ARG_PROPERTIES,
        _narrow_arg_schema,
    )

    slots: dict[str, frozenset[str]] = {}
    for action in actions:
        process_key = action.get("process_key")
        if not isinstance(process_key, str) or not process_key:
            raise RuntimeError(f"WORK_PRODUCTS: invalid process_key: {process_key!r}")
        if process_key in slots:
            continue
        fn = process_key.rsplit("::", 1)[-1] if "::" in process_key else process_key
        if fn in _FUNCTION_ARG_PROPERTIES:
            schema = _narrow_arg_schema([fn])
        else:
            props = process_arg_lookup.get_arg_properties(process_key)
            if not props:
                raise RuntimeError(
                    f"WORK_PRODUCTS: no declared properties for {process_key} "
                    f"— registry drift or missing process definition"
                )
            schema = {"properties": props, "type": "object"}
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise RuntimeError(
                f"WORK_PRODUCTS: malformed argument schema for {process_key}: "
                f"'properties' is {type(properties).__name__}, expected dict"
            )
        slots[process_key] = frozenset(properties.keys())
    return slots
