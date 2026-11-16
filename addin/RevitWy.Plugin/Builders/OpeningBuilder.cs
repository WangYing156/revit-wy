// RevitWy.Plugin 1 — 门窗生成（核心难点：宿主墙）
//
// 门窗必须开在墙上（FamilyInstance 需要 host）。IR 里给的 host_wall_id 来自
// Python 端的几何邻近判断，但可能与 Revit 实际墙位有偏差，所以这里做三级兜底：
//   1) 用 IR 指定的 host_wall_id
//   2) 就近搜索（300mm 内）几何上包含该点的墙
//   3) 仍找不到 → 记录失败，不硬插（硬插会在模型里留悬空构件，清理更麻烦）
using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using RevitWy.Plugin.Model;

namespace RevitWy.Plugin
{
    public sealed class OpeningBuilder
    {
        private readonly Document _doc;
        private readonly TypeResolver _types;
        private readonly Dictionary<string, Level> _levels;
        private readonly Dictionary<string, Wall> _wallMap;

        public List<string> HostedByProximity { get; private set; }
        public int Failed { get; private set; }
        public int CreatedDoors { get; private set; }
        public int CreatedWindows { get; private set; }

        public OpeningBuilder(Document doc, TypeResolver types,
                              Dictionary<string, Level> levels,
                              Dictionary<string, Wall> wallMap)
        {
            _doc = doc;
            _types = types;
            _levels = levels;
            _wallMap = wallMap;
            HostedByProximity = new List<string>();
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

        public void Build(IList<IrOpening> doors, IList<IrOpening> windows,
                          List<string> warnings, List<string> errors)
        {
            BuildOneKind(doors, true, warnings, errors);
            BuildOneKind(windows, false, warnings, errors);
        }

        private void BuildOneKind(IList<IrOpening> items, bool isDoor,
                                  List<string> warnings, List<string> errors)
        {
            if (items == null) return;

            foreach (var op in items)
            {
                try
                {
                    var symbol = _types.ResolveOpeningSymbol(isDoor, op.WidthMm, op.FamilyHint);
                    if (symbol == null) { warnings.Add("门窗 " + op.Id + " 无可用族，已跳过"); continue; }

                    var lv = ResolveLevel(op.BaseLevel);
                    if (lv == null) { errors.Add("门窗 " + op.Id + " 无基准标高"); continue; }

                    Wall host = FindHost(op);
                    if (host == null)
                    {
                        Failed++;
                        errors.Add("门窗 " + op.Id + " 找不到宿主墙（中心点 "
                                   + string.Join(",", op.Center) + "），已跳过以避免悬空构件");
                        continue;
                    }

                    double zMm = RevitUtils.FeetToMm(lv.ProjectElevation) + (isDoor ? 0 : op.SillMm);
                    var xyz = RevitUtils.ToXyz(op.Center, zMm);

                    var inst = _doc.Create.NewFamilyInstance(
                        xyz, symbol, host, lv, StructuralType.NonStructural);

                    if (inst != null)
                    {
                        if (isDoor) CreatedDoors++; else CreatedWindows++;
                        // 窗台高：用“窗台高度”实例参数（存在则设）
                        if (!isDoor && op.SillMm > 0.5)
                        {
                            foreach (var n in new[] { "窗台高度", "Sill Height", "默认窗台高度" })
                            {
                                var p = inst.LookupParameter(n);
                                if (p != null && !p.IsReadOnly && p.StorageType == StorageType.Double)
                                {
                                    try { p.Set(RevitUtils.MmToFeet(op.SillMm)); break; } catch { }
                                }
                            }
                        }

                        // 方向对齐到墙的方向，避免门窗“横插”在墙里
                        if (Math.Abs(op.RotationDeg) > 0.01)
                        {
                            try
                            {
                                var loc = inst.Location as LocationPoint;
                                if (loc != null)
                                {
                                    var axis = Line.CreateBound(xyz, xyz + XYZ.BasisZ);
                                    ElementTransformUtils.RotateInstance(
                                        inst, axis, op.RotationDeg * Math.PI / 180.0);
                                }
                            }
                            catch { }
                        }

                        try
                        {
                            var pc = inst.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                            if (pc != null && !pc.IsReadOnly)
                                pc.Set("revit-wy:" + (op.Layer ?? "") + "|" + op.Id);
                        }
                        catch { }
                    }
                }
                catch (Exception ex)
                {
                    Failed++;
                    errors.Add("门窗 " + op.Id + " 生成失败：" + ex.Message);
                }
            }
        }

        /// <summary>三级兜底找宿主墙。</summary>
        private Wall FindHost(IrOpening op)
        {
            // 1) IR 指定
            if (!string.IsNullOrEmpty(op.HostWallId))
            {
                Wall w;
                if (_wallMap.TryGetValue(op.HostWallId, out w) && w != null && w.IsValidObject)
                    return w;
            }

            // 2) 就近几何搜索（在本次翻模生成的墙里找）
            var pt2d = new XYZ(RevitUtils.MmToFeet(op.Center[0]),
                               RevitUtils.MmToFeet(op.Center[1]), 0);
            Wall best = null;
            double bestDist = double.MaxValue;

            foreach (var w in _wallMap.Values)
            {
                if (w == null || !w.IsValidObject) continue;
                var lc = w.Location as LocationCurve;
                if (lc == null) continue;
                var curve = lc.Curve;

                XYZ proj = curve.Project(pt2d).XYZPoint;
                double d = new XYZ(proj.X - pt2d.X, proj.Y - pt2d.Y, 0).GetLength();

                // 容许 = 墙厚/2 + 150mm
                double tol = w.Width / 2.0 + RevitUtils.MmToFeet(150);
                if (d <= tol && d < bestDist)
                {
                    bestDist = d;
                    best = w;
                }
            }

            // 3) 也在文档内已有墙里找（支持“在已有模型上补门窗”的半自动流程）
            if (best == null)
            {
                double tol = RevitUtils.MmToFeet(400);
                foreach (var e in new FilteredElementCollector(_doc)
                            .OfClass(typeof(Wall)).Cast<Wall>())
                {
                    var lc = e.Location as LocationCurve;
                    if (lc == null) continue;
                    var proj = lc.Curve.Project(pt2d).XYZPoint;
                    double d = new XYZ(proj.X - pt2d.X, proj.Y - pt2d.Y, 0).GetLength();
                    if (d <= tol && d < bestDist) { bestDist = d; best = e; }
                }
            }

            if (best != null && !string.IsNullOrEmpty(op.HostWallId))
                HostedByProximity.Add(op.Id);

            return best;
        }
    }
}
