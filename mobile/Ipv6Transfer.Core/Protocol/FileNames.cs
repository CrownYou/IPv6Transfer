using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>
/// 文件名与路径安全：对端给的名字一律收敛成安全的单段文件名，绝不越出接收目录。
/// 对应 python 的 sanitize_name / ensure_inside / unique_final_name。
/// </summary>
public static class FileNames
{
    private static readonly HashSet<char> InvalidChars = new() { '<', '>', ':', '"', '/', '\\', '|', '?', '*' };

    private static readonly HashSet<string> ReservedNames = BuildReservedNames();

    private static HashSet<string> BuildReservedNames()
    {
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "CON", "PRN", "AUX", "NUL" };
        for (var i = 1; i <= 9; i++)
        {
            names.Add($"COM{i}");
            names.Add($"LPT{i}");
        }

        return names;
    }

    public static bool IsWindows => RuntimeInformation.IsOSPlatform(OSPlatform.Windows);

    /// <summary>把对端给的名字收敛成一个安全的单段文件名（不含目录）。</summary>
    public static string Sanitize(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
        {
            throw new ProtocolException("文件名为空");
        }

        var name = raw!.Replace('\\', '/').TrimEnd('/');
        var slash = name.LastIndexOf('/');
        if (slash >= 0)
        {
            name = name.Substring(slash + 1);
        }

        var builder = new StringBuilder(name.Length);
        foreach (var ch in name)
        {
            builder.Append(InvalidChars.Contains(ch) || ch < 32 ? '_' : ch);
        }

        name = builder.ToString().Trim().TrimEnd('.', ' ');
        if (name.Length == 0 || name == "." || name == "..")
        {
            throw new ProtocolException("文件名不合法");
        }

        var dot = name.IndexOf('.');
        var stem = dot >= 0 ? name.Substring(0, dot) : name;
        if (ReservedNames.Contains(stem))
        {
            name = "_" + name;
        }

        if (Encoding.UTF8.GetByteCount(name) > ProtocolConst.MaxNameBytes)
        {
            throw new ProtocolException("文件名过长");
        }

        return name;
    }

    /// <summary>双保险：确认最终路径仍在目标目录内。</summary>
    public static string EnsureInside(string root, string candidate)
    {
        var rootAbs = Path.GetFullPath(root);
        var targetAbs = Path.GetFullPath(candidate);
        var comparison = IsWindows ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
        var rootWithSeparator = rootAbs.EndsWith(Path.DirectorySeparatorChar)
            ? rootAbs
            : rootAbs + Path.DirectorySeparatorChar;

        if (!string.Equals(targetAbs, rootAbs, comparison)
            && !targetAbs.StartsWith(rootWithSeparator, comparison))
        {
            throw new ProtocolException("目标路径越出目标目录");
        }

        return targetAbs;
    }

    public static string PartPath(string root, string name)
        => EnsureInside(root, Path.Combine(root, name + ".part"));

    /// <summary>重名时加时间戳后缀，绝不覆盖已有文件。</summary>
    public static string UniqueFinalName(string root, string name)
    {
        var target = Path.Combine(root, name);
        if (!File.Exists(target) && !Directory.Exists(target))
        {
            return target;
        }

        var stem = Path.GetFileNameWithoutExtension(name);
        var ext = Path.GetExtension(name);
        var stamp = DateTime.Now.ToString("yyyyMMdd-HHmmss");
        var candidate = Path.Combine(root, $"{stem}_{stamp}{ext}");
        var counter = 2;
        while (File.Exists(candidate) || Directory.Exists(candidate))
        {
            candidate = Path.Combine(root, $"{stem}_{stamp}-{counter}{ext}");
            counter++;
        }

        return candidate;
    }
}

/// <summary>人类可读的大小 / 速率 / 时间格式化，与 python 输出保持一致的观感。</summary>
public static class Formats
{
    public static string HumanBytes(double num)
    {
        var value = num;
        foreach (var unit in new[] { "B", "KiB", "MiB", "GiB", "TiB" })
        {
            if (Math.Abs(value) < 1024.0 || unit == "TiB")
            {
                return unit == "B"
                    ? $"{(long)value} B"
                    : $"{value.ToString("0.0", System.Globalization.CultureInfo.InvariantCulture)} {unit}";
            }

            value /= 1024.0;
        }

        return $"{value:0.0} TiB";
    }

    public static string HumanRate(double num, double seconds)
        => HumanBytes(seconds > 0 ? num / seconds : 0) + "/s";

    public static string FormatDuration(double? seconds)
    {
        if (seconds is null || seconds < 0)
        {
            return "--:--";
        }

        var total = (long)seconds.Value;
        var hours = total / 3600;
        var minutes = total % 3600 / 60;
        var secs = total % 60;
        return hours > 0
            ? $"{hours:00}:{minutes:00}:{secs:00}"
            : $"{minutes:00}:{secs:00}";
    }

    /// <summary>耗时很短时不显示速率，避免出现 TiB/s 这类噪声。</summary>
    public static string TimingSuffix(double num, double seconds)
        => seconds < 0.2
            ? $"，用时 {seconds * 1000:0}ms"
            : $"，用时 {seconds:0.0}s，{HumanRate(num, seconds)}";
}
