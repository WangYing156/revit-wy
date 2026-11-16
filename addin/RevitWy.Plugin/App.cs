// RevitWy.Plugin — IExternalApplication 入口
//
// Revit 2022 加载机制：读到 .addin 清单 → 加载本 DLL → 调 OnStartup。
// 注意：OnStartup 运行在 Revit 主线程且很早期，不要在里面做重活。
// 这里只做三件事：读配置、起 TCP 服务、注册功能区按钮。
using System;
using System.IO;
using System.Reflection;
using System.Text;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Events;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace RevitWy.Plugin
{
    public class WyApp : IExternalApplication
    {
        private SocketServer _server;
        private ExternalEventBridge _bridge;
        private CommandDispatcher _dispatcher;
        private static WyConfig _config;

        public static int Port { get { return _config != null ? _config.Port : 48880; } }
        public static string LogPath
        {
            get
            {
                string dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                    "revit-wy");
                Directory.CreateDirectory(dir);
                return Path.Combine(dir, "plugin.log");
            }
        }

        public static void Log(string msg)
        {
            try
            {
                File.AppendAllText(LogPath,
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + msg + Environment.NewLine,
                    Encoding.UTF8);
            }
            catch { }
        }

        /// <summary>停靠面板唯一 Id。改它等于换一个新的面板实例。</summary>
        public static readonly DockablePaneId PaneId =
            new DockablePaneId(new Guid("D4B1E7A2-6C3F-4E8B-9A21-7F5C0D3B4E19"));

        public static RevitWy.Plugin.UI.WyDockablePane Pane { get; private set; }

        public Result OnStartup(UIControlledApplication app)
        {
            try
            {
                _config = WyConfig.LoadForVersion(app.ControlledApplication.VersionNumber);
                Log("=== revit-wy 启动, Revit " + app.ControlledApplication.VersionNumber
                    + ", port " + _config.Port + " ===");

                _bridge = new ExternalEventBridge();
                _dispatcher = new CommandDispatcher(_bridge);

                _server = new SocketServer(_config.Port,
                    (method, args) =>
                    {
                        var sw = System.Diagnostics.Stopwatch.StartNew();
                        try
                        {
                            return _dispatcher.Dispatch(method, args);
                        }
                        finally
                        {
                            Log(method + " " + sw.ElapsedMilliseconds + "ms");
                        }
                    });
                _server.Start();

                RegisterDockablePane(app);
                BuildRibbon(app);
                Log("监听 127.0.0.1:" + _config.Port + " 成功");
                return Result.Succeeded;
            }
            catch (Exception ex)
            {
                Log("启动失败: " + ex);
                try { TaskDialog.Show("revit-wy",
                    "插件启动失败：\n" + ex.Message + "\n\n日志：" + LogPath); } catch { }
                return Result.Failed;
            }
        }

        public Result OnShutdown(UIControlledApplication app)
        {
            try
            {
                if (_server != null) _server.Dispose();
                Log("=== revit-wy 已卸载 ===");
            }
            catch { }
            return Result.Succeeded;
        }

        // ---------------------------------------------------------- 停靠面板
        /// <summary>
        /// 注册停靠面板。要点：
        ///  · 只在 OnStartup 注册一次，不可重复注册（重复会抛异常）
        ///  · RegisterDockablePane 之后不会自动显示，需要用户在功能区点按钮 Show()
        ///  · 实例必须被静态持有，否则被 GC 后面板变空白
        /// </summary>
        private void RegisterDockablePane(UIControlledApplication app)
        {
            try
            {
                Pane = new RevitWy.Plugin.UI.WyDockablePane();
                app.RegisterDockablePane(PaneId, "revit-wy 翻模", Pane);
                Log("停靠面板已注册");
            }
            catch (Exception ex)
            {
                Log("注册停靠面板失败: " + ex);
            }
        }

        /// <summary>显示面板（需要 UIApplication）。</summary>
        public static void ShowPane(UIApplication uiApp)
        {
            try
            {
                if (Pane != null && uiApp != null) Pane.Attach(uiApp);
                var dp = uiApp.GetDockablePane(PaneId);
                if (dp != null) dp.Show();
            }
            catch (Exception ex)
            {
                Log("显示停靠面板失败: " + ex.Message);
            }
        }

        // ---------------------------------------------------------- Ribbon
        private void BuildRibbon(UIControlledApplication app)
        {
            const string tab = "revit-wy";
            try { app.CreateRibbonTab(tab); } catch { /* 已存在 */ }

            var panel = app.CreateRibbonPanel(tab, "一键翻模");
            var asmPath = Assembly.GetExecutingAssembly().Location;

            var paneBtn = new PushButtonData("WyPane", "翻模面板",
                asmPath, "RevitWy.Plugin.Commands.CmdShowPane");
            paneBtn.ToolTip = "打开停靠面板：查看图层对照、逐条确认归属、清理上次翻模";
            panel.AddItem(paneBtn);
            panel.AddSeparator();

            var infoBtn = new PushButtonData("WyInfo", "连接状态",
                asmPath, "RevitWy.Plugin.Commands.CmdInfo");
            infoBtn.ToolTip = "查看插件监听端口与当前文档状态";
            panel.AddItem(infoBtn);

            var cleanBtn = new PushButtonData("WyClean", "清理上次翻模",
                asmPath, "RevitWy.Plugin.Commands.CmdDeleteTagged");
            cleanBtn.ToolTip = "删除带 revit-wy 标记的构件（重跑前使用，不影响手工模型）";
            panel.AddItem(cleanBtn);

            var restartBtn = new PushButtonData("WyRestart", "重启服务",
                asmPath, "RevitWy.Plugin.Commands.CmdRestart");
            restartBtn.ToolTip = "端口被占用或连接异常时重启 TCP 服务";
            panel.AddItem(restartBtn);

            var logBtn = new PushButtonData("WyLog", "打开日志",
                asmPath, "RevitWy.Plugin.Commands.CmdOpenLog");
            panel.AddItem(logBtn);
        }

        // ---------------------------------------------------------- 配置
        public sealed class WyConfig
        {
            public int Port { get; set; }
            public string Host { get; set; }

            public WyConfig() { Port = 48880; Host = "127.0.0.1"; }

            /// <summary>
            /// 按 Revit 版本查找配置文件：
            /// %APPDATA%\revit-wy\config.json（全局）
            /// 其次：*.addin 同目录下的 config.json
            /// </summary>
            public static WyConfig LoadForVersion(string version)
            {
                var candidates = new System.Collections.Generic.List<string>
                {
                    Path.Combine(Environment.GetFolderPath(
                        Environment.SpecialFolder.ApplicationData), "revit-wy", "config.json"),
                    Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location),
                        "config.json")
                };
                foreach (var c in candidates)
                {
                    try
                    {
                        if (!File.Exists(c)) continue;
                        var cfg = JsonConvert.DeserializeObject<WyConfig>(File.ReadAllText(c));
                        if (cfg != null)
                        {
                            if (cfg.Port <= 1024 || cfg.Port > 65535) cfg.Port = 48880;
                            if (string.IsNullOrEmpty(cfg.Host)) cfg.Host = "127.0.0.1";
                            Log("读取配置: " + c);
                            return cfg;
                        }
                    }
                    catch (Exception ex) { Log("配置读取失败 " + c + ": " + ex.Message); }
                }
                return new WyConfig();
            }
        }

        public static SocketServer Server { get { return _instance != null ? _instance._server : null; } }
        private static WyApp _instance;

        public WyApp() { _instance = this; }

        public static void RestartServer()
        {
            if (_instance == null || _instance._server == null) return;
            _instance._server.Stop();
            _instance._server.Start();
        }
    }
}
