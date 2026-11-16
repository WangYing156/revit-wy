// RevitWy.Plugin — 模型体检与批量数据治理（试点第一阶段）
//
// 为什么先做这个
// --------------
// 「一键全专业翻模」风险太高：一旦建错，返工成本大、信任一次就没了。
// 而模型体检是**只读**的——查出来的问题给清单，不动模型，
// 团队可以零风险地看到"AI 替人做了多少繁琐核对"，再决定要不要往下走。
//
// 三件事：
//   1. audit_model  只读巡检：无用视图 / 空视图 / 未放置视图 / 无编号视图
//                   + 构件缺参数 + 族命名不合规 + 重复类型，输出结构化清单
//   2. fix_issues   批量修正**只碰白名单参数**（注释/标记/说明），
//                   绝不改几何、不改类型、不删视图——那是另一个量级的风险
//   3. export_audit 落盘 CSV/JSON，能直接进 Excel 给领导看
//
// 设计取舍：所有规则都在 Python 侧（src/revit_wy/audit.py）定义并打分，
// 插件只负责"把原始事实捞出来"。这样调规则不用重编译 DLL，
// 也让规则可以在无 Revit 的机器上单测。
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using Autodesk.Revit.DB;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace RevitWy.Plugin
{
    /// <summary>一条巡检原始事实。判定与评分在 Python 侧完成。</summary>
    public sealed class AuditFacts
    {
        public string schema = "1.0";
        public string revit_version;
        public string document;
        public string document_path;
        public bool is_workshared;
        public int element_count;

        public List<ViewFact> views = new List<ViewFact>();
        public List<ElementFact> elements = new List<ElementFact>();
        public List<TypeFact> types = new List<TypeFact>();
        public List<string> warnings = new List<string>();

        public string ToJson()
        {
            return JsonConvert.SerializeObject(this, Formatting.None);
        }
    }

    public sealed class ViewFact
    {
        public long id;
        public string name;
        public string view_type;          // FloorPlan / Section / ThreeD / Sheet ...
        public bool is_template;
        public bool on_sheet;             // 是否被图纸引用
        public bool is_placed;            // 是否在图纸上
        public bool has_annotations;      // 是否含标注/尺寸/文字
        public int element_count;         // 视图内可见元素数（用于判断空视图）
        public bool is_empty;             // element_count == 0
        public string discipline;
    }

    public sealed class ElementFact
    {
        public long id;
        public string kind;               // walls / columns / beams / doors / windows / slabs / other
        public string category;
        public string family;
        public string type_name;
        public string level;
        public long type_id;
        // 关键参数是否缺失（null 表示该参数不适用于此类别，不参与判定）
        public bool? has_mark;            // 标记（Mark）
        public bool? has_comments;        // 注释
        public bool? has_type_mark;       // 类型标记
        public Dictionary<string, string> params_sample = new Dictionary<string, string>();
    }

    public sealed class TypeFact
    {
        public long id;
        public string category;
        public string family;
        public string name;
        public int instance_count;        // 该类型被用了多少次（0 = 未使用类型）
    }

    /// <summary>模型体检与批量治理。全部在 Revit UI 线程执行。</summary>
    public static class AuditCommands
    {
        /// <summary>巡检范围上限，防止超大模型卡死 UI。</summary>
        private const int MaxElements = 200000;

        // ---------------------------------------------------------------- audit
        public static object AuditModel(UIApplication ui, JObject p)
        {
            var doc = ui.ActiveUIDocument != null ? ui.ActiveUIDocument.Document : null;
            if (doc == null) throw new RwyException(ErrorCodes.NoDocument, "Revit 没有打开的文档");

            var opts = new JObject();
            opts["include_views"] = p.Value<bool?>("include_views") ?? true;
            opts["include_elements"] = p.Value<bool?>("include_elements") ?? true;
            opts["include_types"] = p.Value<bool?>("include_types") ?? true;

            var facts = new AuditFacts
            {
                revit_version = ui.Application.VersionNumber,
                document = doc.Title,
                document_path = doc.PathName,
                is_workshared = SafeIsWorkshared(doc)
            };

            if (opts.Value<bool>("include_views")) CollectViews(doc, facts);
            if (opts.Value<bool>("include_elements")) CollectElements(doc, facts);
            if (opts.Value<bool>("include_types")) CollectTypes(doc, facts);

            return facts;
        }

        private static bool SafeIsWorkshared(Document doc)
        {
            try { return doc.IsWorkshared && !doc.IsDetached; } catch { return false; }
        }

        // ---------------------------------------------------------------- views
        private static void CollectViews(Document doc, AuditFacts facts)
        {
            var sheets = new HashSet<ElementId>();
            try
            {
                foreach (var s in new FilteredElementCollector(doc)
                             .OfClass(typeof(ViewSheet)).Cast<ViewSheet>())
                {
                    foreach (var id in s.GetAllPlacedViews()) sheets.Add(id);
                }
            }
            catch (Exception ex) { facts.warnings.Add("读取图纸引用失败：" + ex.Message); }

            var views = new FilteredElementCollector(doc)
                .OfClass(typeof(View)).Cast<View>()
                .Where(v => v != null && !v.IsTemplate || true)   // 模板也收，交由规则判断
                .ToList();

            foreach (var v in views)
            {
                try
                {
                    var f = new ViewFact
                    {
                        id = Compat.IdOf(v),
                        name = v.Name,
                        view_type = v.ViewType.ToString(),
                        is_template = v.IsTemplate,
                        on_sheet = sheets.Contains(v.Id),
                        is_placed = sheets.Contains(v.Id)
                    };

                    // 空视图判断：只看模型类别（排除标注/参照平面这类"必然存在"的东西）
                    if (!v.IsTemplate)
                    {
                        try
                        {
                            var collector = new FilteredElementCollector(doc, v.Id)
                                .WhereElementIsNotElementType();
                            int n = 0;
                            bool annot = false;
                            foreach (var e in collector)
                            {
                                if (n >= 5000) break;
                                n++;
                                if (e.Category != null &&
                                    (e.Category.CategoryType == CategoryType.Annotation))
                                    annot = true;
                            }
                            f.element_count = n;
                            f.is_empty = n == 0;
                            f.has_annotations = annot;
                        }
                        catch (Exception ex)
                        {
                            facts.warnings.Add("视图 " + v.Name + " 统计失败：" + ex.Message);
                        }
                    }
                    facts.views.Add(f);
                }
                catch (Exception ex)
                {
                    facts.warnings.Add("视图读取失败：" + ex.Message);
                }
            }
        }

        // ------------------------------------------------------------- elements
        private static void CollectElements(Document doc, AuditFacts facts)
        {
            var collector = new FilteredElementCollector(doc)
                .WhereElementIsNotElementType();

            int count = 0;
            foreach (var e in collector)
            {
                if (count++ >= MaxElements)
                {
                    facts.warnings.Add("元素超过 " + MaxElements + " 个，已截断（巡检结果不完整）");
                    break;
                }
                try
                {
                    var cat = e.Category;
                    if (cat == null) continue;
                    // 只关心模型类别，标注/详图不参与"构件参数缺失"统计
                    if (cat.CategoryType != CategoryType.Model) continue;

                    var f = new ElementFact
                    {
                        id = Compat.IdOf(e),
                        category = cat.Name,
                        kind = KindOf(e),
                        family = "",
                        type_name = "",
                        type_id = e.GetTypeId() != null ? Compat.IdValue(e.GetTypeId()) : 0
                    };

                    var et = e as ElementType;
                    if (et == null && e.GetTypeId() != null && e.GetTypeId() != ElementId.InvalidElementId)
                    {
                        var t = doc.GetElement(e.GetTypeId());
                        if (t != null)
                        {
                            f.type_name = t.Name;
                            var ft = t as FamilySymbol;
                            if (ft != null && ft.Family != null) f.family = ft.Family.Name;
                        }
                    }

                    try
                    {
                        var lvl = e.get_Parameter(BuiltInParameter.FAMILY_LEVEL_PARAM)
                               ?? e.get_Parameter(BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM)
                               ?? e.get_Parameter(BuiltInParameter.WALL_BASE_CONSTRAINT);
                        if (lvl != null && lvl.HasValue()) f.level = lvl.AsValueString();
                    }
                    catch { }

                    f.has_mark = HasText(e, BuiltInParameter.ALL_MODEL_MARK);
                    f.has_comments = HasText(e, BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                    f.has_type_mark = HasText(e, BuiltInParameter.ALL_MODEL_TYPE_MARK);

                    facts.elements.Add(f);
                    facts.element_count++;
                }
                catch { /* 单个元素读取失败不应中断整体巡检 */ }
            }
        }

        private static bool? HasText(Element e, BuiltInParameter bip)
        {
            try
            {
                var p = e.get_Parameter(bip);
                if (p == null) return null;                 // 该类别没有此参数 → 不参与判定
                if (p.IsReadOnly && p.StorageType == StorageType.None) return null;
                var s = p.AsString();
                return !string.IsNullOrWhiteSpace(s);
            }
            catch { return null; }
        }

        // ---------------------------------------------------------------- types
        private static void CollectTypes(Document doc, AuditFacts facts)
        {
            try
            {
                var counts = new Dictionary<ElementId, int>();
                foreach (var e in new FilteredElementCollector(doc).WhereElementIsNotElementType())
                {
                    var tid = e.GetTypeId();
                    if (tid == null || tid == ElementId.InvalidElementId) continue;
                    int n;
                    counts.TryGetValue(tid, out n);
                    counts[tid] = n + 1;
                }

                foreach (var t in new FilteredElementCollector(doc).WhereElementIsElementType())
                {
                    var fs = t as FamilySymbol;
                    if (fs == null) continue;
                    if (fs.Category == null || fs.Category.CategoryType != CategoryType.Model) continue;
                    int n;
                    counts.TryGetValue(t.Id, out n);
                    facts.types.Add(new TypeFact
                    {
                        id = Compat.IdOf(t),
                        category = fs.Category.Name,
                        family = fs.Family != null ? fs.Family.Name : "",
                        name = fs.Name,
                        instance_count = n
                    });
                }
            }
            catch (Exception ex) { facts.warnings.Add("类型统计失败：" + ex.Message); }
        }

        private static string KindOf(Element e)
        {
            if (Compat.IsCategory(e, BuiltInCategory.OST_Walls)) return "walls";
            if (Compat.IsCategory(e, BuiltInCategory.OST_StructuralColumns) ||
                Compat.IsCategory(e, BuiltInCategory.OST_Columns)) return "columns";
            if (Compat.IsCategory(e, BuiltInCategory.OST_StructuralFraming)) return "beams";
            if (Compat.IsCategory(e, BuiltInCategory.OST_Floors)) return "slabs";
            if (Compat.IsCategory(e, BuiltInCategory.OST_Doors)) return "doors";
            if (Compat.IsCategory(e, BuiltInCategory.OST_Windows)) return "windows";
            return "other";
        }

        // ------------------------------------------------------------------ fix
        /// <summary>
        /// 批量修正。只允许白名单参数，且只处理显式传入的 id 列表。
        /// 绝不改几何、不改类型、不删元素——这三件事都有单独的、需要人确认的入口。
        /// </summary>
        public static object FixIssues(UIApplication ui, JObject p)
        {
            var doc = ui.ActiveUIDocument != null ? ui.ActiveUIDocument.Document : null;
            if (doc == null) throw new RwyException(ErrorCodes.NoDocument, "Revit 没有打开的文档");

            var param = (p.Value<string>("param") ?? "comments").ToLowerInvariant();
            var value = p.Value<string>("value") ?? "";
            var apply = p.Value<bool?>("apply") ?? false;

            BuiltInParameter bip;
            switch (param)
            {
                case "comments":
                case "注释":
                    bip = BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS; break;
                case "mark":
                case "标记":
                    bip = BuiltInParameter.ALL_MODEL_MARK; break;
                case "description":
                case "说明":
                    bip = BuiltInParameter.ALL_MODEL_DESCRIPTION; break;
                default:
                    throw new RwyException(ErrorCodes.BadParams,
                        "只允许修正 comments / mark / description 三个参数。"
                        + "几何、类型、视图结构不在批量修正范围内（风险不可控）。");
            }

            var idsToken = p["ids"] as JArray;
            if (idsToken == null || idsToken.Count == 0)
                throw new RwyException(ErrorCodes.BadParams, "ids 为空：为避免误改，必须显式给出要修正的元素 id");

            var targets = new List<Element>();
            var skipped = new List<object>();
            foreach (var tok in idsToken)
            {
                try
                {
                    var id = Compat.ToId(tok.Value<long>());
                    var e = doc.GetElement(id);
                    if (e == null) { skipped.Add(new { id = tok.Value<long>(), why = "元素不存在" }); continue; }
                    var pe = e.get_Parameter(bip);
                    if (pe == null) { skipped.Add(new { id = tok.Value<long>(), why = "该元素没有此参数" }); continue; }
                    if (pe.IsReadOnly) { skipped.Add(new { id = tok.Value<long>(), why = "参数只读" }); continue; }
                    targets.Add(e);
                }
                catch (Exception ex) { skipped.Add(new { id = tok.ToString(), why = ex.Message }); }
            }

            if (!apply)
            {
                return new
                {
                    ok = true, dry_run = true,
                    would_change = targets.Count,
                    skipped = skipped,
                    sample = targets.Take(5).Select(e => new
                    {
                        id = Compat.IdOf(e),
                        from = Compat.GetComments(e)
                    }).ToList(),
                    note = "干跑：未修改。确认后传 apply=true"
                };
            }

            int changed = 0;
            var errors = new List<string>();
            var tx = new Transaction(doc, "revit-wy 批量修正参数");
            tx.Start();
            try
            {
                foreach (var e in targets)
                {
                    try
                    {
                        var pe = e.get_Parameter(bip);
                        if (pe != null && !pe.IsReadOnly && pe.Set(value)) changed++;
                    }
                    catch (Exception ex) { errors.Add("id " + Compat.IdOf(e) + "：" + ex.Message); }
                }
                if (tx.GetStatus() == TransactionStatus.Started) tx.Commit();
            }
            catch (Exception ex)
            {
                if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
                throw new RwyException(ErrorCodes.RevitApi, "批量修正失败，已回滚：" + ex.Message);
            }

            return new { ok = errors.Count == 0, changed = changed, skipped = skipped, errors = errors };
        }

        // --------------------------------------------------------------- export
        /// <summary>把巡检原始事实落盘（JSON + CSV），给 Excel / 报告用。</summary>
        public static object ExportAudit(UIApplication ui, JObject p)
        {
            var doc = ui.ActiveUIDocument != null ? ui.ActiveUIDocument.Document : null;
            if (doc == null) throw new RwyException(ErrorCodes.NoDocument, "Revit 没有打开的文档");

            string dir = p.Value<string>("out_dir");
            if (string.IsNullOrWhiteSpace(dir))
                dir = Path.Combine(Environment.GetFolderPath(
                    Environment.SpecialFolder.ApplicationData), "revit-wy", "audit");
            Directory.CreateDirectory(dir);
            string stem = SanitizeFileName(doc.Title) + "_" + DateTime.Now.ToString("yyyyMMdd_HHmmss");

            var facts = (AuditFacts)AuditModel(ui, new JObject());
            string jsonPath = Path.Combine(dir, stem + ".json");
            File.WriteAllText(jsonPath, facts.ToJson(), new UTF8Encoding(true));

            string csvPath = Path.Combine(dir, stem + "_elements.csv");
            var sb = new StringBuilder();
            sb.AppendLine("id,kind,category,family,type,level,has_mark,has_comments");
            foreach (var e in facts.elements)
            {
                sb.AppendLine(string.Join(",", new[]
                {
                    e.id.ToString(), Csv(e.kind), Csv(e.category), Csv(e.family),
                    Csv(e.type_name), Csv(e.level),
                    e.has_mark.HasValue ? (e.has_mark.Value ? "1" : "0") : "",
                    e.has_comments.HasValue ? (e.has_comments.Value ? "1" : "0") : ""
                }));
            }
            File.WriteAllText(csvPath, sb.ToString(), new UTF8Encoding(true));

            return new
            {
                ok = true, json = jsonPath, csv = csvPath, out_dir = dir,
                views = facts.views.Count, elements = facts.elements.Count,
                types = facts.types.Count
            };
        }

        private static string Csv(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            if (s.IndexOfAny(new[] { ',', '"', '\n' }) < 0) return s;
            return "\"" + s.Replace("\"", "\"\"") + "\"";
        }

        private static string SanitizeFileName(string name)
        {
            if (string.IsNullOrEmpty(name)) return "model";
            foreach (var c in Path.GetInvalidFileNameChars()) name = name.Replace(c, '_');
            return name.Length > 60 ? name.Substring(0, 60) : name;
        }
    }
}
