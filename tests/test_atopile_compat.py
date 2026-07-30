from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "toolchain"
    / "compat"
    / "atopile_compat.py"
)
SPEC = importlib.util.spec_from_file_location("atopile_compat_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
compat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compat
SPEC.loader.exec_module(compat)


@dataclass
class FakeProperty:
    name: str
    at: dict[str, float]
    effects: dict[str, str]
    hide: bool


@dataclass
class FakeFootprint:
    propertys: list[FakeProperty]
    value: str = "before"


class FakeTransformer:
    @staticmethod
    def identify_obj(prop: FakeProperty) -> str:
        return prop.name

    def update_footprint_from_lib(
        self,
        footprint: FakeFootprint,
        _lib_footprint: object,
    ) -> FakeFootprint:
        footprint.propertys[0].at["r"] = 270
        footprint.propertys[0].effects["justify"] = "right"
        footprint.propertys[0].hide = True
        footprint.propertys.append(
            FakeProperty(
                "New",
                {"x": 3, "y": 4, "r": 180},
                {"justify": "center"},
                False,
            )
        )
        footprint.value = "updated"
        return footprint


class AtopileCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = FakeTransformer.update_footprint_from_lib

    def tearDown(self) -> None:
        FakeTransformer.update_footprint_from_lib = self.original

    def test_preserves_existing_property_spatial_fields_only(self) -> None:
        compat.install_atopile_compatibility(
            installed_version="0.15.7",
            transformer_type=FakeTransformer,
            clone=copy.deepcopy,
        )
        footprint = FakeFootprint(
            [
                FakeProperty(
                    "Reference",
                    {"x": 1, "y": 2, "r": 90},
                    {"justify": "left"},
                    False,
                )
            ]
        )

        updated = FakeTransformer().update_footprint_from_lib(footprint, object())

        self.assertEqual(updated.propertys[0].at, {"x": 1, "y": 2, "r": 90})
        self.assertEqual(updated.propertys[0].effects, {"justify": "left"})
        self.assertFalse(updated.propertys[0].hide)
        self.assertEqual(updated.propertys[1].at["r"], 180)
        self.assertEqual(updated.value, "updated")

    def test_installs_only_once(self) -> None:
        first = compat.install_atopile_compatibility(
            installed_version="0.15.7",
            transformer_type=FakeTransformer,
            clone=copy.deepcopy,
        )
        wrapped = FakeTransformer.update_footprint_from_lib
        second = compat.install_atopile_compatibility(
            installed_version="0.15.7",
            transformer_type=FakeTransformer,
            clone=copy.deepcopy,
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIs(FakeTransformer.update_footprint_from_lib, wrapped)

    def test_rejects_an_unreviewed_atopile_version(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "supports exactly 0.15.7, found 0.16.0",
        ):
            compat.install_atopile_compatibility(
                installed_version="0.16.0",
                transformer_type=FakeTransformer,
                clone=copy.deepcopy,
            )
