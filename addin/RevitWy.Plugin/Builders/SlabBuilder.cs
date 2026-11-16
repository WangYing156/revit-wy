// RevitWy.Plugin — 楼板生成
using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using RevitWy.Plugin.Model;

namespace RevitWy.Plugin
{
    public sealed class SlabBuilder
    {
        private readonly Document _doc;
        private readonly TypeResolver _types;
        private readonly Dictionary<string, Level> _levels;
        public Dictionary<string, Floor> Created { get; private set; }

        public SlabBuilder(Document doc, TypeResolver types, Dictionary<string, Level> levels)
        {
            _doc = doc;
            _types = types;
            _levels = levels;
            Created = new Dictionary<string, Floor>();
        }

        private Level ResolveLevel(string name)
        {
            Level lv;
            if (!string.IsNullOrEmpty(name) && _levels.TryGetValue(name, out lv)) return lv;
            if (!string.IsNullOrEmpty(name))
            {
                lv = RevitUtils.FindLevel(_doc, name);
                if (lv != null) return lv;
            }
            return RevitUtils.LowestLevel(_doc);
        }

        /// <summary>
        /// 楼板轮廓：用 CurveLoop 闭合。注意 Revit 要求轮廓不自交且首尾相连，
        /// 因此会先做一次闭合检查；退化轮廓（面积≈0）直接跳过。
        /// </summary>
        public void Build(IList<IrSlab> slabs, List<string> warnings, List<string> errors)
        {
            if (slabs == null) return;

            foreach (var s in slabs)
            {
                try
                {
                    if (s.Boundary == null || s.Boundary.Count < 3)
                    {
                        warnings.Add("楼板 " + s.Id + " 轮廓点不足，已跳过");
                        continue;
                    }

                    var floorType = _types.ResolveFloorType(s.ThicknessMm);
                    if (floorType == null) { warnings.Add("楼板 " + s.Id + " 无可用楼板类型"); continue; }

                    var lv = ResolveLevel(s.BaseLevel);
                    if (lv == null) { errors.Add("楼板 " + s.Id + " 无基准标高"); continue; }

                    double z = lv.ProjectElevation;
                    var pts = s.Boundary
                        .Select(p => new XYZ(RevitUtils.MmToFeet(p[0]),
                                             RevitUtils.MmToFeet(p[1]), z))
                        .ToList();

                    // 去掉相邻重复点（距离 < 1mm），否则 CreateBound 会报“曲线太短”
                    var clean = new List<XYZ>();
                    foreach (var p in pts)
                    {
                        if (clean.Count == 0 || clean[clean.Count - 1].DistanceTo(p) > RevitUtils.MmToFeet(1))
                            clean.Add(p);
                    }
                    if (clean.Count > 1 && clean[0].DistanceTo(clean[clean.Count - 1]) < RevitUtils.MmToFeet(1))
                        clean.RemoveAt(clean.Count - 1);

                    if (clean.Count < 3)
                    {
                        warnings.Add("楼板 " + s.Id + " 轮廓退化，已跳过");
                        continue;
                    }

                    var curves = new List<Curve>();
                    for (int i = 0; i < clean.Count; i++)
                    {
                        var a = clean[i];
                        var b = clean[(i + 1) % clean.Count];
                        if (a.DistanceTo(b) < RevitUtils.MmToFeet(1)) continue;
                        curves.Add(Line.CreateBound(a, b));
                    }
                    if (curves.Count < 3)
                    {
                        warnings.Add("楼板 " + s.Id + " 有效边不足，已跳过");
                        continue;
                    }

                    var loop = CurveLoop.Create(curves);
                    var floor = Floor.Create(_doc, new List<CurveLoop> { loop },
                                             floorType.Id, lv.Id);
                    if (floor != null)
                    {
                        Created[s.Id] = floor;
                        try
                        {
                            var p = floor.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                            if (p != null && !p.IsReadOnly)
                                p.Set("revit-wy:" + (s.Layer ?? "") + "|" + s.Id);
                        }
                        catch { }
                    }
                }
                catch (Exception ex)
                {
                    errors.Add("楼板 " + s.Id + " 生成失败：" + ex.Message);
                }
            }
        }
    }
}
