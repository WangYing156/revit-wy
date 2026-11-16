// RevitWy.Plugin — 翻模编排器
//
// 分工原则：
//   * 每个 builder 只管“把一类构件造出来”，不关心事务；
//   * 编排器负责事务边界、步骤顺序、失败策略、统计汇总。
//
// 顺序不能乱：标高 → 轴网 → 柱 → 墙 → 梁 → 门窗 → 楼板
//   - 墙必须在门窗之前（门窗要 host）
//   - 柱先于墙（墙可以处理与柱的接头）
//   - 楼板最后（避开与墙的几何冲突，减少 Revit 自动剪切告警）
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using Autodesk.Revit.DB;
using RevitWy.Plugin.Model;

namespace RevitWy.Plugin
{
    public sealed class BuildOptions
    {
        public List<string> Steps { get; set; }
        public bool DryRun { get; set; }
        public bool AutoSave { get; set; }
        public bool TransactionPerElement { get; set; }

        public BuildOptions()
        {
            Steps = new List<string> { "levels", "grids", "columns", "walls", "beams", "openings", "slabs" };
            DryRun = false;
            AutoSave = true;
            TransactionPerElement = false;
        }

        public bool Has(string step)
        {
            return Steps == null || Steps.Count == 0
                || Steps.Contains(step, StringComparer.OrdinalIgnoreCase);
        }
    }

    public sealed class BuildResult
    {
        public bool ok { get; set; }
        public bool dry_run { get; set; }
        public double elapsed_s { get; set; }
        public Dictionary<string, int> created { get; set; }
        public Dictionary<string, int> skipped { get; set; }
        public List<string> warnings { get; set; }
        public List<string> errors { get; set; }
        public string document { get; set; }
        public string mode { get; set; }

        public BuildResult()
        {
            created = new Dictionary<string, int>();
            skipped = new Dictionary<string, int>();
            warnings = new List<string>();
            errors = new List<string>();
        }
    }

    public sealed class ModelBuilder
    {
        private readonly Document _doc;
        private readonly BuildOptions _opt;
        private readonly BuildResult _res = new BuildResult();
        private readonly TypeResolver _types;

        public ModelBuilder(Document doc, BuildOptions opt)
        {
            _doc = doc;
            _opt = opt ?? new BuildOptions();
            _types = new TypeResolver(doc);
            _res.document = doc.Title;
            _res.dry_run = _opt.DryRun;
            _res.mode = _opt.TransactionPerElement ? "per_element" : "single_transaction";
        }

        public BuildResult Run(IrModel ir)
        {
            var sw = Stopwatch.StartNew();
            _res.created["levels"] = 0;
            _res.created["grids"] = 0;
            _res.created["columns"] = 0;
            _res.created["walls"] = 0;
            _res.created["beams"] = 0;
            _res.created["doors"] = 0;
            _res.created["windows"] = 0;
            _res.created["slabs"] = 0;

            if (ir == null)
            {
                _res.ok = false;
                _res.errors.Add("IR 为空");
                return _res;
            }

            if (_opt.DryRun)
            {
                // 干跑只做可行性检查：类型/族/标高是否齐备，不创建任何元素
                _res.warnings.Add("干跑模式：仅校验，不创建元素");
                Validate(ir);
                _res.ok = true;
                _res.elapsed_s = Math.Round(sw.Elapsed.TotalSeconds, 2);
                return _res;
            }

            // 单事务：全部成功或全部回滚，最安全；但大数据量时 Revit 可能长时间无响应
            // 分元素事务：失败时已完成的部分保留，适合边看边调
            if (_opt.TransactionPerElement)
            {
                RunPerElement(ir);
            }
            else
            {
                RunSingleTransaction(ir);
            }

            _res.elapsed_s = Math.Round(sw.Elapsed.TotalSeconds, 2);
            _res.ok = _res.errors.Count == 0;
            if (_types.Warnings.Count > 0) _res.warnings.AddRange(_types.Warnings);
            return _res;
        }

        private void Validate(IrModel ir)
        {
            if (!RevitUtils.SortedLevels(_doc).Any())
                _res.errors.Add("项目内没有任何标高，请先创建标高或使用空白建筑样板");

            if (ir.Walls.Count > 0)
            {
                // 干跑不能触发任何写操作（复制类型也是写操作），只做存在性检查
                var wallTypeCount = new FilteredElementCollector(_doc)
                    .OfClass(typeof(WallType)).GetElementCount();
                if (wallTypeCount == 0)
                {
                    _res.errors.Add("项目内没有任何墙类型，请检查是否使用了空样板");
                }
                else
                {
                    var ths = ir.Walls.Select(w => Math.Round(w.ThicknessMm))
                                      .Distinct().OrderBy(x => x).ToList();
                    _res.warnings.Add("将需要以下墙厚类型：" + string.Join("、", ths)
                        + "mm（不存在时会自动复制基准类型并改厚度）");
                }
            }
            if (ir.Doors.Count > 0 &&
                new FilteredElementCollector(_doc).OfCategory(BuiltInCategory.OST_Doors)
                    .OfClass(typeof(FamilySymbol)).ToElements().Count == 0)
            {
                _res.warnings.Add("项目内无门族，门将跳过（请载入“单扇 - 与墙齐”等族）");
            }
            if (ir.Windows.Count > 0 &&
                new FilteredElementCollector(_doc).OfCategory(BuiltInCategory.OST_Windows)
                    .OfClass(typeof(FamilySymbol)).ToElements().Count == 0)
            {
                _res.warnings.Add("项目内无窗族，窗将跳过");
            }
            try
            {
                if (_doc.IsWorkshared && !_doc.IsDetached)
                    _res.warnings.Add("检测到工作共享模型：请在建模前确认当前工作集可编辑，"
                                      + "并已同步中心文件，避免构件被其他人的修改覆盖");
            }
            catch { }
        }

        private void RunSingleTransaction(IrModel ir)
        {
            Transaction tx = null;
            try
            {
                tx = new Transaction(_doc, "revit-wy 一键翻模");
                tx.Start();

                Execute(ir);

                if (_res.errors.Count > 0)
                {
                    if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
                    _res.warnings.Add("出现 " + _res.errors.Count
                        + " 个错误，已整体回滚（模型未变更）。可改用 per_element 模式保留成功部分。");
                }
                else if (tx.GetStatus() == TransactionStatus.Started)
                {
                    tx.Commit();
                    if (_opt.AutoSave) TrySave();
                }
            }
            catch (Exception ex)
            {
                _res.errors.Add("事务异常：" + ex.Message);
                try
                {
                    if (tx != null && tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
                }
                catch { }
            }
        }

        private void RunPerElement(IrModel ir)
        {
            // 每类构件独立事务：某类失败不影响其它类
            var steps = new List<Action>
            {
                () => { if (_opt.Has("levels")) StepLevels(ir); },
                () => { if (_opt.Has("grids")) StepGrids(ir); },
                () => { if (_opt.Has("columns")) StepColumns(ir); },
                () => { if (_opt.Has("walls")) StepWalls(ir); },
                () => { if (_opt.Has("beams")) StepBeams(ir); },
                () => { if (_opt.Has("openings")) StepOpenings(ir); },
                () => { if (_opt.Has("slabs")) StepSlabs(ir); },
            };

            foreach (var step in steps)
            {
                Transaction tx = null;
                try
                {
                    tx = new Transaction(_doc, "revit-wy " + Guid.NewGuid().ToString("N").Substring(0, 6));
                    tx.Start();
                    step();
                    // builder 内部在出错时可能已自行 RollBack，这里只在仍打开时提交
                    if (tx.GetStatus() == TransactionStatus.Started) tx.Commit();
                }
                catch (Exception ex)
                {
                    _res.errors.Add("步骤异常：" + ex.Message);
                    try
                    {
                        if (tx != null && tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
                    }
                    catch { }
                }
            }
            if (_opt.AutoSave) TrySave();
        }

        private void Execute(IrModel ir)
        {
            if (_opt.Has("levels")) StepLevels(ir);
            if (_opt.Has("grids")) StepGrids(ir);
            if (_opt.Has("columns")) StepColumns(ir);
            if (_opt.Has("walls")) StepWalls(ir);
            if (_opt.Has("beams")) StepBeams(ir);
            if (_opt.Has("openings")) StepOpenings(ir);
            if (_opt.Has("slabs")) StepSlabs(ir);
        }

        // ---------------------------------------------------------- 各步骤
        private Dictionary<string, Level> _levelMap = new Dictionary<string, Level>();
        private Dictionary<string, Grid> _gridMap = new Dictionary<string, Grid>();
        private WallBuilder _walls;

        private void StepLevels(IrModel ir)
        {
            var lb = new LevelBuilder(_doc);
            _levelMap = lb.Build(ir.Levels, _res.warnings);
            _res.created["levels"] = _levelMap.Count;
        }

        private void StepGrids(IrModel ir)
        {
            var gb = new GridBuilder(_doc);
            _gridMap = gb.Build(ir.Grids, _res.warnings);
            _res.created["grids"] = _gridMap.Count;
        }

        private void StepColumns(IrModel ir)
        {
            if (_levelMap.Count == 0) _levelMap = LoadAllLevels();
            var cb = new ColumnBuilder(_doc, _types, _levelMap);
            cb.Build(ir.Columns, _res.warnings, _res.errors);
            _res.created["columns"] = cb.Created.Count;
        }

        private void StepWalls(IrModel ir)
        {
            if (_levelMap.Count == 0) _levelMap = LoadAllLevels();
            bool structural = string.Equals(ir.Profile, "struct", StringComparison.OrdinalIgnoreCase);
            _walls = new WallBuilder(_doc, _types, _levelMap);
            _walls.Build(ir.Walls, _res.warnings, _res.errors, structural);
            _walls.EnableJoins();
            _res.created["walls"] = _walls.Created.Count;
        }

        private void StepBeams(IrModel ir)
        {
            if (_levelMap.Count == 0) _levelMap = LoadAllLevels();
            var bb = new BeamBuilder(_doc, _types, _levelMap);
            bb.Build(ir.Beams, _res.warnings, _res.errors);
            _res.created["beams"] = bb.Created.Count;
        }

        private void StepOpenings(IrModel ir)
        {
            if (_levelMap.Count == 0) _levelMap = LoadAllLevels();
            var wallMap = (_walls != null && _walls.Created.Count > 0)
                ? _walls.Created
                : LoadAllWalls();
            var ob = new OpeningBuilder(_doc, _types, _levelMap, wallMap);
            ob.Build(ir.Doors, ir.Windows, _res.warnings, _res.errors);
            _res.created["doors"] = ob.CreatedDoors;
            _res.created["windows"] = ob.CreatedWindows;
            if (ob.HostedByProximity.Count > 0)
                _res.warnings.Add(ob.HostedByProximity.Count
                    + " 个门窗改用就近墙作为宿主（IR 指定的墙 id 未匹配）");
        }

        private void StepSlabs(IrModel ir)
        {
            if (_levelMap.Count == 0) _levelMap = LoadAllLevels();
            var sb = new SlabBuilder(_doc, _types, _levelMap);
            sb.Build(ir.Slabs, _res.warnings, _res.errors);
            _res.created["slabs"] = sb.Created.Count;
        }

        // ---------------------------------------------------------- 辅助
        private Dictionary<string, Level> LoadAllLevels()
        {
            var map = new Dictionary<string, Level>();
            foreach (var lv in RevitUtils.SortedLevels(_doc)) map[lv.Name] = lv;
            return map;
        }

        private Dictionary<string, Wall> LoadAllWalls()
        {
            var map = new Dictionary<string, Wall>();
            foreach (var e in new FilteredElementCollector(_doc).OfClass(typeof(Wall)).Cast<Wall>())
            {
                try
                {
                    var p = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                    if (p == null) continue;
                    string c = p.AsString();
                    if (string.IsNullOrEmpty(c) || !c.StartsWith("revit-wy:")) continue;
                    int idx = c.LastIndexOf('|');
                    if (idx > 0) map[c.Substring(idx + 1)] = e;
                }
                catch { }
            }
            return map;
        }

        private void TrySave()
        {
            try
            {
                if (_doc.IsModified && !string.IsNullOrEmpty(_doc.PathName))
                    _doc.Save();
                else if (_doc.IsModified && _doc.IsFamilyDocument == false)
                    _res.warnings.Add("文档尚未保存过（无路径），已跳过自动保存");
            }
            catch (Exception ex)
            {
                _res.warnings.Add("自动保存失败（模型本身已生成，可手动保存）：" + ex.Message);
            }
        }
    }
}
