// RevitWy.Plugin — 停靠面板：数模对照确认 + 应用到 Revit
//
// 设计取舍（重要）
// ---------------
// 1) **不嵌 WebView2**。原因见 XAML 顶部注释：崩溃路径多、收益低。
// 2) **面板只做"确认与下发"**，不做图纸解析。因为图纸解析依赖 ezdxf（Python），
//    搬进 C# 等于重写一遍；这里通过 TCP 复用 Python 侧能力，职责不重复。
// 3) **不自动轮询**。Revit 里轮询会持续占用 UI 线程。改为"点刷新"。
// 4) 面板与 AI 并存：AI 在 Cursor 里调 confirm，人在 Revit 里也能改，写的是同一个后端状态。
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using RevitWy.Plugin;

namespace RevitWy.Plugin.UI
{
    public partial class WyDockablePane : UserControl, IDockablePaneProvider
    {
        /// <summary>归属下拉的可选值（与 Python 侧 mapping 约定一致）。</summary>
        public static readonly string[] ComponentOptions =
        {
            "wall", "column", "beam", "door", "window", "slab", "grid",
            "isolate", "ignore", "manual_review"
        };

        private static readonly Dictionary<string, string> Cn =
            new Dictionary<string, string>
            {
                {"wall","墙"},{"column","柱"},{"beam","梁"},{"door","门"},
                {"window","窗"},{"slab","楼板"},{"grid","轴网"},
                {"isolate","隔离"},{"ignore","忽略"},{"manual_review","待确认"}
            };

        private readonly ObservableCollection<LayerRow> _rows =
            new ObservableCollection<LayerRow>();
        private readonly HashSet<string> _changed = new HashSet<string>();
        private UIApplication _ui;
        private IRPreview _preview;
        private DispatcherTimer _timer;

        public WyDockablePane()
        {
            InitializeComponent();
            ComponentOptions_Init();
            LayerGrid.ItemsSource = _rows;

            // 状态灯每 3 秒刷新一次（纯 UI，不碰 Revit API，开销可忽略）
            _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
            _timer.Tick += (s, e) => UpdateStatus();
            _timer.Start();
            UpdateStatus();
        }

        private static void ComponentOptions_Init() { /* 静态数组无需初始化，保留扩展位 */ }

        // ============================================================ 停靠
        public void SetupDockablePane(DockablePaneProviderData data)
        {
            data.FrameworkElement = this;
            data.InitialState = new DockablePaneState
            {
                DockPosition = DockPosition.Right,
                TabBehind = DockablePanes.BuiltInDockablePanes.ProjectBrowser
            };
        }

        public void Attach(UIApplication ui)
        {
            _ui = ui;
            UpdateStatus();
        }

        // ============================================================ 状态
        private void UpdateStatus()
        {
            try
            {
                var server = WyApp.Server;
                bool running = server != null && server.IsRunning;
                StatusDot.Fill = new SolidColorBrush(running
                    ? Color.FromRgb(0x3E, 0xCF, 0x8E)
                    : Color.FromRgb(0xF0, 0x5A, 0x5A));

                var doc = _ui != null && _ui.ActiveUIDocument != null
                    ? _ui.ActiveUIDocument.Document : null;

                StatusText.Text = running
                    ? $"MCP 服务运行中 · 127.0.0.1:{(server != null ? server.Port : 0)}"
                      + (server != null && server.ClientConnected ? " · 已连接客户端" : "")
                    : "MCP 服务未运行（点功能区「重启服务」）";

                DocText.Text = doc != null
                    ? "文档：" + doc.Title
                      + (doc.IsWorkshared ? "（工作共享）" : "")
                    : "文档：无";
            }
            catch (Exception ex)
            {
                StatusText.Text = "状态读取失败：" + ex.Message;
            }
        }

        // ============================================================ 刷新
        private void OnRefresh(object sender, RoutedEventArgs e)
        {
            if (_changed.Count > 0)
            {
                var r = MessageBox.Show(
                    "有未下发的修改，刷新会丢弃。继续？", "revit-wy",
                    MessageBoxButton.OKCancel, MessageBoxImage.Warning);
                if (r != MessageBoxResult.OK) return;
            }
            _changed.Clear();
            _rows.Clear();
            BtnApply.IsEnabled = false;

            try
            {
                // 从 Python 后端取当前对照单（复用 MCP 同一套逻辑，不重复实现）
                var ir = WyBackend.GetPreview();
                if (ir == null)
                {
                    SummaryText.Text = "未取到数据。请先在 Cursor/WorkBuddy 里执行 "
                                     + "rwy_review_mapping，或确认 Python 服务已启动。";
                    return;
                }
                _preview = ir;

                foreach (var L in ir.Layers)
                {
                    _rows.Add(new LayerRow
                    {
                        LayerName = L.Name,
                        Count = L.EntityCount,
                        Component = string.IsNullOrEmpty(L.Component) ? "manual_review" : L.Component,
                        Confidence = (int)Math.Round(L.Confidence * 100),
                        Action = L.Action,
                    });
                }

                var acts = ir.Layers.GroupBy(l => l.Action)
                                   .ToDictionary(g => g.Key, g => g.Count());
                SummaryText.Text = $"共 {ir.Layers.Count} 个图层 ｜ "
                    + $"建模 {Get(acts, "build")} · 待确认 {Get(acts, "manual_review")} · "
                    + $"隔离 {Get(acts, "isolate")} · 忽略 {Get(acts, "ignore")}"
                    + (ir.Truncated ? " ｜ 图层已限流" : "");

                BtnApply.IsEnabled = _rows.Count > 0;
                BtnPreview.IsEnabled = true;
            }
            catch (Exception ex)
            {
                SummaryText.Text = "刷新失败：" + ex.Message;
            }
        }

        private static int Get(Dictionary<string, int> d, string k)
        {
            int v; return d.TryGetValue(k, out v) ? v : 0;
        }

        // ============================================================ 改归属
        private void OnComponentChanged(object sender, SelectionChangedEventArgs e)
        {
            var cb = sender as ComboBox;
            var row = cb != null ? cb.DataContext as LayerRow : null;
            if (row == null) return;

            row.Action = row.Component == "isolate" || row.Component == "ignore"
                       ? row.Component
                       : (row.Component == "manual_review" ? "manual_review" : "build");
            row.RefreshBrush();

            if (row.Component != row.OriginalComponent)
                _changed.Add(row.LayerName);
            else
                _changed.Remove(row.LayerName);

            // 待确认数为 0 时给出可下发的信号
            int pending = _rows.Count(r => r.Component == "manual_review");
            SummaryText.Text = pending == 0
                ? $"全部已确认（{_rows.Count} 个图层）｜ 改动 {_changed.Count} 项，可下发"
                : $"仍有 {pending} 个图层待确认 ｜ 改动 {_changed.Count} 项";
        }

        private void OnRowDoubleClick(object sender, System.Windows.Input.MouseButtonEventArgs e)
        {
            var row = LayerGrid.SelectedItem as LayerRow;
            if (row == null) return;
            // 双击在 建模/隔离 之间切换，比逐个开下拉快
            row.Component = row.Component == "isolate" ? "wall" : "isolate";
            if (row.Component != row.OriginalComponent) _changed.Add(row.LayerName);
            else _changed.Remove(row.LayerName);
            LayerGrid.Items.Refresh();
        }

        // ============================================================ 下发
        private void OnApply(object sender, RoutedEventArgs e)
        {
            if (_changed.Count == 0) { Info("没有改动。"); return; }

            var map = new JObject();
            foreach (var row in _rows)
            {
                if (_changed.Contains(row.LayerName))
                    map[row.LayerName] = row.Component;
            }

            try
            {
                WyBackend.ConfirmMapping(map);
                Info($"已下发 {map.Count} 个图层的归属。\n\n"
                     + "如需生成构件，请在 Cursor/WorkBuddy 里继续：\n"
                     + "rwy_prepare_build → rwy_build(dry_run=false, confirm=true)");
                _changed.Clear();
                OnRefresh(null, null);
            }
            catch (Exception ex)
            {
                Info("下发失败：" + ex.Message);
            }
        }

        private void OnClear(object sender, RoutedEventArgs e)
        {
            if (_ui == null || _ui.ActiveUIDocument == null)
            {
                Info("没有打开的文档。"); return;
            }
            var doc = _ui.ActiveUIDocument.Document;
            var r = MessageBox.Show(
                "删除所有由 revit-wy 生成的构件？\n\n"
                + "只删带 revit-wy 标记的构件，手工建模不受影响。\n"
                + "改图重跑前请先执行这一步，避免重复堆叠。",
                "revit-wy", MessageBoxButton.OKCancel, MessageBoxImage.Question);
            if (r != MessageBoxResult.OK) return;

            try
            {
                int n = WyBackend.DeleteTagged(doc);
                Info($"已删除 {n} 个构件。");
                UpdateStatus();
            }
            catch (Exception ex)
            {
                Info("清理失败：" + ex.Message);
            }
        }

        // ============================================================ 预览
        private void OnPreview(object sender, RoutedEventArgs e)
        {
            var doc = _ui != null && _ui.ActiveUIDocument != null
                ? _ui.ActiveUIDocument.Document : null;
            if (doc == null) { Info("没有打开的文档。"); return; }

            // Revit 里能做的"可视反馈"：把本次翻模的构件在三维视图中高亮并缩放到
            // 这是最轻量、最贴合 BIM 工作者习惯的做法——不用另画平面，直接看模型。
            try
            {
                var ids = WyBackend.CollectTaggedIds(doc);
                if (ids.Count == 0)
                {
                    Info("当前模型里还没有 revit-wy 生成的构件。\n"
                         + "请先在 Cursor/WorkBuddy 里执行 rwy_build。");
                    return;
                }

                var uidoc = _ui.ActiveUIDocument;
                var view = doc.ActiveView;
                uidoc.Selection.SetElementIds(ids);

                // 缩放到选中集（Revit 2022 用 ShowElements）
                uidoc.ShowElements(ids);

                Info($"已选中并缩放到 {ids.Count} 个 revit-wy 构件。\n\n"
                     + "配色建议：在「可见性/图形替换」里按 注释参数(Comments) "
                     + "是否以 revit-wy: 开头过滤，即可一眼区分翻模构件与手工构件。");
            }
            catch (Exception ex)
            {
                Info("预览失败：" + ex.Message);
            }
        }

        private static void Info(string msg)
        {
            TaskDialog.Show("revit-wy", msg);
        }
    }

    // ================================================================ 行模型
    public class LayerRow : INotifyPropertyChanged
    {
        private string _component;

        public string LayerName { get; set; }
        public int Count { get; set; }
        public int Confidence { get; set; }
        public string Action { get; set; }
        public string OriginalComponent { get; set; }

        public string Component
        {
            get { return _component; }
            set
            {
                _component = value;
                if (OriginalComponent == null) OriginalComponent = value;
                OnPropertyChanged();
            }
        }

        public Brush ActionBrush
        {
            get
            {
                switch (Action)
                {
                    case "build": return new SolidColorBrush(Color.FromRgb(0x3E, 0xCF, 0x8E));
                    case "manual_review": return new SolidColorBrush(Color.FromRgb(0xF5, 0xA6, 0x23));
                    case "ignore": return new SolidColorBrush(Color.FromRgb(0x4A, 0x52, 0x62));
                    default: return new SolidColorBrush(Color.FromRgb(0x6B, 0x76, 0x86));
                }
            }
        }

        public void RefreshBrush() { OnPropertyChanged("ActionBrush"); }

        public event PropertyChangedEventHandler PropertyChanged;
        private void OnPropertyChanged([CallerMemberName] string p = null)
        {
            var h = PropertyChanged;
            if (h != null) h(this, new PropertyChangedEventArgs(p));
        }
    }

    // ================================================================ 后端 DTO
    public class IRPreview
    {
        [JsonProperty("path")] public string Path { get; set; }
        [JsonProperty("truncated")] public bool Truncated { get; set; }
        [JsonProperty("layers")] public List<IRPreviewLayer> Layers { get; set; }
    }

    public class IRPreviewLayer
    {
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("component")] public string Component { get; set; }
        [JsonProperty("action")] public string Action { get; set; }
        [JsonProperty("confidence")] public double Confidence { get; set; }
        [JsonProperty("entity_count")] public int EntityCount { get; set; }
    }
}
