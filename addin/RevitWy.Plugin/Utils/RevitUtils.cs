// RevitWy.Plugin — 单位换算与几何工具
using System;
using System.Collections.Generic;
using Autodesk.Revit.DB;

namespace RevitWy.Plugin
{
    public static class RevitUtils
    {
        /// <summary>mm → Revit 内部单位（英尺）。Revit 2022 全 API 以英尺为长度单位。</summary>
        public const double MmPerFoot = 304.8;

        public static double MmToFeet(double mm)
        {
            return mm / MmPerFoot;
        }

        public static double FeetToMm(double feet)
        {
            return feet * MmPerFoot;
        }

        /// <summary>IR 的 [x,y] → XYZ（Z 由调用方给）。</summary>
        public static XYZ ToXyz(double[] xy, double zMm)
        {
            if (xy == null || xy.Length < 2)
                throw new RwyException(ErrorCodes.BadParams, "坐标数组长度不足");
            return new XYZ(MmToFeet(xy[0]), MmToFeet(xy[1]), MmToFeet(zMm));
        }

        public static XYZ ToXyz(double[] xy)
        {
            return ToXyz(xy, 0);
        }

        /// <summary>用两条数值线的角度差判断是否正交，用于选墙类型/放置方向。</summary>
        public static double AngleDeg(XYZ a, XYZ b)
        {
            var v = b - a;
            return Math.Atan2(v.Y, v.X) * 180.0 / Math.PI;
        }

        /// <summary>容差内的比较（Revit 默认 ShortCurveTolerance 为 1/32"≈0.8mm）。</summary>
        public static bool AlmostEqual(double a, double b, double tolMm = 0.5)
        {
            return Math.Abs(a - b) <= MmToFeet(tolMm);
        }

        /// <summary>按名称找标高，找不到返回 null（不抛异常，交由调用方兜底）。</summary>
        public static Level FindLevel(Document doc, string name)
        {
            if (string.IsNullOrEmpty(name)) return null;
            var col = new FilteredElementCollector(doc)
                .OfClass(typeof(Level))
                .ToElements();
            foreach (var e in col)
            {
                var lv = e as Level;
                if (lv != null && lv.Name == name) return lv;
            }
            return null;
        }

        public static Level LowestLevel(Document doc)
        {
            Level best = null;
            foreach (var e in new FilteredElementCollector(doc).OfClass(typeof(Level)))
            {
                var lv = e as Level;
                if (lv == null) continue;
                if (best == null || lv.Elevation < best.Elevation) best = lv;
            }
            return best;
        }

        /// <summary>标高排序（升序），用于推导“上一层”。</summary>
        public static List<Level> SortedLevels(Document doc)
        {
            var list = new List<Level>();
            foreach (var e in new FilteredElementCollector(doc).OfClass(typeof(Level)))
            {
                var lv = e as Level;
                if (lv != null) list.Add(lv);
            }
            list.Sort((a, b) => a.Elevation.CompareTo(b.Elevation));
            return list;
        }

        /// <summary>生成不重名的名字：M-001 → M-002 …</summary>
        public static string UniqueName(HashSet<string> used, string baseName)
        {
            if (!used.Contains(baseName))
            {
                used.Add(baseName);
                return baseName;
            }
            int i = 2;
            while (used.Contains(baseName + "-" + i)) i++;
            var name = baseName + "-" + i;
            used.Add(name);
            return name;
        }
    }
}
