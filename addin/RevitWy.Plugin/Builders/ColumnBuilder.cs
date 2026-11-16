// RevitWy.Plugin — 柱生成
using System;
using System.Collections.Generic;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using RevitWy.Plugin.Model;

namespace RevitWy.Plugin
{
    public sealed class ColumnBuilder
    {
        private readonly Document _doc;
        private readonly TypeResolver _types;
        private readonly Dictionary<string, Level> _levels;
        public Dictionary<string, FamilyInstance> Created { get; private set; }

        public ColumnBuilder(Document doc, TypeResolver types, Dictionary<string, Level> levels)
        {
            _doc = doc;
            _types = types;
            _levels = levels;
            Created = new Dictionary<string, FamilyInstance>();
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
        /// 结构柱：始终“高度受约束到下一层”比固定高度更符合施工图习惯，
        /// 但为避免标高缺失，这里用 baseLevel + 固定高度（稳定优先）。
        /// 旋转通过 FamilyInstance 的 LocationPoint.Rotation 设置。
        /// </summary>
        public void Build(IList<IrColumn> columns, List<string> warnings, List<string> errors)
        {
            if (columns == null) return;

            foreach (var c in columns)
            {
                try
                {
                    var symbol = _types.ResolveColumnSymbol(c.BMm, c.HMm);
                    if (symbol == null) { warnings.Add("柱 " + c.Id + " 无可用族，已跳过"); continue; }

                    var lv = ResolveLevel(c.BaseLevel);
                    if (lv == null) { errors.Add("柱 " + c.Id + " 无基准标高"); continue; }

                    var xyz = RevitUtils.ToXyz(c.Center, RevitUtils.FeetToMm(lv.ProjectElevation));
                    // 结构柱必须传 Structure 重载，否则会被当成建筑柱
                    var inst = _doc.Create.NewFamilyInstance(
                        xyz, symbol, lv, StructuralType.Column);

                    if (inst != null)
                    {
                        double heightMm = c.HeightMm > 1 ? c.HeightMm : 3000.0;
                        TrySet(inst, new[] { "顶部偏移", "Top Offset", "顶部标高偏移" },
                               RevitUtils.MmToFeet(heightMm));
                        TrySetHeight(inst, RevitUtils.MmToFeet(heightMm));

                        if (Math.Abs(c.RotationDeg) > 0.01)
                        {
                            var loc = inst.Location as LocationPoint;
                            if (loc != null)
                            {
                                double rad = c.RotationDeg * Math.PI / 180.0;
                                var axis = Line.CreateBound(xyz, xyz + XYZ.BasisZ);
                                ElementTransformUtils.RotateInstance(inst, axis, rad);
                            }
                        }
                        Created[c.Id] = inst;
                        TagComment(inst, c.Layer, c.Id);
                    }
                }
                catch (Exception ex)
                {
                    errors.Add("柱 " + c.Id + " 生成失败：" + ex.Message);
                }
            }
        }

        private static void TrySet(Element e, string[] names, double valueInFeet)
        {
            foreach (var n in names)
            {
                var p = e.LookupParameter(n);
                if (p != null && !p.IsReadOnly && p.StorageType == StorageType.Double)
                {
                    try { p.Set(valueInFeet); return; } catch { }
                }
            }
        }

        /// <summary>“高度”参数在结构柱里通常是实例参数；不同族名字不同，按候选名逐个试。</summary>
        private static void TrySetHeight(Element e, double heightFeet)
        {
            TrySet(e, new[] { "长度", "Length", "高度", "Height" }, heightFeet);
        }

        private static void TagComment(Element e, string layer, string id)
        {
            try
            {
                var p = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                if (p != null && !p.IsReadOnly) p.Set("revit-wy:" + (layer ?? "") + "|" + id);
            }
            catch { }
        }
    }
}
