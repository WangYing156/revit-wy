// RevitWy.Plugin — 轴网生成
using System;
using System.Collections.Generic;
using Autodesk.Revit.DB;
using RevitWy.Plugin.Model;

namespace RevitWy.Plugin
{
    public sealed class GridBuilder
    {
        private readonly Document _doc;

        public GridBuilder(Document doc) { _doc = doc; }

        /// <summary>
        /// 创建轴网。Revit 的 Grid.Create 会拒绝与已有轴网重合，
        /// 因此先扫一遍已有轴网名称并做去重。
        /// 轴网必须在楼板/墙之前建，否则构件无法“对齐轴网”，后期移动会很痛苦。
        /// </summary>
        public Dictionary<string, Grid> Build(IList<IrGrid> grids, List<string> warnings)
        {
            var map = new Dictionary<string, Grid>();
            if (grids == null) return map;

            var usedNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var e in new FilteredElementCollector(_doc).OfClass(typeof(Grid)))
            {
                if (!string.IsNullOrEmpty(e.Name)) usedNames.Add(e.Name);
            }

            foreach (var g in grids)
            {
                try
                {
                    var p1 = RevitUtils.ToXyz(g.Start);
                    var p2 = RevitUtils.ToXyz(g.End);
                    if (p1.DistanceTo(p2) < RevitUtils.MmToFeet(100))
                    {
                        if (warnings != null)
                            warnings.Add("轴网 “" + g.Name + "” 过短，已跳过");
                        continue;
                    }

                    var line = Line.CreateBound(
                        new XYZ(p1.X, p1.Y, 0), new XYZ(p2.X, p2.Y, 0));
                    var grid = Grid.Create(_doc, line);

                    string name = string.IsNullOrEmpty(g.Name)
                        ? RevitUtils.UniqueName(usedNames, "G")
                        : RevitUtils.UniqueName(usedNames, g.Name);
                    grid.Name = name;
                    map[g.Name] = grid;
                }
                catch (Exception ex)
                {
                    if (warnings != null)
                        warnings.Add("创建轴网 “" + g.Name + "” 失败：" + ex.Message);
                }
            }
            return map;
        }
    }
}
