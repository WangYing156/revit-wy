// RevitWy.Plugin — 功能区按钮命令
using System;
using System.Diagnostics;
using System.IO;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

namespace RevitWy.Plugin.Commands
{
    [Transaction(TransactionMode.ReadOnly)]
    public class CmdInfo : IExternalCommand
    {
        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            var app = commandData.Application;
            var doc = app.ActiveUIDocument != null ? app.ActiveUIDocument.Document : null;
            var server = WyApp.Server;

            string sb = "revit-wy 一键翻模插件\n"
                      + "────────────────────────\n"
                      + "协议：revit-wy/1（NDJSON over TCP）\n"
                      + "监听：127.0.0.1:" + (server != null ? server.Port : -1) + "\n"
                      + "状态：" + (server != null && server.IsRunning ? "运行中" : "已停止") + "\n"
                      + "客户端：" + (server != null && server.ClientConnected ? "已连接" : "未连接") + "\n"
                      + "Revit：" + app.Application.VersionNumber + " ("
                      + app.Application.VersionBuild + ")\n"
                      + "文档：" + (doc != null ? doc.Title : "无") + "\n";

            if (server != null && !string.IsNullOrEmpty(server.LastError))
                sb += "\n最近错误：" + server.LastError + "\n";

            sb += "\n下一步：在 Cursor / WorkBuddy 中让 AI 调用 rwy_doctor 与 rwy_build。\n"
                + "日志：" + WyApp.LogPath;

            TaskDialog.Show("revit-wy", sb);
            return Result.Succeeded;
        }
    }

    [Transaction(TransactionMode.Manual)]
    public class CmdRestart : IExternalCommand
    {
        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            try
            {
                WyApp.RestartServer();
                TaskDialog.Show("revit-wy", "服务已重启，端口 " + WyApp.Port);
                return Result.Succeeded;
            }
            catch (Exception ex)
            {
                message = ex.Message;
                return Result.Failed;
            }
        }
    }

    [Transaction(TransactionMode.Manual)]
    public class CmdOpenLog : IExternalCommand
    {
        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            try
            {
                if (!File.Exists(WyApp.LogPath)) File.WriteAllText(WyApp.LogPath, "");
                Process.Start("notepad.exe", WyApp.LogPath);
                return Result.Succeeded;
            }
            catch (Exception ex)
            {
                message = ex.Message;
                return Result.Failed;
            }
        }
    }

    /// <summary>手工清理：与 delete_tagged 协议方法等价，供不接 AI 时使用。</summary>
    [Transaction(TransactionMode.Manual)]
    public class CmdDeleteTagged : IExternalCommand
    {
        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            var doc = commandData.Application.ActiveUIDocument.Document;
            var td = new TaskDialog("revit-wy");
            td.MainInstruction = "删除所有 revit-wy 生成的构件？";
            td.MainContent = "只删除带 revit-wy 标记的构件，手工建模的构件不受影响。";
            td.CommonButtons = TaskDialogCommonButtons.Yes | TaskDialogCommonButtons.No;
            if (td.Show() != TaskDialogResult.Yes) return Result.Cancelled;

            int deleted = 0;
            using (var tx = new Transaction(doc, "revit-wy 清理"))
            {
                tx.Start();
                var ids = new System.Collections.Generic.List<ElementId>();
                foreach (var e in new FilteredElementCollector(doc).WhereElementIsNotElementType())
                {
                    var p = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                    if (p == null) continue;
                    var s = p.AsString();
                    if (!string.IsNullOrEmpty(s) && s.StartsWith("revit-wy:")) ids.Add(e.Id);
                }
                foreach (var id in ids)
                {
                    try { if (doc.GetElement(id).Delete().Count > 0) deleted++; } catch { }
                }
                tx.Commit();
            }
            TaskDialog.Show("revit-wy", "已删除 " + deleted + " 个构件");
            return Result.Succeeded;
        }
    }
}
