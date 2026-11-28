using System;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Ipv6Transfer.Core.Protocol;

/// <summary>连接配置串解析失败。</summary>
public sealed class ConnectionConfigException : Exception
{
    public ConnectionConfigException(string message) : base(message) { }
    public ConnectionConfigException(string message, Exception inner) : base(message, inner) { }
}

/// <summary>连接配置串的内容。</summary>
public sealed class ConnectionConfigInfo
{
    public ConnectionConfigInfo(string host, int port, string password, int version = 1)
    {
        Host = host;
        Port = port;
        Password = password;
        Version = version;
    }

    public string Host { get; }
    public int Port { get; }
    public string Password { get; }
    public int Version { get; }
}

/// <summary>
/// 把地址、端口、密码序列化成一段可复制粘贴的 <c>FT6.xxx.yyyyyyyy</c> 连接串。
/// 只是 Base64 + 校验和，等同于密码，不是加密。
/// </summary>
public static class ConnectionConfigCodec
{
    private const string Prefix = ProtocolConst.ConfigPrefix + ".";

    private static readonly Regex TokenPattern = new(
        @"FT6\.[A-Za-z0-9_-]+\.[0-9a-fA-F]{8}",
        RegexOptions.Compiled | RegexOptions.CultureInvariant);


    public static string Encode(string host, int port, string password = "")
    {
        host = (host ?? string.Empty).Trim();
        if (host.StartsWith("[", StringComparison.Ordinal) && host.EndsWith("]", StringComparison.Ordinal))
        {
            host = host.Substring(1, host.Length - 2);
        }

        if (host.Length == 0)
        {
            throw new ArgumentException("IPv6 地址不能为空");
        }

        if (port is < 1 or > 65535)
        {
            throw new ArgumentException("端口必须在 1..65535 之间");
        }

        password ??= string.Empty;
        if (password.Length > ProtocolConst.MaxPasswordChars)
        {
            throw new ArgumentException($"密码最多 {ProtocolConst.MaxPasswordChars} 个字符");
        }


        // 用 Utf8JsonWriter 手写，既保证和 python 的 json.dumps 逐字节一致，
        // 也不依赖反射（Release 裁剪后照样可用）。
        byte[] raw;
        using (var buffer = new MemoryStream())
        {
            using (var writer = new Utf8JsonWriter(buffer, new JsonWriterOptions
                   {
                       Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
                       Indented = false,
                   }))
            {
                writer.WriteStartObject();
                writer.WriteNumber("v", 1);
                writer.WriteString("host", host);
                writer.WriteNumber("port", port);
                if (password.Length > 0)
                {
                    writer.WriteString("password", password);
                }

                writer.WriteEndObject();
            }

            raw = buffer.ToArray();
        }

        var body = ToBase64Url(raw);
        var checksum = Sha256Hex(body)[..8];
        return $"{Prefix}{body}.{checksum}";
    }

    /// <summary>解析连接配置串；失败抛 <see cref="ConnectionConfigException"/>（消息可直接给用户看）。</summary>
    public static ConnectionConfigInfo Decode(string? token)
    {
        var text = new StringBuilder();
        foreach (var ch in (token ?? string.Empty))
        {
            if (!char.IsWhiteSpace(ch))
            {
                text.Append(ch);
            }
        }

        var normalized = text.ToString();
        var matches = TokenPattern.Matches(normalized);
        if (matches.Count > 0)
        {
            normalized = matches[^1].Value;
        }

        if (!normalized.StartsWith(Prefix, StringComparison.Ordinal))
        {
            throw new ConnectionConfigException("不是有效的连接配置串（应以 FT6. 开头）");
        }

        var parts = normalized.Split('.');
        if (parts.Length != 3 || parts[1].Length == 0 || parts[2].Length != 8)
        {
            throw new ConnectionConfigException("连接配置串格式不完整，请确认整段复制");
        }

        var body = parts[1];
        var checksum = parts[2].ToLowerInvariant();
        var expected = Sha256Hex(body)[..8];
        if (!string.Equals(expected, checksum, StringComparison.Ordinal))
        {
            throw new ConnectionConfigException("连接配置串校验失败，可能复制不完整或已被修改");
        }

        Dictionary<string, object?> data;
        try
        {
            var bytes = FromBase64Url(body);
            data = JsonMsg.ParseObject(bytes);
        }
        catch (ProtocolException ex)
        {
            throw new ConnectionConfigException("连接配置串内容损坏", ex);
        }
        catch (Exception ex)
        {
            throw new ConnectionConfigException("连接配置串内容损坏", ex);
        }

        if (data.GetLong("v") != 1)
        {
            throw new ConnectionConfigException("不支持的连接配置串版本");
        }

        var host = (data.GetStr("host") ?? string.Empty).Trim();
        if (host.StartsWith("[", StringComparison.Ordinal) && host.EndsWith("]", StringComparison.Ordinal))
        {
            host = host.Substring(1, host.Length - 2);
        }

        if (host.Length == 0)
        {
            throw new ConnectionConfigException("连接配置里缺少 IPv6 地址");
        }

        if (!data.TryGetValue("port", out var portValue) || portValue is not long portRaw
            || portRaw is < 1 or > 65535)
        {
            throw new ConnectionConfigException("连接配置里的端口无效");
        }

        var password = data.GetStr("password") ?? string.Empty;
        if (password.Length > ProtocolConst.MaxPasswordChars)
        {
            throw new ConnectionConfigException("连接配置里的密码无效");
        }

        return new ConnectionConfigInfo(host, (int)portRaw, password);
    }

    /// <summary>把一段任意文本里最后一个连接串找出来（用于从聊天记录里导入）。</summary>
    public static string? ExtractToken(string? text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return null;
        }

        var matches = TokenPattern.Matches(text);
        return matches.Count > 0 ? matches[^1].Value : null;
    }

    private static string ToBase64Url(byte[] raw)
        => Convert.ToBase64String(raw).Replace('+', '-').Replace('/', '_').TrimEnd('=');

    private static byte[] FromBase64Url(string body)
    {
        var text = body.Replace('-', '+').Replace('_', '/');
        var padding = (4 - text.Length % 4) % 4;
        if (padding > 0)
        {
            text += new string('=', padding);
        }

        return Convert.FromBase64String(text);
    }

    private static string Sha256Hex(string text)
        => Convert.ToHexString(SHA256.HashData(Encoding.ASCII.GetBytes(text))).ToLowerInvariant();
}
