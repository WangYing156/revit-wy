// RevitWy.Plugin — 类型/族匹配器
//
// 翻模最容易翻车的地方不是几何，而是“类型不存在”。
// 这里采用三级兜底策略，保证任何情况都能出一个可用构件：
//   1) 精确匹配已有墙类型名（如 “常规 - 200mm”）
//   2) 按厚度匹配最相近的已有类型（容差 ±20mm）
//   3) 复制一个基准类型并改厚度/名称（Duplicate）
//   4) 全都失败 → 退化到默认类型，并记录 warning
using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace RevitWy.Plugin
{
    public sealed class TypeResolver
    {
        private readonly Document _doc;
        private readonly Dictionary<string, WallType> _wallCache = new Dictionary<string, WallType>();
        private WallType _fallbackWall;
        private WallType _baseWallForDup;
        private readonly Dictionary<string, FamilySymbol> _columnCache = new Dictionary<string, FamilySymbol>();
        private readonly Dictionary<string, FamilySymbol> _beamCache = new Dictionary<string, FamilySymbol>();
        private readonly Dictionary<string, FamilySymbol> _doorCache = new Dictionary<string, FamilySymbol>();
        private readonly Dictionary<string, FamilySymbol> _windowCache = new Dictionary<string, FamilySymbol>();
        public List<string> Warnings { get; private set; }

        public TypeResolver(Document doc)
        {
            _doc = doc;
            Warnings = new List<string>();
        }

        // ------------------------------------------------------------ 墙
        public WallType ResolveWallType(double thicknessMm, string typeHint)
        {
            string key = typeHint ?? ("TH-" + Math.Round(thicknessMm));
            WallType cached;
            if (_wallCache.TryGetValue(key, out cached) && cached.IsValidObject) return cached;

            var all = new FilteredElementCollector(_doc)
                .OfClass(typeof(WallType))
                .Cast<WallType>()
                .Where(w => w.Kind == WallKind.Basic)
                .ToList();

            // 1) 名称精确（去掉 Revit 自动加的数字后缀）
            WallType picked = null;
            if (!string.IsNullOrEmpty(typeHint))
            {
                picked = all.FirstOrDefault(w => w.Name == typeHint)
                      ?? all.FirstOrDefault(w =>
                          StripSuffix(w.Name) == StripSuffix(typeHint));
            }

            // 2) 厚度相近
            if (picked == null)
            {
                double target = RevitUtils.MmToFeet(thicknessMm);
                WallType best = null;
                double bestDiff = double.MaxValue;
                foreach (var w in all)
                {
                    double t = w.GetCompoundStructure() != null
                        ? w.GetCompoundStructure().GetWidth()
                        : RevitUtils.MmToFeet(200);
                    double diff = Math.Abs(t - target);
                    if (diff < bestDiff) { bestDiff = diff; best = w; }
                }
                if (best != null && bestDiff <= RevitUtils.MmToFeet(20)) picked = best;
            }

            // 3) 复制基准类型并改厚度
            if (picked == null && thicknessMm > 1)
            {
                picked = TryDuplicateWallType(thicknessMm, typeHint);
            }

            // 4) 兜底
            if (picked == null)
            {
                picked = FallbackWallType(all);
                Warnings.Add("墙厚 " + Math.Round(thicknessMm) + "mm 无可用类型，退回 “"
                             + (picked != null ? picked.Name : "无") + "”");
            }

            if (picked != null) _wallCache[key] = picked;
            return picked;
        }

        private static string StripSuffix(string name)
        {
            if (string.IsNullOrEmpty(name)) return "";
            // 去掉末尾形如 " 2" / "(2)" 的重复后缀与空格
            return name.Trim().Replace(" ", "").Replace("（", "(").Replace("）", ")");
        }

        private WallType TryDuplicateWallType(double thicknessMm, string typeHint)
        {
            WallType newType = null;
            SubTransaction st = null;
            try
            {
                if (_baseWallForDup == null || !_baseWallForDup.IsValidObject)
                {
                    _baseWallForDup = new FilteredElementCollector(_doc)
                        .OfClass(typeof(WallType))
                        .Cast<WallType>()
                        .FirstOrDefault(w => w.Kind == WallKind.Basic);
                }
                if (_baseWallForDup == null) return null;

                st = new SubTransaction(_doc);
                st.Start();
                newType = _baseWallForDup.Duplicate(
                    string.IsNullOrEmpty(typeHint)
                        ? "常规 - " + Math.Round(thicknessMm) + "mm"
                        : typeHint) as WallType;
                if (newType != null && newType.GetCompoundStructure() != null)
                {
                    var cs = newType.GetCompoundStructure();
                    int idx = cs.GetFirstCoreLayerIndex();
                    if (idx >= 0 && idx < cs.LayerCount)
                    {
                        double delta = RevitUtils.MmToFeet(thicknessMm) - cs.GetLayerWidth(idx);
                        if (Math.Abs(delta) > 1e-9) cs.SetLayerWidth(idx, cs.GetLayerWidth(idx) + delta);
                        newType.SetCompoundStructure(cs);
                    }
                }
                st.Commit();
                return newType;
            }
            catch (Exception ex)
            {
                try { if (st != null) st.RollBack(); } catch { }
                Warnings.Add("复制墙类型失败(" + ex.Message + ")，改用相近类型");
                return null;
            }
        }

        private WallType FallbackWallType(List<WallType> all)
        {
            if (_fallbackWall != null && _fallbackWall.IsValidObject) return _fallbackWall;
            _fallbackWall = all.FirstOrDefault(w =>
                                w.Name.IndexOf("200", StringComparison.Ordinal) >= 0)
                         ?? all.FirstOrDefault();
            return _fallbackWall;
        }

        // ------------------------------------------------------------ 柱
        /// <summary>
        /// 柱：优先找可载入族中已放置的矩形柱符号；参数名兼容中英文（b/h、宽度/深度、Width/Depth）。
        /// 无任何柱族时返回 null，由上层跳过并给出提示。
        /// </summary>
        public FamilySymbol ResolveColumnSymbol(double b, double h)
        {
            string key = Math.Round(b) + "x" + Math.Round(h);
            FamilySymbol cached;
            if (_columnCache.TryGetValue(key, out cached) && cached.IsValidObject) return cached;

            var symbols = new FilteredElementCollector(_doc)
                .OfClass(typeof(FamilySymbol))
                .OfCategory(BuiltInCategory.OST_StructuralColumns)
                .Cast<FamilySymbol>()
                .Where(s => s.Family != null)
                .Where(s => s.Name.IndexOf("圆形", StringComparison.Ordinal) < 0
                         && s.Name.IndexOf("CIRCULAR", StringComparison.OrdinalIgnoreCase) < 0)
                .ToList();

            if (symbols.Count == 0)
            {
                Warnings.Add("项目中无结构柱族，柱已跳过。请先载入“混凝土矩形柱”族（载入族 → 结构/柱）");
                return null;
            }

            FamilySymbol picked = symbols.FirstOrDefault(s =>
                s.Name.Contains(Math.Round(b) + "x" + Math.Round(h))
                || (Math.Abs(s.LookupParameter("b")?.AsDouble() ?? -1) < 1e-6));

            if (picked == null)
            {
                picked = symbols.FirstOrDefault(s =>
                    Math.Abs((s.LookupParameter("b")?.AsDouble() ?? 0) - RevitUtils.MmToFeet(b)) < 1e-6);
            }
            if (picked == null) picked = symbols[0];

            if (!picked.IsActive)
            {
                try { picked.Activate(); _doc.Regenerate(); }
                catch (Exception ex) { Warnings.Add("激活柱族失败：" + ex.Message); }
            }

            TrySetParam(picked, new[] { "b", "宽度", "Width" }, RevitUtils.MmToFeet(b));
            TrySetParam(picked, new[] { "h", "高度", "深度", "Depth" }, RevitUtils.MmToFeet(h));

            _columnCache[key] = picked;
            return picked;
        }

        // ------------------------------------------------------------ 梁
        public FamilySymbol ResolveBeamSymbol(double b, double h)
        {
            string key = Math.Round(b) + "x" + Math.Round(h);
            FamilySymbol cached;
            if (_beamCache.TryGetValue(key, out cached) && cached.IsValidObject) return cached;

            var symbols = new FilteredElementCollector(_doc)
                .OfClass(typeof(FamilySymbol))
                .OfCategory(BuiltInCategory.OST_StructuralFraming)
                .Cast<FamilySymbol>()
                .ToList();

            if (symbols.Count == 0)
            {
                Warnings.Add("项目中无结构框架（梁）族，梁已跳过。请载入“混凝土矩形梁”族");
                return null;
            }

            FamilySymbol picked = symbols.FirstOrDefault(s =>
                    s.Name.Contains(Math.Round(b) + "x" + Math.Round(h)))
                ?? symbols.FirstOrDefault(s =>
                    Math.Abs((s.LookupParameter("b")?.AsDouble() ?? 0) - RevitUtils.MmToFeet(b)) < 1e-6)
                ?? symbols[0];

            if (!picked.IsActive)
            {
                try { picked.Activate(); _doc.Regenerate(); }
                catch (Exception ex) { Warnings.Add("激活梁族失败：" + ex.Message); }
            }
            TrySetParam(picked, new[] { "b", "宽度", "Width" }, RevitUtils.MmToFeet(b));
            TrySetParam(picked, new[] { "h", "高度", "深度", "Depth" }, RevitUtils.MmToFeet(h));

            _beamCache[key] = picked;
            return picked;
        }

        // ------------------------------------------------------------ 门窗
        /// <summary>
        /// 门窗族匹配：块名/提示词 → 族名包含关系 → 宽度最接近 → 任意可用。
        /// 因为国内门窗族命名五花八门（单扇-与墙齐、M0921、Door-Single…），
        /// 模糊匹配 + 宽度兜底的实际命中率明显高于严格匹配。
        /// </summary>
        public FamilySymbol ResolveOpeningSymbol(bool isDoor, double widthMm, string hint)
        {
            var cache = isDoor ? _doorCache : _windowCache;
            string key = (hint ?? "") + "|" + Math.Round(widthMm);
            FamilySymbol cached;
            if (cache.TryGetValue(key, out cached) && cached.IsValidObject) return cached;

            var cat = isDoor ? BuiltInCategory.OST_Doors : BuiltInCategory.OST_Windows;
            var symbols = new FilteredElementCollector(_doc)
                .OfClass(typeof(FamilySymbol))
                .OfCategory(cat)
                .Cast<FamilySymbol>()
                .ToList();

            if (symbols.Count == 0)
            {
                Warnings.Add((isDoor ? "门" : "窗") + "族不存在，已跳过。请载入门窗族");
                return null;
            }

            FamilySymbol picked = null;
            if (!string.IsNullOrEmpty(hint))
            {
                picked = symbols.FirstOrDefault(s =>
                    s.Family != null && s.Family.Name.IndexOf(hint, StringComparison.OrdinalIgnoreCase) >= 0)
                    ?? symbols.FirstOrDefault(s =>
                        s.Name.IndexOf(hint, StringComparison.OrdinalIgnoreCase) >= 0);
            }

            if (picked == null)
            {
                // 按宽度匹配类型名（M0921、C1515 这类命名常见）
                string w = Math.Round(widthMm).ToString("0");
                picked = symbols.FirstOrDefault(s => s.Name.Contains(w));
            }
            if (picked == null) picked = symbols[0];

            if (!picked.IsActive)
            {
                try { picked.Activate(); _doc.Regenerate(); }
                catch (Exception ex) { Warnings.Add("激活门窗族失败：" + ex.Message); }
            }

            cache[key] = picked;
            return picked;
        }

        private static void TrySetParam(Element e, string[] names, double value)
        {
            foreach (var n in names)
            {
                var p = e.LookupParameter(n);
                if (p != null && !p.IsReadOnly && p.StorageType == StorageType.Double)
                {
                    try { p.Set(value); return; } catch { }
                }
            }
        }

        /// <summary>楼板类型：按厚度匹配，找不到就复制/兜底。</summary>
        public FloorType ResolveFloorType(double thicknessMm)
        {
            var types = new FilteredElementCollector(_doc)
                .OfClass(typeof(FloorType))
                .Cast<FloorType>()
                .ToList();
            if (types.Count == 0) return null;

            double target = RevitUtils.MmToFeet(thicknessMm);
            FloorType best = null;
            double bestDiff = double.MaxValue;
            foreach (var t in types)
            {
                var cs = t.GetCompoundStructure();
                if (cs == null) continue;
                double diff = Math.Abs(cs.GetWidth() - target);
                if (diff < bestDiff) { bestDiff = diff; best = t; }
            }
            if (best != null && bestDiff <= RevitUtils.MmToFeet(15)) return best;

            try
            {
                if (best == null) return null;
                var dup = best.Duplicate("常规 - " + Math.Round(thicknessMm) + "mm") as FloorType;
                if (dup == null) return best;
                var cs = dup.GetCompoundStructure();
                if (cs != null)
                {
                    int idx = cs.GetFirstCoreLayerIndex();
                    if (idx >= 0)
                    {
                        cs.SetLayerWidth(idx, target);
                        dup.SetCompoundStructure(cs);
                    }
                }
                return dup;
            }
            catch (Exception ex)
            {
                Warnings.Add("复制楼板类型失败：" + ex.Message);
                return best;
            }
        }
    }
}
