// RevitWy.Plugin — 标高生成
using System;
using System.Collections.Generic;
using Autodesk.Revit.DB;
using RevitWy.Plugin.Model;

namespace RevitWy.Plugin
{
    public sealed class LevelBuilder
    {
        private readonly Document _doc;

        public LevelBuilder(Document doc) { _doc = doc; }

        /// <summary>
        /// 创建标高。已同名的直接复用（避免重复翻模时产生“1层 2”）。
        /// 注意：Level.Create 只能创建在 UI 线程，且必须在事务内。
        /// </summary>
        public Dictionary<string, Level> Build(IList<IrLevel> levels, List<string> warnings)
        {
            var map = new Dictionary<string, Level>();
            if (levels == null) return map;

            foreach (var lv in levels)
            {
                if (string.IsNullOrEmpty(lv.Name)) continue;

                Level existing = RevitUtils.FindLevel(_doc, lv.Name);
                if (existing != null)
                {
                    map[lv.Name] = existing;
                    continue;
                }

                try
                {
                    var created = Level.Create(_doc, RevitUtils.MmToFeet(lv.ElevationMm));
                    created.Name = lv.Name;

                    // 名称冲突时 Revit 会自动改名，这里回读真实名字
                    map[created.Name] = created;
                    if (created.Name != lv.Name && warnings != null)
                        warnings.Add("标高 “" + lv.Name + "” 重名，已改用 “" + created.Name + "”");
                    if (!map.ContainsKey(lv.Name)) map[lv.Name] = created;
                }
                catch (Exception ex)
                {
                    if (warnings != null)
                        warnings.Add("创建标高 “" + lv.Name + "” 失败：" + ex.Message);
                }
            }
            return map;
        }
    }
}
