from __future__ import annotations

import unittest

from fph.normalizer import (
    classify_property_type,
    extract_area_sqm,
    extract_district,
    normalize_datetime,
    parse_price_yuan,
    parse_property_info,
)


class NormalizerTest(unittest.TestCase):
    def test_same_property_across_rounds_shares_property_key(self) -> None:
        r1 = parse_property_info(
            item_id="10001",
            title="【一拍】福建省福州市仓山区临江街道梅坞路57号聚龙明珠二期8#楼101室房产（详见评估报告）",
            province="福建",
            city="福州",
        )
        r2 = parse_property_info(
            item_id="10002",
            title="（二拍）福州市仓山区临江街道梅坞路57号聚龙明珠二期8幢101室不动产（按现状拍卖）",
            province="福建",
            city="福州",
        )
        self.assertEqual(r1.property_key, r2.property_key)
        self.assertEqual(r1.district, "仓山区")
        self.assertEqual(r1.property_type, "residential")

    def test_fuzhou_and_longyan_districts(self) -> None:
        self.assertEqual(extract_district("福州市鼓楼区五四路某小区101室", city="福州"), "鼓楼区")
        self.assertEqual(extract_district("福清市音西街道清昌大道某住宅", city="福州"), "福清市")
        self.assertEqual(extract_district("龙岩市新罗区西陂街道华莲社区某花园", city="龙岩"), "新罗区")
        self.assertEqual(extract_district("福建省龙岩市上杭县临江镇某室", city="龙岩"), "上杭县")

    def test_property_type_classification(self) -> None:
        self.assertEqual(
            classify_property_type("龙岩市新罗区某花园地下一层31号车位", category_hint="50025969"),
            "parking",
        )
        self.assertEqual(
            classify_property_type("福州市鼓楼区某小区3幢501室及地下一层车位", category_hint="50025969"),
            "residential",
        )
        self.assertEqual(
            classify_property_type("福州市台江区万达广场一层108号商铺", category_hint="50025969"),
            "commercial",
        )
        self.assertEqual(
            classify_property_type("龙岩市永定区工业园厂房及仓库", category_hint="50025969"),
            "industrial",
        )

    def test_area_and_risk_extraction_from_notice(self) -> None:
        notice = (
            "标的物宗地面积5000平方米，其中房屋建筑面积：128.56平方米。"
            "特别提醒：本标的目前有人居住，法院不负责腾退，买卖双方一切税费均由买受人承担，"
            "且欠缴物业费约1.2万元由买受人承担。"
        )
        info = parse_property_info(
            item_id="20001",
            title="龙岩市新罗区龙腾路某某花园5幢1202室的50%产权份额",
            province="福建",
            city="龙岩",
            notice_text=notice,
        )
        self.assertEqual(info.area_sqm, 128.56)
        self.assertTrue(info.is_partial_share)
        self.assertEqual(info.risk_level, "high")
        self.assertIn("不交吉/不腾退", info.risk_tags)
        self.assertIn("有人占用", info.risk_tags)
        self.assertIn("买受人全额担税", info.risk_tags)
        self.assertIn("欠费风险", info.risk_tags)
        self.assertIn("部分产权", info.risk_tags)

    def test_area_ignores_land_area(self) -> None:
        self.assertEqual(extract_area_sqm("某小区住宅", "宗地面积20000㎡，建筑面积96.4㎡"), 96.4)

    def test_price_and_datetime_parsing(self) -> None:
        self.assertEqual(parse_price_yuan("¥119.05万"), 1_190_500.0)
        self.assertEqual(parse_price_yuan("1.2亿"), 120_000_000.0)
        self.assertEqual(parse_price_yuan("524,528元"), 524_528.0)
        self.assertEqual(normalize_datetime("11月19日18:00", reference_year=2026), "2026-11-19 18:00:00")
        self.assertTrue(normalize_datetime(1763546400000).startswith("2025-11-"))


if __name__ == "__main__":
    unittest.main()
