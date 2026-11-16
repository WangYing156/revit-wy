// RevitWy.Plugin — 版本兼容层
//
// 为什么要这一层
// --------------
// 本项目要支持 Revit 2022 及以上（2022 / 2023 / 2024 / 2025 / 2026 ...），
// 而这些版本在 API 上分成两条线：
//
//   Revit 2022 – 2024 : .NET Framework 4.8 (net48)，ElementId 基于 int32
//   Revit 2025 – 2026 : .NET 8 (net8.0-windows)，ElementId 基于 int64
//   Revit 2027+       : 预计 .NET 10 (net10.0-windows)
//
// 两条线之间有几个必须在**编译期**就分开的差异（运行时才发现就晚了，
// 而且表现是"Revit 静默跳过插件、功能区连按钮都不出现"，最难查）：
//
//   1. ElementId.IntegerValue → ElementId.Value
//      Revit 2024 起 IntegerValue 标注 Obsolete，2025+ 在 .NET 8 上一旦
//      被 JIT 就会抛 NotSupportedException（64 位 id 装不进 int）。
//   2. CS0618 与 TreatWarningsAsErrors：Obsolete 调用在构造函数里可能
//      变成编译错误，必须用#pragma 或直接走新 API。
//   3. TaskDialog 的命名空间在 2025 之后有重排（Autodesk.Revit.UI.TaskDialog
//      仍是主入口，但部分重载被移除）。
//
// 处理方式：本文件提供 `Compat.ToId(long)` / `Compat.IdOf(Element)` 等
// 统一入口，业务代码一律不直接碰 IntegerValue。这样新增一个 Revit 版本时，
// 只需要在 csproj 里加一行 TargetFramework 与 DefineConstants，
// **业务代码零改动**。
//
// 编译符号（由 csproj 按 RevitVersion 注入）：
//   REVIT_2022 / REVIT_2023 / REVIT_2024 / REVIT_2025 / REVIT_2026 ...
//   REVIT_2024_OR_GREATER / REVIT_2025_OR_GREATER / REVIT_2027_OR_GREATER
//   REVIT_NET8_OR_GREATER   （2025+ 的 .NET 8 及以后）
using System;
using System.Collections.Generic;
using Autodesk.Revit.DB;

namespace RevitWy.Plugin
{
    /// <summary>
    /// 跨 Revit 版本的 API 收敛点。业务代码只调这里，不直接碰
    /// IntegerValue / Value / TaskDialog，新增版本时改动面为零。
    /// </summary>
    public static class Compat
    {
        /// <summary>当前编译针对的 Revit 版本号（"2022"…"2027"），来自 csproj 的 DefineConstants。</summary>
        public static readonly string BuildRevitVersion = ResolveBuildVersion();

        /// <summary>该版本是否基于 .NET 8+（Revit 2025 起）。</summary>
        public static bool IsNet8
        {
            get
            {
#if REVIT_NET8_OR_GREATER
                return true;
#else
                return false;
#endif
            }
        }

        private static string ResolveBuildVersion()
        {
#if REVIT_2027
            return "2027";
#elif REVIT_2026
            return "2026";
#elif REVIT_2025
            return "2025";
#elif REVIT_2024
            return "2024";
#elif REVIT_2023
            return "2023";
#elif REVIT_2022
            return "2022";
#else
            return "unknown";
#endif
        }

        // ---------------------------------------------------------- ElementId
        /// <summary>
        /// 用数字造 ElementId。
        /// （本项目的 id 全部来自 Revit 自身，不会超过 int32 范围，
        ///  但 2025+ 的构造函数签名要 long，这里做统一。）
        /// </summary>
        public static ElementId ToId(long value)
        {
#if REVIT_2025_OR_GREATER
            return new ElementId(value);
#else
            return new ElementId((int)value);
#endif
        }

        /// <summary>取 ElementId 的数值。2024 起 IntegerValue 废弃，2025+ 会抛。</summary>
        public static long IdValue(ElementId id)
        {
            if (id == null) return 0;
#if REVIT_2024_OR_GREATER
            return id.Value;
#else
#pragma warning disable CS0618 // 2022/2023 只有 IntegerValue
            return id.IntegerValue;
#pragma warning restore CS0618
#endif
        }

        /// <summary>取元素的 id 数值（业务代码最常用）。</summary>
        public static long IdOf(Element e)
        {
            return e == null ? 0 : IdValue(e.Id);
        }

        /// <summary>
        /// 判断元素是否属于某个 BuiltInCategory。
        ///
        /// 注意：**不能**用 `(int)BuiltInCategory.X` 与 id 数值比较。
        /// BuiltInCategory 的枚举值是负数（如 OST_Walls = -2000011），
        /// 而 ElementId 的数值来自文档分配、是正数，两者永远不等。
        /// 早期版本就是这么写的，导致 KindOf() 恒返回 null、
        /// 工作台点构件联动时只按 id 命中、不按类别过滤。
        /// 正确做法是比较 ElementId。
        /// </summary>
        public static bool IsCategory(Element e, BuiltInCategory bic)
        {
            if (e == null || e.Category == null) return false;
            var want = ToId((long)(int)bic);
            return e.Category.Id.Equals(want);
        }

        /// <summary>把 BuiltInCategory 转成 ElementId（跨版本安全）。</summary>
        public static ElementId CategoryId(BuiltInCategory bic)
        {
            return ToId((long)(int)bic);
        }

        // ---------------------------------------------------------- 参数
        /// <summary>读实例的 Comments（本项目的构件标记都写在这里）。</summary>
        public static string GetComments(Element e)
        {
            if (e == null) return null;
            var p = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
            return p == null ? null : p.AsString();
        }

        /// <summary>写实例的 Comments，只读参数时安全跳过。</summary>
        public static bool SetComments(Element e, string value)
        {
            if (e == null) return false;
            var p = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
            if (p == null || p.IsReadOnly) return false;
            try { return p.Set(value); } catch { return false; }
        }

        /// <summary>
        /// 把一个 ElementId 集合交给选择集。
        /// 2025+ 的 SetElementIds 接收 ICollection&lt;ElementId&gt;，各版本签名一致，
        /// 但某些版本上 List 会被隐式转换，这里显式统一，避免出现"仅在 2023 编译失败"。
        /// </summary>
        public static IList<ElementId> NewIdList()
        {
            return new List<ElementId>();
        }
    }
}
