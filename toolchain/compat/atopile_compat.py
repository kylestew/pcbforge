"""Narrow compatibility fixes for the pinned Atopile toolchain."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import version as package_version
from typing import Any


SUPPORTED_ATOPILE_VERSION = "0.15.7"
_PATCH_SENTINEL = "_pcbforge_preserves_existing_property_spatial_data"
_PROPERTY_SPATIAL_FIELDS = ("at", "effects", "hide")


def require_supported_atopile_version(
    installed_version: str | None = None,
) -> str:
    actual = installed_version or package_version("atopile")
    if actual != SUPPORTED_ATOPILE_VERSION:
        raise RuntimeError(
            "PCBForge's Atopile compatibility patch supports exactly "
            f"{SUPPORTED_ATOPILE_VERSION}, found {actual}; review or remove the "
            "patch before changing the pinned toolchain"
        )
    return actual


def _wrap_update_footprint_from_lib(
    original: Callable[..., Any],
    *,
    transformer_type: type,
    clone: Callable[[Any], Any],
) -> Callable[..., Any]:
    def preserve_existing_property_spatial_data(
        self: Any,
        footprint: Any,
        lib_footprint: Any,
    ) -> Any:
        preserved: dict[Any, dict[str, Any]] = {}
        for prop in footprint.propertys:
            identity = transformer_type.identify_obj(prop)
            preserved[identity] = {
                field: clone(getattr(prop, field))
                for field in _PROPERTY_SPATIAL_FIELDS
                if hasattr(prop, field)
            }

        updated = original(self, footprint, lib_footprint)

        for prop in updated.propertys:
            state = preserved.get(transformer_type.identify_obj(prop))
            if state is None:
                continue
            for field, value in state.items():
                setattr(prop, field, value)
        return updated

    setattr(preserve_existing_property_spatial_data, _PATCH_SENTINEL, True)
    return preserve_existing_property_spatial_data


def install_atopile_compatibility(
    *,
    installed_version: str | None = None,
    transformer_type: type | None = None,
    clone: Callable[[Any], Any] | None = None,
) -> bool:
    """Install the Atopile 0.15.7 property-preservation fix once."""

    require_supported_atopile_version(installed_version)

    if transformer_type is None:
        from faebryk.exporters.pcb.kicad.transformer import PCB_Transformer

        transformer_type = PCB_Transformer
    if clone is None:
        from faebryk.libs.kicad.fileformats import kicad

        clone = kicad.copy

    current = transformer_type.update_footprint_from_lib
    if getattr(current, _PATCH_SENTINEL, False):
        return False

    transformer_type.update_footprint_from_lib = _wrap_update_footprint_from_lib(
        current,
        transformer_type=transformer_type,
        clone=clone,
    )
    return True
