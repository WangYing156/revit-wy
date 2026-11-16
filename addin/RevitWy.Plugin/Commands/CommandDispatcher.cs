// RevitWy.Plugin — 方法分发：method 名 → 具体实现
// 所有方法都在 ExternalEvent 里执行（即 Revit UI 线程 + 有效 API 上下文）。
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RevitWy.Plugin.Model;

namespace RevitWy.Plugin
{
    public sealed class CommandDispatcher
    {
        private readonly ExternalEventBridge _bridge;
        private static readonly TimeSpan DefaultTimeout = TimeSpan.FromMinutes(10);

        public CommandDispatcher(ExternalEventBridge bridge)
        {
            _bridge = bridge;
        }

        public object Dispatch(string method, JObject p)
        {
            switch ((method ?? "").ToLowerInvariant())
            {
                case "ping":
                    return _bridge.Invoke(ui => (object)Ping(ui), TimeSpan.FromSeconds(20));
                case "status":
                    return _bridge.Invoke(ui => (object)Status(ui), TimeSpan.FromSeconds(20));
                case "prepare_project":
                    return _bridge.Invoke(ui => (object)PrepareProject(ui, p), DefaultTimeout);
                case "create_levels":
                    return _bridge.Invoke(ui => (object)WithIr(ui, p, (ir, d, o) =>
                    {
                        var lb = new LevelBuilder(d);
                        var map = lb.Build(ir.Levels, new List<string>());
                        return new Dictionary<string, object> { { "created", map.Count } };
                    }), DefaultTimeout);
                case "create_grids":
                    return _bridge.Invoke(ui => (object)WithIr(ui, p, (ir, d, o) =>
                    {
                        var gb = new GridBuilder(d);
                        var map = gb.Build(ir.Grids, new List<string>());
                        return new Dictionary<string, object> { { "created", map.Count } };
                    }), DefaultTimeout);
                case "create_walls":
                    return _bridge.Invoke(ui => (object)CreateWalls(ui, p), DefaultTimeout);
                case "create_columns":
                    return _bridge.Invoke(ui => (object)CreateColumns(ui, p), DefaultTimeout);
                case "create_beams":
                    return _bridge.Invoke(ui => (object)CreateBeams(ui, p), DefaultTimeout);
                case "create_openings":
                    return _bridge.Invoke(ui => (object)CreateOpenings(ui, p), DefaultTimeout);
                case "create_slabs":
                    return _bridge.Invoke(ui => (object)CreateSlabs(ui, p), DefaultTimeout);
                case "build_ir":
                    return _bridge.Invoke(ui => (object)BuildIr(ui, p), TimeSpan.FromMinutes(60));
                case "list_types":
                    return _bridge.Invoke(ui => (object)ListTypes(ui, p), TimeSpan.FromSeconds(40));
                case "delete_tagged":
                    return _bridge.Invoke(ui => (object)DeleteTagged(ui), DefaultTimeout);
                case "select_by_tags":
                    return _bridge.Invoke(ui => (object)SelectByTags(ui, p), TimeSpan.FromSeconds(60));
                default:
                    throw new RwyException(ErrorCodes.Unsupported,
                        "未知方法: " + method + "（可用: ping/status/prepare_project/build_ir/"
                        + "create_levels/create_grids/create_walls/create_columns/create_beams/"
                        + "create_openings/create_slabs/list_types/delete_tagged/select_by_tags）");
            }
        }

        // ---------------------------------------------------------- 基础
        private static Document Doc(UIApplication ui)
        {
            var doc = ui.ActiveUIDocument != null ? ui.ActiveUIDocument.Document : null;
            if (doc == null) throw new RwyException(ErrorCodes.NoDocument, "Revit 没有打开的文档");
            return doc;
        }

        private static object Ping(UIApplication ui)
        {
            var doc = ui.ActiveUIDocument != null ? ui.ActiveUIDocument.Document : null;
            return new
            {
                pong = true,
                revit_version = ui.Application.VersionNumber,
                revit_build = ui.Application.VersionBuild,
                product = ui.Application.VersionName,
                document = doc != null ? doc.Title : null,
                document_path = doc != null ? doc.PathName : null,
                is_family = doc != null && doc.IsFamilyDocument,
                plugin = "revit-wy 0.1.0",
                protocol = "revit-wy/1"
            };
        }

        private static object Status(UIApplication ui)
        {
            var doc = ui.ActiveUIDocument != null ? ui.ActiveUIDocument.Document : null;
            if (doc == null)
                return new { document = (string)null, revit_version = ui.Application.VersionNumber };

            int levels = new FilteredElementCollector(doc).OfClass(typeof(Level)).GetElementCount();
            int grids = new FilteredElementCollector(doc).OfClass(typeof(Grid)).GetElementCount();
            int walls = new FilteredElementCollector(doc).OfClass(typeof(Wall)).GetElementCount();
            int columns = new FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_StructuralColumns)
                .WhereElementIsNotElementType().GetElementCount();
            int doors = new FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_Doors)
                .WhereElementIsNotElementType().GetElementCount();
            int windows = new FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_Windows)
                .WhereElementIsNotElementType().GetElementCount();
            int beams = new FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_StructuralFraming)
                .WhereElementIsNotElementType().GetElementCount();
            int slabs = new FilteredElementCollector(doc)
                .OfCategory(BuiltInCategory.OST_Floors)
                .WhereElementIsNotElementType().GetElementCount();

            int tagged = new FilteredElementCollector(doc)
                .WhereElementIsNotElementType()
                .Count(e =>
                {
                var pp = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                    if (pp == null) return false;
                    string s = pp.AsString();
                    return !string.IsNullOrEmpty(s) && s.StartsWith("revit-wy:");
                });

            return new
            {
                document = doc.Title,
                document_path = doc.PathName,
                is_modified = doc.IsModified,
                revit_version = ui.Application.VersionNumber,
                units = "feet (internal) / mm (IR)",
                counts = new { levels, grids, walls, columns, beams, doors, windows, slabs },
                revit_wy_tagged = tagged,
                levels_detail = new FilteredElementCollector(doc).OfClass(typeof(Level))
                    .Cast<Level>()
                    .OrderBy(l => l.Elevation)
                    .Select(l => new
                    {
                        name = l.Name,
                        elevation_mm = Math.Round(RevitUtils.FeetToMm(l.Elevation), 1)
                    }).ToList()
            };
        }

        // ---------------------------------------------------------- 工程准备
        /// <summary>
        /// 准备项目：单位设为 mm、创建标高。
        /// 为什么不自动新建项目？因为样板（template）选择是院里的规范强约定，
        /// 强行 NewProject 容易产出不符合出图规范的模型。这里只做“就地修正”。
        /// </summary>
        private static object PrepareProject(UIApplication ui, JObject p)
        {
            var doc = Doc(ui);
            var warnings = new List<string>();

            var tx = new Transaction(doc, "revit-wy 准备项目");
            tx.Start();
            try
            {

                // 单位：长度显示为 mm（仅影响显示；API 内部始终是英尺）
                // Revit 2022 起 UnitType.UT_Length / DisplayUnitType 已废弃，
                // 必须用 ForgeTypeId（SpecTypeId.Length + UnitTypeId.Millimeters）。
                try
                {
                    var units = doc.GetUnits();
                    units.SetFormatOptions(SpecTypeId.Length,
                        new FormatOptions(UnitTypeId.Millimeters, 0.1)
                        { SuppressTrailingZeros = true });
                    doc.SetUnits(units);
                }
                catch (Exception ex) { warnings.Add("设置单位为 mm 失败：" + ex.Message); }

                // 标高
                var levelsToken = p["levels"] as JArray;
                if (levelsToken != null && levelsToken.Count > 0)
                {
                var levels = levelsToken.ToObject<List<IrLevel>>();
                var lb = new LevelBuilder(doc);
                    lb.Build(levels, warnings);
                }

                if (tx.GetStatus() == TransactionStatus.Started) tx.Commit();
            }
            catch (Exception ex)
            {
                if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
                throw new RwyException(ErrorCodes.RevitApi, "准备项目失败：" + ex.Message);
            }

            return new
            {
                ok = true,
                document = doc.Title,
                warnings
            };
        }

        // ---------------------------------------------------------- 分构件

        // ---------------------------------------------------------- 事务封装
        /// <summary>
        /// 事务包装：内部若已自行回滚（builder 出错时会 RollBack），这里只回滚剩余未关闭事务，
        /// 避免出现 "The transaction has already been rolled back" 这类二次操作异常。
        /// </summary>
        private static T InTransaction<T>(Document doc, string name, Func<T> body)
        {
            var tx = new Transaction(doc, name);
            try
            {
                tx.Start();
                var result = body();
                if (tx.GetStatus() == TransactionStatus.Started) tx.Commit();
                return result;
            }
            catch
            {
                try
                {
                    if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
                }
                catch { }
                throw;
            }
        }

        private object WithIr(UIApplication ui, JObject p,
                              Func<IrModel, Document, BuildOptions, object> handler)
        {
            var doc = Doc(ui);
            var ir = ParseIrFromPayload(p);
            var opt = new BuildOptions { DryRun = p.Value<bool?>("dry_run") ?? false };
            return InTransaction(doc, "revit-wy", () => handler(ir, doc, opt));
        }

        /// <summary>兼容两种 payload：{"ir": {...}} 或直接给构件数组。</summary>
        private static IrModel ParseIrFromPayload(JObject p)
        {
            var irToken = p["ir"];
            if (irToken != null && irToken.Type == JTokenType.Object)
                return irToken.ToObject<IrModel>();

            // 允许 {"walls":[...]} 这种局部调用
            var model = new IrModel();
            if (p["walls"] != null) model.Walls = p["walls"].ToObject<List<IrWall>>();
            if (p["columns"] != null) model.Columns = p["columns"].ToObject<List<IrColumn>>();
            if (p["beams"] != null) model.Beams = p["beams"].ToObject<List<IrBeam>>();
            if (p["doors"] != null) model.Doors = p["doors"].ToObject<List<IrOpening>>();
            if (p["windows"] != null) model.Windows = p["windows"].ToObject<List<IrOpening>>();
            if (p["slabs"] != null) model.Slabs = p["slabs"].ToObject<List<IrSlab>>();
            if (p["levels"] != null) model.Levels = p["levels"].ToObject<List<IrLevel>>();
            if (p["grids"] != null) model.Grids = p["grids"].ToObject<List<IrGrid>>();
            return model;
        }

        private object CreateWalls(UIApplication ui, JObject p)
        {
            var doc = Doc(ui);
            var ir = ParseIrFromPayload(p);
            return InTransaction(doc, "revit-wy 墙", () =>
            {
                var levels = new Dictionary<string, Level>();
                foreach (var lv in RevitUtils.SortedLevels(doc)) levels[lv.Name] = lv;
                var wb = new WallBuilder(doc, new TypeResolver(doc), levels);
                var w = new List<string>(); var e = new List<string>();
                wb.Build(ir.Walls, w, e);
                wb.EnableJoins();
                return new { created = wb.Created.Count, warnings = w, errors = e };
            });
        }

        private object CreateColumns(UIApplication ui, JObject p)
        {
            var doc = Doc(ui);
            var ir = ParseIrFromPayload(p);
            return InTransaction(doc, "revit-wy 柱", () =>
            {
                var levels = new Dictionary<string, Level>();
                foreach (var lv in RevitUtils.SortedLevels(doc)) levels[lv.Name] = lv;
                var cb = new ColumnBuilder(doc, new TypeResolver(doc), levels);
                var w = new List<string>(); var e = new List<string>();
                cb.Build(ir.Columns, w, e);
                return new { created = cb.Created.Count, warnings = w, errors = e };
            });
        }

        private object CreateBeams(UIApplication ui, JObject p)
        {
            var doc = Doc(ui);
            var ir = ParseIrFromPayload(p);
            return InTransaction(doc, "revit-wy 梁", () =>
            {
                var levels = new Dictionary<string, Level>();
                foreach (var lv in RevitUtils.SortedLevels(doc)) levels[lv.Name] = lv;
                var bb = new BeamBuilder(doc, new TypeResolver(doc), levels);
                var w = new List<string>(); var e = new List<string>();
                bb.Build(ir.Beams, w, e);
                return new { created = bb.Created.Count, warnings = w, errors = e };
            });
        }

        private object CreateOpenings(UIApplication ui, JObject p)
        {
            var doc = Doc(ui);
            var ir = ParseIrFromPayload(p);
            return InTransaction(doc, "revit-wy 门窗", () =>
            {
                var levels = new Dictionary<string, Level>();
                foreach (var lv in RevitUtils.SortedLevels(doc)) levels[lv.Name] = lv;
                var wallMap = new Dictionary<string, Wall>();
                foreach (var e in new FilteredElementCollector(doc).OfClass(typeof(Wall)).Cast<Wall>())
                    {
                        var pc = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                        if (pc == null) continue;
                        var s = pc.AsString();
                        if (string.IsNullOrEmpty(s) || !s.StartsWith("revit-wy:")) continue;
                        int idx = s.LastIndexOf('|');
                        if (idx > 0) wallMap[s.Substring(idx + 1)] = e;
                    }
                var ob = new OpeningBuilder(doc, new TypeResolver(doc), levels, wallMap);
                var w = new List<string>(); var e2 = new List<string>();
                ob.Build(ir.Doors, ir.Windows, w, e2);
                return new { failed = ob.Failed, warnings = w, errors = e2 };
            });
        }

        private object CreateSlabs(UIApplication ui, JObject p)
        {
            var doc = Doc(ui);
            var ir = ParseIrFromPayload(p);
            return InTransaction(doc, "revit-wy 楼板", () =>
            {
                var levels = new Dictionary<string, Level>();
                foreach (var lv in RevitUtils.SortedLevels(doc)) levels[lv.Name] = lv;
                var sb = new SlabBuilder(doc, new TypeResolver(doc), levels);
                var w = new List<string>(); var e = new List<string>();
                sb.Build(ir.Slabs, w, e);
                return new { created = sb.Created.Count, warnings = w, errors = e };
            });
        }

        // ---------------------------------------------------------- 主入口
        private object BuildIr(UIApplication ui, JObject p)
        {
            var doc = Doc(ui);
            var irToken = p["ir"];
            if (irToken == null)
                throw new RwyException(ErrorCodes.BadParams, "缺少 ir 参数");

            var ir = irToken.ToObject<IrModel>();
            var opt = new BuildOptions();
            opt.DryRun = p.Value<bool?>("dry_run") ?? false;
            opt.AutoSave = p.Value<bool?>("auto_save") ?? true;

            var stepsToken = p["steps"] as JArray;
            if (stepsToken != null && stepsToken.Count > 0)
                opt.Steps = stepsToken.Select(t => t.ToString()).ToList();

            var txMode = p.Value<string>("transaction_mode");
            opt.TransactionPerElement = string.Equals(txMode, "per_element", StringComparison.OrdinalIgnoreCase);

            var builder = new ModelBuilder(doc, opt);
            var result = builder.Run(ir);
            return result;
        }

        // ---------------------------------------------------------- 诊断/维护
        private object ListTypes(UIApplication ui, JObject p)
        {
            var doc = Doc(ui);
            string cat = (p.Value<string>("category") ?? "wall").ToLowerInvariant();

            if (cat == "wall")
            {
                return new FilteredElementCollector(doc).OfClass(typeof(WallType))
                    .Cast<WallType>()
                    .Where(w => w.Kind == WallKind.Basic)
                    .Select(w => new
                    {
                        name = w.Name,
                        width_mm = w.GetCompoundStructure() != null
                            ? Math.Round(RevitUtils.FeetToMm(w.GetCompoundStructure().GetWidth()), 1)
                            : 0
                    }).OrderBy(x => x.width_mm).ToList();
            }
            if (cat == "floor")
            {
                return new FilteredElementCollector(doc).OfClass(typeof(FloorType))
                    .Cast<FloorType>()
                    .Select(w => new
                    {
                        name = w.Name,
                        width_mm = w.GetCompoundStructure() != null
                            ? Math.Round(RevitUtils.FeetToMm(w.GetCompoundStructure().GetWidth()), 1)
                            : 0
                    }).ToList();
            }

            var bic = cat == "door" ? BuiltInCategory.OST_Doors
                    : cat == "window" ? BuiltInCategory.OST_Windows
                    : cat == "column" ? BuiltInCategory.OST_StructuralColumns
                    : BuiltInCategory.OST_StructuralFraming;

            return new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol))
                .OfCategory(bic).Cast<FamilySymbol>()
                .Select(s => new
                {
                    family = s.Family != null ? s.Family.Name : "",
                    type = s.Name,
                    active = s.IsActive
                }).OrderBy(x => x.family).ToList();
        }

        /// <summary>
        /// 按 IR 构件 id 选中并缩放到这些构件。
        /// 用途：浏览器工作台里点一行/一个构件，Revit 视图立刻跳到它——这是
        /// 「浏览器看对照、Revit 看真模型」两个载体之间最省事的联动方式。
        ///
        /// 匹配依据是构件 Comments 里的 "revit-wy:<layer>|<id>"，所以只能用
        /// 最近一次成功写入的 IR id；改图后旧 id 会失效，这是预期行为。
        /// </summary>
        private object SelectByTags(UIApplication ui, JObject p)
        {
            var uidoc = ui.ActiveUIDocument;
            if (uidoc == null) throw new RwyException(ErrorCodes.NoDocument, "没有打开的文档");
            var doc = uidoc.Document;

            var wanted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var arr = p["ids"] as JArray;
            if (arr != null)
                foreach (var t in arr) { var s = t.ToString(); if (s.Length > 0) wanted.Add(s); }

            if (wanted.Count == 0)
                throw new RwyException(ErrorCodes.BadParams, "ids 为空");

            var kinds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var ka = p["kinds"] as JArray;
            if (ka != null)
                foreach (var t in ka) { var s = t.ToString(); if (s.Length > 0) kinds.Add(s); }

            var ids = new List<ElementId>();
            int scanned = 0;
            foreach (var e in new FilteredElementCollector(doc).WhereElementIsNotElementType())
            {
                scanned++;
                var pc = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                if (pc == null) continue;
                var val = pc.AsString();
                if (string.IsNullOrEmpty(val) || !val.StartsWith("revit-wy:")) continue;

                int bar = val.LastIndexOf('|');
                if (bar < 0 || bar + 1 >= val.Length) continue;
                var id = val.Substring(bar + 1);
                if (!wanted.Contains(id)) continue;

                if (kinds.Count > 0)
                {
                    var kind = KindOf(e);
                    if (kind != null && !kinds.Contains(kind)) continue;
                }
                ids.Add(e.Id);
            }

            if (ids.Count == 0)
            {
                return new
                {
                    selected = 0,
                    scanned,
                    message = "没有匹配到构件。ids 来自上次成功写入的 IR；"
                            + "若改过图纸并重新构建过 IR，请先重新 rwy_build 再试。"
                };
            }

            uidoc.Selection.SetElementIds(ids);
            try { uidoc.ShowElements(ids); } catch { /* 某些视图不支持缩放，忽略 */ }

            return new { selected = ids.Count, scanned };
        }

        private static string KindOf(Element e)
        {
            var cat = e.Category != null ? e.Category.Id.IntegerValue : 0;
            if (cat == (int)BuiltInCategory.OST_Walls) return "walls";
            if (cat == (int)BuiltInCategory.OST_StructuralColumns) return "columns";
            if (cat == (int)BuiltInCategory.OST_StructuralFraming) return "beams";
            if (cat == (int)BuiltInCategory.OST_Floors) return "slabs";
            if (cat == (int)BuiltInCategory.OST_Doors) return "doors";
            if (cat == (int)BuiltInCategory.OST_Windows) return "windows";
            if (cat == (int)BuiltInCategory.OST_Grids) return "grids";
            return null;
        }

        /// <summary>
        /// 删除上一次翻模生成的构件（依据 Comments 里的 revit-wy: 标记）。
        /// 这是“改了图纸重跑”时避免重复堆叠的关键；不影响手工建的构件。
        /// </summary>
        private object DeleteTagged(UIApplication ui)
        {
            var doc = Doc(ui);
            int deleted = 0;
            var tx = new Transaction(doc, "revit-wy 清理上一次翻模");
            tx.Start();
            try
            {
                var ids = new List<ElementId>();
                foreach (var e in new FilteredElementCollector(doc).WhereElementIsNotElementType())
                {
                var p = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                    if (p == null) continue;
                var s = p.AsString();
                    if (!string.IsNullOrEmpty(s) && s.StartsWith("revit-wy:"))
                        ids.Add(e.Id);
                }
                // 门窗先删（依赖墙），否则删墙会自动删门窗，计数不准
                ids = ids.OrderBy(i => doc.GetElement(i) is Wall ? 1 : 0).ToList();
                foreach (var id in ids)
                {
                    try { if (doc.GetElement(id).Delete().Count > 0) deleted++; } catch { }
                }
                if (tx.GetStatus() == TransactionStatus.Started) tx.Commit();
            }
            catch (Exception ex)
            {
                if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
                throw new RwyException(ErrorCodes.RevitApi, "清理失败：" + ex.Message);
            }
            return new { deleted };
        }
    }
}
