// RevitWy.Plugin — 面板用后端适配层
//
// 面板需要三类能力：拿对照单 / 下发确认 / 清理构件。
// 前两个必须走 Python 侧（ezdxf 的能力在 C# 里没有等价物），
// 因此这里内嵌一个**轻量 HTTP 客户端**，连的是本地预览服务（tools/viewer/server.py）。
//
// 为什么不走 MCP 协议：MCP 是"客户端 ↔ 服务端"的会话协议，Revit 插件扮演的是
// MCP 的服务端角色（被 AI 调用），它不应该反过来当 MCP 客户端。
// 面板要的是"人机界面"，用一个简单的本地 JSON 接口更直接，也避免协议循环依赖。
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;
using Autodesk.Revit.DB;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace RevitWy.Plugin.UI
{
    public static class WyBackend
    {
        private static readonly HttpClient Http = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(20)
        };

        /// <summary>面板默认连的本地预览服务端口。</summary>
        public static int ViewerPort = 8770;

        private static string Url(string path)
        {
            return "http://127.0.0.1:" + ViewerPort + path;
        }

        /// <summary>
        /// 取当前图纸的图层对照单。
        /// 若本地预览服务未启动，返回 null，面板会给出可读的指引而不是抛异常。
        /// </summary>
        public static IRPreview GetPreview()
        {
            try
            {
                var txt = Http.GetStringAsync(Url("/api/preview")).Result;
                var jo = JObject.Parse(txt);
                if (jo.Value<bool?>("ok") != true) return null;

                // 后端返回的 layers 里字段名是 snake_case，这里手工映射，
                // 避免为了一个 DTO 全项目加 SnakeCaseNamingStrategy
                var layers = new List<IRPreviewLayer>();
                foreach (var t in jo["layers"] ?? new JArray())
                {
                    layers.Add(new IRPreviewLayer
                    {
                        Name = t.Value<string>("name"),
                        Component = t.Value<string>("component"),
                        Action = t.Value<string>("action"),
                        Confidence = t.Value<double?>("confidence") ?? 0,
                        EntityCount = t.Value<int?>("entity_count") ?? 0,
                    });
                }
                return new IRPreview
                {
                    Path = jo.Value<string>("path"),
                    Truncated = jo.Value<bool?>("truncated") ?? false,
                    Layers = layers,
                };
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>下发图层归属确认（会写入别名库）。</summary>
        public static void ConfirmMapping(JObject decisions)
        {
            var body = new JObject { ["decisions"] = decisions };
            var content = new StringContent(body.ToString(Formatting.None),
                                            Encoding.UTF8, "application/json");
            var txt = Http.PostAsync(Url("/api/confirm"), content).Result
                          .Content.ReadAsStringAsync().Result;
            var jo = JObject.Parse(txt);
            if (jo.Value<bool?>("ok") != true)
                throw new RwyException(ErrorCodes.Internal,
                    jo.Value<string>("error") ?? "确认失败");
        }

        // ---------------------------------------------------------- Revit 侧操作
        /// <summary>
        /// 删除带 revit-wy 标记的构件。
        /// 必须由调用方保证在 UI 线程且在事务内执行（本方法自己开事务）。
        /// </summary>
        public static int DeleteTagged(Document doc)
        {
            int deleted = 0;
            var tx = new Transaction(doc, "revit-wy 清理上次翻模");
            tx.Start();
            try
            {
                var ids = CollectTaggedIds(doc);
                // 先删非墙（门窗依附墙），否则删墙会连带删门窗导致计数不准
                ids = ids.OrderBy(i => doc.GetElement(i) is Wall ? 1 : 0).ToList();
                foreach (var id in ids)
                {
                    try
                    {
                        var e = doc.GetElement(id);
                        if (e != null && e.Delete().Count > 0) deleted++;
                    }
                    catch { /* 单个失败不影响整体 */ }
                }
                if (tx.GetStatus() == TransactionStatus.Started) tx.Commit();
            }
            catch
            {
                if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
                throw;
            }
            return deleted;
        }

        /// <summary>收集所有带 revit-wy 标记的构件 Id。</summary>
        public static IList<ElementId> CollectTaggedIds(Document doc)
        {
            var ids = new List<ElementId>();
            var col = new FilteredElementCollector(doc).WhereElementIsNotElementType();
            foreach (var e in col)
            {
                try
                {
                    var p = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                    if (p == null) continue;
                    var s = p.AsString();
                    if (!string.IsNullOrEmpty(s) && s.StartsWith("revit-wy:"))
                        ids.Add(e.Id);
                }
                catch { }
            }
            return ids;
        }
    }
}
