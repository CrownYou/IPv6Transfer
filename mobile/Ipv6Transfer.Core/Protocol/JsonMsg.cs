using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>
/// 控制消息（JSON 行）的编码/解码，以及字典取值的小工具。
/// 与 python 侧 <c>json.dumps(msg, ensure_ascii=False, separators=(",", ":"))</c> 等价。
/// </summary>
public static class JsonMsg
{
    private static readonly JsonSerializerOptions WriteOptions = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        WriteIndented = false,
    };

    /// <summary>序列化成紧凑 UTF-8 JSON（不转义中文）。</summary>
    public static byte[] Serialize(IDictionary<string, object?> message)
        => JsonSerializer.SerializeToUtf8Bytes(message, WriteOptions);

    /// <summary>解析一行控制消息；必须是 JSON 对象。</summary>
    public static Dictionary<string, object?> ParseObject(byte[] utf8Line)
    {
        try
        {
            using var doc = JsonDocument.Parse(utf8Line);
            if (doc.RootElement.ValueKind != JsonValueKind.Object)
            {
                throw new ProtocolException("控制消息必须是 JSON 对象");
            }

            return (Dictionary<string, object?>)ToClr(doc.RootElement)!;
        }
        catch (JsonException ex)
        {
            throw new ProtocolException($"控制消息不是合法 JSON: {ex.Message}", ex);
        }
    }

    private static object? ToClr(JsonElement element)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                var dict = new Dictionary<string, object?>(StringComparer.Ordinal);
                foreach (var prop in element.EnumerateObject())
                {
                    dict[prop.Name] = ToClr(prop.Value);
                }

                return dict;
            case JsonValueKind.Array:
                var list = new List<object?>();
                foreach (var item in element.EnumerateArray())
                {
                    list.Add(ToClr(item));
                }

                return list;
            case JsonValueKind.String:
                return element.GetString();
            case JsonValueKind.Number:
                return element.TryGetInt64(out var value) ? (object)value : element.GetDouble();
            case JsonValueKind.True:
                return true;
            case JsonValueKind.False:
                return false;
            default:
                return null;
        }
    }
}

/// <summary>从解析出来的控制消息里安全取字段。</summary>
public static class MsgExtensions
{
    public static string? GetStr(this IDictionary<string, object?> msg, string key)
        => msg.TryGetValue(key, out var value) ? value as string : null;

    public static long GetLong(this IDictionary<string, object?> msg, string key, long fallback = 0)
    {
        if (!msg.TryGetValue(key, out var value))
        {
            return fallback;
        }

        return value switch
        {
            long l => l,
            int i => i,
            double d => (long)d,
            string s when long.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed) => parsed,
            _ => fallback,
        };
    }

    public static double GetDouble(this IDictionary<string, object?> msg, string key, double fallback = 0)
    {
        if (!msg.TryGetValue(key, out var value))
        {
            return fallback;
        }

        return value switch
        {
            double d => d,
            long l => l,
            _ => fallback,
        };
    }

    public static bool GetBool(this IDictionary<string, object?> msg, string key, bool fallback = false)
    {
        if (!msg.TryGetValue(key, out var value))
        {
            return fallback;
        }

        return value switch
        {
            bool b => b,
            long l => l != 0,
            _ => fallback,
        };
    }

    public static bool Has(this IDictionary<string, object?> msg, string key)
        => msg.ContainsKey(key);

    public static Dictionary<string, object?>? GetObject(this IDictionary<string, object?> msg, string key)
        => msg.TryGetValue(key, out var value) ? value as Dictionary<string, object?> : null;

    public static List<object?>? GetArray(this IDictionary<string, object?> msg, string key)
        => msg.TryGetValue(key, out var value) ? value as List<object?> : null;

    /// <summary>
    /// 严格取整数（对应 python 的 parse_int）：布尔、字符串、缺失都视为协议错误。
    /// </summary>
    public static long RequireLong(this IDictionary<string, object?> msg, string key, string fieldName)
    {
        if (!msg.TryGetValue(key, out var value))
        {
            throw new ProtocolException($"字段 {fieldName} 必须是整数");
        }

        return value switch
        {
            bool => throw new ProtocolException($"字段 {fieldName} 必须是整数"),
            long l => l,
            int i => i,
            _ => throw new ProtocolException($"字段 {fieldName} 必须是整数"),
        };
    }
}
