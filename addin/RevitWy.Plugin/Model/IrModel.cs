// RevitWy.Plugin — IR 数据模型（与 Python 端 ir.py 的 JSON schema 一一对应）
// 改动时务必同步：src/revit_wy/ir.py 与 docs/IR_SCHEMA.md
using System.Collections.Generic;
using Newtonsoft.Json;

namespace RevitWy.Plugin.Model
{
    public class IrModel
    {
        [JsonProperty("schema")] public string Schema { get; set; }
        [JsonProperty("generator")] public string Generator { get; set; }
        [JsonProperty("source")] public string Source { get; set; }
        [JsonProperty("profile")] public string Profile { get; set; }
        [JsonProperty("revit_version")] public string RevitVersion { get; set; }
        [JsonProperty("units")] public string Units { get; set; }
        [JsonProperty("levels")] public List<IrLevel> Levels { get; set; }
        [JsonProperty("grids")] public List<IrGrid> Grids { get; set; }
        [JsonProperty("walls")] public List<IrWall> Walls { get; set; }
        [JsonProperty("columns")] public List<IrColumn> Columns { get; set; }
        [JsonProperty("beams")] public List<IrBeam> Beams { get; set; }
        [JsonProperty("doors")] public List<IrOpening> Doors { get; set; }
        [JsonProperty("windows")] public List<IrOpening> Windows { get; set; }
        [JsonProperty("slabs")] public List<IrSlab> Slabs { get; set; }
        [JsonProperty("meta")] public Dictionary<string, object> Meta { get; set; }

        public IrModel()
        {
            Levels = new List<IrLevel>();
            Grids = new List<IrGrid>();
            Walls = new List<IrWall>();
            Columns = new List<IrColumn>();
            Beams = new List<IrBeam>();
            Doors = new List<IrOpening>();
            Windows = new List<IrOpening>();
            Slabs = new List<IrSlab>();
        }
    }

    /// <summary>所有几何统一 mm，由 RevitUtils.MmToFeet 转换。</summary>
    public class IrLevel
    {
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("elevation_mm")] public double ElevationMm { get; set; }
        [JsonProperty("height_mm")] public double HeightMm { get; set; }
    }

    public class IrGrid
    {
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("start")] public double[] Start { get; set; }
        [JsonProperty("end")] public double[] End { get; set; }
        [JsonProperty("vertical")] public bool Vertical { get; set; }
    }

    public class IrWall
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("start")] public double[] Start { get; set; }
        [JsonProperty("end")] public double[] End { get; set; }
        [JsonProperty("thickness_mm")] public double ThicknessMm { get; set; }
        [JsonProperty("base_level")] public string BaseLevel { get; set; }
        [JsonProperty("top_level")] public string TopLevel { get; set; }
        [JsonProperty("height_mm")] public double HeightMm { get; set; }
        [JsonProperty("type_hint")] public string TypeHint { get; set; }
        [JsonProperty("source")] public string Source { get; set; }
        [JsonProperty("layer")] public string Layer { get; set; }
        [JsonProperty("confidence")] public double Confidence { get; set; }
    }

    public class IrColumn
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("center")] public double[] Center { get; set; }
        [JsonProperty("b_mm")] public double BMm { get; set; }
        [JsonProperty("h_mm")] public double HMm { get; set; }
        [JsonProperty("rotation_deg")] public double RotationDeg { get; set; }
        [JsonProperty("shape")] public string Shape { get; set; }
        [JsonProperty("base_level")] public string BaseLevel { get; set; }
        [JsonProperty("top_level")] public string TopLevel { get; set; }
        [JsonProperty("height_mm")] public double HeightMm { get; set; }
        [JsonProperty("layer")] public string Layer { get; set; }
    }

    public class IrBeam
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("start")] public double[] Start { get; set; }
        [JsonProperty("end")] public double[] End { get; set; }
        [JsonProperty("b_mm")] public double BMm { get; set; }
        [JsonProperty("h_mm")] public double HMm { get; set; }
        [JsonProperty("base_level")] public string BaseLevel { get; set; }
        [JsonProperty("layer")] public string Layer { get; set; }
    }

    public class IrOpening
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("kind")] public string Kind { get; set; }
        [JsonProperty("center")] public double[] Center { get; set; }
        [JsonProperty("width_mm")] public double WidthMm { get; set; }
        [JsonProperty("height_mm")] public double HeightMm { get; set; }
        [JsonProperty("sill_mm")] public double SillMm { get; set; }
        [JsonProperty("rotation_deg")] public double RotationDeg { get; set; }
        [JsonProperty("family_hint")] public string FamilyHint { get; set; }
        [JsonProperty("base_level")] public string BaseLevel { get; set; }
        [JsonProperty("block_name")] public string BlockName { get; set; }
        [JsonProperty("host_wall_id")] public string HostWallId { get; set; }
        [JsonProperty("layer")] public string Layer { get; set; }
    }

    public class IrSlab
    {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("boundary")] public List<double[]> Boundary { get; set; }
        [JsonProperty("thickness_mm")] public double ThicknessMm { get; set; }
        [JsonProperty("base_level")] public string BaseLevel { get; set; }
        [JsonProperty("layer")] public string Layer { get; set; }
    }
}
