"""Work-product register — domain-general provenance for WBS artifacts.

Every WBS-produced artifact gets a stable, platform-owned identity
derived from step coordinates.  The model never invents filenames;
the platform assigns them from ``(wbs_run_id, step_number, output_slot)``.

The register is domain-general.  Audio/MIDI is the first stress-test
adapter (Unit 3B); the same pattern applies to text artifacts, images,
and other domains.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ananta.core.plans.types import ComposedReference


@dataclass(frozen=True)
class WorkProductId:
    """Stable, storage-independent identity for a work product.

    Canonical key: ``(wbs_run_id, step_number, output_slot, version, sub_index)``.

    ``wbs_run_id`` is durable — derived from ``plan_id/wbs_id``, NOT from
    ``flow_id`` (which changes on await-user resume or phase continuation).
    """

    wbs_run_id: str
    step_number: int
    output_slot: str
    version: int = 1
    sub_index: int = 0

    @property
    def key(self) -> str:
        """Canonical string key for lookup and provenance references."""
        return (
            f"{self.wbs_run_id}/step_{self.step_number:03d}"
            f"/{self.output_slot}/v{self.version}/{self.sub_index}"
        )


@dataclass(frozen=True)
class WorkProduct:
    """A single work product recorded by the platform after step execution.

    Identity is ``product_id``; locator and naming metadata are populated
    by the recording adapter and may be absent for non-file products.
    """

    product_id: WorkProductId
    process_key: str
    content_type: str
    namespace: str
    display_name: str

    # Storage — at least one should be set
    locator: str | None = None
    locator_kind: str | None = None  # "blob" | "kb_path" | "state_record"

    # Naming metadata — optional, used by file-like domain adapters
    stem: str | None = None
    extension: str | None = None
    filename: str | None = None

    # Provenance — references by product_id key
    source_product_keys: tuple[str, ...] = ()


class WorkProductRegister:
    """Step-indexed provenance register for a WBS execution run.

    Domain-general — stores and retrieves generic work-product records.
    Identity is by ``product_id``, not by storage locator or filename.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, WorkProduct] = {}
        self._by_step: dict[int, list[WorkProduct]] = {}
        self._by_step_and_slot: dict[tuple[int, str], WorkProduct] = {}
    def record(self, product: WorkProduct) -> None:
        """Record a work product in the register."""
        existing = self._by_key.get(product.product_id.key)
        if existing is not None:
            existing_step_products = self._by_step.get(existing.product_id.step_number, [])
            self._by_step[existing.product_id.step_number] = [
                item
                for item in existing_step_products
                if item.product_id.key != existing.product_id.key
            ]
        self._by_key[product.product_id.key] = product
        self._by_step.setdefault(product.product_id.step_number, []).append(product)
        slot_key = (product.product_id.step_number, product.product_id.output_slot)
        self._by_step_and_slot[slot_key] = product

    def lookup_by_key(self, key: str) -> WorkProduct | None:
        """Look up a product by its canonical key."""
        return self._by_key.get(key)

    def lookup_by_id(self, product_id: WorkProductId) -> WorkProduct | None:
        """Look up a product by its ID."""
        return self._by_key.get(product_id.key)

    def lookup_by_step(self, step_number: int) -> list[WorkProduct]:
        """Look up all products from a given step."""
        return list(self._by_step.get(step_number, []))

    def lookup_by_step_and_slot(
        self, step_number: int, output_slot: str,
    ) -> WorkProduct | None:
        """Look up a product by step number and output slot."""
        return self._by_step_and_slot.get((step_number, output_slot))

    def all_products(self) -> tuple[WorkProduct, ...]:
        """Return all recorded products."""
        return tuple(self._by_key.values())

    def lookup_composed_sources(
        self, ref: ComposedReference,
    ) -> WorkProduct | list[WorkProduct] | None:
        """Resolve a ``Composed:`` reference to work product(s).

        Single source step → scalar ``WorkProduct``.
        Multiple source steps → list of ``WorkProduct``.
        """
        products = []
        for step_num in ref.source_steps:
            product = self.lookup_by_step_and_slot(step_num, ref.source_arg)
            if product is not None:
                products.append(product)
        if not products:
            return None
        return products[0] if len(ref.source_steps) == 1 else products

    def serialize(self) -> str:
        """Serialize the register to JSON for persistence."""
        items = []
        for product in self._by_key.values():
            items.append({
                "product_id": {
                    "wbs_run_id": product.product_id.wbs_run_id,
                    "step_number": product.product_id.step_number,
                    "output_slot": product.product_id.output_slot,
                    "version": product.product_id.version,
                    "sub_index": product.product_id.sub_index,
                },
                "process_key": product.process_key,
                "content_type": product.content_type,
                "namespace": product.namespace,
                "display_name": product.display_name,
                "locator": product.locator,
                "locator_kind": product.locator_kind,
                "stem": product.stem,
                "extension": product.extension,
                "filename": product.filename,
                "source_product_keys": list(product.source_product_keys),
            })
        return json.dumps(
            {"products": items},
            separators=(",", ":"),
        )

    @classmethod
    def deserialize(cls, data: str) -> WorkProductRegister:
        """Deserialize a register from JSON.

        Handles both legacy format (plain list of products) and
        current format (dict with ``phase_stem_offset`` and ``products``).
        """
        register = cls()
        parsed = json.loads(data)
        if isinstance(parsed, list):
            items = parsed
        else:
            items = parsed.get("products", [])
        for item in items:
            pid = item["product_id"]
            product = WorkProduct(
                product_id=WorkProductId(
                    wbs_run_id=pid["wbs_run_id"],
                    step_number=pid["step_number"],
                    output_slot=pid["output_slot"],
                    version=pid.get("version", 1),
                    sub_index=pid.get("sub_index", 0),
                ),
                process_key=item["process_key"],
                content_type=item["content_type"],
                namespace=item["namespace"],
                display_name=item["display_name"],
                locator=item.get("locator"),
                locator_kind=item.get("locator_kind"),
                stem=item.get("stem"),
                extension=item.get("extension"),
                filename=item.get("filename"),
                source_product_keys=tuple(item.get("source_product_keys", ())),
            )
            register.record(product)
        return register


