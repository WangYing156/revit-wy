// RevitWy.Plugin — 梁生成
using System;
using System.Collections.Generic;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using RevitWy.Plugin.Model;

namespace RevitWy.Plugin
{
    public sealed class BeamBuilder
    {
        private readonly Document _doc;
        private readonly TypeResolver _types;
        private readonly Dictionary<string, Level> _levels;
        public Dictionary<string, FamilyInstance> Created { get; private set; }

        public BeamBuilder(Document doc, TypeResolver types, Dictionary<string, Level> levels)
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
        /// 梁按“梁顶与楼层标高平齐”放置：Revit 默认梁顶对齐标高，
        /// 因此直接用标高高度作 Z 即可，无需额外偏移。
        /// 曲线必须是直线；斜梁/圆弧梁在 IR 里已被离散为线段，一期不做曲面梁。
        /// </summary>
        public void Build(IList<IrBeam> beams, List<string> warnings, List<string> errors)
        {
            if (beams == null) return;

            foreach (var b in beams)
            {
                try
                {
                    var symbol = _types.ResolveBeamSymbol(b.BMm, b.HMm);
                    if (symbol == null) { warnings.Add("梁 " + b.Id + " 无可用族，已跳过"); continue; }

                    var lv = ResolveLevel(b.BaseLevel);
                    if (lv == null) { errors.Add("梁 " + b.Id + " 无基准标高"); continue; }

                    var p1 = RevitUtils.ToXyz(b.Start, RevitUtils.FeetToMm(lv.ProjectElevation));
                    var p2 = RevitUtils.ToXyz(b.End, RevitUtils.FeetToMm(lv.ProjectElevation));

                    if (p1.DistanceTo(p2) < RevitUtils.MmToFeet(50))
                    {
                        warnings.Add("梁 " + b.Id + " 过短，已跳过");
                        continue;
                    }

                    var line = Line.CreateBound(p1, p2);
                    var inst = _doc.Create.NewFamilyInstance(
                        line, symbol, lv, StructuralType.Beam);

                    if (inst != null)
                    {
                        Created[b.Id] = inst;
                        try
                        {
                            var p = inst.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                            if (p != null && !p.IsReadOnly)
                                p.Set("revit-wy:" + (b.Layer ?? "") + "|" + b.Id);
                        }
                        catch { }
                    }
                }
                catch (Exception ex)
                {
                    errors.Add("梁 " + b.Id + " 生成失败：" + ex.Message);
                }
            }
        }
    }
}
