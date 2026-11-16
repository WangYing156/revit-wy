// RevitWy.Plugin — 墙生成
using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using RevitWy.Plugin.Model;

namespace RevitWy.Plugin
{
    public sealed class WallBuilder
    {
        private readonly Document _doc;
        private readonly TypeResolver _types;
        private readonly Dictionary<string, Level> _levels;

        /// <summary>IR wall.id → 生成的 Revit 墙（供门窗找宿主、供增量更新）</summary>
        public Dictionary<string, Wall> Created { get; private set; }

        public WallBuilder(Document doc, TypeResolver types, Dictionary<string, Level> levels)
        {
            _doc = doc;
            _types = types;
            _levels = levels;
            Created = new Dictionary<string, Wall>();
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
        /// 逐条生成墙。参数说明：
        ///   heightMm   直接控制“无约束高度”，比设顶部约束更稳（不依赖标高名是否对得上）
        ///   structural 按 IR 里 profile 推导：结构图出的墙标记为结构墙
        /// 失败不中断整批，记录到 errors 由上层汇总。
        /// </summary>
        public void Build(IList<IrWall> walls, List<string> warnings,
                          List<string> errors, bool structural = false)
        {
            if (walls == null) return;
            const double minLenMm = 10.0;

            foreach (var w in walls)
            {
                try
                {
                    var p1 = RevitUtils.ToXyz(w.Start);
                    var p2 = RevitUtils.ToXyz(w.End);
                    double len = p1.DistanceTo(p2);

                    if (len < RevitUtils.MmToFeet(minLenMm))
                    {
                        warnings.Add("墙 " + w.Id + " 长度 " + Math.Round(RevitUtils.FeetToMm(len), 1)
                                     + "mm 过短，已跳过");
                        continue;
                    }

                    var wallType = _types.ResolveWallType(w.ThicknessMm, w.TypeHint);
                    if (wallType == null)
                    {
                        errors.Add("墙 " + w.Id + " 无可用墙类型");
                        continue;
                    }

                    var baseLevel = ResolveLevel(w.BaseLevel);
                    if (baseLevel == null)
                    {
                        errors.Add("墙 " + w.Id + " 找不到基准标高，且项目内无任何标高");
                        continue;
                    }

                    double baseOffset = baseLevel.ProjectElevation;
                    // 高度缺失时按 3000mm 兜底，避免出现零高度墙
                    double heightMm = w.HeightMm > 1 ? w.HeightMm : 3000.0;
                    double heightFeet = RevitUtils.MmToFeet(heightMm);

                    var curve = Line.CreateBound(
                        new XYZ(p1.X, p1.Y, baseOffset),
                        new XYZ(p2.X, p2.Y, baseOffset));

                    // Wall.Create(doc, curve, wallTypeId, levelId, height, offset, flip, structural)
                    var wall = Wall.Create(_doc, curve, wallType.Id, baseLevel.Id,
                                           heightFeet, 0.0, false, structural);
                    if (wall != null)
                    {
                        Created[w.Id] = wall;
                        try
                        {
                            var p = wall.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                            if (p != null && !p.IsReadOnly)
                                p.Set("revit-wy:" + (w.Layer ?? "") + "|" + w.Id);
                        }
                        catch { }
                    }
                }
                catch (Exception ex)
                {
                    errors.Add("墙 " + w.Id + " 生成失败：" + ex.Message);
                }
            }
        }

        /// <summary>
        /// 允许墙体接头。Revit 在墙共享端点时会自动生成 L/T 接头，
        /// 但如果旧模型或族设置关闭了接头，就会出现双线缝隙。
        /// 这里只做“允许接头”+ 重新生成，不强行用 JoinGeometryUtils（对墙不适用）。
        /// </summary>
        public int EnableJoins()
        {
            int n = 0;
            foreach (var w in Created.Values.Where(w => w != null && w.IsValidObject))
            {
                try
                {
                    if (!WallUtils.IsWallJoinAllowedAtEnd(w, 0)) { WallUtils.AllowWallJoinAtEnd(w, 0); n++; }
                    if (!WallUtils.IsWallJoinAllowedAtEnd(w, 1)) { WallUtils.AllowWallJoinAtEnd(w, 1); n++; }
                }
                catch { }
            }
            _doc.Regenerate();
            return n;
        }
    }
}
