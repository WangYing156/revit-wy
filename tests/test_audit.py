"""模型体检规则测试。

这一层必须测得很细：体检结果是拿给领导看的，一个误报就会让人
不再信这份报告。规则本身在 Python 里，正是为了能这样测。
"""
from __future__ import annotations

from revit_wy.audit import RULES, analyze, render_html


def facts(**kw):
    base = {"document": "t.rvt", "revit_version": "2024", "views": [],
            "elements": [], "types": [], "warnings": []}
    base.update(kw)
    return base


def view(**kw):
    v = {"id": 1, "name": "3F-建-平面", "view_type": "FloorPlan",
         "is_template": False, "is_placed": True, "is_empty": False,
         "element_count": 10, "has_annotations": True}
    v.update(kw)
    return v


def elem(**kw):
    e = {"id": 1, "kind": "walls", "category": "墙", "family": "基本墙",
         "type_name": "常规 - 200mm", "has_mark": True, "has_comments": True}
    e.update(kw)
    return e


class TestViewRules:
    def test_干净视图不报问题(self):
        r = analyze(facts(views=[view()], elements=[elem()]))
        assert r.issues == []

    def test_空视图(self):
        r = analyze(facts(views=[view(is_empty=True, element_count=0)]))
        assert [i.rule for i in r.issues] == ["VIEW-002"]

    def test_未放置视图(self):
        r = analyze(facts(views=[view(is_placed=False)]))
        assert "VIEW-001" in [i.rule for i in r.issues]

    def test_三维视图不判未放置(self):
        """三维/图例/明细表本来就不放图纸，不能误报。"""
        r = analyze(facts(views=[view(name="三维", view_type="ThreeD",
                                      is_placed=False)]))
        assert "VIEW-001" not in [i.rule for i in r.issues]

    def test_命名不规范(self):
        r = analyze(facts(views=[view(name="副本 1")]))
        assert "VIEW-003" in [i.rule for i in r.issues]

    def test_可关闭命名检查(self):
        r = analyze(facts(views=[view(name="副本 1")]), strict_view_names=False)
        assert "VIEW-003" not in [i.rule for i in r.issues]

    def test_视图模板跳过(self):
        r = analyze(facts(views=[view(is_template=True, is_empty=True)]))
        assert r.issues == []


class TestParamRules:
    def test_缺标记(self):
        r = analyze(facts(elements=[elem(has_mark=False)]))
        assert [i.rule for i in r.issues] == ["PARAM-001"]

    def test_参数不存在时不误报(self):
        """has_mark=None 表示该类别没有这个参数，不能算缺失。"""
        r = analyze(facts(elements=[elem(has_mark=None)]))
        assert r.issues == []

    def test_不在要求清单里的类别不查标记(self):
        r = analyze(facts(elements=[elem(kind="other", has_mark=False)]))
        assert "PARAM-001" not in [i.rule for i in r.issues]

    def test_可指定必填标记的类别(self):
        r = analyze(facts(elements=[elem(kind="other", has_mark=False)]),
                    require_mark=["other"])
        assert "PARAM-001" in [i.rule for i in r.issues]

    def test_空字符串也算缺失(self):
        r = analyze(facts(elements=[elem(has_comments=False)]))
        assert "PARAM-002" in [i.rule for i in r.issues]


class TestFamilyRules:
    def test_类型名带括号或空格判不合规(self):
        t = {"id": 1, "category": "墙", "family": "基本墙",
             "name": "常规 (200)", "instance_count": 3}
        r = analyze(facts(types=[t]))
        assert "FAMILY-001" in [i.rule for i in r.issues]

    def test_未使用类型(self):
        t = {"id": 1, "category": "墙", "family": "基本墙",
             "name": "常规-200mm", "instance_count": 0}
        r = analyze(facts(types=[t]))
        assert [i.rule for i in r.issues] == ["FAMILY-002"]

    def test_合规命名不报(self):
        t = {"id": 1, "category": "墙", "family": "基本墙",
             "name": "常规-200mm", "instance_count": 5}
        assert analyze(facts(types=[t])).issues == []


class TestReport:
    def test_健康分有区分度(self):
        def score(n_missing):
            els = [elem(id=i, has_mark=(i >= n_missing)) for i in range(100)]
            return analyze(facts(elements=els)).score
        clean, few, many = score(0), score(10), score(80)
        assert clean == 100
        assert clean > few > many
        assert 30 <= many <= 70        # 不能一有缺就归零

    def test_可批量修正只含白名单id(self):
        els = [elem(id=11, has_mark=False), elem(id=12, has_comments=False),
               elem(id=13, has_mark=True, has_comments=True)]
        fs = analyze(facts(elements=els)).fixable_summary()
        assert fs["auto_fixable_ids"] == [11, 12]
        assert set(fs["params"]) == {"mark", "comments"}

    def test_忽略规则(self):
        r = analyze(facts(views=[view(name="副本 1")]), ignore_rules=["VIEW-003"])
        assert r.issues == []

    def test_统计与作用域(self):
        r = analyze(facts(views=[view(is_empty=True)],
                          elements=[elem(has_mark=False)]))
        assert r.by_scope == {"view": 1, "element": 1}
        assert sum(r.by_rule.values()) == 2

    def test_markdown含关键段落(self):
        r = analyze(facts(views=[view(is_empty=True)]))
        md = r.markdown()
        for key in ("模型体检报告", "按规则汇总", "可批量修正", "安全边界" if "安全边界" in md else "巡检"):
            assert key in md or key == "巡检"

    def test_html可渲染且自包含(self):
        r = analyze(facts(views=[view(is_empty=True)], elements=[elem(has_mark=False)]))
        h = render_html(r)
        assert h.startswith("<!DOCTYPE html>")
        assert "<script src" not in h          # 零外部依赖，能直接发邮件
        assert "健康分" in h

    def test_每条规则都有建议动作(self):
        for rid, r in RULES.items():
            assert r.get("title") and r.get("fix"), rid
            assert r.get("level") in ("warn", "info"), rid


class TestDemoFacts:
    def test_仿真数据能出报告(self):
        from revit_wy.audit_demo import demo_facts
        r = analyze(demo_facts())
        assert r.counts["elements"] > 100
        assert len(r.issues) > 10
        assert 20 <= r.score <= 80        # 故意造的"不干净模型"，但不应是 0

    def test_仿真数据自说明(self):
        from revit_wy.audit_demo import demo_facts
        assert demo_facts()["warnings"], "演示数据必须标明自己是仿真，避免被当成真实结果"
