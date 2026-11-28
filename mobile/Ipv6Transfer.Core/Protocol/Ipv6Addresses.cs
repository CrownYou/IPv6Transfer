using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>一条本机 IPv6 地址记录。</summary>
public sealed class Ipv6AddressEntry
{
    public string Address { get; init; } = "";
    public string Ip { get; init; } = "";
    public string Scope { get; init; } = "other";
    public long? Zone { get; init; }
    public string Interface { get; init; } = "";
    public bool Deprecated { get; init; }
    public bool Temporary { get; init; }
    public string Source { get; init; } = "";
}

/// <summary>
/// 枚举本机 IPv6 地址并给出「地址 (说明)」形式的展示文本。
/// 对应 python 的 list_ipv6_addresses / format_address_entry / parse_ipv6_address。
/// </summary>
public static class Ipv6Addresses
{
    public static readonly IReadOnlyDictionary<string, int> ScopeOrder = new Dictionary<string, int>
    {
        ["global"] = 0,
        ["ula"] = 1,
        ["link-local"] = 2,
        ["other"] = 3,
        ["loopback"] = 4,
    };

    public static readonly IReadOnlyDictionary<string, string> ScopeLabels = new Dictionary<string, string>
    {
        ["global"] = "全局地址",
        ["ula"] = "ULA 私有地址",
        ["link-local"] = "链路本地",
        ["other"] = "其他 IPv6 地址",
        ["loopback"] = "回环地址",
    };

    /// <summary>枚举本机 IPv6 地址；默认不含 ::1，按 全局 &gt; ULA &gt; 链路本地 排序。</summary>
    public static List<Ipv6AddressEntry> List(bool includeLoopback = false)
    {
        var entries = new List<Ipv6AddressEntry>();
        entries.AddRange(FromNetworkInterfaces());
        entries.AddRange(FromHostName());

        if (!entries.Any(e => e.Scope is "global" or "ula"))
        {
            entries.AddRange(RouteProbe());
        }

        var merged = Dedupe(entries);
        if (!includeLoopback)
        {
            merged = merged.Where(e => e.Scope != "loopback").ToList();
        }

        merged.Sort((a, b) =>
        {
            var byScope = ScopeOrder.GetValueOrDefault(a.Scope, 9).CompareTo(ScopeOrder.GetValueOrDefault(b.Scope, 9));
            if (byScope != 0)
            {
                return byScope;
            }

            var byDeprecated = a.Deprecated.CompareTo(b.Deprecated);
            if (byDeprecated != 0)
            {
                return byDeprecated;
            }

            var byTemporary = a.Temporary.CompareTo(b.Temporary);
            if (byTemporary != 0)
            {
                return byTemporary;
            }

            return string.CompareOrdinal(a.Ip, b.Ip);
        });
        return merged;
    }

    private static IEnumerable<Ipv6AddressEntry> FromNetworkInterfaces()
    {
        NetworkInterface[] interfaces;
        try
        {
            interfaces = NetworkInterface.GetAllNetworkInterfaces();
        }
        catch (Exception)
        {
            yield break;
        }

        foreach (var nic in interfaces)
        {
            IPInterfaceProperties properties;
            try
            {
                properties = nic.GetIPProperties();
            }
            catch (Exception)
            {
                continue;
            }

            string interfaceName = nic.Name;
            foreach (var unicast in properties.UnicastAddresses)
            {
                if (unicast.Address.AddressFamily != AddressFamily.InterNetworkV6)
                {
                    continue;
                }

                var entry = MakeEntry(
                    unicast.Address,
                    interfaceName: interfaceName,
                    zone: unicast.Address.IsIPv6LinkLocal ? unicast.Address.ScopeId : null,
                    deprecated: IsDeprecated(unicast),
                    temporary: IsTemporary(unicast),
                    source: "nic");
                if (entry is not null)
                {
                    yield return entry;
                }
            }
        }
    }

    private static bool IsDeprecated(UnicastIPAddressInformation info)
    {
        // Win32 上能拿到地址生命周期；其他平台没有该信息，不标记
        if (!OperatingSystem.IsWindows())
        {
            return false;
        }

        try
        {
            return info.DuplicateAddressDetectionState == DuplicateAddressDetectionState.Deprecated;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static bool IsTemporary(UnicastIPAddressInformation info)
    {
        if (!OperatingSystem.IsWindows())
        {
            return false;
        }

        try
        {
            return info.SuffixOrigin == SuffixOrigin.Random;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static IEnumerable<Ipv6AddressEntry> FromHostName()
    {
        IPAddress[] addresses;
        try
        {
            addresses = Dns.GetHostAddresses(Dns.GetHostName());
        }
        catch (Exception)
        {
            yield break;
        }

        foreach (var address in addresses)
        {
            if (address.AddressFamily != AddressFamily.InterNetworkV6)
            {
                continue;
            }

            var entry = MakeEntry(address, source: "hostname");
            if (entry is not null)
            {
                yield return entry;
            }
        }
    }

    /// <summary>兜底：UDP connect 不发送数据，只让系统选一条 IPv6 出口地址。</summary>
    private static IEnumerable<Ipv6AddressEntry> RouteProbe()
    {
        var probes = new (string Host, int Port)[]
        {
            ("2001:4860:4860::8888", 53),
            ("2400:3200::1", 53),
            ("240c::6666", 53),
        };

        foreach (var (host, port) in probes)
        {
            if (!IPAddress.TryParse(host, out var target))
            {
                continue;
            }

            Ipv6AddressEntry? entry = null;
            using (var socket = new Socket(AddressFamily.InterNetworkV6, SocketType.Dgram, ProtocolType.Udp))
            {
                try
                {
                    socket.Connect(new IPEndPoint(target, port));
                    if (socket.LocalEndPoint is IPEndPoint local)
                    {
                        entry = MakeEntry(local.Address, source: "route");
                    }
                }
                catch (Exception)
                {
                    entry = null;
                }
            }

            if (entry is not null)
            {
                yield return entry;
                yield break;
            }
        }
    }

    private static List<Ipv6AddressEntry> Dedupe(IEnumerable<Ipv6AddressEntry> entries)
    {
        var best = new Dictionary<string, (int Score, Ipv6AddressEntry Entry)>(StringComparer.Ordinal);
        foreach (var entry in entries)
        {
            if (entry is null)
            {
                continue;
            }

            var key = entry.Scope == "link-local"
                ? $"{entry.Ip}%{entry.Zone}"
                : entry.Ip;
            var score = (entry.Interface.Length > 0 ? 2 : 0)
                        + (entry.Deprecated ? 1 : 0)
                        + (entry.Temporary ? 0 : 1);
            if (!best.TryGetValue(key, out var existing) || score > existing.Score)
            {
                best[key] = (score, entry);
            }
        }

        return best.Values.Select(pair => pair.Entry).ToList();
    }

    /// <summary>把地址解析成条目；无效或被过滤时返回 null。</summary>
    public static Ipv6AddressEntry? MakeEntry(
        IPAddress address,
        string interfaceName = "",
        long? zone = null,
        bool deprecated = false,
        bool temporary = false,
        string source = "")
    {
        if (address.AddressFamily != AddressFamily.InterNetworkV6)
        {
            return null;
        }

        var bytes = address.GetAddressBytes();
        if (bytes.Length != 16)
        {
            return null;
        }

        if (address.IsIPv6Multicast || IsUnspecified(bytes) || IsIPv4Mapped(bytes))
        {
            return null;
        }

        if (IsTunnel(bytes))
        {
            return null;
        }

        var canonical = new IPAddress(bytes);
        var scope = Classify(canonical, bytes);
        if (zone is null && canonical.IsIPv6LinkLocal && address.ScopeId != 0)
        {
            zone = address.ScopeId;
        }

        var display = canonical.ToString();
        if (scope == "link-local" && zone is not null)
        {
            display = $"{display}%{zone}";
        }

        if (scope == "loopback")
        {
            interfaceName = string.Empty;
        }

        return new Ipv6AddressEntry
        {
            Address = display,
            Ip = canonical.ToString(),
            Scope = scope,
            Zone = zone,
            Interface = interfaceName,
            Deprecated = deprecated,
            Temporary = temporary,
            Source = source,
        };
    }

    /// <summary>把用户输入的地址（可带 %zone、可带方括号）解析成条目。</summary>
    public static Ipv6AddressEntry? Parse(string? text)
    {
        var value = (text ?? string.Empty).Trim();
        if (value.StartsWith("[", StringComparison.Ordinal) && value.EndsWith("]", StringComparison.Ordinal))
        {
            value = value.Substring(1, value.Length - 2);
        }

        if (!IPAddress.TryParse(value, out var address) || address.AddressFamily != AddressFamily.InterNetworkV6)
        {
            return null;
        }

        var zone = address.ScopeId != 0 ? address.ScopeId : (long?)null;
        var bytes = address.GetAddressBytes();
        var canonical = new IPAddress(bytes);
        if (zone is null)
        {
            return MakeEntry(canonical, source: "manual");
        }

        var entry = MakeEntry(canonical, zone: canonical.IsIPv6LinkLocal ? zone : null, source: "manual");
        if (entry is null)
        {
            return null;
        }

        return canonical.IsIPv6LinkLocal
            ? MakeEntry(canonical, zone: zone, source: "manual")
            : entry;
    }

    private static string Classify(IPAddress address, byte[] bytes)
    {
        if (IPAddress.IsLoopback(address))
        {
            return "loopback";
        }

        if (address.IsIPv6LinkLocal)
        {
            return "link-local";
        }

        // fc00::/7 唯一本地地址、fec0::/10 站点本地地址都按「私有」处理
        if ((bytes[0] & 0xFE) == 0xFC)
        {
            return "ula";
        }

        if (bytes[0] == 0xFE && (bytes[1] & 0xC0) == 0xC0)
        {
            return "ula";
        }

        return "global";
    }

    private static bool IsUnspecified(byte[] bytes) => bytes.All(b => b == 0);

    private static bool IsIPv4Mapped(byte[] bytes)
    {
        for (var i = 0; i < 10; i++)
        {
            if (bytes[i] != 0)
            {
                return false;
            }
        }

        return bytes[10] == 0xFF && bytes[11] == 0xFF;
    }

    private static bool IsTunnel(byte[] bytes)
    {
        // 2001:0::/32 Teredo
        if (bytes[0] == 0x20 && bytes[1] == 0x01 && bytes[2] == 0x00 && bytes[3] == 0x00)
        {
            return true;
        }

        // 2002::/16 6to4
        if (bytes[0] == 0x20 && bytes[1] == 0x02)
        {
            return true;
        }

        // ISATAP 接口标识符里带 :5efe:
        var text = new IPAddress(bytes).ToString();
        return text.Contains(":5efe:", StringComparison.OrdinalIgnoreCase)
               || text.Contains(":0:5efe:", StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>把地址条目渲染成「地址  (说明)」，供列表展示。</summary>
    public static string FormatEntry(Ipv6AddressEntry entry)
    {
        var scope = entry.Scope;
        var parts = new List<string> { ScopeLabels.GetValueOrDefault(scope, ScopeLabels["other"]) };
        if (!string.IsNullOrEmpty(entry.Interface))
        {
            parts.Add(entry.Interface);
        }

        if (scope == "link-local")
        {
            parts.Add(entry.Zone is not null
                ? $"同网段需接口索引 {entry.Zone}"
                : "同网段连接需接口索引");
        }

        if (scope == "global")
        {
            parts.Add("推荐远程连接");
        }
        else if (scope == "ula")
        {
            parts.Add("仅内网可用");
        }
        else if (scope == "loopback")
        {
            parts.Add("仅同机测试");
        }

        if (entry.Deprecated)
        {
            parts.Add("已弃用");
        }

        if (entry.Temporary)
        {
            parts.Add("临时地址，会变化");
        }

        return $"{entry.Address}  ({string.Join(" · ", parts)})";
    }
}
